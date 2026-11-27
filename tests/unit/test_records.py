from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from _factories import DIGEST_A, T0
from id42_work import EventType, Evidence, EvidenceType, ExecutionErrorInfo, WorkEvent
from id42_work.journal import NAMESPACE_ID42_WORK, JournalEntry, JournalStatus, operation_id

EXECUTION_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_operation_id_is_stable() -> None:
    assert operation_id(EXECUTION_ID, "post-invoice") == operation_id(EXECUTION_ID, "post-invoice")
    assert operation_id(EXECUTION_ID, "post-invoice") != operation_id(EXECUTION_ID, "other")
    # Pinned: operation ids reach external systems and must never change.
    assert UUID("20ff4f61-4ffc-554c-a314-6d40df64b16a") == NAMESPACE_ID42_WORK
    assert str(operation_id(EXECUTION_ID, "post-invoice")) == str(
        __import__("uuid").uuid5(NAMESPACE_ID42_WORK, f"{EXECUTION_ID}:post-invoice")
    )


def _entry(status: JournalStatus, **overrides: object) -> JournalEntry:
    fields: dict[str, object] = {
        "execution_id": EXECUTION_ID,
        "action_id": "post-invoice",
        "capability": "odoo.invoice.post",
        "params_digest": DIGEST_A,
        "operation_id": operation_id(EXECUTION_ID, "post-invoice"),
        "status": status,
    }
    fields.update(overrides)
    return JournalEntry.model_validate(fields)


def test_valid_journal_entries() -> None:
    _entry(JournalStatus.PENDING_APPROVAL)
    _entry(JournalStatus.IN_FLIGHT, started_at=T0)
    _entry(JournalStatus.UNCERTAIN, started_at=T0)
    done = _entry(
        JournalStatus.SUCCEEDED,
        started_at=T0,
        finished_at=T0 + timedelta(seconds=1),
        result={"move_id": 7},
        result_digest=DIGEST_A,
    )
    assert done.result == {"move_id": 7}
    _entry(
        JournalStatus.FAILED,
        started_at=T0,
        finished_at=T0,
        error=ExecutionErrorInfo(type="KeyError", message="x"),
    )
    _entry(JournalStatus.NOT_APPLIED, started_at=T0)
    _entry(
        JournalStatus.ABANDONED,
        started_at=T0,
        error=ExecutionErrorInfo(type="ReconciliationAbandoned", message="closed"),
    )


@pytest.mark.parametrize(
    ("status", "overrides"),
    [
        (JournalStatus.PENDING_APPROVAL, {"started_at": T0}),
        (JournalStatus.IN_FLIGHT, {}),
        (JournalStatus.SUCCEEDED, {"started_at": T0, "finished_at": T0}),
        (JournalStatus.SUCCEEDED, {"started_at": T0, "result_digest": DIGEST_A}),
        (JournalStatus.FAILED, {"started_at": T0, "finished_at": T0}),
        (JournalStatus.ABANDONED, {"started_at": T0}),
        (JournalStatus.IN_FLIGHT, {"started_at": T0, "operation_id": uuid4()}),
        (JournalStatus.IN_FLIGHT, {"started_at": T0, "action_id": "__start__"}),
    ],
)
def test_invalid_journal_entries(status: JournalStatus, overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _entry(status, **overrides)


def test_evidence_record() -> None:
    evidence = Evidence(
        execution_id=EXECUTION_ID,
        evidence_type=EvidenceType.VALIDATION_REPORT,
        source="validator:invoice_rules",
        created_at=T0,
        content_digest=DIGEST_A,
        metadata={"passed": True},
    )
    assert Evidence.model_validate_json(evidence.model_dump_json()) == evidence
    with pytest.raises(ValidationError):
        Evidence(
            execution_id=EXECUTION_ID,
            evidence_type=EvidenceType.TOOL_RESULT,
            source="x",
            created_at=T0,
            content_digest="not-a-digest",
        )


def test_event_record() -> None:
    event = WorkEvent(
        event_type=EventType.EXECUTION_CREATED,
        execution_id=EXECUTION_ID,
        sequence_number=1,
        timestamp=T0,
        actor_reference="user:42",
    )
    assert event.schema_version == "1"
    assert event.model_dump(mode="json")["event_type"] == "work.execution.created"
    with pytest.raises(ValidationError):
        WorkEvent(
            event_type=EventType.EXECUTION_CREATED,
            execution_id=None,
            sequence_number=0,
            timestamp=T0,
        )


def test_event_types_follow_the_naming_convention() -> None:
    assert all(t.value.startswith("work.") for t in EventType)
    assert len({t.value for t in EventType}) == len(EventType)
