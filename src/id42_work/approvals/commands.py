"""Text interface for ``id42-work approvals``.

The factory is loaded by the command entry point. This module only decides and
prints. It does not register an MCP tool.
"""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from id42_work.approvals.console import (
    ApprovalConsole,
    ApprovalDecision,
    ApprovalSnapshot,
    console_from_server,
)

_MISSING = "-"


def format_pending(row: ApprovalSnapshot) -> str:
    """One pending approval, with no free text, so a host can parse the line."""
    approval = row.approval
    execution = row.execution
    capability = approval.capability if approval.capability is not None else _MISSING
    return (
        "pending"
        f" approval_id={approval.approval_id}"
        f" execution_id={execution.execution_id}"
        f" work_id={execution.work_id}"
        f" work_version={execution.work_version}"
        f" gate={approval.gate.value}"
        f" action_id={approval.action_id}"
        f" capability={capability}"
        f" execution_status={execution.status.value}"
        f" reviewer_capability={approval.reviewer_capability}"
        f" actor_id={execution.actor_id}"
    )


def format_decision(kind: str, decision: ApprovalDecision) -> str:
    """One stored decision. ``kind`` is ``approved`` or ``rejected``."""
    actor = decision.approval.decision_actor_id or _MISSING
    resumed = "true" if decision.resumed else "false"
    return (
        f"{kind}"
        f" approval_id={decision.approval.approval_id}"
        f" execution_id={decision.execution.execution_id}"
        f" approval_status={decision.approval.status.value}"
        f" execution_status={decision.execution.status.value}"
        f" resumed={resumed}"
        f" decision_actor_id={actor}"
    )


def format_show(row: ApprovalSnapshot) -> str:
    """JSON object for one approval, including the redacted parameter preview."""
    approval = row.approval
    execution = row.execution
    decided_at = approval.decided_at.isoformat() if approval.decided_at is not None else None
    payload = {
        "action_id": approval.action_id,
        "actor_id": execution.actor_id,
        "approval_id": str(approval.approval_id),
        "capability": approval.capability,
        "decided_at": decided_at,
        "decision_actor_id": approval.decision_actor_id,
        "execution_id": str(execution.execution_id),
        "execution_status": execution.status.value,
        "expires_at": approval.expires_at.isoformat(),
        "gate": approval.gate.value,
        "params_preview": approval.params_preview,
        "reason": approval.reason,
        "reviewer_capability": approval.reviewer_capability,
        "status": approval.status.value,
        "tenant_id": execution.tenant_id,
        "work_id": execution.work_id,
        "work_version": execution.work_version,
    }
    return json.dumps(payload, sort_keys=True)


async def run_approval(server: object, args: argparse.Namespace) -> None:
    """Run the selected approvals subcommand. Raises on a refused decision."""
    console = console_from_server(server)
    command = args.approvals_command
    if command == "list":
        await _list(console)
        return
    approval_id = _approval_id(args.approval_id)
    if command == "show":
        print(format_show(await console.show(approval_id)))
        return
    if command == "approve":
        decision = await console.approve(
            approval_id,
            actor_id=_required_text(args.actor, "actor"),
            reason=_optional_text(args.reason),
            resume=_flag(args.resume),
        )
        print(format_decision("approved", decision))
        return
    if command == "reject":
        decision = await console.reject(
            approval_id,
            actor_id=_required_text(args.actor, "actor"),
            reason=_required_text(args.reason, "reason"),
        )
        print(format_decision("rejected", decision))
        return
    msg = f"unknown approvals command {command!r}"
    raise ValueError(msg)


async def _list(console: ApprovalConsole) -> None:
    rows = await console.list_pending()
    if not rows:
        print("pending-none")
        return
    for row in rows:
        print(format_pending(row))


def _approval_id(raw: object) -> UUID:
    if not isinstance(raw, str):
        msg = "approval id must be a UUID"
        raise ValueError(msg)
    try:
        return UUID(raw)
    except ValueError as exc:
        msg = "approval id must be a UUID"
        raise ValueError(msg) from exc


def _required_text(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        msg = f"{name} must be a non-blank string"
        raise ValueError(msg)
    return raw.strip()


def _optional_text(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = "reason must be a string"
        raise ValueError(msg)
    return raw


def _flag(raw: object) -> bool:
    if not isinstance(raw, bool):
        msg = "resume must be a flag"
        raise ValueError(msg)
    return raw
