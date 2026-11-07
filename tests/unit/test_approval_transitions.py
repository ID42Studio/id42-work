from datetime import timedelta
from itertools import product
from typing import Any
from uuid import uuid4

import pytest

from _factories import DIGEST_A, T0
from id42_work import Approval, ApprovalGate, ApprovalStatus, InvalidStateTransitionError
from id42_work.approvals import APPROVAL_TRANSITIONS
from id42_work.state_machine import transition_approval

A = ApprovalStatus
SPEC_TABLE = {
    (A.PENDING, A.GRANTED),
    (A.PENDING, A.REJECTED),
    (A.PENDING, A.EXPIRED),
    (A.PENDING, A.INVALIDATED),
    (A.GRANTED, A.CONSUMED),
    (A.GRANTED, A.EXPIRED),
    (A.GRANTED, A.INVALIDATED),
}
DECISION: dict[str, Any] = {"decided_at": T0, "decision_actor_id": "reviewer:7"}


def pending(**overrides: Any) -> Approval:
    fields: dict[str, Any] = {
        "execution_id": uuid4(),
        "gate": ApprovalGate.ACTION,
        "action_id": "post-invoice",
        "capability": "odoo.invoice.post",
        "action_digest": DIGEST_A,
        "reviewer_capability": "approve:odoo.invoice.post",
        "requested_at": T0,
        "expires_at": T0 + timedelta(hours=1),
    }
    fields.update(overrides)
    return Approval(**fields)


def in_status(status: ApprovalStatus) -> Approval:
    extra: dict[str, Any] = {}
    if status in {A.GRANTED, A.REJECTED, A.CONSUMED}:
        extra.update(DECISION)
    if status is A.REJECTED:
        extra["reason"] = "no"
    if status is A.CONSUMED:
        extra["consumed_at"] = T0
    return pending(status=status, **extra)


def changes_for(source: ApprovalStatus, target: ApprovalStatus) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if source is A.PENDING and target in {A.GRANTED, A.REJECTED}:
        changes.update(DECISION)
    if target is A.REJECTED:
        changes["reason"] = "amount mismatch"
    if target is A.CONSUMED:
        changes["consumed_at"] = T0 + timedelta(minutes=1)
    return changes


def test_table_matches_specification() -> None:
    implemented = {(a, b) for a, targets in APPROVAL_TRANSITIONS.items() for b in targets}
    assert implemented == SPEC_TABLE


@pytest.mark.parametrize(("source", "target"), sorted(SPEC_TABLE, key=str))
def test_allowed(source: ApprovalStatus, target: ApprovalStatus) -> None:
    moved = transition_approval(in_status(source), target, **changes_for(source, target))
    assert moved.status is target


@pytest.mark.parametrize(
    ("source", "target"),
    [pair for pair in product(A, A) if pair not in SPEC_TABLE],
    ids=lambda s: s.value,
)
def test_forbidden(source: ApprovalStatus, target: ApprovalStatus) -> None:
    with pytest.raises(InvalidStateTransitionError, match="approval state transition"):
        transition_approval(in_status(source), target, **changes_for(source, target))


def test_digest_and_binding_cannot_change() -> None:
    with pytest.raises(
        InvalidStateTransitionError, match="protected fields cannot change: action_digest"
    ):
        transition_approval(pending(), A.GRANTED, action_digest="c" * 64, **DECISION)
    with pytest.raises(InvalidStateTransitionError, match="protected"):
        transition_approval(pending(), A.GRANTED, expires_at=T0 + timedelta(days=9), **DECISION)


def test_decision_invariants_are_enforced() -> None:
    with pytest.raises(InvalidStateTransitionError, match="reason"):
        transition_approval(pending(), A.REJECTED, **DECISION)
    with pytest.raises(InvalidStateTransitionError, match="record the decision"):
        transition_approval(pending(), A.GRANTED)
    with pytest.raises(InvalidStateTransitionError, match="requires a reason"):
        transition_approval(pending(reason_required=True), A.GRANTED, **DECISION)
