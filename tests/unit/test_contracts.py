"""Structural conformance of user classes to the SDK protocols.

These assignments are checked statically by pyright and exercised at runtime.
"""

from collections.abc import Iterable, Mapping

import pytest
from _factories import T0, InvoiceInput, InvoiceOutput, make_work
from pydantic import BaseModel, JsonValue

from id42_work import (
    ActorAuthority,
    CapabilityCall,
    CapabilityProvider,
    CapabilitySpec,
    ExecutionContext,
    Executor,
    Policy,
    PolicyResolver,
    Severity,
    TrustedActor,
    ValidationContext,
    ValidationResult,
    Validator,
    Work,
)


class InvoiceExecutor:
    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        supplier = await context.invoke(
            "suppliers.read", action_id="load-supplier", params={"id": input_data.supplier_id}
        )
        assert isinstance(supplier, dict)
        return InvoiceOutput(
            invoice_number=input_data.invoice_number,
            supplier_id=str(supplier["id"]),
            status="valid",
        )


class AmountRule:
    validator_id = "amount_rule"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        return ValidationResult(validator_id=self.validator_id, passed=True, severity=Severity.INFO)


class FakeSuppliers:
    def capabilities(self) -> Iterable[CapabilitySpec]:
        return [CapabilitySpec(name="suppliers.read", side_effecting=False)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        return {"id": str(params["id"])}


class NoPolicies:
    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None:
        return None


executor: Executor[InvoiceInput, InvoiceOutput] = InvoiceExecutor()
validator: Validator = AmountRule()
provider: CapabilityProvider = FakeSuppliers()
resolver: PolicyResolver = NoPolicies()


@pytest.mark.asyncio
async def test_provider_and_validator_shapes() -> None:
    call = CapabilityCall(
        execution_id=__import__("uuid").uuid4(),
        action_id="load-supplier",
        operation_id=__import__("uuid").uuid4(),
        actor_id="user:42",
    )
    assert await provider.invoke("suppliers.read", {"id": "SUP-001"}, call) == {"id": "SUP-001"}
    assert [spec.name for spec in provider.capabilities()] == ["suppliers.read"]
    authority = ActorAuthority("host-app", clock=lambda: T0)
    actor = authority.issue(actor_id="a", ttl=__import__("datetime").timedelta(minutes=1))
    assert resolver.resolve(make_work(), actor) is None
    assert validator.required
