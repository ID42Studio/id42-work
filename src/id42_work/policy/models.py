"""Policy, autonomy and policy-decision models (specification 3.2 and 6.2)."""

from datetime import timedelta
from enum import Enum
from functools import total_ordering
from typing import Self

from pydantic import Field, PositiveInt, model_validator

from id42_work._model import FrozenModel
from id42_work.error_info import ExecutionErrorInfo
from id42_work.evidence.models import EvidenceType
from id42_work.identifiers import Capability, Grant, Identifier, SemVer


@total_ordering
class AutonomyLevel(Enum):
    """Autonomy granted to an execution, ordered from least to most autonomous."""

    MANUAL = "MANUAL"
    SUPERVISED = "SUPERVISED"
    GUIDED = "GUIDED"
    AUTONOMOUS = "AUTONOMOUS"

    @property
    def rank(self) -> int:
        """Position in the order ``MANUAL < SUPERVISED < GUIDED < AUTONOMOUS``."""
        return list(AutonomyLevel).index(self)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, AutonomyLevel):
            return NotImplemented
        return self.rank < other.rank


class ApprovalGate(Enum):
    """Points at which an execution may require human approval."""

    START = "START"
    ACTION = "ACTION"
    OUTCOME = "OUTCOME"


class ApprovalRequirement(FrozenModel):
    """A mandatory approval gate. Requirements add gates at every autonomy level."""

    gate: ApprovalGate
    capability: Capability | None = None
    reviewer_capability: Grant
    reason_required: bool = False
    ttl: timedelta = timedelta(hours=24)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.gate is ApprovalGate.ACTION and self.capability is None:
            msg = "ACTION requirements must name a capability"
            raise ValueError(msg)
        if self.gate is not ApprovalGate.ACTION and self.capability is not None:
            msg = f"{self.gate.value} requirements must not name a capability"
            raise ValueError(msg)
        if self.ttl <= timedelta(0):
            msg = "ttl must be positive"
            raise ValueError(msg)
        return self

    @property
    def key(self) -> tuple[ApprovalGate, str | None]:
        """The gate and capability this requirement applies to."""
        return (self.gate, self.capability)


class ExecutionLimits(FrozenModel):
    """Runtime bounds for one execution."""

    timeout: timedelta | None = None
    max_actions: PositiveInt | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.timeout is not None and self.timeout <= timedelta(0):
            msg = "timeout must be positive"
            raise ValueError(msg)
        return self


class Policy(FrozenModel):
    """How and under which conditions work may execute.

    Policies belong to the host application and reach the runner only through a
    ``PolicyResolver``. A work definition never carries a policy.
    """

    id: Identifier
    version: SemVer
    autonomy: AutonomyLevel
    allowed_capabilities: frozenset[Capability] = frozenset()
    approval_requirements: tuple[ApprovalRequirement, ...] = ()
    execution_limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    evidence_requirements: frozenset[EvidenceType] = frozenset()
    require_idempotency_key: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        keys = [requirement.key for requirement in self.approval_requirements]
        if len(keys) != len(set(keys)):
            msg = "approval_requirements contains more than one entry for the same gate"
            raise ValueError(msg)
        return self


class PolicyDecision(FrozenModel):
    """Deterministic result of evaluating a policy for one execution request.

    The model enforces fail-closed shape: a denial grants nothing and always explains
    why; an allowance always names the policy and the effective autonomy.
    """

    allowed: bool
    effective_autonomy: AutonomyLevel | None = None
    granted_capabilities: frozenset[Capability] = frozenset()
    required_gates: tuple[ApprovalRequirement, ...] = ()
    reasons: tuple[str, ...] = ()
    policy_id: Identifier | None = None
    policy_version: SemVer | None = None
    idempotency_key_required: bool = False
    error: ExecutionErrorInfo | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.allowed:
            if self.error is not None:
                msg = "an allowed decision cannot carry an evaluation error"
                raise ValueError(msg)
            if self.effective_autonomy is None or self.policy_id is None:
                msg = "an allowed decision must name the policy and the effective autonomy"
                raise ValueError(msg)
            if self.policy_version is None:
                msg = "an allowed decision must name the policy version"
                raise ValueError(msg)
        else:
            if not self.reasons:
                msg = "a denied decision must state at least one reason"
                raise ValueError(msg)
            if self.granted_capabilities or self.required_gates:
                msg = "a denied decision must not grant capabilities or gates"
                raise ValueError(msg)
            if self.idempotency_key_required:
                msg = "a denied decision has no idempotency requirement"
                raise ValueError(msg)
        return self

    def gate_for(
        self, gate: ApprovalGate, capability: str | None = None
    ) -> ApprovalRequirement | None:
        """The requirement for ``gate`` (and ``capability`` for ACTION), if any."""
        for requirement in self.required_gates:
            if requirement.key == (gate, capability):
                return requirement
        return None
