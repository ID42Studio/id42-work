"""Posting a supplier bill through a (fake) Odoo 18 adapter.

The work reads the partner, drafts the move and posts it. Posting is a separate,
high-risk capability: the policy runs AUTONOMOUS, yet an explicit requirement makes
every post wait for finance approval with a reason. The second scenario shows an
uncertain post: the SDK stops, flags reconciliation and never retries.

Run: python examples/fake_odoo_invoice.py
"""

import asyncio
from datetime import timedelta

from _invoice import InvoiceInput, InvoiceOutput, Setup, build, check, run, show

from id42_work import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    ExecutionContext,
    ExecutionStatus,
)
from id42_work.testing import INVOICE_POST, INVOICE_PREPARE, PARTNER_READ

S = ExecutionStatus


class PostBill:
    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        partner = await context.invoke(
            PARTNER_READ, action_id="read-partner", params={"ref": input_data.supplier_id}
        )
        assert isinstance(partner, dict)
        if not partner["active"]:
            msg = f"partner {input_data.supplier_id} is blocked"
            raise ValueError(msg)
        move = await context.invoke(
            INVOICE_PREPARE,
            action_id="draft-bill",
            params={
                "partner_ref": input_data.supplier_id,
                "invoice_number": input_data.invoice_number,
                "amount": input_data.amount,
                "currency": input_data.currency,
            },
        )
        assert isinstance(move, dict)
        posted = await context.invoke(
            INVOICE_POST, action_id="post-bill", params={"move_id": move["move_id"]}
        )
        assert isinstance(posted, dict)
        return InvoiceOutput(
            invoice_number=input_data.invoice_number,
            supplier_id=input_data.supplier_id,
            amount=input_data.amount,
            currency=input_data.currency,
            status=str(posted["state"]),
            move_id=int(str(posted["move_id"])),
        )


def build_poster() -> Setup:
    post_needs_finance = ApprovalRequirement(
        gate=ApprovalGate.ACTION,
        capability=INVOICE_POST,
        reviewer_capability="approve:odoo.invoice.post",
        reason_required=True,
        ttl=timedelta(hours=4),
    )
    return build(
        autonomy=AutonomyLevel.AUTONOMOUS,
        requirements=(post_needs_finance,),
        executor=PostBill(),
        required=frozenset({PARTNER_READ, INVOICE_PREPARE, INVOICE_POST}),
    )


async def main() -> None:
    print("Odoo bill posting")

    setup = build_poster()
    waiting = await run(setup, idempotency_key="bill-INV-1001")
    show("draft created, post waiting", waiting)
    check(
        setup.odoo.count(INVOICE_PREPARE) == 1 and setup.odoo.count(INVOICE_POST) == 0, "post gated"
    )
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    print(f"    finance must approve {approval.capability} for {approval.params_preview}")

    await setup.runner.approvals.approve(
        approval.approval_id, reviewer=setup.reviewer, reason="matched PO-778 and receipt"
    )
    posted = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    show("approved and posted", posted)
    check(posted.status is S.COMPLETED, "post should complete")
    check(setup.odoo.count(INVOICE_PREPARE) == 1, "the draft is replayed, not created twice")
    print(f"    ERP moves: {list(setup.odoo.moves.values())}")

    setup = build_poster()
    setup.odoo.fail_with_uncertain_outcome(INVOICE_POST)
    waiting = await run(setup, idempotency_key="bill-INV-1001")
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    await setup.runner.approvals.approve(
        approval.approval_id, reviewer=setup.reviewer, reason="matched PO-778"
    )
    uncertain = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    show("ERP connection lost during post", uncertain)
    check(uncertain.requires_reconciliation, "must require reconciliation")
    journal = await setup.runner.journal(uncertain.execution_id)
    print(f"    journal: {[(j.action_id, j.status.value) for j in journal]}")
    print("    -> reconcile with the ERP using the operation id; the SDK never retries")

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
