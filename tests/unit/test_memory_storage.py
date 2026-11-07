from datetime import timedelta
from uuid import uuid4

import pytest

from _factories import DIGEST_A, T0, InvoiceInput, make_execution
from id42_work import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    ConcurrentModificationError,
    EventType,
    Evidence,
    EvidenceType,
    ExecutionStatus,
    MemoryStorage,
    StorageError,
    WorkEvent,
)
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from id42_work.state_machine import transition_approval, transition_execution
from id42_work.storage import IdempotencyRecord, IdempotencyScope


async def test_execution_create_get_and_compare_and_set() -> None:
    repo = MemoryStorage().executions
    execution = make_execution()
    await repo.create(execution)
    assert await repo.get(execution.execution_id) == execution
    assert await repo.get(uuid4()) is None
    with pytest.raises(StorageError, match="already exists"):
        await repo.create(execution)
    moved = transition_execution(execution, ExecutionStatus.POLICY_CHECK, at=T0)
    await repo.compare_and_set(moved)
    with pytest.raises(ConcurrentModificationError):
        await repo.compare_and_set(moved)  # stale: revision already applied
    with pytest.raises(StorageError, match="does not exist"):
        await repo.compare_and_set(make_execution(revision=1))
    with pytest.raises(StorageError, match="revision 0"):
        await repo.create(make_execution(revision=3))


def _entry(execution_id: object, status: JournalStatus, action_id: str = "a1") -> JournalEntry:
    return JournalEntry.model_validate(
        {
            "execution_id": execution_id,
            "action_id": action_id,
            "capability": "x.read",
            "params_digest": DIGEST_A,
            "operation_id": operation_id(execution_id, action_id),  # type: ignore[arg-type]
            "status": status,
            "started_at": None if status is JournalStatus.PENDING_APPROVAL else T0,
        }
    )


async def test_journal_operations() -> None:
    repo = MemoryStorage().executions
    execution = make_execution()
    eid = execution.execution_id
    with pytest.raises(StorageError):
        await repo.append_journal(_entry(eid, JournalStatus.IN_FLIGHT))
    await repo.create(execution)
    await repo.append_journal(_entry(eid, JournalStatus.PENDING_APPROVAL))
    with pytest.raises(StorageError, match="already journaled"):
        await repo.append_journal(_entry(eid, JournalStatus.IN_FLIGHT))
    await repo.update_journal(
        _entry(eid, JournalStatus.IN_FLIGHT), expected_status=JournalStatus.PENDING_APPROVAL
    )
    with pytest.raises(ConcurrentModificationError):
        await repo.update_journal(
            _entry(eid, JournalStatus.UNCERTAIN), expected_status=JournalStatus.PENDING_APPROVAL
        )
    with pytest.raises(StorageError, match="not journaled"):
        await repo.update_journal(
            _entry(eid, JournalStatus.IN_FLIGHT, "zz"), expected_status=JournalStatus.IN_FLIGHT
        )
    assert [e.status for e in await repo.list_journal(eid)] == [JournalStatus.IN_FLIGHT]
    assert await repo.list_journal(uuid4()) == ()


async def test_approval_repository() -> None:
    repo = MemoryStorage().approvals
    eid = uuid4()
    approval = Approval(
        execution_id=eid,
        gate=ApprovalGate.START,
        action_id="__start__",
        action_digest=DIGEST_A,
        reviewer_capability="approve:w",
        requested_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    await repo.create(approval)
    with pytest.raises(StorageError):
        await repo.create(approval)
    assert await repo.get(approval.approval_id) == approval
    assert await repo.list_for_execution(eid, status=ApprovalStatus.PENDING) == (approval,)
    assert await repo.list_for_execution(eid, status=ApprovalStatus.GRANTED) == ()
    expired = transition_approval(approval, ApprovalStatus.EXPIRED)
    await repo.compare_and_set(expired, expected_status=ApprovalStatus.PENDING)
    with pytest.raises(ConcurrentModificationError):
        await repo.compare_and_set(expired, expected_status=ApprovalStatus.PENDING)
    with pytest.raises(StorageError, match="does not exist"):
        await repo.compare_and_set(
            approval.model_copy(update={"approval_id": uuid4()}),
            expected_status=ApprovalStatus.PENDING,
        )


async def test_event_streams_are_gapless() -> None:
    repo = MemoryStorage().events
    eid = uuid4()

    def event(sequence: int, execution_id: object = eid) -> WorkEvent:
        return WorkEvent(
            event_type=EventType.EXECUTION_CREATED,
            execution_id=execution_id,  # type: ignore[arg-type]
            sequence_number=sequence,
            timestamp=T0,
        )

    await repo.append(event(1))
    with pytest.raises(ConcurrentModificationError):
        await repo.append(event(1))
    with pytest.raises(ConcurrentModificationError):
        await repo.append(event(3))
    await repo.append(event(2))
    await repo.append(event(1, None))  # registry-level stream
    assert await repo.last_sequence(eid) == 2
    assert await repo.last_sequence(None) == 1
    assert [e.sequence_number for e in await repo.list(eid)] == [1, 2]


async def test_idempotency_reservation_is_first_writer_wins() -> None:
    repo = MemoryStorage().idempotency
    scope = IdempotencyScope(tenant_id="t", work_id="w", work_version="1.0.0", idempotency_key="k")
    first = IdempotencyRecord(scope=scope, input_fingerprint=DIGEST_A, execution_id=uuid4())
    second = IdempotencyRecord(scope=scope, input_fingerprint="b" * 64, execution_id=uuid4())
    assert await repo.reserve(first) == first
    assert await repo.reserve(second) == first
    assert await repo.get(scope) == first


async def test_evidence_and_inputs() -> None:
    storage = MemoryStorage()
    eid = uuid4()
    evidence = Evidence(
        execution_id=eid, evidence_type=EvidenceType.TOOL_RESULT, source="x", created_at=T0
    )
    await storage.evidence.add(evidence)
    with pytest.raises(StorageError):
        await storage.evidence.add(evidence)
    assert await storage.evidence.list(eid) == (evidence,)
    data = InvoiceInput(invoice_number="I", supplier_id="S", currency="BRL", amount="1")  # type: ignore[arg-type]
    reference = await storage.inputs.put(eid, data)
    assert await storage.inputs.get(reference) is data
    assert await storage.inputs.get("memory://inputs/none") is None
    with pytest.raises(StorageError):
        await storage.inputs.put(eid, data)
