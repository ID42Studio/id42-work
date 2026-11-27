"""MCP server that exposes governed execution. Requires the ``mcp`` extra."""

from id42_work.mcp._dependency import require_mcp

require_mcp()

from id42_work.mcp.errors import McpCallError  # noqa: E402
from id42_work.mcp.pages import DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT  # noqa: E402
from id42_work.mcp.server import WorkMcpServer, load_factory, serve  # noqa: E402
from id42_work.mcp.session import (  # noqa: E402
    ACTOR_CREDENTIAL_HEADER,
    McpCaller,
    bind_mcp_caller,
    credential_session_actor,
)

__all__ = [
    "ACTOR_CREDENTIAL_HEADER",
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "McpCallError",
    "McpCaller",
    "WorkMcpServer",
    "bind_mcp_caller",
    "credential_session_actor",
    "load_factory",
    "serve",
]
