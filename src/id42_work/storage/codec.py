"""JSON codec for records stored by persistent adapters.

Domain records round-trip through Pydantic's JSON mode. ``Execution.outcome.output``
and stored inputs are arbitrary Pydantic models, so their class is recorded as
``module:qualname`` (the same shape as a work schema reference) and imported on load.

The database is trusted host storage: it replaces process memory, and the host already
ran the code that produced these classes.
"""

import importlib
import json
import sys
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from id42_work.exceptions import StorageError
from id42_work.execution import Execution

_ModelT = TypeVar("_ModelT", bound=BaseModel)


def model_ref(model: BaseModel) -> str:
    """Importable reference of a model instance's class."""
    cls = type(model)
    return f"{cls.__module__}:{cls.__qualname__}"


def resolve_model(ref: str) -> type[BaseModel]:
    """Import the Pydantic class named by ``ref``.

    Raises ``StorageError`` when the reference is malformed or does not name a
    ``BaseModel`` subclass. A class already loaded in this process is reused, so a
    script can reload inputs it defined as ``__main__``.
    """
    module_name, separator, qualname = ref.partition(":")
    if separator != ":" or not module_name or not qualname:
        msg = f"invalid model reference {ref!r}"
        raise StorageError(msg)
    module = sys.modules.get(module_name)
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except Exception as error:
            msg = f"cannot import model {ref!r}"
            raise StorageError(msg) from error
    obj: object = module
    try:
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except AttributeError as error:
        msg = f"cannot import model {ref!r}"
        raise StorageError(msg) from error
    if not isinstance(obj, type) or not issubclass(obj, BaseModel):
        msg = f"model reference {ref!r} is not a pydantic model"
        raise StorageError(msg)
    return obj


def dump_record(model: BaseModel) -> str:
    """Serialize ``model`` as compact JSON."""
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def load_record(model: type[_ModelT], payload: str) -> _ModelT:
    """Deserialize ``payload`` as ``model``.

    Raises ``StorageError`` when the JSON is not a valid instance.
    """
    try:
        return model.model_validate_json(payload)
    except ValidationError as error:
        msg = f"stored {model.__name__} is invalid"
        raise StorageError(msg) from error


def dump_execution(execution: Execution) -> str:
    """Serialize an execution, tagging the outcome model when one is present."""
    data = execution.model_dump(mode="json")
    if execution.outcome is not None:
        data["outcome"]["output_model"] = model_ref(execution.outcome.output)
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def load_execution(payload: str) -> Execution:
    """Deserialize an execution written by :func:`dump_execution`."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as error:
        msg = "stored execution is not valid JSON"
        raise StorageError(msg) from error
    if not isinstance(raw, dict):
        msg = "stored execution is not a JSON object"
        raise StorageError(msg)
    data = cast("dict[str, object]", raw)
    outcome = data.get("outcome")
    if outcome is not None:
        data["outcome"] = _restore_outcome(outcome)
    try:
        return Execution.model_validate(data)
    except ValidationError as error:
        msg = "stored execution is invalid"
        raise StorageError(msg) from error


def dump_input(model: BaseModel) -> tuple[str, str]:
    """Return ``(model reference, JSON)`` for a validated input."""
    return model_ref(model), dump_record(model)


def load_input(ref: str, payload: str) -> BaseModel:
    """Rebuild a validated input from the reference stored with it."""
    model = resolve_model(ref)
    return load_record(model, payload)


def _restore_outcome(outcome: object) -> object:
    if not isinstance(outcome, dict):
        msg = "stored outcome is not a JSON object"
        raise StorageError(msg)
    fields = cast("dict[str, object]", outcome)
    ref = fields.pop("output_model", None)
    if not isinstance(ref, str):
        msg = "stored outcome has no output model"
        raise StorageError(msg)
    output = fields.get("output")
    try:
        fields["output"] = resolve_model(ref).model_validate(output)
    except ValidationError as error:
        msg = "stored outcome output is invalid"
        raise StorageError(msg) from error
    return fields
