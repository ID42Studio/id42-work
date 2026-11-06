"""Timezone-aware UTC time helpers."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

Clock = Callable[[], datetime]
"""A callable returning the current time as a timezone-aware datetime."""


def to_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC, rejecting naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "a timezone-aware datetime is required"
        raise ValueError(msg)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    """Default clock: the current time in UTC."""
    return datetime.now(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(to_utc)]
"""A datetime that must be timezone-aware and is normalized to UTC."""
