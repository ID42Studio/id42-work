"""context.record: journaled non-deterministic computations (and task-group executors)."""

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import PREPARE, READ, Env, FunctionExecutor
from id42_work import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    ExecutionContext,
    ExecutionLimits,
    ExecutionStatus,
    WorkRegistrationError,
)
from id42_work.context import ExecutionSuspended
from id42_work.execution import ExecutionErrorInfo
from id42_work.identifiers import RECORD_CAPABILITY
from id42_work.journal import JournalEntry, JournalStatus

S = ExecutionStatus
REVIEW = "approve:supplier_invoice_validation"
OK = InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")


def _inflight(entry: JournalEntry) -> JournalEntry:
    return entry.model_copy(
        update={
            "status": JournalStatus.IN_FLIGHT,
            "result": None,
            "result_digest": None,
            "finished_at": None,
        }
    )


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


async def test_a_crashed_record_is_recomputed_and_a_completed_call_is_not() -> None:
    computed: list[int] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def think() -> Any:
            computed.append(1)
            return "plan"

        await context.record("plan:1", fingerprint="p", compute=think)
        await context.invoke(READ, action_id="read", params={"supplier_id": "SUP-001"})
        return OK

    env = Env(executor=FunctionExecutor(body))
    done = await env.run(idempotency_key="k")
    assert done.status is S.COMPLETED
    (record, read) = await env.runner.journal(done.execution_id)
    await env.storage.executions.update_journal(
        _inflight(record), expected_status=JournalStatus.SUCCEEDED
    )
    running = done.model_copy(
        update={
            "status": S.RUNNING,
            "finished_at": None,
            "outcome": None,
            "status_reason": None,
            "revision": done.revision + 1,
        }
    )
    await env.storage.executions.compare_and_set(running)
    again = await env.run(idempotency_key="k")
    assert again.execution_id == done.execution_id
    assert again.status is S.RUNNING
    assert computed == [1]

    resumed = await env.runner.resume(done.execution_id, actor=env.actor)
    assert resumed.status is S.COMPLETED
    assert computed == [1, 1]
    assert env.provider.count(READ) == 1
    entries = await env.runner.journal(done.execution_id)
    journal = {entry.action_id: entry.status for entry in entries}
    assert journal == {"plan:1": JournalStatus.SUCCEEDED, "read": JournalStatus.SUCCEEDED}
    assert read.action_id == "read"


async def test_a_crashed_capability_still_blocks_record_recompute() -> None:
    computed: list[int] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def think() -> Any:
            computed.append(1)
            return "plan"

        await context.record("plan:1", fingerprint="p", compute=think)
        await context.invoke(READ, action_id="read", params={"supplier_id": "SUP-001"})
        return OK

    env = Env(executor=FunctionExecutor(body))
    done = await env.run(idempotency_key="k")
    record, read = await env.runner.journal(done.execution_id)
    await env.storage.executions.update_journal(
        _inflight(record), expected_status=JournalStatus.SUCCEEDED
    )
    await env.storage.executions.update_journal(
        _inflight(read), expected_status=JournalStatus.SUCCEEDED
    )
    await env.storage.executions.compare_and_set(
        done.model_copy(
            update={
                "status": S.RUNNING,
                "finished_at": None,
                "outcome": None,
                "status_reason": None,
                "revision": done.revision + 1,
            }
        )
    )
    failed = await env.runner.resume(done.execution_id, actor=env.actor)
    assert failed.status is S.FAILED
    assert failed.status_reason == "uncertain_outcome"
    assert failed.requires_reconciliation
    assert computed == [1]
    assert env.provider.count(READ) == 1
    entries = await env.runner.journal(done.execution_id)
    journal = {entry.action_id: entry.status for entry in entries}
    assert journal["read"] is JournalStatus.UNCERTAIN
    assert journal["plan:1"] is JournalStatus.IN_FLIGHT


async def test_an_inflight_record_with_a_different_fingerprint_is_not_recomputed() -> None:
    runs = {"n": 0}
    computed: list[int] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        runs["n"] += 1

        async def think() -> Any:
            computed.append(1)
            return "plan"

        await context.record("plan:1", fingerprint={"run": runs["n"]}, compute=think)
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    (record, _) = await env.runner.journal(waiting.execution_id)
    await env.storage.executions.update_journal(
        _inflight(record), expected_status=JournalStatus.SUCCEEDED
    )
    failed = await _approve_and_resume(env, waiting)
    assert failed.status_reason == "non_deterministic"
    assert computed == [1]
    assert env.provider.count(PREPARE) == 0


