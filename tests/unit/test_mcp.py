"""MCP server, views, factory and transports."""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, InputRequiredResult
from pydantic import JsonValue

import mcp_support
from _factories import T0
from id42_work import UntrustedActorError
from id42_work.actors import TrustedActor
from id42_work.mcp import McpCallError, WorkMcpServer, load_factory, serve
from id42_work.mcp._dependency import require_mcp
from id42_work.mcp.server import HTTP_HOST, execution_uri, guard
from id42_work.mcp.views import redact_json
from mcp_support import greeting_server


async def _tool(
    hosted: WorkMcpServer, name: str, arguments: dict[str, object] | None = None
) -> dict[str, JsonValue]:
    result = await hosted.server.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    content = result.structured_content
    assert isinstance(content, dict)
    return content


def _mapping(value: object) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _items(value: object) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


async def _resource(hosted: WorkMcpServer, uri: str) -> str:
    contents = await hosted.server.read_resource(uri)
    assert not isinstance(contents, InputRequiredResult)
    return "".join(
        item.content
        for item in contents
        if isinstance(item, ReadResourceContents) and isinstance(item.content, str)
    )


def test_redact_json_masks_sensitive_keys_and_keeps_scalars() -> None:
    assert redact_json("plain") == "plain"
    assert redact_json(None) is None
    assert redact_json([{"token": "secret", "n": 1}, "ok", {"nested": {"password": "p"}}]) == [
        {"token": "***", "n": 1},
        "ok",
        {"nested": {"password": "***"}},
    ]


def test_require_mcp_accepts_the_installed_sdk() -> None:
    require_mcp()


def test_require_mcp_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr("id42_work.mcp._dependency.importlib.import_module", _missing)
    with pytest.raises(ImportError, match=r"id42-work\[mcp\]"):
        require_mcp()


def test_factory_loads_a_server_and_rejects_bad_specs() -> None:
    loaded = load_factory("mcp_support:server")
    assert isinstance(loaded, WorkMcpServer)
    with pytest.raises(ValueError, match="module:callable"):
        load_factory("not a spec")
    with pytest.raises(ValueError, match="module:callable"):
        load_factory("pkg..mod:server")
    with pytest.raises(ValueError, match="module:callable"):
        load_factory("pkg.:server")
    with pytest.raises(ImportError, match="cannot import"):
        load_factory("mcp_support_missing:server")
    with pytest.raises(AttributeError, match="was not found"):
        load_factory("mcp_support:missing")


def test_factory_rejects_a_non_callable_and_a_wrong_return() -> None:
    mcp_support.NOT_A_FACTORY = 1  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="not callable"):
        load_factory("mcp_support:NOT_A_FACTORY")

    def _wrong() -> None:
        return None

    mcp_support.wrong_factory = _wrong  # type: ignore[attr-defined]
    with pytest.raises(TypeError, match="WorkMcpServer"):
        load_factory("mcp_support:wrong_factory")


def test_constructor_rejects_the_wrong_types() -> None:
    hosted, runner, _provider = greeting_server()
    authority = hosted._authority
    with pytest.raises(TypeError, match="WorkRunner"):
        WorkMcpServer(object(), authority=authority, issue_actor=lambda: None)
    with pytest.raises(TypeError, match="ActorAuthority"):
        WorkMcpServer(runner, authority=object(), issue_actor=lambda: None)
    with pytest.raises(TypeError, match="callable"):
        WorkMcpServer(runner, authority=authority, issue_actor=object())


@pytest.mark.asyncio
async def test_tools_and_resources_round_trip() -> None:
    hosted, _runner, provider = greeting_server(
        result={"display_name": "Ada", "token": "secret-token"}
    )
    catalog = await _tool(hosted, "list_works")
    works = _items(catalog["works"])
    first = _mapping(works[0])
    assert first["id"] == "greet"
    assert "properties" in _mapping(first["input_schema"])

    started = await _tool(
        hosted,
        "start_execution",
        {
            "work_id": "greet",
            "version": "1.0.0",
            "input_data": {"name": "ada"},
            "actor_id": "user:root",
            "grants": ["payments.execute"],
        },
    )
    assert started["status"] == "COMPLETED"
    assert started["actor_id"] == "user:ada"
    assert started["needs_human_decision"] is False
    outcome = started["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["accepted"] is True
    assert outcome["output"] == {"message": "Hello, Ada!"}
    execution_id = str(started["execution_id"])

    fetched = await _tool(hosted, "get_execution", {"execution_id": execution_id})
    assert fetched["execution_id"] == execution_id
    again = await _tool(hosted, "resume_execution", {"execution_id": execution_id})
    assert again["status"] == "COMPLETED"
    listed = await _tool(hosted, "list_executions", {"status": "COMPLETED"})
    rows = _items(listed["executions"])
    assert _mapping(rows[0])["execution_id"] == execution_id
    assert provider.calls == 1

    journal = json.loads(await _resource(hosted, f"id42://executions/{execution_id}/journal"))
    assert journal["entries"][0]["result"]["token"] == "***"  # noqa: S105
    assert journal["entries"][0]["result"]["display_name"] == "Ada"
    assert "params" not in journal["entries"][0]
    events = json.loads(await _resource(hosted, f"id42://executions/{execution_id}/events"))
    assert events["events"][0]["event_type"] == "work.execution.created"
    evidence = json.loads(await _resource(hosted, f"id42://executions/{execution_id}/evidence"))
    assert evidence["evidence"]
    approvals = json.loads(await _resource(hosted, f"id42://executions/{execution_id}/approvals"))
    assert approvals["approvals"] == []


@pytest.mark.asyncio
async def test_hidden_unknown_and_filtered_executions() -> None:
    hosted, runner, _provider = greeting_server()
    other = hosted._authority.issue(
        actor_id="user:other",
        grants={"directory.read"},
        ttl=timedelta(minutes=5),
        tenant_id="other",
    )
    foreign = await runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "bea"}, actor=other
    )
    with pytest.raises(ToolError, match="execution not found"):
        await hosted.server.call_tool("get_execution", {"execution_id": str(foreign.execution_id)})
    with pytest.raises(ResourceNotFoundError, match="execution not found"):
        await hosted.server.read_resource(f"id42://executions/{foreign.execution_id}/journal")
    with pytest.raises(McpCallError, match="execution not found"):
        await hosted.get_execution("not-a-uuid")
    listed = await hosted.list_executions()
    assert listed["executions"] == []
    stored = await runner.list_executions()
    assert [item.execution_id for item in stored] == [foreign.execution_id]
    assert foreign.status.value == "COMPLETED"


