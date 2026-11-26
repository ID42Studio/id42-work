"""Attempts to obtain or forge approvals (specification 7.3, 17)."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from _factories import make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    ActorAuthority,
    ApprovalError,
    ApprovalNotAllowedError,
    ApprovalStatus,
    AutonomyLevel,
    ExecutionContext,
    ExecutionError,
    ExecutionStatus,
    PolicyDeniedError,
    UntrustedActorError,
)

S = ExecutionStatus
REVIEW = "approve:supplier_invoice_validation"


def _manual() -> Env:
    return Env(
        policy=make_policy(
            autonomy=AutonomyLevel.MANUAL, allowed_capabilities=frozenset({READ, VALIDATE})
        )
    )


async def _waiting(env: Env) -> tuple[Any, Any]:
    execution = await env.run()
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    return execution, approval


async def _assert_untouched(env: Env, execution: Any, approval: Any) -> None:
    stored = await env.runner.approvals.get(approval.approval_id)
    assert stored == approval
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    assert current.status is S.WAITING_APPROVAL


async def test_actor_cannot_approve_its_own_execution() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    self_reviewer = env.issue("user:42", (READ, VALIDATE, REVIEW))
    with pytest.raises(ApprovalNotAllowedError, match="own execution"):
        await env.runner.approvals.approve(approval.approval_id, reviewer=self_reviewer)
    await _assert_untouched(env, execution, approval)


async def test_reviewer_needs_the_exact_grant() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    for grants in [
        (),
        (READ, VALIDATE),
        ("approve:other_work",),
        ("resume:supplier_invoice_validation",),
    ]:
        with pytest.raises(ApprovalNotAllowedError, match="lacks the grant"):
            await env.runner.approvals.approve(
                approval.approval_id, reviewer=env.issue("reviewer:1", grants)
            )
    await _assert_untouched(env, execution, approval)


async def test_reviewer_from_another_tenant_is_refused() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    outsider = env.authority.issue(
        actor_id="reviewer:9", tenant_id="globex", grants={REVIEW}, ttl=timedelta(minutes=5)
    )
    with pytest.raises(ApprovalNotAllowedError, match="tenant"):
        await env.runner.approvals.approve(approval.approval_id, reviewer=outsider)
    await _assert_untouched(env, execution, approval)


@pytest.mark.parametrize(
    "candidate", ["reviewer:7", {"actor_id": "reviewer:7", "grants": [REVIEW]}]
)
async def test_untrusted_reviewer_identity_is_refused(candidate: object) -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    with pytest.raises(UntrustedActorError):
        await env.runner.approvals.approve(approval.approval_id, reviewer=candidate)
    await _assert_untouched(env, execution, approval)


async def test_forged_or_foreign_reviewer_is_refused() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    weak = env.issue("reviewer:7", ())
    forged = weak.model_copy(update={"grants": frozenset({REVIEW})})
    foreign = ActorAuthority("host-app", clock=env.clock).issue(
        actor_id="reviewer:7", tenant_id="acme", grants={REVIEW}, ttl=timedelta(minutes=5)
    )
    for reviewer in (forged, foreign):
        with pytest.raises(UntrustedActorError):
            await env.runner.approvals.approve(approval.approval_id, reviewer=reviewer)
    await _assert_untouched(env, execution, approval)


async def test_there_is_no_approval_by_execution_id() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    with pytest.raises(ApprovalError, match="does not exist"):
        await env.runner.approvals.approve(
            execution.execution_id, reviewer=env.issue("reviewer:7", (REVIEW,))
        )
    await _assert_untouched(env, execution, approval)


@pytest.mark.parametrize("reason", ["", "   ", "x" * 2001])
async def test_rejection_requires_a_real_reason(reason: str) -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    with pytest.raises(ApprovalError, match="reason"):
        await env.runner.approvals.reject(
            approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,)), reason=reason
        )
    await _assert_untouched(env, execution, approval)


async def test_an_approval_is_decided_once() -> None:
    env = _manual()
    _, approval = await _waiting(env)
    reviewer = env.issue("reviewer:7", (REVIEW,))
    await env.runner.approvals.approve(approval.approval_id, reviewer=reviewer)
    with pytest.raises(ApprovalError, match="GRANTED, not PENDING"):
        await env.runner.approvals.reject(approval.approval_id, reviewer=reviewer, reason="late")


async def test_concurrent_approve_and_reject_have_one_winner() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    reviewers = [env.issue(f"reviewer:{i}", (REVIEW,)) for i in range(6)]
    calls = [
        env.runner.approvals.approve(approval.approval_id, reviewer=r)
        if i % 2
        else env.runner.approvals.reject(approval.approval_id, reviewer=r, reason="no")
        for i, r in enumerate(reviewers)
    ]
    results = await asyncio.gather(*calls, return_exceptions=True)
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(r, ApprovalError) for r in results if isinstance(r, BaseException))
    current = await env.runner.get(execution.execution_id)
    assert current is not None
    expected = S.READY if winners[0].status is ApprovalStatus.GRANTED else S.REJECTED
    assert current.status is expected


async def test_executor_context_exposes_no_approval_or_storage_handles() -> None:
    seen: dict[str, bool] = {}

    async def body(data: Any, context: ExecutionContext) -> Any:
        public = {name for name in dir(context) if not name.startswith("_")}
        seen["clean"] = public <= {
            "execution_id",
            "work_id",
            "work_version",
            "actor_id",
            "tenant_id",
            "granted_capabilities",
            "logger",
            "invoke",
            "record",
            "capability_specs",
            "add_evidence",
            "drain_evidence",
        }
        raise RuntimeError("stop")

    await Env(executor=FunctionExecutor(body)).run()
    assert seen == {"clean": True}


async def test_resume_and_cancel_by_other_actors() -> None:
    env = _manual()
    execution, approval = await _waiting(env)
    await env.runner.approvals.approve(
        approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    stranger = env.issue("user:99", (READ, VALIDATE))
    with pytest.raises(PolicyDeniedError) as info:
        await env.runner.resume(execution.execution_id, actor=stranger)
    assert info.value.reasons == ("resume_not_allowed",)
    with pytest.raises(PolicyDeniedError) as info:
        await env.runner.cancel(execution.execution_id, actor=stranger)
    assert info.value.reasons == ("cancel_not_allowed",)

    operator = env.issue("ops:1", (READ, VALIDATE, "resume:supplier_invoice_validation"))
    resumed = await env.runner.resume(execution.execution_id, actor=operator)
    assert resumed.status is S.WAITING_REVIEW
    assert resumed.actor_id == "user:42"  # the execution keeps its original actor
    canceller = env.issue("ops:2", ("cancel:supplier_invoice_validation",))
    cancelled = await env.runner.cancel(execution.execution_id, actor=canceller, reason="ops")
    assert cancelled.status is S.CANCELLED


async def test_operator_from_another_tenant_cannot_resume() -> None:
    env = _manual()
    execution, _ = await _waiting(env)
    outsider = env.authority.issue(
        actor_id="user:42",
        tenant_id="globex",
        grants={"resume:supplier_invoice_validation"},
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(PolicyDeniedError):
        await env.runner.resume(execution.execution_id, actor=outsider)


async def test_unknown_execution_and_bad_cancel_reason() -> None:
    env = _manual()
    with pytest.raises(ExecutionError, match="does not exist"):
        await env.runner.resume(uuid4(), actor=env.actor)
    with pytest.raises(ExecutionError, match="reason"):
        await env.runner.cancel(uuid4(), actor=env.actor, reason=" ")


async def test_provider_sees_the_execution_actor_after_operator_resume() -> None:
    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED, allowed_capabilities=frozenset({READ, PREPARE})
        ),
    )
    execution = await env.run(idempotency_key="k")
    (approval,) = await env.runner.approvals.list_pending(execution.execution_id)
    await env.runner.approvals.approve(
        approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEW,))
    )
    operator = env.issue("ops:1", (READ, PREPARE, "resume:supplier_invoice_validation"))
    done = await env.runner.resume(execution.execution_id, actor=operator)
    assert done.status is S.COMPLETED
    (_, _, call) = next(c for c in env.provider.calls if c[0] == PREPARE)
    assert call.actor_id == "user:42"
