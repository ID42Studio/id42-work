"""MCP server that exposes governed execution. Requires the ``mcp`` extra."""

from id42_work.mcp._dependency import require_mcp

require_mcp()

from id42_work.mcp.server import McpCallError, WorkMcpServer, load_factory, serve  # noqa: E402

__all__ = ["McpCallError", "WorkMcpServer", "load_factory", "serve"]
