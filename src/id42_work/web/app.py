"""ASGI application. Every decision goes through the approval or reconciliation console."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote
from uuid import UUID

from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from id42_work.actors import ActorAuthority
from id42_work.approvals.commands import format_show as format_approval
from id42_work.approvals.console import (
    ApprovalDecision,
    ApprovalResumeError,
    ApprovalSnapshot,
)
from id42_work.approvals.console import (
    console_from_server as approval_console_from_server,
)
from id42_work.approvals.models import ApprovalStatus
from id42_work.exceptions import WorkError
from id42_work.identifiers import ACTION_ID_PATTERN
from id42_work.journal import JournalStatus
from id42_work.reconciliation.commands import format_show as format_call
from id42_work.reconciliation.console import (
    ReconciliationDecision,
    ReconciliationResumeError,
    ReconciliationSnapshot,
)
from id42_work.reconciliation.console import (
    console_from_server as reconcile_console_from_server,
)
from id42_work.web.auth import (
    AUTH_FAILED,
    CSRF_COOKIE,
    CSRF_FIELD,
    SESSION_COOKIE,
    LoginError,
    Session,
    SessionStore,
    authenticate,
    csrf_matches,
    form_text,
    new_csrf,
    origin_allowed,
)
from id42_work.web.config import OperatorSecret
from id42_work.web.render import render

_LOG = logging.getLogger("id42_work.web")
_DEFAULT_SESSION_TTL = timedelta(hours=8)
_MIN_SESSION_TTL = timedelta(seconds=1)
_CSRF_MAX_AGE = 3600
_MAX_BODY_BYTES = 262144
_ACTION_ID = re.compile(ACTION_ID_PATTERN)
_SEE_OTHER = 303
_BAD_REQUEST = 400
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404
_TOO_LARGE = 413
_SERVER_ERROR = 500
_FIELDS = ("csrf", "token", "actor_id", "credential", "reason", "result", "resume")

_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cache-control", "no-store"),
    (
        "content-security-policy",
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    ),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
)

_AUTH_MISSING = (
    "operator authentication is not configured: "
    "set ID42_WORK_WEB_TOKEN or --token-file, or pass --credential-auth"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SecurityHeaders:
    """Add the response headers and log the method, path and status only."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = cast("str", scope.get("method", "-"))
        path = cast("str", scope.get("path", "-"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                _stamp(message, method, path)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _stamp(message: Message, method: str, path: str) -> None:
    raw = message.get("headers", [])
    headers = MutableHeaders(raw=list(raw))
    for name, value in _SECURITY_HEADERS:
        headers[name] = value
    if "server" in headers:
        del headers["server"]
    message["headers"] = headers.raw
    _LOG.info("%s %s %s", method, path, message["status"])


class OperatorSite:
    """HTTP handlers. State changes call ``ApprovalConsole`` or ``ReconciliationConsole``."""

    def __init__(
        self,
        server: object,
        *,
        token: OperatorSecret | None,
        credential_auth: bool,
        sessions: SessionStore,
    ) -> None:
        self._approvals = approval_console_from_server(server)
        self._reconcile = reconcile_console_from_server(server)
        self._authority = cast(ActorAuthority, getattr(server, "authority", None))
        self._token = token
        self._credential_auth = credential_auth
        self._sessions = sessions

    def routes(self) -> list[Route]:
        """Routes for one Starlette app. Nothing here is an MCP tool."""
        return [
            Route("/login", self.login, methods=["GET", "POST"]),
            Route("/logout", self.logout, methods=["POST"]),
            Route("/", self.home, methods=["GET"]),
            Route("/approvals/{approval_id}", self.approval, methods=["GET"]),
            Route("/approvals/{approval_id}/approve", self.approve, methods=["POST"]),
            Route("/approvals/{approval_id}/reject", self.reject, methods=["POST"]),
            Route("/reconcile/{execution_id}/{action_id}", self.reconcile, methods=["GET"]),
            Route(
                "/reconcile/{execution_id}/{action_id}/applied",
                self.applied,
                methods=["POST"],
            ),
            Route(
                "/reconcile/{execution_id}/{action_id}/not-applied",
                self.not_applied,
                methods=["POST"],
            ),
            Route(
                "/reconcile/{execution_id}/{action_id}/abandon",
                self.abandon,
                methods=["POST"],
            ),
        ]

    async def login(self, request: Request) -> Response:
        """Show the sign-in form, or accept a token or a verified credential."""
        if request.method == "GET":
            return self._login_get(request)
        return await self._login_post(request)

    def _login_get(self, request: Request) -> Response:
        if self._live(request) is not None:
            return _redirect("/")
        return self._login_form(None)

    async def _login_post(self, request: Request) -> Response:
        blocked = _too_large(request)
        if blocked is not None:
            return blocked
        if not _same_origin(request):
            return _forbidden()
        fields = await _read_form(request)
        if isinstance(fields, Response):
            return fields
        if not csrf_matches(fields[CSRF_FIELD], request.cookies.get(CSRF_COOKIE)):
            return _forbidden()
        try:
            actor_id = authenticate(
                token=self._token,
                credential_auth=self._credential_auth,
                submitted_token=fields["token"],
                actor_id=fields["actor_id"],
                credential=fields["credential"],
                authority=self._authority,
            )
        except LoginError:
            return self._login_form(AUTH_FAILED)
        return self._start_session(actor_id)

    async def logout(self, request: Request) -> Response:
        """Drop the session and clear its cookie."""
        opened = await self._open_post(request)
        if isinstance(opened, Response):
            return opened
        session_id, _session, _fields = opened
        self._sessions.pop(session_id)
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def home(self, request: Request) -> Response:
        """List pending approvals and uncertain calls."""
        opened = self._open_get(request)
        if isinstance(opened, Response):
            return opened
        session_id, session = opened
        flash, flash_error = self._sessions.take_flash(session_id)
        try:
            approvals = await self._approvals.list_pending()
            calls = await self._reconcile.list_open()
        except WorkError as exc:
            return _message(str(exc), status=_BAD_REQUEST, session=session, flash=flash)
        except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
            return _unexpected(exc)
        body = render(
            "home.html",
            title="Decisions",
            actor_id=session.actor_id,
            csrf=session.csrf,
            flash=flash,
            flash_error=flash_error,
            approvals=[_approval_row(row) for row in approvals],
            calls=[_call_row(row) for row in calls],
        )
        return HTMLResponse(body)

    async def approval(self, request: Request) -> Response:
        """Show one approval, using the same JSON as ``id42-work approvals show``."""
        opened = self._open_get(request)
        if isinstance(opened, Response):
            return opened
        session_id, session = opened
        flash, flash_error = self._sessions.take_flash(session_id)
        try:
            approval_id = _uuid(request.path_params["approval_id"], "approval id")
        except ValueError as exc:
            return _message(
                str(exc), status=_BAD_REQUEST, session=session, flash=flash, flash_error=flash_error
            )
        try:
            snapshot = await self._approvals.show(approval_id)
        except WorkError as exc:
            return _message(
                str(exc), status=_NOT_FOUND, session=session, flash=flash, flash_error=flash_error
            )
        except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
            return _unexpected(exc)
        pending = snapshot.approval.status is ApprovalStatus.PENDING
        body = render(
            "approval.html",
            title="Approval",
            actor_id=session.actor_id,
            csrf=session.csrf,
            flash=flash,
            flash_error=flash_error,
            record_json=format_approval(snapshot),
            approval_id=str(approval_id),
            pending=pending,
        )
        return HTMLResponse(body)

    async def reconcile(self, request: Request) -> Response:
        """Show one call, using the same JSON as ``id42-work reconcile show``."""
        opened = self._open_get(request)
        if isinstance(opened, Response):
            return opened
        session_id, session = opened
        flash, flash_error = self._sessions.take_flash(session_id)
        try:
            execution_id, action_id = _call_target(request)
        except ValueError as exc:
            return _message(
                str(exc), status=_BAD_REQUEST, session=session, flash=flash, flash_error=flash_error
            )
        try:
            snapshot = await self._reconcile.show(execution_id, action_id)
        except WorkError as exc:
            return _message(
                str(exc), status=_NOT_FOUND, session=session, flash=flash, flash_error=flash_error
            )
        except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
            return _unexpected(exc)
        body = render(
            "reconcile.html",
            title="Uncertain call",
            actor_id=session.actor_id,
            csrf=session.csrf,
            flash=flash,
            flash_error=flash_error,
            record_json=format_call(snapshot),
            action=_call_href(execution_id, action_id),
            open_call=snapshot.entry.status is JournalStatus.UNCERTAIN,
        )
        return HTMLResponse(body)

    async def approve(self, request: Request) -> Response:
        """Grant the approval as the signed-in operator."""
        return await self._approval_post(request, granted=True)

    async def reject(self, request: Request) -> Response:
        """Reject the approval. The execution ends ``REJECTED``."""
        return await self._approval_post(request, granted=False)

    async def applied(self, request: Request) -> Response:
        """Record that the effect happened."""
        return await self._reconcile_post(request, "applied")

    async def not_applied(self, request: Request) -> Response:
        """Record that the effect did not happen."""
        return await self._reconcile_post(request, "not-applied")

    async def abandon(self, request: Request) -> Response:
        """Close the uncertainty. The execution stays failed."""
        return await self._reconcile_post(request, "abandon")

    async def _approval_post(self, request: Request, *, granted: bool) -> Response:
        opened = await self._open_post(request)
        if isinstance(opened, Response):
            return opened
        session_id, session, fields = opened
        try:
            approval_id = _uuid(request.path_params["approval_id"], "approval id")
        except ValueError as exc:
            return _message(str(exc), status=_BAD_REQUEST, session=session)
        target = f"/approvals/{approval_id}"
        try:
            decision = await self._decide_approval(granted, approval_id, session, fields)
        except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
            return self._failed(exc, session_id, session, target)
        self._note(session_id, _approval_notice(granted, decision), error=False)
        return _redirect(target)

    async def _decide_approval(
        self,
        granted: bool,
        approval_id: UUID,
        session: Session,
        fields: Mapping[str, str],
    ) -> ApprovalDecision:
        reason = _optional_reason(fields["reason"])
        if granted:
            return await self._approvals.approve(
                approval_id,
                actor_id=session.actor_id,
                reason=reason,
                resume=fields["resume"] == "on",
            )
        if reason is None:
            msg = "a reason is required"
            raise WorkError(msg)
        return await self._approvals.reject(
            approval_id,
            actor_id=session.actor_id,
            reason=reason,
        )

    async def _reconcile_post(self, request: Request, kind: str) -> Response:
        opened = await self._open_post(request)
        if isinstance(opened, Response):
            return opened
        session_id, session, fields = opened
        try:
            execution_id, action_id = _call_target(request)
        except ValueError as exc:
            return _message(str(exc), status=_BAD_REQUEST, session=session)
        target = _call_href(execution_id, action_id)
        try:
            decision = await self._decide_call(kind, execution_id, action_id, session, fields)
        except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
            return self._failed(exc, session_id, session, target)
        status = decision.execution.status.value
        resumed = "Resume finished." if decision.resumed else "It was not resumed."
        self._note(session_id, f"Recorded {kind}. Execution is {status}. {resumed}", error=False)
        return _redirect(target)

    def _failed(
        self,
        exc: BaseException,
        session_id: str,
        session: Session,
        target: str,
    ) -> Response:
        if isinstance(exc, (ApprovalResumeError, ReconciliationResumeError)):
            self._note(session_id, "The decision was stored. Resume did not finish.", error=True)
            return _redirect(target)
        if isinstance(exc, WorkError):
            self._note(session_id, str(exc), error=True)
            return _redirect(target)
        if isinstance(exc, ValueError):
            return _message(str(exc), status=_BAD_REQUEST, session=session)
        return _unexpected(exc)

    async def _decide_call(
        self,
        kind: str,
        execution_id: UUID,
        action_id: str,
        session: Session,
        fields: Mapping[str, str],
    ) -> ReconciliationDecision:
        reason = _optional_reason(fields["reason"])
        if reason is None:
            msg = "a reason is required"
            raise ValueError(msg)
        resume = fields["resume"] == "on"
        if kind == "applied":
            return await self._reconcile.mark_applied(
                execution_id,
                action_id,
                actor_id=session.actor_id,
                reason=reason,
                result=_json_result(fields["result"]),
                resume=resume,
            )
        if kind == "not-applied":
            return await self._reconcile.mark_not_applied(
                execution_id,
                action_id,
                actor_id=session.actor_id,
                reason=reason,
                resume=resume,
            )
        return await self._reconcile.abandon(
            execution_id,
            action_id,
            actor_id=session.actor_id,
            reason=reason,
        )

    def _open_get(self, request: Request) -> Response | tuple[str, Session]:
        live = self._live(request)
        if live is None:
            return _redirect("/login")
        return live

    async def _open_post(
        self, request: Request
    ) -> Response | tuple[str, Session, Mapping[str, str]]:
        blocked = _too_large(request)
        if blocked is not None:
            return blocked
        if not _same_origin(request):
            return _forbidden()
        live = self._live(request)
        if live is None:
            return _unauthorized()
        fields = await _read_form(request)
        if isinstance(fields, Response):
            return fields
        session_id, session = live
        if not csrf_matches(fields[CSRF_FIELD], session.csrf):
            return _forbidden()
        return session_id, session, fields

    def _live(self, request: Request) -> tuple[str, Session] | None:
        raw = request.cookies.get(SESSION_COOKIE)
        row = self._sessions.get(raw)
        if row is None or not isinstance(raw, str):
            return None
        return raw, row

    def _login_form(self, flash: str | None) -> Response:
        csrf = new_csrf()
        body = render(
            "login.html",
            title="Sign in",
            csrf=csrf,
            actor_id=None,
            flash=flash,
            flash_error=flash is not None,
            token_auth=self._token is not None,
            credential_auth=self._credential_auth,
        )
        response = HTMLResponse(body, status_code=_UNAUTHORIZED if flash else 200)
        _set_cookie(response, CSRF_COOKIE, csrf, _CSRF_MAX_AGE)
        return response

    def _start_session(self, actor_id: str) -> Response:
        session_id = self._sessions.create(actor_id)
        response = _redirect("/")
        _set_cookie(response, SESSION_COOKIE, session_id, int(self._sessions.ttl.total_seconds()))
        response.delete_cookie(CSRF_COOKIE, path="/")
        return response

    def _note(self, session_id: str, message: str, *, error: bool) -> None:
        self._sessions.set_flash(session_id, message, error=error)


def create_app(
    server: object,
    *,
    token: object = None,
    credential_auth: object = False,
    clock: Callable[[], datetime] | None = None,
    session_ttl: timedelta = _DEFAULT_SESSION_TTL,
) -> ASGIApp:
    """Build the operator UI. ``server`` is the same factory object ``mcp serve`` loads.

    The app calls ``ApprovalConsole`` and ``ReconciliationConsole`` only. Pass an
    ``OperatorSecret`` from :func:`id42_work.web.config.load_operator_secret`, or
    set ``credential_auth`` so a verified actor credential can sign in.
    """
    if not isinstance(credential_auth, bool):
        msg = "credential auth must be a flag"
        raise TypeError(msg)
    if token is not None and not isinstance(token, OperatorSecret):
        msg = "token must be an OperatorSecret"
        raise TypeError(msg)
    if token is None and not credential_auth:
        raise ValueError(_AUTH_MISSING)
    if session_ttl < _MIN_SESSION_TTL:
        msg = "session ttl must be at least one second"
        raise ValueError(msg)
    site = OperatorSite(
        server,
        token=token,
        credential_auth=credential_auth,
        sessions=SessionStore(ttl=session_ttl, clock=_utc_now if clock is None else clock),
    )
    return SecurityHeaders(Starlette(debug=False, routes=site.routes()))


def _approval_notice(granted: bool, decision: ApprovalDecision) -> str:
    resumed = "Resume finished." if decision.resumed else "It was not resumed."
    verb = "granted" if granted else "rejected"
    return f"Approval {verb}. Execution is {decision.execution.status.value}. {resumed}"


def _approval_row(row: ApprovalSnapshot) -> dict[str, str]:
    capability = row.approval.capability if row.approval.capability is not None else "-"
    return {
        "approval_id": str(row.approval.approval_id),
        "work_id": row.execution.work_id,
        "gate": row.approval.gate.value,
        "capability": capability,
        "execution_status": row.execution.status.value,
    }


def _call_row(row: ReconciliationSnapshot) -> dict[str, str]:
    return {
        "href": _call_href(row.execution.execution_id, row.entry.action_id),
        "work_id": row.execution.work_id,
        "action_id": row.entry.action_id,
        "capability": row.entry.capability,
        "execution_status": row.execution.status.value,
    }


def _call_href(execution_id: UUID, action_id: str) -> str:
    return "/reconcile/" + quote(str(execution_id), safe="") + "/" + quote(action_id, safe="")


def _call_target(request: Request) -> tuple[UUID, str]:
    execution_id = _uuid(request.path_params["execution_id"], "execution id")
    action_id = request.path_params["action_id"]
    if not isinstance(action_id, str) or _ACTION_ID.fullmatch(action_id) is None:
        msg = "action id is invalid"
        raise ValueError(msg)
    return execution_id, action_id


def _uuid(raw: object, label: str) -> UUID:
    if not isinstance(raw, str):
        msg = f"{label} must be a UUID"
        raise ValueError(msg)
    try:
        return UUID(raw)
    except ValueError as exc:
        msg = f"{label} must be a UUID"
        raise ValueError(msg) from exc


def _optional_reason(raw: str) -> str | None:
    text = raw.strip()
    if text == "":
        return None
    return text


def _json_result(raw: str) -> JsonValue:
    if raw.strip() == "":
        msg = "result must be a JSON value"
        raise ValueError(msg)
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "result must be a JSON value"
        raise ValueError(msg) from exc
    return cast(JsonValue, parsed)


def _same_origin(request: Request) -> bool:
    return origin_allowed(
        request.headers.get("origin"),
        request.headers.get("referer"),
        request.url.netloc,
    )


def _too_large(request: Request) -> Response | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        size = int(raw)
    except ValueError:
        return _static("The request is too large.", _TOO_LARGE)
    if size < 0 or size > _MAX_BODY_BYTES:
        return _static("The request is too large.", _TOO_LARGE)
    return None


async def _read_form(request: Request) -> dict[str, str] | Response:
    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001 - the page must not echo an unexpected trace
        _LOG.error("operator ui form was refused (%s)", type(exc).__name__)
        return _static("The form could not be read.", _BAD_REQUEST)
    try:
        return {name: form_text(form, name) for name in _FIELDS}
    finally:
        await form.close()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=_SEE_OTHER)


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _unauthorized() -> Response:
    return _static("Sign in is required.", _UNAUTHORIZED, show_login=True)


def _forbidden() -> Response:
    return _static("The request was refused.", _FORBIDDEN)


def _unexpected(exc: BaseException) -> Response:
    _LOG.error("operator ui request failed (%s)", type(exc).__name__)
    return _static("The server could not complete the request.", _SERVER_ERROR)


def _message(
    message: str,
    *,
    status: int,
    session: Session | None,
    flash: str | None = None,
    flash_error: bool = False,
) -> Response:
    body = render(
        "message.html",
        title="Request refused",
        heading="Request refused",
        message=message,
        show_login=False,
        actor_id=None if session is None else session.actor_id,
        csrf="" if session is None else session.csrf,
        flash=flash,
        flash_error=flash_error,
    )
    return HTMLResponse(body, status_code=status)


def _static(message: str, status: int, *, show_login: bool = False) -> Response:
    body = render(
        "message.html",
        title="Request refused",
        heading="Request refused",
        message=message,
        show_login=show_login,
        actor_id=None,
        csrf="",
        flash=None,
        flash_error=False,
    )
    return HTMLResponse(body, status_code=status)
