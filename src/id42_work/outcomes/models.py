"""Outcome and validation result models (specification 3.6 and 9)."""

from enum import Enum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.identifiers import Identifier


class Severity(Enum):
    """Severity of a validation result."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class ValidationResult(FrozenModel):
    """Result of one validator applied to one output."""

    validator_id: Identifier
    passed: bool
    severity: Severity
    message: str = ""
    required: bool = True
    evidence_ids: tuple[UUID, ...] = ()


class Outcome(FrozenModel):
    """Result produced by an execution, with its verification state.

    ``verified`` is derived, not asserted: it must equal "every required validation
    result passed", and at least one required result (the schema validator) must
    exist. ``accepted_at`` is set only when the execution reached ``COMPLETED``.
    """

    execution_id: UUID
    output: SerializeAsAny[BaseModel]
    verified: bool
    validation_results: tuple[ValidationResult, ...] = Field(min_length=1)
    evidence_ids: tuple[UUID, ...] = ()
    produced_at: UtcDatetime
    accepted_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        required = [result for result in self.validation_results if result.required]
        if not required:
            msg = "an outcome needs at least one required validation result"
            raise ValueError(msg)
        if self.verified != all(result.passed for result in required):
            msg = "verified must equal 'all required validation results passed'"
            raise ValueError(msg)
        if self.accepted_at is not None:
            if not self.verified:
                msg = "an unverified outcome cannot be accepted"
                raise ValueError(msg)
            if self.accepted_at < self.produced_at:
                msg = "accepted_at cannot precede produced_at"
                raise ValueError(msg)
        return self

    @property
    def failed_results(self) -> tuple[ValidationResult, ...]:
        """Validation results that did not pass."""
        return tuple(result for result in self.validation_results if not result.passed)
