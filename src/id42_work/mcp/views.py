"""JSON views of governed records for MCP clients.

Views omit actor seals, raw inputs and approval parameter previews. Journal results
pass through the default redactor. Events and evidence are already redacted by the
runner before they are stored.
"""

from typing import cast
from uuid import UUID

from pydantic import JsonValue

from id42_work.approvals.models import Approval
from id42_work.definitions import Work
from id42_work.events.models import WorkEvent
from id42_work.events.redaction import default_redactor
from id42_work.evidence.models import Evidence
from id42_work.execution import Execution
from id42_work.journal import JournalEntry
from id42_work.states import ExecutionStatus

_HUMAN = frozenset({ExecutionStatus.WAITING_APPROVAL, ExecutionStatus.WAITING_REVIEW})


def redact_json(value: JsonValue) -> JsonValue:
    """Mask sensitive keys in a JSON value. Scalars are unchanged."""
    if isinstance(value, dict):
        return default_redactor(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    return value


def work_view(work: Work) -> dict[str, JsonValue]:
    """Serializable description of a work definition. It contains no executable code."""
    described = work.describe()
    return {
        "id": described.id,
        "version": described.version,
        "name": described.name,
        "description": described.description,
        "definition_digest": described.definition_digest,
        "autonomy_ceiling": described.autonomy_ceiling.value,
        "required_capabilities": list(described.required_capabilities),
        "input_schema": described.input_schema.json_schema,
        "output_schema": described.output_schema.json_schema,
    }


def execution_summary(execution: Execution) -> dict[str, JsonValue]:
    """Status of one execution, without its output."""
    finished = execution.finished_at.isoformat() if execution.finished_at is not None else None
    return {
        "execution_id": str(execution.execution_id),
        "work_id": execution.work_id,
        "work_version": execution.work_version,
        "status": execution.status.value,
        "status_reason": execution.status_reason,
        "needs_human_decision": execution.status in _HUMAN,
        "requires_reconciliation": execution.requires_reconciliation,
        "created_at": execution.created_at.isoformat(),
        "updated_at": execution.updated_at.isoformat(),
        "finished_at": finished,
    }


def approval_view(approval: Approval) -> dict[str, JsonValue]:
    """Approval state without the parameter preview or the action digest."""
    return {
        "approval_id": str(approval.approval_id),
        "gate": approval.gate.value,
        "status": approval.status.value,
        "capability": approval.capability,
        "action_id": approval.action_id,
        "reviewer_capability": approval.reviewer_capability,
        "expires_at": approval.expires_at.isoformat(),
        "reason": approval.reason,
        "decision_actor_id": approval.decision_actor_id,
    }


def execution_view(execution: Execution, approvals: tuple[Approval, ...]) -> dict[str, JsonValue]:
    """Status, verified output and approval state of one execution."""
    error = execution.error.model_dump(mode="json") if execution.error is not None else None
    return {
        **execution_summary(execution),
        "definition_digest": execution.definition_digest,
        "actor_id": execution.actor_id,
        "tenant_id": execution.tenant_id,
        "policy_id": execution.policy_id,
        "policy_version": execution.policy_version,
        "policy_reasons": cast("list[JsonValue]", list(execution.policy_reasons)),
        "granted_capabilities": cast("list[JsonValue]", sorted(execution.granted_capabilities)),
        "error": cast("dict[str, JsonValue] | None", error),
        "outcome": _outcome(execution),
        "approvals": cast("list[JsonValue]", [approval_view(approval) for approval in approvals]),
    }


def journal_view(execution_id: UUID, entries: tuple[JournalEntry, ...]) -> dict[str, JsonValue]:
    """Journal entries. Parameters stay as digests; results are redacted."""
    rendered: list[JsonValue] = []
    for entry in entries:
        error = entry.error.model_dump(mode="json") if entry.error is not None else None
        rendered.append(
            {
                "action_id": entry.action_id,
                "capability": entry.capability,
                "status": entry.status.value,
                "operation_id": str(entry.operation_id),
                "params_digest": entry.params_digest,
                "result": redact_json(entry.result),
                "error": cast("dict[str, JsonValue] | None", error),
            }
        )
    return {"execution_id": str(execution_id), "entries": rendered}


def events_view(execution_id: UUID, events: tuple[WorkEvent, ...]) -> dict[str, JsonValue]:
    """Redacted events, in sequence order."""
    rendered: list[JsonValue] = [
        {
            "sequence_number": event.sequence_number,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "actor_reference": event.actor_reference,
            "payload": event.payload,
        }
        for event in events
    ]
    return {"execution_id": str(execution_id), "events": rendered}


def evidence_view(execution_id: UUID, evidence: tuple[Evidence, ...]) -> dict[str, JsonValue]:
    """Evidence references and digests. Payloads stay in storage."""
    rendered: list[JsonValue] = [
        {
            "evidence_id": str(item.evidence_id),
            "evidence_type": item.evidence_type.value,
            "source": item.source,
            "created_at": item.created_at.isoformat(),
            "content_reference": item.content_reference,
            "content_digest": item.content_digest,
            "metadata": item.metadata,
        }
        for item in evidence
    ]
    return {"execution_id": str(execution_id), "evidence": rendered}


def approvals_view(execution_id: UUID, approvals: tuple[Approval, ...]) -> dict[str, JsonValue]:
    """Approval states for one execution. Decisions are not accepted here."""
    return {
        "execution_id": str(execution_id),
        "approvals": [approval_view(approval) for approval in approvals],
    }


def _outcome(execution: Execution) -> dict[str, JsonValue] | None:
    outcome = execution.outcome
    if outcome is None:
        return None
    output = cast("dict[str, JsonValue]", outcome.output.model_dump(mode="json"))
    validation: list[JsonValue] = [
        {
            "validator_id": result.validator_id,
            "passed": result.passed,
            "severity": result.severity.value,
            "required": result.required,
            "message": result.message,
        }
        for result in outcome.validation_results
    ]
    return {
        "verified": outcome.verified,
        "accepted": outcome.accepted_at is not None,
        "output": output,
        "validation": validation,
    }
