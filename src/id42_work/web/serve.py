"""Run the operator UI on a loopback address."""

from __future__ import annotations

import os
from collections.abc import Mapping

from id42_work.web.app import create_app
from id42_work.web.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    load_operator_secret,
    require_loopback,
    require_port,
)

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "serve_web"]


def serve_web(
    server: object,
    *,
    host: object = DEFAULT_HOST,
    port: object = DEFAULT_PORT,
    token_file: object = None,
    credential_auth: object = False,
    factory: object = "",
    environ: Mapping[str, str] | None = None,
) -> None:
    """Serve ``server`` until the process is interrupted.

    ``host`` must be a loopback IP. The default is ``127.0.0.1``. A token is
    read from ``token_file``, ``ID42_WORK_WEB_TOKEN_FILE`` or
    ``ID42_WORK_WEB_TOKEN``. Without a token, ``credential_auth`` must be set.
    Access logs are off, so a request line cannot record a form body.
    """
    if not isinstance(credential_auth, bool):
        msg = "credential auth must be a flag"
        raise TypeError(msg)
    if token_file is not None and not isinstance(token_file, str):
        msg = "token file must be a path"
        raise TypeError(msg)
    if not isinstance(factory, str):
        msg = "factory label must be a string"
        raise TypeError(msg)
    bind = require_loopback(host)
    number = require_port(port)
    secret = load_operator_secret(token_file, os.environ if environ is None else environ)
    app = create_app(server, token=secret, credential_auth=credential_auth, factory=factory)
    import uvicorn  # noqa: PLC0415

    uvicorn.run(
        app,
        host=bind,
        port=number,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_level="warning",
    )