async def test_an_uncertain_record_is_not_recomputed() -> None:
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
    uncertain = record.model_copy(
        update={
            "status": JournalStatus.UNCERTAIN,
            "result": None,
            "result_digest": None,
            "finished_at": None,
            "error": ExecutionErrorInfo(
                type="UncertainOutcomeError", message="lost", requires_reconciliation=True
            ),
        }
    )
    await env.storage.executions.update_journal(uncertain, expected_status=JournalStatus.SUCCEEDED)
    failed = await _approve_and_resume(env, waiting)
    assert failed.status_reason == "uncertain_outcome"
    assert failed.requires_reconciliation
    assert computed == [1]
    assert env.provider.count(PREPARE) == 0


async def test_record_does_not_overwrite_a_pending_capability_call() -> None:
    calls = {"n": 0}

    async def body(data: Any, context: ExecutionContext) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            await context.invoke(PREPARE, action_id="step", params={})
            return OK

        async def compute() -> Any:
            calls["computed"] = 1
            return "x"

        await context.record("step", fingerprint="p", compute=compute)
        return OK

    env = _supervised(body)
    failed = await _approve_and_resume(env, await env.run(idempotency_key="k"))
    assert failed.status_reason == "non_deterministic"
    assert env.provider.count(PREPARE) == 0
    assert "computed" not in calls


async def test_a_record_that_suspends_for_approval_resumes() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            await context.invoke(PREPARE, action_id="prepare", params={})
            return {"ok": True}

        result = await context.record("plan:1", fingerprint="p", compute=compute)
        assert result == {"ok": True}
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    assert waiting.status is S.WAITING_APPROVAL
    entries = await env.runner.journal(waiting.execution_id)
    journal = {entry.action_id: entry for entry in entries}
    assert journal["plan:1"].status is JournalStatus.IN_FLIGHT
    assert journal["plan:1"].capability == RECORD_CAPABILITY
    assert journal["prepare"].status is JournalStatus.PENDING_APPROVAL
    done = await _approve_and_resume(env, waiting)
    assert done.status is S.COMPLETED
    assert env.provider.count(PREPARE) == 1
    replayed_entries = await env.runner.journal(waiting.execution_id)
    replayed = {entry.action_id: entry.status for entry in replayed_entries}
    assert replayed["plan:1"] is JournalStatus.SUCCEEDED


async def test_a_record_that_suspends_inside_a_task_group_resumes() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            async with asyncio.TaskGroup() as group:
                group.create_task(context.invoke(PREPARE, action_id="prepare", params={}))
            return {"ok": True}

        await context.record("plan:1", fingerprint="p", compute=compute)
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    assert waiting.status is S.WAITING_APPROVAL
    (record, _) = await env.runner.journal(waiting.execution_id)
    assert record.status is JournalStatus.IN_FLIGHT
    done = await _approve_and_resume(env, waiting)
    assert done.status is S.COMPLETED
    assert env.provider.count(PREPARE) == 1


async def test_task_group_retries_a_read_cancelled_by_a_sibling_approval() -> None:
    entered = asyncio.Event()

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def read() -> None:
            await context.invoke(READ, action_id="read", params={"supplier_id": "SUP-001"})

        async def prepare() -> None:
            await entered.wait()
            await context.invoke(PREPARE, action_id="prepare", params={})

        async with asyncio.TaskGroup() as group:
            group.create_task(read())
            group.create_task(prepare())
        return OK

    env = _supervised(body)
    original = env.provider.invoke

    async def blocking_read(capability: str, params: Any, call: Any) -> Any:
        if capability == READ:
            entered.set()
            await asyncio.Event().wait()
        return await original(capability, params, call)

    env.provider.invoke = blocking_read  # type: ignore[method-assign]
    waiting = await env.run(idempotency_key="k")
    assert waiting.status is S.WAITING_APPROVAL
    journal = {entry.action_id: entry for entry in await env.runner.journal(waiting.execution_id)}
    assert journal["read"].status is JournalStatus.FAILED
    assert journal["read"].error is not None
    assert journal["read"].error.type == "CancelledError"
    env.provider.invoke = original  # type: ignore[method-assign]
    done = await _approve_and_resume(env, waiting)
    assert done.status is S.COMPLETED
    assert env.provider.count(READ) == 1
    assert env.provider.count(PREPARE) == 1


async def test_a_cancelled_side_effect_is_not_retried() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        await context.invoke(PREPARE, action_id="prepare", params={})
        return OK

    env = _supervised(body)
    waiting = await env.run(idempotency_key="k")
    (prepare,) = await env.runner.journal(waiting.execution_id)
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    failed_entry = prepare.model_copy(
        update={
            "status": JournalStatus.FAILED,
            "started_at": moment,
            "finished_at": moment,
            "error": ExecutionErrorInfo(type="CancelledError", message="cancelled"),
        }
    )
    await env.storage.executions.update_journal(
        failed_entry, expected_status=JournalStatus.PENDING_APPROVAL
    )
    failed = await _approve_and_resume(env, waiting)
    assert failed.status is S.FAILED
    assert env.provider.count(PREPARE) == 0


