"""Less common runner paths: storage races, odd outputs, interrupted reads."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import READ, VALIDATE, Env, FunctionExecutor
from id42_work import (
    AutonomyLevel,
    ConcurrentModificationError,
    EventType,
    ExecutionContext,
    ExecutionLimits,
    ExecutionStatus,
    StaticPolicyResolver,
    StorageError,
    WorkEvent,
    WorkRunner,
)
from id42_work.journal import JournalStatus
from id42_work.storage import IdempotencyRecord, IdempotencyScope

S = ExecutionStatus
OK = InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")


async def test_event_append_retries_after_a_conflict() -> None:
    env = Env()
    repo = env.storage.events
    original = repo.append
    conflicts = {"left": 1}

    async def flaky(event: WorkEvent) -> None:
        if conflicts["left"]:
            conflicts["left"] -= 1
            raise ConcurrentModificationError("simulated race")
        await original(event)

    repo.append = flaky  # type: ignore[method-assign]
    execution = await env.run()
    assert execution.status is S.COMPLETED


async def test_event_append_gives_up_after_repeated_conflicts() -> None:
    env = Env()

    async def always_conflicts(event: WorkEvent) -> None:
        raise ConcurrentModificationError("simulated race")

    env.storage.events.append = always_conflicts  # type: ignore[method-assign]
    execution = await env.run()
    assert execution.status is S.FAILED
    assert execution.status_reason == "executor_error"
    assert execution.error is not None
    assert "could not append" in execution.error.message


async def test_a_raced_transition_keeps_the_event_sequence_it_committed() -> None:
    env = Env()
    original = env.storage.events.require_next
    raced = {"done": False}

    def steal_one_sequence(event: WorkEvent) -> None:
        if not raced["done"] and event.event_type is EventType.EXECUTION_COMPLETED:
            raced["done"] = True
            env.storage.events.add(
                event.model_copy(update={"event_type": EventType.ACTION_FAILED, "payload": {}})
            )
        original(event)

    env.storage.events.require_next = steal_one_sequence  # type: ignore[method-assign]
    execution = await env.run()
    events = await env.runner.events(execution.execution_id)
    assert events[-2].event_type is EventType.ACTION_FAILED
    assert events[-1].event_type is EventType.EXECUTION_COMPLETED
    assert execution.last_event_sequence == events[-1].sequence_number


async def test_a_commit_conflict_on_a_missing_execution_is_not_retried() -> None:
    env = Env()
    original_require = env.storage.events.require_next
    original_get = env.storage.executions.get
    conflicted = {"done": False}

    def conflict_once(event: WorkEvent) -> None:
        if event.event_type is not EventType.EXECUTION_CREATED and not conflicted["done"]:
            conflicted["done"] = True
            raise ConcurrentModificationError("race")
        original_require(event)

    async def disappear(execution_id: Any) -> Any:
        if conflicted["done"]:
            return None
        return await original_get(execution_id)

    env.storage.events.require_next = conflict_once  # type: ignore[method-assign]
    env.storage.executions.get = disappear  # type: ignore[method-assign]
    with pytest.raises(ConcurrentModificationError, match="race"):
        await env.run()


async def test_commit_gives_up_after_repeated_sequence_races() -> None:
    env = Env()

    def always_conflicts(event: WorkEvent) -> None:
        raise ConcurrentModificationError("sequence raced")

    env.storage.events.require_next = always_conflicts  # type: ignore[method-assign]
    with pytest.raises(StorageError, match=r"could not commit work\.execution\.created"):
        await env.run()


async def test_idempotency_owner_that_never_appears() -> None:
    env = Env()
    runner = WorkRunner(
        registry=env.registry,
        policies=StaticPolicyResolver({(env.work.id, env.work.version): env.policy}),
        capabilities=env.capabilities,
        actor_authority=env.authority,
        storage=env.storage,
        clock=env.clock,
        idempotency_wait=timedelta(milliseconds=20),
    )
    first = await runner.run(
        work_id=env.work.id,
        version="1.0.0",
        input_data=_input(),
        actor=env.actor,
        idempotency_key="k",
    )
    scope = IdempotencyScope(
        tenant_id="acme", work_id=env.work.id, work_version="1.0.0", idempotency_key="ghost"
    )
    await env.storage.idempotency.reserve(
        IdempotencyRecord(
            scope=scope, input_fingerprint=first.input_fingerprint, execution_id=uuid4()
        )
    )
    with pytest.raises(StorageError, match="still being created"):
        await runner.run(
            work_id=env.work.id,
            version="1.0.0",
            input_data=_input(),
            actor=env.actor,
            idempotency_key="ghost",
        )


def _input() -> dict[str, str]:
    return {
        "invoice_number": "INV-1001",
        "supplier_id": "SUP-001",
        "currency": "BRL",
        "amount": "1250.00",
    }


async def test_float_output_cannot_be_put_under_review() -> None:
    class Scored(BaseModel):
        score: float

    async def body(data: Any, context: ExecutionContext) -> Scored:
        return Scored(score=0.5)

    env = Env(
        work=make_work(output_schema=Scored),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.GUIDED, allowed_capabilities=frozenset({READ, VALIDATE})
        ),
    )
    execution = await env.run()
    assert execution.status is S.VALIDATION_FAILED
    assert execution.status_reason == "output_not_canonical"


async def test_timeout_during_a_read_marks_the_action_failed_without_reconciliation() -> None:
    env = Env(
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE}),
            execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=30)),
        )
    )

    async def slow(capability: str, params: Any, call: Any) -> Any:
        await asyncio.sleep(5)

    env.provider.invoke = slow  # type: ignore[method-assign]
    execution = await env.run()
    assert execution.status_reason == "timeout"
    assert not execution.requires_reconciliation
    (entry,) = await env.runner.journal(execution.execution_id)
    assert entry.status is JournalStatus.FAILED


async def test_executor_that_masks_the_timeout_still_times_out() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    env = Env(
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE}),
            execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=20)),
        ),
    )
    execution = await env.run()
    assert execution.status_reason == "timeout"


async def test_first_violation_is_the_one_recorded() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        for capability in ("payments.execute", "shell.exec"):
            try:
                await context.invoke(capability, action_id=f"try-{capability}", params={})
            except Exception:  # noqa: BLE001, S112
                continue
        return OK

    execution = await Env(executor=FunctionExecutor(body)).run()
    assert execution.status_reason == "capability_denied"
    assert execution.error is not None
    assert "payments.execute" in execution.error.message


async def test_context_identifiers() -> None:
    seen: dict[str, object] = {}

    async def body(data: Any, context: ExecutionContext) -> Any:
        seen["execution_id"] = context.execution_id
        return OK

    class IdValidator:
        validator_id = "ids"
        required = True

        async def validate(self, output: BaseModel, context: Any) -> Any:
            from id42_work import Severity, ValidationResult  # noqa: PLC0415

            seen["validation_execution_id"] = context.execution_id
            return ValidationResult(validator_id="ids", passed=True, severity=Severity.INFO)

    env = Env(executor=FunctionExecutor(body), validators=[IdValidator()])
    execution = await env.run()
    assert seen == {
        "execution_id": execution.execution_id,
        "validation_execution_id": execution.execution_id,
    }


async def test_preview_of_non_mapping_params() -> None:
    env = Env()
    recorder = env.runner._rec
    assert recorder.preview(["a", 1]) == {"value": ["a", 1]}
    assert recorder.preview({"token": "x", "n": 1}) == {"token": "***", "n": 1}
    assert EventType.ACTION_FAILED.value == "work.action.failed"
