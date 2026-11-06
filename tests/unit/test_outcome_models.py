from datetime import timedelta
from uuid import uuid4

import pytest
from _factories import T0, InvoiceOutput, make_outcome, make_output
from pydantic import ValidationError

from id42_work import Outcome, Severity, ValidationResult


def _result(*, passed: bool, required: bool = True, vid: str = "rules") -> ValidationResult:
    return ValidationResult(
        validator_id=vid, passed=passed, severity=Severity.ERROR, required=required
    )


def test_verified_is_derived_from_required_results() -> None:
    eid = uuid4()
    advisory_failure = _result(passed=False, required=False, vid="advisory")
    outcome = make_outcome(eid, extra=(advisory_failure,))
    assert outcome.verified
    assert outcome.failed_results == (advisory_failure,)

    assert not make_outcome(eid, extra=(_result(passed=False),)).verified

    with pytest.raises(ValidationError, match="verified must equal"):
        Outcome(
            execution_id=eid,
            output=make_output(),
            verified=True,
            validation_results=(_result(passed=False),),
            produced_at=T0,
        )


def test_outcome_needs_a_required_result() -> None:
    with pytest.raises(ValidationError):
        Outcome(
            execution_id=uuid4(),
            output=make_output(),
            verified=True,
            validation_results=(),
            produced_at=T0,
        )
    with pytest.raises(ValidationError, match="at least one required"):
        Outcome(
            execution_id=uuid4(),
            output=make_output(),
            verified=True,
            validation_results=(_result(passed=True, required=False),),
            produced_at=T0,
        )


def test_acceptance_rules() -> None:
    eid = uuid4()
    with pytest.raises(ValidationError, match="unverified"):
        Outcome(
            execution_id=eid,
            output=make_output(),
            verified=False,
            validation_results=(_result(passed=False),),
            produced_at=T0,
            accepted_at=T0,
        )
    with pytest.raises(ValidationError, match="precede"):
        Outcome(
            execution_id=eid,
            output=make_output(),
            verified=True,
            validation_results=(_result(passed=True),),
            produced_at=T0,
            accepted_at=T0 - timedelta(seconds=1),
        )


def test_output_keeps_its_concrete_type_and_fields() -> None:
    outcome = make_outcome(uuid4())
    assert isinstance(outcome.output, InvoiceOutput)
    assert outcome.model_dump()["output"] == {
        "invoice_number": "INV-1001",
        "supplier_id": "SUP-001",
        "status": "valid",
    }
