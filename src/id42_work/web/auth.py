"""Operator sessions, CSRF tokens and credential checks.

The raw operator token never enters this module. Callers pass an
``OperatorSecret`` and form fields. Nothing here is written to a log.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.exceptions import UntrustedActorError
from id42_work.web.config import OperatorSecret

SESSION_COOKIE = "id42_session"
CSRF_COOKIE = "id42_csrf"
CSRF_FIELD = "csrf"

AUTH_FAILED = "authentication failed"
_MAX_ACTOR_ID = 256
_FORBIDDEN_ACTOR_CHARS = "\r\n\x00"


class LoginError(Exception):
    """Authentication failed. The message is fixed and has no token or credential."""

    def __init__(self) -> None:
        super().__init__(AUTH_FAILED)


@dataclass(slots=True)
class Session:
    """One signed-in operator. ``csrf`` is the synchronizer token for POST forms."""

    actor_id: str
    csrf: str
    created: datetime
    flash: str | None = None
    flash_error: bool = False


class SessionStore:
    """In-memory sessions for one process. Restarting the UI drops them."""

    def __init__(self, *, ttl: timedelta, clock: Callable[[], datetime]) -> None:
        if not callable(clock):
            msg = "clock must be callable"
            raise TypeError(msg)
        if ttl <= timedelta(0):
            msg = "session ttl must be positive"
            raise ValueError(msg)
        self._ttl = ttl
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> timedelta:
        """How long a session stays valid after it is created."""
        return self._ttl

    def create(self, actor_id: str) -> str:
        """Store a new session and return its cookie value."""
        session_id = secrets.token_urlsafe(32)
        row = Session(
            actor_id=actor_id,
            csrf=secrets.token_urlsafe(32),
            created=self._now(),
        )
        with self._lock:
            self._sessions[session_id] = row
        return session_id

    def get(self, session_id: object) -> Session | None:
        """Return a live session, or ``None`` when the cookie is missing or expired."""
        if not isinstance(session_id, str) or session_id == "":
            return None
        with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return None
            if self._now() >= row.created + self._ttl:
                del self._sessions[session_id]
                return None
            return row

    def pop(self, session_id: object) -> None:
        """Drop a session. A missing id is ignored."""
        if not isinstance(session_id, str) or session_id == "":
            return
        with self._lock:
            self._sessions.pop(session_id, None)

    def set_flash(self, session_id: object, message: str, *, error: bool) -> None:
        """Attach a one-time message. A dead session keeps the message unstored."""
        if not isinstance(session_id, str):
            return
        with self._lock:
            row = self._sessions.get(session_id)
            if row is None:
                return
            row.flash = message
            row.flash_error = error

    def take_flash(self, session_id: object) -> tuple[str | None, bool]:
        """Return and clear the one-time message."""
        if not isinstance(session_id, str):
            return None, False
        with self._lock:
            row = self._sessions.get(session_id)
            if row is None or row.flash is None:
                return None, False
            message, error = row.flash, row.flash_error
            row.flash = None
            row.flash_error = False
            return message, error

    def _now(self) -> datetime:
        return self._clock()


def new_csrf() -> str:
    """Random synchronizer token for the anonymous login form."""
    return secrets.token_urlsafe(32)


def csrf_matches(provided: object, expected: object) -> bool:
    """Constant-time comparison of two CSRF tokens. Empty values never match."""
    if not isinstance(provided, str) or not isinstance(expected, str):
        return False
    if provided == "" or expected == "":
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def authenticate(
    *,
    token: OperatorSecret | None,
    credential_auth: bool,
    submitted_token: str,
    actor_id: str,
    credential: str,
    authority: ActorAuthority,
) -> str:
    """Return the operator actor id, or raise ``LoginError``.

    A configured token is never skipped. ``--credential-auth`` additionally
    accepts a ``TrustedActor`` credential that this authority verifies. The
    credential does not replace the token when a token is configured: both must
    succeed, and the session actor is the credential's actor. Without a
    credential, the token and a typed actor id are enough.
    """
    submitted = submitted_token.strip()
    typed = actor_id.strip()
    presented = credential.strip()
    token_ok = token is not None and token.matches(submitted)
    if presented != "" and credential_auth:
        if token is not None and not token_ok:
            raise LoginError
        verified = _actor_from_credential(authority, presented)
        if typed not in {"", verified}:
            raise LoginError
        return verified
    if not token_ok or not _actor_ok(typed):
        raise LoginError
    return typed


def origin_allowed(origin: str | None, referer: str | None, netloc: str) -> bool:
    """Return whether ``Origin`` or, if absent, ``Referer`` matches ``netloc``."""
    candidate = origin if origin is not None else referer
    if candidate is None or netloc == "":
        return False
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or parts.netloc == "":
        return False
    return parts.netloc == netloc


def _actor_from_credential(authority: ActorAuthority, credential: str) -> str:
    try:
        parsed = TrustedActor.from_credential(credential)
        verified = authority.verify(parsed)
    except (ValueError, TypeError, UntrustedActorError):
        raise LoginError from None
    return verified.actor_id


def _actor_ok(actor_id: str) -> bool:
    if actor_id == "" or len(actor_id) > _MAX_ACTOR_ID:
        return False
    return not any(char in actor_id for char in _FORBIDDEN_ACTOR_CHARS)


def form_text(form: Mapping[str, object], name: str) -> str:
    """Return a form field when it is text. Uploads and missing fields are empty."""
    value = form.get(name)
    if not isinstance(value, str):
        return ""
    return value
