"""Settle uncertain capability calls from a provider probe or an operator.

The journal compare-and-set is the decision lock. The resume claim keeps a
decision from running beside ``resume``. A lost race writes nothing further.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from id42_work._recorder import ExecutionRecorder
from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.canonical import digest
from id42_work.error_info import ExecutionErrorInfo
from id42_work.events.models import EventType
from id42_work.evidence.models import EvidenceType
from id42_work.exceptions import (
    ConcurrentModificationError,
    ExecutionError,
    ReconciliationError,
    ReconciliationNotAllowedError,
)
from id42_work.execution import Execution
from id42_work.identifiers import RECORD_CAPABILITY, reconcile_grant
from id42_work.journal import JournalEntry, JournalStatus
from id42_work.policy.capabilities import CapabilityCall
from id42_work.reconciliation.models import ReconciliationProbe, ReconciliationVerdict
from id42_work.states import ExecutionStatus

_logger = logging.getLogger("id42_work.reconciliation")
_JSON = TypeAdapter(JsonValue)
_MAX_REASON = 2000
_CLAIM_TTL = timedelta(minutes=5)
_OPEN = frozenset({ExecutionStatus.FAILED, ExecutionStatus.VALIDATION_FAILED})
_UNRESOLVED = frozenset({JournalStatus.IN_FLIGHT, JournalStatus.UNCERTAIN})
_DECISIONS = frozenset(
    {
        ReconciliationVerdict.APPLIED,
        ReconciliationVerdict.NOT_APPLIED,
        ReconciliationVerdict.ABANDONED,
    }
)
_PROVIDER_REASONS = {
    ReconciliationVerdict.APPLIED: "provider reported the effect as applied",
    ReconciliationVerdict.NOT_APPLIED: "provider reported the effect as not applied",
}


@dataclass(frozen=True, slots=True)
class StoredReconciliation:
    """A journal entry after one accepted decision, and the execution afterwards."""

    execution: Execution
    entry: JournalEntry


class ReconciliationService:
    """Probe and decide uncertain capability calls for one runner."""

    def __init__(self, *, recorder: ExecutionRecorder, authority: ActorAuthority) -> None:
        self._rec = recorder
        self._authority = authority

    async def list_open(self) -> tuple[tuple[Execution, JournalEntry], ...]:
        """Uncertain capability calls whose execution still requires reconciliation.

        Abandoned executions are omitted. Recorded computations are omitted: they
        are not external effects. The order is the execution list, then journal order.
        """
        rows: list[tuple[Execution, JournalEntry]] = []
        for execution in await self._rec.storage.executions.list():
            if not _is_open(execution):
                continue
            journal = await self._rec.storage.executions.list_journal(execution.execution_id)
            if _abandoned(journal):
                continue
            rows.extend(
                (execution, entry)
                for entry in journal
                if entry.status is JournalStatus.UNCERTAIN and entry.capability != RECORD_CAPABILITY
            )
        return tuple(rows)

    async def get(self, execution_id: UUID, action_id: str) -> tuple[Execution, JournalEntry]:
        """One journal entry, whether or not it is still uncertain."""
        execution = await self._load(execution_id)
        entry = await self._find(execution_id, action_id)
        if entry is None:
            msg = f"action {action_id!r} is not journaled"
            raise ReconciliationError(msg, execution_id=execution_id)
        return execution, entry

    async def decide(
        self,
        execution_id: UUID,
        action_id: str,
        verdict: ReconciliationVerdict,
        *,
        actor: object,
        reason: str,
        result: JsonValue | None = None,
        result_supplied: bool = False,
    ) -> StoredReconciliation:
        """Record one operator decision for an ``UNCERTAIN`` capability call.

        ``applied`` requires ``result_supplied``. JSON null is a supplied outcome.
        The actor is verified, must hold ``reconcile:<work_id>`` in the execution's
        tenant, and must not be the execution's actor. A second decision, a call
        that is not uncertain, and a decision beside a live resume are refused.
        """
        if verdict not in _DECISIONS:
            msg = "verdict is not an operator decision"
            raise ReconciliationError(msg, execution_id=execution_id)
        cleaned = _clean_reason(reason, execution_id)
        if verdict is ReconciliationVerdict.APPLIED and not result_supplied:
            msg = "applied requires the outcome that was confirmed"
            raise ReconciliationError(msg, execution_id=execution_id)
        trusted = self._authority.verify(actor, now=self._rec.now())
        execution = await self._load(execution_id)
        _authorize(trusted, execution)
        async with self._claim(execution_id):
            execution = await self._load(execution_id)
            _authorize(trusted, execution)
            entry = await self._require_uncertain(execution, action_id)
            return await self._commit(
                execution,
                entry,
                verdict,
                actor_id=trusted.actor_id,
                reason=cleaned,
                result=result,
                source="operator",
                strict_result=True,
            )

    async def probe(
        self,
        execution: Execution,
        entries: tuple[JournalEntry, ...] | list[JournalEntry],
        *,
        actor_id: str,
    ) -> list[JournalEntry]:
        """Probe each in-flight or uncertain capability call. Return those still open.

        A missing hook, ``UNKNOWN``, a probe error, or a non-JSON applied result
        leaves the entry unresolved. The caller is expected to hold the resume claim.
        """
        unresolved: list[JournalEntry] = []
        for entry in entries:
            if entry.capability == RECORD_CAPABILITY or entry.status not in _UNRESOLVED:
                continue
            settled = await self._probe_one(execution, entry, actor_id=actor_id)
            if settled:
                continue
            fresh = await self._find(execution.execution_id, entry.action_id)
            if fresh is not None and fresh.status in _UNRESOLVED:
                unresolved.append(fresh)
        return unresolved

    def blocked(self, entries: tuple[JournalEntry, ...] | list[JournalEntry]) -> bool:
        """Whether resume must not continue this execution yet.

        An abandoned call, any uncertain call, or an in-flight capability call
        still blocks. An in-flight recorded computation does not: its result was
        never returned.
        """
        for entry in entries:
            if entry.status is JournalStatus.ABANDONED:
                return True
            if entry.status is JournalStatus.UNCERTAIN:
                return True
            if entry.status is JournalStatus.IN_FLIGHT and entry.capability != RECORD_CAPABILITY:
                return True
        return False

    async def _probe_one(self, execution: Execution, entry: JournalEntry, *, actor_id: str) -> bool:
        provider = self._rec.capabilities.provider(entry.capability)
        if provider is None:
            return False
        call = CapabilityCall(
            execution_id=execution.execution_id,
            action_id=entry.action_id,
            operation_id=entry.operation_id,
            actor_id=execution.actor_id,
            tenant_id=execution.tenant_id,
        )
        probe = await _invoke_probe(provider, call)
        if probe is None or probe.verdict is ReconciliationVerdict.UNKNOWN:
            return False
        reason = _PROVIDER_REASONS[probe.verdict]
        try:
            await self._commit(
                execution,
                entry,
                probe.verdict,
                actor_id=actor_id,
                reason=reason,
                result=probe.result,
                source="provider",
                strict_result=False,
            )
        except ReconciliationError as exc:
            _logger.warning(
                "provider reconciliation was not stored",
                extra={
                    "execution.id": str(execution.execution_id),
                    "error_type": type(exc).__name__,
                },
            )
            return False
        return True

    async def _commit(
        self,
        execution: Execution,
        entry: JournalEntry,
        verdict: ReconciliationVerdict,
        *,
        actor_id: str,
        reason: str,
        result: JsonValue | None,
        source: str,
        strict_result: bool,
    ) -> StoredReconciliation:
        result_digest: str | None = None
        if verdict is ReconciliationVerdict.APPLIED:
            parsed = _parse_result(result, execution.execution_id, strict=strict_result)
            if parsed is None:
                msg = "applied outcome is not JSON"
                raise ReconciliationError(msg, execution_id=execution.execution_id)
            value, result_digest = parsed
            updated = _succeeded(entry, value, result_digest, finished_at=self._rec.now())
        elif verdict is ReconciliationVerdict.NOT_APPLIED:
            updated = _marked(
                entry,
                JournalStatus.NOT_APPLIED,
                error_type="ReconciliationNotApplied",
                reason=reason,
            )
        else:
            updated = _marked(
                entry,
                JournalStatus.ABANDONED,
                error_type="ReconciliationAbandoned",
                reason=reason,
            )
        await self._cas(updated, entry.status)
        current = await self._audit(
            execution,
            updated,
            decision=verdict.value,
            actor_id=actor_id,
            reason=reason,
            source=source,
            result_digest=result_digest,
            close=verdict is ReconciliationVerdict.ABANDONED,
        )
        return StoredReconciliation(execution=current, entry=updated)

    async def _audit(
        self,
        execution: Execution,
        entry: JournalEntry,
        *,
        decision: str,
        actor_id: str,
        reason: str,
        source: str,
        result_digest: str | None,
        close: bool,
    ) -> Execution:
        metadata: dict[str, JsonValue] = {
            "action_id": entry.action_id,
            "capability": entry.capability,
            "operation_id": str(entry.operation_id),
            "decision": decision,
            "decision_actor_id": actor_id,
            "reason": reason,
            "source": source,
        }
        await self._rec.add_evidence(
            execution.execution_id,
            EvidenceType.RECONCILIATION_DECISION,
            source=f"reconciliation:{source}",
            content_digest=result_digest,
            metadata=metadata,
        )
        payload: dict[str, JsonValue] = {
            key: value for key, value in metadata.items() if key != "reason"
        }
        if result_digest is not None:
            payload["result_digest"] = result_digest
        if not close:
            await self._rec.emit(execution, EventType.ACTION_RECONCILED, payload)
            return execution
        info = ExecutionErrorInfo(
            type="ReconciliationAbandoned",
            message=reason,
            requires_reconciliation=False,
        )
        return await self._rec.revise_execution(
            execution,
            EventType.ACTION_RECONCILED,
            payload,
            status_reason="reconciliation_abandoned",
            error=info,
        )

    async def _cas(self, entry: JournalEntry, expected: JournalStatus) -> None:
        try:
            await self._rec.storage.executions.update_journal(entry, expected_status=expected)
        except ConcurrentModificationError as exc:
            msg = f"action {entry.action_id!r} was reconciled concurrently"
            raise ReconciliationError(msg, execution_id=entry.execution_id) from exc

    async def _require_uncertain(self, execution: Execution, action_id: str) -> JournalEntry:
        journal = await self._rec.storage.executions.list_journal(execution.execution_id)
        if _abandoned(journal):
            msg = "reconciliation for this execution is closed"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        if not _is_open(execution):
            msg = f"execution is {execution.status.value} and is not awaiting reconciliation"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        entry = _by_action(journal, action_id)
        if entry is None:
            msg = f"action {action_id!r} is not journaled"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        if entry.capability == RECORD_CAPABILITY:
            msg = "a recorded computation is not reconciled as a capability call"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        if entry.status is not JournalStatus.UNCERTAIN:
            msg = f"action {action_id!r} is {entry.status.value}, not UNCERTAIN"
            raise ReconciliationError(msg, execution_id=execution.execution_id)
        return entry

    async def _load(self, execution_id: UUID) -> Execution:
        execution = await self._rec.storage.executions.get(execution_id)
        if execution is None:
            msg = f"execution {execution_id} does not exist"
            raise ExecutionError(msg, execution_id=execution_id)
        return execution

    async def _find(self, execution_id: UUID, action_id: str) -> JournalEntry | None:
        journal = await self._rec.storage.executions.list_journal(execution_id)
        return _by_action(journal, action_id)

    @contextlib.asynccontextmanager
    async def _claim(self, execution_id: UUID) -> AsyncGenerator[None]:
        owner = f"reconcile:{os.getpid()}:{uuid4()}"
        acquired = await self._rec.storage.claim_resume(
            execution_id, owner, now=self._rec.now(), ttl=_CLAIM_TTL
        )
        if not acquired:
            msg = f"execution {execution_id} is being reconciled or resumed"
            raise ReconciliationError(msg, execution_id=execution_id)
        try:
            yield
        finally:
            await self._rec.storage.release_resume(execution_id, owner)


def _is_open(execution: Execution) -> bool:
    return execution.status in _OPEN and execution.requires_reconciliation


def _abandoned(journal: tuple[JournalEntry, ...] | list[JournalEntry]) -> bool:
    return any(entry.status is JournalStatus.ABANDONED for entry in journal)


def _by_action(
    journal: tuple[JournalEntry, ...] | list[JournalEntry], action_id: str
) -> JournalEntry | None:
    for entry in journal:
        if entry.action_id == action_id:
            return entry
    return None


def _authorize(actor: TrustedActor, execution: Execution) -> None:
    eid = execution.execution_id
    if actor.tenant_id != execution.tenant_id:
        msg = "operator belongs to another tenant"
        raise ReconciliationNotAllowedError(msg, execution_id=eid)
    if actor.actor_id == execution.actor_id:
        msg = "an actor cannot reconcile its own execution"
        raise ReconciliationNotAllowedError(msg, execution_id=eid)
    grant = reconcile_grant(execution.work_id)
    if not actor.has_grant(grant):
        msg = f"operator lacks the grant {grant!r}"
        raise ReconciliationNotAllowedError(msg, execution_id=eid)


def _clean_reason(reason: str, execution_id: UUID) -> str:
    if not reason.strip() or len(reason.strip()) > _MAX_REASON:
        msg = f"reason must be non-blank and at most {_MAX_REASON} characters"
        raise ReconciliationError(msg, execution_id=execution_id)
    return reason.strip()


def _parse_result(
    result: object, execution_id: UUID, *, strict: bool
) -> tuple[JsonValue, str] | None:
    try:
        value = _JSON.validate_python(result)
        return value, digest(value, floats="repr")
    except (ValidationError, TypeError) as exc:
        if not strict:
            return None
        msg = "applied outcome must be a JSON value"
        raise ReconciliationError(msg, execution_id=execution_id) from exc


def _succeeded(
    entry: JournalEntry, result: JsonValue, result_digest: str, *, finished_at: datetime
) -> JournalEntry:
    started = entry.started_at or finished_at
    return JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "status": JournalStatus.SUCCEEDED,
            "result": result,
            "result_digest": result_digest,
            "finished_at": max(finished_at, started),
            "error": None,
        }
    )


def _marked(
    entry: JournalEntry, status: JournalStatus, *, error_type: str, reason: str
) -> JournalEntry:
    return JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "status": status,
            "result": None,
            "result_digest": None,
            "finished_at": None,
            "error": ExecutionErrorInfo(
                type=error_type, message=reason, requires_reconciliation=False
            ),
        }
    )


async def _invoke_probe(provider: object, call: CapabilityCall) -> ReconciliationProbe | None:
    method = getattr(provider, "reconcile", None)
    if not callable(method):
        return None
    probe_method = cast("Callable[[CapabilityCall], object]", method)
    try:
        invoked = probe_method(call)
    except Exception as exc:  # noqa: BLE001 - a probe must not perform or fail the resume
        _log_probe_failure(exc)
        return None
    if not isinstance(invoked, Awaitable):
        _logger.warning(
            "provider reconcile did not return an awaitable",
            extra={"error_type": type(invoked).__name__},
        )
        return None
    try:
        value = await cast("Awaitable[object]", invoked)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - a probe must not perform or fail the resume
        _log_probe_failure(exc)
        return None
    return _coerce_probe(value)


def _coerce_probe(value: object) -> ReconciliationProbe | None:
    if isinstance(value, ReconciliationProbe):
        return value
    if isinstance(value, ReconciliationVerdict) and value is not ReconciliationVerdict.ABANDONED:
        return ReconciliationProbe(verdict=value)
    _logger.warning(
        "provider reconcile returned an invalid verdict",
        extra={"error_type": type(value).__name__},
    )
    return None


def _log_probe_failure(exc: Exception) -> None:
    _logger.warning(
        "provider reconcile failed",
        extra={"error_type": type(exc).__name__},
    )
