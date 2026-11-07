"""Attempts to bypass runtime controls from executor, validator or caller code."""

import asyncio
import contextlib
import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    AutonomyLevel,
    ExecutionContext,
    ExecutionStatus,
    Severity,
    UncertainOutcomeError,
    ValidationContext,
    ValidationResult,
)
from id42_work.journal import JournalStatus

S = ExecutionStatus
OK = InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")


async def test_ungranted_capability_fails_even_if_the_executor_catches_it() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        with contextlib.suppress(Exception):
            await context.invoke(PREPARE, action_id="sneak", params={})
        return OK

    env = Env(executor=FunctionExecutor(body))
    execution = await env.run()
    assert execution.status is S.FAILED
    assert execution.status_reason == "capability_denied"
    assert env.provider.count(PREPARE) == 0


async def test_actor_grants_do_not_leak_into_the_context() -> None:
    seen: list[frozenset[str]] = []

    async def body(data: Any, context: ExecutionContext) -> Any:
        seen.append(context.granted_capabilities)
        return OK

    env = Env(executor=FunctionExecutor(body))  # actor also holds PREPARE
    await env.run()
    assert seen == [frozenset({READ, VALIDATE})]


async def test_swallowing_the_suspension_signal_does_not_skip_the_gate() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        with contextlib.suppress(BaseException):  # hostile executor
            await context.invoke(PREPARE, action_id="prepare", params={"amount": Decimal("1")})
        with contextlib.suppress(BaseException):
            await context.invoke(PREPARE, action_id="prepare-again", params={})
        return OK

    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=FunctionExecutor(body),
        policy=make_policy(
            autonomy=AutonomyLevel.SUPERVISED, allowed_capabilities=frozenset({READ, PREPARE})
        ),
    )
    execution = await env.run(idempotency_key="k")
    assert execution.status is S.WAITING_APPROVAL
    assert env.provider.count(PREPARE) == 0
    assert len(await env.runner.approvals_for(execution.execution_id)) == 1


class SideEffectingValidator:
    validator_id = "sneaky"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        await context.invoke(PREPARE, action_id="prepare", params={})
        return ValidationResult(validator_id="sneaky", passed=True, severity=Severity.INFO)


class ReadingValidator:
    validator_id = "supplier_exists"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        assert context.work_id == "supplier_invoice_validation"
        assert context.work_version == "1.0.0"
        supplier = await context.invoke(
            READ, action_id="check-supplier", params={"supplier_id": "SUP-001"}
        )
        passed = isinstance(supplier, dict) and supplier.get("active") is True
        return ValidationResult(validator_id="ignored", passed=passed, severity=Severity.INFO)


async def test_validators_cannot_cause_side_effects() -> None:
    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        validators=[SideEffectingValidator()],
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, PREPARE}),
            require_idempotency_key=False,
        ),
    )
    execution = await env.run()
    assert execution.status is S.VALIDATION_FAILED
    assert env.provider.count(PREPARE) == 0


async def test_validators_may_read_through_granted_capabilities() -> None:
    env = Env(validators=[ReadingValidator()])
    execution = await env.run()
    assert execution.status is S.COMPLETED
    assert execution.outcome is not None
    ids = [r.validator_id for r in execution.outcome.validation_results]
    assert ids == ["schema", "supplier_exists"]  # validator cannot rename its result
    (_, _, call) = env.provider.calls[-1]
    assert call.action_id == "validation:check-supplier"


@pytest.mark.parametrize(
    ("capability", "action_id"),
    [(PREPARE, "x"), ("payments.execute", "x"), (READ, "_bad")],
)
async def test_validator_capability_misuse_fails_closed(capability: str, action_id: str) -> None:
    class Misuse:
        validator_id = "misuse"
        required = True

        async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
            await context.invoke(capability, action_id=action_id, params={})
            return ValidationResult(validator_id="misuse", passed=True, severity=Severity.INFO)

    execution = await Env(validators=[Misuse()]).run()
    assert execution.status is S.VALIDATION_FAILED


@pytest.mark.parametrize(
    ("action_id", "params", "reason"),
    [
        ("_reserved", {}, "invalid_action_id"),
        ("load-supplier", {}, "duplicate_action_id"),
        ("float-params", {"amount": 1.5}, "invalid_action_params"),
    ],
)
async def test_malformed_actions_fail_the_execution(
    action_id: str, params: dict[str, object], reason: str
) -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        await context.invoke(READ, action_id="load-supplier", params={"supplier_id": "S"})
        await context.invoke(READ, action_id=action_id, params=params)
        return OK

    execution = await Env(executor=FunctionExecutor(body)).run()
    assert execution.status is S.FAILED
    assert execution.status_reason == reason


