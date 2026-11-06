"""Execution records (specification 3.4)."""

from typing import Self
from uuid import UUID

from pydantic import Field, NonNegativeInt, model_validator

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.exceptions import (
    ApprovalRejectedError,
    ApprovalRequiredError,
    CapabilityDeniedError,
    ExecutionCancelledError,
    ExecutionError,
    ExecutionFailedError,
    NonDeterministicExecutionError,
    OutcomeValidationError,
    PolicyDeniedError,
    UncertainOutcomeError,
    WorkError,
)
from id42_work.identifiers import Capability, Identifier, NonEmptyStr, SemVer, Sha256Hex
from id42_work.outcomes.models import Outcome
from id42_work.policy.models import AutonomyLevel
from id42_work.states import ExecutionStatus

_MAX_CAUSES = 10

_FAILURE_TYPES: dict[str, type[ExecutionError | CapabilityDeniedError]] = {
    cls.__name__: cls
    for cls in (
        ExecutionFailedError,
        NonDeterministicExecutionError,
        UncertainOutcomeError,
        CapabilityDeniedError,
    )
}


class ErrorCause(FrozenModel):
    """One link of an exception cause chain."""

    type: NonEmptyStr
    message: str


class ExecutionErrorInfo(FrozenModel):
    """Serializable description of why an execution failed."""

    type: NonEmptyStr
    message: str
    causes: tuple[ErrorCause, ...] = ()
    requires_reconciliation: bool = False

    @classmethod
    def from_exception(cls, error: BaseException, *, requires_reconciliation: bool = False) -> Self:
        """Describe ``error`` and its ``__cause__`` / ``__context__`` chain."""
        causes: list[ErrorCause] = []
        seen: set[int] = {id(error)}
        current = error.__cause__ or error.__context__
        while current is not None and id(current) not in seen and len(causes) < _MAX_CAUSES:
            seen.add(id(current))
            causes.append(ErrorCause(type=type(current).__name__, message=str(current)))
            current = current.__cause__ or current.__context__
        return cls(
            type=type(error).__name__,
            message=str(error),
            causes=tuple(causes),
            requires_reconciliation=requires_reconciliation,
        )


class Execution(FrozenModel):
    """An attempt to complete a specific work definition.

    Records are immutable; every change produces a new record with a higher
    ``revision``, persisted through compare-and-set.
    """

    execution_id: UUID
    revision: NonNegativeInt = 0
    work_id: Identifier
    work_version: SemVer
    definition_digest: Sha256Hex
    policy_id: Identifier | None = None
    policy_version: SemVer | None = None
    policy_reasons: tuple[str, ...] = ()
    effective_autonomy: AutonomyLevel | None = None
    granted_capabilities: frozenset[Capability] = Field(default_factory=frozenset[Capability])
    status: ExecutionStatus = ExecutionStatus.CREATED
    status_reason: str | None = None
    actor_id: NonEmptyStr
    tenant_id: NonEmptyStr | None = None
    input_fingerprint: Sha256Hex
    input_reference: NonEmptyStr
    idempotency_key: NonEmptyStr | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    outcome: Outcome | None = None
    evidence_ids: tuple[UUID, ...] = ()
    error: ExecutionErrorInfo | None = None
    last_event_sequence: NonNegativeInt = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems: list[str] = []
        if self.updated_at < self.created_at:
            problems.append("updated_at cannot precede created_at")
        if self.status.is_terminal != (self.finished_at is not None):
            problems.append("finished_at must be set exactly when the status is terminal")
        if self.outcome is not None and self.outcome.execution_id != self.execution_id:
            problems.append("outcome belongs to another execution")
        if self.status is ExecutionStatus.COMPLETED:
            if self.outcome is None or not self.outcome.verified:
                problems.append("COMPLETED requires a verified outcome")
            elif self.outcome.accepted_at is None:
                problems.append("COMPLETED requires an accepted outcome")
        elif self.outcome is not None and self.outcome.accepted_at is not None:
            problems.append("only a COMPLETED execution has an accepted outcome")
        if self.status is ExecutionStatus.FAILED and self.error is None:
            problems.append("FAILED requires error details")
        if self.status is ExecutionStatus.DENIED:
            if not self.policy_reasons:
                problems.append("DENIED requires policy reasons")
            if self.granted_capabilities:
                problems.append("DENIED executions hold no capabilities")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether the execution reached a terminal state."""
        return self.status.is_terminal

    @property
    def is_completed(self) -> bool:
        """Whether the execution completed with an accepted, verified outcome."""
        return self.status is ExecutionStatus.COMPLETED

    @property
    def requires_reconciliation(self) -> bool:
        """Whether an external side effect may have happened without confirmation."""
        return self.error is not None and self.error.requires_reconciliation

    def raise_for_status(self) -> Outcome:
        """Return the outcome if ``COMPLETED``; otherwise raise the matching exception."""
        if self.status is ExecutionStatus.COMPLETED and self.outcome is not None:
            return self.outcome
        raise self._status_error()

    def _status_error(self) -> WorkError:  # noqa: PLR0911
        eid = self.execution_id
        status = self.status
        reason = f" ({self.status_reason})" if self.status_reason else ""
        if status is ExecutionStatus.DENIED:
            msg = f"execution denied: {', '.join(self.policy_reasons)}"
            return PolicyDeniedError(msg, execution_id=eid, reasons=self.policy_reasons)
        if status is ExecutionStatus.WAITING_APPROVAL:
            msg = "execution is waiting for approval"
            return ApprovalRequiredError(msg, execution_id=eid)
        if status is ExecutionStatus.WAITING_REVIEW:
            msg = "execution is waiting for outcome review"
            return ApprovalRequiredError(msg, execution_id=eid)
        if status is ExecutionStatus.REJECTED:
            msg = f"execution rejected{reason}"
            return ApprovalRejectedError(msg, execution_id=eid)
        if status is ExecutionStatus.FAILED:
            error_type = self.error.type if self.error else ""
            message = self.error.message if self.error else "execution failed"
            return _FAILURE_TYPES.get(error_type, ExecutionFailedError)(message, execution_id=eid)
        if status is ExecutionStatus.VALIDATION_FAILED:
            msg = f"outcome validation failed{reason}"
            return OutcomeValidationError(msg, execution_id=eid)
        if status is ExecutionStatus.CANCELLED:
            msg = f"execution cancelled{reason}"
            return ExecutionCancelledError(msg, execution_id=eid)
        msg = f"execution is not finished (status {status.value})"
        return ExecutionError(msg, execution_id=eid)
