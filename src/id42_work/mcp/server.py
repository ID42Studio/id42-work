"""MCP server over ``WorkRunner``.

The client can start, inspect, resume, list and cancel executions of works the host
registered. It cannot supply an actor id, a grant or an approval decision, and it
cannot call a capability except by running a registered executor. Policies, validators
and approval gates stay inside the runner.

By default one ``issue_actor`` identity is pinned for the process. A host may pass
``session_actor`` so an HTTP session presents a credential from
``TrustedActor.to_credential``; the server verifies it. Stdio stays the factory actor.
Reads and writes are limited to executions whose actor and tenant match that actor.
A ``resume:`` or ``cancel:`` grant does not widen the set. Listing tools return one
page.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TypeVar, cast
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from pydantic import JsonValue

from id42_work import __version__
from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.exceptions import UntrustedActorError, WorkError
from id42_work.execution import Execution
from id42_work.mcp.errors import McpCallError
from id42_work.mcp.pages import (
    EXECUTION_CURSOR_FIELDS,
    WORK_CURSOR_FIELDS,
    bounded_limit,
    decode_cursor,
    encode_cursor,
)
from id42_work.mcp.session import McpCaller, bind_request_actor, current_caller
from id42_work.mcp.views import (
    approvals_view,
    events_view,
    evidence_view,
    execution_summary,
    execution_view,
    journal_view,
    work_view,
)
from id42_work.runner import WorkRunner
from id42_work.states import ExecutionStatus
from id42_work.storage.codec import execution_list_key
from id42_work.storage.protocols import ExecutionListQuery

_LOG = logging.getLogger("id42_work.mcp")
_T = TypeVar("_T")

HTTP_HOST = "127.0.0.1"
"""Streamable HTTP listens only on this address. It is not configurable."""

DEFAULT_HTTP_PORT = 8766
_MIN_PORT = 1
_MAX_PORT = 65535

_INSTRUCTIONS = (
    "This server starts and inspects governed executions of works the host registered. "
    "It cannot approve, reject, reconcile or grant capabilities, and it cannot call a "
    "capability except by running a registered work. The actor is chosen by the host. "
    "If status is WAITING_APPROVAL or WAITING_REVIEW, a person must decide outside "
    "this server; resume_execution does not decide. An UNCERTAIN capability call is "
    "reconciled outside this server; resume_execution does not mark it applied. "
    "Send exact amounts as JSON strings when the work schema uses them. "
    "list_works and list_executions return one page; pass cursor to continue."
)

_FACTORY_RE = re.compile(r"([A-Za-z_][\w.]*):([A-Za-z_]\w*)\Z")

JOURNAL_URI = "id42://executions/{execution_id}/journal"
EVENTS_URI = "id42://executions/{execution_id}/events"
EVIDENCE_URI = "id42://executions/{execution_id}/evidence"
APPROVALS_URI = "id42://executions/{execution_id}/approvals"


def execution_uri(kind: str, execution_id: UUID) -> str:
    """Concrete resource URI for one execution."""
    return f"id42://executions/{execution_id}/{kind}"


def dump_json(payload: Mapping[str, JsonValue]) -> str:
    """Stable JSON text for a resource body."""
    return json.dumps(payload, indent=2, sort_keys=True)


async def guard(awaitable: Awaitable[_T]) -> _T:
    """Turn runner failures into ``McpCallError`` and hide unexpected traces."""
    try:
        return await awaitable
    except McpCallError:
        raise
    except asyncio.CancelledError:
        raise
    except UntrustedActorError as exc:
        raise McpCallError("the host actor was refused") from exc
    except WorkError as exc:
        raise McpCallError(str(exc)) from exc
    except Exception as exc:
        _LOG.exception("mcp call failed")
        raise McpCallError("the server could not complete the call") from exc


class WorkMcpServer:
    """Host-configured MCP server bound to one runner and one actor identity."""

    def __init__(
        self,
        runner: object,
        *,
        authority: object,
        issue_actor: object,
        session_actor: object | None = None,
        name: str = "id42-work",
        instructions: str | None = None,
    ) -> None:
        if not isinstance(runner, WorkRunner):
            msg = "runner must be a WorkRunner"
            raise TypeError(msg)
        if not isinstance(authority, ActorAuthority):
            msg = "authority must be the ActorAuthority that seals the runner's actors"
            raise TypeError(msg)
        if not callable(issue_actor):
            msg = "issue_actor must be a callable that returns a TrustedActor"
            raise TypeError(msg)
        if session_actor is not None and not callable(session_actor):
            msg = "session_actor must be a callable that returns a TrustedActor"
            raise TypeError(msg)
        self._runner = runner
        self._authority = authority
        self._issue_actor = cast("Callable[[], TrustedActor]", issue_actor)
        self._session_actor = cast("Callable[[McpCaller], TrustedActor] | None", session_actor)
        self._host_identity: tuple[str, str | None] | None = None
        self._unscoped_identity: tuple[str, str | None] | None = None
        self._session_identities: dict[str, tuple[str, str | None]] = {}
        self._identity_lock = threading.Lock()
        self._server = MCPServer(
            name,
            instructions=instructions if instructions is not None else _INSTRUCTIONS,
            version=__version__,
        )
        self._server.middleware.append(bind_request_actor)
        self._install(self._server)

    @property
    def server(self) -> MCPServer:
        """The official MCP server. Transports call ``run`` on it."""
        return self._server

    @property
    def runner(self) -> WorkRunner:
        """Runner this server calls. Not an MCP tool, and not a way to approve."""
        return self._runner

    @property
    def authority(self) -> ActorAuthority:
        """Authority that seals actors for this process. Not an MCP tool."""
        return self._authority

    def host_actor(self) -> TrustedActor:
        """Factory actor from ``issue_actor``. Approvals resume as this actor.

        It does not follow a session credential. It can run and resume work. It
        cannot approve it.
        """
        return self._host_actor()

    async def list_works(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> dict[str, JsonValue]:
        """One page of registered works. Requires a valid actor and returns no code."""
        return await guard(self._list_works(limit, cursor))

    async def start_execution(
        self,
        work_id: str,
        version: str,
        input_data: object,
        idempotency_key: str | None = None,
    ) -> dict[str, JsonValue]:
        """Start an execution as the host actor. Approval gates are not skipped."""
        return await guard(self._start(work_id, version, input_data, idempotency_key))

    async def get_execution(self, execution_id: str) -> dict[str, JsonValue]:
        """Status and verified output of one execution owned by the host actor."""
        return await guard(self._get(execution_id))

    async def resume_execution(self, execution_id: str) -> dict[str, JsonValue]:
        """Continue an execution the host actor owns. This does not grant an approval."""
        return await guard(self._resume(execution_id))

    async def list_executions(
        self,
        status: str | None = None,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, JsonValue]:
        """One page of summaries for executions owned by the request actor."""
        return await guard(self._list_executions(status, limit, cursor))

    async def cancel_execution(
        self, execution_id: str, reason: str = "cancelled"
    ) -> dict[str, JsonValue]:
        """Cancel an execution the host actor owns. Cancellation is not an approval."""
        return await guard(self._cancel(execution_id, reason))

    async def read_journal(self, execution_id: str) -> str:
        """JSON journal of an execution owned by the host actor."""
        return await guard(self._read_journal(execution_id))

    async def read_events(self, execution_id: str) -> str:
        """JSON events of an execution owned by the host actor."""
        return await guard(self._read_events(execution_id))

    async def read_evidence(self, execution_id: str) -> str:
        """JSON evidence of an execution owned by the host actor."""
        return await guard(self._read_evidence(execution_id))

    async def read_approvals(self, execution_id: str) -> str:
        """JSON approval states. The resource does not accept a decision."""
        return await guard(self._read_approvals(execution_id))

    async def _list_works(self, limit: int | None, cursor: str | None) -> dict[str, JsonValue]:
        self._request_actor()
        page_limit = bounded_limit(limit)
        works = self._runner.registered_works()
        if cursor is not None:
            fields = decode_cursor(cursor, WORK_CURSOR_FIELDS)
            marker = (fields["id"], fields["version"])
            works = tuple(work for work in works if (work.id, work.version) > marker)
        chosen = works[:page_limit]
        next_cursor = None
        if len(works) > page_limit:
            last = chosen[-1]
            next_cursor = encode_cursor({"id": last.id, "version": last.version})
        payload = {"works": [work_view(work) for work in chosen], "next_cursor": next_cursor}
        return cast("dict[str, JsonValue]", payload)

    async def _start(
        self,
        work_id: str,
        version: str,
        input_data: object,
        idempotency_key: str | None,
    ) -> dict[str, JsonValue]:
        if not isinstance(input_data, dict):
            msg = "input_data must be a JSON object"
            raise McpCallError(msg)
        actor = self._request_actor()
        execution = await self._runner.run(
            work_id=work_id,
            version=version,
            input_data=cast("dict[str, JsonValue]", input_data),
            actor=actor,
            idempotency_key=idempotency_key,
        )
        return await self._view(execution)

    async def _get(self, execution_id: str) -> dict[str, JsonValue]:
        return await self._view(await self._owned(execution_id))

    async def _resume(self, execution_id: str) -> dict[str, JsonValue]:
        current = await self._owned(execution_id)
        execution = await self._runner.resume(current.execution_id, actor=self._request_actor())
        return await self._view(execution)

    async def _list_executions(
        self, status: str | None, limit: int | None, cursor: str | None
    ) -> dict[str, JsonValue]:
        actor = self._request_actor()
        wanted = _parse_status(status)
        page_limit = bounded_limit(limit)
        after_created, after_id = _execution_cursor(cursor)
        rows = await self._runner.list_executions(
            ExecutionListQuery(
                restrict_actor=True,
                actor_id=actor.actor_id,
                tenant_id=actor.tenant_id,
                status=None if wanted is None else wanted.value,
                after_created_at=after_created,
                after_execution_id=after_id,
                limit=page_limit + 1,
            )
        )
        chosen = rows[:page_limit]
        next_cursor = None
        if len(rows) > page_limit:
            last = chosen[-1]
            created, execution_id = execution_list_key(last)
            next_cursor = encode_cursor({"created_at": created, "execution_id": execution_id})
        payload = {
            "executions": [execution_summary(execution) for execution in chosen],
            "next_cursor": next_cursor,
        }
        return cast("dict[str, JsonValue]", payload)

    async def _cancel(self, execution_id: str, reason: str) -> dict[str, JsonValue]:
        current = await self._owned(execution_id)
        execution = await self._runner.cancel(
            current.execution_id, actor=self._request_actor(), reason=reason
        )
        return await self._view(execution)

    async def _read_journal(self, execution_id: str) -> str:
        execution = await self._owned(execution_id)
        entries = await self._runner.journal(execution.execution_id)
        return dump_json(journal_view(execution.execution_id, entries))

    async def _read_events(self, execution_id: str) -> str:
        execution = await self._owned(execution_id)
        events = await self._runner.events(execution.execution_id)
        return dump_json(events_view(execution.execution_id, events))

    async def _read_evidence(self, execution_id: str) -> str:
        execution = await self._owned(execution_id)
        evidence = await self._runner.evidence(execution.execution_id)
        return dump_json(evidence_view(execution.execution_id, evidence))

    async def _read_approvals(self, execution_id: str) -> str:
        execution = await self._owned(execution_id)
        approvals = await self._runner.approvals_for(execution.execution_id)
        return dump_json(approvals_view(execution.execution_id, approvals))

    async def _view(self, execution: Execution) -> dict[str, JsonValue]:
        approvals = await self._runner.approvals_for(execution.execution_id)
        return execution_view(execution, approvals)

    async def _owned(self, execution_id: str) -> Execution:
        actor = self._request_actor()
        try:
            parsed = UUID(execution_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise McpCallError("execution not found", found=False) from exc
        execution = await self._runner.get(parsed)
        if execution is None or not _owns(actor, execution):
            raise McpCallError("execution not found", found=False)
        return execution

    def _host_actor(self) -> TrustedActor:
        trusted = self._verified(self._call_issuer(self._issue_actor), credential=False)
        self._pin_host(trusted)
        return trusted

    def _request_actor(self) -> TrustedActor:
        session_actor = self._session_actor
        if session_actor is None:
            return self._host_actor()
        caller = current_caller()
        issued = self._call_session(session_actor, caller)
        trusted = self._verified(issued, credential=caller.credential is not None)
        self._pin_session(caller, trusted)
        return trusted

    def _call_issuer(self, issue: Callable[[], TrustedActor]) -> TrustedActor:
        try:
            return issue()
        except McpCallError:
            raise
        except Exception as exc:
            raise McpCallError("the host could not issue an actor") from exc

    def _call_session(
        self, session_actor: Callable[[McpCaller], TrustedActor], caller: McpCaller
    ) -> TrustedActor:
        try:
            return session_actor(caller)
        except McpCallError:
            raise
        except Exception as exc:
            raise McpCallError("the host could not resolve an actor") from exc

    def _verified(self, issued: object, *, credential: bool) -> TrustedActor:
        try:
            return self._authority.verify(issued)
        except UntrustedActorError as exc:
            if credential:
                raise McpCallError("the actor credential was refused") from exc
            raise McpCallError("the host actor was refused") from exc

    def _pin_host(self, actor: TrustedActor) -> None:
        identity = (actor.actor_id, actor.tenant_id)
        with self._identity_lock:
            if self._host_identity is None:
                self._host_identity = identity
            elif self._host_identity != identity:
                raise McpCallError("the host actor identity changed")

    def _pin_session(self, caller: McpCaller, actor: TrustedActor) -> None:
        identity = (actor.actor_id, actor.tenant_id)
        if caller.transport == "http" and caller.session_id is None:
            return
        key = caller.session_id if caller.transport == "http" else None
        with self._identity_lock:
            if key is None:
                if self._unscoped_identity is None:
                    self._unscoped_identity = identity
                elif self._unscoped_identity != identity:
                    raise McpCallError("the host actor identity changed")
                return
            current = self._session_identities.get(key)
            if current is None:
                self._session_identities[key] = identity
            elif current != identity:
                raise McpCallError("the session actor changed")

    def _install(self, protocol: MCPServer) -> None:
        host = self

        @protocol.tool()
        async def list_works(
            limit: int | None = None, cursor: str | None = None
        ) -> dict[str, JsonValue]:
            """List one page of works the host registered, including schemas."""
            return await _as_tool(host.list_works(limit=limit, cursor=cursor))

        @protocol.tool()
        async def start_execution(
            work_id: str,
            version: str,
            input_data: dict[str, JsonValue],
            idempotency_key: str | None = None,
        ) -> dict[str, JsonValue]:
            """Start a governed execution. The actor is the host's. Gates still apply."""
            started = host.start_execution(work_id, version, input_data, idempotency_key)
            return await _as_tool(started)

        @protocol.tool()
        async def get_execution(execution_id: str) -> dict[str, JsonValue]:
            """Return the status and verified output of an execution the host actor owns."""
            return await _as_tool(host.get_execution(execution_id))

        @protocol.tool()
        async def resume_execution(execution_id: str) -> dict[str, JsonValue]:
            """Resume an owned execution. A waiting approval stays waiting."""
            return await _as_tool(host.resume_execution(execution_id))

        @protocol.tool()
        async def list_executions(
            status: str | None = None,
            limit: int | None = None,
            cursor: str | None = None,
        ) -> dict[str, JsonValue]:
            """List one page of executions owned by the caller. Optional status filter."""
            return await _as_tool(host.list_executions(status, limit=limit, cursor=cursor))

        @protocol.tool()
        async def cancel_execution(
            execution_id: str, reason: str = "cancelled"
        ) -> dict[str, JsonValue]:
            """Cancel an owned execution. Cancelling does not approve it."""
            return await _as_tool(host.cancel_execution(execution_id, reason))

        @protocol.resource(
            JOURNAL_URI,
            mime_type="application/json",
            description="Action journal of an execution owned by the host actor.",
        )
        async def execution_journal(execution_id: str) -> str:
            return await _as_resource(host.read_journal(execution_id))

        @protocol.resource(
            EVENTS_URI,
            mime_type="application/json",
            description="Events of an execution owned by the host actor.",
        )
        async def execution_events(execution_id: str) -> str:
            return await _as_resource(host.read_events(execution_id))

        @protocol.resource(
            EVIDENCE_URI,
            mime_type="application/json",
            description="Evidence references of an execution owned by the host actor.",
        )
        async def execution_evidence(execution_id: str) -> str:
            return await _as_resource(host.read_evidence(execution_id))

        @protocol.resource(
            APPROVALS_URI,
            mime_type="application/json",
            description="Approval states for an owned execution. Decisions are not accepted.",
        )
        async def execution_approvals(execution_id: str) -> str:
            return await _as_resource(host.read_approvals(execution_id))


