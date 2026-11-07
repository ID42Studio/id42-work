"""Storage protocols and the in-memory implementation."""

from id42_work.storage.memory import MemoryStorage
from id42_work.storage.protocols import (
    ApprovalRepository,
    EventRepository,
    EvidenceRepository,
    ExecutionRepository,
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyScope,
    InputStore,
    Storage,
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
    "MemoryStorage",
    "Storage",
]
