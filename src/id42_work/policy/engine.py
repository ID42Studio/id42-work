"""Deterministic policy engine (specification 6).

The engine never calls an LLM and fails closed: an unknown policy, a missing
capability, a resolver error or any unexpected exception during evaluation produces a
denial. An untrusted actor is not a decision: ``UntrustedActorError`` is raised before
anything is evaluated.
"""

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.contracts import PolicyResolver
from id42_work.definitions import Work
from id42_work.error_info import ExecutionErrorInfo
from id42_work.identifiers import approve_grant
from id42_work.policy.models import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    Policy,
    PolicyDecision,
)
from id42_work.policy.registry import CapabilityRegistry

_logger = logging.getLogger("id42_work.policy")

DEFAULT_APPROVAL_TTL = timedelta(hours=24)

_START_LEVELS = frozenset({AutonomyLevel.MANUAL})
_ACTION_LEVELS = frozenset({AutonomyLevel.MANUAL, AutonomyLevel.SUPERVISED})
_OUTCOME_LEVELS = frozenset({AutonomyLevel.MANUAL, AutonomyLevel.GUIDED})

_GATE_ORDER = {ApprovalGate.START: 0, ApprovalGate.ACTION: 1, ApprovalGate.OUTCOME: 2}


def default_gates(level: AutonomyLevel) -> frozenset[ApprovalGate]:
    """Gates an autonomy level requires by default (specification 3.2)."""
    gates: set[ApprovalGate] = set()
    if level in _START_LEVELS:
        gates.add(ApprovalGate.START)
    if level in _ACTION_LEVELS:
        gates.add(ApprovalGate.ACTION)
    if level in _OUTCOME_LEVELS:
        gates.add(ApprovalGate.OUTCOME)
    return frozenset(gates)


def compute_gates(
    work: Work,
    policy: Policy,
    level: AutonomyLevel,
    capabilities: CapabilityRegistry,
) -> tuple[ApprovalRequirement, ...]:
    """Combine the level's default gates with the policy's explicit requirements.

    Explicit requirements are monotonic: they apply at every autonomy level and replace
    the default reviewer settings of the same gate. ACTION gates only concern
    capabilities the work requires. A capability is sensitive when its provider declares
    it side-effecting or an explicit ACTION requirement names it.
    """
    gates: dict[tuple[ApprovalGate, str | None], ApprovalRequirement] = {}
    defaults = default_gates(level)
    default_reviewer = approve_grant(work.id)

    def add_default(gate: ApprovalGate, capability: str | None = None) -> None:
        gates[(gate, capability)] = ApprovalRequirement(
            gate=gate,
            capability=capability,
            reviewer_capability=default_reviewer,
            ttl=DEFAULT_APPROVAL_TTL,
        )

    if ApprovalGate.START in defaults:
        add_default(ApprovalGate.START)
    if ApprovalGate.ACTION in defaults:
        for capability in sorted(work.required_capabilities):
            if capabilities.is_side_effecting(capability):
                add_default(ApprovalGate.ACTION, capability)
    if ApprovalGate.OUTCOME in defaults:
        add_default(ApprovalGate.OUTCOME)

    for requirement in policy.approval_requirements:
        if (
            requirement.gate is ApprovalGate.ACTION
            and requirement.capability not in work.required_capabilities
        ):
            continue
        gates[requirement.key] = requirement

    return tuple(sorted(gates.values(), key=lambda r: (_GATE_ORDER[r.gate], r.capability or "")))


def idempotency_key_required(work: Work, policy: Policy, capabilities: CapabilityRegistry) -> bool:
    """Whether executions need an idempotency key (specification 10.2)."""
    if policy.require_idempotency_key is not None:
        return policy.require_idempotency_key
    return any(capabilities.is_side_effecting(c) for c in work.required_capabilities)


class PolicyEngine:
    """Evaluates whether a trusted actor may execute a work, and under which gates."""

    def __init__(
        self,
        *,
        resolver: PolicyResolver,
        capabilities: CapabilityRegistry,
        actor_authority: ActorAuthority,
    ) -> None:
        self._resolver = resolver
        self._capabilities = capabilities
        self._authority = actor_authority

    def evaluate(
        self,
        work: Work,
        actor: object,
        *,
        constraints: Iterable[str] | None = None,
        now: datetime | None = None,
    ) -> PolicyDecision:
        """Return the policy decision for ``actor`` executing ``work``.

        ``constraints`` is the optional execution-specific capability bound set by the
        host (``E`` in specification 6.2); ``None`` imposes no extra restriction.

        Raises ``UntrustedActorError`` when ``actor`` is not a valid sealed actor.
        """
        trusted = self._authority.verify(actor, now=now)
        try:
            return self._evaluate(work, trusted, constraints)
        except Exception as error:
            _logger.warning(
                "policy evaluation failed; denying",
                extra={"work.id": work.id, "work.version": work.version},
                exc_info=error,
            )
            return PolicyDecision(
                allowed=False,
                reasons=("policy_error",),
                error=ExecutionErrorInfo.from_exception(error),
            )

    def _evaluate(
        self,
        work: Work,
        actor: TrustedActor,
        constraints: Iterable[str] | None,
    ) -> PolicyDecision:
        policy = self._resolver.resolve(work, actor)
        if policy is None:
            return PolicyDecision(allowed=False, reasons=("no_policy",))
        if not isinstance(policy, Policy):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = f"resolver returned {type(policy).__name__}, expected Policy"
            raise TypeError(msg)

        required = work.required_capabilities
        bound = None if constraints is None else frozenset(constraints)
        reasons: list[str] = []
        for capability in sorted(required):
            if capability not in actor.grants:
                reasons.append(f"missing_capability:{capability}")
            if capability not in policy.allowed_capabilities:
                reasons.append(f"policy_disallows_capability:{capability}")
            if bound is not None and capability not in bound:
                reasons.append(f"constraint_excludes_capability:{capability}")
            if capability not in self._capabilities:
                reasons.append(f"capability_unavailable:{capability}")
        if reasons:
            return PolicyDecision(
                allowed=False,
                reasons=tuple(reasons),
                policy_id=policy.id,
                policy_version=policy.version,
            )

        level = min(policy.autonomy, work.autonomy_ceiling)
        return PolicyDecision(
            allowed=True,
            effective_autonomy=level,
            granted_capabilities=required,
            required_gates=compute_gates(work, policy, level, self._capabilities),
            reasons=(),
            policy_id=policy.id,
            policy_version=policy.version,
            idempotency_key_required=idempotency_key_required(work, policy, self._capabilities),
            execution_limits=policy.execution_limits,
            evidence_requirements=policy.evidence_requirements,
        )
