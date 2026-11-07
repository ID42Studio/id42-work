"""ID42 Work SDK: define work, govern execution, verify outcomes."""

from importlib.metadata import PackageNotFoundError, version

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.approvals import Approval, ApprovalStatus
from id42_work.contracts import (
    CapabilityProvider,
    ExecutionContext,
    Executor,
    PolicyResolver,
    ValidationContext,
    Validator,
)
from id42_work.definitions import Work, WorkDescriptor, WorkRef
from id42_work.events import EventType, WorkEvent
from id42_work.evidence import Evidence, EvidenceType
from id42_work.exceptions import (
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotAllowedError,
    ApprovalRejectedError,
    ApprovalRequiredError,
    CapabilityDeniedError,
    ConcurrentModificationError,
    ExecutionCancelledError,
    ExecutionError,
    ExecutionFailedError,
    IdempotencyConflictError,
    InputValidationError,
    InvalidStateTransitionError,
    NonDeterministicExecutionError,
    OutcomeError,
    OutcomeValidationError,
    PolicyDeniedError,
    PolicyError,
    StorageError,
    UncertainOutcomeError,
    UntrustedActorError,
    WorkDefinitionError,
    WorkError,
    WorkRegistrationError,
)
from id42_work.execution import Execution, ExecutionErrorInfo
from id42_work.outcomes import Outcome, Severity, ValidationResult
from id42_work.policy import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    CapabilityCall,
    CapabilitySpec,
    ExecutionLimits,
    Policy,
    PolicyDecision,
    StaticPolicyResolver,
)
from id42_work.policy.engine import PolicyEngine
from id42_work.policy.registry import CapabilityRegistry
from id42_work.states import TRANSITIONS, ExecutionStatus

try:
    __version__: str = version("id42-work")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"

__all__ = [
    "TRANSITIONS",
    "ActorAuthority",
    "Approval",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalGate",
    "ApprovalNotAllowedError",
    "ApprovalRejectedError",
    "ApprovalRequiredError",
    "ApprovalRequirement",
    "ApprovalStatus",
    "AutonomyLevel",
    "CapabilityCall",
    "CapabilityDeniedError",
    "CapabilityProvider",
    "CapabilityRegistry",
    "CapabilitySpec",
    "ConcurrentModificationError",
    "EventType",
    "Evidence",
    "EvidenceType",
    "Execution",
    "ExecutionCancelledError",
    "ExecutionContext",
    "ExecutionError",
    "ExecutionErrorInfo",
    "ExecutionFailedError",
    "ExecutionLimits",
    "ExecutionStatus",
    "Executor",
    "IdempotencyConflictError",
    "InputValidationError",
    "InvalidStateTransitionError",
    "NonDeterministicExecutionError",
    "Outcome",
    "OutcomeError",
    "OutcomeValidationError",
    "Policy",
    "PolicyDecision",
    "PolicyDeniedError",
    "PolicyEngine",
    "PolicyError",
    "PolicyResolver",
    "Severity",
    "StaticPolicyResolver",
    "StorageError",
    "TrustedActor",
    "UncertainOutcomeError",
    "UntrustedActorError",
    "ValidationContext",
    "ValidationResult",
    "Validator",
    "Work",
    "WorkDefinitionError",
    "WorkDescriptor",
    "WorkError",
    "WorkEvent",
    "WorkRef",
    "WorkRegistrationError",
    "__version__",
]
