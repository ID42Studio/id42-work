"""Bind address, operator token and other settings that use the standard library only."""

from __future__ import annotations

import errno
import hashlib
import hmac
import ipaddress
import os
import stat
from collections.abc import Mapping
from pathlib import Path

TOKEN_ENV = "ID42_WORK_WEB_TOKEN"  # noqa: S105
"""Environment variable holding the operator token. The constructor does not read it."""

TOKEN_FILE_ENV = "ID42_WORK_WEB_TOKEN_FILE"  # noqa: S105
"""Environment variable naming a mode-``0600`` file that holds the operator token."""

DEFAULT_HOST = "127.0.0.1"
"""Web UI listens here unless ``--host`` names another loopback address."""

DEFAULT_PORT = 8767
"""Default TCP port. The MCP HTTP server uses 8766, so the two can run together."""

_MIN_PORT = 1
_MAX_PORT = 65535
_MAX_TOKEN_BYTES = 4096
_GROUP_OR_OTHER = 0o077
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class OperatorSecret:
    """SHA-256 of the operator token. The raw token is not retained.

    ``matches`` compares digests with ``hmac.compare_digest``, so the check does
    not stop early on the first differing byte of the token.
    """

    __slots__ = ("_digest",)

    def __init__(self, token: str) -> None:
        if token == "" or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            msg = "operator token is empty or too long"
            raise ValueError(msg)
        self._digest = hashlib.sha256(token.encode("utf-8")).digest()

    def matches(self, provided: object) -> bool:
        """Return whether ``provided`` is the configured token."""
        if not isinstance(provided, str):
            candidate = b""
        else:
            encoded = provided.encode("utf-8")
            candidate = b"" if len(encoded) > _MAX_TOKEN_BYTES else encoded
        digest = hashlib.sha256(candidate).digest()
        return hmac.compare_digest(digest, self._digest)

    def __repr__(self) -> str:
        return "OperatorSecret(redacted)"


def require_loopback(host: object) -> str:
    """Return ``host`` when it is a loopback IP. Names and other addresses fail."""
    if not isinstance(host, str):
        msg = "host must be a loopback IP address"
        raise ValueError(msg)
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError as exc:
        msg = "host must be a loopback IP address"
        raise ValueError(msg) from exc
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None or not parsed.is_loopback:
        msg = "host must be a loopback IP address"
        raise ValueError(msg)
    return host


def require_port(port: object) -> int:
    """Return ``port`` when it is an integer from 1 to 65535."""
    if isinstance(port, bool) or not isinstance(port, int) or not _MIN_PORT <= port <= _MAX_PORT:
        msg = f"port must be an integer from {_MIN_PORT} to {_MAX_PORT}"
        raise ValueError(msg)
    return port


def load_operator_secret(
    token_file: str | None,
    environ: Mapping[str, str],
) -> OperatorSecret | None:
    """Load the token from a file, then the file env, then ``ID42_WORK_WEB_TOKEN``.

    A configured empty value is an error. When none of the three is set, return
    ``None`` so the caller can require ``--credential-auth`` instead.
    """
    if token_file is not None:
        return OperatorSecret(_read_token_file(token_file))
    file_env = environ.get(TOKEN_FILE_ENV)
    if file_env is not None:
        if file_env.strip() == "":
            msg = "operator token file path is empty"
            raise ValueError(msg)
        return OperatorSecret(_read_token_file(file_env))
    if TOKEN_ENV not in environ:
        return None
    return OperatorSecret(_one_line(environ[TOKEN_ENV], "operator token"))


def _one_line(raw: str, label: str) -> str:
    token = raw.strip()
    if token == "" or "\n" in token or "\r" in token:
        msg = f"{label} must be a single non-empty line"
        raise ValueError(msg)
    return token


def _read_token_file(path: str) -> str:
    file_path = Path(path)
    if file_path.name in {"", ".", ".."}:
        msg = "operator token file must name a file"
        raise ValueError(msg)
    try:
        fd = os.open(file_path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        msg = "operator token file does not exist"
        raise ValueError(msg) from None
    except OSError as exc:
        msg = _open_error(exc)
        raise ValueError(msg) from None
    try:
        return _read_fd(fd)
    finally:
        os.close(fd)


def _open_error(exc: OSError) -> str:
    if exc.errno == errno.ELOOP:
        return "operator token file must not be a symbolic link"
    return "operator token file cannot be read"


def _read_fd(fd: int) -> str:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        msg = "operator token file must be a regular file"
        raise ValueError(msg)
    if info.st_mode & _GROUP_OR_OTHER:
        msg = "operator token file must not be readable by group or others"
        raise ValueError(msg)
    if info.st_size > _MAX_TOKEN_BYTES:
        msg = "operator token file is too large"
        raise ValueError(msg)
    data = os.read(fd, _MAX_TOKEN_BYTES + 1)
    if len(data) > _MAX_TOKEN_BYTES:
        msg = "operator token file is too large"
        raise ValueError(msg)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        msg = "operator token file must be utf-8"
        raise ValueError(msg) from None
    return _one_line(text, "operator token file")
