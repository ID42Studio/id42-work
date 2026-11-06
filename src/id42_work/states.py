"""Execution status values (specification 4).

The transition table and the state machine are introduced in milestone M2.
"""

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
