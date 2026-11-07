"""Shared pieces of the invoice examples: schemas, executor, validators and wiring.

Not an example on its own. The other examples import it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, Field

from id42_work import (
    ActorAuthority,
    ApprovalRequirement,
    AutonomyLevel,
    CapabilityRegistry,
    Execution,
    ExecutionContext,
    MemoryStorage,
    Policy,
    Severity,
    StaticPolicyResolver,
    TrustedActor,
    ValidationContext,
    ValidationResult,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.testing import (
    INVOICE_POST,
    INVOICE_PREPARE,
    PARTNER_READ,
    FakeOdooProvider,
    ManualClock,
)

WORK_ID = "supplier_invoice_validation"
VERSION = "1.0.0"
REVIEWER_GRANT = f"approve:{WORK_ID}"
INVOICE = {
    "invoice_number": "INV-1001",
    "supplier_id": "SUP-001",
    "currency": "BRL",
    "amount": "1250.00",
}


class InvoiceInput(BaseModel):
    invoice_number: str = Field(pattern=r"^INV-\d{4}$")
    supplier_id: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Decimal


class InvoiceOutput(BaseModel):
    invoice_number: str
    supplier_id: str
    amount: Decimal
    currency: str
    status: str
    move_id: int | None = None


class ValidateInvoice:
    """Checks the supplier in the ERP; with ``prepare=True`` it also drafts the move."""

    def __init__(self, *, prepare: bool = False) -> None:
        self.prepare = prepare

    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        partner = await context.invoke(
            PARTNER_READ, action_id="read-supplier", params={"ref": input_data.supplier_id}
        )
        assert isinstance(partner, dict)
        status = "valid" if partner["active"] else "supplier_blocked"
        move_id = None
        if self.prepare and status == "valid":
            move = await context.invoke(
                INVOICE_PREPARE,
                action_id="prepare-move",
                params={
                    "partner_ref": input_data.supplier_id,
                    "invoice_number": input_data.invoice_number,
                    "amount": input_data.amount,
                    "currency": input_data.currency,
                },
            )
            assert isinstance(move, dict)
            move_id = int(str(move["move_id"]))
            status = "prepared"
        return InvoiceOutput(
            invoice_number=input_data.invoice_number,
            supplier_id=input_data.supplier_id,
            amount=input_data.amount,
            currency=input_data.currency,
            status=status,
            move_id=move_id,
        )


class AmountIsPositive:
    validator_id = "amount_is_positive"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        assert isinstance(output, InvoiceOutput)
        passed = output.amount > 0
        return ValidationResult(
            validator_id=self.validator_id,
            passed=passed,
            severity=Severity.INFO if passed else Severity.ERROR,
            message="amount is positive" if passed else f"amount {output.amount} is not positive",
        )


class SupplierIsActive:
    """Re-checks the supplier through a read-only capability."""

    validator_id = "supplier_is_active"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        assert isinstance(output, InvoiceOutput)
        partner = await context.invoke(
            PARTNER_READ, action_id="recheck-supplier", params={"ref": output.supplier_id}
        )
        passed = isinstance(partner, dict) and partner["active"] is True
        return ValidationResult(
            validator_id=self.validator_id,
            passed=passed,
            severity=Severity.INFO if passed else Severity.ERROR,
            message="supplier is active" if passed else "supplier is blocked",
        )


@dataclass
class Setup:
    runner: WorkRunner
    authority: ActorAuthority
    odoo: FakeOdooProvider
    clock: ManualClock
    clerk: TrustedActor
    reviewer: TrustedActor

    def issue(self, actor_id: str, grants: Iterable[str]) -> TrustedActor:
        return _issue(self.authority, actor_id, grants)


def build(
    *,
    autonomy: AutonomyLevel = AutonomyLevel.AUTONOMOUS,
    prepare: bool = False,
    requirements: tuple[ApprovalRequirement, ...] = (),
    with_policy: bool = True,
    executor: object | None = None,
    required: frozenset[str] | None = None,
) -> Setup:
    """Wire a runner with in-memory storage, a manual clock and the fake Odoo adapter."""
    if required is None:
        required = frozenset({PARTNER_READ, INVOICE_PREPARE} if prepare else {PARTNER_READ})
    work = Work(
        id=WORK_ID,
        version=VERSION,
        name="Supplier Invoice Validation",
        description="Validate a supplier invoice before preparing an ERP operation.",
        input_schema=InvoiceInput,
        output_schema=InvoiceOutput,
        required_capabilities=frozenset(required),
    )
    registry = WorkRegistry()
    registry.register(
        work,
        executor=executor or ValidateInvoice(prepare=prepare),  # type: ignore[arg-type]
        validators=[AmountIsPositive(), SupplierIsActive()],
    )
    policy = Policy(
        id="invoice_review",
        version="1.0.0",
        autonomy=autonomy,
        allowed_capabilities=frozenset({PARTNER_READ, INVOICE_PREPARE, INVOICE_POST}),
        approval_requirements=requirements,
    )
    odoo = FakeOdooProvider()
    capabilities = CapabilityRegistry()
    capabilities.add(odoo)
    clock = ManualClock()
    authority = ActorAuthority("invoice-app", clock=clock)
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver({(WORK_ID, VERSION): policy} if with_policy else {}),
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
        clock=clock,
    )
    clerk = _issue(authority, "clerk:ana", {PARTNER_READ, INVOICE_PREPARE, INVOICE_POST})
    reviewer = _issue(authority, "finance:bruno", {REVIEWER_GRANT, "approve:odoo.invoice.post"})
    return Setup(runner, authority, odoo, clock, clerk, reviewer)


def _issue(authority: ActorAuthority, actor_id: str, grants: Iterable[str]) -> TrustedActor:
    return authority.issue(
        actor_id=actor_id, tenant_id="acme", grants=set(grants), ttl=timedelta(hours=1)
    )


async def run(setup: Setup, data: dict[str, str] | None = None, **kwargs: object) -> Execution:
    kwargs.setdefault("actor", setup.clerk)
    return await setup.runner.run(
        work_id=WORK_ID,
        version=VERSION,
        input_data=data or INVOICE,
        **kwargs,  # type: ignore[arg-type]
    )


def show(title: str, execution: Execution) -> None:
    reason = execution.status_reason or ""
    if execution.policy_reasons:
        reason = ", ".join(execution.policy_reasons)
    print(f"- {title}: {execution.status.value}" + (f" ({reason})" if reason else ""))


def check(condition: bool, message: str) -> None:
    """Fail the example (exit code 1) if an expectation does not hold."""
    if not condition:
        raise SystemExit(f"FAILED: {message}")
