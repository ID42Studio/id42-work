"""The operator UI requires auth, a CSRF token, a loopback bind, and the consoles' grants."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send
from uvicorn import Config, Server

import approval_host
import reconcile_host
from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityRegistry,
    EvidenceType,
    ExecutionContext,
    ExecutionStatus,
    MemoryStorage,
    Policy,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.actors import TrustedActor
from id42_work.mcp import WorkMcpServer
from id42_work.web.app import SecurityHeaders, create_app
from id42_work.web.config import DEFAULT_HOST, require_loopback
from web_client import OPERATOR, ORIGIN, app_client, csrf, secret, sign_in, signed_in

_RAW_PREVIEW = "raw-secret-value"


def _headers(response: object) -> None:
    headers = response.headers  # type: ignore[attr-defined]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"
    policy = headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src" not in policy
    assert "unsafe-eval" not in policy
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert "server" not in headers


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
    assert execution.requires_reconciliation
    return str(execution.execution_id), "append-note"


def test_unauthenticated_reads_and_posts_do_not_decide() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-auth"))
    client = app_client(approval_host.server())
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 303
    assert home.headers["location"] == "/login"
    _headers(home)
    denied = client.post(
        f"/approvals/{approval_id}/approve",
        data={"reason": "no", "csrf": "missing"},
        headers={"origin": ORIGIN},
    )
    assert denied.status_code == 401
    assert "Sign in is required." in denied.text
    _headers(denied)
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    assert approval.status.value == "PENDING"


def test_login_rejects_a_wrong_token_and_keeps_it_out_of_the_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    approval_host.reset()
    phrase = "correct-horse"
    client = app_client(approval_host.server(), phrase=phrase)
    caplog.set_level(logging.INFO, logger="id42_work.web")
    leaked = client.get("/login", params={"token": phrase})
    assert phrase not in leaked.text
    assert phrase not in caplog.text
    response = sign_in(client, phrase="wrong-horse")
    assert response.status_code == 401
    assert "authentication failed" in response.text
    assert "wrong-horse" not in response.text
    assert "id42_session" not in response.headers.get("set-cookie", "")
    _headers(response)


def test_a_post_without_csrf_or_from_another_origin_does_not_decide() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-csrf"))
    client = app_client(approval_host.server())
    token = signed_in(client)
    missing = client.post(
        f"/approvals/{approval_id}/approve",
        data={"reason": "skipped", "csrf": "not-the-token"},
        headers={"origin": ORIGIN},
    )
    assert missing.status_code == 403
    assert "refused" in missing.text
    foreign = client.post(
        f"/approvals/{approval_id}/approve",
        data={"reason": "foreign", "csrf": token, "resume": "on"},
        headers={"origin": "http://evil.example"},
    )
    assert foreign.status_code == 403
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    assert approval.status.value == "PENDING"
    assert approval_host.calls() == 0


def test_default_bind_is_loopback_and_other_addresses_are_refused() -> None:
    assert DEFAULT_HOST == "127.0.0.1"
    assert require_loopback(DEFAULT_HOST) == "127.0.0.1"
    assert require_loopback("::1") == "::1"
    for host in ("0.0.0.0", "192.168.1.9", "localhost", "::ffff:127.0.0.1", "", None, 127):  # noqa: S104
        with pytest.raises(ValueError, match="loopback"):
            require_loopback(host)


def test_the_listener_binds_the_loopback_address_only() -> None:
    approval_host.reset()
    application = create_app(approval_host.server(), token=secret("loopback-only"))
    probe = socket.socket()
    probe.bind((DEFAULT_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()
    server = Server(
        Config(application, host=DEFAULT_HOST, port=port, access_log=False, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        addresses = _listening(port)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert "0100007F" in addresses
    assert "00000000" not in addresses


def test_approval_seals_only_the_reviewer_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-grant"))
    issued: list[Mapping[str, object]] = []
    real = ActorAuthority.issue

    def spy(self: ActorAuthority, **kwargs: object) -> TrustedActor:
        issued.append(kwargs)
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ActorAuthority, "issue", spy)
    client = app_client(approval_host.server())
    token = signed_in(client)
    response = client.post(
        f"/approvals/{approval_id}/approve",
        data={"csrf": token, "reason": "checked", "grants": "payments.execute"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Approval granted" in response.text
    assert issued[0]["grants"] == frozenset({"approve:note"})
    assert issued[0]["actor_id"] == OPERATOR
    assert issued[0]["tenant_id"] == approval_host.TENANT_ID
    assert all(row["grants"] == frozenset({"approve:note"}) for row in issued)
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    evidence = asyncio.run(hosted.runner.evidence(approval.execution_id))
    recorded = [item for item in evidence if item.evidence_type is EvidenceType.APPROVAL_DECISION]
    assert recorded[-1].metadata["decision_actor_id"] == OPERATOR
    assert approval_host.calls() == 0


def test_the_execution_actor_cannot_approve_itself() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-self"))
    client = app_client(approval_host.server())
    token = signed_in(client, approval_host.ACTOR_ID)
    page = client.post(
        f"/approvals/{approval_id}/reject",
        data={"csrf": token, "reason": "self"},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "own execution" in page.text
    hosted = approval_host.server()
    approval = asyncio.run(hosted.runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    assert approval.status.value == "PENDING"
    assert approval.decision_actor_id is None


def test_reconcile_seals_only_the_reconcile_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-reconcile"))
    issued: list[Mapping[str, object]] = []
    real = ActorAuthority.issue

    def spy(self: ActorAuthority, **kwargs: object) -> TrustedActor:
        issued.append(kwargs)
        return real(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ActorAuthority, "issue", spy)
    client = app_client(reconcile_host.server())
    token = signed_in(client)
    outcome = '{"ledger":"hidden-outcome"}'
    page = client.post(
        f"/reconcile/{execution_id}/{action_id}/applied",
        data={"csrf": token, "reason": "seen in the ledger", "result": outcome},
        headers={"origin": ORIGIN},
        follow_redirects=True,
    )
    assert "hidden-outcome" not in page.text
    assert "Recorded applied" in page.text
    assert issued[0]["grants"] == frozenset({"reconcile:note"})
    assert issued[0]["actor_id"] == OPERATOR
    show = client.get(f"/reconcile/{execution_id}/{action_id}")
    assert "hidden-outcome" not in show.text
    assert "result_digest" in show.text
    assert '"result"' not in show.text


def test_parameter_preview_is_redacted_like_the_cli() -> None:
    server = _manual_server()
    approval_id = asyncio.run(_manual_approval(server))
    client = app_client(server)
    signed_in(client)
    page = client.get(f"/approvals/{approval_id}")
    assert page.status_code == 200
    assert _RAW_PREVIEW not in page.text
    assert "***" in page.text
    assert "42" in page.text
    home = client.get("/")
    assert _RAW_PREVIEW not in home.text


def test_pages_escape_operator_text() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-escape"))
    client = app_client(approval_host.server())
    actor = "<script>alert(1)</script>"
    signed_in(client, actor)
    home = client.get("/")
    assert "<script>alert(1)</script>" not in home.text
    assert "&lt;script&gt;" in home.text
    token = csrf(home.text)
    client.post(
        f"/approvals/{approval_id}/reject",
        data={"csrf": token, "reason": "<img src=x onerror=alert(1)>"},
        headers={"origin": ORIGIN},
    )
    detail = client.get(f"/approvals/{approval_id}")
    assert "<img src=x" not in detail.text
    assert "&lt;img" in detail.text


def test_a_verified_credential_signs_in_when_the_token_also_matches() -> None:
    approval_host.reset()
    hosted = approval_host.server()
    credential = hosted.authority.issue(
        actor_id="id42-operator",
        tenant_id=approval_host.TENANT_ID,
        grants=frozenset(),
        ttl=timedelta(minutes=5),
    ).to_credential()
    client = app_client(hosted, credential_auth=True)
    failed = sign_in(client, credential=credential, phrase="nope")
    assert failed.status_code == 401
    assert credential not in failed.text
    signed = sign_in(client, credential=credential)
    assert signed.status_code == 303
    home = client.get("/")
    assert "id42-operator" in home.text
    assert credential not in home.text


def test_mcp_server_still_has_no_decision_tools() -> None:
    approval_host.reset()
    server = approval_host.server()
    names = {tool.name for tool in asyncio.run(server.server.list_tools())}
    assert names.isdisjoint({"approve", "reject", "reconcile"})
    assert all(
        "approv" not in name and "reject" not in name and "reconcil" not in name for name in names
    )


def test_the_ui_source_calls_the_consoles_only() -> None:
    banned = (
        "ApprovalManager",
        "ReconciliationService",
        "runner.approvals",
        "runner.reconciliation",
    )
    root = Path(__file__).resolve().parents[2] / "src" / "id42_work" / "web"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in text


def test_security_headers_strip_a_server_banner() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        del receive
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"server", b"secret-server")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    wrapped = SecurityHeaders(inner)

    async def run() -> list[tuple[bytes, bytes]]:
        captured: list[tuple[bytes, bytes]] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b""}

        async def send(message: Message) -> None:
            if message["type"] != "http.response.start":
                return
            headers = message.get("headers", [])
            assert isinstance(headers, list)
            captured.extend(headers)

        http: Scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
        lifespan: Scope = {"type": "lifespan"}
        await wrapped(http, receive, send)
        await wrapped(lifespan, receive, send)
        return captured

    headers = asyncio.run(run())
    names = {name.lower() for name, _value in headers}
    assert b"server" not in names
    assert b"x-content-type-options" in names


class HoldInput(BaseModel):
    token: str
    amount: str


class HoldOutput(BaseModel):
    ok: bool


class Hold:
    async def execute(self, input_data: HoldInput, context: ExecutionContext) -> HoldOutput:
        del input_data, context
        return HoldOutput(ok=True)


def _manual_server() -> WorkMcpServer:
    registry = WorkRegistry()
    registry.register(
        Work(
            id="hold",
            version="1.0.0",
            name="Hold",
            input_schema=HoldInput,
            output_schema=HoldOutput,
            required_capabilities=frozenset(),
            autonomy_ceiling=AutonomyLevel.MANUAL,
        ),
        executor=Hold(),
    )
    authority = ActorAuthority("web-preview")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {
                ("hold", "1.0.0"): Policy(
                    id="hold",
                    version="1.0.0",
                    autonomy=AutonomyLevel.MANUAL,
                    allowed_capabilities=frozenset(),
                )
            }
        ),
        capabilities=CapabilityRegistry(),
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    def issue() -> TrustedActor:
        return authority.issue(
            actor_id="id42-agent",
            tenant_id="local",
            grants=frozenset(),
            ttl=timedelta(hours=1),
        )

    return WorkMcpServer(runner, authority=authority, issue_actor=issue)


async def _manual_approval(server: WorkMcpServer) -> str:
    execution = await server.runner.run(
        work_id="hold",
        version="1.0.0",
        input_data={"token": _RAW_PREVIEW, "amount": "42"},
        actor=server.host_actor(),
        idempotency_key="preview",
    )
    (approval,) = await server.runner.approvals.list_pending(execution.execution_id)
    return str(approval.approval_id)


def _listening(port: int) -> set[str]:
    found: set[str] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        path = Path(name)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            local, _remote, state = line.split()[1:4]
            address, hex_port = local.split(":")
            if state == "0A" and int(hex_port, 16) == port:
                found.add(address)
    return found
