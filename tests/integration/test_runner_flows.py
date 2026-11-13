"""End-to-end runner flows with in-memory storage (specification 5.5, 16)."""

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from _factories import InvoiceInput, InvoiceOutput, make_policy, make_work
from _runtime import (
    INVOICE,
    PREPARE,
    READ,
    VALIDATE,
    AmountRule,
    Env,
    FunctionExecutor,
    InvoiceExecutor,
)
from id42_work import (
    ApprovalGate,
    ApprovalRequiredError,
    ApprovalStatus,
    AutonomyLevel,
    EvidenceType,
    ExecutionContext,
    ExecutionFailedError,
    ExecutionStatus,
    InputValidationError,
    OutcomeValidationError,
    PolicyDeniedError,
    UntrustedActorError,
    WorkRegistrationError,
)
from id42_work.journal import JournalStatus

S = ExecutionStatus


def _policy(level: AutonomyLevel, **overrides: Any):
    return make_policy(
        autonomy=level, allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}), **overrides
    )


async def test_autonomous_execution_completes_with_evidence() -> None:
    env = Env()
    execution = await env.run()
    assert execution.status is S.COMPLETED
    outcome = execution.raise_for_status()
    assert outcome.verified
    assert outcome.accepted_at is not None
    assert outcome.output == InvoiceOutput(
        invoice_number="INV-1001", supplier_id="SUP-001", status="valid"
    )
    assert [r.validator_id for r in outcome.validation_results] == ["schema"]
    assert await env.event_types(execution.execution_id) == [
        "work.execution.created",
        "work.policy.evaluated",
        "work.execution.authorized",
        "work.execution.started",
        "work.action.invoked",
        "work.action.completed",
        "work.outcome.validation_started",
        "work.outcome.validation_passed",
        "work.execution.completed",
    ]
    events = await env.runner.events(execution.execution_id)
    assert [e.sequence_number for e in events] == list(range(1, len(events) + 1))
    assert execution.last_event_sequence == len(events)
    evidence = await env.runner.evidence(execution.execution_id)
    assert [e.evidence_type for e in evidence] == [
        EvidenceType.INPUT_REFERENCE,
        EvidenceType.TOOL_RESULT,
        EvidenceType.VALIDATION_REPORT,
    ]
    assert set(execution.evidence_ids) == {e.evidence_id for e in evidence}
    journal = await env.runner.journal(execution.execution_id)
    assert [(j.action_id, j.status) for j in journal] == [
        ("load-supplier", JournalStatus.SUCCEEDED)
    ]
    assert await env.runner.get(execution.execution_id) == execution


async def test_input_may_be_a_model_and_is_revalidated() -> None:
    env = Env()
    model = InvoiceInput.model_validate(INVOICE)
    assert (await env.run(model)).status is S.COMPLETED
    forged = InvoiceInput.model_construct(
        invoice_number="X", supplier_id="S", currency="BRL", amount="bad"
    )
    with pytest.raises(InputValidationError):
        await env.run(forged)


@pytest.mark.parametrize(
    "bad_input",
    [{"invoice_number": "INV-1"}, {**INVOICE, "amount": "not-a-number"}, "string"],
)
async def test_invalid_input_raises_before_creating_an_execution(bad_input: Any) -> None:
    env = Env()
    seen: list[object] = []
    env.runner.dispatcher.subscribe(seen.append)
    with pytest.raises(InputValidationError) as info:
        await env.run(bad_input)
    assert info.value.__cause__ is not None
    assert seen == []


async def test_float_input_cannot_be_fingerprinted() -> None:
    class FloatInput(BaseModel):
        value: float

    async def body(data: Any, context: ExecutionContext) -> InvoiceOutput:
        raise AssertionError

    env = Env(work=make_work(input_schema=FloatInput), executor=FunctionExecutor(body))
    with pytest.raises(InputValidationError, match="Decimal"):
        await env.run({"value": 1.5})


async def test_untrusted_actor_and_unknown_work_raise() -> None:
    env = Env()
    with pytest.raises(UntrustedActorError):
        await env.run(actor="user:42")
    with pytest.raises(WorkRegistrationError):
        await env.runner.run(
            work_id="unknown", version="1.0.0", input_data=INVOICE, actor=env.actor
        )
    with pytest.raises(WorkRegistrationError):
        await env.runner.run(work_id="Not Valid", version="x", input_data=INVOICE, actor=env.actor)


