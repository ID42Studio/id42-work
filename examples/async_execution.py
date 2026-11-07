"""Concurrent executions and the synchronous API.

Twenty requests run at once: ten distinct invoices and ten retries of one invoice. The
retries share an idempotency key, so the ERP drafts that invoice once.

Run: python examples/async_execution.py
"""

import asyncio

from _invoice import INVOICE, VERSION, WORK_ID, Setup, build, check, run

from id42_work import ExecutionStatus
from id42_work.testing import INVOICE_PREPARE


async def main(setup: Setup) -> None:
    print("Concurrent executions")
    distinct = [
        run(setup, {**INVOICE, "invoice_number": f"INV-{2000 + i}"}, idempotency_key=f"inv-{i}")
        for i in range(10)
    ]
    retries = [run(setup, idempotency_key="inv-retried") for _ in range(10)]
    results = await asyncio.gather(*distinct, *retries)
    retried = {e.execution_id for e in results[10:]}
    print(f"- executions for distinct invoices: {len({e.execution_id for e in results[:10]})}")
    print(f"- executions for 10 retries:        {len(retried)}")
    print(f"- drafts in the ERP:                {setup.odoo.count(INVOICE_PREPARE)}")
    check(len(retried) == 1, "retries must share one execution")
    check(setup.odoo.count(INVOICE_PREPARE) == 11, "11 drafts expected")
    check(all(e.status is ExecutionStatus.COMPLETED for e in results), "all should complete")


if __name__ == "__main__":
    setup = build(prepare=True)
    asyncio.run(main(setup))

    # The sync API runs its own event loop; never call it from inside one.
    execution = setup.runner.run_sync(
        work_id=WORK_ID,
        version=VERSION,
        input_data={**INVOICE, "invoice_number": "INV-3000"},
        actor=setup.clerk,
        idempotency_key="inv-sync",
    )
    print(f"- run_sync: {execution.status.value}")
    check(execution.status is ExecutionStatus.COMPLETED, "sync run should complete")
    print("OK")
