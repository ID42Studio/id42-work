"""Supplier invoice validation without AI (specification 16).

Scenarios: success, policy rejection, missing capability, invalid input, invalid
outcome, executor exception, idempotent retry and conflicting idempotency key.

Run: python examples/invoice_validation.py
"""

import asyncio

from _invoice import INVOICE, ValidateInvoice, build, check, run, show

from id42_work import (
    ExecutionContext,
    ExecutionStatus,
    IdempotencyConflictError,
    InputValidationError,
)
from id42_work.testing import INVOICE_PREPARE, PARTNER_READ

S = ExecutionStatus


class BrokenExecutor(ValidateInvoice):
    async def execute(self, input_data, context: ExecutionContext):  # type: ignore[override]  # noqa: ANN001, ANN201
        raise ConnectionRefusedError("ERP gateway refused the connection")


async def main() -> None:
    print("Supplier invoice validation")

    setup = build()
    done = await run(setup)
    show("valid invoice", done)
    outcome = done.raise_for_status()
    check(outcome.verified, "outcome should be verified")
    print(f"    validators: {[r.validator_id for r in outcome.validation_results]}")
    evidence = await setup.runner.evidence(done.execution_id)
    print(f"    evidence:   {[e.evidence_type.value for e in evidence]}")

    denied = await run(build(with_policy=False))
    show("no policy for the work", denied)
    check(denied.status is S.DENIED and denied.policy_reasons == ("no_policy",), "policy rejection")

    setup = build()
    no_grant = setup.issue("intern:caio", set())
    missing = await run(setup, actor=no_grant)
    show("actor without odoo.partner.read", missing)
    check(missing.policy_reasons == (f"missing_capability:{PARTNER_READ}",), "missing capability")

    try:
        await run(build(), {**INVOICE, "invoice_number": "1001"})
    except InputValidationError as error:
        print(f"- malformed input: InputValidationError ({str(error).splitlines()[0]})")
    else:
        check(False, "malformed input should be rejected")

    negative = await run(build(), {**INVOICE, "amount": "-50.00"})
    show("negative amount", negative)
    check(negative.status is S.VALIDATION_FAILED, "invalid outcome must not complete")
    assert negative.outcome is not None
    print(f"    failed: {[r.message for r in negative.outcome.failed_results]}")

    blocked = await run(build(), {**INVOICE, "supplier_id": "SUP-002"})
    show("blocked supplier", blocked)
    check(blocked.status is S.VALIDATION_FAILED, "blocked supplier must not complete")

    crashed = await run(build(executor=BrokenExecutor()))
    show("executor exception", crashed)
    check(crashed.status is S.FAILED and crashed.error is not None, "executor failure")
    assert crashed.error is not None
    print(f"    error: {crashed.error.type}: {crashed.error.message}")

    setup = build(prepare=True)
    first = await run(setup, idempotency_key="invoice-SUP-001-INV-1001")
    retry = await run(setup, idempotency_key="invoice-SUP-001-INV-1001")
    show("idempotent retry", retry)
    check(retry.execution_id == first.execution_id, "retry must return the same execution")
    check(setup.odoo.count(INVOICE_PREPARE) == 1, "the move must be drafted once")
    print(f"    drafts created in the ERP: {setup.odoo.count(INVOICE_PREPARE)}")

    try:
        await run(
            setup, {**INVOICE, "amount": "1300.00"}, idempotency_key="invoice-SUP-001-INV-1001"
        )
    except IdempotencyConflictError:
        print("- same key, different amount: IdempotencyConflictError")
    else:
        check(False, "conflicting key must be rejected")

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
