from typing import Any

import pytest

from _factories import make_work
from _runtime import AmountRule, InvoiceExecutor
from id42_work import WorkRegistrationError, WorkRegistry


def test_register_and_get() -> None:
    registry = WorkRegistry()
    work = make_work()
    binding = registry.register(work, executor=InvoiceExecutor(), validators=[AmountRule()])
    assert registry.get(work.id, work.version) is binding
    assert binding.work is work
    assert binding.definition_digest == work.definition_digest
    assert [v.validator_id for v in binding.validators] == ["amount_rule"]
    assert repr(binding) == "WorkBinding(supplier_invoice_validation@1.0.0)"
    assert [str(r) for r in registry.refs()] == ["supplier_invoice_validation@1.0.0"]


def test_duplicates_and_replacement() -> None:
    registry = WorkRegistry()
    registry.register(make_work(), executor=InvoiceExecutor())
    with pytest.raises(WorkRegistrationError, match="already registered"):
        registry.register(make_work(), executor=InvoiceExecutor())
    replaced = registry.register(
        make_work(name="Renamed"), executor=InvoiceExecutor(), replace=True
    )
    assert registry.get("supplier_invoice_validation", "1.0.0") is replaced
    registry.register(make_work(version="1.1.0"), executor=InvoiceExecutor())
    assert len(registry.refs()) == 2


@pytest.mark.parametrize(
    ("work_id", "version"), [("missing", "1.0.0"), ("Bad Id", "1.0.0"), ("w", "x")]
)
def test_unknown_work(work_id: str, version: str) -> None:
    with pytest.raises(WorkRegistrationError, match="not registered"):
        WorkRegistry().get(work_id, version)


class SyncExecutor:
    def execute(self, input_data: Any, context: Any) -> Any:
        return None


def test_executor_must_be_async() -> None:
    with pytest.raises(WorkRegistrationError, match="async def execute"):
        WorkRegistry().register(make_work(), executor=SyncExecutor())  # type: ignore[arg-type]
    with pytest.raises(WorkRegistrationError, match="async def execute"):
        WorkRegistry().register(make_work(), executor=object())  # type: ignore[arg-type]


def _validator(**attrs: Any) -> Any:
    fields: dict[str, Any] = {"validator_id": "rule", "required": True}
    fields.update(attrs)

    async def validate(self: Any, output: Any, context: Any) -> Any:
        return None

    namespace: dict[str, Any] = {**fields}
    if "validate" not in attrs:
        namespace["validate"] = validate
    return type("V", (), namespace)()


@pytest.mark.parametrize(
    ("validators", "match"),
    [
        ([_validator(validator_id="schema")], "reserved"),
        ([_validator(validator_id="Bad Id")], "validator_id"),
        ([_validator(validator_id=None)], "validator_id"),
        ([_validator(), _validator()], "duplicate"),
        ([_validator(required="yes")], "required"),
        ([_validator(validate=lambda self, o, c: None)], "async def validate"),
    ],
)
def test_invalid_validators(validators: list[Any], match: str) -> None:
    with pytest.raises(WorkRegistrationError, match=match):
        WorkRegistry().register(make_work(), executor=InvoiceExecutor(), validators=validators)
