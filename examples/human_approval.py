"""Human approval checkpoints (specification 7).

Scenarios: pending approval, rejection, approval and resumption, self-approval refused,
an action whose parameters changed after approval, and outcome review.

Run: python examples/human_approval.py
"""

import asyncio
from decimal import Decimal

from _invoice import InvoiceInput, InvoiceOutput, build, check, run, show

from id42_work import (
    ApprovalNotAllowedError,
    AutonomyLevel,
    ExecutionContext,
    ExecutionStatus,
)
from id42_work.testing import INVOICE_PREPARE

S = ExecutionStatus


class DriftingExecutor:
    """Drafts 1250.00 on the first run and 1300.00 on the next: the approval must not carry over."""

    def __init__(self) -> None:
        self.amounts = [Decimal("1250.00"), Decimal("1300.00")]

    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        amount = self.amounts.pop(0) if len(self.amounts) > 1 else self.amounts[0]
        await context.invoke(
            INVOICE_PREPARE,
            action_id="prepare-move",
            params={
                "partner_ref": input_data.supplier_id,
                "invoice_number": input_data.invoice_number,
                "amount": amount,
                "currency": input_data.currency,
            },
        )
        return InvoiceOutput(
            invoice_number=input_data.invoice_number,
            supplier_id=input_data.supplier_id,
            amount=amount,
            currency=input_data.currency,
            status="prepared",
        )


async def main() -> None:
    print("Human approval")

    # SUPERVISED: drafting the move in the ERP is side-effecting, so it needs approval.
    setup = build(autonomy=AutonomyLevel.SUPERVISED, prepare=True)
    waiting = await run(setup, idempotency_key="k-1")
    show("draft move under SUPERVISED", waiting)
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    print(f"    approval for {approval.capability}: {approval.params_preview}")
    check(setup.odoo.count(INVOICE_PREPARE) == 0, "nothing may happen before approval")

    try:
        await setup.runner.approvals.approve(approval.approval_id, reviewer=setup.clerk)
    except ApprovalNotAllowedError as error:
        print(f"- clerk approving own request: refused ({error})")

    await setup.runner.approvals.approve(approval.approval_id, reviewer=setup.reviewer)
    done = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    show("approved and resumed", done)
    check(done.status is S.COMPLETED, "approved execution should complete")
    check(setup.odoo.count(INVOICE_PREPARE) == 1, "exactly one draft")

    setup = build(autonomy=AutonomyLevel.SUPERVISED, prepare=True)
    waiting = await run(setup, idempotency_key="k-2")
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    rejected = await setup.runner.approvals.reject(
        approval.approval_id, reviewer=setup.reviewer, reason="supplier contract expired"
    )
    final = await setup.runner.get(waiting.execution_id)
    assert final is not None
    show(f"rejected ({rejected.reason})", final)
    check(final.status is S.REJECTED, "rejection ends the execution")

    setup = build(autonomy=AutonomyLevel.SUPERVISED, prepare=True, executor=DriftingExecutor())
    waiting = await run(setup, idempotency_key="k-3")
    (first,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    await setup.runner.approvals.approve(first.approval_id, reviewer=setup.reviewer)
    again = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    (second,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    show("amount changed after approval", again)
    print(
        f"    approved {first.params_preview['amount']}, now asks {second.params_preview['amount']}"
    )
    check(again.status is S.WAITING_APPROVAL, "a changed action needs a new approval")
    check(setup.odoo.count(INVOICE_PREPARE) == 0, "the changed action must not run")

    # GUIDED: runs on its own, but a person accepts the verified outcome.
    setup = build(autonomy=AutonomyLevel.GUIDED)
    review = await run(setup)
    show("GUIDED validation", review)
    (approval,) = await setup.runner.approvals.list_pending(review.execution_id)
    await setup.runner.approvals.approve(approval.approval_id, reviewer=setup.reviewer)
    accepted = await setup.runner.get(review.execution_id)
    assert accepted is not None
    show("outcome reviewed", accepted)
    check(accepted.status is S.COMPLETED, "reviewed outcome should complete")

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
