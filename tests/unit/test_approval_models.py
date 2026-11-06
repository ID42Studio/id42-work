from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from _factories import DIGEST_A, T0
from pydantic import ValidationError

from id42_work import Approval, ApprovalGate, ApprovalStatus
from id42_work.approvals import OUTCOME_ACTION_ID, START_ACTION_ID, compute_action_digest

EXECUTION_ID = uuid4()


def _digest(**overrides: Any) -> str:
    fields: dict[str, Any] = {
        "execution_id": EXECUTION_ID,
        "work_id": "supplier_invoice_validation",
        "work_version": "1.0.0",
        "definition_digest": DIGEST_A,
        "gate": ApprovalGate.ACTION,
        "action_id": "post-invoice",
        "capability": "odoo.invoice.post",
        "params": {"invoice": "INV-1001", "amount": Decimal("1250.00")},
    }
    fields.update(overrides)
    return compute_action_digest(**fields)


def test_action_digest_binds_every_part() -> None:
    base = _digest()
    assert _digest() == base
    variants = [
        {"params": {"invoice": "INV-1001", "amount": Decimal("1300.00")}},
        {"params": {"invoice": "INV-1001", "amount": Decimal("1250.0")}},
        {"execution_id": uuid4()},
        {"work_version": "1.0.1"},
        {"definition_digest": "c" * 64},
        {"action_id": "post-invoice-2"},
        {"capability": "odoo.invoice.validate"},
        {"gate": ApprovalGate.START},
    ]
    assert len({_digest(**v) for v in variants} | {base}) == len(variants) + 1


def test_action_digest_rejects_float_money() -> None:
    with pytest.raises(TypeError, match="float"):
        _digest(params={"amount": 1250.0})


def _approval(**overrides: Any) -> Approval:
    fields: dict[str, Any] = {
        "execution_id": EXECUTION_ID,
        "gate": ApprovalGate.ACTION,
        "action_id": "post-invoice",
        "capability": "odoo.invoice.post",
        "action_digest": _digest(),
        "reviewer_capability": "approve:odoo.invoice.post",
        "requested_at": T0,
        "expires_at": T0 + timedelta(hours=1),
    }
    fields.update(overrides)
    return Approval(**fields)


def test_pending_approval() -> None:
    approval = _approval()
    assert approval.status is ApprovalStatus.PENDING
    assert not approval.is_usable_for(_digest(), T0)
    assert Approval.model_validate_json(approval.model_dump_json()) == approval


def test_granted_approval_is_bound_to_digest_and_window() -> None:
    approval = _approval(
        status=ApprovalStatus.GRANTED, decided_at=T0, decision_actor_id="reviewer:7"
    )
    assert approval.is_usable_for(_digest(), T0 + timedelta(minutes=5))
    altered = _digest(params={"invoice": "INV-1001", "amount": Decimal("1300.00")})
    assert not approval.is_usable_for(altered, T0)
    assert approval.is_expired(T0 + timedelta(hours=1))
    assert not approval.is_usable_for(_digest(), T0 + timedelta(hours=1))


@pytest.mark.parametrize(
    "overrides",
    [
        {"gate": ApprovalGate.START},
        {"gate": ApprovalGate.START, "action_id": START_ACTION_ID},
        {"gate": ApprovalGate.OUTCOME, "action_id": OUTCOME_ACTION_ID},
        {"action_id": "__start__"},
        {"capability": None},
        {"expires_at": T0},
        {"status": ApprovalStatus.GRANTED},
        {"decided_at": T0},
        {"decided_at": T0, "decision_actor_id": "r"},
        {"status": ApprovalStatus.REJECTED, "decided_at": T0, "decision_actor_id": "r"},
        {
            "status": ApprovalStatus.GRANTED,
            "decided_at": T0,
            "decision_actor_id": "r",
            "reason_required": True,
        },
        {"status": ApprovalStatus.CONSUMED, "decided_at": T0, "decision_actor_id": "r"},
        {"consumed_at": T0},
        {
            "status": ApprovalStatus.GRANTED,
            "decided_at": T0 - timedelta(seconds=1),
            "decision_actor_id": "r",
        },
    ],
)
def test_invalid_approvals(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _approval(**overrides)


def test_gate_specific_action_ids() -> None:
    start = _approval(gate=ApprovalGate.START, action_id=START_ACTION_ID, capability=None)
    outcome = _approval(gate=ApprovalGate.OUTCOME, action_id=OUTCOME_ACTION_ID, capability=None)
    assert start.gate is ApprovalGate.START
    assert outcome.gate is ApprovalGate.OUTCOME


def test_consumed_and_rejected_records() -> None:
    consumed = _approval(
        status=ApprovalStatus.CONSUMED,
        decided_at=T0,
        decision_actor_id="r",
        consumed_at=T0 + timedelta(minutes=1),
    )
    rejected = _approval(
        status=ApprovalStatus.REJECTED, decided_at=T0, decision_actor_id="r", reason="no"
    )
    expired = _approval(status=ApprovalStatus.EXPIRED)
    assert consumed.consumed_at is not None
    assert rejected.reason == "no"
    assert not expired.is_usable_for(_digest(), T0)
