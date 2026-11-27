"""Text interface for ``id42-work reconcile``.

The factory is loaded by the command entry point. This module only decides and
prints. It does not register an MCP tool.
"""

from __future__ import annotations

import argparse
import json
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError

from id42_work.reconciliation.console import (
    ReconciliationConsole,
    ReconciliationDecision,
    ReconciliationSnapshot,
    console_from_server,
)

_JSON = TypeAdapter(JsonValue)
_MISSING = "-"


def format_open(row: ReconciliationSnapshot) -> str:
    """One uncertain call, with no free text, so a host can parse the line."""
    entry = row.entry
    execution = row.execution
    return (
        "uncertain"
        f" execution_id={execution.execution_id}"
        f" action_id={entry.action_id}"
        f" work_id={execution.work_id}"
        f" work_version={execution.work_version}"
        f" capability={entry.capability}"
        f" operation_id={entry.operation_id}"
        f" execution_status={execution.status.value}"
        f" actor_id={execution.actor_id}"
    )


def format_decision(kind: str, decision: ReconciliationDecision) -> str:
    """One stored decision. ``kind`` is ``applied``, ``not-applied`` or ``abandoned``."""
    resumed = "true" if decision.resumed else "false"
    reason = decision.execution.status_reason or _MISSING
    return (
        f"{kind}"
        f" execution_id={decision.execution.execution_id}"
        f" action_id={decision.entry.action_id}"
        f" journal_status={decision.entry.status.value}"
        f" execution_status={decision.execution.status.value}"
        f" resumed={resumed}"
        f" decision_actor_id={decision.actor_id}"
        f" status_reason={reason}"
    )


def format_show(row: ReconciliationSnapshot) -> str:
    """JSON object for one journal entry. The result is included only as a digest."""
    entry = row.entry
    execution = row.execution
    payload = {
        "action_id": entry.action_id,
        "actor_id": execution.actor_id,
        "capability": entry.capability,
        "execution_id": str(execution.execution_id),
        "execution_status": execution.status.value,
        "journal_status": entry.status.value,
        "operation_id": str(entry.operation_id),
        "requires_reconciliation": execution.requires_reconciliation,
        "result_digest": entry.result_digest,
        "tenant_id": execution.tenant_id,
        "work_id": execution.work_id,
        "work_version": execution.work_version,
    }
    return json.dumps(payload, sort_keys=True)


async def run_reconcile(server: object, args: argparse.Namespace) -> None:
    """Run the selected reconcile subcommand. Raises on a refused decision."""
    console = console_from_server(server)
    command = args.reconcile_command
    if command == "list":
        await _list(console)
        return
    execution_id = _execution_id(args.execution_id)
    action_id = _action_id(args.action_id)
    if command == "show":
        print(format_show(await console.show(execution_id, action_id)))
        return
    actor = _required_text(args.actor, "actor")
    reason = _required_text(args.reason, "reason")
    if command == "applied":
        decision = await console.mark_applied(
            execution_id,
            action_id,
            actor_id=actor,
            reason=reason,
            result=_result(args.result),
            resume=_flag(args.resume),
        )
        print(format_decision("applied", decision))
        return
    if command == "not-applied":
        decision = await console.mark_not_applied(
            execution_id,
            action_id,
            actor_id=actor,
            reason=reason,
            resume=_flag(args.resume),
        )
        print(format_decision("not-applied", decision))
        return
    if command == "abandon":
        decision = await console.abandon(execution_id, action_id, actor_id=actor, reason=reason)
        print(format_decision("abandoned", decision))
        return
    msg = f"unknown reconcile command {command!r}"
    raise ValueError(msg)


async def _list(console: ReconciliationConsole) -> None:
    rows = await console.list_open()
    if not rows:
        print("uncertain-none")
        return
    for row in rows:
        print(format_open(row))


def _execution_id(raw: object) -> UUID:
    if not isinstance(raw, str):
        msg = "execution id must be a UUID"
        raise ValueError(msg)
    try:
        return UUID(raw)
    except ValueError as exc:
        msg = "execution id must be a UUID"
        raise ValueError(msg) from exc


def _action_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        msg = "action id must be a non-blank string"
        raise ValueError(msg)
    return raw.strip()


def _required_text(raw: object, name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        msg = f"{name} must be a non-blank string"
        raise ValueError(msg)
    return raw.strip()


def _result(raw: object) -> JsonValue:
    if not isinstance(raw, str) or not raw.strip():
        msg = "result must be a JSON value"
        raise ValueError(msg)
    try:
        return _JSON.validate_python(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        msg = "result must be a JSON value"
        raise ValueError(msg) from exc


def _flag(raw: object) -> bool:
    if not isinstance(raw, bool):
        msg = "resume must be a flag"
        raise ValueError(msg)
    return raw
