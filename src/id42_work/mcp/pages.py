"""Opaque cursors for MCP list tools.

A cursor is a position in one ordered list. It is not a capability: the server
still checks the actor before reading the next page. Tampering it can only move
that actor's window, or make the cursor invalid.
"""

import base64
import json
from collections.abc import Mapping
from typing import cast

from id42_work.canonical import canonical_json
from id42_work.mcp.errors import McpCallError

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
_CURSOR_PREFIX = "id42c1."
_MAX_CURSOR = 1024
_MAX_FIELD = 200

WORK_CURSOR_FIELDS = frozenset({"id", "version"})
EXECUTION_CURSOR_FIELDS = frozenset({"created_at", "execution_id"})


def bounded_limit(limit: object) -> int:
    """Page size. ``None`` is the default. Anything outside 1..max is refused."""
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        msg = f"limit must be an integer from 1 to {MAX_PAGE_LIMIT}"
        raise McpCallError(msg)
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        msg = f"limit must be an integer from 1 to {MAX_PAGE_LIMIT}"
        raise McpCallError(msg)
    return limit


def encode_cursor(fields: Mapping[str, str]) -> str:
    """Opaque cursor for ``fields``. Keys are part of the token."""
    payload = {key: fields[key] for key in sorted(fields)}
    token = base64.urlsafe_b64encode(canonical_json(payload)).decode("ascii").rstrip("=")
    return _CURSOR_PREFIX + token


def decode_cursor(token: object, expected: frozenset[str]) -> dict[str, str]:
    """Parse a cursor produced by :func:`encode_cursor` with exactly ``expected`` keys."""
    if not isinstance(token, str):
        raise McpCallError("cursor is invalid")
    text = token.strip()
    if not text.startswith(_CURSOR_PREFIX) or len(text) > _MAX_CURSOR:
        raise McpCallError("cursor is invalid")
    raw = text[len(_CURSOR_PREFIX) :]
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        parsed: object = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        raise McpCallError("cursor is invalid") from None
    if not isinstance(parsed, dict):
        raise McpCallError("cursor is invalid")
    body = cast("dict[object, object]", parsed)
    if frozenset(body) != expected:
        raise McpCallError("cursor is invalid")
    fields: dict[str, str] = {}
    for key in expected:
        value = body[key]
        if not isinstance(value, str) or not _field_ok(value):
            raise McpCallError("cursor is invalid")
        fields[key] = value
    return fields


def _field_ok(value: str) -> bool:
    if not value or len(value) > _MAX_FIELD:
        return False
    return all(character not in value for character in "\r\n\x00")
