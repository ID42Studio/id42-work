"""Resume recovers an execution left in VALIDATING without repeating the side effect."""

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    AutonomyLevel,
    EventType,
    ExecutionContext,
    ExecutionLimits,
    ExecutionStatus,
    Policy,
    Severity,
    SqliteStorage,
    ValidationContext,
    ValidationResult,
    WorkDefinitionError,
)
from id42_work.identifiers import RECORD_CAPABILITY
from id42_work.integrations.pydantic_ai import RecordedModel
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from id42_work.storage.protocols import Storage
from postgres_support import configured_dsn, create_database, drop_database
from validating_recovery_worker import (
    PUBLISH,
    CrashValidator,
    LogProvider,
    NoteExecutor,
    leave_validating,
    make_runner,
)
from validating_recovery_worker import READ as NOTE_READ

S = ExecutionStatus
CAPS = frozenset({READ, VALIDATE, PREPARE})


class Halt(BaseException):
    """Stands in for a process death after the effect, during validation."""


class Rule:
    """One validator whose second run is chosen by the test."""

    validator_id = "recover_rule"
    required = True

    def __init__(self) -> None:
        self.mode = "halt"
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def validate(self, output: object, context: ValidationContext) -> ValidationResult:
        del output
        self.calls += 1
        if self.mode == "halt":
            raise Halt
        if self.mode == "hang":
            await asyncio.sleep(30)
        if self.mode == "fail":
            return ValidationResult(
                validator_id=self.validator_id,
                passed=False,
                severity=Severity.ERROR,
                message="rejected",
            )
        if self.mode == "wait":
            self.started.set()
            await self.release.wait()
        else:
            await context.invoke(
                VALIDATE, action_id="check-invoice", params={"invoice": "INV-1001"}
            )
        return ValidationResult(
            validator_id=self.validator_id,
            passed=True,
            severity=Severity.INFO,
            message="ok",
        )

    def arm_wait(self) -> None:
        self.mode = "wait"
        self.started = asyncio.Event()
        self.release = asyncio.Event()


def _policy(**overrides: Any) -> Policy:
    return make_policy(allowed_capabilities=CAPS, **overrides)


def _env(
    storage: Storage,
    rule: Rule,
    *,
    policy: Policy | None = None,
    executor: Any = None,
) -> Env:
    return Env(
        storage=storage,
        work=make_work(required_capabilities=CAPS),
        policy=policy or _policy(),
        executor=executor or InvoiceExecutor(prepare=True),
        validators=[rule],
    )


