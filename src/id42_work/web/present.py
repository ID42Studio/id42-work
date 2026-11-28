"""Turn console snapshots into the fields the pages render.

The values are the same ones ``approvals show`` and ``reconcile show`` print.
Parameter previews are already redacted by the runner. The provider result is
never included; only its digest is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime

from id42_work.approvals.console import ApprovalSnapshot
from id42_work.reconciliation.console import ReconciliationSnapshot

_MINUTE = 60
_HOUR = 3600
_DAY = 86400
_JUST_NOW = 45
_LABEL_LIMIT = 200


def factory_label(value: str) -> str:
    """One line for the header. Newlines and NULs cannot break the layout."""
    cleaned = " ".join(value.replace("\x00", " ").split())
    if len(cleaned) > _LABEL_LIMIT:
        return cleaned[:_LABEL_LIMIT]
    return cleaned


def relative_time(moment: datetime, now: datetime) -> str:
    """A short distance from ``now``, in UTC, for a person reading the page."""
    seconds = int((_aware(now) - _aware(moment)).total_seconds())
    future = seconds < 0
    span = abs(seconds)
    if span < _JUST_NOW:
        return "just now"
    count, unit = _unit(span)
    if future:
        return f"in {count} {unit}"
    return f"{count} {unit} ago"


def absolute_time(moment: datetime) -> str:
    """Clock time in UTC, shown beside the relative phrase."""
    return _aware(moment).strftime("%Y-%m-%d %H:%M UTC")


def iso_time(moment: datetime) -> str:
    """Machine-readable instant for a ``time`` element."""
    return _aware(moment).isoformat()


def badge(status: str) -> str:
    """CSS suffix for a known status. Anything else stays neutral."""
    key = status.lower().replace("_", "-")
    known = {
        "pending",
        "waiting-approval",
        "uncertain",
        "failed",
        "rejected",
        "granted",
        "completed",
        "succeeded",
    }
    if key in known:
        return key
    return "neutral"


def format_preview(value: Mapping[str, object] | None) -> str:
    """Pretty JSON for a redacted parameter preview. An empty preview is blank."""
    if not value:
        return ""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def approval_fields(row: ApprovalSnapshot, now: datetime) -> list[dict[str, str]]:
    """Labeled fields for one approval. The preview is separate."""
    approval = row.approval
    execution = row.execution
    capability = approval.capability if approval.capability is not None else "-"
    fields = [
        _text("Work", execution.work_id),
        _text("Version", execution.work_version),
        _text("Gate", approval.gate.value),
        _text("Capability", capability),
        _text("Action", approval.action_id),
        _badge("Approval", approval.status.value),
        _badge("Execution", execution.status.value),
        _text("Actor", execution.actor_id),
        _text("Tenant", _shown(execution.tenant_id)),
        _copy("Execution id", str(execution.execution_id)),
        _text("Reviewer capability", approval.reviewer_capability),
        _when("Requested", approval.requested_at, now),
        _when("Expires", approval.expires_at, now),
    ]
    if approval.decided_at is not None:
        fields.append(_when("Decided", approval.decided_at, now))
    if approval.decision_actor_id is not None:
        fields.append(_text("Decision actor", approval.decision_actor_id))
    if approval.reason is not None and approval.reason != "":
        fields.append(_text("Reason", approval.reason))
    return fields


def call_fields(row: ReconciliationSnapshot, now: datetime) -> list[dict[str, str]]:
    """Labeled fields for one uncertain call. The result body is omitted."""
    entry = row.entry
    execution = row.execution
    digest = entry.result_digest if entry.result_digest is not None else "Not recorded"
    moment = entry.started_at if entry.started_at is not None else execution.created_at
    return [
        _text("Work", execution.work_id),
        _text("Version", execution.work_version),
        _text("Capability", entry.capability),
        _text("Action", entry.action_id),
        _badge("Call", entry.status.value),
        _badge("Execution", execution.status.value),
        _text("Actor", execution.actor_id),
        _text("Tenant", _shown(execution.tenant_id)),
        _copy("Execution id", str(execution.execution_id)),
        _text("Operation id", str(entry.operation_id)),
        _text("Result digest", digest),
        _text("Needs reconciliation", "Yes" if execution.requires_reconciliation else "No"),
        _when("Started", moment, now),
    ]


def _unit(span: int) -> tuple[int, str]:
    if span < _HOUR:
        count = span // _MINUTE
        unit = "minute" if count == 1 else "minutes"
        return count, unit
    if span < _DAY:
        count = span // _HOUR
        unit = "hour" if count == 1 else "hours"
        return count, unit
    count = span // _DAY
    unit = "day" if count == 1 else "days"
    return count, unit


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _shown(value: str | None) -> str:
    return value if value is not None else "-"


def _text(label: str, value: str) -> dict[str, str]:
    return {"kind": "text", "label": label, "value": value}


def _copy(label: str, value: str) -> dict[str, str]:
    return {"kind": "copy", "label": label, "value": value}


def _badge(label: str, value: str) -> dict[str, str]:
    return {"kind": "badge", "label": label, "value": value, "badge": badge(value)}


def _when(label: str, moment: datetime, now: datetime) -> dict[str, str]:
    return {
        "kind": "time",
        "label": label,
        "value": absolute_time(moment),
        "relative": relative_time(moment, now),
        "iso": iso_time(moment),
    }
