"""Canonical JSON encoding and SHA-256 digests (specification section 3.8).

Every fingerprint and digest in the SDK goes through this module so that the same
logical value always produces the same bytes, in any process and on any platform.

Rules:

- Mapping keys must be strings; output keys are sorted.
- ``Decimal`` is encoded by its exact string form: ``Decimal("1250.00")`` and
  ``Decimal("1250.0")`` produce different digests on purpose (fail closed).
- ``float`` is rejected by default. Definitions, whose JSON Schemas may contain
  floats, use ``floats="repr"`` (shortest round-trip representation).
- Datetimes must be timezone-aware and are encoded in UTC (ISO 8601).
- ``timedelta`` is encoded as an integer number of microseconds with a ``us`` suffix.
- Sets are encoded as lists sorted by the canonical encoding of their elements.
- Pydantic models are encoded from ``model_dump(mode="python")``.
- ``bytes`` and any other type raise ``TypeError``.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel

FloatPolicy = Literal["reject", "repr"]


def to_canonical(value: object, *, floats: FloatPolicy = "reject") -> object:
    """Convert ``value`` into plain JSON-compatible data following the canonical rules."""
    return _normalize(value, floats, "$")


def canonical_json(value: object, *, floats: FloatPolicy = "reject") -> bytes:
    """Return the canonical UTF-8 JSON encoding of ``value``."""
    return _dumps(to_canonical(value, floats=floats)).encode("utf-8")


def digest(value: object, *, floats: FloatPolicy = "reject") -> str:
    """Return the lowercase hex SHA-256 of the canonical JSON encoding of ``value``."""
    return hashlib.sha256(canonical_json(value, floats=floats)).hexdigest()


def _dumps(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalize(value: object, floats: FloatPolicy, path: str) -> object:  # noqa: PLR0911, PLR0912
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _normalize(cast("object", value.value), floats, path)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if floats == "reject":
            msg = f"float values cannot be digested (at {path}); use Decimal or int"
            raise TypeError(msg)
        if value != value or value in (float("inf"), float("-inf")):  # noqa: PLR0124
            msg = f"non-finite float at {path}"
            raise TypeError(msg)
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            msg = f"non-finite Decimal at {path}"
            raise TypeError(msg)
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            msg = f"naive datetime at {path}; timezone-aware datetimes are required"
            raise TypeError(msg)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return f"{value // timedelta(microseconds=1)}us"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python"), floats, path)
    if isinstance(value, Mapping):
        return _normalize_mapping(cast("Mapping[object, object]", value), floats, path)
    if isinstance(value, list | tuple):
        items = cast("list[object] | tuple[object, ...]", value)
        return [_normalize(item, floats, f"{path}[{i}]") for i, item in enumerate(items)]
    if isinstance(value, set | frozenset):
        members = cast("set[object] | frozenset[object]", value)
        normalized = [_normalize(item, floats, f"{path}{{}}") for item in members]
        return sorted(normalized, key=_dumps)
    msg = f"type {type(value).__name__} cannot be canonicalized (at {path})"
    raise TypeError(msg)


def _normalize_mapping(
    value: Mapping[object, object], floats: FloatPolicy, path: str
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, Enum) and isinstance(key.value, str):
            key_str = key.value
        elif isinstance(key, str):
            key_str = str(key)
        else:
            msg = f"mapping keys must be strings (at {path}, got {type(key).__name__})"
            raise TypeError(msg)
        if key_str in result:
            msg = f"duplicate key {key_str!r} after normalization (at {path})"
            raise TypeError(msg)
        result[key_str] = _normalize(item, floats, f"{path}.{key_str}")
    return result
