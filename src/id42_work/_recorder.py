"""Internal services shared by the runner and the action invoker.

Owns persistence side effects of an execution: transitions (compare-and-set plus
exactly one event), events, evidence, journal settlement and approval gates.
"""

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from id42_work._time import Clock, to_utc
from id42_work.approvals.models import Approval, ApprovalStatus, compute_action_digest
from id42_work.canonical import to_canonical
from id42_work.context import RunContext
from id42_work.error_info import ExecutionErrorInfo
from id42_work.events.dispatcher import EventDispatcher
from id42_work.events.models import EventType, WorkEvent
from id42_work.events.redaction import Redactor
from id42_work.evidence.models import Evidence, EvidenceType
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus
from id42_work.policy.models import ApprovalGate, ApprovalRequirement
from id42_work.policy.registry import CapabilityRegistry
from id42_work.state_machine import transition_approval, transition_execution
from id42_work.states import ExecutionStatus
from id42_work.storage.protocols import Storage

_logger = logging.getLogger("id42_work.runner")
_EVENT_APPEND_ATTEMPTS = 16


class ExecutionRecorder:
    """Persistence and gate services for executions (internal API)."""

    def __init__(
        self,
        *,
        storage: Storage,
        capabilities: CapabilityRegistry,
        clock: Clock,
        redactor: Redactor,
        dispatcher: EventDispatcher,
    ) -> None:
        self.storage = storage
        self.capabilities = capabilities
        self.dispatcher = dispatcher
        self._clock = clock
        self._redactor = redactor

    async def settle(
        self, entry: JournalEntry, status: JournalStatus, error: BaseException
    ) -> None:
        settled = JournalEntry.model_validate(
            {
                **entry.model_dump(),
                "status": status,
                "error": ExecutionErrorInfo.from_exception(
                    error, requires_reconciliation=status is JournalStatus.UNCERTAIN
                ),
                "finished_at": (
                    max(self.now(), entry.started_at or self.now())
                    if status is JournalStatus.FAILED
                    else None
                ),
            }
        )
        await self.storage.executions.update_journal(
            settled, expected_status=JournalStatus.IN_FLIGHT
        )

    def action_digest(
        self,
        execution: Execution,
        gate: ApprovalGate,
        action_id: str,
        capability: str | None,
        params: object,
    ) -> str:
        return compute_action_digest(
            execution_id=execution.execution_id,
            work_id=execution.work_id,
            work_version=execution.work_version,
            definition_digest=execution.definition_digest,
            gate=gate,
            action_id=action_id,
            capability=capability,
            params=params,
        )

    async def check_gate(
        self,
        execution: Execution,
        requirement: ApprovalRequirement,
        action_id: str,
        action_digest: str,
        params: object,
    ) -> bool:
        """Consume a usable approval for this exact action, or request one.

        Returns ``True`` when an approval was consumed. Granted approvals that expired
        or whose digest differs are retired; a pending request for the same digest is
        reused; otherwise a new request is created.
        """
        now = self.now()
        repo = self.storage.approvals
        candidates = [
            a
            for a in await repo.list_for_execution(execution.execution_id)
            if a.gate is requirement.gate and a.action_id == action_id
        ]
        for approval in candidates:
            if approval.status not in {ApprovalStatus.PENDING, ApprovalStatus.GRANTED}:
                continue
            if approval.is_expired(now):
                await self.retire(
                    execution, approval, ApprovalStatus.EXPIRED, EventType.APPROVAL_EXPIRED
                )
            elif approval.action_digest != action_digest:
                await self.retire(
                    execution, approval, ApprovalStatus.INVALIDATED, EventType.APPROVAL_INVALIDATED
                )
            elif approval.status is ApprovalStatus.GRANTED:
                consumed = transition_approval(approval, ApprovalStatus.CONSUMED, consumed_at=now)
                await repo.compare_and_set(consumed, expected_status=ApprovalStatus.GRANTED)
                await self.emit(execution, EventType.APPROVAL_CONSUMED, _approval_payload(consumed))
                return True
            else:
                return False

        approval = Approval(
            execution_id=execution.execution_id,
            gate=requirement.gate,
            action_id=action_id,
            capability=requirement.capability,
            action_digest=action_digest,
            params_preview=self.preview(params),
            reviewer_capability=requirement.reviewer_capability,
            reason_required=requirement.reason_required,
            requested_at=now,
            expires_at=now + requirement.ttl,
        )
        await repo.create(approval)
        await self.emit(execution, EventType.APPROVAL_REQUESTED, _approval_payload(approval))
        return False

    async def retire(
        self,
        execution: Execution,
        approval: Approval,
        status: ApprovalStatus,
        event_type: EventType,
    ) -> None:
        retired = transition_approval(approval, status)
        await self.storage.approvals.compare_and_set(retired, expected_status=approval.status)
        await self.emit(execution, event_type, _approval_payload(retired))

    def preview(self, params: object) -> dict[str, JsonValue]:
        canonical = to_canonical(params)
        if not isinstance(canonical, dict):
            return {"value": cast("JsonValue", canonical)}
        return self._redactor(cast("dict[str, JsonValue]", canonical))

    def now(self) -> datetime:
        return to_utc(self._clock())

    async def transition(
        self,
        execution: Execution,
        to_status: ExecutionStatus,
        event_type: EventType,
        payload: dict[str, JsonValue],
        *,
        reason: str | None = None,
        **changes: object,
    ) -> Execution:
        eid = execution.execution_id
        evidence_ids = tuple(e.evidence_id for e in await self.storage.evidence.list(eid))
        sequence = await self.storage.events.last_sequence(eid) + 1
        updated = transition_execution(
            execution,
            to_status,
            at=max(self.now(), execution.updated_at),
            reason=reason,
            evidence_ids=evidence_ids,
            last_event_sequence=sequence,
            **changes,
        )
        await self.storage.executions.compare_and_set(updated)
        await self.emit(
            updated,
            event_type,
            {"from": execution.status.value, "to": to_status.value, "reason": reason, **payload},
        )
        return updated

    async def fail(
        self,
        execution: Execution,
        error: BaseException,
        reason: str,
        *,
        reconcile: bool,
    ) -> Execution:
        info = ExecutionErrorInfo.from_exception(error, requires_reconciliation=reconcile)
        _logger.warning(
            "execution failed",
            extra={"execution.id": str(execution.execution_id), "reason": reason},
        )
        return await self.transition(
            execution,
            ExecutionStatus.FAILED,
            EventType.EXECUTION_FAILED,
            {"error_type": info.type, "requires_reconciliation": reconcile},
            reason=reason,
            error=info,
        )

    async def emit(
        self, execution: Execution, event_type: EventType, payload: dict[str, JsonValue]
    ) -> WorkEvent:
        repo = self.storage.events
        redacted = self._redactor(payload)
        for _ in range(_EVENT_APPEND_ATTEMPTS):
            event = WorkEvent(
                event_type=event_type,
                execution_id=execution.execution_id,
                sequence_number=await repo.last_sequence(execution.execution_id) + 1,
                timestamp=self.now(),
                actor_reference=execution.actor_id,
                payload=redacted,
            )
            try:
                await repo.append(event)
            except ConcurrentModificationError:
                continue
            self.dispatcher.publish(event)
            return event
        msg = f"could not append {event_type.value} after {_EVENT_APPEND_ATTEMPTS} attempts"
        raise StorageError(msg, execution_id=execution.execution_id)

    async def add_evidence(
        self,
        execution_id: UUID,
        evidence_type: EvidenceType,
        *,
        source: str,
        content_reference: str | None = None,
        content_digest: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
        evidence_id: UUID | None = None,
    ) -> UUID:
        evidence = Evidence(
            evidence_id=evidence_id or uuid4(),
            execution_id=execution_id,
            evidence_type=evidence_type,
            source=source,
            created_at=self.now(),
            content_reference=content_reference,
            content_digest=content_digest,
            metadata=self._redactor(dict(metadata or {})),
        )
        await self.storage.evidence.add(evidence)
        return evidence.evidence_id

    async def persist_executor_evidence(self, execution: Execution, context: RunContext) -> None:
        for evidence_id, pending in context.drain_evidence():
            await self.add_evidence(
                execution.execution_id,
                pending.evidence_type,
                source="executor",
                content_reference=pending.content_reference,
                content_digest=pending.content_digest,
                metadata=pending.metadata,
                evidence_id=evidence_id,
            )


def _approval_payload(approval: Approval) -> dict[str, JsonValue]:
    return {
        "approval_id": str(approval.approval_id),
        "gate": approval.gate.value,
        "action_id": approval.action_id,
        "capability": approval.capability,
        "status": approval.status.value,
        "expires_at": approval.expires_at.isoformat(),
    }
