from datetime import timedelta
from uuid import uuid4

import pytest
from _factories import T0, make_execution, make_outcome
from pydantic import ValidationError

from id42_work import (
    ApprovalRejectedError,
    ApprovalRequiredError,
    CapabilityDeniedError,
    Execution,
    ExecutionCancelledError,
    ExecutionError,
    ExecutionErrorInfo,
    ExecutionFailedError,
    ExecutionStatus,
    NonDeterministicExecutionError,
    OutcomeValidationError,
    PolicyDeniedError,
    UncertainOutcomeError,
)
from id42_work.states import TERMINAL_STATUSES


def test_terminal_statuses() -> None:
    assert {s.value for s in TERMINAL_STATUSES} == {
        "DENIED",
        "FAILED",
        "VALIDATION_FAILED",
        "COMPLETED",
        "REJECTED",
        "CANCELLED",
    }
    assert not ExecutionStatus.WAITING_REVIEW.is_terminal


def test_minimal_execution_round_trip() -> None:
    execution = make_execution()
    assert execution.status is ExecutionStatus.CREATED
    assert not execution.is_terminal
    assert Execution.model_validate_json(execution.model_dump_json()) == execution


def test_completed_requires_verified_accepted_outcome() -> None:
    eid = uuid4()
    done = make_execution(
        execution_id=eid,
        status=ExecutionStatus.COMPLETED,
        outcome=make_outcome(eid, accepted=True),
    )
    assert done.is_completed
    assert done.raise_for_status().verified

    with pytest.raises(ValidationError, match="verified outcome"):
        make_execution(execution_id=eid, status=ExecutionStatus.COMPLETED)
    with pytest.raises(ValidationError, match="verified outcome"):
        make_execution(
            execution_id=eid,
            status=ExecutionStatus.COMPLETED,
            outcome=make_outcome(eid, passed=False),
        )
    with pytest.raises(ValidationError, match="accepted outcome"):
        make_execution(
            execution_id=eid, status=ExecutionStatus.COMPLETED, outcome=make_outcome(eid)
        )


def test_only_completed_has_accepted_outcome() -> None:
    eid = uuid4()
    with pytest.raises(ValidationError, match="only a COMPLETED"):
        make_execution(
            execution_id=eid,
            status=ExecutionStatus.WAITING_REVIEW,
            outcome=make_outcome(eid, accepted=True),
        )


def test_structural_invariants() -> None:
    with pytest.raises(ValidationError, match="finished_at"):
        make_execution(status=ExecutionStatus.CANCELLED, finished_at=None)
    with pytest.raises(ValidationError, match="finished_at"):
        make_execution(status=ExecutionStatus.RUNNING, finished_at=T0)
    with pytest.raises(ValidationError, match="FAILED requires"):
        make_execution(status=ExecutionStatus.FAILED)
    with pytest.raises(ValidationError, match="DENIED requires"):
        make_execution(status=ExecutionStatus.DENIED)
    with pytest.raises(ValidationError, match="hold no capabilities"):
        make_execution(
            status=ExecutionStatus.DENIED,
            policy_reasons=("x",),
            granted_capabilities=frozenset({"a.read"}),
        )
    with pytest.raises(ValidationError, match="another execution"):
        make_execution(outcome=make_outcome(uuid4()))
    with pytest.raises(ValidationError, match="precede"):
        make_execution(updated_at=T0 - timedelta(seconds=1))


def _failed(error_type: str, *, reconcile: bool = False) -> Execution:
    return make_execution(
        status=ExecutionStatus.FAILED,
        error=ExecutionErrorInfo(
            type=error_type, message="boom", requires_reconciliation=reconcile
        ),
    )


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        (
            make_execution(status=ExecutionStatus.DENIED, policy_reasons=("no_policy",)),
            PolicyDeniedError,
        ),
        (make_execution(status=ExecutionStatus.WAITING_APPROVAL), ApprovalRequiredError),
        (make_execution(status=ExecutionStatus.WAITING_REVIEW), ApprovalRequiredError),
        (
            make_execution(status=ExecutionStatus.REJECTED, status_reason="no"),
            ApprovalRejectedError,
        ),
        (_failed("ExecutionFailedError"), ExecutionFailedError),
        (_failed("NonDeterministicExecutionError"), NonDeterministicExecutionError),
        (_failed("UncertainOutcomeError", reconcile=True), UncertainOutcomeError),
        (_failed("CapabilityDeniedError"), CapabilityDeniedError),
        (_failed("KeyError"), ExecutionFailedError),
        (make_execution(status=ExecutionStatus.VALIDATION_FAILED), OutcomeValidationError),
        (make_execution(status=ExecutionStatus.CANCELLED), ExecutionCancelledError),
        (make_execution(status=ExecutionStatus.RUNNING), ExecutionError),
    ],
)
def test_raise_for_status(execution: Execution, expected: type[Exception]) -> None:
    with pytest.raises(expected) as info:
        execution.raise_for_status()
    assert type(info.value) is expected
    assert info.value.execution_id == execution.execution_id  # type: ignore[attr-defined]


def test_denied_error_carries_reasons() -> None:
    execution = make_execution(
        status=ExecutionStatus.DENIED, policy_reasons=("missing_capability:x.read",)
    )
    with pytest.raises(PolicyDeniedError) as info:
        execution.raise_for_status()
    assert info.value.reasons == ("missing_capability:x.read",)


def test_requires_reconciliation() -> None:
    assert _failed("UncertainOutcomeError", reconcile=True).requires_reconciliation
    assert not make_execution().requires_reconciliation


def test_error_info_preserves_cause_chain() -> None:
    def fail() -> None:
        try:
            try:
                raise KeyError("supplier")
            except KeyError as inner:
                raise ValueError("lookup failed") from inner
        except ValueError as outer:
            raise ExecutionFailedError("executor failed") from outer

    with pytest.raises(ExecutionFailedError) as info:
        fail()
    described = ExecutionErrorInfo.from_exception(info.value, requires_reconciliation=True)
    assert described.type == "ExecutionFailedError"
    assert [c.type for c in described.causes] == ["ValueError", "KeyError"]
    assert described.requires_reconciliation


def test_error_info_handles_cycles() -> None:
    first = ValueError("a")
    second = ValueError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert len(ExecutionErrorInfo.from_exception(first).causes) == 1
