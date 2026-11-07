"""Concurrent duplicate requests (specification 12.2)."""

import asyncio
import threading
from typing import Any

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import INVOICE, PREPARE, READ, VALIDATE, Env, FunctionExecutor
from id42_work import AutonomyLevel, Execution, ExecutionContext, ExecutionStatus


def _env() -> Env:
    async def body(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await asyncio.sleep(0.01)  # yield so other requests interleave
        await context.invoke(PREPARE, action_id="prepare", params={"invoice": data.invoice_number})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="prepared")

    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
        ),
    )


async def test_concurrent_tasks_create_one_execution() -> None:
    env = _env()
    results = await asyncio.gather(*(env.run(idempotency_key="same") for _ in range(25)))
    assert len({r.execution_id for r in results}) == 1
    assert env.provider.count(PREPARE) == 1
    assert env.executor.runs == 1
    final = await env.runner.get(results[0].execution_id)
    assert final is not None
    assert final.status is ExecutionStatus.COMPLETED


def test_concurrent_threads_create_one_execution() -> None:
    env = _env()
    results: list[Execution] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        try:
            execution = env.runner.run_sync(
                work_id=env.work.id,
                version=env.work.version,
                input_data=INVOICE,
                actor=env.actor,
                idempotency_key="same",
            )
        except BaseException as error:  # noqa: BLE001
            with lock:
                errors.append(error)
            return
        with lock:
            results.append(execution)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len({r.execution_id for r in results}) == 1
    assert env.provider.count(PREPARE) == 1


async def test_distinct_keys_run_independently() -> None:
    env = _env()
    results = await asyncio.gather(*(env.run(idempotency_key=f"k-{i}") for i in range(10)))
    assert len({r.execution_id for r in results}) == 10
    assert env.provider.count(PREPARE) == 10