async def test_no_policy_denies_without_running() -> None:
    env = Env(no_policy=True)
    execution = await env.run()
    assert execution.status is S.DENIED
    assert execution.policy_reasons == ("no_policy",)
    assert execution.granted_capabilities == frozenset()
    assert env.executor.runs == 0
    with pytest.raises(PolicyDeniedError):
        execution.raise_for_status()
    assert await env.event_types(execution.execution_id) == [
        "work.execution.created",
        "work.policy.evaluated",
        "work.execution.denied",
    ]


async def test_missing_capability_denies() -> None:
    env = Env(grants=(READ,))
    execution = await env.run()
    assert execution.status is S.DENIED
    assert execution.policy_reasons == ("missing_capability:invoices.validate",)
    assert execution.policy_id == "invoice_review"


async def test_constraints_narrow_the_execution() -> None:
    env = Env()
    execution = await env.run(constraints={READ})
    assert execution.policy_reasons == ("constraint_excludes_capability:invoices.validate",)


async def test_manual_waits_for_start_approval() -> None:
    env = Env(policy=_policy(AutonomyLevel.MANUAL))
    execution = await env.run()
    assert execution.status is S.WAITING_APPROVAL
    assert execution.status_reason == "awaiting_start_approval"
    assert env.executor.runs == 0
    (approval,) = await env.runner.approvals_for(execution.execution_id)
    assert approval.gate is ApprovalGate.START
    assert approval.status is ApprovalStatus.PENDING
    assert approval.reviewer_capability == "approve:supplier_invoice_validation"
    assert approval.params_preview["invoice_number"] == "INV-1001"
    with pytest.raises(ApprovalRequiredError):
        execution.raise_for_status()


async def test_supervised_suspends_before_side_effect() -> None:
    work = make_work(required_capabilities=frozenset({READ, PREPARE}))
    env = Env(
        policy=_policy(AutonomyLevel.SUPERVISED),
        work=work,
        executor=InvoiceExecutor(prepare=True),
    )
    execution = await env.run(idempotency_key="k-1")
    assert execution.status is S.WAITING_APPROVAL
    assert execution.status_reason == "awaiting_action_approval"
    assert env.provider.count(READ) == 1
    assert env.provider.count(PREPARE) == 0
    journal = {j.action_id: j.status for j in await env.runner.journal(execution.execution_id)}
    assert journal == {
        "load-supplier": JournalStatus.SUCCEEDED,
        "prepare-invoice": JournalStatus.PENDING_APPROVAL,
    }
    (approval,) = await env.runner.approvals_for(execution.execution_id)
    assert approval.gate is ApprovalGate.ACTION
    assert approval.capability == PREPARE
    assert approval.params_preview == {"amount": "1250.00", "invoice": "INV-1001"}


async def test_guided_waits_for_outcome_review() -> None:
    env = Env(policy=_policy(AutonomyLevel.GUIDED))
    execution = await env.run()
    assert execution.status is S.WAITING_REVIEW
    assert execution.outcome is not None
    assert execution.outcome.verified
    assert execution.outcome.accepted_at is None
    (approval,) = await env.runner.approvals_for(execution.execution_id)
    assert approval.gate is ApprovalGate.OUTCOME
    assert approval.params_preview["status"] == "valid"
    types = await env.event_types(execution.execution_id)
    assert types[-2:] == ["work.approval.requested", "work.outcome.review_requested"]


async def test_failed_validator_blocks_completion() -> None:
    env = Env(validators=[AmountRule()])
    execution = await env.run({**INVOICE, "amount": "-5.00"})
    assert execution.status is S.VALIDATION_FAILED
    assert execution.status_reason == "validation_failed"
    assert execution.outcome is not None
    assert not execution.outcome.verified
    assert [r.validator_id for r in execution.outcome.failed_results] == ["amount_rule"]
    with pytest.raises(OutcomeValidationError):
        execution.raise_for_status()


async def test_advisory_validator_does_not_block() -> None:
    env = Env(validators=[AmountRule(required=False, minimum=Decimal(10_000))])
    execution = await env.run()
    assert execution.status is S.COMPLETED
    assert execution.outcome is not None
    assert [r.validator_id for r in execution.outcome.failed_results] == ["amount_rule"]


class RaisingValidator:
    validator_id = "raising"
    required = True

    async def validate(self, output: BaseModel, context: Any) -> Any:
        raise RuntimeError("rules service down")


