"""Distinct MCP callers stay distinct actors. A grant does not widen the page."""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import CallToolResult

from id42_work import ActorAuthority, ExecutionStatus
from id42_work.actors import TrustedActor
from id42_work.mcp import McpCaller, McpCallError, WorkMcpServer, bind_mcp_caller
from id42_work.mcp.session import (
    ACTOR_CREDENTIAL_HEADER,
    bind_request_actor,
    credential_session_actor,
)
from id42_work.testing.fakes import ManualClock
from mcp_support import greeting_server

_DECISION_TOOLS = ("approve", "reject", "reconcile", "abandon")


def _issue(authority: ActorAuthority, actor_id: str, grants: set[str]) -> TrustedActor:
    return authority.issue(
        actor_id=actor_id,
        grants=grants,
        ttl=timedelta(hours=1),
        tenant_id="acme",
    )


def _server(
    *,
    grants: set[str] | None = None,
    fallback: bool = True,
) -> tuple[WorkMcpServer, ActorAuthority]:
    hosted, _runner, _provider = greeting_server(grants=grants)
    authority = hosted.authority

    def issue() -> TrustedActor:
        return _issue(authority, "user:ada", {"directory.read"} if grants is None else grants)

    hook = credential_session_actor(fallback=issue if fallback else None)
    server = WorkMcpServer(
        hosted.runner,
        authority=authority,
        issue_actor=issue,
        session_actor=hook,
    )
    return server, authority


def _http(actor: TrustedActor, session_id: str | None) -> McpCaller:
    return McpCaller(
        transport="http",
        session_id=session_id,
        credential=actor.to_credential(),
    )


def _ids(page: object) -> list[str]:
    assert isinstance(page, dict)
    rows = page["executions"]
    assert isinstance(rows, list)
    found: list[str] = []
    for item in rows:
        assert isinstance(item, dict)
        value = item["execution_id"]
        assert isinstance(value, str)
        found.append(value)
    return found


def _rewrite_actor(credential: str, actor_id: str) -> str:
    raw = credential.removeprefix("id42a1.")
    body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert isinstance(body, dict)
    body["actor_id"] = actor_id
    token = base64.urlsafe_b64encode(json.dumps(body).encode()).decode("ascii").rstrip("=")
    return "id42a1." + token


@pytest.mark.asyncio
async def test_the_default_server_ignores_a_foreign_credential() -> None:
    hosted, runner, _provider = greeting_server()
    own = await hosted.start_execution("greet", "1.0.0", {"name": "ada"})
    other = hosted.authority.issue(
        actor_id="user:bea",
        grants={"directory.read"},
        ttl=timedelta(minutes=5),
        tenant_id="acme",
    )
    foreign = await runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "bea"}, actor=other
    )
    caller = _http(other, "sess-bea")

    async def _list(_ctx: object) -> dict[str, Any]:
        return cast("dict[str, Any]", await hosted.list_executions())

    ctx = cast(
        "ServerRequestContext[Any, Any]",
        SimpleNamespace(
            request=SimpleNamespace(
                headers={
                    ACTOR_CREDENTIAL_HEADER: other.to_credential(),
                    "mcp-session-id": "sess-bea",
                }
            )
        ),
    )
    page = await bind_request_actor(ctx, _list)
    assert isinstance(page, dict)
    assert _ids(page) == [str(own["execution_id"])]
    assert str(foreign.execution_id) not in _ids(page)
    with bind_mcp_caller(caller):
        again = await hosted.list_executions(limit=100)
    assert _ids(again) == [str(own["execution_id"])]
    result = await hosted.server.call_tool(
        "list_executions",
        {"credential": other.to_credential(), "actor_id": "user:bea", "grants": ["directory.read"]},
    )
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    content = result.structured_content
    assert isinstance(content, dict)
    assert _ids(content) == [str(own["execution_id"])]


