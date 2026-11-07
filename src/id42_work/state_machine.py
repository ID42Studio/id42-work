"""Pure state transitions for executions and approvals (specification 4.2 and 7.4).

These functions check the transition tables, build the next immutable record and
re-run every model invariant. They do not persist anything: the runner stores the
result with a compare-and-set and emits exactly one event per transition.
"""

from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from id42_work._time import to_utc
from id42_work.approvals.models import APPROVAL_TRANSITIONS, Approval, ApprovalStatus
from id42_work.exceptions import InvalidStateTransitionError
from id42_work.execution import Execution
from id42_work.states import ExecutionStatus, can_transition

_EXECUTION_PROTECTED = frozenset(
    {
        "execution_id",
        "revision",
        "work_id",
        "work_version",
        "definition_digest",
        "actor_id",
        "tenant_id",
        "input_fingerprint",
        "input_reference",
        "idempotency_key",
        "created_at",
        "status",
        "updated_at",
        "finished_at",
        "status_reason",
    }
)
"""Fields a transition may never set through ``changes``."""

_APPROVAL_PROTECTED = frozenset(
    {
        "approval_id",
        "execution_id",
        "gate",
        "action_id",
        "capability",
        "action_digest",
        "reviewer_capability",
        "reason_required",
        "requested_at",
        "expires_at",
        "status",
    }
)


def transition_execution(
    execution: Execution,
    to_status: ExecutionStatus,
    *,
    at: datetime,
    reason: str | None = None,
    **changes: object,
) -> Execution:
    """Return ``execution`` moved to ``to_status``, with ``revision`` incremented.

    ``changes`` may set non-identity fields (outcome, error, policy fields, evidence
    ids, event sequence). Raises ``InvalidStateTransitionError`` if the transition is
    not in the table, if ``changes`` touch protected fields, if ``at`` precedes the
    last update, or if the resulting record violates an invariant.
    """
    eid = execution.execution_id
    current = execution.status
    if not can_transition(current, to_status):
        raise InvalidStateTransitionError(current.value, to_status.value, execution_id=eid)
    _check_changes(
        changes,
        _EXECUTION_PROTECTED,
        transition=(current.value, to_status.value),
        execution_id=eid,
    )
    moment = to_utc(at)
    if moment < execution.updated_at:
        raise InvalidStateTransitionError(
            current.value, to_status.value, execution_id=eid, detail="time moved backwards"
        )
    fields: dict[str, object] = {name: getattr(execution, name) for name in Execution.model_fields}
    fields.update(changes)
    fields.update(
        status=to_status,
        status_reason=reason,
        revision=execution.revision + 1,
        updated_at=moment,
        finished_at=moment if to_status.is_terminal else None,
    )
    try:
        return Execution.model_validate(fields)
    except ValidationError as error:
        raise InvalidStateTransitionError(
            current.value, to_status.value, execution_id=eid, detail=_first_error(error)
        ) from error


def transition_approval(
    approval: Approval,
    to_status: ApprovalStatus,
    **changes: object,
) -> Approval:
    """Return ``approval`` moved to ``to_status``.

    ``changes`` carry the decision fields (``decided_at``, ``decision_actor_id``,
    ``reason``, ``consumed_at``). Raises ``InvalidStateTransitionError`` when the
    change is not allowed or the result violates an invariant.
    """
    current = approval.status
    eid = approval.execution_id
    if to_status not in APPROVAL_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            current.value, to_status.value, execution_id=eid, kind="approval"
        )
    _check_changes(
        changes,
        _APPROVAL_PROTECTED,
        transition=(current.value, to_status.value),
        execution_id=eid,
        kind="approval",
    )
    fields: dict[str, object] = {name: getattr(approval, name) for name in Approval.model_fields}
    fields.update(changes)
    fields["status"] = to_status
    try:
        return Approval.model_validate(fields)
    except ValidationError as error:
        raise InvalidStateTransitionError(
            current.value,
            to_status.value,
            execution_id=eid,
            kind="approval",
            detail=_first_error(error),
        ) from error


def _check_changes(
    changes: dict[str, object],
    protected: frozenset[str],
    *,
    transition: tuple[str, str],
    execution_id: UUID,
    kind: str = "execution",
) -> None:
    forbidden = sorted(protected & changes.keys())
    if forbidden:
        raise InvalidStateTransitionError(
            transition[0],
            transition[1],
            execution_id=execution_id,
            kind=kind,
            detail=f"protected fields cannot change: {', '.join(forbidden)}",
        )


def _first_error(error: ValidationError) -> str:
    details = error.errors()
    return str(details[0]["msg"]) if details else str(error)
