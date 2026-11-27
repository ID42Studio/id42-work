"""Approval decisions and expiry (specification 7).

The SDK records decisions; it does not authenticate reviewers. The host authenticates
them and issues a ``TrustedActor`` through the same ``ActorAuthority`` the runner uses.
Every check runs before anything is written: a refused decision records nothing.

Not re-exported from ``id42_work.approvals`` (import cycle with the runner's internal
services); reach it through ``WorkRunner.approvals`` or import ``ApprovalManager`` from
``id42_work``.
"""

import asyncio
from datetime import datetime
from uuid import UUID

from id42_work._recorder import ExecutionRecorder
from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.approvals.models import OUTCOME_ACTION_ID, Approval, ApprovalStatus
from id42_work.events.models import EventType
from id42_work.evidence.models import EvidenceType
from id42_work.exceptions import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAllowedError,
    ConcurrentModificationError,
    InvalidStateTransitionError,
)
from id42_work.execution import Execution
from id42_work.policy.models import ApprovalGate
from id42_work.state_machine import transition_approval
from id42_work.states import ExecutionStatus

_MAX_REASON = 2000
_WAITING_FOR = {
    ApprovalGate.START: ExecutionStatus.WAITING_APPROVAL,
    ApprovalGate.ACTION: ExecutionStatus.WAITING_APPROVAL,
    ApprovalGate.OUTCOME: ExecutionStatus.WAITING_REVIEW,
}


