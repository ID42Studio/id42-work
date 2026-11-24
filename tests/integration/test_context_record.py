"""context.record: journaled non-deterministic computations (and task-group executors)."""

import asyncio
import contextlib
from typing import Any

import pytest

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import PREPARE, READ, Env, FunctionExecutor
from id42_work import (
    AutonomyLevel,
    ExecutionContext,
    ExecutionLimits,
    ExecutionStatus,
    WorkRegistrationError,
)
from id42_work.identifiers import RECORD_CAPABILITY
from id42_work.journal import JournalStatus

S = ExecutionStatus
REVIEW = "approve:supplier_invoice_validation"
OK = InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")


def _supervised(body: Any) -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED, allowed_capabilities=frozenset({READ, PREPARE})
        ),
    )


async def _approve_and_resume(env: Env, execution: Any) -> Any:
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    await env.runner.approvals.approve(
        approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    return await env.runner.resume(execution.execution_id, actor=env.actor)


async def test_recorded_results_are_replayed_not_recomputed() -> None:
    computed: list[int] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def think() -> Any:
            computed.append(1)
            return {"decision": "prepare", "confidence": 0.93}

        decision = await context.record("plan:1", fingerprint={"prompt": "p"}, compute=think)
        assert decision == {"decision": "prepare", "confidence": 0.93}
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    assert waiting.status is S.WAITING_APPROVAL
    done = await _approve_and_resume(env, waiting)
    assert done.status is S.COMPLETED
    assert computed == [1]
    (record, _) = await env.runner.journal(waiting.execution_id)
    assert (record.capability, record.status) == (RECORD_CAPABILITY, JournalStatus.SUCCEEDED)
    types = [e.event_type.value for e in await env.runner.events(waiting.execution_id)]
    assert "work.action.replayed" in types


async def test_a_different_fingerprint_on_replay_fails() -> None:
    runs = {"n": 0}

    async def body(data: Any, context: ExecutionContext) -> Any:
        runs["n"] += 1

        async def think() -> Any:
            return "x"

        await context.record("plan:1", fingerprint={"run": runs["n"]}, compute=think)
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    failed = await _approve_and_resume(env, await env.run(idempotency_key="k"))
    assert failed.status_reason == "non_deterministic"


async def test_a_failed_record_raises_again_on_replay() -> None:
    attempts: list[str] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def flaky() -> Any:
            attempts.append("called")
            raise ConnectionError("model down")

        try:
            await context.record("plan:1", fingerprint="p", compute=flaky)
        except Exception as error:  # noqa: BLE001
            attempts.append(type(error).__name__)
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    done = await _approve_and_resume(env, await env.run(idempotency_key="k"))
    assert done.status is S.COMPLETED
    assert attempts == ["called", "ConnectionError", "ExecutionFailedError"]


async def test_interrupted_record_is_computed_again() -> None:
    computed: list[int] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def think() -> Any:
            computed.append(1)
            return "plan"

        await context.record("plan:1", fingerprint="p", compute=think)
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    (record, _) = await env.runner.journal(waiting.execution_id)
    interrupted = record.model_copy(
        update={
            "status": JournalStatus.IN_FLIGHT,
            "result": None,
            "result_digest": None,
            "finished_at": None,
        }
    )
    await env.storage.executions.update_journal(
        interrupted, expected_status=JournalStatus.SUCCEEDED
    )
    done = await _approve_and_resume(env, waiting)
    assert done.status is S.COMPLETED
    assert computed == [1, 1]


@pytest.mark.parametrize(
    ("fingerprint", "result", "reason"),
    [
        (object(), "fine", "invalid_record_fingerprint"),
        ("p", {"amount": object()}, "invalid_record_result"),
    ],
)
async def test_bad_fingerprints_and_results_fail_the_execution(
    fingerprint: object, result: object, reason: str
) -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            return result

        with contextlib.suppress(Exception):  # the violation sticks anyway
            await context.record("plan:1", fingerprint=fingerprint, compute=compute)
        return OK

    execution = await Env(executor=FunctionExecutor(body)).run()
    assert execution.status is S.FAILED
    assert execution.status_reason == reason


async def test_records_share_action_ids_and_limits_with_invoke() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            return 1

        await context.record("step", fingerprint="a", compute=compute)
        await context.invoke(READ, action_id="step", params={"supplier_id": "S"})
        return OK

    duplicate = await Env(executor=FunctionExecutor(body)).run()
    assert duplicate.status_reason == "duplicate_action_id"

    async def reversed_order(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            return 1

        await context.invoke(READ, action_id="step", params={"supplier_id": "S"})
        await context.record("step", fingerprint="a", compute=compute)
        return OK

    duplicate_record = await Env(executor=FunctionExecutor(reversed_order)).run()
    assert duplicate_record.status_reason == "duplicate_action_id"

    async def many(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            return 1

        for i in range(3):
            await context.record(f"r{i}", fingerprint=i, compute=compute)
        return OK

    limited = Env(
        executor=FunctionExecutor(many),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, "invoices.validate"}),
            execution_limits=ExecutionLimits(max_actions=2),
        ),
    )
    assert (await limited.run()).status_reason == "max_actions_exceeded"

    async def invalid(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            return 1

        await context.record("_bad", fingerprint="a", compute=compute)
        return OK

    assert (
        await Env(executor=FunctionExecutor(invalid)).run()
    ).status_reason == "invalid_action_id"


async def test_record_after_suspension_is_refused() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        with contextlib.suppress(BaseException):  # hostile executor
            await context.invoke(PREPARE, action_id="prepare", params={})

        async def compute() -> Any:
            raise AssertionError("must not run")

        await context.record("plan:1", fingerprint="p", compute=compute)
        return OK

    env = _supervised(body)
    assert (await env.run(idempotency_key="k")).status is S.WAITING_APPROVAL


async def test_cancelled_record_is_marked_failed() -> None:
    started = asyncio.Event()

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def slow() -> Any:
            started.set()
            await asyncio.sleep(10)

        await context.record("plan:1", fingerprint="p", compute=slow)
        return OK

    env = Env(executor=FunctionExecutor(body))
    task = asyncio.create_task(env.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    (execution_id,) = env.storage.executions._executions
    (entry,) = await env.runner.journal(execution_id)
    assert entry.status is JournalStatus.FAILED


async def test_task_group_suspension_waits_for_approval() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async with asyncio.TaskGroup() as group:
            group.create_task(context.invoke(READ, action_id="read", params={"supplier_id": "S"}))
            group.create_task(context.invoke(PREPARE, action_id="prepare", params={}))
        return OK

    env = _supervised(body)
    assert (await env.run(idempotency_key="k")).status is S.WAITING_APPROVAL


async def test_task_group_errors_fail_the_execution() -> None:
    async def plain(data: Any, context: ExecutionContext) -> Any:
        async def boom() -> None:
            raise ValueError("bad")

        async with asyncio.TaskGroup() as group:
            group.create_task(boom())
        return OK

    failed = await Env(executor=FunctionExecutor(plain)).run()
    assert failed.status is S.FAILED
    assert failed.error is not None
    assert failed.error.type == "ExceptionGroup"

    async def odd(data: Any, context: ExecutionContext) -> Any:
        raise BaseExceptionGroup("odd", [GeneratorExit()])

    odd_failure = await Env(executor=FunctionExecutor(odd)).run()
    assert odd_failure.status is S.FAILED
    assert odd_failure.error is not None
    assert odd_failure.error.type == "ExecutionFailedError"

    async def interrupt(data: Any, context: ExecutionContext) -> Any:
        raise BaseExceptionGroup("stop", [KeyboardInterrupt()])

    with pytest.raises(BaseExceptionGroup):
        await Env(executor=FunctionExecutor(interrupt)).run()


def test_reserved_namespace_cannot_be_provided() -> None:
    from id42_work import CapabilityRegistry, CapabilitySpec  # noqa: PLC0415

    class Sneaky:
        def capabilities(self) -> list[CapabilitySpec]:
            return [CapabilitySpec(name="id42.record", side_effecting=False)]

        async def invoke(self, capability: str, params: Any, call: Any) -> Any:  # pragma: no cover
            return None

    with pytest.raises(WorkRegistrationError, match="reserved"):
        CapabilityRegistry().add(Sneaky())
