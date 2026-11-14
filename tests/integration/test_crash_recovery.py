"""Resume after a restart: replay, uncertain side effects, and durable approvals."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from _factories import InvoiceOutput, make_policy, make_work
from _runtime import PREPARE, READ, VALIDATE, Env, FunctionExecutor, InvoiceExecutor
from id42_work import (
    ApprovalStatus,
    AutonomyLevel,
    ExecutionContext,
    ExecutionStatus,
    SqliteStorage,
)
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from recovery_worker import PUBLISH, make_runner
from recovery_worker import READ as NOTE_READ
from recovery_worker import make_work as make_note_work

S = ExecutionStatus
REVIEWER = "approve:supplier_invoice_validation"


class Halt(BaseException):
    """Stands in for a process death between two journaled actions."""


def _store(tmp_path: Path) -> tuple[Path, SqliteStorage]:
    path = tmp_path / "work.sqlite"
    return path, SqliteStorage(path)


def _autonomous(storage: SqliteStorage, executor: Any) -> Env:
    return Env(
        storage=storage,
        executor=executor,
        work=make_work(required_capabilities=frozenset({READ, VALIDATE})),
        policy=make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
        ),
    )


async def _stop_after_read(data: Any, context: ExecutionContext) -> InvoiceOutput:
    await context.invoke(READ, action_id="load-supplier", params={"supplier_id": data.supplier_id})
    raise Halt


async def _finish(data: Any, context: ExecutionContext) -> InvoiceOutput:
    supplier = await context.invoke(
        READ, action_id="load-supplier", params={"supplier_id": data.supplier_id}
    )
    assert isinstance(supplier, dict)
    return InvoiceOutput(
        invoice_number=data.invoice_number, supplier_id=str(supplier["id"]), status="valid"
    )


async def _drift(data: Any, context: ExecutionContext) -> InvoiceOutput:
    await context.invoke(READ, action_id="load-supplier", params={"supplier_id": data.supplier_id})
    await context.invoke(
        PREPARE,
        action_id="prepare-invoice",
        params={"invoice": data.invoice_number, "amount": "999.00"},
    )
    return InvoiceOutput(
        invoice_number=data.invoice_number, supplier_id=data.supplier_id, status="prepared"
    )


async def test_restart_replays_a_completed_action_and_does_not_repeat_it(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    seen: list[UUID] = []

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        seen.append(context.execution_id)
        return await _stop_after_read(data, context)

    first = _autonomous(storage, FunctionExecutor(stop))
    with pytest.raises(Halt):
        await first.run()
    execution_id = seen[0]
    stored = await first.runner.get(execution_id)
    assert stored is not None
    assert stored.status is S.RUNNING
    storage.close()

    reopened = SqliteStorage(path)
    second = _autonomous(reopened, FunctionExecutor(_finish))
    try:
        done = await second.runner.resume(execution_id, actor=second.actor)
        journal = await second.runner.journal(execution_id)
    finally:
        reopened.close()
    assert done.status is S.COMPLETED
    assert second.provider.count(READ) == 0
    assert second.executor.runs == 1
    assert [(entry.action_id, entry.status) for entry in journal] == [
        ("load-supplier", JournalStatus.SUCCEEDED)
    ]


async def test_inflight_action_is_uncertain_after_restart_and_not_retried(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    seen: list[UUID] = []

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        seen.append(context.execution_id)
        return await _stop_after_read(data, context)

    first = _autonomous(storage, FunctionExecutor(stop))
    with pytest.raises(Halt):
        await first.run()
    execution_id = seen[0]
    await storage.executions.append_journal(
        JournalEntry(
            execution_id=execution_id,
            action_id="prepare-invoice",
            capability=PREPARE,
            params_digest="ab" * 32,
            operation_id=operation_id(execution_id, "prepare-invoice"),
            status=JournalStatus.IN_FLIGHT,
            started_at=datetime(2026, 11, 14, tzinfo=UTC),
        )
    )
    storage.close()

    reopened = SqliteStorage(path)
    second = _autonomous(reopened, FunctionExecutor(_finish))
    try:
        failed = await second.runner.resume(execution_id, actor=second.actor)
        journal = {
            entry.action_id: entry.status for entry in await second.runner.journal(execution_id)
        }
        types = [event.event_type.value for event in await second.runner.events(execution_id)]
    finally:
        reopened.close()
    assert failed.status is S.FAILED
    assert failed.status_reason == "uncertain_outcome"
    assert failed.requires_reconciliation
    assert second.executor.runs == 0
    assert second.provider.count(READ) == 0
    assert journal["load-supplier"] is JournalStatus.SUCCEEDED
    assert journal["prepare-invoice"] is JournalStatus.UNCERTAIN
    assert "work.action.uncertain" in types
    assert "work.execution.failed" in types


def test_process_death_during_publish_is_not_repeated(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite"
    log_path = tmp_path / "effects.log"
    id_path = tmp_path / "execution.id"
    env = os.environ.copy()
    tests = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = tests + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "from recovery_worker import crash_process; import sys; crash_process(*sys.argv[1:])",
            str(database),
            str(log_path),
            str(id_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 86, completed.stderr
    before = log_path.read_text(encoding="utf-8").splitlines()
    assert before == [NOTE_READ, PUBLISH]

    runner, authority, storage = make_runner(
        str(database), str(log_path), crash=False, id_path=str(tmp_path / "ignored.id")
    )
    actor = authority.issue(
        actor_id="user:42",
        tenant_id="acme",
        grants={NOTE_READ, PUBLISH},
        ttl=timedelta(hours=1),
    )
    execution_id = UUID(id_path.read_text(encoding="utf-8"))

    async def recover() -> None:
        failed = await runner.resume(execution_id, actor=actor)
        assert failed.status is S.FAILED
        assert failed.requires_reconciliation
        journal = {entry.action_id: entry.status for entry in await runner.journal(execution_id)}
        assert journal == {
            "read-note": JournalStatus.SUCCEEDED,
            "publish-note": JournalStatus.UNCERTAIN,
        }
        work = make_note_work()
        again = await runner.run(
            work_id=work.id,
            version=work.version,
            input_data={"title": "hello"},
            actor=actor,
            idempotency_key="note-1",
        )
        assert again.execution_id == execution_id
        assert again.status is S.FAILED

    try:
        asyncio.run(recover())
    finally:
        storage.close()
    assert log_path.read_text(encoding="utf-8").splitlines() == before


async def test_approval_digest_stays_valid_across_a_restart(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    work = make_work(required_capabilities=frozenset({READ, PREPARE}))
    policy = make_policy(
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
    )
    first = Env(storage=storage, work=work, policy=policy, executor=InvoiceExecutor(prepare=True))
    waiting = await first.run(idempotency_key="bill-1")
    approval = (await first.runner.approvals.list_pending(waiting.execution_id))[0]
    digest = approval.action_digest
    storage.close()

    reopened = SqliteStorage(path)
    second = Env(
        storage=reopened,
        work=work,
        policy=policy,
        executor=InvoiceExecutor(prepare=True),
    )
    try:
        (pending,) = await second.runner.approvals.list_pending(waiting.execution_id)
        assert pending.approval_id == approval.approval_id
        assert pending.action_digest == digest
        assert pending.status is ApprovalStatus.PENDING
        reviewer = second.issue("reviewer:7", (REVIEWER,))
        granted = await second.runner.approvals.approve(pending.approval_id, reviewer=reviewer)
        assert granted.status is ApprovalStatus.GRANTED
        done = await second.runner.resume(waiting.execution_id, actor=second.actor)
        consumed = await second.runner.approvals.get(pending.approval_id)
    finally:
        reopened.close()
    assert done.status is S.COMPLETED
    assert consumed is not None
    assert consumed.status is ApprovalStatus.CONSUMED
    assert second.provider.count(READ) == 0
    assert second.provider.count(PREPARE) == 1


async def test_changed_params_after_restart_invalidate_the_approval(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    work = make_work(required_capabilities=frozenset({READ, PREPARE}))
    policy = make_policy(
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
    )
    first = Env(storage=storage, work=work, policy=policy, executor=InvoiceExecutor(prepare=True))
    waiting = await first.run(idempotency_key="bill-2")
    approval = (await first.runner.approvals.list_pending(waiting.execution_id))[0]
    storage.close()

    reopened = SqliteStorage(path)
    second = Env(storage=reopened, work=work, policy=policy, executor=FunctionExecutor(_drift))
    try:
        (pending,) = await second.runner.approvals.list_pending(waiting.execution_id)
        await second.runner.approvals.approve(
            pending.approval_id, reviewer=second.issue("reviewer:7", (REVIEWER,))
        )
        again = await second.runner.resume(waiting.execution_id, actor=second.actor)
        stored = await second.runner.approvals.get(approval.approval_id)
        pending_now = await second.runner.approvals.list_pending(waiting.execution_id)
    finally:
        reopened.close()
    assert again.status is S.WAITING_APPROVAL
    assert stored is not None
    assert stored.status is ApprovalStatus.INVALIDATED
    assert len(pending_now) == 1
    assert pending_now[0].action_digest != approval.action_digest
    assert second.provider.count(PREPARE) == 0
    assert second.provider.count(READ) == 0


async def test_policy_denial_of_an_interrupted_run_fails_it(tmp_path: Path) -> None:
    _path, storage = _store(tmp_path)
    seen: list[UUID] = []

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        seen.append(context.execution_id)
        return await _stop_after_read(data, context)

    first = _autonomous(storage, FunctionExecutor(stop))
    with pytest.raises(Halt):
        await first.run()
    revoked = first.issue("user:42", (READ,))
    failed = await first.runner.resume(seen[0], actor=revoked)
    storage.close()
    assert failed.status is S.FAILED
    assert failed.status_reason == "policy_denied_on_resume"
    assert failed.requires_reconciliation is False
    assert "missing_capability:invoices.validate" in failed.policy_reasons


async def test_a_live_claim_keeps_an_interrupted_execution_unchanged(tmp_path: Path) -> None:
    _path, storage = _store(tmp_path)
    seen: list[UUID] = []

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        seen.append(context.execution_id)
        return await _stop_after_read(data, context)

    env = _autonomous(storage, FunctionExecutor(stop))
    with pytest.raises(Halt):
        await env.run()
    execution_id = seen[0]
    with env.runner._claim(execution_id) as claimed:
        assert claimed
        unchanged = await env.runner.resume(execution_id, actor=env.actor)
    assert unchanged.status is S.RUNNING
    journal = await env.runner.journal(execution_id)
    assert [entry.status for entry in journal] == [JournalStatus.SUCCEEDED]
    storage.close()


async def test_ready_resume_does_not_call_a_provider_for_an_inflight_action() -> None:
    env = Env(
        work=make_work(required_capabilities=frozenset({READ, VALIDATE})),
        policy=make_policy(autonomy=AutonomyLevel.MANUAL),
        executor=InvoiceExecutor(),
    )
    waiting = await env.run()
    approval = (await env.runner.approvals.list_pending(waiting.execution_id))[0]
    await env.runner.approvals.approve(
        approval.approval_id, reviewer=env.issue("reviewer:7", (REVIEWER,))
    )
    await env.storage.executions.append_journal(
        JournalEntry(
            execution_id=waiting.execution_id,
            action_id="load-supplier",
            capability=READ,
            params_digest="cd" * 32,
            operation_id=operation_id(waiting.execution_id, "load-supplier"),
            status=JournalStatus.IN_FLIGHT,
            started_at=datetime(2026, 11, 14, tzinfo=UTC),
        )
    )
    failed = await env.runner.resume(waiting.execution_id, actor=env.actor)
    assert failed.status is S.FAILED
    assert failed.requires_reconciliation
    assert env.provider.count(READ) == 0
    journal = await env.runner.journal(waiting.execution_id)
    assert journal[0].status is JournalStatus.UNCERTAIN


async def test_an_already_uncertain_action_is_not_retried(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    seen: list[UUID] = []

    async def stop(data: Any, context: ExecutionContext) -> InvoiceOutput:
        seen.append(context.execution_id)
        return await _stop_after_read(data, context)

    first = _autonomous(storage, FunctionExecutor(stop))
    with pytest.raises(Halt):
        await first.run()
    execution_id = seen[0]
    succeeded = (await storage.executions.list_journal(execution_id))[0]
    uncertain = JournalEntry.model_validate(
        {
            **succeeded.model_dump(),
            "status": JournalStatus.UNCERTAIN,
            "result": None,
            "result_digest": None,
            "finished_at": None,
        }
    )
    await storage.executions.update_journal(uncertain, expected_status=JournalStatus.SUCCEEDED)
    storage.close()

    reopened = SqliteStorage(path)
    second = _autonomous(reopened, FunctionExecutor(_finish))
    try:
        failed = await second.runner.resume(execution_id, actor=second.actor)
    finally:
        reopened.close()
    assert failed.status is S.FAILED
    assert failed.requires_reconciliation
    assert second.provider.count(READ) == 0


async def test_idempotency_returns_the_finished_execution_after_reopen(tmp_path: Path) -> None:
    path, storage = _store(tmp_path)
    first = _autonomous(storage, FunctionExecutor(_finish))
    done = await first.run(idempotency_key="same")
    assert done.status is S.COMPLETED
    storage.close()

    reopened = SqliteStorage(path)
    second = _autonomous(reopened, FunctionExecutor(_finish))
    try:
        again = await second.run(idempotency_key="same")
    finally:
        reopened.close()
    assert again.execution_id == done.execution_id
    assert again.status is S.COMPLETED
    assert second.provider.count(READ) == 0