class WrongTypeValidator:
    validator_id = "wrong_type"
    required = True

    async def validate(self, output: BaseModel, context: Any) -> Any:
        return True


@pytest.mark.parametrize("validator", [RaisingValidator(), WrongTypeValidator()])
async def test_broken_validators_fail_closed(validator: Any) -> None:
    env = Env(validators=[validator])
    execution = await env.run()
    assert execution.status is S.VALIDATION_FAILED
    assert execution.outcome is not None
    (failed,) = execution.outcome.failed_results
    assert failed.validator_id == validator.validator_id


class HangingValidator:
    validator_id = "hanging"

    def __init__(self, *, required: bool = True) -> None:
        self.required = required

    async def validate(self, output: BaseModel, context: Any) -> Any:
        await asyncio.sleep(5)


async def test_hanging_validator_is_bounded_by_the_execution_timeout() -> None:
    from datetime import timedelta  # noqa: PLC0415

    from id42_work import ExecutionLimits  # noqa: PLC0415

    policy = _policy(
        AutonomyLevel.AUTONOMOUS,
        execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=20)),
    )
    env = Env(policy=policy, validators=[HangingValidator(), AmountRule()])
    execution = await asyncio.wait_for(env.run(), timeout=2)
    assert execution.status is S.VALIDATION_FAILED
    assert execution.status_reason == "validation_failed"
    assert execution.outcome is not None
    failed = {r.validator_id: r.message for r in execution.outcome.failed_results}
    assert failed == {"hanging": "validator timed out (execution_limits.timeout)"}


async def test_hanging_advisory_validator_does_not_block() -> None:
    from datetime import timedelta  # noqa: PLC0415

    from id42_work import ExecutionLimits  # noqa: PLC0415

    policy = _policy(
        AutonomyLevel.AUTONOMOUS,
        execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=20)),
    )
    env = Env(policy=policy, validators=[HangingValidator(required=False)])
    execution = await asyncio.wait_for(env.run(), timeout=2)
    assert execution.status is S.COMPLETED
    assert execution.outcome is not None
    assert [r.validator_id for r in execution.outcome.failed_results] == ["hanging"]


async def test_validator_raising_timeout_error_itself_is_reported_as_raised() -> None:
    class RemoteTimeout:
        validator_id = "remote"
        required = True

        async def validate(self, output: BaseModel, context: Any) -> Any:
            raise TimeoutError("rules service timed out")

    execution = await Env(validators=[RemoteTimeout()]).run()
    assert execution.outcome is not None
    (failed,) = execution.outcome.failed_results
    assert failed.message == "validator raised TimeoutError"


class ExtendedOutput(InvoiceOutput):
    internal_note: str = "x"


@pytest.mark.parametrize(
    "returned",
    [
        InvoiceOutput.model_construct(invoice_number="INV-1001", supplier_id="SUP-001"),
        ExtendedOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid"),
        {"invoice_number": "INV-1001"},
        None,
    ],
)
async def test_invalid_output_fails_schema_validation(returned: Any) -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        return returned

    env = Env(executor=FunctionExecutor(body))
    execution = await env.run()
    assert execution.status is S.VALIDATION_FAILED
    assert execution.status_reason == "output_schema_invalid"
    assert execution.outcome is None


async def test_dict_output_matching_the_schema_is_accepted() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        return {"invoice_number": "INV-1001", "supplier_id": "SUP-001", "status": "valid"}

    assert (await Env(executor=FunctionExecutor(body)).run()).status is S.COMPLETED


async def test_executor_exception_is_recorded_with_its_cause() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        try:
            raise KeyError("supplier")
        except KeyError as error:
            raise ValueError("lookup failed") from error

    env = Env(executor=FunctionExecutor(body))
    execution = await env.run()
    assert execution.status is S.FAILED
    assert execution.status_reason == "executor_error"
    assert execution.error is not None
    assert execution.error.type == "ValueError"
    assert [c.type for c in execution.error.causes] == ["KeyError"]
    assert not execution.requires_reconciliation
    with pytest.raises(ExecutionFailedError, match="lookup failed"):
        execution.raise_for_status()


async def test_timeout_fails_the_execution() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        await asyncio.sleep(5)

    from datetime import timedelta  # noqa: PLC0415

    from id42_work import ExecutionLimits  # noqa: PLC0415

    policy = _policy(
        AutonomyLevel.AUTONOMOUS,
        execution_limits=ExecutionLimits(timeout=timedelta(milliseconds=20)),
    )
    execution = await Env(policy=policy, executor=FunctionExecutor(body)).run()
    assert execution.status is S.FAILED
    assert execution.status_reason == "timeout"


