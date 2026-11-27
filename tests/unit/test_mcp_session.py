"""Session actor resolution and MCP list pages."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from mcp.server.context import ServerRequestContext

from id42_work import CapabilityRegistry, StaticPolicyResolver, Work, WorkRegistry
from id42_work.actors import TrustedActor
from id42_work.mcp import (
    McpCaller,
    McpCallError,
    WorkMcpServer,
    bind_mcp_caller,
    credential_session_actor,
)
from id42_work.mcp.pages import encode_cursor
from id42_work.mcp.session import bind_request_actor, caller_from_request, current_caller
from mcp_support import Greet, GreetingInput, GreetingOutput, greeting_server, hosted


def test_caller_rejects_a_bad_transport_session_or_credential() -> None:
    with pytest.raises(ValueError, match="transport"):
        McpCaller(transport="socket")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="session_id"):
        McpCaller(transport="http", session_id="bad id")
    with pytest.raises(ValueError, match="session_id"):
        McpCaller(transport="http", session_id="x" * 129)
    with pytest.raises(ValueError, match="credential"):
        McpCaller(transport="http", credential="one\ntwo")
    with pytest.raises(ValueError, match="credential"):
        McpCaller(transport="http", credential="")
    wrong: object = object()
    with pytest.raises(TypeError, match="McpCaller"), bind_mcp_caller(wrong):  # type: ignore[arg-type]
        pass
    assert current_caller().transport == "direct"
    bound = McpCaller(transport="stdio")
    with bind_mcp_caller(bound):
        assert current_caller() is bound
    assert current_caller().transport == "direct"


def test_credential_hook_requires_http_and_parses_stdio_fallback() -> None:
    hook = credential_session_actor()
    with pytest.raises(McpCallError, match="credential is required"):
        hook(McpCaller(transport="http", session_id="s"))
    with pytest.raises(McpCallError, match="credential is required"):
        hook(McpCaller(transport="direct"))
    with pytest.raises(McpCallError, match="McpCaller"):
        hook(object())  # type: ignore[arg-type]
    with pytest.raises(McpCallError, match="refused"):
        hook(McpCaller(transport="http", credential="id42a1.not-a-token"))

    def _keep() -> TrustedActor:
        raise McpCallError("keep me")

    def _boom() -> TrustedActor:
        raise RuntimeError("down")

    with pytest.raises(McpCallError, match="keep me"):
        credential_session_actor(fallback=_keep)(McpCaller(transport="stdio"))
    with pytest.raises(McpCallError, match="could not issue"):
        credential_session_actor(fallback=_boom)(McpCaller(transport="stdio"))


class _SecondLookup:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def get(self, name: str) -> object:
        if name not in self._seen:
            self._seen.add(name)
            return None
        if name == "mcp-session-id":
            return "sess-1"
        if name == "id42-actor-credential":
            return "  id42a1.abc  "
        return ["nope"]


class _ListHeaders:
    def get(self, _name: str) -> list[str]:
        return ["x"]


def test_request_headers_become_a_caller_and_are_reset() -> None:
    assert caller_from_request(None).transport == "stdio"
    assert caller_from_request(SimpleNamespace(headers=None)).credential is None
    assert caller_from_request(SimpleNamespace(headers=object())).session_id is None
    caller = caller_from_request(SimpleNamespace(headers=_SecondLookup()))
    assert caller.transport == "http"
    assert caller.session_id == "sess-1"
    assert caller.credential == "id42a1.abc"
    blank = caller_from_request(
        SimpleNamespace(headers={"mcp-session-id": "bad id", "id42-actor-credential": "   "})
    )
    assert blank.session_id is None
    assert blank.credential == "invalid"
    listed = caller_from_request(SimpleNamespace(headers=_ListHeaders()))
    assert listed.credential is None
    assert listed.session_id is None
    huge = caller_from_request(
        SimpleNamespace(headers={"id42-actor-credential": "a" * 20_000, "mcp-session-id": "ok"})
    )
    assert huge.credential == "invalid"
    assert huge.session_id == "ok"

    seen: list[str] = []

    async def _next(_ctx: object) -> None:
        seen.append(current_caller().transport)
        raise RuntimeError("stop")

    ctx = cast(
        "ServerRequestContext[Any, Any]",
        SimpleNamespace(request=SimpleNamespace(headers={"mcp-session-id": "sess-9"})),
    )

    async def _run() -> None:
        with pytest.raises(RuntimeError, match="stop"):
            await bind_request_actor(ctx, _next)

    asyncio.run(_run())
    assert seen == ["http"]
    assert current_caller().transport == "direct"


def _catalog() -> WorkMcpServer:
    registry = WorkRegistry()
    for work_id, version in (("alpha", "1.0.0"), ("alpha", "1.1.0"), ("beta", "1.0.0")):
        registry.register(
            Work(
                id=work_id,
                version=version,
                name=work_id,
                input_schema=GreetingInput,
                output_schema=GreetingOutput,
                required_capabilities=frozenset({"directory.read"}),
            ),
            executor=Greet(),
        )
    server, _runner, _authority = hosted(
        registry=registry,
        policies=StaticPolicyResolver({}),
        capabilities=CapabilityRegistry(),
        grants={"directory.read"},
    )
    return server


def _ids(page: object) -> list[str]:
    assert isinstance(page, dict)
    rows = page["works"] if "works" in page else page["executions"]
    assert isinstance(rows, list)
    found: list[str] = []
    for item in rows:
        assert isinstance(item, dict)
        value = item["id"] if "id" in item else item["execution_id"]
        assert isinstance(value, str)
        found.append(value)
    return found


@pytest.mark.asyncio
async def test_work_pages_follow_the_registry_order() -> None:
    server = _catalog()
    first = await server.list_works(limit=2)
    assert _ids(first) == ["alpha", "alpha"]
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    second = await server.list_works(limit=2, cursor=cursor)
    assert _ids(second) == ["beta"]
    assert second["next_cursor"] is None
    past = encode_cursor({"id": "beta", "version": "1.0.0"})
    empty = await server.list_works(limit=2, cursor=past)
    assert empty["works"] == []
    assert empty["next_cursor"] is None
    with pytest.raises(McpCallError, match="limit"):
        await server.list_works(limit=True)  # type: ignore[arg-type]
    with pytest.raises(McpCallError, match="cursor is invalid"):
        await server.list_works(cursor=encode_cursor({"created_at": "x", "execution_id": "y"}))


@pytest.mark.asyncio
async def test_execution_pages_follow_storage_order() -> None:
    server, runner, _provider = greeting_server()
    for name in ("ada", "bea", "cy"):
        await server.start_execution("greet", "1.0.0", {"name": name})
    expected = [str(item.execution_id) for item in await runner.list_executions()]
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        page = await server.list_executions(limit=1, cursor=cursor)
        seen.extend(_ids(page))
        nxt = page["next_cursor"]
        if not isinstance(nxt, str):
            break
        cursor = nxt
    assert seen == expected
    with pytest.raises(McpCallError, match="cursor is invalid"):
        await server.list_executions(cursor=encode_cursor({"id": "greet", "version": "1.0.0"}))
    with pytest.raises(McpCallError, match="cursor is invalid"):
        await server.list_executions(
            cursor=encode_cursor(
                {"created_at": "2026-01-01T12:00:00", "execution_id": str(uuid4())}
            )
        )
    with pytest.raises(McpCallError, match="cursor is invalid"):
        await server.list_executions(
            cursor=encode_cursor(
                {"created_at": "2026-01-01T12:00:00+00:00", "execution_id": "not-a-uuid"}
            )
        )
    with pytest.raises(McpCallError, match="limit"):
        await server.list_executions(limit=0)
