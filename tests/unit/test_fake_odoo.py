from datetime import timedelta
from uuid import uuid4

import pytest

from id42_work import CapabilityCall, UncertainOutcomeError
from id42_work.journal import operation_id
from id42_work.testing import (
    INVOICE_POST,
    INVOICE_PREPARE,
    INVOICE_READ,
    INVOICE_VALIDATE,
    PARTNER_READ,
    FakeOdooProvider,
    ManualClock,
)


def _call(action_id: str) -> CapabilityCall:
    eid = uuid4()
    return CapabilityCall(
        execution_id=eid,
        action_id=action_id,
        operation_id=operation_id(eid, action_id),
        actor_id="user:1",
    )


async def test_invoice_lifecycle_and_idempotent_side_effects() -> None:
    odoo = FakeOdooProvider()
    assert [s.name for s in odoo.capabilities() if s.side_effecting] == [
        INVOICE_PREPARE,
        INVOICE_VALIDATE,
        INVOICE_POST,
    ]
    partner = await odoo.invoke(PARTNER_READ, {"ref": "SUP-001"}, _call("p"))
    assert partner == {"ref": "SUP-001", "name": "Acme Supplies", "active": True}
    prepare = _call("prepare")
    params = {
        "partner_ref": "SUP-001",
        "invoice_number": "INV-1",
        "amount": "10.50",
        "currency": "BRL",
    }
    move = await odoo.invoke(INVOICE_PREPARE, params, prepare)
    again = await odoo.invoke(INVOICE_PREPARE, params, prepare)
    assert move == again
    assert odoo.count(INVOICE_PREPARE) == 1
    assert isinstance(move, dict)
    move_id = move["move_id"]
    checked = await odoo.invoke(INVOICE_VALIDATE, {"move_id": move_id}, _call("v"))
    assert checked == {"move_id": move_id, "valid": True, "problems": []}
    posted = await odoo.invoke(INVOICE_POST, {"move_id": move_id}, _call("post"))
    assert posted == {"move_id": move_id, "state": "posted", "name": "BILL/0001"}
    read = await odoo.invoke(INVOICE_READ, {"move_id": move_id}, _call("r"))
    assert isinstance(read, dict)
    assert read["state"] == "posted"
    with pytest.raises(ValueError, match="not draft"):
        await odoo.invoke(INVOICE_POST, {"move_id": move_id}, _call("post-2"))


async def test_validation_problems_and_errors() -> None:
    odoo = FakeOdooProvider()
    move = await odoo.invoke(
        INVOICE_PREPARE,
        {"partner_ref": "SUP-001", "invoice_number": "INV-2", "amount": "0", "currency": "BRL"},
        _call("prepare"),
    )
    assert isinstance(move, dict)
    checked = await odoo.invoke(INVOICE_VALIDATE, {"move_id": move["move_id"]}, _call("v"))
    assert checked == {
        "move_id": move["move_id"],
        "valid": False,
        "problems": ["amount must be positive"],
    }
    for capability, params in [
        (PARTNER_READ, {"ref": "nobody"}),
        (INVOICE_READ, {"move_id": "x"}),
        (INVOICE_POST, {"move_id": 999}),
        ("odoo.other", {"move_id": move["move_id"]}),
    ]:
        with pytest.raises(LookupError):
            await odoo.invoke(capability, params, _call("x"))
    with pytest.raises(ValueError, match="invalid amount"):
        await odoo.invoke(INVOICE_PREPARE, {"amount": "abc"}, _call("bad"))


async def test_uncertain_outcome_hook() -> None:
    odoo = FakeOdooProvider()
    odoo.fail_with_uncertain_outcome(INVOICE_POST)
    with pytest.raises(UncertainOutcomeError):
        await odoo.invoke(INVOICE_POST, {"move_id": 1}, _call("post"))
    assert await odoo.invoke(PARTNER_READ, {"ref": "SUP-002"}, _call("p")) == {
        "ref": "SUP-002",
        "name": "Blocked Parts",
        "active": False,
    }


def test_manual_clock() -> None:
    clock = ManualClock()
    start = clock()
    clock.advance(timedelta(minutes=5))
    assert clock() - start == timedelta(minutes=5)
