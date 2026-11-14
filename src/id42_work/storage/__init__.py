"""Storage protocols, the in-memory implementation and the SQLite backend."""

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
from id42_work.storage.sqlite import SqliteStorage

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
    "SqliteStorage",
    "Storage",
]
