"""Storage contract test suite (specification 12, 18).

Subclass ``StorageContractSuite`` in a pytest module and implement ``make_storage``::

    from id42_work.testing.contract import StorageContractSuite

    class TestPostgresStorage(StorageContractSuite):
        def make_storage(self) -> Storage:
            return PostgresStorage(...)

Every test gets a fresh storage. Requires ``pytest`` and ``pytest-asyncio``.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import EventType, WorkEvent
from id42_work.evidence.models import Evidence, EvidenceType
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from id42_work.policy.models import ApprovalGate
from id42_work.state_machine import transition_approval, transition_execution
from id42_work.states import ExecutionStatus
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope, Storage

_T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
_DIGEST = "a" * 64


class _Payload(BaseModel):
    value: str


def _execution(**overrides: Any) -> Execution:
    execution_id = overrides.pop("execution_id", uuid4())
    fields: dict[str, Any] = {
        "execution_id": execution_id,
        "work_id": "contract_work",
        "work_version": "1.0.0",
        "definition_digest": _DIGEST,
        "actor_id": "actor:1",
        "tenant_id": "tenant",
        "input_fingerprint": _DIGEST,
        "input_reference": f"ref://{execution_id}",
        "created_at": _T0,
        "updated_at": _T0,
    }
    fields.update(overrides)
    return Execution(**fields)


def _entry(execution_id: UUID, action_id: str, status: JournalStatus) -> JournalEntry:
    return JournalEntry(
        execution_id=execution_id,
        action_id=action_id,
        capability="contract.read",
        params_digest=_DIGEST,
        operation_id=operation_id(execution_id, action_id),
        status=status,
        started_at=None if status is JournalStatus.PENDING_APPROVAL else _T0,
    )


def _approval(execution_id: UUID) -> Approval:
    return Approval(
        execution_id=execution_id,
        gate=ApprovalGate.START,
        action_id="__start__",
        action_digest=_DIGEST,
        reviewer_capability="approve:contract_work",
        requested_at=_T0,
        expires_at=_T0 + timedelta(hours=1),
    )


def _event(execution_id: UUID | None, sequence: int) -> WorkEvent:
    return WorkEvent(
        event_type=EventType.EXECUTION_CREATED,
        execution_id=execution_id,
        sequence_number=sequence,
        timestamp=_T0,
    )


class StorageContractSuite(ABC):
    """Behaviour every ``Storage`` implementation must have."""

    @abstractmethod
    def make_storage(self) -> Storage:
        """Return a new, empty storage."""

    # ------------------------------------------------------------------ executions

    @pytest.mark.asyncio
    async def test_execution_create_and_get(self) -> None:
        repo = self.make_storage().executions
        execution = _execution()
        await repo.create(execution)
        assert await repo.get(execution.execution_id) == execution
        assert await repo.get(uuid4()) is None

    @pytest.mark.asyncio
    async def test_execution_create_rejects_duplicates_and_nonzero_revisions(self) -> None:
        repo = self.make_storage().executions
        execution = _execution()
        await repo.create(execution)
        with pytest.raises(StorageError):
            await repo.create(execution)
        with pytest.raises(StorageError):
            await repo.create(_execution(revision=2))

    @pytest.mark.asyncio
    async def test_execution_compare_and_set_detects_lost_updates(self) -> None:
        repo = self.make_storage().executions
        execution = _execution()
        await repo.create(execution)
        first = transition_execution(execution, ExecutionStatus.POLICY_CHECK, at=_T0)
        competing = transition_execution(execution, ExecutionStatus.CANCELLED, at=_T0)
        await repo.compare_and_set(first)
        with pytest.raises(ConcurrentModificationError):
            await repo.compare_and_set(competing)
        assert await repo.get(execution.execution_id) == first

    @pytest.mark.asyncio
    async def test_execution_compare_and_set_requires_an_existing_record(self) -> None:
        repo = self.make_storage().executions
        with pytest.raises(StorageError):
            await repo.compare_and_set(_execution(revision=1))

    # ------------------------------------------------------------------ journal

    @pytest.mark.asyncio
    async def test_journal_append_update_and_order(self) -> None:
        repo = self.make_storage().executions
        execution = _execution()
        eid = execution.execution_id
        await repo.create(execution)
        await repo.append_journal(_entry(eid, "b-first", JournalStatus.PENDING_APPROVAL))
        await repo.append_journal(_entry(eid, "a-second", JournalStatus.IN_FLIGHT))
        await repo.update_journal(
            _entry(eid, "b-first", JournalStatus.IN_FLIGHT),
            expected_status=JournalStatus.PENDING_APPROVAL,
        )
        entries = await repo.list_journal(eid)
        assert [(e.action_id, e.status) for e in entries] == [
            ("b-first", JournalStatus.IN_FLIGHT),
            ("a-second", JournalStatus.IN_FLIGHT),
        ]
        assert await repo.list_journal(uuid4()) == ()

    @pytest.mark.asyncio
    async def test_journal_rejects_duplicates_stale_updates_and_unknown_entries(self) -> None:
        repo = self.make_storage().executions
        execution = _execution()
        eid = execution.execution_id
        with pytest.raises(StorageError):
            await repo.append_journal(_entry(eid, "a", JournalStatus.IN_FLIGHT))
        await repo.create(execution)
        await repo.append_journal(_entry(eid, "a", JournalStatus.IN_FLIGHT))
        with pytest.raises(StorageError):
            await repo.append_journal(_entry(eid, "a", JournalStatus.IN_FLIGHT))
        with pytest.raises(ConcurrentModificationError):
            await repo.update_journal(
                _entry(eid, "a", JournalStatus.UNCERTAIN),
                expected_status=JournalStatus.PENDING_APPROVAL,
            )
        with pytest.raises(StorageError):
            await repo.update_journal(
                _entry(eid, "missing", JournalStatus.UNCERTAIN),
                expected_status=JournalStatus.IN_FLIGHT,
            )

    # ------------------------------------------------------------------ approvals

    @pytest.mark.asyncio
    async def test_approval_create_get_and_list(self) -> None:
        repo = self.make_storage().approvals
        eid = uuid4()
        approval = _approval(eid)
        other = _approval(uuid4())
        await repo.create(approval)
        await repo.create(other)
        with pytest.raises(StorageError):
            await repo.create(approval)
        assert await repo.get(approval.approval_id) == approval
        assert await repo.get(uuid4()) is None
        assert await repo.list_for_execution(eid) == (approval,)
        assert await repo.list_for_execution(eid, status=ApprovalStatus.GRANTED) == ()
        pending = await repo.list_by_status(ApprovalStatus.PENDING)
        assert {a.approval_id for a in pending} == {approval.approval_id, other.approval_id}

    @pytest.mark.asyncio
    async def test_approval_compare_and_set_has_one_winner(self) -> None:
        repo = self.make_storage().approvals
        approval = _approval(uuid4())
        await repo.create(approval)
        decision = {"decided_at": _T0, "decision_actor_id": "reviewer:1"}
        granted = transition_approval(approval, ApprovalStatus.GRANTED, **decision)
        rejected = transition_approval(approval, ApprovalStatus.REJECTED, reason="no", **decision)
        await repo.compare_and_set(granted, expected_status=ApprovalStatus.PENDING)
        with pytest.raises(ConcurrentModificationError):
            await repo.compare_and_set(rejected, expected_status=ApprovalStatus.PENDING)
        assert await repo.get(approval.approval_id) == granted
        assert await repo.list_by_status(ApprovalStatus.PENDING) == ()
        with pytest.raises(StorageError):
            await repo.compare_and_set(_approval(uuid4()), expected_status=ApprovalStatus.PENDING)

    # ------------------------------------------------------------------ events

    @pytest.mark.asyncio
    async def test_events_are_append_only_and_gapless(self) -> None:
        repo = self.make_storage().events
        eid = uuid4()
        await repo.append(_event(eid, 1))
        with pytest.raises(ConcurrentModificationError):
            await repo.append(_event(eid, 1))
        with pytest.raises(ConcurrentModificationError):
            await repo.append(_event(eid, 3))
        await repo.append(_event(eid, 2))
        assert await repo.last_sequence(eid) == 2
        assert [e.sequence_number for e in await repo.list(eid)] == [1, 2]
        assert await repo.last_sequence(uuid4()) == 0

    @pytest.mark.asyncio
    async def test_registry_level_events_have_their_own_stream(self) -> None:
        repo = self.make_storage().events
        await repo.append(_event(None, 1))
        await repo.append(_event(uuid4(), 1))
        assert await repo.last_sequence(None) == 1
        assert len(await repo.list(None)) == 1

    # ------------------------------------------------------------------ idempotency

    @pytest.mark.asyncio
    async def test_idempotency_reservation_is_first_writer_wins(self) -> None:
        repo = self.make_storage().idempotency
        scope = IdempotencyScope(
            tenant_id="tenant", work_id="contract_work", work_version="1.0.0", idempotency_key="k"
        )
        first = IdempotencyRecord(scope=scope, input_fingerprint=_DIGEST, execution_id=uuid4())
        second = IdempotencyRecord(scope=scope, input_fingerprint="b" * 64, execution_id=uuid4())
        assert await repo.reserve(first) == first
        assert await repo.reserve(second) == first
        assert await repo.get(scope) == first
        other_tenant = scope.model_copy(update={"tenant_id": "other"})
        assert await repo.get(other_tenant) is None

    # ------------------------------------------------------------------ evidence and inputs

    @pytest.mark.asyncio
    async def test_evidence_add_and_list(self) -> None:
        repo = self.make_storage().evidence
        eid = uuid4()
        evidence = Evidence(
            execution_id=eid,
            evidence_type=EvidenceType.TOOL_RESULT,
            source="contract",
            created_at=_T0,
            content_digest=_DIGEST,
        )
        await repo.add(evidence)
        with pytest.raises(StorageError):
            await repo.add(evidence)
        assert await repo.list(eid) == (evidence,)
        assert await repo.list(uuid4()) == ()

    @pytest.mark.asyncio
    async def test_inputs_round_trip(self) -> None:
        store = self.make_storage().inputs
        eid = uuid4()
        payload = _Payload(value="x")
        reference = await store.put(eid, payload)
        assert isinstance(reference, str)
        assert reference
        stored = await store.get(reference)
        assert stored is not None
        assert stored.model_dump() == payload.model_dump()
        assert await store.get(f"{reference}-missing") is None
        with pytest.raises(StorageError):
            await store.put(eid, payload)
