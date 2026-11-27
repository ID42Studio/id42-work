"""An operator needs an explicit grant and cannot decide its own execution."""

from datetime import timedelta
from uuid import UUID

import pytest

from _factories import make_policy, make_work
from _runtime import PREPARE, READ, Env, InvoiceExecutor
from id42_work import (
    AutonomyLevel,
    EvidenceType,
    ReconciliationConsole,
    ReconciliationError,
    ReconciliationNotAllowedError,
    UncertainOutcomeError,
)
from id42_work.identifiers import reconcile_grant

WORK_ID = "supplier_invoice_validation"


def _env() -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, PREPARE}),
        ),
        executor=InvoiceExecutor(prepare=True),
    )


async def _uncertain() -> tuple[Env, UUID, str]:
    env = _env()

    def fail(capability: str, params: object) -> object:
        del params
        if capability == PREPARE:
            raise UncertainOutcomeError("the invoice store did not answer")
        return {"id": "SUP-001", "active": True}

    env.provider.behaviour = fail
    execution = await env.run(idempotency_key="bill-1")
    return env, execution.execution_id, "prepare-invoice"


def _console(env: Env) -> ReconciliationConsole:
    return ReconciliationConsole(
        env.runner, authority=env.authority, resume_actor=lambda: env.actor
    )


async def test_the_execution_actor_cannot_reconcile_its_own_call() -> None:
    env, execution_id, action_id = await _uncertain()
    with pytest.raises(ReconciliationNotAllowedError, match="own execution"):
        await _console(env).mark_not_applied(
            execution_id, action_id, actor_id=env.actor.actor_id, reason="no"
        )


async def test_another_tenant_is_refused() -> None:
    env, execution_id, action_id = await _uncertain()
    foreign = env.authority.issue(
        actor_id="operator",
        tenant_id="other",
        grants={reconcile_grant(WORK_ID)},
        ttl=timedelta(hours=1),
    )
    with pytest.raises(ReconciliationNotAllowedError, match="another tenant"):
        await _console(env).mark_not_applied(execution_id, action_id, operator=foreign, reason="no")


async def test_a_missing_grant_is_refused_before_a_write() -> None:
    env, execution_id, action_id = await _uncertain()
    weak = env.authority.issue(
        actor_id="operator", tenant_id="acme", grants=set(), ttl=timedelta(hours=1)
    )
    with pytest.raises(ReconciliationNotAllowedError, match="lacks the grant"):
        await _console(env).abandon(execution_id, action_id, operator=weak, reason="no")
    evidence = await env.runner.evidence(execution_id)
    assert all(item.evidence_type is not EvidenceType.RECONCILIATION_DECISION for item in evidence)


async def test_a_second_decision_does_not_write_again() -> None:
    env, execution_id, action_id = await _uncertain()
    console = _console(env)
    await console.mark_applied(
        execution_id, action_id, actor_id="operator", reason="confirmed", result={"ok": True}
    )
    before = await env.runner.evidence(execution_id)
    with pytest.raises(ReconciliationError, match="not UNCERTAIN"):
        await console.mark_applied(
            execution_id, action_id, actor_id="operator", reason="again", result={"ok": False}
        )
    assert await env.runner.evidence(execution_id) == before
