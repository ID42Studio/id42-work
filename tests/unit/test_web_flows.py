"""Operator UI flows: decisions, errors, sessions and redacted pages."""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from starlette.requests import Request

import approval_host
import reconcile_host
from id42_work import ExecutionStatus
from id42_work.approvals.console import ApprovalConsole
from id42_work.exceptions import WorkError
from id42_work.reconciliation.console import ReconciliationConsole
from id42_work.web.app import _json_result, _too_large, _uuid, create_app
from id42_work.web.auth import (
    AUTH_FAILED,
    LoginError,
    SessionStore,
    authenticate,
    csrf_matches,
    form_text,
    origin_allowed,
)
from id42_work.web.config import (
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    OperatorSecret,
    load_operator_secret,
    require_port,
)
from web_client import ORIGIN, app_client, csrf, secret, sign_in, signed_in


def test_approve_with_resume_and_reject_and_logout() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-resume"))
    client = app_client(approval_host.server())
    token = signed_in(client)
    granted = client.post(
        f"/approvals/{approval_id}/approve",
        data={"csrf": token, "reason": "  checked  ", "resume": "on"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "Resume finished." in granted.text
    assert approval_host.calls() == 1
    second = asyncio.run(_pending("web-reject"))
    home = client.get("/")
    token = csrf(home.text)
    rejected = client.post(
        f"/approvals/{second}/reject",
        data={"csrf": token, "reason": "no", "confirm": "yes"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "rejected" in rejected.text
    assert "REJECTED" in rejected.text
    blank = client.post(
        f"/approvals/{second}/reject",
        data={"csrf": csrf(client.get(f"/approvals/{second}").text)},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "a reason is required" in blank.text or "no longer waiting" in blank.text
    logged_out = client.post(
        "/logout",
        data={"csrf": csrf(client.get(f"/approvals/{second}").text)},
        headers={"origin": ORIGIN},
        follow_redirects=False,
    )
    assert logged_out.status_code == 303
    again = client.get("/", follow_redirects=False)
    assert again.headers["location"] == "/login"


def test_resume_failure_is_reported_without_the_inner_error() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-broken"))
    approval_host.break_resume()
    client = app_client(approval_host.server())
    token = signed_in(client)
    page = client.post(
        f"/approvals/{approval_id}/approve",
        data={"csrf": token, "resume": "on"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "Resume did not finish." in page.text
    assert "Traceback" not in page.text
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    assert approval.status.value == "GRANTED"


def test_reconcile_not_applied_abandon_and_bad_result() -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-retry"))
    client = app_client(reconcile_host.server())
    token = signed_in(client)
    bad = client.post(
        f"/reconcile/{execution_id}/{action_id}/applied",
        data={"csrf": token, "reason": "x", "result": "{not-json secret-blob"},
        headers={"origin": ORIGIN},
    )
    assert bad.status_code == 400
    assert "JSON" in bad.text
    assert "secret-blob" not in bad.text
    missing_reason = client.post(
        f"/reconcile/{execution_id}/{action_id}/not-applied",
        data={"csrf": token},
        headers={"origin": ORIGIN},
    )
    assert missing_reason.status_code == 400
    retried = client.post(
        f"/reconcile/{execution_id}/{action_id}/not-applied",
        data={"csrf": token, "reason": "nothing was written", "resume": "on"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "COMPLETED" in retried.text
    assert reconcile_host.calls() == 2
    execution_id, action_id = asyncio.run(_uncertain("web-stop"))
    home = client.get("/")
    assert "uncertain" not in home.text.lower() or action_id in home.text
    token = csrf(home.text)
    abandoned = client.post(
        f"/reconcile/{execution_id}/{action_id}/abandon",
        data={"csrf": token, "reason": "cannot be known", "resume": "on", "confirm": "yes"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "FAILED" in abandoned.text
    assert "Recorded abandon" in abandoned.text
    closed = client.get(f"/reconcile/{execution_id}/{action_id}")
    assert "no longer uncertain" in closed.text


def test_reconcile_resume_failure_keeps_the_decision() -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-resume-fail"))
    reconcile_host.break_resume()
    client = app_client(reconcile_host.server())
    token = signed_in(client)
    page = client.post(
        f"/reconcile/{execution_id}/{action_id}/not-applied",
        data={"csrf": token, "reason": "absent", "resume": "on"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "Resume did not finish." in page.text


def test_unknown_and_malformed_ids_are_refused() -> None:
    approval_host.reset()
    client = app_client(approval_host.server())
    signed_in(client)
    missing = client.get("/approvals/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    bad = client.get("/approvals/not-a-uuid")
    assert bad.status_code == 400
    assert "UUID" in bad.text
    post = client.post(
        "/approvals/not-a-uuid/approve",
        data={"csrf": csrf(client.get("/").text)},
        headers={"origin": ORIGIN},
    )
    assert post.status_code == 400
    reconcile_host.reset()
    other = app_client(reconcile_host.server())
    signed_in(other)
    assert other.get("/reconcile/not-a-uuid/append-note").status_code == 400
    assert other.get("/reconcile/00000000-0000-0000-0000-000000000000/bad id").status_code == 400
    gone = other.get("/reconcile/00000000-0000-0000-0000-000000000000/append-note")
    assert gone.status_code == 404
    refused = other.post(
        "/reconcile/not-a-uuid/append-note/applied",
        data={"csrf": csrf(other.get("/").text), "reason": "x", "result": "null"},
        headers={"origin": ORIGIN},
    )
    assert refused.status_code == 400


def test_login_edges_referer_size_and_session_expiry() -> None:
    approval_host.reset()
    client = app_client(approval_host.server())
    page = client.get("/login")
    assert "Operator token" in page.text
    assert "Actor credential" not in page.text
    again = sign_in(client)
    assert again.status_code == 303
    repeat = client.get("/login", follow_redirects=False)
    assert repeat.headers["location"] == "/"
    logged = client.post(
        "/logout",
        data={"csrf": csrf(client.get("/").text)},
        headers={"origin": ORIGIN},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    page = client.get("/login")
    referer = client.post(
        "/login",
        data={"csrf": csrf(page.text), "token": "operator-secret", "actor_id": "id42-operator"},
        headers={"referer": f"{ORIGIN}/login"},
        follow_redirects=False,
    )
    assert referer.status_code == 303
    huge = client.post(
        "/login",
        data={"csrf": "x"},
        headers={"origin": ORIGIN, "content-length": "999999"},
    )
    assert huge.status_code == 413
    nonsense = client.post(
        "/login",
        content=b"csrf=x",
        headers={
            "origin": ORIGIN,
            "content-length": "nope",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert nonsense.status_code == 413
    negative = client.post(
        "/login",
        content=b"csrf=x",
        headers={
            "origin": ORIGIN,
            "content-length": "-1",
            "content-type": "application/x-www-form-urlencoded",
        },
    )
    assert negative.status_code == 413
    now = datetime.now(UTC)
    clock = {"at": now}

    def _clock() -> datetime:
        return clock["at"]

    expiring = app_client(approval_host.server(), clock=_clock, session_ttl=timedelta(seconds=1))
    assert sign_in(expiring).status_code == 303
    clock["at"] = now + timedelta(seconds=2)
    expired = expiring.get("/", follow_redirects=False)
    assert expired.headers["location"] == "/login"


def test_credential_only_login_and_a_mismatched_actor() -> None:
    approval_host.reset()
    hosted = approval_host.server()
    credential = hosted.authority.issue(
        actor_id="id42-reviewer",
        tenant_id="local",
        grants=frozenset(),
        ttl=timedelta(minutes=5),
    ).to_credential()
    client = app_client(hosted, token=None, credential_auth=True)
    page = client.get("/login")
    assert "Operator token" not in page.text
    assert "Actor credential" in page.text
    refused = sign_in(client, phrase="", credential="not-a-credential")
    assert refused.status_code == 401
    assert AUTH_FAILED in refused.text
    mismatch = sign_in(client, actor_id="someone-else", phrase="", credential=credential)
    assert mismatch.status_code == 401
    ok = sign_in(client, actor_id="", phrase="", credential=credential)
    assert ok.status_code == 303
    both = app_client(hosted, credential_auth=True)
    typed = sign_in(both, actor_id="id42-operator")
    assert typed.status_code == 303


def test_unexpected_errors_hide_the_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    approval_host.reset()
    client = app_client(approval_host.server())
    signed_in(client)
    caplog.set_level(logging.ERROR, logger="id42_work.web")

    async def _boom(self: object) -> object:
        del self
        msg = "token=hidden-trace"
        raise RuntimeError(msg)

    monkeypatch.setattr(ApprovalConsole, "list_pending", _boom)
    page = client.get("/")
    assert page.status_code == 500
    assert "hidden-trace" not in page.text
    assert "hidden-trace" not in caplog.text
    assert "RuntimeError" in caplog.text

    async def _work(self: object) -> object:
        del self
        raise WorkError("approval queue is unavailable")

    monkeypatch.setattr(ApprovalConsole, "list_pending", _work)
    listed = client.get("/")
    assert listed.status_code == 400
    assert "unavailable" in listed.text


def test_show_and_post_hide_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-boom"))
    client = app_client(approval_host.server())
    token = signed_in(client)

    async def _boom(self: object, approval: object) -> object:
        del self, approval
        msg = "secret-in-show"
        raise RuntimeError(msg)

    monkeypatch.setattr(ApprovalConsole, "show", _boom)
    page = client.get(f"/approvals/{approval_id}")
    assert page.status_code == 500
    assert "secret-in-show" not in page.text
    monkeypatch.undo()

    async def _post_boom(self: object, *args: object, **kwargs: object) -> object:
        del self, args, kwargs
        msg = "secret-in-post"
        raise RuntimeError(msg)

    monkeypatch.setattr(ApprovalConsole, "approve", _post_boom)
    posted = client.post(
        f"/approvals/{approval_id}/approve",
        data={"csrf": token},
        headers={"origin": ORIGIN},
    )
    assert posted.status_code == 500
    assert "secret-in-post" not in posted.text

    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-boom-call"))
    other = app_client(reconcile_host.server())
    other_token = signed_in(other)

    async def _call_boom(self: object, *args: object, **kwargs: object) -> object:
        del self, args, kwargs
        msg = "secret-in-call"
        raise RuntimeError(msg)

    monkeypatch.setattr(ReconciliationConsole, "show", _call_boom)
    shown = other.get(f"/reconcile/{execution_id}/{action_id}")
    assert shown.status_code == 500
    assert "secret-in-call" not in shown.text
    monkeypatch.undo()
    monkeypatch.setattr(ReconciliationConsole, "mark_applied", _call_boom)
    applied = other.post(
        f"/reconcile/{execution_id}/{action_id}/applied",
        data={"csrf": other_token, "reason": "x", "result": "1"},
        headers={"origin": ORIGIN},
    )
    assert applied.status_code == 500
    assert "secret-in-call" not in applied.text


def test_a_broken_form_is_a_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _form(self: Request) -> object:
        del self
        msg = "body=secret-form"
        raise RuntimeError(msg)

    monkeypatch.setattr(Request, "form", _form)
    approval_host.reset()
    client = app_client(approval_host.server())
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"csrf": csrf(page.text)},
        headers={"origin": ORIGIN},
    )
    assert response.status_code == 400
    assert "could not be read" in response.text
    assert "secret-form" not in response.text


def test_refused_requests_cover_the_remaining_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-guards"))
    client = app_client(approval_host.server())
    page = client.get("/login")
    naked = client.post(
        "/login",
        data={"csrf": csrf(page.text), "token": "operator-secret", "actor_id": "id42-operator"},
    )
    assert naked.status_code == 403
    wrong = client.post(
        "/login",
        data={"csrf": "nope", "token": "operator-secret", "actor_id": "id42-operator"},
        headers={"origin": ORIGIN},
    )
    assert wrong.status_code == 403
    logged_out = client.post("/logout", headers={"origin": ORIGIN})
    assert logged_out.status_code == 401
    assert client.get(f"/approvals/{approval_id}", follow_redirects=False).status_code == 303
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-guards"))
    other = app_client(reconcile_host.server())
    assert (
        other.get(f"/reconcile/{execution_id}/{action_id}", follow_redirects=False).status_code
        == 303
    )
    assert (
        other.post(
            f"/reconcile/{execution_id}/{action_id}/abandon",
            data={"reason": "x", "csrf": "nope"},
            headers={"origin": ORIGIN},
        ).status_code
        == 401
    )
    token = signed_in(client)
    huge = client.post(
        f"/approvals/{approval_id}/approve",
        data={"csrf": token},
        headers={"origin": ORIGIN, "content-length": "999999"},
    )
    assert huge.status_code == 413

    def _broken(self: Request) -> object:
        del self
        raise RuntimeError("form exploded")

    monkeypatch.setattr(Request, "form", _broken)
    broken = client.post(
        f"/approvals/{approval_id}/reject",
        data={"csrf": token, "reason": "no"},
        headers={"origin": ORIGIN},
    )
    assert broken.status_code == 400
    assert "could not be read" in broken.text


def test_direct_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="UUID"):
        _uuid(None, "approval id")
    with pytest.raises(ValueError, match="JSON"):
        _json_result("  ")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    assert _too_large(Request(scope)) is None
    store = SessionStore(ttl=timedelta(seconds=5), clock=lambda: datetime.now(UTC))
    assert store.take_flash(None) == (None, False)
    path = tmp_path / "token"
    path.write_text("file-token\n", encoding="utf-8")
    path.chmod(0o600)

    def _boom(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "open", _boom)
    with pytest.raises(ValueError, match="cannot be read"):
        load_operator_secret(str(path), {})
    monkeypatch.undo()

    def _long_read(fd: int, size: int) -> bytes:
        del fd, size
        return b"a" * 4097

    monkeypatch.setattr(os, "read", _long_read)
    with pytest.raises(ValueError, match="too large"):
        load_operator_secret(str(path), {})


def test_method_not_allowed_still_has_security_headers() -> None:
    approval_host.reset()
    client = app_client(approval_host.server())
    response = client.post("/", headers={"origin": ORIGIN})
    assert response.status_code == 405
    assert response.headers["x-frame-options"] == "DENY"


def test_create_app_rejects_a_bad_configuration() -> None:
    approval_host.reset()
    server = approval_host.server()
    with pytest.raises(TypeError, match="flag"):
        create_app(server, credential_auth="yes")
    plain = "plain"
    with pytest.raises(TypeError, match="OperatorSecret"):
        create_app(server, token=plain)
    with pytest.raises(ValueError, match="not configured"):
        create_app(server, token=None, credential_auth=False)
    with pytest.raises(ValueError, match="one second"):
        create_app(server, token=secret(), session_ttl=timedelta(milliseconds=1))
    with pytest.raises(TypeError, match="WorkMcpServer"):
        create_app(object(), token=secret())
    with pytest.raises(TypeError, match="factory"):
        create_app(server, token=secret(), factory=1)  # type: ignore[arg-type]


def test_sessions_tokens_and_origins() -> None:
    now = datetime.now(UTC)
    store = SessionStore(ttl=timedelta(seconds=5), clock=lambda: now)
    session_id = store.create("id42-operator")
    row = store.get(session_id)
    assert row is not None
    store.set_flash(session_id, "saved", error=False)
    assert store.take_flash(session_id) == ("saved", False)
    assert store.take_flash(session_id) == (None, False)
    store.set_flash("missing", "nope", error=True)
    store.set_flash(None, "nope", error=True)
    assert store.get("") is None
    assert store.get(1) is None
    store.pop("")
    store.pop(None)
    store.pop(session_id)
    assert store.get(session_id) is None
    with pytest.raises(TypeError, match="clock"):
        SessionStore(ttl=timedelta(seconds=1), clock=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        SessionStore(ttl=timedelta(0), clock=lambda: now)


def test_secret_csrf_origin_and_login_rules() -> None:
    phrase = OperatorSecret("alpha")
    assert phrase.matches("alpha")
    assert not phrase.matches("beta")
    assert not phrase.matches(None)
    assert not phrase.matches("a" * 5000)
    assert "alpha" not in repr(phrase)
    with pytest.raises(ValueError, match="empty"):
        OperatorSecret("")
    assert csrf_matches("same", "same")
    assert not csrf_matches("", "same")
    assert not csrf_matches("same", "")
    assert not csrf_matches(1, "same")
    assert not csrf_matches("a", "b")
    assert form_text({"name": "ada"}, "name") == "ada"
    assert form_text({"name": 1}, "name") == ""
    assert origin_allowed(None, None, "testserver") is False
    assert origin_allowed(None, f"{ORIGIN}/login", "testserver") is True
    assert origin_allowed("javascript:alert(1)", None, "testserver") is False
    assert origin_allowed("http://evil.example", None, "testserver") is False
    assert origin_allowed(ORIGIN, None, "") is False
    with pytest.raises(ValueError, match="port"):
        require_port(True)
    with pytest.raises(ValueError, match="port"):
        require_port(0)
    assert require_port(65535) == 65535
    authority = approval_host.server().authority
    alpha = "alpha"
    nope = "nope"
    assert (
        authenticate(
            token=phrase,
            credential_auth=False,
            submitted_token=alpha,
            actor_id=" id42-operator ",
            credential="ignored",
            authority=authority,
        )
        == "id42-operator"
    )
    with pytest.raises(LoginError):
        authenticate(
            token=phrase,
            credential_auth=False,
            submitted_token=nope,
            actor_id="id42-operator",
            credential="",
            authority=authority,
        )
    with pytest.raises(LoginError):
        authenticate(
            token=phrase,
            credential_auth=False,
            submitted_token=alpha,
            actor_id="bad\nid",
            credential="",
            authority=authority,
        )
    with pytest.raises(LoginError):
        authenticate(
            token=phrase,
            credential_auth=False,
            submitted_token=alpha,
            actor_id="x" * 300,
            credential="",
            authority=authority,
        )
    with pytest.raises(LoginError):
        authenticate(
            token=None,
            credential_auth=True,
            submitted_token="",
            actor_id="",
            credential="not-a-credential",
            authority=authority,
        )
    credential = authority.issue(
        actor_id="id42-reviewer",
        tenant_id="local",
        grants=frozenset(),
        ttl=timedelta(minutes=5),
    ).to_credential()
    assert (
        authenticate(
            token=None,
            credential_auth=True,
            submitted_token="",
            actor_id="",
            credential=credential,
            authority=authority,
        )
        == "id42-reviewer"
    )
    with pytest.raises(LoginError):
        authenticate(
            token=phrase,
            credential_auth=True,
            submitted_token=nope,
            actor_id="",
            credential=credential,
            authority=authority,
        )


def test_token_files_and_environment(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "token"
    path.write_text(" from-file \n", encoding="utf-8")
    path.chmod(0o600)
    loaded = load_operator_secret(str(path), {TOKEN_ENV: "from-env"})
    assert loaded is not None
    assert loaded.matches("from-file")
    assert not loaded.matches("from-env")
    from_env_file = load_operator_secret(None, {TOKEN_FILE_ENV: str(path), TOKEN_ENV: "from-env"})
    assert from_env_file is not None
    assert from_env_file.matches("from-file")
    from_env = load_operator_secret(None, {TOKEN_ENV: " from-env "})
    assert from_env is not None
    assert from_env.matches("from-env")


def test_token_sources_reject_unsafe_files(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "token"
    path.write_text(" from-file \n", encoding="utf-8")
    path.chmod(0o600)
    assert load_operator_secret(None, {}) is None
    with pytest.raises(ValueError, match="empty"):
        load_operator_secret(None, {TOKEN_FILE_ENV: "  "})
    with pytest.raises(ValueError, match=r"empty|single"):
        load_operator_secret(None, {TOKEN_ENV: "  "})
    with pytest.raises(ValueError, match="single"):
        load_operator_secret(None, {TOKEN_ENV: "one\ntwo"})
    missing = root / "missing"
    with pytest.raises(ValueError, match="does not exist"):
        load_operator_secret(str(missing), {})
    link = root / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symbolic link"):
        load_operator_secret(str(link), {})
    loose = root / "loose"
    loose.write_text("loose\n", encoding="utf-8")
    loose.chmod(0o640)
    with pytest.raises(ValueError, match="group or others"):
        load_operator_secret(str(loose), {})
    empty = root / "empty"
    empty.write_text("\n", encoding="utf-8")
    empty.chmod(0o600)
    with pytest.raises(ValueError, match=r"empty|single"):
        load_operator_secret(str(empty), {})
    multi = root / "multi"
    multi.write_text("one\ntwo\n", encoding="utf-8")
    multi.chmod(0o600)
    with pytest.raises(ValueError, match=r"one token|single"):
        load_operator_secret(str(multi), {})
    huge = root / "huge"
    huge.write_bytes(b"a" * 4097)
    huge.chmod(0o600)
    with pytest.raises(ValueError, match="too large"):
        load_operator_secret(str(huge), {})
    binary = root / "binary"
    binary.write_bytes(b"\xff\xfe")
    binary.chmod(0o600)
    with pytest.raises(ValueError, match="utf-8"):
        load_operator_secret(str(binary), {})
    with pytest.raises(ValueError, match="name a file"):
        load_operator_secret(".", {})
    with pytest.raises(ValueError, match="regular file"):
        load_operator_secret(str(root), {})


async def _pending(key: str) -> str:
    hosted = approval_host.server()
    execution = await hosted.runner.run(
        work_id=approval_host.WORK_ID,
        version=approval_host.WORK_VERSION,
        input_data={"text": "hello"},
        actor=hosted.host_actor(),
        idempotency_key=key,
    )
    assert execution.status is ExecutionStatus.WAITING_APPROVAL
    (approval,) = await hosted.runner.approvals.list_pending(execution.execution_id)
    return str(approval.approval_id)


async def _uncertain(key: str) -> tuple[str, str]:
    hosted = reconcile_host.server()
    execution = await hosted.runner.run(
        work_id=reconcile_host.WORK_ID,
        version=reconcile_host.WORK_VERSION,
        input_data={"text": "hello"},
        actor=hosted.host_actor(),
        idempotency_key=key,
    )
    return str(execution.execution_id), "append-note"
