"""Import guard for the optional MCP extra."""

import importlib

_MISSING = "MCP support is not installed. Install it with: pip install 'id42-work[mcp]'"


def require_mcp() -> None:
    """Raise ``ImportError`` when the official MCP SDK is not installed."""
    try:
        importlib.import_module("mcp")
    except ImportError as exc:
        raise ImportError(_MISSING) from exc
