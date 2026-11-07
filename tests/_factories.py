"""Builders for domain records used across unit tests."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue

from id42_work import (
    AutonomyLevel,
    CapabilityCall,
    CapabilitySpec,
    Execution,
    ExecutionStatus,
    Outcome,
    Policy,
    Severity,
    ValidationResult,
    Work,
)

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class InvoiceInput(BaseModel):
    invoice_number: str
    supplier_id: str
    currency: str
    amount: Decimal


class InvoiceOutput(BaseModel):
    invoice_number: str
    supplier_id: str
    status: str


def make_work(**overrides: Any) -> Work:
    fields: dict[str, Any] = {
        "id": "supplier_invoice_validation",
        "version": "1.0.0",
        "name": "Supplier Invoice Validation",
        "input_schema": InvoiceInput,
        "output_schema": InvoiceOutput,
        "required_capabilities": frozenset({"suppliers.read", "invoices.validate"}),
    }
    fields.update(overrides)
    return Work(**fields)


def make_output() -> InvoiceOutput:
    return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")


def make_outcome(
    execution_id: UUID,
    *,
    passed: bool = True,
    accepted: bool = False,
    extra: tuple[ValidationResult, ...] = (),
) -> Outcome:
    results = (
        ValidationResult(
            validator_id="schema", passed=passed, severity=Severity.ERROR, message="schema"
        ),
        *extra,
    )
    verified = all(r.passed for r in results if r.required)
    return Outcome(
        execution_id=execution_id,
        output=make_output(),
        verified=verified,
        validation_results=results,
        produced_at=T0,
        accepted_at=T0 + timedelta(seconds=1) if accepted else None,
    )


def make_execution(**overrides: Any) -> Execution:
    execution_id = overrides.pop("execution_id", uuid4())
    status: ExecutionStatus = overrides.get("status", ExecutionStatus.CREATED)
    fields: dict[str, Any] = {
        "execution_id": execution_id,
        "work_id": "supplier_invoice_validation",
        "work_version": "1.0.0",
        "definition_digest": DIGEST_A,
        "actor_id": "user:42",
        "tenant_id": "acme",
        "input_fingerprint": DIGEST_B,
        "input_reference": f"memory://{execution_id}",
        "created_at": T0,
        "updated_at": T0,
        "finished_at": T0 if status.is_terminal else None,
    }
    fields.update(overrides)
    return Execution(**fields)


# --- policy and capability helpers -------------------------------------------------


class StaticCapabilities:
    """Provider declaring fixed capabilities; ``invoke`` echoes its params."""

    def __init__(self, *specs: tuple[str, bool]) -> None:
        self._specs = [CapabilitySpec(name=n, side_effecting=s) for n, s in specs]

    def capabilities(self) -> list[CapabilitySpec]:
        return list(self._specs)

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        return {"capability": capability}


def make_policy(**overrides: Any) -> Policy:
    fields: dict[str, Any] = {
        "id": "invoice_review",
        "version": "1.0.0",
        "autonomy": AutonomyLevel.AUTONOMOUS,
        "allowed_capabilities": frozenset({"suppliers.read", "invoices.validate"}),
    }
    fields.update(overrides)
    return Policy(**fields)


class FixedClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now
