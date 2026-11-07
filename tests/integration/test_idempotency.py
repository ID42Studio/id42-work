"""Idempotency (specification 10)."""

import pytest

from _factories import make_policy, make_work
from _runtime import INVOICE, PREPARE, READ, VALIDATE, Env, InvoiceExecutor
from id42_work import AutonomyLevel, ExecutionStatus, IdempotencyConflictError, InputValidationError

S = ExecutionStatus


def _side_effecting_env(**policy_overrides: object) -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
            **policy_overrides,
        ),
    )


async def test_retry_returns_the_same_execution_without_side_effects() -> None:
    env = _side_effecting_env()
    first = await env.run(idempotency_key="invoice-SUP-001-INV-1001")
    second = await env.run(idempotency_key="invoice-SUP-001-INV-1001")
    assert first.status is S.COMPLETED
    assert second == first
    assert env.provider.count(PREPARE) == 1
    assert env.executor.runs == 1
    (_, _, call) = next(c for c in env.provider.calls if c[0] == PREPARE)
    assert call.execution_id == first.execution_id
    assert call.action_id == "prepare-invoice"


async def test_same_key_with_different_input_conflicts() -> None:
    env = _side_effecting_env()
    first = await env.run(idempotency_key="k")
    with pytest.raises(IdempotencyConflictError) as info:
        await env.run({**INVOICE, "amount": "1300.00"}, idempotency_key="k")
    assert info.value.execution_id == first.execution_id
    with pytest.raises(IdempotencyConflictError):
        await env.run({**INVOICE, "amount": "1250.0"}, idempotency_key="k")  # representation counts


async def test_keys_are_scoped_by_tenant() -> None:
    env = _side_effecting_env()
    other_tenant = env.authority.issue(
        actor_id="user:9",
        tenant_id="globex",
        grants={READ, VALIDATE, PREPARE},
        ttl=__import__("datetime").timedelta(hours=1),
    )
    first = await env.run(idempotency_key="k")
    second = await env.run({**INVOICE, "amount": "9.00"}, idempotency_key="k", actor=other_tenant)
    assert first.execution_id != second.execution_id


async def test_key_required_for_side_effecting_work() -> None:
    env = _side_effecting_env()
    with pytest.raises(InputValidationError, match="idempotency_key is required"):
        await env.run()
    assert env.executor.runs == 0


async def test_policy_may_waive_or_demand_the_key() -> None:
    waived = _side_effecting_env(require_idempotency_key=False)
    assert (await waived.run()).status is S.COMPLETED
    demanding = Env(
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE}),
            require_idempotency_key=True,
        )
    )
    with pytest.raises(InputValidationError):
        await demanding.run()


async def test_read_only_work_without_key_is_not_deduplicated() -> None:
    env = Env()
    first = await env.run()
    second = await env.run()
    assert first.execution_id != second.execution_id


@pytest.mark.parametrize("key", ["", "x" * 257])
async def test_malformed_keys_are_rejected(key: str) -> None:
    with pytest.raises(InputValidationError, match="idempotency_key"):
        await Env().run(idempotency_key=key)


async def test_denied_executions_are_idempotent_too() -> None:
    env = Env(no_policy=True)
    first = await env.run(idempotency_key="k")
    assert first.status is S.DENIED
    assert (await env.run(idempotency_key="k")) == first