class ApprovalManager:
    """Lists, decides and expires approvals."""

    def __init__(self, *, recorder: ExecutionRecorder, actor_authority: ActorAuthority) -> None:
        self._rec = recorder
        self._authority = actor_authority

    # ------------------------------------------------------------------ queries

    async def get(self, approval_id: UUID) -> Approval | None:
        """An approval by id."""
        return await self._rec.storage.approvals.get(approval_id)

    async def list_pending(self, execution_id: UUID) -> tuple[Approval, ...]:
        """Pending approvals of an execution, after expiring the ones that are due."""
        await self.expire_for_execution(execution_id)
        return await self._rec.storage.approvals.list_for_execution(
            execution_id, status=ApprovalStatus.PENDING
        )

    # ------------------------------------------------------------------ decisions

    async def approve(
        self, approval_id: UUID, *, reviewer: object, reason: str | None = None
    ) -> Approval:
        """Grant an approval.

        START and ACTION approvals move the execution to ``READY`` (call
        ``WorkRunner.resume`` next). An OUTCOME approval completes the execution.

        Raises ``UntrustedActorError``, ``ApprovalNotAllowedError`` (missing reviewer
        grant, self-approval, other tenant), ``ApprovalExpiredError`` or
        ``ApprovalError`` (unknown, already decided, missing reason). The reviewer and
        the reason are stored on the approval and on ``APPROVAL_DECISION`` evidence.
        """
        return await self._decide(approval_id, reviewer, reason, grant=True)

    async def reject(self, approval_id: UUID, *, reviewer: object, reason: str) -> Approval:
        """Reject an approval; the execution ends ``REJECTED``. A reason is mandatory."""
        return await self._decide(approval_id, reviewer, reason, grant=False)

    # ------------------------------------------------------------------ expiry

    async def expire_due(self, now: datetime | None = None) -> int:
        """Expire every due approval and return how many were expired.

        Hosts may call this periodically; the SDK never starts timers. Expiry also runs
        lazily whenever an approval or its execution is touched.
        """
        at = now or self._rec.now()
        repo = self._rec.storage.approvals
        due = [
            approval
            for status in (ApprovalStatus.PENDING, ApprovalStatus.GRANTED)
            for approval in await repo.list_by_status(status)
            if approval.is_expired(at)
        ]
        expired = 0
        for approval in due:
            if await self._expire(approval):
                expired += 1
        return expired

    async def expire_for_execution(self, execution_id: UUID) -> None:
        """Expire the due approvals of one execution."""
        now = self._rec.now()
        for approval in await self._rec.storage.approvals.list_for_execution(execution_id):
            if approval.status in {
                ApprovalStatus.PENDING,
                ApprovalStatus.GRANTED,
            } and approval.is_expired(now):
                await self._expire(approval)

    # ------------------------------------------------------------------ sync wrappers

    def approve_sync(
        self, approval_id: UUID, *, reviewer: object, reason: str | None = None
    ) -> Approval:
        """Synchronous ``approve``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("approve_sync")
        return asyncio.run(self.approve(approval_id, reviewer=reviewer, reason=reason))

    def reject_sync(self, approval_id: UUID, *, reviewer: object, reason: str) -> Approval:
        """Synchronous ``reject``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("reject_sync")
        return asyncio.run(self.reject(approval_id, reviewer=reviewer, reason=reason))

    def list_pending_sync(self, execution_id: UUID) -> tuple[Approval, ...]:
        """Synchronous ``list_pending``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("list_pending_sync")
        return asyncio.run(self.list_pending(execution_id))

    # ------------------------------------------------------------------ internals

    async def _decide(
        self, approval_id: UUID, reviewer: object, reason: str | None, *, grant: bool
    ) -> Approval:
        rec = self._rec
        now = rec.now()
        trusted = self._authority.verify(reviewer, now=now)
        approval = await rec.storage.approvals.get(approval_id)
        if approval is None:
            msg = f"approval {approval_id} does not exist"
            raise ApprovalError(msg)
        execution = await self._execution(approval)
        if approval.status is ApprovalStatus.PENDING and approval.is_expired(now):
            await self._expire(approval)
            msg = f"approval {approval_id} expired at {approval.expires_at.isoformat()}"
            raise ApprovalExpiredError(msg, execution_id=execution.execution_id)
        _check_decision(approval, execution, trusted, reason, grant=grant)

        decided = transition_approval(
            approval,
            ApprovalStatus.GRANTED if grant else ApprovalStatus.REJECTED,
            decided_at=now,
            decision_actor_id=trusted.actor_id,
            reason=reason,
        )
        try:
            await rec.storage.approvals.compare_and_set(
                decided, expected_status=ApprovalStatus.PENDING
            )
        except ConcurrentModificationError as error:
            msg = f"approval {approval_id} was decided concurrently"
            raise ApprovalError(msg, execution_id=execution.execution_id) from error

        await rec.add_evidence(
            execution.execution_id,
            EvidenceType.APPROVAL_DECISION,
            source="approval_manager",
            content_digest=approval.action_digest,
            metadata={
                "approval_id": str(approval.approval_id),
                "gate": approval.gate.value,
                "action_id": approval.action_id,
                "capability": approval.capability,
                "decision": decided.status.value,
                "decision_actor_id": trusted.actor_id,
                "reason_provided": bool(reason),
                "reason": reason,
            },
        )
        await rec.emit(
            execution,
            EventType.APPROVAL_GRANTED if grant else EventType.APPROVAL_REJECTED,
            {
                "approval_id": str(approval.approval_id),
                "gate": approval.gate.value,
                "action_id": approval.action_id,
                "decision_actor_id": trusted.actor_id,
            },
        )
        if not grant:
            await rec.transition(
                execution,
                ExecutionStatus.REJECTED,
                EventType.EXECUTION_REJECTED,
                {"approval_id": str(approval.approval_id)},
                reason="approval_rejected",
            )
            return decided
        if approval.gate is ApprovalGate.OUTCOME:
            return await self._accept_outcome(execution, decided)
        await rec.transition(
            execution,
            ExecutionStatus.READY,
            EventType.EXECUTION_AUTHORIZED,
            {"approval_id": str(approval.approval_id), "gate": approval.gate.value},
            reason="approval_granted",
        )
        return decided

    async def _accept_outcome(self, execution: Execution, approval: Approval) -> Approval:
        rec = self._rec
        outcome = execution.outcome
        if outcome is None:  # pragma: no cover - WAITING_REVIEW always carries an outcome
            msg = "execution under review has no outcome"
            raise ApprovalError(msg, execution_id=execution.execution_id)
        expected = rec.action_digest(
            execution, ApprovalGate.OUTCOME, OUTCOME_ACTION_ID, None, outcome.output
        )
        if expected != approval.action_digest:
            msg = "the reviewed outcome does not match the approval"
            raise ApprovalError(msg, execution_id=execution.execution_id)
        now = rec.now()
        consumed = transition_approval(approval, ApprovalStatus.CONSUMED, consumed_at=now)
        await rec.storage.approvals.compare_and_set(
            consumed, expected_status=ApprovalStatus.GRANTED
        )
        await rec.emit(
            execution,
            EventType.APPROVAL_CONSUMED,
            {"approval_id": str(approval.approval_id), "gate": approval.gate.value},
        )
        await rec.transition(
            execution,
            ExecutionStatus.COMPLETED,
            EventType.EXECUTION_COMPLETED,
            {"approval_id": str(approval.approval_id)},
            reason="outcome_approved",
            outcome=outcome.accepted(now),
        )
        return consumed

    async def _expire(self, approval: Approval) -> bool:
        """Expire ``approval``; cancel its execution if it was still waiting on it.

        Returns ``False`` when another caller changed the approval first.
        """
        rec = self._rec
        expired = transition_approval(approval, ApprovalStatus.EXPIRED)
        try:
            await rec.storage.approvals.compare_and_set(expired, expected_status=approval.status)
        except ConcurrentModificationError:
            return False
        execution = await self._execution(approval)
        await rec.emit(
            execution,
            EventType.APPROVAL_EXPIRED,
            {
                "approval_id": str(approval.approval_id),
                "gate": approval.gate.value,
                "previous_status": approval.status.value,
            },
        )
        waiting = _WAITING_FOR[approval.gate]
        if approval.status is ApprovalStatus.PENDING and execution.status is waiting:
            try:
                await rec.transition(
                    execution,
                    ExecutionStatus.CANCELLED,
                    EventType.EXECUTION_CANCELLED,
                    {"approval_id": str(approval.approval_id)},
                    reason="approval_expired",
                )
            except (ConcurrentModificationError, InvalidStateTransitionError):
                return True
        return True

    async def _execution(self, approval: Approval) -> Execution:
        execution = await self._rec.storage.executions.get(approval.execution_id)
        if execution is None:  # pragma: no cover - approvals always belong to an execution
            msg = f"execution {approval.execution_id} of approval {approval.approval_id} is missing"
            raise ApprovalError(msg)
        return execution


def _check_decision(
    approval: Approval,
    execution: Execution,
    reviewer: TrustedActor,
    reason: str | None,
    *,
    grant: bool,
) -> None:
    eid = execution.execution_id
    if approval.status is not ApprovalStatus.PENDING:
        msg = f"approval is {approval.status.value}, not PENDING"
        raise ApprovalError(msg, execution_id=eid)
    if execution.status is not _WAITING_FOR[approval.gate]:
        msg = f"execution is {execution.status.value}, not waiting for this approval"
        raise ApprovalError(msg, execution_id=eid)
    if reviewer.tenant_id != execution.tenant_id:
        msg = "reviewer belongs to another tenant"
        raise ApprovalNotAllowedError(msg, execution_id=eid)
    if reviewer.actor_id == execution.actor_id:
        msg = "an actor cannot decide approvals for its own execution"
        raise ApprovalNotAllowedError(msg, execution_id=eid)
    if not reviewer.has_grant(approval.reviewer_capability):
        msg = f"reviewer lacks the grant {approval.reviewer_capability!r}"
        raise ApprovalNotAllowedError(msg, execution_id=eid)
    if reason is not None and (not reason.strip() or len(reason) > _MAX_REASON):
        msg = f"reason must be non-blank and at most {_MAX_REASON} characters"
        raise ApprovalError(msg, execution_id=eid)
    if not reason and (not grant or approval.reason_required):
        msg = "a reason is required for this decision"
        raise ApprovalError(msg, execution_id=eid)


def _ensure_no_running_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    msg = f"{name}() cannot be called from a running event loop; await the async method instead"
    raise RuntimeError(msg)
