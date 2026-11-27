"""Host decisions for uncertain capability calls.

The MCP server cannot reconcile. A host process uses ``ReconciliationConsole``
instead: it seals an operator that is not the execution actor, gives that
operator only ``reconcile:<work_id>``, and records the decision through
``ReconciliationService``. ``--resume`` continues the execution as a different
actor, the one the host uses to run the work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

from pydantic import JsonValue, ValidationError

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.exceptions import ReconciliationError
from id42_work.execution import Execution
from id42_work.identifiers import reconcile_grant
from id42_work.journal import JournalEntry
from id42_work.reconciliation.models import ReconciliationVerdict
from id42_work.reconciliation.service import ReconciliationService, StoredReconciliation
from id42_work.runner import WorkRunner

_DEFAULT_OPERATOR_TTL = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    """One journal entry and the execution it belongs to."""

    execution: Execution
    entry: JournalEntry


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    """A decision that was stored, plus the execution afterwards.

    ``resumed`` is true only when this call drove ``WorkRunner.resume`` and it
    returned. The attached execution is the one resume returned, or the execution
    reloaded after the decision when resume was not requested.
    """

    execution: Execution
    entry: JournalEntry
    actor_id: str
    resumed: bool


class ReconciliationResumeError(ReconciliationError):
    """The decision was stored, and the following resume did not finish."""

    def __init__(self, message: str, *, decision: ReconciliationDecision) -> None:
        super().__init__(message, execution_id=decision.execution.execution_id)
        self.decision = decision


class ReconciliationConsole:
    """List and decide uncertain calls as an explicit operator.

    ``resume_actor`` is called only when a grant asks to resume. It must return the
    host actor that is allowed to continue the execution. The operator sealed here
    is never that actor: it receives only ``reconcile:<work_id>``.

    ``authority`` must be the authority the runner verifies. Pass ``actor_id`` to
    seal a new operator, or pass an already sealed ``operator``. Exactly one of the
    two. A blank actor, a missing grant, a different tenant, the execution's own
    actor, a call that is not uncertain, and a second decision are refused before
    a second write.
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

    async def list_open(self) -> tuple[ReconciliationSnapshot, ...]:
        """Uncertain capability calls that still require an operator, oldest first."""
        rows = await self._service().list_open()
        return tuple(
            ReconciliationSnapshot(execution=execution, entry=entry) for execution, entry in rows
        )

    async def show(self, execution_id: UUID, action_id: str) -> ReconciliationSnapshot:
        """One journal entry and its execution."""
        execution, entry = await self._service().get(execution_id, action_id)
        return ReconciliationSnapshot(execution=execution, entry=entry)

    async def mark_applied(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
        result: JsonValue,
        resume: bool = False,
    ) -> ReconciliationDecision:
        """Record that the effect happened, using ``result`` as the journaled outcome."""
        return await self._decide(
            execution_id,
            action_id,
            ReconciliationVerdict.APPLIED,
            actor_id=actor_id,
            operator=operator,
            reason=reason,
            result=result,
            result_supplied=True,
            resume=resume,
        )

    async def mark_not_applied(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
        resume: bool = False,
    ) -> ReconciliationDecision:
        """Record that the effect did not happen, so resume may retry the same operation id."""
        return await self._decide(
            execution_id,
            action_id,
            ReconciliationVerdict.NOT_APPLIED,
            actor_id=actor_id,
            operator=operator,
            reason=reason,
            resume=resume,
        )

    async def abandon(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
    ) -> ReconciliationDecision:
        """Close the uncertainty. The execution stays failed and is not retried."""
        return await self._decide(
            execution_id,
            action_id,
            ReconciliationVerdict.ABANDONED,
            actor_id=actor_id,
            operator=operator,
            reason=reason,
            resume=False,
        )

    def list_open_sync(self) -> tuple[ReconciliationSnapshot, ...]:
        """Synchronous ``list_open``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("list_open_sync")
        return asyncio.run(self.list_open())

    def show_sync(self, execution_id: UUID, action_id: str) -> ReconciliationSnapshot:
        """Synchronous ``show``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("show_sync")
        return asyncio.run(self.show(execution_id, action_id))

    def mark_applied_sync(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
        result: JsonValue,
        resume: bool = False,
    ) -> ReconciliationDecision:
        """Synchronous ``mark_applied``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("mark_applied_sync")
        return asyncio.run(
            self.mark_applied(
                execution_id,
                action_id,
                actor_id=actor_id,
                operator=operator,
                reason=reason,
                result=result,
                resume=resume,
            )
        )

    def mark_not_applied_sync(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
        resume: bool = False,
    ) -> ReconciliationDecision:
        """Synchronous ``mark_not_applied``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("mark_not_applied_sync")
        return asyncio.run(
            self.mark_not_applied(
                execution_id,
                action_id,
                actor_id=actor_id,
                operator=operator,
                reason=reason,
                resume=resume,
            )
        )

    def abandon_sync(
        self,
        execution_id: UUID,
        action_id: str,
        *,
        actor_id: str | None = None,
        operator: object | None = None,
        reason: str,
    ) -> ReconciliationDecision:
        """Synchronous ``abandon``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("abandon_sync")
        return asyncio.run(
            self.abandon(
                execution_id, action_id, actor_id=actor_id, operator=operator, reason=reason
            )
        )

    async def _decide(
        self,
        execution_id: UUID,
        action_id: str,
        verdict: ReconciliationVerdict,
        *,
        actor_id: str | None,
        operator: object | None,
        reason: str,
        result: JsonValue | None = None,
        result_supplied: bool = False,
        resume: bool,
    ) -> ReconciliationDecision:
        execution = await self._load_execution(execution_id)
        sealed = self._operator(execution, actor_id=actor_id, operator=operator)
        stored = await self._service().decide(
            execution_id,
            action_id,
            verdict,
            actor=sealed,
            reason=reason,
            result=result,
            result_supplied=result_supplied,
        )
        actor = sealed.actor_id
        if not resume:
            current = await self._load_execution(execution_id, fallback=stored.execution)
            return ReconciliationDecision(
                execution=current, entry=stored.entry, actor_id=actor, resumed=False
            )
        return await self._resume_after(stored, actor)

    async def _resume_after(
        self, stored: StoredReconciliation, actor_id: str
    ) -> ReconciliationDecision:
        try:
            continued = await self._runner.resume(
                stored.execution.execution_id, actor=self._resume_actor()
            )
        except Exception as exc:
            current = await self._load_execution(
                stored.execution.execution_id, fallback=stored.execution
            )
            decision = ReconciliationDecision(
                execution=current, entry=stored.entry, actor_id=actor_id, resumed=False
            )
            msg = f"action {stored.entry.action_id!r} was reconciled; resume failed: {exc}"
            raise ReconciliationResumeError(msg, decision=decision) from exc
        fresh = await self._service().get(continued.execution_id, stored.entry.action_id)
        return ReconciliationDecision(
            execution=continued, entry=fresh[1], actor_id=actor_id, resumed=True
        )

    async def _load_execution(
        self, execution_id: UUID, *, fallback: Execution | None = None
    ) -> Execution:
        execution = await self._runner.get(execution_id)
        if execution is not None:
            return execution
        if fallback is not None:
            return fallback
        msg = f"execution {execution_id} is missing"
        raise ReconciliationError(msg, execution_id=execution_id)

    def _operator(
        self,
        execution: Execution,
        *,
        actor_id: str | None,
        operator: object | None,
    ) -> TrustedActor:
        if (actor_id is None) == (operator is None):
            msg = "pass actor_id or operator, not both"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        if operator is not None:
            if not isinstance(operator, TrustedActor):
                msg = "operator must be a TrustedActor"
                raise ReconciliationError(msg, execution_id=execution.execution_id)
            return operator
        if actor_id is None or not actor_id.strip():
            msg = "actor_id must be non-blank"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        try:
            return self._authority.issue(
                actor_id=actor_id.strip(),
                tenant_id=execution.tenant_id,
                grants=frozenset({reconcile_grant(execution.work_id)}),
                ttl=self._operator_ttl,
            )
        except (ValueError, TypeError, ValidationError) as exc:
            msg = "actor_id is not a valid operator id"
            raise ReconciliationError(msg, execution_id=execution.execution_id) from exc

    def _service(self) -> ReconciliationService:
        return self._runner.reconciliation


def console_from_server(server: object) -> ReconciliationConsole:
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
    return ReconciliationConsole(runner, authority=authority, resume_actor=resume_actor)


def _ensure_no_running_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    msg = f"{name}() cannot be called from a running event loop; await the async method instead"
    raise RuntimeError(msg)
