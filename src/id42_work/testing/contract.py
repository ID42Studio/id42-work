"""Storage contract test suite (specification 12, 18).

Subclass ``StorageContractSuite`` in a pytest module and implement ``make_storage``::

    from id42_work.testing.contract import StorageContractSuite

    class TestPostgresStorage(StorageContractSuite):
        def make_storage(self) -> Storage:
            return PostgresStorage(...)

Every test gets a fresh storage. Requires ``pytest`` and ``pytest-asyncio``.
"""

import json
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
from id42_work.outcomes.models import Outcome, Severity, ValidationResult
from id42_work.policy.models import ApprovalGate
from id42_work.state_machine import transition_approval, transition_execution
from id42_work.states import ExecutionStatus
from id42_work.storage.codec import dump_execution, execution_list_key
from id42_work.storage.listing import page_executions
from id42_work.storage.protocols import (
    ExecutionListQuery,
    ExecutionRepository,
    IdempotencyRecord,
    IdempotencyScope,
    Storage,
)

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
    async def test_execution_list_orders_by_creation_time_then_id(self) -> None:
        repo = self.make_storage().executions
        assert await repo.list() == ()
        same_time_later = _execution(execution_id=UUID(int=2))
        same_time_earlier = _execution(execution_id=UUID(int=1))
        newer = _execution(
            execution_id=UUID(int=0),
            created_at=_T0 + timedelta(seconds=5),
            updated_at=_T0 + timedelta(seconds=5),
        )
        await repo.create(newer)
        await repo.create(same_time_later)
        await repo.create(same_time_earlier)
        assert await repo.list() == (same_time_earlier, same_time_later, newer)

    @pytest.mark.asyncio
    async def test_execution_list_pages_by_actor_status_and_keyset(self) -> None:
        repo = self.make_storage().executions
        a1 = _execution(execution_id=UUID(int=1), actor_id="actor:1", tenant_id="tenant")
        a2 = _execution(execution_id=UUID(int=2), actor_id="actor:1", tenant_id="tenant")
        other = _execution(
            execution_id=UUID(int=3),
            actor_id="actor:2",
            tenant_id="tenant",
            created_at=_T0 + timedelta(seconds=1),
            updated_at=_T0 + timedelta(seconds=1),
        )
        no_tenant = _execution(
            execution_id=UUID(int=4),
            actor_id="actor:1",
            tenant_id=None,
            created_at=_T0 + timedelta(seconds=2),
            updated_at=_T0 + timedelta(seconds=2),
        )
        cancelled = _execution(
            execution_id=UUID(int=5),
            actor_id="actor:1",
            tenant_id="tenant",
            status=ExecutionStatus.CANCELLED,
            finished_at=_T0 + timedelta(seconds=3),
            created_at=_T0 + timedelta(seconds=3),
            updated_at=_T0 + timedelta(seconds=3),
        )
        later = _execution(
            execution_id=UUID(int=6),
            actor_id="actor:1",
            tenant_id="tenant",
            created_at=_T0 + timedelta(seconds=5),
            updated_at=_T0 + timedelta(seconds=5),
        )
        for execution in (later, other, a2, cancelled, no_tenant, a1):
            await repo.create(execution)
        full = (a1, a2, other, no_tenant, cancelled, later)
        assert await repo.list() == full
        assert await repo.list(ExecutionListQuery()) == full
        stored_created = json.loads(dump_execution(a1))["created_at"]
        assert execution_list_key(a1) == (stored_created, str(a1.execution_id))

        owned = ExecutionListQuery(restrict_actor=True, actor_id="actor:1", tenant_id="tenant")
        assert await self._page(repo, owned) == (a1, a2, cancelled, later)
        first = ExecutionListQuery(
            restrict_actor=True, actor_id="actor:1", tenant_id="tenant", limit=2
        )
        assert await self._page(repo, first) == (a1, a2)
        created, execution_id = execution_list_key(a2)
        after_same_time = ExecutionListQuery(
            restrict_actor=True,
            actor_id="actor:1",
            tenant_id="tenant",
            after_created_at=execution_list_key(a1)[0],
            after_execution_id=str(a1.execution_id),
            limit=1,
        )
        assert await self._page(repo, after_same_time) == (a2,)
        after_other = ExecutionListQuery(
            restrict_actor=True,
            actor_id="actor:1",
            tenant_id="tenant",
            after_created_at=created,
            after_execution_id=execution_id,
            limit=2,
        )
        assert await self._page(repo, after_other) == (cancelled, later)
        assert await self._page(
            repo,
            ExecutionListQuery(restrict_actor=True, actor_id="actor:1", tenant_id=None),
        ) == (no_tenant,)
        assert await self._page(
            repo,
            ExecutionListQuery(
                restrict_actor=True,
                actor_id="actor:1",
                tenant_id="tenant",
                status=ExecutionStatus.CANCELLED.value,
            ),
        ) == (cancelled,)
        assert await self._page(
            repo, ExecutionListQuery(status=ExecutionStatus.CANCELLED.value)
        ) == (cancelled,)
        missing = ExecutionListQuery(
            restrict_actor=True, actor_id="actor:missing", tenant_id="tenant"
        )
        assert await self._page(repo, missing) == ()
        past_end = ExecutionListQuery(
            restrict_actor=True,
            actor_id="actor:1",
            tenant_id="tenant",
            after_created_at=execution_list_key(later)[0],
            after_execution_id=str(later.execution_id),
        )
        assert await self._page(repo, past_end) == ()

    async def _page(
        self, repo: ExecutionRepository, query: ExecutionListQuery
    ) -> tuple[Execution, ...]:
        listed = await repo.list(query)
        assert listed == page_executions(await repo.list(), query)
        return listed

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

    @pytest.mark.asyncio
    async def test_release_drops_only_the_owning_reservation(self) -> None:
        repo = self.make_storage().idempotency
        scope = IdempotencyScope(
            tenant_id="tenant", work_id="contract_work", work_version="1.0.0", idempotency_key="k"
        )
        owner = uuid4()
        record = IdempotencyRecord(scope=scope, input_fingerprint=_DIGEST, execution_id=owner)
        await repo.reserve(record)
        await repo.release(scope, execution_id=uuid4())
        assert await repo.get(scope) == record
        await repo.release(scope, execution_id=owner)
        assert await repo.get(scope) is None
        await repo.release(scope, execution_id=owner)
        replacement = record.model_copy(update={"execution_id": uuid4()})
        assert await repo.reserve(replacement) == replacement

    @pytest.mark.asyncio
    async def test_commit_execution_is_all_or_nothing(self) -> None:
        storage = self.make_storage()
        execution = _execution(last_event_sequence=1)
        eid = execution.execution_id
        created = _event(eid, 1)
        await storage.commit_execution(execution, created, create=True)
        assert await storage.executions.get(eid) == execution
        assert [e.sequence_number for e in await storage.events.list(eid)] == [1]

        with pytest.raises(StorageError):
            await storage.commit_execution(execution, _event(eid, 2), create=True)
        assert await storage.events.last_sequence(eid) == 1

        foreign = execution.model_copy(update={"execution_id": uuid4(), "revision": 1})
        with pytest.raises(StorageError, match="revision 0"):
            await storage.commit_execution(foreign, _event(foreign.execution_id, 1), create=True)

        with pytest.raises(StorageError, match="does not belong"):
            await storage.commit_execution(execution, _event(uuid4(), 2))
        assert await storage.events.last_sequence(eid) == 1

        missing = _execution()
        with pytest.raises(StorageError, match="does not exist"):
            await storage.commit_execution(
                transition_execution(
                    missing, ExecutionStatus.POLICY_CHECK, at=_T0, last_event_sequence=1
                ),
                _event(missing.execution_id, 1),
            )

        stale = transition_execution(
            execution, ExecutionStatus.POLICY_CHECK, at=_T0, last_event_sequence=2
        )
        await storage.executions.compare_and_set(
            transition_execution(
                execution, ExecutionStatus.POLICY_CHECK, at=_T0, last_event_sequence=1
            )
        )
        with pytest.raises(ConcurrentModificationError):
            await storage.commit_execution(stale, _event(eid, 2))
        assert await storage.events.last_sequence(eid) == 1

        current = await storage.executions.get(eid)
        assert current is not None
        raced = transition_execution(current, ExecutionStatus.READY, at=_T0, last_event_sequence=2)
        with pytest.raises(ConcurrentModificationError):
            await storage.commit_execution(raced, _event(eid, 1))
        assert (await storage.executions.get(eid)) == current

        committed = transition_execution(
            current, ExecutionStatus.READY, at=_T0, last_event_sequence=2
        )
        await storage.commit_execution(
            committed, _event(eid, 2).model_copy(update={"event_type": EventType.POLICY_EVALUATED})
        )
        stored = await storage.executions.get(eid)
        assert stored == committed
        assert stored is not None
        assert stored.last_event_sequence == 2
        assert await storage.events.last_sequence(eid) == 2

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

    # ------------------------------------------------------------------ recovery

    @pytest.mark.asyncio
    async def test_resume_claim_is_exclusive(self) -> None:
        storage = self.make_storage()
        execution_id = uuid4()
        ttl = timedelta(seconds=30)
        assert await storage.claim_resume(execution_id, "owner-a", now=_T0, ttl=ttl)
        assert not await storage.claim_resume(execution_id, "owner-b", now=_T0, ttl=ttl)
        await storage.release_resume(execution_id, "owner-b")
        assert not await storage.claim_resume(execution_id, "owner-b", now=_T0, ttl=ttl)
        await storage.release_resume(execution_id, "owner-a")
        assert await storage.claim_resume(execution_id, "owner-b", now=_T0, ttl=ttl)

    @pytest.mark.asyncio
    async def test_validating_commit_is_atomic_and_keeps_the_journal(self) -> None:
        """The recovery transition and its event commit together; the journal stays."""
        storage = self.make_storage()
        execution = _execution()
        await storage.executions.create(execution)
        current = execution
        for status in (
            ExecutionStatus.POLICY_CHECK,
            ExecutionStatus.READY,
            ExecutionStatus.RUNNING,
            ExecutionStatus.VALIDATING,
        ):
            current = transition_execution(current, status, at=_T0)
            await storage.executions.compare_and_set(current)
        eid = current.execution_id
        done = JournalEntry(
            execution_id=eid,
            action_id="post",
            capability="contract.read",
            params_digest=_DIGEST,
            operation_id=operation_id(eid, "post"),
            status=JournalStatus.SUCCEEDED,
            result={"ok": True},
            result_digest=_DIGEST,
            started_at=_T0,
            finished_at=_T0,
        )
        await storage.executions.append_journal(done)
        outcome = Outcome(
            execution_id=eid,
            output=_Payload(value="x"),
            verified=True,
            validation_results=(
                ValidationResult(
                    validator_id="schema",
                    passed=True,
                    severity=Severity.INFO,
                    message="ok",
                ),
            ),
            produced_at=_T0,
            accepted_at=_T0,
        )
        completed = transition_execution(
            current,
            ExecutionStatus.COMPLETED,
            at=_T0,
            outcome=outcome,
            last_event_sequence=1,
        )
        await storage.commit_execution(
            completed,
            _event(eid, 1).model_copy(update={"event_type": EventType.EXECUTION_COMPLETED}),
        )
        assert await storage.executions.get(eid) == completed
        assert await storage.executions.list_journal(eid) == (done,)
        assert await storage.events.last_sequence(eid) == 1

        stale = transition_execution(
            current,
            ExecutionStatus.VALIDATION_FAILED,
            at=_T0,
            reason="validation_failed",
            last_event_sequence=2,
        )
        with pytest.raises(ConcurrentModificationError):
            await storage.commit_execution(
                stale,
                _event(eid, 2).model_copy(update={"event_type": EventType.VALIDATION_FAILED}),
            )
        assert await storage.executions.get(eid) == completed
        assert await storage.events.last_sequence(eid) == 1
        assert await storage.executions.list_journal(eid) == (done,)