@pytest.fixture(params=["memory", "sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    kind = request.param
    if kind == "memory":
        from id42_work import MemoryStorage  # noqa: PLC0415

        yield MemoryStorage()
        return
    if kind == "sqlite":
        database = SqliteStorage(tmp_path / "work.sqlite")
        try:
            yield database
        finally:
            database.close()
        return
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    from id42_work import PostgresStorage  # noqa: PLC0415

    dsn, name = create_database(admin)
    database = PostgresStorage(dsn)
    try:
        yield database
    finally:
        database.close()
        drop_database(admin, name)


async def _crashed(storage: Storage, rule: Rule) -> tuple[Env, UUID]:
    env = _env(storage, rule)
    with pytest.raises(Halt):
        await env.run(idempotency_key="bill-1")
    execution_id = await _execution_id(env)
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.VALIDATING
    assert env.provider.count(PREPARE) == 1
    return env, execution_id


async def _execution_id(env: Env) -> UUID:
    again = await env.run(idempotency_key="bill-1")
    assert again.status is S.VALIDATING
    return again.execution_id


def _recover(env: Env, rule: Rule, *, policy: Policy | None = None, executor: Any = None) -> Env:
    return _env(
        env.storage,
        rule,
        policy=policy or env.policy,
        executor=executor or InvoiceExecutor(prepare=True),
    )


async def _prepared(data: Any, context: ExecutionContext) -> InvoiceOutput:
    supplier = await context.invoke(
        READ, action_id="load-supplier", params={"supplier_id": data.supplier_id}
    )
    await context.invoke(
        PREPARE,
        action_id="prepare-invoice",
        params={"invoice": data.invoice_number, "amount": data.amount},
    )
    assert isinstance(supplier, dict)
    return InvoiceOutput(
        invoice_number=data.invoice_number,
        supplier_id=str(supplier["id"]),
        status="prepared",
    )


async def test_resume_reruns_validators_and_does_not_repeat_the_side_effect(
    storage: Storage,
) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    second = _recover(env, rule)
    done = await second.runner.resume(execution_id, actor=second.actor)
    events = await second.runner.events(execution_id)
    types = [event.event_type for event in events]
    evidence = await second.runner.evidence(execution_id)
    journal = [
        (entry.action_id, entry.status) for entry in await second.runner.journal(execution_id)
    ]
    again = await second.runner.resume(execution_id, actor=second.actor)

    assert done.status is S.COMPLETED
    assert done.outcome is not None
    assert done.outcome.verified
    assert done.last_event_sequence == events[-1].sequence_number
    assert events[-1].event_type is EventType.EXECUTION_COMPLETED
    assert types.count(EventType.VALIDATION_STARTED) == 1
    assert EventType.EXECUTION_RESUMED in types
    assert EventType.ACTION_REPLAYED in types
    assert journal == [
        ("load-supplier", JournalStatus.SUCCEEDED),
        ("prepare-invoice", JournalStatus.SUCCEEDED),
    ]
    assert sum(1 for item in evidence if item.evidence_type.value == "VALIDATION_REPORT") == 1
    assert rule.calls == 2
    assert second.executor.runs == 1
    assert second.provider.count(PREPARE) == 0
    assert second.provider.count(READ) == 0
    assert second.provider.count(VALIDATE) == 1
    assert again == done


async def test_a_failing_validator_on_recovery_is_not_accepted(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "fail"
    second = _recover(env, rule)
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "validation_failed"
    assert failed.error is None
    assert failed.outcome is not None
    assert not failed.outcome.verified
    assert second.provider.count(PREPARE) == 0


async def test_a_hanging_validator_on_recovery_is_bounded_by_the_timeout(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "hang"
    second = _recover(
        env,
        rule,
        policy=_policy(execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=30))),
    )
    failed = await asyncio.wait_for(
        second.runner.resume(execution_id, actor=second.actor),
        timeout=2,
    )
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "validation_failed"
    assert failed.outcome is not None
    messages = {result.validator_id: result.message for result in failed.outcome.validation_results}
    assert messages["recover_rule"] == "validator timed out (execution_limits.timeout)"
    assert second.provider.count(PREPARE) == 0


async def test_policy_denial_fails_closed_without_calling_the_provider(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    second = _recover(env, rule)
    revoked = second.issue("user:42", (READ,))
    failed = await second.runner.resume(execution_id, actor=revoked)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "policy_denied_on_resume"
    assert failed.policy_reasons
    assert not failed.requires_reconciliation
    assert second.executor.runs == 0
    assert second.provider.count(PREPARE) == 0
    assert rule.calls == 1


async def test_a_live_claim_leaves_a_validating_execution_unchanged(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    with env.runner._claim(execution_id) as claimed:
        assert claimed
        unchanged = await env.runner.resume(execution_id, actor=env.actor)
    assert unchanged.status is S.VALIDATING
    assert rule.calls == 1
    assert env.provider.count(PREPARE) == 1


async def test_an_inflight_action_is_uncertain_and_not_retried(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    await env.storage.executions.append_journal(
        JournalEntry(
            execution_id=execution_id,
            action_id="extra-post",
            capability=PREPARE,
            params_digest="ab" * 32,
            operation_id=operation_id(execution_id, "extra-post"),
            status=JournalStatus.IN_FLIGHT,
            started_at=env.clock.now,
        )
    )
    second = _recover(env, rule)
    failed = await second.runner.resume(execution_id, actor=second.actor)
    journal = {entry.action_id: entry.status for entry in await second.runner.journal(execution_id)}
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "uncertain_outcome"
    assert failed.requires_reconciliation
    assert failed.error is not None
    assert failed.error.type == "UncertainOutcomeError"
    assert journal["extra-post"] is JournalStatus.UNCERTAIN
    assert journal["prepare-invoice"] is JournalStatus.SUCCEEDED
    assert second.executor.runs == 0
    assert second.provider.count(PREPARE) == 0


async def test_an_already_uncertain_action_fails_closed_without_a_provider(
    storage: Storage,
) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    succeeded = next(
        entry
        for entry in await env.runner.journal(execution_id)
        if entry.action_id == "prepare-invoice"
    )
    uncertain = JournalEntry.model_validate(
        {
            **succeeded.model_dump(),
            "status": JournalStatus.UNCERTAIN,
            "result": None,
            "result_digest": None,
            "finished_at": None,
        }
    )
    await env.storage.executions.update_journal(uncertain, expected_status=JournalStatus.SUCCEEDED)
    second = _recover(env, rule)
    failed = await second.runner.resume(execution_id, actor=second.actor)
    journal = {entry.action_id: entry.status for entry in await second.runner.journal(execution_id)}
    assert failed.status is S.VALIDATION_FAILED
    assert failed.requires_reconciliation
    assert journal["prepare-invoice"] is JournalStatus.UNCERTAIN
    assert second.executor.runs == 0
    assert second.provider.count(PREPARE) == 0


async def test_a_pending_action_is_not_performed(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    await env.storage.executions.append_journal(
        JournalEntry(
            execution_id=execution_id,
            action_id="extra-post",
            capability=PREPARE,
            params_digest="cd" * 32,
            operation_id=operation_id(execution_id, "extra-post"),
            status=JournalStatus.PENDING_APPROVAL,
        )
    )
    second = _recover(env, rule)
    failed = await second.runner.resume(execution_id, actor=second.actor)
    journal = {entry.action_id: entry.status for entry in await second.runner.journal(execution_id)}
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert not failed.requires_reconciliation
    assert journal["extra-post"] is JournalStatus.PENDING_APPROVAL
    assert second.executor.runs == 0
    assert second.provider.count(PREPARE) == 0


async def test_a_new_side_effect_during_replay_is_not_performed(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def extra(data: Any, context: ExecutionContext) -> InvoiceOutput:
        output = await _prepared(data, context)
        await context.invoke(PREPARE, action_id="extra-post", params={"invoice": "NEW"})
        return output

    second = _recover(env, rule, executor=FunctionExecutor(extra))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert failed.error is not None
    assert failed.error.type == "NonDeterministicExecutionError"
    assert second.provider.count(PREPARE) == 0
    assert second.provider.count(READ) == 0


async def _crash_with(storage: Storage, rule: Rule, body: Any) -> tuple[Env, UUID]:
    env = _env(storage, rule, executor=FunctionExecutor(body))
    with pytest.raises(Halt):
        await env.run(idempotency_key="bill-1")
    execution_id = await _execution_id(env)
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.VALIDATING
    assert env.provider.count(PREPARE) == 1
    return env, execution_id


def _recording_body(
    calls: dict[str, int], fingerprint: dict[str, str], *, extra: dict[str, bool]
) -> Any:
    async def body(data: Any, context: ExecutionContext) -> InvoiceOutput:
        async def think() -> Any:
            calls["n"] += 1
            return {"note": "held"}

        recorded = await context.record("plan:1", fingerprint=dict(fingerprint), compute=think)
        assert recorded == {"note": "held"}
        if extra["on"]:
            await context.record("plan:2", fingerprint=dict(fingerprint), compute=think)
        return await _prepared(data, context)

    return body


async def test_a_stored_record_is_replayed_without_calling_compute(storage: Storage) -> None:
    rule = Rule()
    calls = {"n": 0}
    fingerprint = {"prompt": "hold"}
    extra = {"on": False}
    body = _recording_body(calls, fingerprint, extra=extra)
    env, execution_id = await _crash_with(storage, rule, body)
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    done = await second.runner.resume(execution_id, actor=second.actor)
    record = next(
        entry for entry in await second.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    assert done.status is S.COMPLETED
    assert calls["n"] == 1
    assert second.provider.count(PREPARE) == 0
    assert record.capability == RECORD_CAPABILITY
    assert record.status is JournalStatus.SUCCEEDED
    assert record.result == {"note": "held"}


async def test_a_missing_record_fails_closed_without_computing(storage: Storage) -> None:
    rule = Rule()
    calls = {"n": 0}
    fingerprint = {"prompt": "hold"}
    extra = {"on": False}
    body = _recording_body(calls, fingerprint, extra=extra)
    env, execution_id = await _crash_with(storage, rule, body)
    extra["on"] = True
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert not failed.requires_reconciliation
    assert failed.error is not None
    assert failed.error.type == "NonDeterministicExecutionError"
    assert calls["n"] == 1
    assert second.provider.count(PREPARE) == 0


async def test_a_mismatched_record_fails_closed_without_computing(storage: Storage) -> None:
    rule = Rule()
    calls = {"n": 0}
    fingerprint = {"prompt": "hold"}
    extra = {"on": False}
    body = _recording_body(calls, fingerprint, extra=extra)
    env, execution_id = await _crash_with(storage, rule, body)
    fingerprint["prompt"] = "changed"
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert calls["n"] == 1
    assert second.provider.count(PREPARE) == 0
    record = next(
        entry for entry in await second.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    assert record.status is JournalStatus.SUCCEEDED


async def test_an_unfinished_record_is_not_computed_again(storage: Storage) -> None:
    rule = Rule()
    calls = {"n": 0}
    fingerprint = {"prompt": "hold"}
    extra = {"on": False}
    body = _recording_body(calls, fingerprint, extra=extra)
    env, execution_id = await _crash_with(storage, rule, body)
    stored = next(
        entry for entry in await env.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    unfinished = stored.model_copy(
        update={
            "status": JournalStatus.IN_FLIGHT,
            "result": None,
            "result_digest": None,
            "finished_at": None,
        }
    )
    await env.storage.executions.update_journal(unfinished, expected_status=JournalStatus.SUCCEEDED)
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    record = next(
        entry for entry in await second.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert not failed.requires_reconciliation
    assert calls["n"] == 1
    assert second.provider.count(PREPARE) == 0
    assert record.status is JournalStatus.IN_FLIGHT


async def test_an_unfinished_record_the_replay_skips_still_fails_closed(storage: Storage) -> None:
    rule = Rule()
    calls = {"n": 0}
    body = _recording_body(calls, {"prompt": "hold"}, extra={"on": False})
    env, execution_id = await _crash_with(storage, rule, body)
    stored = next(
        entry for entry in await env.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    await env.storage.executions.update_journal(
        stored.model_copy(
            update={
                "status": JournalStatus.IN_FLIGHT,
                "result": None,
                "result_digest": None,
                "finished_at": None,
            }
        ),
        expected_status=JournalStatus.SUCCEEDED,
    )
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(_prepared))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    record = next(
        entry for entry in await second.runner.journal(execution_id) if entry.action_id == "plan:1"
    )
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert calls["n"] == 1
    assert second.provider.count(PREPARE) == 0
    assert record.status is JournalStatus.IN_FLIGHT


class _HoldModel:
    """A model that returns one fixed sentence and counts provider calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        self.calls += 1
        return ModelResponse(parts=[TextPart(content="held")])


def _recorded_model_body(model: _HoldModel, prompt: dict[str, str], prefix: dict[str, str]) -> Any:
    async def body(data: Any, context: ExecutionContext) -> InvoiceOutput:
        agent = Agent(
            RecordedModel(FunctionModel(model), context, prefix=prefix["value"]),
            output_type=str,
        )
        result = await agent.run(prompt["text"])
        assert result.output == "held"
        return await _prepared(data, context)

    return body


async def test_a_recorded_model_response_is_replayed_without_calling_the_model(
    storage: Storage,
) -> None:
    rule = Rule()
    model = _HoldModel()
    prompt = {"text": "hold the invoice"}
    prefix = {"value": "model"}
    body = _recorded_model_body(model, prompt, prefix)
    env, execution_id = await _crash_with(storage, rule, body)
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    done = await second.runner.resume(execution_id, actor=second.actor)
    record = next(
        entry
        for entry in await second.runner.journal(execution_id)
        if entry.capability == RECORD_CAPABILITY
    )
    assert done.status is S.COMPLETED
    assert model.calls == 1
    assert second.provider.count(PREPARE) == 0
    assert record.action_id == "model:1"
    assert record.status is JournalStatus.SUCCEEDED


async def test_a_missing_model_recording_fails_closed(storage: Storage) -> None:
    rule = Rule()
    model = _HoldModel()
    prompt = {"text": "hold the invoice"}
    prefix = {"value": "model"}
    body = _recorded_model_body(model, prompt, prefix)
    env, execution_id = await _crash_with(storage, rule, body)
    prefix["value"] = "draft"
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert model.calls == 1
    assert second.provider.count(PREPARE) == 0


async def test_a_mismatched_model_recording_fails_closed(storage: Storage) -> None:
    rule = Rule()
    model = _HoldModel()
    prompt = {"text": "hold the invoice"}
    prefix = {"value": "model"}
    body = _recorded_model_body(model, prompt, prefix)
    env, execution_id = await _crash_with(storage, rule, body)
    prompt["text"] = "a different conversation"
    rule.mode = "pass"
    second = _recover(env, rule, executor=FunctionExecutor(body))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert model.calls == 1
    assert second.provider.count(PREPARE) == 0


async def test_a_swallowed_new_action_still_fails_closed(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def extra(data: Any, context: ExecutionContext) -> InvoiceOutput:
        output = await _prepared(data, context)
        with contextlib.suppress(Exception):
            await context.invoke(PREPARE, action_id="extra-post", params={"invoice": "NEW"})
        return output

    second = _recover(env, rule, executor=FunctionExecutor(extra))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "non_deterministic"
    assert second.provider.count(PREPARE) == 0
    assert rule.calls == 1


async def test_a_raising_replay_fails_closed(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def boom(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await _prepared(data, context)
        raise ValueError("boom")

    second = _recover(env, rule, executor=FunctionExecutor(boom))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "executor_error"
    assert failed.error is not None
    assert failed.error.type == "ValueError"
    assert not failed.requires_reconciliation
    assert second.provider.count(PREPARE) == 0


async def test_a_timeout_error_raised_by_the_replay_is_an_executor_error(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def boom(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await _prepared(data, context)
        raise TimeoutError("remote timed out")

    second = _recover(env, rule, executor=FunctionExecutor(boom))
    failed = await second.runner.resume(execution_id, actor=second.actor)
    assert failed.status_reason == "executor_error"
    assert failed.error is not None
    assert failed.error.type == "TimeoutError"


async def test_a_hung_replay_fails_closed_on_the_execution_timeout(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def slow(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await _prepared(data, context)
        await asyncio.sleep(30)
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="prepared")

    second = _recover(
        env,
        rule,
        policy=_policy(execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=30))),
        executor=FunctionExecutor(slow),
    )
    failed = await asyncio.wait_for(
        second.runner.resume(execution_id, actor=second.actor),
        timeout=2,
    )
    assert failed.status is S.VALIDATION_FAILED
    assert failed.status_reason == "timeout"
    assert second.provider.count(PREPARE) == 0
    assert rule.calls == 1


async def test_a_replay_that_masks_the_timeout_still_times_out(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def slow(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await _prepared(data, context)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="prepared")

    second = _recover(
        env,
        rule,
        policy=_policy(execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=30))),
        executor=FunctionExecutor(slow),
    )
    failed = await asyncio.wait_for(
        second.runner.resume(execution_id, actor=second.actor),
        timeout=2,
    )
    assert failed.status_reason == "timeout"
    assert failed.error is not None
    assert failed.error.type == "RuntimeError"


async def test_an_outcome_gate_waits_for_review_after_recovery(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"
    second = _recover(env, rule, policy=_policy(autonomy=AutonomyLevel.GUIDED))
    waiting = await second.runner.resume(execution_id, actor=second.actor)
    types = [event.event_type for event in await second.runner.events(execution_id)]
    assert waiting.status is S.WAITING_REVIEW
    assert waiting.status_reason == "awaiting_outcome_review"
    assert waiting.outcome is not None
    assert waiting.outcome.verified
    assert waiting.outcome.accepted_at is None
    assert types.count(EventType.VALIDATION_STARTED) == 1
    assert EventType.REVIEW_REQUESTED in types
    assert second.provider.count(PREPARE) == 0


async def test_a_replaced_definition_cannot_be_recovered(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    renamed = make_work(name="Renamed invoice validation", required_capabilities=CAPS)
    env.registry.register(
        renamed, executor=InvoiceExecutor(prepare=True), validators=[rule], replace=True
    )
    with pytest.raises(WorkDefinitionError):
        await env.runner.resume(execution_id, actor=env.actor)
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.VALIDATING


async def test_run_with_the_same_key_does_not_resume_validation(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    assert rule.calls == 1
    assert env.executor.runs == 1
    assert execution_id


async def test_cancellation_during_replay_leaves_the_execution_validating(storage: Storage) -> None:
    rule = Rule()
    env, execution_id = await _crashed(storage, rule)
    rule.mode = "pass"

    async def cancel(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await _prepared(data, context)
        raise asyncio.CancelledError

    second = _recover(env, rule, executor=FunctionExecutor(cancel))
    with pytest.raises(asyncio.CancelledError):
        await second.runner.resume(execution_id, actor=second.actor)
    stored = await second.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.VALIDATING
    assert second.provider.count(PREPARE) == 0


async def test_one_resume_drives_a_validating_execution() -> None:
    rule = Rule()
    env, execution_id = await _crashed_memory(rule)
    rule.arm_wait()
    first = asyncio.create_task(env.runner.resume(execution_id, actor=env.actor))
    await asyncio.wait_for(rule.started.wait(), timeout=2)
    other = await env.runner.resume(execution_id, actor=env.actor)
    assert other.status is S.VALIDATING
    rule.release.set()
    done = await first
    stored = await env.runner.get(execution_id)
    assert done.status is S.COMPLETED
    assert stored is not None
    assert stored.status is S.COMPLETED
    assert rule.calls == 2


async def _crashed_memory(rule: Rule) -> tuple[Env, UUID]:
    from id42_work import MemoryStorage  # noqa: PLC0415

    return await _crashed(MemoryStorage(), rule)


def _diagnostics(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.stat().st_size:
            parts.append(f"--- {path.name}\n{path.read_text(encoding='utf-8', errors='replace')}")
    return "\n".join(parts)


def _run_child(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    tests = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = tests + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_process_death_during_validation_is_not_repeated(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite"
    log_path = tmp_path / "effects.log"
    id_path = tmp_path / "execution.id"
    completed = _run_child(
        [
            "from validating_recovery_worker import crash_process; "
            "import sys; crash_process(*sys.argv[1:])",
            "sqlite",
            str(database),
            str(log_path),
            str(id_path),
        ],
        _child_env(),
    )
    assert completed.returncode == 86, completed.stderr
    before = log_path.read_text(encoding="utf-8").splitlines()
    assert before == [NOTE_READ, PUBLISH]

    storage = SqliteStorage(database)
    runner, authority = make_runner(
        storage,
        NoteExecutor(),
        CrashValidator(str(log_path), crash=False),
        LogProvider(str(log_path)),
    )
    actor = authority.issue(
        actor_id="user:42",
        tenant_id="acme",
        grants={NOTE_READ, PUBLISH},
        ttl=timedelta(hours=1),
    )
    execution_id = UUID(id_path.read_text(encoding="utf-8"))

    async def recover() -> None:
        done = await runner.resume(execution_id, actor=actor)
        assert done.status is S.COMPLETED
        journal = {entry.action_id: entry.status for entry in await runner.journal(execution_id)}
        assert journal == {
            "read-note": JournalStatus.SUCCEEDED,
            "publish-note": JournalStatus.SUCCEEDED,
        }
        types = [event.event_type for event in await runner.events(execution_id)]
        assert types.count(EventType.VALIDATION_STARTED) == 1
        assert EventType.EXECUTION_RESUMED in types

    try:
        asyncio.run(recover())
    finally:
        storage.close()
    assert log_path.read_text(encoding="utf-8").splitlines() == [*before, "validated"]


@pytest.mark.postgres
def test_process_death_during_validation_is_not_repeated_on_postgres(tmp_path: Path) -> None:
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    from id42_work import PostgresStorage  # noqa: PLC0415

    dsn, name = create_database(admin)
    log_path = tmp_path / "effects.log"
    id_path = tmp_path / "execution.id"
    try:
        completed = _run_child(
            [
                "from validating_recovery_worker import crash_process; "
                "import sys; crash_process(*sys.argv[1:])",
                "postgres",
                dsn,
                str(log_path),
                str(id_path),
            ],
            _child_env(),
        )
        assert completed.returncode == 86, completed.stderr
        before = log_path.read_text(encoding="utf-8").splitlines()
        assert before == [NOTE_READ, PUBLISH]
        storage = PostgresStorage(dsn)
        runner, authority = make_runner(
            storage,
            NoteExecutor(),
            CrashValidator(str(log_path), crash=False),
            LogProvider(str(log_path)),
        )
        actor = authority.issue(
            actor_id="user:42",
            tenant_id="acme",
            grants={NOTE_READ, PUBLISH},
            ttl=timedelta(hours=1),
        )
        execution_id = UUID(id_path.read_text(encoding="utf-8"))

        async def recover() -> None:
            done = await runner.resume(execution_id, actor=actor)
            assert done.status is S.COMPLETED

        try:
            asyncio.run(recover())
        finally:
            storage.close()
        assert log_path.read_text(encoding="utf-8").splitlines() == [*before, "validated"]
    finally:
        drop_database(admin, name)


def _race(backend: str, target: str, execution_id: UUID, root: Path) -> None:
    import time  # noqa: PLC0415

    gate = root / "gate"
    gate.mkdir(parents=True)
    log = root / "entered.log"
    results = root / "results"
    results.mkdir()
    env = _child_env()
    command = "from validating_recovery_worker import contend; import sys; contend(*sys.argv[1:])"
    processes: list[subprocess.Popen[str]] = []
    handles: list[Any] = []
    try:
        for index in range(2):
            handle = (root / f"proc-{index}.log").open("w", encoding="utf-8")
            handles.append(handle)
            processes.append(
                subprocess.Popen(  # noqa: S603
                    [
                        sys.executable,
                        "-c",
                        command,
                        backend,
                        target,
                        str(execution_id),
                        str(gate),
                        str(log),
                        str(results / f"{index}.txt"),
                    ],
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        (gate / "go").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 20
        loser: str | None = None
        while time.monotonic() < deadline:
            done = sorted(results.glob("*.txt"))
            if (gate / "holding").exists() and done:
                loser = done[0].read_text(encoding="utf-8").strip()
                break
            time.sleep(0.02)
        assert loser == "VALIDATING", _diagnostics(root)
        (gate / "release").write_text("1", encoding="utf-8")
        for process in processes:
            process.wait(timeout=20)
        statuses = sorted(
            path.read_text(encoding="utf-8").strip() for path in results.glob("*.txt")
        )
        assert statuses == ["COMPLETED", "VALIDATING"], _diagnostics(root)
        assert log.read_text(encoding="utf-8").splitlines() == ["enter"]
        assert [process.returncode for process in processes] == [0, 0], _diagnostics(root)
    finally:
        (gate / "release").write_text("1", encoding="utf-8")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()


def test_two_sqlite_processes_cannot_recover_validation_together(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite"
    storage = SqliteStorage(database)
    try:
        execution_id = asyncio.run(leave_validating(storage))
    finally:
        storage.close()
    _race("sqlite", str(database), execution_id, tmp_path / "race")


@pytest.mark.postgres
def test_two_postgres_processes_cannot_recover_validation_together(tmp_path: Path) -> None:
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    from id42_work import PostgresStorage  # noqa: PLC0415

    dsn, name = create_database(admin)
    try:
        storage = PostgresStorage(dsn)
        try:
            execution_id = asyncio.run(leave_validating(storage))
        finally:
            storage.close()
        _race("postgres", dsn, execution_id, tmp_path / "race")
    finally:
        drop_database(admin, name)
