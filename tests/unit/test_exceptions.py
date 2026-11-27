import inspect

import pytest

import id42_work
from id42_work import exceptions as exc

HIERARCHY = {
    "WorkDefinitionError": "WorkError",
    "WorkRegistrationError": "WorkError",
    "InputValidationError": "WorkError",
    "PolicyError": "WorkError",
    "PolicyDeniedError": "PolicyError",
    "CapabilityDeniedError": "PolicyError",
    "UntrustedActorError": "PolicyError",
    "ApprovalError": "WorkError",
    "ApprovalRequiredError": "ApprovalError",
    "ApprovalRejectedError": "ApprovalError",
    "ApprovalExpiredError": "ApprovalError",
    "ApprovalNotAllowedError": "ApprovalError",
    "ExecutionError": "WorkError",
    "ExecutionFailedError": "ExecutionError",
    "ExecutionCancelledError": "ExecutionError",
    "InvalidStateTransitionError": "ExecutionError",
    "IdempotencyConflictError": "ExecutionError",
    "NonDeterministicExecutionError": "ExecutionError",
    "UncertainOutcomeError": "ExecutionError",
    "OutcomeError": "WorkError",
    "OutcomeValidationError": "OutcomeError",
    "StorageError": "WorkError",
    "ConcurrentModificationError": "StorageError",
    "ReconciliationError": "WorkError",
    "ReconciliationNotAllowedError": "ReconciliationError",
    "AuthorityKeyError": "WorkError",
}


@pytest.mark.parametrize(("name", "parent"), HIERARCHY.items())
def test_hierarchy(name: str, parent: str) -> None:
    cls = getattr(exc, name)
    assert cls.__bases__ == (getattr(exc, parent),)
    assert getattr(id42_work, name) is cls


def test_hierarchy_is_complete() -> None:
    defined = {
        name
        for name, obj in inspect.getmembers(exc, inspect.isclass)
        if issubclass(obj, exc.WorkError) and obj is not exc.WorkError
    }
    assert defined == set(HIERARCHY)
    assert "ValidationError" not in dir(exc)


def test_invalid_transition_message() -> None:
    error = exc.InvalidStateTransitionError("CREATED", "RUNNING")
    assert str(error) == "invalid execution state transition: CREATED -> RUNNING"
    assert (error.from_status, error.to_status) == ("CREATED", "RUNNING")


def test_wrapping_preserves_cause() -> None:
    original = KeyError("supplier")
    with pytest.raises(exc.ExecutionFailedError) as info:
        raise exc.ExecutionFailedError("failed") from original
    assert info.value.__cause__ is original
