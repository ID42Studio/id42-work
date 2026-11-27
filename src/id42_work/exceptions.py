"""Exception hierarchy of the SDK.

No class here is named ``ValidationError`` to avoid confusion with
``pydantic.ValidationError``. Wrapping exceptions always uses ``raise ... from cause``.
"""

from uuid import UUID


class WorkError(Exception):
    """Base class for every SDK error."""

    def __init__(self, message: str = "", *, execution_id: UUID | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.execution_id = execution_id


class WorkDefinitionError(WorkError):
    """A work definition is invalid or does not match a recorded definition."""


class WorkRegistrationError(WorkError):
    """A work cannot be registered or resolved from the registry."""


class InputValidationError(WorkError):
    """Input data does not satisfy the work's input schema or call requirements."""


class PolicyError(WorkError):
    """Base class for authorization failures."""


class PolicyDeniedError(PolicyError):
    """The policy decision denied the execution."""

    def __init__(
        self,
        message: str = "",
        *,
        execution_id: UUID | None = None,
        reasons: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message, execution_id=execution_id)
        self.reasons = reasons


class CapabilityDeniedError(PolicyError):
    """A capability was used without being granted to the execution."""


class UntrustedActorError(PolicyError):
    """An actor was not issued by the configured authority, was altered, or expired."""


class ApprovalError(WorkError):
    """Base class for approval failures."""


class ApprovalRequiredError(ApprovalError):
    """The execution is waiting for an approval or an outcome review."""


class ApprovalRejectedError(ApprovalError):
    """A reviewer rejected the approval."""


class ApprovalExpiredError(ApprovalError):
    """The approval expired before it was decided or consumed."""


class ApprovalNotAllowedError(ApprovalError):
    """The reviewer may not decide this approval (grant, tenant or self-approval)."""


class ExecutionError(WorkError):
    """Base class for execution failures."""


class ExecutionFailedError(ExecutionError):
    """The executor raised, timed out or exceeded its limits."""


class ExecutionCancelledError(ExecutionError):
    """The execution was cancelled."""


class InvalidStateTransitionError(ExecutionError):
    """A transition not present in the state machine's table was attempted."""

    def __init__(
        self,
        from_status: object,
        to_status: object,
        *,
        execution_id: UUID | None = None,
        kind: str = "execution",
        detail: str | None = None,
    ) -> None:
        message = f"invalid {kind} state transition: {from_status} -> {to_status}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message, execution_id=execution_id)
        self.from_status = from_status
        self.to_status = to_status


class IdempotencyConflictError(ExecutionError):
    """An idempotency key was reused with a different input."""


class NonDeterministicExecutionError(ExecutionError):
    """A replayed executor issued an action that differs from the journal."""


class UncertainOutcomeError(ExecutionError):
    """An external side effect may or may not have happened; reconciliation is required."""


class ReconciliationError(WorkError):
    """A reconciliation decision was refused or could not be stored."""


class ReconciliationNotAllowedError(ReconciliationError):
    """The actor may not reconcile this execution (grant, tenant or self-decision)."""


class AuthorityKeyError(WorkError):
    """The authority key file is missing, unreadable, or not safe to use.

    ``reason`` is a stable code: ``missing``, ``missing_directory``, ``exists``,
    ``permissions``, ``symlink``, ``corrupt``, ``mismatch``, ``io``, ``memory``,
    ``limit``, ``relative`` or ``invalid``. The message may name a path. It never
    includes key material.
    """

    def __init__(self, message: str = "", *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


class OutcomeError(WorkError):
    """Base class for outcome failures."""


class OutcomeValidationError(OutcomeError):
    """The outcome failed mandatory validation or evidence requirements."""


class StorageError(WorkError):
    """A storage operation failed."""


class ConcurrentModificationError(StorageError):
    """A compare-and-set operation found a different current state."""
