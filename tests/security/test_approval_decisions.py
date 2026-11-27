"""An operator decision cannot widen the execution actor or be decided twice."""

import asyncio
from datetime import timedelta

import pytest

from _factories import make_policy, make_work
from _runtime import PREPARE, READ, Env, InvoiceExecutor
from id42_work import (
    ActorAuthority,
    Approval,
    ApprovalError,
    ApprovalGate,
    ApprovalNotAllowedError,
    ApprovalRequirement,
    ApprovalStatus,
    AutonomyLevel,
    EvidenceType,
    Execution,
    ExecutionStatus,
    UntrustedActorError,
)
from id42_work.approvals.console import ApprovalConsole, ApprovalDecision

REVIEW = "approve:supplier_invoice_validation"


def _env(**policy: object) -> Env:
    fields = {
        "autonomy": AutonomyLevel.SUPERVISED,
        "allowed_capabilities": frozenset({READ, PREPARE}),
    }
    fields.update(policy)
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(**fields),
    )


def _console(env: Env) -> ApprovalConsole:
    return ApprovalConsole(env.runner, authority=env.authority, resume_actor=lambda: env.actor)


async def _pending(env: Env) -> tuple[Execution, Approval]:
    execution = await env.run(idempotency_key="sec-1")
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    return execution, approval


async def _untouched(env: Env, execution: Execution, approval: Approval) -> None:
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored == approval
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    assert current.status is ExecutionStatus.WAITING_APPROVAL
    assert env.provider.count(PREPARE) == 0


async def test_the_operator_receives_only_the_reviewer_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _env(
        approval_requirements=(
            ApprovalRequirement(
                gate=ApprovalGate.ACTION,
                capability=PREPARE,
                reviewer_capability="approve:finance",
            ),
        )
    )
    _execution, approval = await _pending(env)
    issued: list[dict[str, object]] = []
    original = ActorAuthority.issue

    def spy(self: ActorAuthority, **kwargs: object) -> object:
        issued.append(kwargs)
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ActorAuthority, "issue", spy)
    decision = await _console(env).approve(approval.approval_id, actor_id="id42-operator")
    assert decision.approval.decision_actor_id == "id42-operator"
    grants = issued[0]["grants"]
    assert grants == frozenset({"approve:finance"})
    assert issued[0]["tenant_id"] == "acme"
    assert isinstance(grants, frozenset)
    assert PREPARE not in grants
    assert env.provider.count(PREPARE) == 0


async def test_the_execution_actor_cannot_approve_itself() -> None:
    env = _env()
    execution, approval = await _pending(env)
    with pytest.raises(ApprovalNotAllowedError, match="own execution"):
        await _console(env).approve(approval.approval_id, actor_id=execution.actor_id)
    await _untouched(env, execution, approval)


async def test_a_reviewer_without_the_grant_is_refused() -> None:
    env = _env()
    execution, approval = await _pending(env)
    weak = env.issue("reviewer:7", ())
    with pytest.raises(ApprovalNotAllowedError, match="lacks the grant"):
        await _console(env).approve(approval.approval_id, reviewer=weak)
    await _untouched(env, execution, approval)


async def test_an_untrusted_or_foreign_reviewer_is_refused() -> None:
    env = _env()
    execution, approval = await _pending(env)
    foreign = ActorAuthority("other-host", clock=env.clock).issue(
        actor_id="reviewer:7",
        tenant_id="acme",
        grants={REVIEW},
        ttl=timedelta(minutes=5),
    )
    for reviewer in ("reviewer:7", foreign):
        with pytest.raises(UntrustedActorError):
            await _console(env).approve(approval.approval_id, reviewer=reviewer)
    await _untouched(env, execution, approval)


async def test_a_reviewer_from_another_tenant_is_refused() -> None:
    env = _env()
    execution, approval = await _pending(env)
    outsider = env.authority.issue(
        actor_id="reviewer:9",
        tenant_id="globex",
        grants={REVIEW},
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(ApprovalNotAllowedError, match="tenant"):
        await _console(env).approve(approval.approval_id, reviewer=outsider)
    await _untouched(env, execution, approval)


async def test_a_decided_approval_is_refused_and_not_rewritten() -> None:
    env = _env()
    execution, approval = await _pending(env)
    console = _console(env)
    granted = await console.approve(approval.approval_id, actor_id="reviewer:7", reason="ok")
    evidence = [
        item
        for item in await env.runner.evidence(execution.execution_id)
        if item.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    with pytest.raises(ApprovalError, match="not PENDING"):
        await console.reject(approval.approval_id, actor_id="reviewer:8", reason="late")
    with pytest.raises(ApprovalError, match="not PENDING"):
        await console.approve(approval.approval_id, actor_id="reviewer:8")
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    assert current.status is ExecutionStatus.READY
    assert current.revision == granted.execution.revision
    again = [
        item
        for item in await env.runner.evidence(execution.execution_id)
        if item.evidence_type is EvidenceType.APPROVAL_DECISION
    ]
    assert len(again) == len(evidence) == 1
    assert again[0].metadata["decision_actor_id"] == "reviewer:7"
    assert again[0].metadata["reason"] == "ok"


async def test_concurrent_decisions_have_one_winner() -> None:
    env = _env()
    execution, approval = await _pending(env)
    console = _console(env)

    async def decide(index: int) -> object:
        if index % 2:
            return await console.approve(approval.approval_id, actor_id=f"reviewer:{index}")
        return await console.reject(approval.approval_id, actor_id=f"reviewer:{index}", reason="no")

    results = await asyncio.gather(*(decide(i) for i in range(6)), return_exceptions=True)
    winners = [result for result in results if isinstance(result, ApprovalDecision)]
    assert len(winners) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures
    assert all(isinstance(result, ApprovalError) for result in failures)
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    winner = winners[0]
    assert winner.approval.status in {ApprovalStatus.GRANTED, ApprovalStatus.REJECTED}
    expected = (
        ExecutionStatus.READY
        if winner.approval.status is ApprovalStatus.GRANTED
        else ExecutionStatus.REJECTED
    )
    assert current.status is expected
    assert env.provider.count(PREPARE) == 0
