"""Durable HMAC keys for an ``ActorAuthority``.

The default authority still keeps its key in process memory. A host that must
verify the same seals after a restart passes a file path. The file holds the
current signing key and any retired keys that can still verify an unexpired
actor. It is mode ``0600``, replaced atomically, and never written into the
execution database: a reader of executions must not be able to seal actors.

Nothing in this module logs or prints key bytes. ``AuthoritySigningKey`` hides
them from ``repr``. Callers must not log ``secret``.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from abc import ABC, abstractmethod
from collections.abc import Generator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

from id42_work._time import to_utc
from id42_work.canonical import canonical_json
from id42_work.exceptions import AuthorityKeyError

AUTHORITY_KEY_ENV = "ID42_WORK_AUTHORITY_KEY_PATH"
"""Environment variable a host may read. ``ActorAuthority`` does not read it."""

_VERSION = 1
_SECRET_LENGTH = 32
_MAX_KEYS = 16
_MAX_BYTES = 65_536
_PRIVATE_MODE = 0o600
_OTHERS_WRITE = 0o002
_GROUP_OR_OTHER = 0o077
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_KEY_ID = re.compile(r"^[0-9a-f]{16}$")
_FIELDS = frozenset({"authority_id", "keys", "version"})
_KEY_FIELDS = frozenset({"created_at", "key_id", "retired_at", "secret"})


class AuthoritySigningKey:
    """One HMAC key. ``secret`` is the raw key and must not be logged."""

    __slots__ = ("created_at", "key_id", "retired_at", "secret")

    def __init__(
        self,
        *,
        key_id: str,
        secret: bytes,
        created_at: datetime,
        retired_at: datetime | None,
    ) -> None:
        self.key_id = key_id
        self.secret = secret
        self.created_at = created_at
        self.retired_at = retired_at

    def sign(self, payload: bytes) -> bytes:
        """HMAC-SHA256 of ``payload``."""
        return hmac.new(self.secret, payload, hashlib.sha256).digest()

    def __repr__(self) -> str:
        state = "current" if self.retired_at is None else "retired"
        return f"AuthoritySigningKey(key_id={self.key_id!r}, {state})"


class AuthorityKeyStore(ABC):
    """Loads and stores the signing keys of one authority."""

    @abstractmethod
    def load(self) -> tuple[AuthoritySigningKey, ...]:
        """Return the stored keys. Do not log the result."""
        raise NotImplementedError

    @abstractmethod
    def save(self, keys: tuple[AuthoritySigningKey, ...], *, exclusive: bool = False) -> None:
        """Persist ``keys``. ``exclusive`` fails when a file is already there."""
        raise NotImplementedError

    @abstractmethod
    def rotate(self, now: datetime, *, max_ttl: timedelta) -> tuple[AuthoritySigningKey, ...]:
        """Retire the current key, sign with a new one, and return the full set."""
        raise NotImplementedError


class FileAuthorityKeyStore(AuthorityKeyStore):
    """HMAC keys in one file the host names.

    The file mode is ``0600``. Writes create a temporary file in the same
    directory, sync it, and replace the destination. A lock file beside the
    key file serializes readers and writers on this machine. The directory
    must already exist and must not be writable by others. A symbolic link is
    refused. Retired keys stay until ``max_ttl`` has passed since retirement,
    so an actor they sealed can still be verified while it is allowed to live.
    """

    def __init__(self, path: str | os.PathLike[str], authority_id: str) -> None:
        if not authority_id:
            msg = "authority_id is required"
            raise ValueError(msg)
        self._path = Path(path)
        if self._path.name in {"", ".", ".."}:
            msg = "authority key path must name a file"
            raise ValueError(msg)
        self._authority_id = authority_id
        self._lock_path = Path(str(self._path) + ".lock")

    @property
    def path(self) -> Path:
        """File that holds the keys. The lock file is this path plus ``.lock``."""
        return self._path

    def load(self) -> tuple[AuthoritySigningKey, ...]:
        """Read the key file. A missing, loose, or foreign file is an error."""
        with self._locked():
            return self._load_unlocked()

    def save(self, keys: tuple[AuthoritySigningKey, ...], *, exclusive: bool = False) -> None:
        """Write ``keys``. ``exclusive`` is for the first create, not for rotation."""
        with self._locked():
            self._save_unlocked(keys, exclusive=exclusive)

    def rotate(self, now: datetime, *, max_ttl: timedelta) -> tuple[AuthoritySigningKey, ...]:
        """Replace the current key under the file lock and return every kept key."""
        with self._locked():
            current = self._load_unlocked()
            updated = _rotate(current, now, max_ttl=max_ttl)
            self._save_unlocked(updated, exclusive=False)
            return updated

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        self._ensure_parent()
        flags = os.O_RDWR | os.O_CREAT | _NOFOLLOW
        try:
            fd = os.open(self._lock_path, flags, _PRIVATE_MODE)
        except OSError as exc:
            raise AuthorityKeyError("cannot lock the authority key file", reason="io") from exc
        try:
            os.fchmod(fd, _PRIVATE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_parent(self) -> None:
        parent = self._path.parent
        if not parent.is_dir():
            raise AuthorityKeyError(
                f"authority key directory does not exist: {parent}",
                reason="missing_directory",
            )
        if parent.stat().st_mode & _OTHERS_WRITE:
            raise AuthorityKeyError(
                "authority key directory is writable by others",
                reason="permissions",
            )

    def _load_unlocked(self) -> tuple[AuthoritySigningKey, ...]:
        try:
            fd = os.open(self._path, os.O_RDONLY | _NOFOLLOW)
        except FileNotFoundError:
            raise AuthorityKeyError(
                f"authority key file does not exist: {self._path}",
                reason="missing",
            ) from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AuthorityKeyError(
                    "authority key file must not be a symbolic link",
                    reason="symlink",
                ) from None
            raise AuthorityKeyError("cannot read the authority key file", reason="io") from exc
        try:
            return _read_fd(fd, self._authority_id)
        finally:
            os.close(fd)

    def _save_unlocked(self, keys: tuple[AuthoritySigningKey, ...], *, exclusive: bool) -> None:
        payload = _dump(self._authority_id, keys)
        try:
            if exclusive:
                self._write_exclusive(payload)
            else:
                self._write_replace(payload)
        except AuthorityKeyError:
            raise
        except OSError as exc:
            raise AuthorityKeyError("cannot write the authority key file", reason="io") from exc

    def _write_exclusive(self, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        try:
            fd = os.open(self._path, flags, _PRIVATE_MODE)
        except FileExistsError:
            raise AuthorityKeyError("authority key file already exists", reason="exists") from None
        except OSError as exc:
            raise AuthorityKeyError("cannot write the authority key file", reason="io") from exc
        try:
            _write_fd(fd, payload)
        except OSError:
            os.close(fd)
            _discard(self._path)
            raise
        os.close(fd)

    def _write_replace(self, payload: bytes) -> None:
        tmp = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
        fd = os.open(tmp, flags, _PRIVATE_MODE)
        try:
            _write_fd(fd, payload)
        except OSError:
            os.close(fd)
            _discard(tmp)
            raise
        os.close(fd)
        try:
            os.replace(tmp, self._path)
            _fsync_dir(self._path.parent)
        except OSError:
            _discard(tmp)
            raise


def authority_key_path_from_env(environ: Mapping[str, str] | None = None) -> Path | None:
    """Return the absolute path named by ``ID42_WORK_AUTHORITY_KEY_PATH``.

    An unset or blank value returns ``None``. A relative path is an error.
    ``ActorAuthority`` does not call this; the host passes the result as
    ``key_path`` when it wants the seals to survive a restart.
    """
    source = os.environ if environ is None else environ
    raw = source.get(AUTHORITY_KEY_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise AuthorityKeyError(
            f"{AUTHORITY_KEY_ENV} must be an absolute path",
            reason="relative",
        )
    return path


def new_signing_key(now: datetime) -> AuthoritySigningKey:
    return AuthoritySigningKey(
        key_id=secrets.token_hex(8),
        secret=secrets.token_bytes(_SECRET_LENGTH),
        created_at=to_utc(now),
        retired_at=None,
    )


def current_signing_key(keys: tuple[AuthoritySigningKey, ...]) -> AuthoritySigningKey:
    current = [key for key in keys if key.retired_at is None]
    if len(current) != 1:
        _corrupt("authority key file is not valid")
    return current[0]


def _rotate(
    keys: tuple[AuthoritySigningKey, ...], now: datetime, *, max_ttl: timedelta
) -> tuple[AuthoritySigningKey, ...]:
    moment = to_utc(now)
    current = current_signing_key(keys)
    retired = AuthoritySigningKey(
        key_id=current.key_id,
        secret=current.secret,
        created_at=current.created_at,
        retired_at=moment,
    )
    kept: list[AuthoritySigningKey] = [new_signing_key(moment), retired]
    for key in keys:
        if key.key_id == current.key_id:
            continue
        retired_at = cast(datetime, key.retired_at)
        if moment >= retired_at + max_ttl:
            continue
        kept.append(key)
    if len(kept) > _MAX_KEYS:
        raise AuthorityKeyError(
            "too many authority keys are still inside the verification window",
            reason="limit",
        )
    return tuple(kept)


def _read_fd(fd: int, authority_id: str) -> tuple[AuthoritySigningKey, ...]:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise AuthorityKeyError("authority key file must be a regular file", reason="permissions")
    if info.st_mode & _GROUP_OR_OTHER:
        raise AuthorityKeyError("authority key file permissions are too open", reason="permissions")
    if info.st_size == 0 or info.st_size > _MAX_BYTES:
        _corrupt("authority key file is not valid")
    payload = os.read(fd, info.st_size)
    if len(payload) != info.st_size:
        _corrupt("authority key file is not valid")
    return _parse(payload, authority_id)


def _parse(payload: bytes, authority_id: str) -> tuple[AuthoritySigningKey, ...]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if not text:
        _corrupt("authority key file is not valid")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        _corrupt("authority key file is not valid")
    body = cast("dict[str, object]", parsed)
    if frozenset(body) != _FIELDS:
        _corrupt("authority key file is not valid")
    _check_version(body.get("version"))
    _check_authority(body.get("authority_id"), authority_id)
    return _parse_keys(body.get("keys"))


def _check_version(version: object) -> None:
    if isinstance(version, bool) or version != _VERSION:
        _corrupt("authority key file version is not supported")


def _check_authority(found: object, expected: str) -> None:
    if not isinstance(found, str):
        _corrupt("authority key file is not valid")
    if found != expected:
        raise AuthorityKeyError(
            "authority key file belongs to a different authority",
            reason="mismatch",
        )


def _parse_keys(raw: object) -> tuple[AuthoritySigningKey, ...]:
    if not isinstance(raw, list):
        _corrupt("authority key file is not valid")
    items = cast("list[object]", raw)
    if not items or len(items) > _MAX_KEYS:
        _corrupt("authority key file is not valid")
    keys = tuple(_parse_key(item) for item in items)
    if len({key.key_id for key in keys}) != len(keys):
        _corrupt("authority key file is not valid")
    current_signing_key(keys)
    return keys


def _parse_key(raw: object) -> AuthoritySigningKey:
    if not isinstance(raw, dict):
        _corrupt("authority key file is not valid")
    item = cast("dict[str, object]", raw)
    if frozenset(item) != _KEY_FIELDS:
        _corrupt("authority key file is not valid")
    key_id = item["key_id"]
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        _corrupt("authority key file is not valid")
    secret = _parse_secret(item["secret"])
    created_at = _parse_time(item["created_at"])
    retired_at = _parse_retired(item["retired_at"])
    if retired_at is not None and retired_at < created_at:
        _corrupt("authority key file is not valid")
    return AuthoritySigningKey(
        key_id=key_id,
        secret=secret,
        created_at=created_at,
        retired_at=retired_at,
    )


def _parse_secret(raw: object) -> bytes:
    secret = b""
    if isinstance(raw, str):
        try:
            secret = base64.b64decode(raw, validate=True)
        except ValueError:
            secret = b""
    if len(secret) != _SECRET_LENGTH:
        _corrupt("authority key file is not valid")
    return secret


def _parse_retired(raw: object) -> datetime | None:
    if raw is None:
        return None
    return _parse_time(raw)


def _parse_time(raw: object) -> datetime:
    parsed: datetime | None = None
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
    if parsed is None:
        _corrupt("authority key file is not valid")
    try:
        return to_utc(parsed)
    except ValueError:
        _corrupt("authority key file is not valid")


def _dump(authority_id: str, keys: tuple[AuthoritySigningKey, ...]) -> bytes:
    ordered = _ordered(keys)
    body = {
        "authority_id": authority_id,
        "keys": [_key_body(key) for key in ordered],
        "version": _VERSION,
    }
    return canonical_json(body) + b"\n"


def _ordered(keys: tuple[AuthoritySigningKey, ...]) -> tuple[AuthoritySigningKey, ...]:
    current = current_signing_key(keys)
    retired = [key for key in keys if key.retired_at is not None]
    retired.sort(key=lambda key: cast(datetime, key.retired_at), reverse=True)
    return (current, *retired)


def _key_body(key: AuthoritySigningKey) -> dict[str, object]:
    return {
        "created_at": key.created_at.isoformat(),
        "key_id": key.key_id,
        "retired_at": None if key.retired_at is None else key.retired_at.isoformat(),
        "secret": base64.b64encode(key.secret).decode("ascii"),
    }


def _write_fd(fd: int, payload: bytes) -> None:
    os.fchmod(fd, _PRIVATE_MODE)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "authority key write failed")
        view = view[written:]
    os.fsync(fd)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _corrupt(message: str) -> NoReturn:
    raise AuthorityKeyError(message, reason="corrupt")