@pytest.mark.asyncio
async def test_http_sessions_see_only_their_own_executions() -> None:
    server, authority = _server()
    ada = _issue(authority, "user:ada", {"directory.read"})
    bea = _issue(authority, "user:bea", {"directory.read"})
    with bind_mcp_caller(_http(ada, "sess-ada")):
        ada_run = await server.start_execution("greet", "1.0.0", {"name": "ada"})
    with bind_mcp_caller(_http(bea, "sess-bea")):
        bea_run = await server.start_execution("greet", "1.0.0", {"name": "bea"})
    with bind_mcp_caller(_http(ada, "sess-ada")):
        ada_later = await server.start_execution("greet", "1.0.0", {"name": "ada-2"})
    ada_id = str(ada_run["execution_id"])
    ada_later_id = str(ada_later["execution_id"])
    bea_id = str(bea_run["execution_id"])
    with bind_mcp_caller(_http(ada, "sess-ada")):
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(4):
            page = await server.list_executions(limit=1, cursor=cursor)
            seen.extend(_ids(page))
            nxt = page["next_cursor"]
            if not isinstance(nxt, str):
                break
            cursor = nxt
        assert bea_id not in seen
        assert set(seen) == {ada_id, ada_later_id}
        assert (await server.get_execution(ada_id))["actor_id"] == "user:ada"
        with pytest.raises(McpCallError, match="execution not found"):
            await server.get_execution(bea_id)
        with pytest.raises(McpCallError, match="execution not found"):
            await server.resume_execution(bea_id)
        with pytest.raises(McpCallError, match="execution not found"):
            await server.cancel_execution(bea_id)
        with pytest.raises(ResourceNotFoundError):
            await server.server.read_resource(f"id42://executions/{bea_id}/journal")
    with bind_mcp_caller(_http(bea, "sess-bea")):
        assert _ids(await server.list_executions()) == [bea_id]
        journal = await server.read_journal(bea_id)
        first = await server.list_executions(limit=1)
        assert _ids(first) == [bea_id]
        assert first["next_cursor"] is None
    assert bea_id in journal
    assert ada_id not in journal


@pytest.mark.asyncio
async def test_resume_and_cancel_grants_do_not_reveal_another_actor() -> None:
    grants = {"directory.read", "resume:greet", "cancel:greet"}
    server, authority = _server(grants=grants)
    ada = _issue(authority, "user:ada", grants)
    bea = _issue(authority, "user:bea", {"directory.read"})
    foreign = await server.runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "bea"}, actor=bea
    )
    with bind_mcp_caller(_http(ada, "sess-ada")):
        page = await server.list_executions(limit=50)
        assert _ids(page) == []
        with pytest.raises(McpCallError, match="execution not found"):
            await server.resume_execution(str(foreign.execution_id))
        with pytest.raises(McpCallError, match="execution not found"):
            await server.cancel_execution(str(foreign.execution_id))
    kept = await server.runner.get(foreign.execution_id)
    assert kept is not None
    assert kept.status is ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_bad_credential_is_refused_and_a_session_stays_pinned() -> None:
    server, authority = _server()
    ada = _issue(authority, "user:ada", {"directory.read"})
    bea = _issue(authority, "user:bea", {"directory.read"})
    with bind_mcp_caller(_http(ada, "sess-1")):
        await server.list_works()
    refreshed = _issue(authority, "user:ada", {"directory.read"})
    with bind_mcp_caller(_http(refreshed, "sess-1")):
        await server.list_works()
    with (
        bind_mcp_caller(_http(bea, "sess-1")),
        pytest.raises(McpCallError, match="session actor changed"),
    ):
        await server.list_works()
    with (
        bind_mcp_caller(McpCaller(transport="http", session_id="sess-2")),
        pytest.raises(McpCallError, match="credential is required"),
    ):
        await server.list_works()
    flipped = ada.to_credential()
    forged = McpCaller(
        transport="http",
        session_id="sess-3",
        credential=flipped[:-1] + ("B" if flipped[-1] != "B" else "C"),
    )
    with bind_mcp_caller(forged), pytest.raises(McpCallError, match="refused"):
        await server.list_works()
    rewritten = McpCaller(
        transport="http",
        session_id="sess-4",
        credential=_rewrite_actor(ada.to_credential(), "user:root"),
    )
    with bind_mcp_caller(rewritten), pytest.raises(McpCallError, match="refused"):
        await server.list_works()
    outsider = ActorAuthority("other-host").issue(
        actor_id="user:ada",
        grants={"directory.read"},
        ttl=timedelta(minutes=5),
        tenant_id="acme",
    )
    with bind_mcp_caller(_http(outsider, "sess-5")), pytest.raises(McpCallError, match="refused"):
        await server.list_works()


