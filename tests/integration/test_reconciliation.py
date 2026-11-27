"""Provider probes and operator decisions for uncertain capability calls."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from _factories import DIGEST_A, InvoiceOutput, make_policy, make_work
from _runtime import INVOICE, PREPARE, READ, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    AutonomyLevel,
    CapabilityCall,
    EventType,
    EvidenceType,
    ExecutionContext,
    ExecutionError,
    ExecutionStatus,
    ReconciliationConsole,
    ReconciliationError,
    ReconciliationProbe,
    ReconciliationVerdict,
    SqliteStorage,
    StorageError,
    UncertainOutcomeError,
    WorkDefinitionError,
)
from id42_work.canonical import digest
from id42_work.error_info import ExecutionErrorInfo
from id42_work.identifiers import RECORD_CAPABILITY, reconcile_grant
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from id42_work.storage.protocols import Storage
from postgres_support import configured_dsn, create_database, drop_database

S = ExecutionStatus
WORK_ID = "supplier_invoice_validation"
PREPARE_PARAMS = {"invoice": "INV-1001", "amount": Decimal("1250.00")}


def _env(**kwargs: Any) -> Env:
    return Env(
        work=make_work(required_capabilities=frozenset({READ, PREPARE})),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, PREPARE}),
        ),
        executor=kwargs.pop("executor", InvoiceExecutor(prepare=True)),
        **kwargs,
    )


def _raise_once(capability: str, params: object) -> object:
    del params
    if capability == PREPARE:
        raise UncertainOutcomeError("the invoice store did not answer")
    return {"id": "SUP-001", "active": True}


async def _uncertain(env: Env | None = None) -> tuple[Env, UUID, str]:
    chosen = _env() if env is None else env
    chosen.provider.behaviour = _raise_once
    execution = await chosen.run(idempotency_key="bill-1")
    assert execution.status is S.FAILED
    assert execution.requires_reconciliation
    return chosen, execution.execution_id, "prepare-invoice"


def _console(env: Env) -> ReconciliationConsole:
    return ReconciliationConsole(
        env.runner, authority=env.authority, resume_actor=lambda: env.actor
    )


def _operator(env: Env, grants: set[str] | None = None, *, tenant: str = "acme") -> Any:
    return env.authority.issue(
        actor_id="operator",
        tenant_id=tenant,
        grants=set() if grants is None else grants,
        ttl=timedelta(hours=1),
    )


async def _journal(env: Env, execution_id: UUID) -> dict[str, JournalEntry]:
    return {entry.action_id: entry for entry in await env.runner.journal(execution_id)}


async def test_a_provider_probe_that_applied_is_replayed_and_not_repeated() -> None:
    env, execution_id, action_id = await _uncertain()
    before = (await _journal(env, execution_id))[action_id].operation_id

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        assert call.operation_id == before
        assert call.action_id == action_id
        return ReconciliationProbe(
            verdict=ReconciliationVerdict.APPLIED, result={"move_id": 77, "state": "draft"}
        )

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    done = await env.runner.resume(execution_id, actor=env.actor)
    journal = await _journal(env, execution_id)
    evidence = await env.runner.evidence(execution_id)
    types = await env.event_types(execution_id)
    assert done.status is S.COMPLETED
    assert env.provider.count(PREPARE) == 1
    assert journal[action_id].status is JournalStatus.SUCCEEDED
    assert journal[action_id].result == {"move_id": 77, "state": "draft"}
    recorded = [
        item for item in evidence if item.evidence_type is EvidenceType.RECONCILIATION_DECISION
    ]
    assert recorded[-1].source == "reconciliation:provider"
    assert recorded[-1].metadata["source"] == "provider"
    assert recorded[-1].metadata["reason"] == "provider reported the effect as applied"
    assert "reason" not in (await env.runner.events(execution_id))[-2].payload
    assert EventType.ACTION_RECONCILED.value in types
    assert EventType.EXECUTION_REOPENED.value in types


async def test_a_provider_probe_that_did_not_apply_retries_the_same_operation() -> None:
    env, execution_id, action_id = await _uncertain()
    operation = (await _journal(env, execution_id))[action_id].operation_id
    seen: list[UUID] = []

    async def reconcile(call: CapabilityCall) -> ReconciliationVerdict:
        seen.append(call.operation_id)
        return ReconciliationVerdict.NOT_APPLIED

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    env.provider.behaviour = None
    done = await env.runner.resume(execution_id, actor=env.actor)
    journal = await _journal(env, execution_id)
    assert done.status is S.COMPLETED
    assert seen == [operation]
    assert env.provider.count(PREPARE) == 2
    assert env.provider.calls[-1][2].operation_id == operation
    assert journal[action_id].status is JournalStatus.SUCCEEDED


async def test_unknown_or_failing_probes_leave_the_call_uncertain() -> None:
    env, execution_id, action_id = await _uncertain()

    async def unknown(call: CapabilityCall) -> ReconciliationProbe:
        del call
        return ReconciliationProbe(verdict=ReconciliationVerdict.UNKNOWN)

    env.provider.reconcile = unknown  # type: ignore[attr-defined]
    still = await env.runner.resume(execution_id, actor=env.actor)
    assert still.status is S.FAILED
    assert still.requires_reconciliation
    assert env.provider.count(PREPARE) == 1
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.UNCERTAIN

    async def broken(call: CapabilityCall) -> ReconciliationProbe:
        del call
        raise RuntimeError("lookup failed")

    env.provider.reconcile = broken  # type: ignore[attr-defined]
    again = await env.runner.resume(execution_id, actor=env.actor)
    assert again.status is S.FAILED
    assert env.provider.count(PREPARE) == 1


async def test_a_probe_that_is_not_awaitable_or_not_a_verdict_does_not_settle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env, execution_id, _action_id = await _uncertain()

    def sync_probe(call: CapabilityCall) -> ReconciliationProbe:
        del call
        raise RuntimeError("sync failure")

    env.provider.reconcile = sync_probe  # type: ignore[attr-defined]
    with caplog.at_level("WARNING"):
        failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation
    assert "provider reconcile failed" in caplog.text

    def not_awaited(call: CapabilityCall) -> ReconciliationVerdict:
        del call
        return ReconciliationVerdict.APPLIED

    env.provider.reconcile = not_awaited  # type: ignore[attr-defined]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation

    async def invalid(call: CapabilityCall) -> str:
        del call
        return "APPLIED"

    env.provider.reconcile = invalid  # type: ignore[attr-defined]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation

    async def abandoned(call: CapabilityCall) -> ReconciliationVerdict:
        del call
        return ReconciliationVerdict.ABANDONED

    env.provider.reconcile = abandoned  # type: ignore[attr-defined]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation
    assert env.provider.count(PREPARE) == 1


async def test_a_non_json_applied_probe_is_left_unresolved() -> None:
    env, execution_id, action_id = await _uncertain()

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        del call
        return ReconciliationProbe.model_construct(
            verdict=ReconciliationVerdict.APPLIED, result=object()
        )

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.UNCERTAIN


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
async def test_probe_cancellation_signals_propagate(signal: type[BaseException]) -> None:
    env, execution_id, _action_id = await _uncertain()

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        del call
        raise signal

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    with pytest.raises(signal):
        await env.runner.resume(execution_id, actor=env.actor)
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert stored.requires_reconciliation


async def test_cancelled_error_from_a_probe_propagates() -> None:
    env, execution_id, _action_id = await _uncertain()

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        del call
        raise asyncio.CancelledError

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await env.runner.resume(execution_id, actor=env.actor)


async def test_an_inflight_call_is_probed_before_it_is_marked_uncertain(tmp_path: Any) -> None:
    class Halt(BaseException):
        """Stands in for a process death between two journaled actions."""

    path = tmp_path / "work.sqlite"
    storage = SqliteStorage(path)

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await context.invoke(
            READ, action_id="load-supplier", params={"supplier_id": data.supplier_id}
        )
        raise Halt

    first = _env(storage=storage, executor=FunctionExecutor(stop))
    with pytest.raises(Halt):
        await first.run(idempotency_key="crash-1")
    execution = await first.runner.run(
        work_id=first.work.id,
        version=first.work.version,
        input_data=INVOICE,
        actor=first.actor,
        idempotency_key="crash-1",
    )
    assert execution.status is S.RUNNING
    await storage.executions.append_journal(
        JournalEntry(
            execution_id=execution.execution_id,
            action_id="prepare-invoice",
            capability=PREPARE,
            params_digest=digest(PREPARE_PARAMS),
            operation_id=operation_id(execution.execution_id, "prepare-invoice"),
            status=JournalStatus.IN_FLIGHT,
            started_at=first.clock.now,
        )
    )
    storage.close()

    reopened = SqliteStorage(path)
    second = _env(storage=reopened, executor=InvoiceExecutor(prepare=True))

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        del call
        return ReconciliationProbe(verdict=ReconciliationVerdict.APPLIED, result={"move_id": 9})

    second.provider.reconcile = reconcile  # type: ignore[attr-defined]
    try:
        done = await second.runner.resume(execution.execution_id, actor=second.actor)
    finally:
        reopened.close()
    assert done.status is S.COMPLETED
    assert second.provider.count(PREPARE) == 0


async def test_not_applied_with_different_params_fails_closed() -> None:
    env, execution_id, _action_id = await _uncertain()
    console = _console(env)
    await console.mark_not_applied(
        execution_id,
        "prepare-invoice",
        operator=_operator(env, {reconcile_grant(WORK_ID)}),
        reason="the store has no invoice",
    )

    async def drift(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await context.invoke(
            READ, action_id="load-supplier", params={"supplier_id": data.supplier_id}
        )
        await context.invoke(
            PREPARE,
            action_id="prepare-invoice",
            params={"invoice": "OTHER", "amount": data.amount},
        )
        return InvoiceOutput(
            invoice_number=data.invoice_number, supplier_id=data.supplier_id, status="prepared"
        )

    env.registry.register(env.work, executor=FunctionExecutor(drift), replace=True)
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.status is S.FAILED
    assert failed.status_reason == "non_deterministic"
    assert not failed.requires_reconciliation


async def test_an_abandoned_journal_fails_the_replay() -> None:
    env, execution_id, action_id = await _uncertain()
    entry = (await _journal(env, execution_id))[action_id]
    abandoned = JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "status": JournalStatus.ABANDONED,
            "error": ExecutionErrorInfo(type="ReconciliationAbandoned", message="closed"),
        }
    )
    await env.storage.executions.update_journal(abandoned, expected_status=JournalStatus.UNCERTAIN)
    env.runner.reconciliation.blocked = lambda entries: False  # type: ignore[method-assign]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.status is S.FAILED
    assert failed.status_reason == "reconciliation_abandoned"


async def test_operator_applied_records_evidence_and_refuses_a_second_decision() -> None:
    env, execution_id, action_id = await _uncertain()
    console = _console(env)
    operator = _operator(env, {reconcile_grant(WORK_ID)})
    decision = await console.mark_applied(
        execution_id,
        action_id,
        operator=operator,
        reason="  confirmed in the ledger  ",
        result=None,
    )
    assert decision.resumed is False
    assert decision.actor_id == "operator"
    assert decision.entry.status is JournalStatus.SUCCEEDED
    assert decision.entry.result is None
    assert decision.execution.requires_reconciliation
    evidence = await env.runner.evidence(execution_id)
    recorded = [
        item for item in evidence if item.evidence_type is EvidenceType.RECONCILIATION_DECISION
    ]
    assert recorded[-1].metadata["reason"] == "confirmed in the ledger"
    assert recorded[-1].metadata["decision_actor_id"] == "operator"
    assert recorded[-1].metadata["source"] == "operator"
    assert recorded[-1].content_digest == digest(None, floats="repr")
    events = await env.runner.events(execution_id)
    reconciled = [event for event in events if event.event_type is EventType.ACTION_RECONCILED]
    assert "reason" not in reconciled[-1].payload
    with pytest.raises(ReconciliationError, match="not UNCERTAIN"):
        await console.mark_applied(
            execution_id,
            action_id,
            actor_id="operator",
            reason="again",
            result={"move_id": 1},
        )
    assert len(recorded) == len(
        [
            item
            for item in await env.runner.evidence(execution_id)
            if item.evidence_type is EvidenceType.RECONCILIATION_DECISION
        ]
    )


async def test_operator_not_applied_then_resume_retries() -> None:
    env, execution_id, action_id = await _uncertain()
    env.provider.behaviour = None
    decision = await _console(env).mark_not_applied(
        execution_id,
        action_id,
        actor_id="operator",
        reason="nothing was written",
        resume=True,
    )
    assert decision.resumed is True
    assert decision.execution.status is S.COMPLETED
    assert decision.entry.status is JournalStatus.SUCCEEDED
    assert env.provider.count(PREPARE) == 2


async def test_abandon_closes_the_execution_and_refuses_another_decision() -> None:
    env, execution_id, action_id = await _uncertain()
    console = _console(env)
    decision = await console.abandon(
        execution_id, action_id, actor_id="operator", reason="cannot be known"
    )
    assert decision.entry.status is JournalStatus.ABANDONED
    assert decision.execution.status is S.FAILED
    assert decision.execution.status_reason == "reconciliation_abandoned"
    assert not decision.execution.requires_reconciliation
    again = await env.runner.resume(execution_id, actor=env.actor)
    assert again.status is S.FAILED
    assert not again.requires_reconciliation
    with pytest.raises(ReconciliationError, match="closed"):
        await console.mark_not_applied(execution_id, action_id, actor_id="operator", reason="later")
    assert await console.list_open() == ()


async def test_run_of_a_failed_execution_does_not_retry_it() -> None:
    env, execution_id, _action_id = await _uncertain()
    again = await env.run(idempotency_key="bill-1")
    assert again.execution_id == execution_id
    assert again.status is S.FAILED
    assert env.provider.count(PREPARE) == 1


async def test_a_held_claim_blocks_the_decision_and_writes_nothing() -> None:
    env, execution_id, action_id = await _uncertain()
    assert await env.storage.claim_resume(
        execution_id, "holder", now=env.clock(), ttl=timedelta(minutes=5)
    )
    with pytest.raises(ReconciliationError, match="being reconciled or resumed"):
        await _console(env).mark_applied(
            execution_id, action_id, actor_id="operator", reason="confirmed", result={"ok": True}
        )
    evidence = await env.runner.evidence(execution_id)
    assert not any(item.evidence_type is EvidenceType.RECONCILIATION_DECISION for item in evidence)
    await env.storage.release_resume(execution_id, "holder")
    stored = await _console(env).mark_applied(
        execution_id, action_id, actor_id="operator", reason="confirmed", result={"ok": True}
    )
    assert stored.entry.result == {"ok": True}


async def test_a_lost_journal_race_refuses_the_decision() -> None:
    env, execution_id, action_id = await _uncertain()
    from id42_work import ConcurrentModificationError  # noqa: PLC0415

    original = env.storage.executions.update_journal

    async def conflict(entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        del entry, expected_status
        raise ConcurrentModificationError("lost")

    env.storage.executions.update_journal = conflict  # type: ignore[method-assign]
    with pytest.raises(ReconciliationError, match="concurrently"):
        await _console(env).mark_not_applied(
            execution_id, action_id, actor_id="operator", reason="retry"
        )
    env.storage.executions.update_journal = original  # type: ignore[method-assign]
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.UNCERTAIN


async def test_a_lost_journal_race_during_a_probe_stays_uncertain() -> None:
    env, execution_id, action_id = await _uncertain()
    from id42_work import ConcurrentModificationError  # noqa: PLC0415

    async def reconcile(call: CapabilityCall) -> ReconciliationVerdict:
        del call
        return ReconciliationVerdict.APPLIED

    async def conflict(entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        del entry, expected_status
        raise ConcurrentModificationError("lost")

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    env.storage.executions.update_journal = conflict  # type: ignore[method-assign]
    failed = await env.runner.resume(execution_id, actor=env.actor)
    assert failed.requires_reconciliation
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.UNCERTAIN


async def _race(storage: Storage) -> None:
    env = _env(storage=storage)
    _ignored, execution_id, action_id = await _uncertain(env)
    service = env.runner.reconciliation
    actor = _operator(env, {reconcile_grant(WORK_ID)})

    async def decide() -> object:
        return await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.NOT_APPLIED,
            actor=actor,
            reason="one decision",
        )

    outcomes = await asyncio.gather(decide(), decide(), return_exceptions=True)
    successes = [item for item in outcomes if not isinstance(item, Exception)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ReconciliationError)
    evidence = [
        item
        for item in await env.runner.evidence(execution_id)
        if item.evidence_type is EvidenceType.RECONCILIATION_DECISION
    ]
    assert len(evidence) == 1
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.NOT_APPLIED


async def test_one_decision_wins_on_memory_and_sqlite(tmp_path: Any) -> None:
    await _race(_env().storage)
    database = SqliteStorage(tmp_path / "race.sqlite")
    try:
        await _race(database)
    finally:
        database.close()


@pytest.mark.postgres
async def test_one_decision_wins_on_postgres() -> None:
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    from id42_work import PostgresStorage  # noqa: PLC0415

    dsn, name = create_database(admin)
    database = PostgresStorage(dsn)
    try:
        await _race(database)
    finally:
        database.close()
        drop_database(admin, name)


async def test_resume_while_the_claim_is_held_does_not_reopen() -> None:
    env, execution_id, _action_id = await _uncertain()
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert await env.storage.claim_resume(
        execution_id, "holder", now=env.clock(), ttl=timedelta(minutes=5)
    )
    current = await env.runner._reopen_if_reconciled(stored, env.actor)
    assert current.status is S.FAILED
    assert current.requires_reconciliation


async def test_reopen_stops_when_the_execution_moved_or_no_longer_needs_it() -> None:
    env, execution_id, _action_id = await _uncertain()
    stored = await env.runner.get(execution_id)
    assert stored is not None
    original = env.runner._load

    async def ready(execution_id: UUID) -> Any:
        found = await original(execution_id)
        assert found is not None
        return found.model_copy(update={"status": S.READY, "finished_at": None})

    env.runner._load = ready  # type: ignore[method-assign]
    moved = await env.runner._reopen_if_reconciled(stored, env.actor)
    assert moved.status is S.READY
    assert (await original(execution_id)).status is S.FAILED

    async def cleared(execution_id: UUID) -> Any:
        found = await original(execution_id)
        assert found is not None
        assert found.error is not None
        return found.model_copy(
            update={"error": found.error.model_copy(update={"requires_reconciliation": False})}
        )

    env.runner._load = cleared  # type: ignore[method-assign]
    done = await env.runner._reopen_if_reconciled(stored, env.actor)
    assert not done.requires_reconciliation
    assert (await original(execution_id)).requires_reconciliation


async def test_a_replaced_definition_is_refused_only_when_continuation_is_possible() -> None:
    env, execution_id, action_id = await _uncertain()
    replaced = make_work(
        required_capabilities=frozenset({READ, PREPARE}), name="Replaced definition"
    )
    env.registry.register(replaced, executor=env.executor, replace=True)
    unresolved = await env.runner.resume(execution_id, actor=env.actor)
    assert unresolved.status is S.FAILED
    await _console(env).mark_applied(
        execution_id,
        action_id,
        actor_id="operator",
        reason="the effect is in the ledger",
        result={"move_id": 3},
    )
    with pytest.raises(WorkDefinitionError, match="replaced"):
        await env.runner.resume(execution_id, actor=env.actor)
    stored = await env.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.FAILED


async def test_validation_failure_is_probed_on_the_resume_that_can_continue() -> None:
    class Halt(BaseException):
        pass

    class Rule:
        validator_id = "recover_rule"
        required = True

        def __init__(self) -> None:
            self.calls = 0

        async def validate(self, output: object, context: object) -> Any:
            del output, context
            self.calls += 1
            if self.calls == 1:
                raise Halt
            from id42_work import Severity, ValidationResult  # noqa: PLC0415

            return ValidationResult(
                validator_id=self.validator_id,
                passed=True,
                severity=Severity.INFO,
                message="ok",
            )

    rule = Rule()
    env = _env(executor=InvoiceExecutor(prepare=True), validators=[rule])
    with pytest.raises(Halt):
        await env.run(idempotency_key="validating")
    execution = await env.run(idempotency_key="validating")
    assert execution.status is S.VALIDATING
    probed: list[str] = []

    async def reconcile(call: CapabilityCall) -> ReconciliationProbe:
        probed.append(call.action_id)
        return ReconciliationProbe(verdict=ReconciliationVerdict.APPLIED, result={"move_id": 4})

    env.provider.reconcile = reconcile  # type: ignore[attr-defined]
    await env.storage.executions.append_journal(
        JournalEntry(
            execution_id=execution.execution_id,
            action_id="extra-post",
            capability=PREPARE,
            params_digest=DIGEST_A,
            operation_id=operation_id(execution.execution_id, "extra-post"),
            status=JournalStatus.IN_FLIGHT,
            started_at=env.clock.now,
        )
    )
    failed = await env.runner.resume(execution.execution_id, actor=env.actor)
    assert failed.status is S.VALIDATION_FAILED
    assert failed.requires_reconciliation
    assert probed == []
    done = await env.runner.resume(execution.execution_id, actor=env.actor)
    assert probed == ["extra-post"]
    assert done.status is S.COMPLETED
    assert env.provider.count(PREPARE) == 1


async def test_an_inflight_probe_that_reports_uncertain_is_not_marked_again() -> None:
    class Halt(BaseException):
        """Stands in for a process death before the capability call returns."""

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        await context.invoke(
            READ, action_id="load-supplier", params={"supplier_id": data.supplier_id}
        )
        raise Halt

    env = _env(executor=FunctionExecutor(stop))
    with pytest.raises(Halt):
        await env.run(idempotency_key="crash-2")
    execution = await env.run(idempotency_key="crash-2")
    assert execution.status is S.RUNNING
    entry = JournalEntry(
        execution_id=execution.execution_id,
        action_id="prepare-invoice",
        capability=PREPARE,
        params_digest=digest(PREPARE_PARAMS),
        operation_id=operation_id(execution.execution_id, "prepare-invoice"),
        status=JournalStatus.IN_FLIGHT,
        started_at=env.clock.now,
    )
    await env.storage.executions.append_journal(entry)

    async def already(execution: Any, entries: Any, *, actor_id: str) -> list[JournalEntry]:
        del execution, entries, actor_id
        return [entry.model_copy(update={"status": JournalStatus.UNCERTAIN})]

    env.runner.reconciliation.probe = already  # type: ignore[method-assign]
    failed = await env.runner.resume(execution.execution_id, actor=env.actor)
    journal = await _journal(env, execution.execution_id)
    assert failed.status is S.FAILED
    assert failed.requires_reconciliation
    assert journal["prepare-invoice"].status is JournalStatus.IN_FLIGHT


async def test_service_probe_ignores_settled_records_and_missing_entries() -> None:
    env, execution_id, action_id = await _uncertain()
    execution = await env.runner.get(execution_id)
    assert execution is not None
    entry = (await _journal(env, execution_id))[action_id]
    record = JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "action_id": "plan-1",
            "capability": RECORD_CAPABILITY,
            "operation_id": operation_id(execution_id, "plan-1"),
        }
    )
    phantom = JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "action_id": "missing-post",
            "capability": "nowhere.read",
            "status": JournalStatus.IN_FLIGHT,
            "operation_id": operation_id(execution_id, "missing-post"),
        }
    )
    succeeded = JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "status": JournalStatus.SUCCEEDED,
            "result": None,
            "result_digest": digest(None, floats="repr"),
            "finished_at": entry.started_at,
        }
    )
    abandoned = JournalEntry.model_validate(
        {
            **record.model_dump(),
            "status": JournalStatus.ABANDONED,
            "error": ExecutionErrorInfo(type="ReconciliationAbandoned", message="x"),
        }
    )
    service = env.runner.reconciliation
    env.provider.reconcile = "not-callable"  # type: ignore[attr-defined]
    still = await service.probe(execution, [entry], actor_id=env.actor.actor_id)
    assert [item.action_id for item in still] == [action_id]

    async def move(call: CapabilityCall) -> ReconciliationProbe:
        del call
        current = (await _journal(env, execution_id))[action_id]
        updated = JournalEntry.model_validate(
            {
                **current.model_dump(),
                "status": JournalStatus.NOT_APPLIED,
                "error": ExecutionErrorInfo(type="ReconciliationNotApplied", message="moved"),
            }
        )
        await env.storage.executions.update_journal(
            updated, expected_status=JournalStatus.UNCERTAIN
        )
        return ReconciliationProbe(verdict=ReconciliationVerdict.UNKNOWN)

    env.provider.reconcile = move  # type: ignore[attr-defined]
    assert await service.probe(execution, [entry], actor_id=env.actor.actor_id) == []
    skipped = await service.probe(
        execution, [succeeded, record, phantom], actor_id=env.actor.actor_id
    )
    assert skipped == []
    assert service.blocked(()) is False
    assert service.blocked([record.model_copy(update={"status": JournalStatus.IN_FLIGHT})]) is False
    assert service.blocked([phantom]) is True
    assert service.blocked([entry]) is True
    assert service.blocked([abandoned]) is True


async def test_list_and_get_and_refused_decisions() -> None:
    env, execution_id, action_id = await _uncertain()
    service = env.runner.reconciliation
    listed = await service.list_open()
    assert [entry.action_id for _execution, entry in listed] == [action_id]
    shown, entry = await service.get(execution_id, action_id)
    assert shown.execution_id == execution_id
    assert entry.action_id == action_id
    with pytest.raises(ReconciliationError, match="not journaled"):
        await service.get(execution_id, "missing")
    with pytest.raises(ExecutionError, match="does not exist"):
        await service.get(uuid4(), action_id)
    with pytest.raises(ReconciliationError, match="not an operator decision"):
        await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.UNKNOWN,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="no",
        )
    with pytest.raises(ReconciliationError, match="applied requires"):
        await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="confirmed",
        )
    with pytest.raises(ReconciliationError, match="non-blank"):
        await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.NOT_APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="   ",
        )
    with pytest.raises(ReconciliationError, match="at most"):
        await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.NOT_APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="x" * 2001,
        )
    bad_result: Any = object()
    with pytest.raises(ReconciliationError, match="JSON"):
        await service.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="confirmed",
            result=bad_result,
            result_supplied=True,
        )
    record = (await _journal(env, execution_id))[action_id]
    extra = JournalEntry.model_validate(
        {
            **record.model_dump(),
            "action_id": "plan-1",
            "capability": RECORD_CAPABILITY,
            "operation_id": operation_id(execution_id, "plan-1"),
        }
    )
    await env.storage.executions.append_journal(extra)
    with pytest.raises(ReconciliationError, match="recorded computation"):
        await service.decide(
            execution_id,
            "plan-1",
            ReconciliationVerdict.NOT_APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="no",
        )
    inflight = JournalEntry.model_validate(
        {
            **record.model_dump(),
            "action_id": "extra-post",
            "status": JournalStatus.IN_FLIGHT,
            "operation_id": operation_id(execution_id, "extra-post"),
        }
    )
    await env.storage.executions.append_journal(inflight)
    with pytest.raises(ReconciliationError, match="IN_FLIGHT"):
        await service.decide(
            execution_id,
            "extra-post",
            ReconciliationVerdict.NOT_APPLIED,
            actor=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="no",
        )
    rows = await _console(env).list_open()
    assert [row.entry.action_id for row in rows] == [action_id]
    shown_row = await _console(env).show(execution_id, action_id)
    assert shown_row.entry.action_id == action_id


async def test_revise_execution_refuses_a_status_change() -> None:
    env, execution_id, _action_id = await _uncertain()
    stored = await env.runner.get(execution_id)
    assert stored is not None
    with pytest.raises(StorageError, match="cannot change status"):
        await env.runner._rec.revise_execution(
            stored, EventType.ACTION_RECONCILED, {}, status=S.READY
        )


async def test_console_constructors_and_operator_selection() -> None:
    env, execution_id, action_id = await _uncertain()
    with pytest.raises(TypeError, match="WorkRunner"):
        ReconciliationConsole(object(), authority=env.authority, resume_actor=lambda: env.actor)
    with pytest.raises(TypeError, match="ActorAuthority"):
        ReconciliationConsole(env.runner, authority=object(), resume_actor=lambda: env.actor)
    with pytest.raises(TypeError, match="callable"):
        ReconciliationConsole(env.runner, authority=env.authority, resume_actor=object())
    with pytest.raises(ValueError, match="positive"):
        ReconciliationConsole(
            env.runner,
            authority=env.authority,
            resume_actor=lambda: env.actor,
            operator_ttl=timedelta(0),
        )
    console = _console(env)
    with pytest.raises(ReconciliationError, match="not both"):
        await console.abandon(
            execution_id,
            action_id,
            actor_id="operator",
            operator=_operator(env, {reconcile_grant(WORK_ID)}),
            reason="no",
        )
    with pytest.raises(ReconciliationError, match="TrustedActor"):
        await console.abandon(execution_id, action_id, operator=object(), reason="no")
    with pytest.raises(ReconciliationError, match="non-blank"):
        await console.abandon(execution_id, action_id, actor_id="  ", reason="no")
    with pytest.raises(ReconciliationError, match="not a valid operator"):
        await console.abandon(execution_id, action_id, actor_id="x" * 300, reason="no")
    missing = uuid4()
    with pytest.raises(ExecutionError, match="does not exist"):
        await console.show(missing, action_id)
    stored = await env.runner.get(execution_id)
    assert stored is not None

    async def gone(execution_id: UUID) -> None:
        del execution_id

    console._runner.get = gone  # type: ignore[method-assign]
    assert await console._load_execution(execution_id, fallback=stored) is stored
    with pytest.raises(ReconciliationError, match="missing"):
        await console._load_execution(missing)


def test_sync_methods_round_trip() -> None:
    env, execution_id, action_id = asyncio.run(_uncertain())
    console = _console(env)
    assert console.list_open_sync()[0].entry.action_id == action_id
    assert console.show_sync(execution_id, action_id).execution.execution_id == execution_id
    applied = console.mark_applied_sync(
        execution_id,
        action_id,
        actor_id="operator",
        reason="seen",
        result={"move_id": 1},
        resume=False,
    )
    assert applied.entry.status is JournalStatus.SUCCEEDED
    other, other_id, other_action = asyncio.run(_uncertain())
    other.provider.behaviour = None
    other_console = _console(other)
    retried = other_console.mark_not_applied_sync(
        other_id, other_action, actor_id="operator", reason="absent", resume=True
    )
    assert retried.resumed is True
    third, third_id, third_action = asyncio.run(_uncertain())
    closed = _console(third).abandon_sync(
        third_id, third_action, actor_id="operator", reason="stop"
    )
    assert closed.entry.status is JournalStatus.ABANDONED


async def test_sync_methods_refuse_a_running_loop() -> None:
    env, _execution_id, _action_id = await _uncertain()
    console = _console(env)
    with pytest.raises(RuntimeError, match="list_open_sync"):
        console.list_open_sync()


async def test_a_resume_failure_keeps_the_decision() -> None:
    env, execution_id, action_id = await _uncertain()

    def down() -> Any:
        raise RuntimeError("down")

    console = ReconciliationConsole(env.runner, authority=env.authority, resume_actor=down)
    from id42_work import ReconciliationResumeError  # noqa: PLC0415

    with pytest.raises(ReconciliationResumeError, match="resume failed") as info:
        await console.mark_not_applied(
            execution_id, action_id, actor_id="operator", reason="absent", resume=True
        )
    assert info.value.decision.entry.status is JournalStatus.NOT_APPLIED
    assert info.value.decision.resumed is False
    assert (await _journal(env, execution_id))[action_id].status is JournalStatus.NOT_APPLIED


async def test_resume_reloads_the_execution_when_get_misses_it() -> None:
    env, execution_id, action_id = await _uncertain()
    console = _console(env)
    original = env.runner.get
    calls = {"n": 0}

    async def flaky(execution_id: UUID) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            return await original(execution_id)
        return None

    env.runner.get = flaky  # type: ignore[method-assign]
    decision = await console.mark_not_applied(
        execution_id, action_id, actor_id="operator", reason="absent", resume=False
    )
    assert decision.execution.status is S.FAILED
    assert decision.entry.status is JournalStatus.NOT_APPLIED


def test_package_exports_load_on_demand() -> None:
    from id42_work import reconciliation  # noqa: PLC0415

    assert reconciliation.ReconciliationVerdict.APPLIED.value == "APPLIED"
    assert reconciliation.ReconciliationService.__name__ == "ReconciliationService"
    assert reconciliation.ReconciliationConsole.__name__ == "ReconciliationConsole"
    missing = "missing"
    with pytest.raises(AttributeError, match="no attribute"):
        getattr(reconciliation, missing)


async def test_closed_and_finished_executions_are_not_listed_or_decided() -> None:
    env, execution_id, action_id = await _uncertain()
    actor = _operator(env, {reconcile_grant(WORK_ID)})
    with pytest.raises(ReconciliationError, match="not journaled"):
        await env.runner.reconciliation.decide(
            execution_id,
            "missing-action",
            ReconciliationVerdict.NOT_APPLIED,
            actor=actor,
            reason="later",
        )
    entry = (await _journal(env, execution_id))[action_id]
    abandoned = JournalEntry.model_validate(
        {
            **entry.model_dump(),
            "status": JournalStatus.ABANDONED,
            "error": ExecutionErrorInfo(type="ReconciliationAbandoned", message="closed"),
        }
    )
    await env.storage.executions.update_journal(abandoned, expected_status=JournalStatus.UNCERTAIN)
    assert await env.runner.reconciliation.list_open() == ()
    with pytest.raises(ReconciliationError, match="closed"):
        await env.runner.reconciliation.decide(
            execution_id,
            action_id,
            ReconciliationVerdict.NOT_APPLIED,
            actor=actor,
            reason="later",
        )
    finished = _env()
    done = await finished.run(idempotency_key="done")
    assert done.status is S.COMPLETED
    with pytest.raises(ReconciliationError, match="not awaiting"):
        await finished.runner.reconciliation.decide(
            done.execution_id,
            "prepare-invoice",
            ReconciliationVerdict.NOT_APPLIED,
            actor=_operator(finished, {reconcile_grant(WORK_ID)}),
            reason="later",
        )


def test_a_probe_cannot_abandon() -> None:
    with pytest.raises(ValueError, match="cannot abandon"):
        ReconciliationProbe(verdict=ReconciliationVerdict.ABANDONED)
