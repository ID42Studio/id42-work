"""Shared checks for a resume claim.

SQLite stores the deadline this returns. PostgreSQL keeps a session advisory lock
instead, and still rejects the same bad arguments so every backend fails the same way.
"""

from datetime import UTC, datetime, timedelta

from id42_work.exceptions import StorageError


def claim_deadline(owner: str, now: datetime, ttl: timedelta) -> datetime:
    """Return the UTC instant when a lease stops being reserved.

    Raises ``StorageError`` when ``owner`` is blank, ``now`` is naive or ``ttl``
    is not positive.
    """
    if not owner.strip():
        msg = "resume claim owner must be non-empty"
        raise StorageError(msg)
    if now.tzinfo is None:
        msg = "resume claim time must be timezone-aware"
        raise StorageError(msg)
    if ttl <= timedelta(0):
        msg = "resume claim ttl must be positive"
        raise StorageError(msg)
    return now.astimezone(UTC) + ttl