@pytest.mark.asyncio
async def test_caller_errors_and_a_failed_host_actor() -> None:
    hosted, _runner, _provider = greeting_server(grants=set())
    denied = await hosted.start_execution("greet", "1.0.0", {"name": "ada"})
    assert denied["status"] == "DENIED"
    reasons = denied["policy_reasons"]
    assert isinstance(reasons, list)
    assert any("missing_capability" in str(reason) for reason in reasons)
    with pytest.raises(McpCallError, match="not registered"):
        await hosted.start_execution("missing", "1.0.0", {})
    with pytest.raises(McpCallError, match="JSON object"):
        await hosted.start_execution("greet", "1.0.0", ["ada"])
    with pytest.raises(McpCallError, match="unknown status"):
        await hosted.list_executions(status="NOPE")

    def _broken() -> TrustedActor:
        raise RuntimeError("authority down")

    hosted._issue_actor = _broken  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="could not issue"):
        await hosted.list_works()
    with pytest.raises(ResourceError, match="could not issue"):
        await hosted.server.read_resource(f"id42://executions/{uuid4()}/events")


@pytest.mark.asyncio
async def test_refuses_an_unsealed_or_changing_actor() -> None:
    hosted, _runner, _provider = greeting_server()
    await hosted.list_works()
    unsealed = TrustedActor(
        actor_id="user:ada",
        tenant_id="acme",
        grants=frozenset({"directory.read"}),
        issued_at=T0,
        expires_at=T0.replace(hour=13),
        authority_id="mcp-test",
    )
    hosted._issue_actor = lambda: unsealed  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="refused"):
        await hosted.get_execution(str(uuid4()))

    hosted2, _runner2, _provider2 = greeting_server()
    await hosted2.list_works()

    def _switch() -> TrustedActor:
        return hosted2._authority.issue(
            actor_id="user:other",
            grants={"directory.read"},
            ttl=timedelta(minutes=5),
            tenant_id="acme",
        )

    hosted2._issue_actor = _switch  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="identity changed"):
        await hosted2.list_works()


@pytest.mark.asyncio
async def test_guard_hides_unexpected_errors_and_propagates_cancellation() -> None:
    async def _cancel() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guard(_cancel())

    hosted, runner, _provider = greeting_server()

    async def _boom() -> tuple[object, ...]:
        raise RuntimeError("disk failed")

    runner.list_executions = _boom  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="could not complete the call") as raised:
        await hosted.list_executions()
    assert "disk failed" not in str(raised.value)


def test_serve_uses_stdio_or_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    hosted, _runner, _provider = greeting_server()
    seen: list[tuple[str, dict[str, object]]] = []

    def _run(transport: str = "stdio", **kwargs: object) -> None:
        seen.append((transport, kwargs))

    monkeypatch.setattr(hosted.server, "run", _run)
    serve(hosted)
    serve(hosted, http=True, port=9000)
    assert seen == [("stdio", {}), ("streamable-http", {"host": "127.0.0.1", "port": 9000})]
    assert HTTP_HOST == "127.0.0.1"
    with pytest.raises(ValueError, match="port"):
        serve(hosted, http=True, port=0)
    with pytest.raises(ValueError, match="port"):
        serve(hosted, http=True, port=True)
    with pytest.raises(TypeError, match="WorkMcpServer"):
        serve(object())


@pytest.mark.asyncio
async def test_cancel_and_idempotent_restart() -> None:
    hosted, _runner, provider = greeting_server()
    first = await hosted.start_execution(
        "greet", "1.0.0", {"name": "ada"}, idempotency_key="greet-1"
    )
    second = await hosted.start_execution(
        "greet", "1.0.0", {"name": "ada"}, idempotency_key="greet-1"
    )
    assert first["execution_id"] == second["execution_id"]
    assert provider.calls == 1
    cancelled = await _tool(
        hosted,
        "cancel_execution",
        {"execution_id": str(first["execution_id"]), "reason": "stop"},
    )
    assert cancelled["status"] == "COMPLETED"
    execution_id = first["execution_id"]
    assert isinstance(execution_id, str)
    assert execution_uri("journal", UUID(execution_id)) == f"id42://executions/{execution_id}/journal"


@pytest.mark.asyncio
async def test_a_refused_runner_and_a_host_error_stay_visible() -> None:
    hosted, runner, _provider = greeting_server()

    async def _refuse(**_kwargs: object) -> object:
        raise UntrustedActorError("rejected by the runner")

    runner.run = _refuse  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="the host actor was refused"):
        await hosted.start_execution("greet", "1.0.0", {"name": "ada"})

    def _already() -> TrustedActor:
        raise McpCallError("keep me")

    hosted._issue_actor = _already  # type: ignore[method-assign]
    with pytest.raises(McpCallError, match="keep me"):
        await hosted.list_works()
