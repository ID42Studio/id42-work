"""Race guards and integrity checks around approvals and resumption."""

import contextlib
from datetime import timedelta
from typing import Any

import pytest

from _factories import InvoiceOutput, make_outcome, make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, FunctionExecutor
from id42_work import (
    Approval,
    ApprovalError,
    ApprovalStatus,
    AutonomyLevel,
    ConcurrentModificationError,
    Execution,
    ExecutionContext,
    ExecutionStatus,
    StorageError,
)

S = ExecutionStatus
REVIEW = "approve:supplier_invoice_validation"


def _env(level: AutonomyLevel) -> Env:
    return Env(policy=make_policy(autonomy=level, allowed_capabilities=frozenset({READ, VALIDATE})))


async def _pending(env: Env, execution: Execution) -> Approval:
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    return approval


async def test_lost_race_on_decision_records_nothing_more() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    approval = await _pending(env, execution)

    async def conflict(updated: Approval, *, expected_status: ApprovalStatus) -> None:
        raise ConcurrentModificationError("another reviewer won")

    env.storage.approvals.compare_and_set = conflict  # type: ignore[method-assign]
    with pytest.raises(ApprovalError, match="concurrently"):
        await env.runner.approvals.approve(
            approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
        )
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    assert current.status is S.WAITING_APPROVAL


async def test_lost_race_on_expiry_is_skipped() -> None:
    env = _env(AutonomyLevel.MANUAL)
    await env.run()
    env.clock.now += timedelta(hours=25)

    async def conflict(updated: Approval, *, expected_status: ApprovalStatus) -> None:
        raise ConcurrentModificationError("decided meanwhile")

    env.storage.approvals.compare_and_set = conflict  # type: ignore[method-assign]
    assert await env.runner.expire_due() == 0


async def test_expiry_tolerates_an_execution_that_moved_on() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    env.clock.now += timedelta(hours=25)
    original = env.storage.executions.replace
    original_get = env.storage.executions.get
    conflicted = {"done": False}

    def conflict(updated: Execution) -> None:
        if updated.status is S.CANCELLED:
            conflicted["done"] = True
            raise ConcurrentModificationError("execution changed meanwhile")
        original(updated)

    async def newer(execution_id: Any) -> Execution | None:
        current = await original_get(execution_id)
        if conflicted["done"] and current is not None:
            return current.model_copy(update={"revision": current.revision + 1})
        return current

    env.storage.executions.replace = conflict  # type: ignore[method-assign]
    env.storage.executions.get = newer  # type: ignore[method-assign]
    assert await env.runner.expire_due() == 1
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    assert current.status is S.WAITING_APPROVAL


async def test_tampered_outcome_cannot_be_accepted() -> None:
    env = _env(AutonomyLevel.GUIDED)
    execution = await env.run()
    approval = await _pending(env, execution)
    tampered_output = InvoiceOutput(
        invoice_number="INV-9999", supplier_id="SUP-001", status="valid"
    )
    stored = await env.runner.get(execution.execution_id)
    assert stored is not None
    assert stored.outcome is not None
    tampered = stored.model_copy(
        update={"outcome": stored.outcome.model_copy(update={"output": tampered_output})}
    )
    env.storage.executions._executions[execution.execution_id] = tampered
    with pytest.raises(ApprovalError, match="does not match"):
        await env.runner.approvals.approve(
            approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
        )


async def test_missing_input_prevents_resume() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    await env.runner.approvals.approve(
        (await _pending(env, execution)).approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    env.storage.inputs._inputs.clear()
    with pytest.raises(StorageError, match="not available"):
        await env.runner.resume(execution.execution_id, actor=env.actor)


async def test_claim_is_exclusive_and_released() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    await env.runner.approvals.approve(
        (await _pending(env, execution)).approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    claim = env.runner._claim
    with claim(execution.execution_id) as first:
        assert first
        with claim(execution.execution_id) as second:
            assert not second
        unchanged = await env.runner.resume(execution.execution_id, actor=env.actor)
        assert unchanged.status is S.READY
    resumed = await env.runner.resume(execution.execution_id, actor=env.actor)
    assert resumed.status is S.WAITING_REVIEW


async def test_replayed_failed_action_with_new_params_is_non_deterministic() -> None:
    runs = {"n": 0}

    async def body(data: Any, context: ExecutionContext) -> Any:
        runs["n"] += 1
        with contextlib.suppress(Exception):
            await context.invoke(READ, action_id="load", params={"attempt": runs["n"]})
        await context.invoke(PREPARE, action_id="prepare", params={})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="ok")

    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED, allowed_capabilities=frozenset({READ, PREPARE})
        ),
    )

    def flaky(capability: str, params: Any) -> object:
        raise ConnectionError("down")

    env.provider.behaviour = flaky
    execution = await env.run(idempotency_key="k")
    await env.runner.approvals.approve(
        (await _pending(env, execution)).approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    failed = await env.runner.resume(execution.execution_id, actor=env.actor)
    assert failed.status_reason == "non_deterministic"


def test_outcome_accepted_requires_verification() -> None:
    from uuid import uuid4  # noqa: PLC0415

    from _factories import T0  # noqa: PLC0415

    unverified = make_outcome(uuid4(), passed=False)
    with pytest.raises(ValueError, match="unverified"):
        unverified.accepted(T0)
    verified = make_outcome(uuid4())
    assert verified.accepted(T0 - timedelta(days=1)).accepted_at == verified.produced_at


async def test_approval_left_open_on_a_finished_execution_cannot_be_decided() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    approval = await _pending(env, execution)

    async def conflict(updated: Approval, *, expected_status: ApprovalStatus) -> None:
        raise ConcurrentModificationError("simulated race")

    original = env.storage.approvals.compare_and_set
    env.storage.approvals.compare_and_set = conflict  # type: ignore[method-assign]
    await env.runner.cancel(execution.execution_id, actor=env.actor)
    env.storage.approvals.compare_and_set = original  # type: ignore[method-assign]
    with pytest.raises(ApprovalError, match="not waiting"):
        await env.runner.approvals.approve(
            approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
        )


async def test_gate_reuses_an_identical_pending_request_and_retires_expired_grants() -> None:
    env = _env(AutonomyLevel.MANUAL)
    execution = await env.run()
    approval = await _pending(env, execution)
    recorder = env.runner._rec
    from id42_work import ApprovalGate, ApprovalRequirement  # noqa: PLC0415

    requirement = ApprovalRequirement(gate=ApprovalGate.START, reviewer_capability=REVIEW)
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    same = await recorder.check_gate(
        current, requirement, approval.action_id, approval.action_digest, {}
    )
    assert same is False
    assert len(await env.runner.approvals_for(execution.execution_id)) == 1

    await env.runner.approvals.approve(
        approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    env.clock.now += timedelta(hours=25)
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    renewed = await recorder.check_gate(
        current, requirement, approval.action_id, approval.action_digest, {}
    )
    assert renewed is False
    statuses = sorted(
        a.status.value for a in await env.runner.approvals_for(execution.execution_id)
    )
    assert statuses == ["EXPIRED", "PENDING"]
