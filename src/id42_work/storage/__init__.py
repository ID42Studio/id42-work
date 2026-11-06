"""Storage protocols. In-memory implementations arrive in milestone M3."""

from id42_work.storage.protocols import (
    ApprovalRepository,
    EventRepository,
    EvidenceRepository,
    ExecutionRepository,
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyScope,
    InputStore,
)

__all__ = [
    "ApprovalRepository",
    "EventRepository",
    "EvidenceRepository",
    "ExecutionRepository",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "IdempotencyScope",
    "InputStore",
]
