"""In-memory storage (specification 12).

Scope: one Python process. Each repository guards its state with one
``threading.Lock``; critical sections never ``await``, so an instance is safe from one
event loop and from several threads that each run their own loop. Nothing survives the
process: no crash recovery, durability or distributed consistency is claimed.
"""

import threading
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import BaseModel

from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope


class MemoryExecutionRepository:
    """Executions and their action journals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: dict[UUID, Execution] = {}
        self._journals: dict[UUID, dict[str, JournalEntry]] = {}

    async def create(self, execution: Execution) -> None:
        with self._lock:
            if execution.execution_id in self._executions:
                msg = f"execution {execution.execution_id} already exists"
                raise StorageError(msg, execution_id=execution.execution_id)
            if execution.revision != 0:
                msg = "new executions must have revision 0"
                raise StorageError(msg, execution_id=execution.execution_id)
            self._executions[execution.execution_id] = execution
            self._journals[execution.execution_id] = {}

    async def get(self, execution_id: UUID) -> Execution | None:
        with self._lock:
            return self._executions.get(execution_id)

    async def compare_and_set(self, updated: Execution) -> None:
        eid = updated.execution_id
        with self._lock:
            current = self._executions.get(eid)
            if current is None:
                msg = f"execution {eid} does not exist"
                raise StorageError(msg, execution_id=eid)
            if current.revision != updated.revision - 1:
                msg = (
                    f"execution {eid} is at revision {current.revision}, "
                    f"update expects {updated.revision - 1}"
                )
                raise ConcurrentModificationError(msg, execution_id=eid)
            self._executions[eid] = updated

    async def append_journal(self, entry: JournalEntry) -> None:
        eid = entry.execution_id
        with self._lock:
            journal = self._journals.get(eid)
            if journal is None:
                msg = f"execution {eid} does not exist"
                raise StorageError(msg, execution_id=eid)
            if entry.action_id in journal:
                msg = f"action {entry.action_id!r} is already journaled"
                raise StorageError(msg, execution_id=eid)
            journal[entry.action_id] = entry

    async def update_journal(self, entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        eid = entry.execution_id
        with self._lock:
            current = self._journals.get(eid, {}).get(entry.action_id)
            if current is None:
                msg = f"action {entry.action_id!r} is not journaled"
                raise StorageError(msg, execution_id=eid)
            if current.status is not expected_status:
                msg = (
                    f"action {entry.action_id!r} is {current.status.value}, "
                    f"expected {expected_status.value}"
                )
                raise ConcurrentModificationError(msg, execution_id=eid)
            self._journals[eid][entry.action_id] = entry

    async def list_journal(self, execution_id: UUID) -> tuple[JournalEntry, ...]:
        with self._lock:
            return tuple(self._journals.get(execution_id, {}).values())


class MemoryApprovalRepository:
    """Approval records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._approvals: dict[UUID, Approval] = {}

    async def create(self, approval: Approval) -> None:
        with self._lock:
            if approval.approval_id in self._approvals:
                msg = f"approval {approval.approval_id} already exists"
                raise StorageError(msg, execution_id=approval.execution_id)
            self._approvals[approval.approval_id] = approval

    async def get(self, approval_id: UUID) -> Approval | None:
        with self._lock:
            return self._approvals.get(approval_id)

    async def list_for_execution(
        self, execution_id: UUID, *, status: ApprovalStatus | None = None
    ) -> tuple[Approval, ...]:
        with self._lock:
            return tuple(
                a
                for a in self._approvals.values()
                if a.execution_id == execution_id and (status is None or a.status is status)
            )

    async def list_by_status(self, status: ApprovalStatus) -> tuple[Approval, ...]:
        with self._lock:
            return tuple(a for a in self._approvals.values() if a.status is status)

    async def compare_and_set(self, updated: Approval, *, expected_status: ApprovalStatus) -> None:
        with self._lock:
            current = self._approvals.get(updated.approval_id)
            if current is None:
                msg = f"approval {updated.approval_id} does not exist"
                raise StorageError(msg, execution_id=updated.execution_id)
            if current.status is not expected_status:
                msg = (
                    f"approval {updated.approval_id} is {current.status.value}, "
                    f"expected {expected_status.value}"
                )
                raise ConcurrentModificationError(msg, execution_id=updated.execution_id)
            self._approvals[updated.approval_id] = updated


class MemoryEventRepository:
    """Append-only, gapless event streams."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: dict[UUID | None, list[WorkEvent]] = {}

    async def append(self, event: WorkEvent) -> None:
        with self._lock:
            stream = self._streams.setdefault(event.execution_id, [])
            expected = len(stream) + 1
            if event.sequence_number != expected:
                msg = f"expected sequence {expected}, got {event.sequence_number}"
                raise ConcurrentModificationError(msg, execution_id=event.execution_id)
            stream.append(event)

    async def last_sequence(self, execution_id: UUID | None) -> int:
        with self._lock:
            return len(self._streams.get(execution_id, ()))

    async def list(self, execution_id: UUID | None) -> tuple[WorkEvent, ...]:
        with self._lock:
            return tuple(self._streams.get(execution_id, ()))


class MemoryIdempotencyRepository:
    """Atomic idempotency reservations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[IdempotencyScope, IdempotencyRecord] = {}

    async def reserve(self, record: IdempotencyRecord) -> IdempotencyRecord:
        with self._lock:
            return self._records.setdefault(record.scope, record)

    async def get(self, scope: IdempotencyScope) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(scope)


class MemoryEvidenceRepository:
    """Evidence records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._evidence: dict[UUID, list[Evidence]] = {}

    async def add(self, evidence: Evidence) -> None:
        with self._lock:
            records = self._evidence.setdefault(evidence.execution_id, [])
            if any(e.evidence_id == evidence.evidence_id for e in records):
                msg = f"evidence {evidence.evidence_id} already exists"
                raise StorageError(msg, execution_id=evidence.execution_id)
            records.append(evidence)

    async def list(self, execution_id: UUID) -> tuple[Evidence, ...]:
        with self._lock:
            return tuple(self._evidence.get(execution_id, ()))


class MemoryInputStore:
    """Validated inputs, kept out of events and evidence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inputs: dict[str, BaseModel] = {}

    async def put(self, execution_id: UUID, data: BaseModel) -> str:
        reference = f"memory://inputs/{execution_id}"
        with self._lock:
            if reference in self._inputs:
                msg = f"input for execution {execution_id} already stored"
                raise StorageError(msg, execution_id=execution_id)
            self._inputs[reference] = data
        return reference

    async def get(self, reference: str) -> BaseModel | None:
        with self._lock:
            return self._inputs.get(reference)


@dataclass(frozen=True, slots=True)
class MemoryStorage:
    """All in-memory repositories, bundled for ``WorkRunner``."""

    executions: MemoryExecutionRepository = field(default_factory=MemoryExecutionRepository)
    approvals: MemoryApprovalRepository = field(default_factory=MemoryApprovalRepository)
    events: MemoryEventRepository = field(default_factory=MemoryEventRepository)
    idempotency: MemoryIdempotencyRepository = field(default_factory=MemoryIdempotencyRepository)
    evidence: MemoryEvidenceRepository = field(default_factory=MemoryEvidenceRepository)
    inputs: MemoryInputStore = field(default_factory=MemoryInputStore)
