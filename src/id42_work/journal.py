"""Action journal entries (specification 8.1)."""

from enum import Enum
from typing import Self
from uuid import UUID, uuid5

from pydantic import JsonValue, model_validator

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.execution import ExecutionErrorInfo
from id42_work.identifiers import ActionId, Capability, Sha256Hex

NAMESPACE_ID42_WORK = UUID("20ff4f61-4ffc-554c-a314-6d40df64b16a")
"""Fixed namespace for operation ids: ``uuid5(NAMESPACE_URL, "https://id42studio.github.io/id42-work")``.

Never change this value: operation ids forwarded to external systems depend on it.
"""


def operation_id(execution_id: UUID, action_id: str) -> UUID:
    """Stable id of one action, identical across replays; forwarded to integrations."""
    return uuid5(NAMESPACE_ID42_WORK, f"{execution_id}:{action_id}")


class JournalStatus(Enum):
    """State of one journaled action."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class JournalEntry(FrozenModel):
    """Record of one ``context.invoke`` call.

    ``result`` holds the provider's JSON-compatible result so that replays return the
    same value without calling the provider again.
    """

    execution_id: UUID
    action_id: ActionId
    capability: Capability
    params_digest: Sha256Hex
    operation_id: UUID
    status: JournalStatus
    result: JsonValue = None
    result_digest: Sha256Hex | None = None
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    error: ExecutionErrorInfo | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems: list[str] = []
        if self.operation_id != operation_id(self.execution_id, self.action_id):
            problems.append("operation_id must be derived from execution_id and action_id")
        if self.status is JournalStatus.PENDING_APPROVAL and self.started_at is not None:
            problems.append("PENDING_APPROVAL actions have not started")
        if self.status is not JournalStatus.PENDING_APPROVAL and self.started_at is None:
            problems.append(f"{self.status.value} actions record started_at")
        finished = self.status in {JournalStatus.SUCCEEDED, JournalStatus.FAILED}
        if finished != (self.finished_at is not None):
            problems.append("finished_at is set exactly for SUCCEEDED and FAILED actions")
        if (self.status is JournalStatus.SUCCEEDED) != (self.result_digest is not None):
            problems.append("result_digest is set exactly for SUCCEEDED actions")
        if self.status is JournalStatus.FAILED and self.error is None:
            problems.append("FAILED actions record the error")
        if problems:
            raise ValueError("; ".join(problems))
        return self
