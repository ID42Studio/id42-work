"""Cursors, page limits and the SQL listing query."""

import base64
import json

import pytest
from pydantic import ValidationError

from id42_work.mcp import McpCallError
from id42_work.mcp.pages import (
    MAX_PAGE_LIMIT,
    bounded_limit,
    decode_cursor,
    encode_cursor,
)
from id42_work.storage.listing import execution_select
from id42_work.storage.protocols import ExecutionListQuery


def _token(payload: object) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode("ascii").rstrip("=")
    return "id42c1." + raw


def test_limit_defaults_and_rejects_out_of_range() -> None:
    assert bounded_limit(None) == 50
    assert bounded_limit(1) == 1
    assert bounded_limit(MAX_PAGE_LIMIT) == MAX_PAGE_LIMIT
    for value in (0, -1, MAX_PAGE_LIMIT + 1, True, False, "2", 1.5):
        with pytest.raises(McpCallError, match="limit"):
            bounded_limit(value)


def test_cursor_round_trip_and_rejections() -> None:
    fields = {"id": "greet", "version": "1.0.0"}
    token = encode_cursor(fields)
    assert token.startswith("id42c1.")
    assert decode_cursor(f"  {token}  ", frozenset({"id", "version"})) == fields
    rejected: tuple[object, ...] = (
        1,
        "id42a1.abc",
        "id42c1." + ("a" * 2000),
        "id42c1.@@@",
        _token("not-json"),
        _token(["id"]),
        _token({"id": "greet", "extra": "x"}),
        _token({"id": 1}),
        _token({"id": ""}),
        _token({"id": "a" * 201}),
        _token({"id": "a\nb"}),
        _token({"id": "a\x00b"}),
    )
    for token in rejected:
        with pytest.raises(McpCallError, match="cursor is invalid"):
            decode_cursor(token, frozenset({"id"}))


def test_execution_list_query_rejects_a_partial_page() -> None:
    with pytest.raises(ValidationError, match="actor_id"):
        ExecutionListQuery(restrict_actor=True, actor_id="")
    with pytest.raises(ValidationError, match="together"):
        ExecutionListQuery(after_created_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValidationError, match="positive"):
        ExecutionListQuery(limit=0)
    with pytest.raises(ValidationError, match="empty"):
        ExecutionListQuery(status="")
    accepted = ExecutionListQuery(
        restrict_actor=True,
        actor_id="actor:1",
        after_created_at="2026-01-01T00:00:00Z",
        after_execution_id="00000000-0000-0000-0000-000000000001",
        limit=2,
    )
    assert accepted.actor_id == "actor:1"


def test_sql_page_uses_parameters_on_both_dialects() -> None:
    query = ExecutionListQuery(
        restrict_actor=True,
        actor_id="actor:1",
        tenant_id="tenant",
        status="CANCELLED",
        after_created_at="2026-01-01T12:00:00Z",
        after_execution_id="00000000-0000-0000-0000-000000000002",
        limit=2,
    )
    dialects = (("sqlite", "?"), ("postgres", "%s"))
    for dialect, mark in dialects:
        statement, params = execution_select(query, dialect=dialect)  # type: ignore[arg-type]
        assert mark in statement
        assert "ORDER BY" in statement
        assert params == [
            "actor:1",
            "tenant",
            "CANCELLED",
            "2026-01-01T12:00:00Z",
            "2026-01-01T12:00:00Z",
            "00000000-0000-0000-0000-000000000002",
            2,
        ]
        if dialect == "sqlite":
            assert "json_extract" in statement
        else:
            assert "jsonb" in statement
    no_tenant = ExecutionListQuery(restrict_actor=True, actor_id="actor:1", tenant_id=None)
    statement, params = execution_select(no_tenant, dialect="postgres")
    assert "IS NULL" in statement
    assert params == ["actor:1"]
    unfiltered, empty = execution_select(None, dialect="sqlite")
    assert unfiltered == "SELECT body FROM executions"
    assert empty == []
    plain, plain_params = execution_select(ExecutionListQuery(limit=3), dialect="sqlite")
    assert "WHERE" not in plain
    assert plain_params == [3]
