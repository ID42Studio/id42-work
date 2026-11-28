"""Labeled pages, confirmation, and the local stylesheet."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import approval_host
import reconcile_host
from id42_work import ExecutionStatus
from id42_work.approvals.console import ApprovalSnapshot
from id42_work.approvals.console import console_from_server as approval_console_from_server
from id42_work.approvals.models import ApprovalStatus
from id42_work.journal import JournalStatus
from id42_work.reconciliation.console import ReconciliationSnapshot
from id42_work.reconciliation.console import console_from_server as reconcile_console_from_server
from id42_work.web.app import _call_row
from id42_work.web.present import (
    absolute_time,
    approval_fields,
    badge,
    call_fields,
    factory_label,
    format_preview,
    iso_time,
    relative_time,
)
from web_client import ORIGIN, app_client, signed_in


def test_relative_times_and_labels() -> None:
    now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    assert relative_time(now - timedelta(seconds=10), now) == "just now"
    assert relative_time(now - timedelta(seconds=60), now) == "1 minute ago"
    assert relative_time(now - timedelta(seconds=120), now) == "2 minutes ago"
    assert relative_time(now - timedelta(hours=1), now) == "1 hour ago"
    assert relative_time(now - timedelta(hours=2), now) == "2 hours ago"
    assert relative_time(now - timedelta(days=1), now) == "1 day ago"
    assert relative_time(now - timedelta(days=3), now) == "3 days ago"
    assert relative_time(now + timedelta(minutes=2), now) == "in 2 minutes"
    naive_now = datetime(2026, 11, 27, 12, 0)
    assert relative_time(datetime(2026, 11, 27, 11, 0), naive_now) == "1 hour ago"
    assert absolute_time(naive_now).endswith("UTC")
    assert iso_time(naive_now).endswith("+00:00")
    assert factory_label("") == ""
    assert factory_label("  alpha\nbeta\x00gamma  ") == "alpha beta gamma"
    assert len(factory_label("x" * 250)) == 200
    assert badge("PENDING") == "pending"
    assert badge("NOPE") == "neutral"
    assert format_preview({}) == ""
    assert format_preview(None) == ""
    pretty = format_preview({"b": 1, "a": "é"})
    assert pretty.index('"a"') < pretty.index('"b"')


def test_field_lists_cover_optional_rows() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-fields"))
    hosted = approval_host.server()
    snapshot = asyncio.run(approval_console_from_server(hosted).show(UUID(approval_id)))
    now = datetime.now(UTC)
    blank = snapshot.approval.model_copy(update={"reason": ""})
    unlabeled = approval_fields(
        ApprovalSnapshot(execution=snapshot.execution, approval=blank),
        now,
    )
    assert all(field["label"] != "Reason" for field in unlabeled)
    decided = snapshot.approval.model_copy(
        update={
            "status": ApprovalStatus.GRANTED,
            "decided_at": snapshot.approval.requested_at,
            "decision_actor_id": "id42-operator",
            "reason": "checked",
        }
    )
    labeled = {
        field["label"]
        for field in approval_fields(
            ApprovalSnapshot(execution=snapshot.execution, approval=decided),
            now,
        )
    }
    assert {"Decided", "Decision actor", "Reason"} <= labeled

    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-fields"))
    call = asyncio.run(
        reconcile_console_from_server(reconcile_host.server()).show(UUID(execution_id), action_id)
    )
    entry = call.entry.model_copy(
        update={
            "status": JournalStatus.PENDING_APPROVAL,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "result_digest": None,
            "error": None,
        }
    )
    execution = call.execution.model_copy(
        update={
            "status": ExecutionStatus.READY,
            "error": None,
            "finished_at": None,
            "outcome": None,
            "tenant_id": None,
        }
    )
    copied = ReconciliationSnapshot(execution=execution, entry=entry)
    values = {field["label"]: field["value"] for field in call_fields(copied, now)}
    assert values["Needs reconciliation"] == "No"
    assert values["Tenant"] == "-"
    assert values["Result digest"] == "Not recorded"
    assert _call_row(copied, now)["absolute"] == absolute_time(execution.created_at)


def test_reject_and_abandon_ask_before_they_decide() -> None:
    approval_host.reset()
    approval_id = asyncio.run(_pending("web-confirm"))
    client = app_client(approval_host.server(), factory="notes:server")
    token = signed_in(client)
    page = client.post(
        f"/approvals/{approval_id}/reject",
        data={"csrf": token, "reason": "not now"},
        headers={"origin": ORIGIN},
    )
    assert page.status_code == 200
    assert "Reject this approval?" in page.text
    approval = asyncio.run(approval_host.server().runner.approvals.get(UUID(approval_id)))
    assert approval is not None
    assert approval.status.value == "PENDING"

    reconcile_host.reset()
    execution_id, action_id = asyncio.run(_uncertain("web-confirm"))
    other = app_client(reconcile_host.server())
    other_token = signed_in(other)
    asked = other.post(
        f"/reconcile/{execution_id}/{action_id}/abandon",
        data={"csrf": other_token, "reason": "hold"},
        headers={"origin": ORIGIN},
    )
    assert asked.status_code == 200
    assert "Abandon this call?" in asked.text
    blank = other.post(
        f"/reconcile/{execution_id}/{action_id}/abandon",
        data={"csrf": other_token, "reason": "  "},
        headers={"origin": ORIGIN},
    )
    assert blank.status_code == 400
    assert "a reason is required" in blank.text
    listed = other.get("/reconcile")
    assert action_id in listed.text
    assert "UNCERTAIN" in listed.text


def test_stylesheet_is_local_and_the_factory_is_labeled() -> None:
    approval_host.reset()
    client = app_client(approval_host.server(), factory="  notes:server\n")
    css = client.get("/static/operator.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "prefers-color-scheme" in css.text
    page = client.get("/login")
    assert 'href="/static/operator.css"' in page.text
    assert "<style" not in page.text
    assert "notes:server" in page.text
    plain = app_client(approval_host.server())
    assert "Local factory" in plain.get("/login").text


async def _pending(key: str) -> str:
    hosted = approval_host.server()
    execution = await hosted.runner.run(
        work_id=approval_host.WORK_ID,
        version=approval_host.WORK_VERSION,
        input_data={"text": "hello"},
        actor=hosted.host_actor(),
        idempotency_key=key,
    )
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
