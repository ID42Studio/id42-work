"""Optional operator Web UI for approvals and reconciliation.

Importing this package does not import Starlette. :func:`create_app` and
:func:`serve_web` require the ``web`` extra.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    """Load the ASGI app or the server after checking the extra."""
    if name == "create_app":
        from id42_work.web._dependency import require_web  # noqa: PLC0415
        from id42_work.web.app import create_app  # noqa: PLC0415

        require_web()
        return create_app
    if name == "serve_web":
        from id42_work.web._dependency import require_web  # noqa: PLC0415
        from id42_work.web.serve import serve_web  # noqa: PLC0415

        require_web()
        return serve_web
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
