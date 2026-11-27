"""ApprovalConsole: host decisions outside the MCP server."""

import argparse
from datetime import timedelta
from uuid import uuid4

import pytest

from _factories import make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, InvoiceExecutor
from approval_host import reset
from approval_host import server as approval_server
from id42_work import (
    ApprovalError,
    ApprovalGate,
    ApprovalRequirement,
    ApprovalStatus,
    AutonomyLevel,
    EventType,
    EvidenceType,
    ExecutionStatus,
)
from id42_work.approvals import START_ACTION_ID
from id42_work.approvals.commands import (
    format_decision,
    format_pending,
    format_show,
    run_approval,
)
from id42_work.approvals.console import (
    ApprovalConsole,
    ApprovalDecision,
    ApprovalResumeError,
    console_from_server,
)
from id42_work.approvals.models import Approval, compute_action_digest
from id42_work.journal import JournalStatus
from id42_work.state_machine import transition_execution
from mcp_support import greeting_server

S = ExecutionStatus
REVIEW = "approve:supplier_invoice_validation"


def _supervised() -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_capabilities=frozenset({READ, PREPARE}),
        ),
    )


def _console(env: Env, resume_actor: object | None = None) -> ApprovalConsole:
    def issue() -> object:
        return env.actor

    return ApprovalConsole(
        env.runner,
        authority=env.authority,
        resume_actor=issue if resume_actor is None else resume_actor,
    )


async def _pending(env: Env):
    execution = await env.run(idempotency_key="k-1")
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    return execution, approval


def test_constructor_rejects_bad_wiring() -> None:
    env = _supervised()
    with pytest.raises(TypeError, match="WorkRunner"):
        ApprovalConsole(object(), authority=env.authority, resume_actor=lambda: env.actor)
    with pytest.raises(TypeError, match="ActorAuthority"):
        ApprovalConsole(env.runner, authority=object(), resume_actor=lambda: env.actor)
    with pytest.raises(TypeError, match="callable"):
        ApprovalConsole(env.runner, authority=env.authority, resume_actor=object())
    with pytest.raises(ValueError, match="operator_ttl"):
        ApprovalConsole(
            env.runner,
            authority=env.authority,
            resume_actor=lambda: env.actor,
            operator_ttl=timedelta(0),
        )


def test_console_from_server_rejects_a_partial_host() -> None:
    env = _supervised()
    with pytest.raises(TypeError, match="WorkMcpServer"):
        console_from_server(object())

    class Incomplete:
        def __init__(self) -> None:
            self.runner = env.runner
            self.authority = env.authority
            self.host_actor = None

    with pytest.raises(TypeError, match="WorkMcpServer"):
        console_from_server(Incomplete())