async def test_executor_raising_timeout_error_itself_is_an_executor_error() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        raise TimeoutError("remote timed out")

    execution = await Env(executor=FunctionExecutor(body)).run()
    assert execution.status_reason == "executor_error"


async def test_executor_evidence_is_persisted_and_redacted() -> None:
    async def body(data: Any, context: ExecutionContext) -> Any:
        context.add_evidence(
            EvidenceType.OUTPUT_ARTIFACT,
            content_reference="s3://bucket/report.pdf",
            metadata={"pages": 3, "api_token": "abc123"},
        )
        context.logger.info("artifact stored")
        assert context.work_id == "supplier_invoice_validation"
        assert context.work_version == "1.0.0"
        assert context.actor_id == "user:42"
        assert context.tenant_id == "acme"
        assert context.granted_capabilities == {READ, VALIDATE}
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="valid")

    env = Env(executor=FunctionExecutor(body))
    execution = await env.run()
    artifact = next(
        e
        for e in await env.runner.evidence(execution.execution_id)
        if e.evidence_type is EvidenceType.OUTPUT_ARTIFACT
    )
    assert artifact.source == "executor"
    assert artifact.metadata == {"pages": 3, "api_token": "***"}
    assert artifact.evidence_id in execution.evidence_ids


async def test_evidence_requirements() -> None:
    required = frozenset({EvidenceType.EXTERNAL_TRANSACTION})
    env = Env(policy=_policy(AutonomyLevel.AUTONOMOUS, evidence_requirements=required))
    execution = await env.run()
    assert execution.status is S.VALIDATION_FAILED
    assert execution.status_reason == "evidence_missing:EXTERNAL_TRANSACTION"

    decision_needed = frozenset({EvidenceType.APPROVAL_DECISION, EvidenceType.TOOL_RESULT})
    guided = Env(policy=_policy(AutonomyLevel.GUIDED, evidence_requirements=decision_needed))
    assert (await guided.run()).status is S.WAITING_REVIEW


async def test_provider_error_caught_by_executor() -> None:
    env = Env()

    def boom(capability: str, params: Any) -> object:
        raise ConnectionError("supplier api down")

    env.provider.behaviour = boom

    async def body(data: Any, context: ExecutionContext) -> Any:
        with contextlib.suppress(ConnectionError):
            await context.invoke(READ, action_id="load", params={"supplier_id": "SUP-001"})
        return InvoiceOutput(invoice_number="INV-1001", supplier_id="SUP-001", status="unverified")

    env2 = Env(executor=FunctionExecutor(body))
    env2.provider.behaviour = boom
    execution = await env2.run()
    assert execution.status is S.COMPLETED
    (entry,) = await env2.runner.journal(execution.execution_id)
    assert entry.status is JournalStatus.FAILED
    assert entry.error is not None
    assert entry.error.type == "ConnectionError"
    assert "work.action.failed" in await env2.event_types(execution.execution_id)


async def test_results_with_floats_are_journaled() -> None:
    env = Env()
    env.provider.behaviour = lambda capability, params: {"id": "SUP-001", "score": 0.97}
    execution = await env.run()
    assert execution.status is S.COMPLETED
    (entry,) = await env.runner.journal(execution.execution_id)
    assert entry.result == {"id": "SUP-001", "score": 0.97}


async def test_sync_api() -> None:
    env = Env()
    with pytest.raises(RuntimeError, match="running event loop"):
        env.runner.run_sync(
            work_id=env.work.id, version="1.0.0", input_data=INVOICE, actor=env.actor
        )


def test_sync_api_outside_a_loop() -> None:
    env = Env()
    execution = env.runner.run_sync(
        work_id=env.work.id, version="1.0.0", input_data=INVOICE, actor=env.actor
    )
    assert execution.status is S.COMPLETED


async def test_subscriber_failures_do_not_affect_execution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = Env()
    received: list[str] = []

    def broken(event: Any) -> None:
        raise RuntimeError("subscriber bug")

    env.runner.dispatcher.subscribe(broken)
    env.runner.dispatcher.subscribe(lambda e: received.append(e.event_type.value))
    execution = await env.run()
    assert execution.status is S.COMPLETED
    assert received[-1] == "work.execution.completed"
    assert "event subscriber failed" in caplog.text