def serve(server: object, *, http: bool = False, port: object = DEFAULT_HTTP_PORT) -> None:
    """Serve ``server`` on stdio, or on streamable HTTP bound to ``127.0.0.1``.

    ``--http`` is the only way to leave stdio. The bind address cannot be changed.
    """
    if not isinstance(server, WorkMcpServer):
        msg = "server must be a WorkMcpServer"
        raise TypeError(msg)
    if not http:
        server.server.run(transport="stdio")
        return
    if isinstance(port, bool) or not isinstance(port, int) or not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"port must be an integer from {_MIN_PORT} to {_MAX_PORT}"
        raise ValueError(msg)
    server.server.run(transport="streamable-http", host=HTTP_HOST, port=port)


def load_factory(spec: str) -> WorkMcpServer:
    """Import ``module:callable`` and return the ``WorkMcpServer`` it builds."""
    match = _FACTORY_RE.fullmatch(spec)
    if match is None or ".." in spec or spec.split(":", 1)[0].endswith("."):
        msg = "factory must be 'module:callable', for example 'myapp.governed:server'"
        raise ValueError(msg)
    module_name, attr = match.group(1), match.group(2)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = f"cannot import factory module {module_name!r}"
        raise ImportError(msg) from exc
    try:
        factory = getattr(module, attr)
    except AttributeError as exc:
        msg = f"{spec} was not found"
        raise AttributeError(msg) from exc
    if not callable(factory):
        msg = f"{spec} is not callable"
        raise TypeError(msg)
    built = factory()
    if not isinstance(built, WorkMcpServer):
        msg = f"{spec} must return a WorkMcpServer"
        raise TypeError(msg)
    return built


def _owns(actor: TrustedActor, execution: Execution) -> bool:
    return actor.actor_id == execution.actor_id and actor.tenant_id == execution.tenant_id


def _execution_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if cursor is None:
        return None, None
    fields = decode_cursor(cursor, EXECUTION_CURSOR_FIELDS)
    created = fields["created_at"]
    execution_id = fields["execution_id"]
    try:
        parsed = datetime.fromisoformat(created)
        UUID(execution_id)
    except ValueError:
        raise McpCallError("cursor is invalid") from None
    if parsed.tzinfo is None:
        raise McpCallError("cursor is invalid")
    return created, execution_id


def _parse_status(status: str | None) -> ExecutionStatus | None:
    if status is None:
        return None
    try:
        return ExecutionStatus(status)
    except ValueError as exc:
        raise McpCallError(f"unknown status {status!r}") from exc


async def _as_tool(awaitable: Awaitable[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    try:
        return await awaitable
    except McpCallError as exc:
        raise ToolError(str(exc)) from exc


async def _as_resource(awaitable: Awaitable[str]) -> str:
    try:
        return await awaitable
    except McpCallError as exc:
        if not exc.found:
            raise ResourceNotFoundError(str(exc)) from exc
        raise ResourceError(str(exc)) from exc