async def test_task_group_does_not_suspend_over_an_uncertain_side_effect() -> None:
    entered = asyncio.Event()

    async def body(data: Any, context: ExecutionContext) -> Any:
        async def prepare() -> None:
            await context.invoke(PREPARE, action_id="prepare", params={})

        async def read() -> None:
            await entered.wait()
            await context.invoke(READ, action_id="read", params={"supplier_id": "SUP-001"})

        async with asyncio.TaskGroup() as group:
            group.create_task(prepare())
            group.create_task(read())
        return OK

    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, PREPARE}),
            approval_requirements=(
                ApprovalRequirement(
                    gate=ApprovalGate.ACTION,
                    capability=READ,
                    reviewer_capability=REVIEW,
                ),
            ),
        ),
    )
    original = env.provider.invoke

    async def blocking_prepare(capability: str, params: Any, call: Any) -> Any:
        if capability == PREPARE:
            entered.set()
            await asyncio.Event().wait()
        return await original(capability, params, call)

    env.provider.invoke = blocking_prepare  # type: ignore[method-assign]
    execution = await env.run(idempotency_key="k")
    assert execution.status is S.FAILED
    assert execution.status_reason == "uncertain_outcome"
    assert execution.requires_reconciliation
    entries = await env.runner.journal(execution.execution_id)
    journal = {entry.action_id: entry.status for entry in entries}
    assert journal["prepare"] is JournalStatus.UNCERTAIN
    assert execution.status is not S.RUNNING
    assert execution.status is not S.WAITING_APPROVAL


async def test_a_nested_group_that_suspends_and_fails_does_not_swallow_the_error() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def boom() -> None:
            raise BaseExceptionGroup("inner", [GeneratorExit()])

        async def suspend() -> None:
            raise ExecutionSuspended()

        async with asyncio.TaskGroup() as group:
            group.create_task(boom())
            group.create_task(suspend())
        return OK

    failed = await Env(executor=FunctionExecutor(body)).run()
    assert failed.status is S.FAILED
    assert failed.error is not None
    assert any(cause.type == "GeneratorExit" for cause in failed.error.causes)


async def test_a_task_group_that_suspends_and_fails_does_not_swallow_the_error() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def boom() -> None:
            raise GeneratorExit()

        async def suspend() -> None:
            raise ExecutionSuspended()

        async with asyncio.TaskGroup() as group:
            group.create_task(boom())
            group.create_task(suspend())
        return OK

    failed = await Env(executor=FunctionExecutor(body)).run()
    assert failed.status is S.FAILED
    assert failed.status is not S.RUNNING
    assert failed.status is not S.WAITING_APPROVAL
    assert failed.error is not None
    assert failed.error.type == "ExecutionFailedError"
    assert any(cause.type == "GeneratorExit" for cause in failed.error.causes)


async def test_a_record_whose_task_group_fails_is_marked_failed() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        async def compute() -> Any:
            raise BaseExceptionGroup("stopped", [GeneratorExit()])

        await context.record("plan:1", fingerprint="p", compute=compute)
        return OK

    env = Env(executor=FunctionExecutor(body))
    failed = await env.run()
    assert failed.status is S.FAILED
    (record,) = await env.runner.journal(failed.execution_id)
    assert record.status is JournalStatus.FAILED
    assert record.error is not None
    assert record.error.type == "BaseExceptionGroup"


async def test_a_task_group_of_only_cancellation_fails_the_execution() -> None:
    seen: dict[str, Any] = {}

    async def body(data: Any, context: ExecutionContext) -> Any:
        seen["id"] = context.execution_id
        raise BaseExceptionGroup("cancelled", [asyncio.CancelledError()])

    env = Env(executor=FunctionExecutor(body))
    with pytest.raises(asyncio.CancelledError):
        await env.run()
    execution = await env.runner.get(seen["id"])
    assert execution is not None
    assert execution.status is S.FAILED
    assert execution.status_reason == "cancelled_in_flight"


def test_reserved_namespace_cannot_be_provided() -> None:
    from id42_work import CapabilityRegistry, CapabilitySpec  # noqa: PLC0415

    class Sneaky:
        def capabilities(self) -> list[CapabilitySpec]:
            return [CapabilitySpec(name="id42.record", side_effecting=False)]

        async def invoke(self, capability: str, params: Any, call: Any) -> Any:  # pragma: no cover
            return None

    with pytest.raises(WorkRegistrationError, match="reserved"):
        CapabilityRegistry().add(Sneaky())
