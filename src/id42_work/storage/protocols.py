"""Storage protocols (specification 12.1).

All methods are asynchronous so that persistent adapters can implement them without
changing callers. ``MemoryStorage`` keeps state in one process. ``SqliteStorage``
writes the same records to a file. Every state change is a single atomic operation:
implementations must never use read-then-write.
"""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from id42_work._model import FrozenModel
from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence
from id42_work.execution import Execution
from id42_work.identifiers import Identifier, NonEmptyStr, SemVer, Sha256Hex
from id42_work.journal import JournalEntry, JournalStatus


class IdempotencyScope(FrozenModel):
    """Scope in which an idempotency key must be unique."""

    tenant_id: NonEmptyStr | None
    work_id: Identifier
    work_version: SemVer
    idempotency_key: NonEmptyStr


class IdempotencyRecord(FrozenModel):
    """Reservation of an idempotency scope for one execution."""

    scope: IdempotencyScope
    input_fingerprint: Sha256Hex
    execution_id: UUID


class ExecutionRepository(Protocol):
    """Execution records and their action journal."""

    async def create(self, execution: Execution) -> None:
        """Store a new execution (revision 0). Raise ``StorageError`` if it exists."""
        ...

    async def get(self, execution_id: UUID) -> Execution | None: ...

    async def compare_and_set(self, updated: Execution) -> None:
        """Replace the stored record if its revision is ``updated.revision - 1``.

        Raise ``ConcurrentModificationError`` otherwise.
        """
        ...

    async def append_journal(self, entry: JournalEntry) -> None:
        """Add a journal entry. Raise ``StorageError`` if the action id exists."""
        ...

    async def update_journal(self, entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        """Replace a journal entry if its status is ``expected_status``.

        Raise ``ConcurrentModificationError`` otherwise.
        """
        ...

    async def list_journal(self, execution_id: UUID) -> tuple[JournalEntry, ...]: ...


class ApprovalRepository(Protocol):
    """Approval records."""

    async def create(self, approval: Approval) -> None: ...

    async def get(self, approval_id: UUID) -> Approval | None: ...

    async def list_for_execution(
        self, execution_id: UUID, *, status: ApprovalStatus | None = None
    ) -> tuple[Approval, ...]: ...

    async def list_by_status(self, status: ApprovalStatus) -> tuple[Approval, ...]:
        """Every approval currently in ``status`` (used to expire due approvals)."""
        ...

    async def compare_and_set(self, updated: Approval, *, expected_status: ApprovalStatus) -> None:
        """Replace the approval if its current status is ``expected_status``.

        Raise ``ConcurrentModificationError`` otherwise.
        """
        ...


class EventRepository(Protocol):
    """Append-only event streams, one per execution (``None`` for registry events)."""

    async def append(self, event: WorkEvent) -> None:
        """Append if ``event.sequence_number`` is the stream's last sequence plus one.

        Raise ``ConcurrentModificationError`` otherwise. Sequences are gapless.
        """
        ...

    async def last_sequence(self, execution_id: UUID | None) -> int: ...

    async def list(self, execution_id: UUID | None) -> tuple[WorkEvent, ...]: ...


class IdempotencyRepository(Protocol):
    """Atomic reservation of idempotency scopes."""

    async def reserve(self, record: IdempotencyRecord) -> IdempotencyRecord:
        """Store ``record`` if its scope is free; otherwise return the existing record.

        The caller compares fingerprints to detect conflicts.
        """
        ...

    async def release(self, scope: IdempotencyScope, *, execution_id: UUID) -> None:
        """Drop the reservation if it still belongs to ``execution_id``.

        A reservation owned by another execution is left in place. Releasing a scope
        that is already free does nothing.
        """
        ...

    async def get(self, scope: IdempotencyScope) -> IdempotencyRecord | None: ...


class EvidenceRepository(Protocol):
    """Evidence records."""

    async def add(self, evidence: Evidence) -> None: ...

    async def list(self, execution_id: UUID) -> tuple[Evidence, ...]: ...


class InputStore(Protocol):
    """Holds validated inputs outside events and evidence."""

    async def put(self, execution_id: UUID, data: BaseModel) -> str:
        """Store ``data`` and return an opaque reference."""
        ...

    async def get(self, reference: str) -> BaseModel | None: ...


class Storage(Protocol):
    """The set of repositories a runner needs."""

    async def commit_execution(
        self, updated: Execution, event: WorkEvent, *, create: bool = False
    ) -> None:
        """Store ``updated`` and append ``event``, both or neither.

        When ``create`` is true, ``updated`` is a new revision-0 execution. Otherwise
        it replaces the stored record only if the stored revision is
        ``updated.revision - 1``. The event is appended only if its sequence is the
        stream's next one. A conflict raises ``ConcurrentModificationError`` and leaves
        both the execution and the stream unchanged.
        """
        ...

    @property
    def executions(self) -> ExecutionRepository: ...

    @property
    def approvals(self) -> ApprovalRepository: ...

    @property
    def events(self) -> EventRepository: ...

    @property
    def idempotency(self) -> IdempotencyRepository: ...

    @property
    def evidence(self) -> EvidenceRepository: ...

    @property
    def inputs(self) -> InputStore: ...
