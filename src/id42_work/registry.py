"""Registry binding work definitions to their executors and validators (spec 5.3)."""

import inspect
import threading
from collections.abc import Iterable
from typing import Any

from id42_work.contracts import Executor, Validator
from id42_work.definitions import Work, WorkRef
from id42_work.exceptions import WorkRegistrationError
from id42_work.identifiers import is_capability
from id42_work.outcomes.validators import SCHEMA_VALIDATOR_ID


class WorkBinding:
    """Immutable association of a work definition with its runtime implementation."""

    __slots__ = ("_definition_digest", "_executor", "_validators", "_work")

    def __init__(
        self, work: Work, executor: Executor[Any, Any], validators: tuple[Validator, ...]
    ) -> None:
        self._work = work
        self._executor = executor
        self._validators = validators
        self._definition_digest = work.definition_digest

    @property
    def work(self) -> Work:
        return self._work

    @property
    def executor(self) -> Executor[Any, Any]:
        return self._executor

    @property
    def validators(self) -> tuple[Validator, ...]:
        """User validators, in registration order (the schema validator always runs first)."""
        return self._validators

    @property
    def definition_digest(self) -> str:
        """Digest of the work definition, computed once at registration."""
        return self._definition_digest

    def __repr__(self) -> str:
        return f"WorkBinding({self._work.ref})"


class WorkRegistry:
    """Thread-safe registry of ``WorkBinding`` objects keyed by ``(id, version)``."""

    def __init__(self) -> None:
        self._bindings: dict[WorkRef, WorkBinding] = {}
        self._lock = threading.Lock()

    def register(
        self,
        work: Work,
        *,
        executor: Executor[Any, Any],
        validators: Iterable[Validator] = (),
        replace: bool = False,
    ) -> WorkBinding:
        """Bind ``work`` to ``executor`` and ``validators``.

        Raises ``WorkRegistrationError`` for a duplicate ``(id, version)`` (unless
        ``replace=True``), an executor without an async ``execute`` method, or invalid
        validators. Executions that recorded a different definition digest cannot be
        resumed after a replacement.
        """
        if not inspect.iscoroutinefunction(getattr(executor, "execute", None)):
            msg = f"executor for {work.ref} must define 'async def execute(...)'"
            raise WorkRegistrationError(msg)
        checked = _check_validators(work, tuple(validators))
        binding = WorkBinding(work, executor, checked)
        with self._lock:
            if work.ref in self._bindings and not replace:
                msg = f"work {work.ref} is already registered"
                raise WorkRegistrationError(msg)
            self._bindings[work.ref] = binding
        return binding

    def get(self, work_id: str, version: str) -> WorkBinding:
        """Return the binding for ``(work_id, version)``.

        Raises ``WorkRegistrationError`` if it is not registered.
        """
        with self._lock:
            ref = _ref_or_none(work_id, version)
            binding = self._bindings.get(ref) if ref is not None else None
        if binding is not None:
            return binding
        msg = f"work {work_id}@{version} is not registered"
        raise WorkRegistrationError(msg)

    def refs(self) -> tuple[WorkRef, ...]:
        """Registered definitions, sorted by id and version string."""
        with self._lock:
            return tuple(sorted(self._bindings, key=lambda r: (r.id, r.version)))


def _check_validators(work: Work, validators: tuple[Validator, ...]) -> tuple[Validator, ...]:
    seen: set[str] = set()
    for validator in validators:
        validator_id = getattr(validator, "validator_id", None)
        if not isinstance(validator_id, str) or not is_capability(validator_id):
            msg = f"validator {validator!r} of {work.ref} needs a valid 'validator_id'"
            raise WorkRegistrationError(msg)
        if validator_id == SCHEMA_VALIDATOR_ID:
            msg = f"validator id {SCHEMA_VALIDATOR_ID!r} is reserved for the built-in validator"
            raise WorkRegistrationError(msg)
        if validator_id in seen:
            msg = f"duplicate validator id {validator_id!r} for {work.ref}"
            raise WorkRegistrationError(msg)
        if not isinstance(getattr(validator, "required", None), bool):
            msg = f"validator {validator_id!r} needs a boolean 'required' attribute"
            raise WorkRegistrationError(msg)
        if not inspect.iscoroutinefunction(getattr(validator, "validate", None)):
            msg = f"validator {validator_id!r} must define 'async def validate(...)'"
            raise WorkRegistrationError(msg)
        seen.add(validator_id)
    return validators


def _ref_or_none(work_id: str, version: str) -> WorkRef | None:
    try:
        return WorkRef(id=work_id, version=version)
    except ValueError:
        return None
