"""Import guard for the optional Web UI extra."""

from __future__ import annotations

import importlib

_MODULES = ("starlette", "jinja2", "uvicorn")
_MISSING = "Web UI support is not installed. Install it with: pip install 'id42-work[web]'"


def require_web() -> None:
    """Raise ``ImportError`` when the Web UI dependencies are not installed."""
    for name in _MODULES:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            raise ImportError(_MISSING) from exc
