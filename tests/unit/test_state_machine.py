from datetime import timedelta
from itertools import product
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from _factories import T0, make_execution, make_outcome
from id42_work import (
    TRANSITIONS,
    Execution,
    ExecutionErrorInfo,
    ExecutionStatus,
    InvalidStateTransitionError,
)
from id42_work.state_machine import transition_execution
from id42_work.states import can_transition

S = ExecutionStatus

# Specification 4.1, transcribed independently from the implementation's table.
# D51 adds the two reopen edges. They are not in the specification.
SPEC_TABLE: set[tuple[ExecutionStatus, ExecutionStatus]] = {
    (S.CREATED, S.POLICY_CHECK),
    (S.POLICY_CHECK, S.DENIED),
    (S.POLICY_CHECK, S.READY),
    (S.READY, S.WAITING_APPROVAL),
    (S.READY, S.RUNNING),
    (S.READY, S.DENIED),
    (S.WAITING_APPROVAL, S.READY),
    (S.WAITING_APPROVAL, S.REJECTED),
    (S.RUNNING, S.WAITING_APPROVAL),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.VALIDATING),
    (S.VALIDATING, S.VALIDATION_FAILED),
    (S.VALIDATING, S.WAITING_REVIEW),
    (S.VALIDATING, S.COMPLETED),
    (S.WAITING_REVIEW, S.COMPLETED),
    (S.WAITING_REVIEW, S.REJECTED),
    (S.CREATED, S.CANCELLED),
    (S.READY, S.CANCELLED),
    (S.WAITING_APPROVAL, S.CANCELLED),
    (S.WAITING_REVIEW, S.CANCELLED),
}
D51_REOPEN: set[tuple[ExecutionStatus, ExecutionStatus]] = {
    (S.FAILED, S.READY),
    (S.VALIDATION_FAILED, S.READY),
}
ALLOWED = SPEC_TABLE | D51_REOPEN
ALL_PAIRS = list(product(S, S))
FORBIDDEN = [pair for pair in ALL_PAIRS if pair not in ALLOWED]
EID = UUID("00000000-0000-4000-8000-000000000001")


def in_status(status: ExecutionStatus) -> Execution:
    extra: dict[str, object] = {}
    if status is S.DENIED:
        extra["policy_reasons"] = ("no_policy",)
    if status is S.FAILED:
        extra["error"] = ExecutionErrorInfo(type="ExecutionFailedError", message="x")
    if status is S.COMPLETED:
        extra["outcome"] = make_outcome(EID, accepted=True)
    return make_execution(execution_id=EID, status=status, **extra)


def changes_for(target: ExecutionStatus) -> dict[str, Any]:
    if target is S.DENIED:
        return {
            "policy_reasons": ("missing_capability:x.read",),
            "granted_capabilities": frozenset(),
        }
    if target is S.FAILED:
        return {"error": ExecutionErrorInfo(type="ExecutionFailedError", message="boom")}
    if target is S.COMPLETED:
        return {"outcome": make_outcome(EID, accepted=True)}
    return {}


def test_table_matches_specification_plus_reconciliation() -> None:
    implemented = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    assert implemented == ALLOWED
    assert implemented - SPEC_TABLE == D51_REOPEN
    assert set(TRANSITIONS) == set(S)


def test_terminal_states_have_no_exits() -> None:
    for status in S:
        if status in {S.FAILED, S.VALIDATION_FAILED}:
            assert status.is_terminal
            assert TRANSITIONS[status] == frozenset({S.READY})
        else:
            assert (not TRANSITIONS[status]) == status.is_terminal


@pytest.mark.parametrize(("source", "target"), sorted(ALLOWED, key=str))
def test_allowed_transitions(source: ExecutionStatus, target: ExecutionStatus) -> None:
    record = in_status(source)
    at = T0 + timedelta(seconds=5)
    moved = transition_execution(record, target, at=at, **changes_for(target))
    assert moved.status is target
    assert moved.revision == record.revision + 1
    assert moved.updated_at == at
    assert moved.finished_at == (at if target.is_terminal else None)
    assert moved.execution_id == record.execution_id
    assert record.status is source  # the original record is untouched


@pytest.mark.parametrize(("source", "target"), FORBIDDEN, ids=lambda s: s.value)
def test_every_forbidden_transition_raises(
    source: ExecutionStatus, target: ExecutionStatus
) -> None:
    assert not can_transition(source, target)
    with pytest.raises(InvalidStateTransitionError) as info:
        transition_execution(in_status(source), target, at=T0, **changes_for(target))
    assert info.value.from_status == source.value
    assert info.value.to_status == target.value
    assert info.value.execution_id == EID
    assert "(" not in str(info.value)  # rejected by the table, not by an invariant


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.CREATED, S.RUNNING),
        (S.DENIED, S.RUNNING),
        (S.COMPLETED, S.RUNNING),
        (S.FAILED, S.RUNNING),
    ],
)
def test_specification_examples_are_forbidden(
    source: ExecutionStatus, target: ExecutionStatus
) -> None:
    with pytest.raises(InvalidStateTransitionError):
        transition_execution(in_status(source), target, at=T0)


def test_reason_is_recorded_and_cleared() -> None:
    cancelled = transition_execution(in_status(S.READY), S.CANCELLED, at=T0, reason="duplicate")
    assert cancelled.status_reason == "duplicate"
    ready = transition_execution(in_status(S.POLICY_CHECK), S.READY, at=T0)
    assert ready.status_reason is None


@pytest.mark.parametrize(
    "field",
    [
        "execution_id",
        "revision",
        "actor_id",
        "tenant_id",
        "definition_digest",
        "status",
        "finished_at",
    ],
)
def test_protected_fields_cannot_change(field: str) -> None:
    with pytest.raises(InvalidStateTransitionError, match="protected fields"):
        transition_execution(in_status(S.CREATED), S.POLICY_CHECK, at=T0, **{field: None})


def test_invariant_violations_are_wrapped() -> None:
    with pytest.raises(InvalidStateTransitionError, match="verified outcome") as info:
        transition_execution(in_status(S.VALIDATING), S.COMPLETED, at=T0)
    assert isinstance(info.value.__cause__, ValidationError)
    with pytest.raises(InvalidStateTransitionError, match="FAILED requires"):
        transition_execution(in_status(S.RUNNING), S.FAILED, at=T0)
    with pytest.raises(InvalidStateTransitionError, match="verified outcome"):
        transition_execution(
            in_status(S.VALIDATING),
            S.COMPLETED,
            at=T0,
            outcome=make_outcome(EID, passed=False),
        )


def test_time_cannot_move_backwards() -> None:
    record = in_status(S.CREATED)
    with pytest.raises(InvalidStateTransitionError, match="backwards"):
        transition_execution(record, S.POLICY_CHECK, at=T0 - timedelta(seconds=1))


def test_naive_time_is_rejected() -> None:
    from datetime import datetime  # noqa: PLC0415

    with pytest.raises(ValueError, match="timezone-aware"):
        transition_execution(
            in_status(S.CREATED),
            S.POLICY_CHECK,
            at=datetime(2026, 11, 6, 13),
        )


def test_full_happy_path_increments_revision() -> None:
    record = in_status(S.CREATED)
    path = [S.POLICY_CHECK, S.READY, S.RUNNING, S.VALIDATING, S.COMPLETED]
    for step, status in enumerate(path, start=1):
        record = transition_execution(
            record, status, at=T0 + timedelta(seconds=step), **changes_for(status)
        )
    assert record.revision == len(path)
    assert record.is_completed
    assert record.raise_for_status().verified