async def test_grant_records_the_operator_and_does_not_run_the_effect() -> None:
    env = _supervised()
    execution, approval = await _pending(env)
    decision = await _console(env).approve(
        approval.approval_id, actor_id=" reviewer:7 ", reason="PO matched"
    )
    assert decision.resumed is False
    assert decision.approval.status is ApprovalStatus.GRANTED
    assert decision.approval.decision_actor_id == "reviewer:7"
    assert decision.approval.reason == "PO matched"
    assert decision.execution.status is S.READY
    assert env.provider.count(PREPARE) == 0
    evidence = [
        item
        for item in await env.runner.evidence(execution.execution_id)
        if item.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    assert evidence[-1].metadata["decision_actor_id"] == "reviewer:7"
    assert evidence[-1].metadata["reason"] == "PO matched"
    assert evidence[-1].metadata["reason_provided"] is True
    granted = [
        event
        for event in await env.runner.events(execution.execution_id)
        if event.event_type is EventType.APPROVAL_GRANTED
    ]
    assert granted[-1].payload["decision_actor_id"] == "reviewer:7"
    journal = await env.runner.journal(execution.execution_id)
    assert {entry.action_id: entry.status for entry in journal}["prepare-invoice"] is (
        JournalStatus.PENDING_APPROVAL
    )


async def test_resume_runs_as_the_host_actor() -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    seen: list[str] = []

    def resume() -> object:
        seen.append(env.actor.actor_id)
        return env.actor

    decision = await _console(env, resume).approve(
        approval.approval_id, actor_id="reviewer:7", reason="PO matched", resume=True
    )
    assert seen == ["user:42"]
    assert decision.resumed is True
    assert decision.approval.decision_actor_id == "reviewer:7"
    assert decision.execution.status is S.COMPLETED
    assert env.provider.count(PREPARE) == 1
    assert "resumed=true" in format_decision("approved", decision)


async def test_resume_failure_keeps_the_grant() -> None:
    env = _supervised()
    execution, approval = await _pending(env)

    def resume() -> object:
        raise RuntimeError("host actor unavailable")

    with pytest.raises(ApprovalResumeError, match="was granted") as raised:
        await _console(env, resume).approve(
            approval.approval_id, actor_id="reviewer:7", resume=True
        )
    assert raised.value.decision.resumed is False
    assert raised.value.decision.execution.status is S.READY
    assert env.provider.count(PREPARE) == 0
    stored = await env.runner.get(execution.execution_id)
    assert stored is not None
    assert stored.status is S.READY


async def test_rejection_ends_the_execution() -> None:
    env = _supervised()
    execution, approval = await _pending(env)
    decision = await _console(env).reject(
        approval.approval_id, actor_id="reviewer:7", reason="duplicate bill"
    )
    assert decision.resumed is False
    assert decision.approval.status is ApprovalStatus.REJECTED
    assert decision.approval.reason == "duplicate bill"
    assert decision.approval.decision_actor_id == "reviewer:7"
    assert decision.execution.status is S.REJECTED
    assert decision.execution.status.is_terminal
    evidence = [
        item
        for item in await env.runner.evidence(execution.execution_id)
        if item.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    assert evidence[-1].metadata["reason"] == "duplicate bill"
    assert evidence[-1].metadata["decision"] == "REJECTED"
    assert env.provider.count(PREPARE) == 0


async def test_outcome_approval_completes_without_resume() -> None:
    env = Env(
        policy=make_policy(
            autonomy=AutonomyLevel.GUIDED,
            allowed_capabilities=frozenset({READ, VALIDATE}),
        )
    )
    waiting = await env.run()
    assert waiting.status is S.WAITING_REVIEW
    (approval,) = await env.runner.approvals.list_pending(waiting.execution_id)
    assert approval.gate is ApprovalGate.OUTCOME
    decision = await _console(env).approve(approval.approval_id, actor_id="reviewer:7")
    assert decision.approval.status is ApprovalStatus.CONSUMED
    assert decision.execution.status is S.COMPLETED
    assert decision.resumed is False
    evidence = [
        item
        for item in await env.runner.evidence(waiting.execution_id)
        if item.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    assert evidence[-1].metadata["reason"] is None
    assert evidence[-1].metadata["reason_provided"] is False


async def test_list_skips_executions_that_are_not_waiting() -> None:
    env = _supervised()
    first, first_approval = await _pending(env)
    await _console(env).approve(first_approval.approval_id, actor_id="reviewer:7")
    second = await env.run(idempotency_key="k-2")
    listed = await _console(env).list_pending()
    assert [row.execution.execution_id for row in listed] == [second.execution_id]
    assert first.execution_id not in {row.execution.execution_id for row in listed}
    line = format_pending(listed[0])
    assert "capability=odoo.invoice.prepare" in line
    assert "execution_status=WAITING_APPROVAL" in line


async def test_start_gate_line_has_no_capability() -> None:
    env = Env(
        policy=make_policy(
            autonomy=AutonomyLevel.MANUAL,
            allowed_capabilities=frozenset({READ, VALIDATE}),
        )
    )
    await env.run()
    (row,) = await _console(env).list_pending()
    assert row.approval.action_id == START_ACTION_ID
    line = format_pending(row)
    assert "gate=START" in line
    assert "capability=-" in line
    shown = format_show(row)
    assert '"decided_at": null' in shown
    assert '"decision_actor_id": null' in shown
    missing_actor = format_decision(
        "approved",
        ApprovalDecision(approval=row.approval, execution=row.execution, resumed=False),
    )
    assert "decision_actor_id=-" in missing_actor
    assert "resumed=false" in missing_actor


async def test_show_reports_a_stored_decision_and_an_expiry() -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    await _console(env).reject(approval.approval_id, actor_id="reviewer:7", reason="no")
    shown = await _console(env).show(approval.approval_id)
    rendered = format_show(shown)
    assert shown.approval.status is ApprovalStatus.REJECTED
    assert '"decision_actor_id": "reviewer:7"' in rendered
    assert shown.approval.decided_at is not None

    fresh = _supervised()
    _waiting, pending = await _pending(fresh)
    fresh.clock.now = fresh.clock.now + timedelta(hours=25)
    expired = await _console(fresh).show(pending.approval_id)
    assert expired.approval.status is ApprovalStatus.EXPIRED
    assert expired.execution.status is S.CANCELLED
    assert await _console(fresh).list_pending() == ()
    waiting = _supervised()
    await _pending(waiting)
    waiting.clock.now = waiting.clock.now + timedelta(hours=25)
    assert await _console(waiting).list_pending() == ()


async def test_show_keeps_the_approval_when_a_refresh_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    calls = {"n": 0}
    original = env.runner.approvals.get

    async def flaky(approval_id: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 2:
            return None
        return await original(approval_id)  # type: ignore[arg-type]

    monkeypatch.setattr(env.runner.approvals, "get", flaky)
    shown = await _console(env).show(approval.approval_id)
    assert shown.approval.approval_id == approval.approval_id
    assert shown.approval.status is ApprovalStatus.PENDING


async def test_a_missing_execution_row_after_the_grant_uses_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    calls = {"n": 0}
    original = env.runner.get

    async def flaky(execution_id: object) -> object:
        calls["n"] += 1
        if calls["n"] >= 2:
            return None
        return await original(execution_id)  # type: ignore[arg-type]

    monkeypatch.setattr(env.runner, "get", flaky)
    decision = await _console(env).approve(approval.approval_id, actor_id="reviewer:7")
    assert decision.approval.status is ApprovalStatus.GRANTED
    assert decision.execution.status is S.WAITING_APPROVAL


async def test_an_orphan_approval_is_refused() -> None:
    env = _supervised()
    execution_id = uuid4()
    approval = Approval(
        execution_id=execution_id,
        gate=ApprovalGate.START,
        action_id=START_ACTION_ID,
        action_digest=compute_action_digest(
            execution_id=execution_id,
            work_id="supplier_invoice_validation",
            work_version="1.0.0",
            definition_digest="a" * 64,
            gate=ApprovalGate.START,
            action_id=START_ACTION_ID,
            capability=None,
            params={},
        ),
        reviewer_capability=REVIEW,
        requested_at=env.clock.now,
        expires_at=env.clock.now + timedelta(hours=1),
    )
    await env.storage.approvals.create(approval)
    with pytest.raises(ApprovalError, match="missing"):
        await _console(env).show(approval.approval_id)
    with pytest.raises(ApprovalError, match="missing"):
        await _console(env).approve(approval.approval_id, actor_id="reviewer:7")


async def test_operator_arguments_are_exclusive() -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    console = _console(env)
    reviewer = env.issue("reviewer:7", (REVIEW,))
    with pytest.raises(ApprovalError, match="not both"):
        await console.approve(approval.approval_id)
    with pytest.raises(ApprovalError, match="not both"):
        await console.approve(approval.approval_id, actor_id="reviewer:7", reviewer=reviewer)
    with pytest.raises(ApprovalError, match="non-blank"):
        await console.approve(approval.approval_id, actor_id="   ")
    with pytest.raises(ApprovalError, match="not a valid operator"):
        await console.approve(approval.approval_id, actor_id="r" * 300)
    with pytest.raises(ApprovalError, match="does not exist"):
        await console.show(uuid4())


async def test_an_explicit_reviewer_can_decide() -> None:
    env = _supervised()
    _execution, approval = await _pending(env)
    reviewer = env.issue("reviewer:7", (REVIEW,))
    decision = await _console(env).approve(approval.approval_id, reviewer=reviewer, reason="ok")
    assert decision.approval.decision_actor_id == "reviewer:7"


async def test_reason_required_by_the_gate_is_enforced() -> None:
    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_capabilities=frozenset({READ, PREPARE}),
            approval_requirements=(
                ApprovalRequirement(
                    gate=ApprovalGate.ACTION,
                    capability=PREPARE,
                    reviewer_capability=REVIEW,
                    reason_required=True,
                ),
            ),
        ),
    )
    _execution, approval = await _pending(env)
    with pytest.raises(ApprovalError, match="reason"):
        await _console(env).approve(approval.approval_id, actor_id="reviewer:7")
    decision = await _console(env).approve(
        approval.approval_id, actor_id="reviewer:7", reason="matched"
    )
    assert decision.approval.reason == "matched"


async def test_execution_left_waiting_is_not_decided() -> None:
    env = _supervised()
    execution, approval = await _pending(env)
    cancelled = transition_execution(execution, S.CANCELLED, at=env.clock.now, reason="stop")
    await env.storage.executions.compare_and_set(cancelled)
    with pytest.raises(ApprovalError, match="not waiting"):
        await _console(env).reject(approval.approval_id, actor_id="reviewer:7", reason="late")
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored is not None
    assert stored.status is ApprovalStatus.PENDING


def test_sync_wrappers_decide_outside_a_loop() -> None:
    env = _supervised()
    execution = env.runner.run_sync(
        work_id=env.work.id,
        version=env.work.version,
        input_data={
            "invoice_number": "INV-1001",
            "supplier_id": "SUP-001",
            "currency": "BRL",
            "amount": "1250.00",
        },
        actor=env.actor,
        idempotency_key="sync-1",
    )
    console = _console(env)
    listed = console.list_pending_sync()
    assert len(listed) == 1
    shown = console.show_sync(listed[0].approval.approval_id)
    assert shown.execution.execution_id == execution.execution_id
    granted = console.approve_sync(listed[0].approval.approval_id, actor_id="reviewer:7")
    assert granted.execution.status is S.READY

    second = env.runner.run_sync(
        work_id=env.work.id,
        version=env.work.version,
        input_data={
            "invoice_number": "INV-1002",
            "supplier_id": "SUP-001",
            "currency": "BRL",
            "amount": "10.00",
        },
        actor=env.actor,
        idempotency_key="sync-2",
    )
    (pending,) = console.list_pending_sync()
    assert pending.execution.execution_id == second.execution_id
    rejected = console.reject_sync(pending.approval.approval_id, actor_id="reviewer:8", reason="no")
    assert rejected.execution.status is S.REJECTED


async def test_sync_wrappers_refuse_a_running_loop() -> None:
    env = _supervised()
    console = _console(env)
    with pytest.raises(RuntimeError, match="list_pending_sync"):
        console.list_pending_sync()
    with pytest.raises(RuntimeError, match="show_sync"):
        console.show_sync(uuid4())
    with pytest.raises(RuntimeError, match="approve_sync"):
        console.approve_sync(uuid4(), actor_id="reviewer:7")
    with pytest.raises(RuntimeError, match="reject_sync"):
        console.reject_sync(uuid4(), actor_id="reviewer:7", reason="no")


async def test_command_validation_stops_before_a_decision() -> None:
    hosted, _runner, _provider = greeting_server()

    async def run(command: str, **fields: object) -> None:
        payload: dict[str, object] = {"approvals_command": command, "approval_id": str(uuid4())}
        payload.update(fields)
        await run_approval(hosted, argparse.Namespace(**payload))

    with pytest.raises(ValueError, match="unknown approvals command"):
        await run("drop")
    with pytest.raises(ValueError, match="UUID"):
        await run("show", approval_id=1)
    with pytest.raises(ValueError, match="UUID"):
        await run("show", approval_id="not-a-uuid")
    with pytest.raises(ValueError, match="flag"):
        await run("approve", actor="reviewer:7", reason=None, resume="yes")
    with pytest.raises(ValueError, match="reason must be a string"):
        await run("approve", actor="reviewer:7", reason=1, resume=False)
    with pytest.raises(ValueError, match="actor"):
        await run("approve", actor=1, reason=None, resume=False)
    with pytest.raises(ValueError, match="actor"):
        await run("reject", actor="  ", reason="no", resume=False)


def test_server_exposes_runner_and_host_actor_without_a_tool() -> None:
    reset()
    hosted = approval_server()
    assert hosted.runner is hosted._runner
    assert hosted.authority is hosted._authority
    actor = hosted.host_actor()
    assert actor.actor_id == "id42-agent"
    built = console_from_server(hosted)
    assert isinstance(built, ApprovalConsole)
