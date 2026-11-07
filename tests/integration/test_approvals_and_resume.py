"""Approval decisions, resumption by replay, expiry and cancellation (spec 7, 8, 4.3)."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import INVOICE, PREPARE, READ, VALIDATE, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    ApprovalGate,
    ApprovalRejectedError,
    ApprovalRequirement,
    ApprovalStatus,
    AutonomyLevel,
    EvidenceType,
    ExecutionContext,
    ExecutionStatus,
    InvalidStateTransitionError,
)
from id42_work.journal import JournalStatus

S = ExecutionStatus
WORK_REVIEWER = "approve:supplier_invoice_validation"


def _env(level: AutonomyLevel, *, prepare: bool = False, **kwargs: Any) -> Env:
    caps = frozenset({READ, PREPARE}) if prepare else frozenset({READ, VALIDATE})
    return Env(
        work=make_work(required_capabilities=caps),
        executor=kwargs.pop("executor", InvoiceExecutor(prepare=prepare)),
        policy=make_policy(
            autonomy=level, allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}), **kwargs
        ),
    )


def _reviewer(env: Env, *grants: str):
    return env.issue("reviewer:7", grants or (WORK_REVIEWER,))


async def _pending(env: Env, execution_id: Any):
    (approval,) = await env.runner.approvals.list_pending(execution_id)
    return approval


async def test_start_approval_then_resume_completes() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    granted = await env.runner.approvals.approve(approval.approval_id, reviewer=_reviewer(env))
    assert granted.status is ApprovalStatus.GRANTED
    ready = await env.runner.get(waiting.execution_id)
    assert ready is not None
    assert ready.status is S.READY
    assert env.executor.runs == 0

    review = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert review.status is S.WAITING_REVIEW  # MANUAL also reviews the outcome
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.CONSUMED
    outcome_approval = await _pending(env, waiting.execution_id)
    assert outcome_approval.gate is ApprovalGate.OUTCOME
    consumed = await env.runner.approvals.approve(
        outcome_approval.approval_id, reviewer=_reviewer(env)
    )
    assert consumed.status is ApprovalStatus.CONSUMED
    done = await env.runner.get(waiting.execution_id)
    assert done is not None
    assert done.status is S.COMPLETED
    assert done.raise_for_status().accepted_at is not None
    assert env.executor.runs == 1
    decisions = [
        e
        for e in await env.runner.evidence(waiting.execution_id)
        if e.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    assert [d.metadata["gate"] for d in decisions] == ["START", "OUTCOME"]
    assert all(d.metadata["decision_actor_id"] == "reviewer:7" for d in decisions)


async def test_action_approval_replays_without_repeating_side_effects() -> None:
    env = _env(AutonomyLevel.SUPERVISED, prepare=True)
    waiting = await env.run(idempotency_key="k")
    approval = await _pending(env, waiting.execution_id)
    assert approval.capability == PREPARE
    await env.runner.approvals.approve(approval.approval_id, reviewer=_reviewer(env))
    done = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert done.status is S.COMPLETED
    assert env.executor.runs == 2  # replayed once
    assert env.provider.count(READ) == 1  # replayed from the journal
    assert env.provider.count(PREPARE) == 1
    types = await env.event_types(waiting.execution_id)
    assert "work.execution.resumed" in types
    assert "work.action.replayed" in types
    assert "work.approval.consumed" in types
    journal = {j.action_id: j.status for j in await env.runner.journal(waiting.execution_id)}
    assert journal == {
        "load-supplier": JournalStatus.SUCCEEDED,
        "prepare-invoice": JournalStatus.SUCCEEDED,
    }
    (_, params, _) = next(c for c in env.provider.calls if c[0] == PREPARE)
    assert params == {"invoice": "INV-1001", "amount": Decimal("1250.00")}


class DriftingExecutor:
    """Prepares a different amount on every run: models an executor whose action changes."""

    def __init__(self, amounts: list[str]) -> None:
        self.amounts = amounts
        self.runs = 0

    async def execute(self, input_data: Any, context: ExecutionContext) -> InvoiceOutput:
        amount = Decimal(self.amounts[min(self.runs, len(self.amounts) - 1)])
        self.runs += 1
        await context.invoke(PREPARE, action_id="prepare", params={"amount": amount})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="prepared")


async def test_changed_parameters_invalidate_the_approval() -> None:
    env = _env(
        AutonomyLevel.SUPERVISED, prepare=True, executor=DriftingExecutor(["1250.00", "1300.00"])
    )
    waiting = await env.run(idempotency_key="k")
    first = await _pending(env, waiting.execution_id)
    assert first.params_preview == {"amount": "1250.00"}
    await env.runner.approvals.approve(first.approval_id, reviewer=_reviewer(env))

    again = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert again.status is S.WAITING_APPROVAL
    assert env.provider.count(PREPARE) == 0
    old = await env.runner.approvals.get(first.approval_id)
    assert old is not None
    assert old.status is ApprovalStatus.INVALIDATED
    second = await _pending(env, waiting.execution_id)
    assert second.params_preview == {"amount": "1300.00"}
    assert second.action_digest != first.action_digest

    await env.runner.approvals.approve(second.approval_id, reviewer=_reviewer(env))
    done = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert done.status is S.COMPLETED
    assert [p for c, p, _ in env.provider.calls if c == PREPARE] == [{"amount": Decimal("1300.00")}]


async def test_replay_that_changes_a_completed_action_fails() -> None:
    calls = {"n": 0}

    async def body(data: Any, context: ExecutionContext) -> Any:
        calls["n"] += 1
        await context.invoke(READ, action_id="load", params={"supplier_id": f"S-{calls['n']}"})
        await context.invoke(PREPARE, action_id="prepare", params={})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="ok")

    env = _env(AutonomyLevel.SUPERVISED, prepare=True, executor=FunctionExecutor(body))
    waiting = await env.run(idempotency_key="k")
    await env.runner.approvals.approve(
        (await _pending(env, waiting.execution_id)).approval_id, reviewer=_reviewer(env)
    )
    failed = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert failed.status is S.FAILED
    assert failed.status_reason == "non_deterministic"
    assert env.provider.count(PREPARE) == 0


async def test_replay_of_a_failed_action_raises_again() -> None:
    seen: list[str] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        try:
            await context.invoke(READ, action_id="load", params={"supplier_id": "S"})
        except Exception as error:  # noqa: BLE001
            seen.append(type(error).__name__)
        await context.invoke(PREPARE, action_id="prepare", params={})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="ok")

    env = _env(AutonomyLevel.SUPERVISED, prepare=True, executor=FunctionExecutor(body))

    def flaky(capability: str, params: Any) -> object:
        if capability == READ:
            raise ConnectionError("down")
        return {"ok": True}

    env.provider.behaviour = flaky
    waiting = await env.run(idempotency_key="k")
    await env.runner.approvals.approve(
        (await _pending(env, waiting.execution_id)).approval_id, reviewer=_reviewer(env)
    )
    done = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert done.status is S.COMPLETED
    assert seen == ["ConnectionError", "ExecutionFailedError"]
    assert env.provider.count(READ) == 1


async def test_rejection_ends_the_execution() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    rejected = await env.runner.approvals.reject(
        approval.approval_id, reviewer=_reviewer(env), reason="supplier blocked"
    )
    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.reason == "supplier blocked"
    final = await env.runner.get(waiting.execution_id)
    assert final is not None
    assert final.status is S.REJECTED
    assert final.status_reason == "approval_rejected"
    with pytest.raises(ApprovalRejectedError):
        final.raise_for_status()
    assert env.executor.runs == 0


async def test_outcome_review_rejection() -> None:
    env = _env(AutonomyLevel.GUIDED)
    review = await env.run()
    approval = await _pending(env, review.execution_id)
    await env.runner.approvals.reject(approval.approval_id, reviewer=_reviewer(env), reason="wrong")
    final = await env.runner.get(review.execution_id)
    assert final is not None
    assert final.status is S.REJECTED
    assert final.outcome is not None
    assert final.outcome.accepted_at is None


async def test_reason_required_by_policy() -> None:
    requirement = ApprovalRequirement(
        gate=ApprovalGate.START, reviewer_capability="approve:finance", reason_required=True
    )
    env = _env(AutonomyLevel.AUTONOMOUS, approval_requirements=(requirement,))
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    reviewer = _reviewer(env, "approve:finance")
    from id42_work import ApprovalError  # noqa: PLC0415

    with pytest.raises(ApprovalError, match="reason is required"):
        await env.runner.approvals.approve(approval.approval_id, reviewer=reviewer)
    granted = await env.runner.approvals.approve(
        approval.approval_id, reviewer=reviewer, reason="checked contract"
    )
    assert granted.reason == "checked contract"


async def test_pending_approval_expiry_cancels_the_execution() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    env.clock.now += timedelta(hours=25)
    assert await env.runner.approvals.list_pending(waiting.execution_id) == ()
    final = await env.runner.get(waiting.execution_id)
    assert final is not None
    assert final.status is S.CANCELLED
    assert final.status_reason == "approval_expired"
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.EXPIRED


async def test_deciding_an_expired_approval_raises() -> None:
    from id42_work import ApprovalExpiredError  # noqa: PLC0415

    env = _env(AutonomyLevel.GUIDED)
    review = await env.run()
    approval = await _pending(env, review.execution_id)
    env.clock.now += timedelta(days=2)
    reviewer = _reviewer(env)
    with pytest.raises(ApprovalExpiredError):
        await env.runner.approvals.approve(approval.approval_id, reviewer=reviewer)
    final = await env.runner.get(review.execution_id)
    assert final is not None
    assert final.status is S.CANCELLED


async def test_expire_due_sweeps_all_executions() -> None:
    env = _env(AutonomyLevel.MANUAL)
    first = await env.run()
    second = await env.run()
    assert await env.runner.expire_due() == 0
    env.clock.now += timedelta(hours=25)
    assert await env.runner.expire_due() == 2
    for execution in (first, second):
        current = await env.runner.get(execution.execution_id)
        assert current is not None
        assert current.status is S.CANCELLED


async def test_granted_but_unused_approval_expires_and_is_requested_again() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    await env.runner.approvals.approve(approval.approval_id, reviewer=_reviewer(env))
    env.clock.now += timedelta(hours=25)
    actor = env.issue("user:42", (READ, VALIDATE, PREPARE))
    again = await env.runner.resume(waiting.execution_id, actor=actor)
    assert again.status is S.WAITING_APPROVAL
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.EXPIRED
    assert env.executor.runs == 0


async def test_resume_reevaluates_the_policy() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    await env.runner.approvals.approve(
        (await _pending(env, waiting.execution_id)).approval_id, reviewer=_reviewer(env)
    )
    revoked = env.issue("user:42", (READ,))
    denied = await env.runner.resume(waiting.execution_id, actor=revoked)
    assert denied.status is S.DENIED
    assert denied.status_reason == "policy_denied_on_resume"
    assert denied.policy_reasons == ("missing_capability:invoices.validate",)
    assert env.executor.runs == 0


async def test_resume_of_non_ready_execution_is_a_no_op() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    assert await env.runner.resume(waiting.execution_id, actor=env.actor) == waiting
    other = Env()
    done = await other.run()
    assert await other.runner.resume(done.execution_id, actor=other.actor) == done


async def test_concurrent_resumes_drive_once() -> None:
    gate = asyncio.Event()

    async def body(data: Any, context: ExecutionContext) -> Any:
        await gate.wait()
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")

    executor = FunctionExecutor(body)
    env = _env(AutonomyLevel.MANUAL, executor=executor)
    waiting = await env.run()
    await env.runner.approvals.approve(
        (await _pending(env, waiting.execution_id)).approval_id, reviewer=_reviewer(env)
    )
    first = asyncio.create_task(env.runner.resume(waiting.execution_id, actor=env.actor))
    await asyncio.sleep(0.01)
    second = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert second.status in {S.READY, S.RUNNING}
    gate.set()
    assert (await first).status is S.WAITING_REVIEW
    assert executor.runs == 1


async def test_replaced_definition_cannot_be_resumed() -> None:
    from id42_work import WorkDefinitionError  # noqa: PLC0415

    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    await env.runner.approvals.approve(
        (await _pending(env, waiting.execution_id)).approval_id, reviewer=_reviewer(env)
    )
    env.registry.register(
        make_work(description="changed"), executor=InvoiceExecutor(), replace=True
    )
    with pytest.raises(WorkDefinitionError):
        await env.runner.resume(waiting.execution_id, actor=env.actor)


async def test_cancel_invalidates_open_approvals() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    cancelled = await env.runner.cancel(waiting.execution_id, actor=env.actor, reason="duplicate")
    assert cancelled.status is S.CANCELLED
    assert cancelled.status_reason == "duplicate"
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.INVALIDATED
    from id42_work import ApprovalError, ExecutionCancelledError  # noqa: PLC0415

    with pytest.raises(ApprovalError, match="INVALIDATED"):
        await env.runner.approvals.approve(approval.approval_id, reviewer=_reviewer(env))
    with pytest.raises(ExecutionCancelledError):
        cancelled.raise_for_status()
    assert await env.runner.cancel(waiting.execution_id, actor=env.actor) == cancelled


async def test_cancel_after_grant_invalidates_the_granted_approval() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    await env.runner.approvals.approve(approval.approval_id, reviewer=_reviewer(env))
    await env.runner.cancel(waiting.execution_id, actor=env.actor)
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.INVALIDATED


async def test_running_execution_cannot_be_cancelled() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def body(data: Any, context: ExecutionContext) -> Any:
        started.set()
        await release.wait()
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")

    env = Env(executor=FunctionExecutor(body))
    task = asyncio.create_task(env.run())
    await started.wait()
    execution_id = _only_execution_id(env)
    with pytest.raises(InvalidStateTransitionError):
        await env.runner.cancel(execution_id, actor=env.actor)
    release.set()
    assert (await task).status is S.COMPLETED


def _only_execution_id(env: Env) -> Any:
    (execution_id,) = env.storage.executions._executions
    return execution_id


def test_sync_wrappers() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = env.runner.run_sync(
        work_id=env.work.id, version="1.0.0", input_data=INVOICE, actor=env.actor
    )
    (approval,) = env.runner.approvals.list_pending_sync(waiting.execution_id)
    env.runner.approvals.approve_sync(approval.approval_id, reviewer=_reviewer(env))
    review = env.runner.resume_sync(waiting.execution_id, actor=env.actor)
    assert review.status is S.WAITING_REVIEW
    (outcome_approval,) = env.runner.approvals.list_pending_sync(waiting.execution_id)
    env.runner.approvals.reject_sync(
        outcome_approval.approval_id, reviewer=_reviewer(env), reason="no"
    )
    other = env.runner.run_sync(
        work_id=env.work.id, version="1.0.0", input_data=INVOICE, actor=env.actor
    )
    assert env.runner.cancel_sync(other.execution_id, actor=env.actor).status is S.CANCELLED


async def test_sync_wrappers_refuse_inside_a_loop() -> None:
    env = _env(AutonomyLevel.MANUAL)
    waiting = await env.run()
    approval = await _pending(env, waiting.execution_id)
    calls = [
        lambda: env.runner.approvals.approve_sync(approval.approval_id, reviewer=env.actor),
        lambda: env.runner.approvals.reject_sync(
            approval.approval_id, reviewer=env.actor, reason="x"
        ),
        lambda: env.runner.approvals.list_pending_sync(waiting.execution_id),
        lambda: env.runner.resume_sync(waiting.execution_id, actor=env.actor),
        lambda: env.runner.cancel_sync(waiting.execution_id, actor=env.actor),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="running event loop"):
            call()
