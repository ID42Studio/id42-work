from datetime import timedelta

import pytest
from pydantic import ValidationError

from id42_work import (
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    EvidenceType,
    ExecutionLimits,
    Policy,
    PolicyDecision,
)


def test_autonomy_order() -> None:
    order = [
        AutonomyLevel.MANUAL,
        AutonomyLevel.SUPERVISED,
        AutonomyLevel.GUIDED,
        AutonomyLevel.AUTONOMOUS,
    ]
    shuffled = [order[2], order[0], order[3], order[1]]
    assert sorted(shuffled) == order
    assert min(AutonomyLevel.AUTONOMOUS, AutonomyLevel.GUIDED) is AutonomyLevel.GUIDED
    assert AutonomyLevel.MANUAL <= AutonomyLevel.MANUAL < AutonomyLevel.SUPERVISED
    assert AutonomyLevel.AUTONOMOUS > AutonomyLevel.GUIDED
    with pytest.raises(TypeError):
        _ = AutonomyLevel.MANUAL < "MANUAL"  # type: ignore[operator]


def test_action_requirement_needs_capability() -> None:
    requirement = ApprovalRequirement(
        gate=ApprovalGate.ACTION,
        capability="payments.execute",
        reviewer_capability="approve:payments.execute",
    )
    assert requirement.key == (ApprovalGate.ACTION, "payments.execute")
    with pytest.raises(ValidationError):
        ApprovalRequirement(gate=ApprovalGate.ACTION, reviewer_capability="approve:x")
    with pytest.raises(ValidationError):
        ApprovalRequirement(
            gate=ApprovalGate.START, capability="x.read", reviewer_capability="approve:x"
        )
    with pytest.raises(ValidationError):
        ApprovalRequirement(
            gate=ApprovalGate.START, reviewer_capability="approve:x", ttl=timedelta(0)
        )


def test_execution_limits_must_be_positive() -> None:
    assert ExecutionLimits(timeout=timedelta(seconds=5), max_actions=3).max_actions == 3
    with pytest.raises(ValidationError):
        ExecutionLimits(timeout=timedelta(0))
    with pytest.raises(ValidationError):
        ExecutionLimits(max_actions=0)


def _policy(**overrides: object) -> Policy:
    fields: dict[str, object] = {
        "id": "invoice_review",
        "version": "1.0.0",
        "autonomy": AutonomyLevel.GUIDED,
        "allowed_capabilities": frozenset({"suppliers.read"}),
    }
    fields.update(overrides)
    return Policy.model_validate(fields)


def test_policy_round_trip() -> None:
    policy = _policy(
        evidence_requirements=frozenset({EvidenceType.VALIDATION_REPORT}),
        approval_requirements=(
            ApprovalRequirement(gate=ApprovalGate.OUTCOME, reviewer_capability="approve:w"),
        ),
    )
    assert Policy.model_validate_json(policy.model_dump_json()) == policy


def test_duplicate_requirements_are_rejected() -> None:
    requirement = ApprovalRequirement(gate=ApprovalGate.START, reviewer_capability="approve:w")
    with pytest.raises(ValidationError, match="more than one entry"):
        _policy(approval_requirements=(requirement, requirement))


def test_allowed_decision_names_policy_and_autonomy() -> None:
    decision = PolicyDecision(
        allowed=True,
        effective_autonomy=AutonomyLevel.GUIDED,
        granted_capabilities=frozenset({"suppliers.read"}),
        policy_id="invoice_review",
        policy_version="1.0.0",
    )
    assert decision.allowed
    with pytest.raises(ValidationError):
        PolicyDecision(allowed=True, policy_id="p", policy_version="1.0.0")
    with pytest.raises(ValidationError):
        PolicyDecision(allowed=True, effective_autonomy=AutonomyLevel.MANUAL, policy_id="p")


def test_denied_decision_grants_nothing_and_explains() -> None:
    assert PolicyDecision(allowed=False, reasons=("no_policy",)).reasons == ("no_policy",)
    with pytest.raises(ValidationError, match="reason"):
        PolicyDecision(allowed=False)
    with pytest.raises(ValidationError, match="must not grant"):
        PolicyDecision(
            allowed=False, reasons=("x",), granted_capabilities=frozenset({"suppliers.read"})
        )
