"""Per-request actor for the MCP server.

The default server never reads this. A host that passes ``session_actor`` receives
an :class:`McpCaller` for the current request. HTTP carries headers. Stdio does not,
so a stdio caller stays the single factory actor.

The header is not an identity. :func:`credential_session_actor` only parses
``TrustedActor.to_credential``. ``WorkMcpServer`` still runs ``ActorAuthority.verify``.
"""

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext

from id42_work.actors import TrustedActor
from id42_work.mcp.errors import McpCallError

ACTOR_CREDENTIAL_HEADER = "id42-actor-credential"
SESSION_HEADER = "mcp-session-id"

_MAX_CREDENTIAL = 16_384
_SESSION_RE = re.compile(r"[\x21-\x7E]{1,128}")

Transport = Literal["stdio", "http", "direct"]


class McpCaller:
    """What this request can present. Trust is decided later, by ``verify``."""

    __slots__ = ("credential", "session_id", "transport")

    def __init__(
        self,
        *,
        transport: Transport,
        session_id: str | None = None,
        credential: str | None = None,
    ) -> None:
        if transport not in ("stdio", "http", "direct"):
            msg = "transport must be stdio, http or direct"
            raise ValueError(msg)
        if session_id is not None and _SESSION_RE.fullmatch(session_id) is None:
            msg = "session_id is invalid"
            raise ValueError(msg)
        if credential is not None and not _credential_ok(credential):
            msg = "credential is invalid"
            raise ValueError(msg)
        self.transport: Transport = transport
        self.session_id = session_id
        self.credential = credential


_BOUND: ContextVar[McpCaller | None] = ContextVar("id42_mcp_caller", default=None)


def _credential_ok(value: str) -> bool:
    if not value or len(value) > _MAX_CREDENTIAL:
        return False
    return all(character not in value for character in "\r\n\x00")


def current_caller() -> McpCaller:
    """Caller bound to this task, or a direct in-process call when nothing is bound."""
    bound = _BOUND.get()
    if bound is not None:
        return bound
    return McpCaller(transport="direct")


@contextmanager
def bind_mcp_caller(caller: object) -> Generator[None]:
    """Bind ``caller`` for the current task. Tests and embeddings use this."""
    if not isinstance(caller, McpCaller):
        msg = "caller must be an McpCaller"
        raise TypeError(msg)
    token = _BOUND.set(caller)
    try:
        yield
    finally:
        _BOUND.reset(token)


def credential_session_actor(
    *,
    fallback: Callable[[], TrustedActor] | None = None,
) -> Callable[[McpCaller], TrustedActor]:
    """Resolve an actor from ``Id42-Actor-Credential``.

    On HTTP the header is required. A missing header is not replaced with the
    factory actor, so a neighbor on loopback cannot skip the credential. Stdio
    and a direct in-process call have no header; ``fallback`` (usually
    ``issue_actor``) supplies the single actor. The server verifies the result.
    """

    def resolve(caller: object) -> TrustedActor:
        if not isinstance(caller, McpCaller):
            msg = "caller must be an McpCaller"
            raise McpCallError(msg)
        if caller.credential is None:
            if caller.transport == "http" or fallback is None:
                raise McpCallError("an actor credential is required")
            try:
                return fallback()
            except McpCallError:
                raise
            except Exception as exc:
                raise McpCallError("the host could not issue an actor") from exc
        try:
            return TrustedActor.from_credential(caller.credential)
        except ValueError as exc:
            raise McpCallError("the actor credential was refused") from exc

    return resolve


def caller_from_request(request: object | None) -> McpCaller:
    """Build a caller from the transport request. ``None`` is stdio."""
    if request is None:
        return McpCaller(transport="stdio")
    headers = getattr(request, "headers", None)
    return McpCaller(
        transport="http",
        session_id=_session_header(headers),
        credential=_credential_header(headers),
    )


async def bind_request_actor(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    """Middleware: bind the caller for this message, then continue."""
    token = _BOUND.set(caller_from_request(ctx.request))
    try:
        return await call_next(ctx)
    finally:
        _BOUND.reset(token)


def _header(headers: object, name: str) -> str | None:
    get = getattr(headers, "get", None)
    if not callable(get):
        return None
    value = get(name)
    if value is None:
        value = get(name.lower())
    if isinstance(value, str):
        return value
    return None


def _credential_header(headers: object) -> str | None:
    value = _header(headers, ACTOR_CREDENTIAL_HEADER)
    if value is None:
        return None
    text = value.strip()
    if not _credential_ok(text):
        return "invalid"
    return text


def _session_header(headers: object) -> str | None:
    value = _header(headers, SESSION_HEADER)
    if value is None:
        return None
    text = value.strip()
    if _SESSION_RE.fullmatch(text) is None:
        return None
    return text