async def test_max_actions_limit() -> None:
    from id42_work import ExecutionLimits  # noqa: PLC0415

    async def body(data: Any, context: ExecutionContext) -> Any:
        for i in range(5):
            await context.invoke(READ, action_id=f"read-{i}", params={"supplier_id": "S"})
        return OK

    policy = make_policy(
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({READ, VALIDATE}),
        execution_limits=ExecutionLimits(max_actions=3),
    )
    env = Env(executor=FunctionExecutor(body), policy=policy)
    execution = await env.run()
    assert execution.status_reason == "max_actions_exceeded"
    assert env.provider.count(READ) == 3


def _prepare_env(**kwargs: Any) -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS, allowed_capabilities=frozenset({READ, PREPARE})
        ),
        executor=kwargs.pop("executor", InvoiceExecutor(prepare=True)),
        **kwargs,
    )


async def test_uncertain_side_effect_requires_reconciliation() -> None:
    env = _prepare_env()

    def uncertain(capability: str, params: Any) -> object:
        if capability == PREPARE:
            raise UncertainOutcomeError("ERP timed out after accepting the request")
        return {"id": "SUP-001", "active": True}

    env.provider.behaviour = uncertain
    execution = await env.run(idempotency_key="k")
    assert execution.status is S.FAILED
    assert execution.status_reason == "uncertain_outcome"
    assert execution.requires_reconciliation
    journal = {j.action_id: j.status for j in await env.runner.journal(execution.execution_id)}
    assert journal["prepare-invoice"] is JournalStatus.UNCERTAIN
    assert "work.action.uncertain" in await env.event_types(execution.execution_id)
    assert env.provider.count(PREPARE) == 1  # never retried


async def test_non_json_result_from_side_effect_requires_reconciliation() -> None:
    env = _prepare_env()

    def decimal_result(capability: str, params: Any) -> object:
        if capability == PREPARE:
            return {"amount": Decimal("1.00")}
        return {"id": "SUP-001", "active": True}

    env.provider.behaviour = decimal_result
    execution = await env.run(idempotency_key="k")
    assert execution.status_reason == "invalid_capability_result"
    assert execution.requires_reconciliation


async def test_cancellation_mid_side_effect_is_recorded_and_propagated() -> None:
    env = _prepare_env()
    started = asyncio.Event()

    async def slow(capability: str, params: Any, call: Any) -> Any:
        if capability == PREPARE:
            started.set()
            await asyncio.sleep(10)
        return {"id": "SUP-001", "active": True}

    env.provider.invoke = slow  # type: ignore[method-assign]
    task = asyncio.create_task(env.run(idempotency_key="k"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = await env.storage.idempotency.get(
        __import__("id42_work").storage.IdempotencyScope(
            tenant_id="acme", work_id=env.work.id, work_version="1.0.0", idempotency_key="k"
        )
    )
    assert record is not None
    execution = await env.runner.get(record.execution_id)
    assert execution is not None
    assert execution.status is S.FAILED
    assert execution.status_reason == "cancelled_in_flight"
    assert execution.requires_reconciliation
    journal = {j.action_id: j.status for j in await env.runner.journal(execution.execution_id)}
    assert journal["prepare-invoice"] is JournalStatus.UNCERTAIN


async def test_timeout_during_side_effect_requires_reconciliation() -> None:
    from datetime import timedelta  # noqa: PLC0415

    from id42_work import ExecutionLimits  # noqa: PLC0415

    env = Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        executor=InvoiceExecutor(prepare=True),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, PREPARE}),
            execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=30)),
        ),
    )

    async def slow(capability: str, params: Any, call: Any) -> Any:
        if capability == PREPARE:
            await asyncio.sleep(5)
        return {"id": "SUP-001", "active": True}

    env.provider.invoke = slow  # type: ignore[method-assign]
    execution = await env.run(idempotency_key="k")
    assert execution.status_reason == "timeout"
    assert execution.requires_reconciliation


async def test_events_never_contain_raw_input_or_results() -> None:
    env = _prepare_env()
    execution = await env.run(idempotency_key="k")
    events = await env.runner.events(execution.execution_id)
    dumped = json.dumps([e.model_dump(mode="json") for e in events])
    for raw in ("INV-1001", "SUP-001", "1250.00", "move_id"):
        assert raw not in dumped