@pytest.mark.asyncio
async def test_an_expired_credential_is_refused() -> None:
    clock = ManualClock()
    hosted, _runner, _provider = greeting_server()
    authority = hosted.authority
    authority._clock = clock
    server = WorkMcpServer(
        hosted.runner,
        authority=authority,
        issue_actor=lambda: _issue(authority, "user:ada", {"directory.read"}),
        session_actor=credential_session_actor(),
    )
    actor = authority.issue(
        actor_id="user:ada",
        grants={"directory.read"},
        ttl=timedelta(seconds=30),
        tenant_id="acme",
    )
    clock.advance(timedelta(minutes=5))
    with bind_mcp_caller(_http(actor, "sess-old")), pytest.raises(McpCallError, match="refused"):
        await server.list_works()


@pytest.mark.asyncio
async def test_stdio_stays_one_actor_and_http_without_a_session_does_not() -> None:
    hosted, _runner, _provider = greeting_server()
    authority = hosted.authority
    who = {"actor_id": "user:ada"}

    def issue() -> TrustedActor:
        return _issue(authority, who["actor_id"], {"directory.read"})

    server = WorkMcpServer(
        hosted.runner,
        authority=authority,
        issue_actor=issue,
        session_actor=credential_session_actor(fallback=issue),
    )
    with bind_mcp_caller(McpCaller(transport="stdio")):
        assert (await server.list_works())["works"]
    who["actor_id"] = "user:bea"
    with (
        bind_mcp_caller(McpCaller(transport="stdio")),
        pytest.raises(McpCallError, match="identity changed"),
    ):
        await server.list_works()
    with pytest.raises(McpCallError, match="identity changed"):
        await server.list_works()
    bea = _issue(authority, "user:bea", {"directory.read"})
    ada = _issue(authority, "user:ada", {"directory.read"})
    foreign = await server.runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "bea"}, actor=bea
    )
    with bind_mcp_caller(_http(bea, None)):
        assert _ids(await server.list_executions(limit=1)) == [str(foreign.execution_id)]
    who["actor_id"] = "user:ada"
    with bind_mcp_caller(McpCaller(transport="direct")):
        await server.list_works()
    with bind_mcp_caller(_http(ada, None)):
        await server.list_works()
        assert _ids(await server.list_executions()) == []


@pytest.mark.asyncio
async def test_host_actor_stays_the_factory_actor() -> None:
    server, authority = _server()
    bea = _issue(authority, "user:bea", {"directory.read"})
    with bind_mcp_caller(_http(bea, "sess-bea")):
        assert server.host_actor().actor_id == "user:ada"
        started = await server.start_execution("greet", "1.0.0", {"name": "bea"})
        assert started["actor_id"] == "user:bea"
    assert server.host_actor().actor_id == "user:ada"
    assert server.host_actor().tenant_id == "acme"


@pytest.mark.asyncio
async def test_a_hook_error_stays_a_client_error_and_tools_do_not_decide() -> None:
    hosted, _runner, _provider = greeting_server()

    def hook(caller: McpCaller) -> TrustedActor:
        if caller.credential == "keep":
            raise McpCallError("keep me")
        if caller.credential == "boom":
            raise RuntimeError("down")
        return TrustedActor(
            actor_id="user:ada",
            tenant_id="acme",
            grants=frozenset({"directory.read"}),
            issued_at=hosted.host_actor().issued_at,
            expires_at=hosted.host_actor().expires_at,
            authority_id="mcp-test",
        )

    server = WorkMcpServer(
        hosted.runner,
        authority=hosted.authority,
        issue_actor=hosted.host_actor,
        session_actor=hook,
    )
    with (
        bind_mcp_caller(McpCaller(transport="http", session_id="s", credential="keep")),
        pytest.raises(McpCallError, match="keep me"),
    ):
        await server.list_works()
    with (
        bind_mcp_caller(McpCaller(transport="http", session_id="s", credential="boom")),
        pytest.raises(McpCallError, match="could not resolve"),
    ):
        await server.list_works()
    with (
        bind_mcp_caller(McpCaller(transport="direct")),
        pytest.raises(McpCallError, match="the host actor was refused"),
    ):
        await server.list_works()
    names = {tool.name for tool in await server.server.list_tools()}
    assert names.isdisjoint(set(_DECISION_TOOLS))
    assert "cannot approve, reject, reconcile" in str(server.server.instructions)
    with pytest.raises(ToolError, match="Unknown tool"):
        await server.server.call_tool("approve", {})
