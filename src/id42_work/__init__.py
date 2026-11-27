"""ID42 Work SDK: define work, govern execution, verify outcomes."""

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.approvals import Approval, ApprovalStatus
from id42_work.approvals.console import (
    ApprovalConsole,
    ApprovalDecision,
    ApprovalResumeError,
    ApprovalSnapshot,
)
from id42_work.approvals.manager import ApprovalManager
from id42_work.contracts import (
    CapabilityProvider,
    ExecutionContext,
    Executor,
    PolicyResolver,
    ValidationContext,
    Validator,
)
from id42_work.definitions import Work, WorkDescriptor, WorkRef
from id42_work.events import EventDispatcher, EventType, WorkEvent
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
    ReconciliationError,
    ReconciliationNotAllowedError,
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
from id42_work.reconciliation.console import (
    ReconciliationConsole,
    ReconciliationDecision,
    ReconciliationResumeError,
    ReconciliationSnapshot,
)
from id42_work.reconciliation.models import ReconciliationProbe, ReconciliationVerdict
from id42_work.reconciliation.service import ReconciliationService
from id42_work.registry import WorkBinding, WorkRegistry
from id42_work.runner import WorkRunner
from id42_work.states import TRANSITIONS, ExecutionStatus
from id42_work.storage import MemoryStorage, SqliteStorage

if TYPE_CHECKING:
    from id42_work.storage.postgres import PostgresStorage


def __getattr__(name: str) -> object:
    """Load ``PostgresStorage`` only when the caller asks for it.

    The class needs ``psycopg``, which is not a required dependency. Importing the
    package must keep working without the ``postgres`` extra.
    """
    if name == "PostgresStorage":
        from id42_work.storage.postgres import PostgresStorage  # noqa: PLC0415

        return PostgresStorage
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


# A library must not emit log records unless the application configures logging.
logging.getLogger("id42_work").addHandler(logging.NullHandler())

try:
    __version__: str = version("id42-work")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"

__all__ = [
    "TRANSITIONS",
    "ActorAuthority",
    "Approval",
    "ApprovalConsole",
    "ApprovalDecision",
    "ApprovalError",
    "ApprovalExpiredError",
    "ApprovalGate",
    "ApprovalManager",
    "ApprovalNotAllowedError",
    "ApprovalRejectedError",
    "ApprovalRequiredError",
    "ApprovalRequirement",
    "ApprovalResumeError",
    "ApprovalSnapshot",
    "ApprovalStatus",
    "AutonomyLevel",
    "CapabilityCall",
    "CapabilityDeniedError",
    "CapabilityProvider",
    "CapabilityRegistry",
    "CapabilitySpec",
    "ConcurrentModificationError",
    "EventDispatcher",
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
    "MemoryStorage",
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
    "PostgresStorage",
    "ReconciliationConsole",
    "ReconciliationDecision",
    "ReconciliationError",
    "ReconciliationNotAllowedError",
    "ReconciliationProbe",
    "ReconciliationResumeError",
    "ReconciliationService",
    "ReconciliationSnapshot",
    "ReconciliationVerdict",
    "Severity",
    "SqliteStorage",
    "StaticPolicyResolver",
    "StorageError",
    "TrustedActor",
    "UncertainOutcomeError",
    "UntrustedActorError",
    "ValidationContext",
    "ValidationResult",
    "Validator",
    "Work",
    "WorkBinding",
    "WorkDefinitionError",
    "WorkDescriptor",
    "WorkError",
    "WorkEvent",
    "WorkRef",
    "WorkRegistrationError",
    "WorkRegistry",
    "WorkRunner",
    "__version__",
]
