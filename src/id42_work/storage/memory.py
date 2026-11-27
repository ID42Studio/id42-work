"""In-memory storage (specification 12).

Scope: one Python process. Each repository guards its state with one
``threading.Lock``; critical sections never ``await``, so an instance is safe from one
event loop and from several threads that each run their own loop. Nothing survives the
process: no crash recovery, durability or distributed consistency is claimed.
"""

import contextlib
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel

from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus
from id42_work.storage.claims import claim_deadline
from id42_work.storage.codec import executions_by_age
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope


class MemoryExecutionRepository:
    """Executions and their action journals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executions: dict[UUID, Execution] = {}
        self._journals: dict[UUID, dict[str, JournalEntry]] = {}

    @contextlib.contextmanager
    def hold(self) -> Generator[None]:
        """Hold this repository's lock. Joint commits acquire executions, then events."""
        with self._lock:
            yield

    def insert_new(self, execution: Execution) -> None:
        """Store a revision-0 execution. Caller holds :meth:`hold`."""
        eid = execution.execution_id
        if eid in self._executions:
            msg = f"execution {eid} already exists"
            raise StorageError(msg, execution_id=eid)
        if execution.revision != 0:
            msg = "new executions must have revision 0"
            raise StorageError(msg, execution_id=eid)
        self._executions[eid] = execution
        self._journals[eid] = {}

    def replace(self, updated: Execution) -> None:
        """Compare-and-set. Caller holds :meth:`hold`."""
        eid = updated.execution_id
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

    async def create(self, execution: Execution) -> None:
        with self._lock:
            self.insert_new(execution)

    async def get(self, execution_id: UUID) -> Execution | None:
        with self._lock:
            return self._executions.get(execution_id)

    async def list(self) -> tuple[Execution, ...]:
        with self._lock:
            rows = tuple(self._executions.values())
        return executions_by_age(rows)

    async def compare_and_set(self, updated: Execution) -> None:
        with self._lock:
            self.replace(updated)

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

    @contextlib.contextmanager
    def hold(self) -> Generator[None]:
        """Hold this repository's lock. Joint commits acquire executions, then events."""
        with self._lock:
            yield

    def require_next(self, event: WorkEvent) -> None:
        """Raise if ``event`` is not the next one in its stream. Caller holds :meth:`hold`."""
        expected = len(self._streams.get(event.execution_id, ())) + 1
        if event.sequence_number != expected:
            msg = f"expected sequence {expected}, got {event.sequence_number}"
            raise ConcurrentModificationError(msg, execution_id=event.execution_id)

    def add(self, event: WorkEvent) -> None:
        """Append ``event``. Caller holds :meth:`hold` and has called :meth:`require_next`."""
        self._streams.setdefault(event.execution_id, []).append(event)

    async def append(self, event: WorkEvent) -> None:
        with self._lock:
            self.require_next(event)
            self.add(event)

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

    async def release(self, scope: IdempotencyScope, *, execution_id: UUID) -> None:
        with self._lock:
            current = self._records.get(scope)
            if current is not None and current.execution_id == execution_id:
                del self._records[scope]

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


def _empty_claims() -> dict[UUID, tuple[str, datetime]]:
    return {}


@dataclass(frozen=True, slots=True)
class MemoryStorage:
    """All in-memory repositories, bundled for ``WorkRunner``."""

    executions: MemoryExecutionRepository = field(default_factory=MemoryExecutionRepository)
    approvals: MemoryApprovalRepository = field(default_factory=MemoryApprovalRepository)
    events: MemoryEventRepository = field(default_factory=MemoryEventRepository)
    idempotency: MemoryIdempotencyRepository = field(default_factory=MemoryIdempotencyRepository)
    evidence: MemoryEvidenceRepository = field(default_factory=MemoryEvidenceRepository)
    inputs: MemoryInputStore = field(default_factory=MemoryInputStore)
    _claims: dict[UUID, tuple[str, datetime]] = field(
        default_factory=_empty_claims, compare=False, repr=False
    )
    _claims_lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    async def commit_execution(
        self, updated: Execution, event: WorkEvent, *, create: bool = False
    ) -> None:
        """Store an execution and its event under both repository locks.

        Lock order is executions then events, everywhere. The two writes happen
        before either lock is released, so a conflict leaves both unchanged.
        """
        if event.execution_id != updated.execution_id:
            msg = "event does not belong to the execution"
            raise StorageError(msg, execution_id=updated.execution_id)
        with self.executions.hold(), self.events.hold():
            self.events.require_next(event)
            if create:
                self.executions.insert_new(updated)
            else:
                self.executions.replace(updated)
            self.events.add(event)

    async def claim_resume(
        self, execution_id: UUID, owner: str, *, now: datetime, ttl: timedelta
    ) -> bool:
        """Reserve ``execution_id`` for ``owner`` until ``ttl`` elapses.

        Memory disappears with the process, so a dead pid needs no separate check.
        Callers that share one instance still cannot both hold the reservation.
        """
        expires = claim_deadline(owner, now, ttl)
        aware_now = now.astimezone(UTC)
        with self._claims_lock:
            current = self._claims.get(execution_id)
            if current is not None and current[1] > aware_now:
                return False
            self._claims[execution_id] = (owner, expires)
            return True

    async def release_resume(self, execution_id: UUID, owner: str) -> None:
        """Drop the reservation when ``owner`` still holds it."""
        with self._claims_lock:
            current = self._claims.get(execution_id)
            if current is not None and current[0] == owner:
                del self._claims[execution_id]
