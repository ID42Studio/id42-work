"""Structured execution events (specification 11)."""

from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, PositiveInt

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.identifiers import NonEmptyStr


class EventType(Enum):
    """Event types emitted by the SDK."""

    DEFINITION_REGISTERED = "work.definition.registered"
    DEFINITION_REPLACED = "work.definition.replaced"
    EXECUTION_CREATED = "work.execution.created"
    POLICY_EVALUATED = "work.policy.evaluated"
    EXECUTION_DENIED = "work.execution.denied"
    EXECUTION_AUTHORIZED = "work.execution.authorized"
    APPROVAL_REQUESTED = "work.approval.requested"
    APPROVAL_GRANTED = "work.approval.granted"
    APPROVAL_REJECTED = "work.approval.rejected"
    APPROVAL_EXPIRED = "work.approval.expired"
    APPROVAL_INVALIDATED = "work.approval.invalidated"
    APPROVAL_CONSUMED = "work.approval.consumed"
    EXECUTION_STARTED = "work.execution.started"
    EXECUTION_RESUMED = "work.execution.resumed"
    ACTION_INVOKED = "work.action.invoked"
    ACTION_REPLAYED = "work.action.replayed"
    ACTION_COMPLETED = "work.action.completed"
    ACTION_FAILED = "work.action.failed"
    ACTION_UNCERTAIN = "work.action.uncertain"
    ACTION_RECONCILED = "work.action.reconciled"
    EXECUTION_REOPENED = "work.execution.reopened"
    EXECUTION_SUSPENDED = "work.execution.suspended"
    EXECUTION_FAILED = "work.execution.failed"
    VALIDATION_STARTED = "work.outcome.validation_started"
    VALIDATION_PASSED = "work.outcome.validation_passed"
    VALIDATION_FAILED = "work.outcome.validation_failed"
    REVIEW_REQUESTED = "work.outcome.review_requested"
    EXECUTION_COMPLETED = "work.execution.completed"
    EXECUTION_REJECTED = "work.execution.rejected"
    EXECUTION_CANCELLED = "work.execution.cancelled"


class WorkEvent(FrozenModel):
    """An append-only, structured event.

    ``execution_id`` is ``None`` for registry-level events. ``payload`` must be
    redacted: it never contains raw inputs, outputs, parameters or secrets.
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: EventType
    schema_version: Literal["1"] = "1"
    execution_id: UUID | None
    sequence_number: PositiveInt
    timestamp: UtcDatetime
    actor_reference: NonEmptyStr | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
