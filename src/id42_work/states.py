"""Execution status values and the transition table (specification 4.1)."""

from enum import Enum


class ExecutionStatus(Enum):
    """Lifecycle states of an execution."""

    CREATED = "CREATED"
    POLICY_CHECK = "POLICY_CHECK"
    DENIED = "DENIED"
    READY = "READY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    VALIDATING = "VALIDATING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    WAITING_REVIEW = "WAITING_REVIEW"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Whether no transition may leave this state."""
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.DENIED,
        ExecutionStatus.FAILED,
        ExecutionStatus.VALIDATION_FAILED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.REJECTED,
        ExecutionStatus.CANCELLED,
    }
)

_S = ExecutionStatus

TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    _S.CREATED: frozenset({_S.POLICY_CHECK, _S.CANCELLED}),
    _S.POLICY_CHECK: frozenset({_S.DENIED, _S.READY}),
    _S.READY: frozenset({_S.WAITING_APPROVAL, _S.RUNNING, _S.DENIED, _S.CANCELLED}),
    _S.WAITING_APPROVAL: frozenset({_S.READY, _S.REJECTED, _S.CANCELLED}),
    _S.RUNNING: frozenset({_S.WAITING_APPROVAL, _S.FAILED, _S.VALIDATING}),
    _S.VALIDATING: frozenset({_S.VALIDATION_FAILED, _S.WAITING_REVIEW, _S.COMPLETED}),
    _S.WAITING_REVIEW: frozenset({_S.COMPLETED, _S.REJECTED, _S.CANCELLED}),
    _S.DENIED: frozenset(),
    _S.FAILED: frozenset(),
    _S.VALIDATION_FAILED: frozenset(),
    _S.COMPLETED: frozenset(),
    _S.REJECTED: frozenset(),
    _S.CANCELLED: frozenset(),
}
"""Allowed execution transitions. Every other pair is forbidden.

Business guards (e.g. ``WAITING_APPROVAL -> READY`` needs a granted approval) are
enforced by the runner and the approval manager; this table only fixes which
transitions can exist at all.
"""


def can_transition(from_status: ExecutionStatus, to_status: ExecutionStatus) -> bool:
    """Whether ``from_status -> to_status`` is in the transition table."""
    return to_status in TRANSITIONS[from_status]
