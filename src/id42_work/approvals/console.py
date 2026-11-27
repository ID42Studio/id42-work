"""Host decisions for approvals.

The MCP server cannot approve or reject. A host process uses ``ApprovalConsole``
instead: it seals an operator that is not the execution actor, gives that operator
only the approval's reviewer grant, and records the decision through
``ApprovalManager``. ``--resume`` continues the execution as a different actor, the
one the host uses to run the work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

from pydantic import ValidationError

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.approvals.models import Approval
from id42_work.exceptions import ApprovalError
from id42_work.execution import Execution
from id42_work.runner import WorkRunner
from id42_work.states import ExecutionStatus

_DEFAULT_OPERATOR_TTL = timedelta(minutes=5)
_WAITING = frozenset({ExecutionStatus.WAITING_APPROVAL, ExecutionStatus.WAITING_REVIEW})


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    """One approval and the execution it belongs to, after expiry has been applied."""

    approval: Approval
    execution: Execution


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """A decision that was stored, plus the execution afterwards.

    ``resumed`` is true only when this call drove ``WorkRunner.resume`` and it
    returned. The attached execution is the one resume returned, or the execution
    reloaded after the decision when resume was not requested.
    """

    approval: Approval
    execution: Execution
    resumed: bool


class ApprovalResumeError(ApprovalError):
    """The decision was stored, and the following resume did not finish."""

    def __init__(self, message: str, *, decision: ApprovalDecision) -> None:
        super().__init__(message, execution_id=decision.execution.execution_id)
        self.decision = decision


class ApprovalConsole:
    """List and decide approvals as an explicit operator.

    ``resume_actor`` is called only when a grant asks to resume. It must return the
    host actor that is allowed to continue the execution (the execution's own actor,
    or an actor holding ``resume:<work_id>``). The operator sealed here is never
    that actor: it receives only the approval's ``reviewer_capability``.

    ``authority`` must be the authority the runner verifies. Pass ``actor_id`` to
    seal a new operator, or pass an already sealed ``reviewer``. Exactly one of the
    two. A blank actor, a missing grant, a different tenant, the execution's own
    actor, an approval that is no longer pending, and an execution that is not
    waiting for that gate are refused before a second write. Concurrent decisions
    still have one winner: the manager compare-and-sets the approval.
    """

    def __init__(
        self,
        runner: object,
        *,
        authority: object,
        resume_actor: object,
        operator_ttl: timedelta = _DEFAULT_OPERATOR_TTL,
    ) -> None:
        if not isinstance(runner, WorkRunner):
            msg = "runner must be a WorkRunner"
            raise TypeError(msg)
        if not isinstance(authority, ActorAuthority):
            msg = "authority must be the ActorAuthority that seals the runner's actors"
            raise TypeError(msg)
        if not callable(resume_actor):
            msg = "resume_actor must be a callable that returns a TrustedActor"
            raise TypeError(msg)
        if operator_ttl <= timedelta(0):
            msg = "operator_ttl must be positive"
            raise ValueError(msg)
        self._runner = runner
        self._authority = authority
        self._resume_actor = cast("Callable[[], TrustedActor]", resume_actor)
        self._operator_ttl = operator_ttl

    async def list_pending(self) -> tuple[ApprovalSnapshot, ...]:
        """Pending approvals whose execution is still waiting, oldest execution first."""
        rows: list[ApprovalSnapshot] = []
        for execution in await self._runner.list_executions():
            if execution.status not in _WAITING:
                continue
            pending = await self._runner.approvals.list_pending(execution.execution_id)
            if not pending:
                continue
            current = await self._load_execution(execution.execution_id, fallback=execution)
            for approval in pending:
                rows.append(ApprovalSnapshot(approval=approval, execution=current))
        return tuple(rows)

    async def show(self, approval_id: UUID) -> ApprovalSnapshot:
        """One approval, after expiring it when it is due."""
        approval = await self._load_approval(approval_id)
        await self._runner.approvals.expire_for_execution(approval.execution_id)
        refreshed = await self._runner.approvals.get(approval_id)
        if refreshed is not None:
            approval = refreshed
        execution = await self._load_execution(approval.execution_id)
        return ApprovalSnapshot(approval=approval, execution=execution)

    async def approve(
        self,
        approval_id: UUID,
        *,
        actor_id: str | None = None,
        reviewer: object | None = None,
        reason: str | None = None,
        resume: bool = False,
    ) -> ApprovalDecision:
        """Grant ``approval_id`` and, when ``resume`` is set, continue the execution.

        Resume uses ``resume_actor``, not the operator. A resume failure leaves the
        grant stored and raises ``ApprovalResumeError``.
        """
        approval, execution = await self._waiting(approval_id)
        operator = self._operator(approval, execution, actor_id=actor_id, reviewer=reviewer)
        decided = await self._runner.approvals.approve(
            approval_id, reviewer=operator, reason=reason
        )
        if not resume:
            current = await self._load_execution(execution.execution_id, fallback=execution)
            return ApprovalDecision(approval=decided, execution=current, resumed=False)
        try:
            continued = await self._runner.resume(
                execution.execution_id, actor=self._resume_actor()
            )
        except Exception as exc:
            current = await self._load_execution(execution.execution_id, fallback=execution)
            decision = ApprovalDecision(approval=decided, execution=current, resumed=False)
            msg = f"approval {decided.approval_id} was granted; resume failed: {exc}"
            raise ApprovalResumeError(msg, decision=decision) from exc
        return ApprovalDecision(approval=decided, execution=continued, resumed=True)

    async def reject(
        self,
        approval_id: UUID,
        *,
        actor_id: str | None = None,
        reviewer: object | None = None,
        reason: str,
    ) -> ApprovalDecision:
        """Reject ``approval_id``. The execution ends ``REJECTED``. A reason is mandatory."""
        approval, execution = await self._waiting(approval_id)
        operator = self._operator(approval, execution, actor_id=actor_id, reviewer=reviewer)
        decided = await self._runner.approvals.reject(approval_id, reviewer=operator, reason=reason)
        current = await self._load_execution(execution.execution_id, fallback=execution)
        return ApprovalDecision(approval=decided, execution=current, resumed=False)

    def list_pending_sync(self) -> tuple[ApprovalSnapshot, ...]:
        """Synchronous ``list_pending``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("list_pending_sync")
        return asyncio.run(self.list_pending())

    def show_sync(self, approval_id: UUID) -> ApprovalSnapshot:
        """Synchronous ``show``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("show_sync")
        return asyncio.run(self.show(approval_id))

    def approve_sync(
        self,
        approval_id: UUID,
        *,
        actor_id: str | None = None,
        reviewer: object | None = None,
        reason: str | None = None,
        resume: bool = False,
    ) -> ApprovalDecision:
        """Synchronous ``approve``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("approve_sync")
        return asyncio.run(
            self.approve(
                approval_id,
                actor_id=actor_id,
                reviewer=reviewer,
                reason=reason,
                resume=resume,
            )
        )

    def reject_sync(
        self,
        approval_id: UUID,
        *,
        actor_id: str | None = None,
        reviewer: object | None = None,
        reason: str,
    ) -> ApprovalDecision:
        """Synchronous ``reject``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("reject_sync")
        return asyncio.run(
            self.reject(approval_id, actor_id=actor_id, reviewer=reviewer, reason=reason)
        )

    async def _waiting(self, approval_id: UUID) -> tuple[Approval, Execution]:
        approval = await self._load_approval(approval_id)
        execution = await self._load_execution(approval.execution_id)
        return approval, execution

    async def _load_approval(self, approval_id: UUID) -> Approval:
        approval = await self._runner.approvals.get(approval_id)
        if approval is None:
            msg = f"approval {approval_id} does not exist"
            raise ApprovalError(msg)
        return approval

    async def _load_execution(
        self, execution_id: UUID, *, fallback: Execution | None = None
    ) -> Execution:
        execution = await self._runner.get(execution_id)
        if execution is not None:
            return execution
        if fallback is not None:
            return fallback
        msg = f"execution {execution_id} is missing"
        raise ApprovalError(msg, execution_id=execution_id)

    def _operator(
        self,
        approval: Approval,
        execution: Execution,
        *,
        actor_id: str | None,
        reviewer: object | None,
    ) -> object:
        if (actor_id is None) == (reviewer is None):
            msg = "pass actor_id or reviewer, not both"
            raise ApprovalError(msg, execution_id=execution.execution_id)
        if reviewer is not None:
            return reviewer
        if actor_id is None or not actor_id.strip():
            msg = "actor_id must be non-blank"
            raise ApprovalError(msg, execution_id=execution.execution_id)
        try:
            return self._authority.issue(
                actor_id=actor_id.strip(),
                tenant_id=execution.tenant_id,
                grants=frozenset({approval.reviewer_capability}),
                ttl=self._operator_ttl,
            )
        except (ValueError, TypeError, ValidationError) as exc:
            msg = "actor_id is not a valid operator id"
            raise ApprovalError(msg, execution_id=execution.execution_id) from exc


def console_from_server(server: object) -> ApprovalConsole:
    """Build a console from a ``WorkMcpServer`` (the ``mcp serve`` factory result)."""
    runner = getattr(server, "runner", None)
    authority = getattr(server, "authority", None)
    resume_actor = getattr(server, "host_actor", None)
    if not isinstance(runner, WorkRunner) or not isinstance(authority, ActorAuthority):
        msg = "factory must return a WorkMcpServer"
        raise TypeError(msg)
    if not callable(resume_actor):
        msg = "factory must return a WorkMcpServer"
        raise TypeError(msg)
    return ApprovalConsole(runner, authority=authority, resume_actor=resume_actor)


def _ensure_no_running_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    msg = f"{name}() cannot be called from a running event loop; await the async method instead"
    raise RuntimeError(msg)
