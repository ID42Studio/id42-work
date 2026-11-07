"""Approval records and action binding (specification 3.5 and 7.2)."""

import re
from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.canonical import digest
from id42_work.identifiers import (
    ACTION_ID_PATTERN,
    Capability,
    Grant,
    NonEmptyStr,
    Sha256Hex,
)
from id42_work.policy.models import ApprovalGate

START_ACTION_ID = "__start__"
"""Action id of ``START`` gate approvals."""

OUTCOME_ACTION_ID = "__outcome__"
"""Action id of ``OUTCOME`` gate approvals."""

_ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)


class ApprovalStatus(Enum):
    """Lifecycle of an approval."""

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    CONSUMED = "CONSUMED"


APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.GRANTED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.INVALIDATED,
        }
    ),
    ApprovalStatus.GRANTED: frozenset(
        {ApprovalStatus.CONSUMED, ApprovalStatus.EXPIRED, ApprovalStatus.INVALIDATED}
    ),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.INVALIDATED: frozenset(),
    ApprovalStatus.CONSUMED: frozenset(),
}
"""Allowed approval status changes (specification 7.4)."""

_DECIDED = frozenset({ApprovalStatus.GRANTED, ApprovalStatus.REJECTED, ApprovalStatus.CONSUMED})


def compute_action_digest(
    *,
    execution_id: UUID,
    work_id: str,
    work_version: str,
    definition_digest: str,
    gate: ApprovalGate,
    action_id: str,
    capability: str | None,
    params: object,
) -> str:
    """Digest binding an approval to one exact action and its parameters.

    For ``START`` gates ``params`` is the validated input; for ``OUTCOME`` gates it is
    the validated output. Any change to any part produces a different digest.
    """
    return digest(
        {
            "execution_id": execution_id,
            "work_id": work_id,
            "work_version": work_version,
            "definition_digest": definition_digest,
            "gate": gate,
            "action_id": action_id,
            "capability": capability,
            "params": params,
        }
    )


class Approval(FrozenModel):
    """An authorization decision tied to one execution, one gate and one exact action."""

    approval_id: UUID = Field(default_factory=uuid4)
    execution_id: UUID
    gate: ApprovalGate
    action_id: NonEmptyStr
    capability: Capability | None = None
    action_digest: Sha256Hex
    params_preview: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    reviewer_capability: Grant
    reason_required: bool = False
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: UtcDatetime
    expires_at: UtcDatetime
    decided_at: UtcDatetime | None = None
    decision_actor_id: NonEmptyStr | None = None
    reason: str | None = None
    consumed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems: list[str] = []
        if self.gate is ApprovalGate.START and (
            self.action_id != START_ACTION_ID or self.capability is not None
        ):
            problems.append(f"START approvals use action_id {START_ACTION_ID!r} and no capability")
        if self.gate is ApprovalGate.OUTCOME and (
            self.action_id != OUTCOME_ACTION_ID or self.capability is not None
        ):
            problems.append(
                f"OUTCOME approvals use action_id {OUTCOME_ACTION_ID!r} and no capability"
            )
        if self.gate is ApprovalGate.ACTION and (
            _ACTION_ID_RE.fullmatch(self.action_id) is None or self.capability is None
        ):
            problems.append("ACTION approvals need a valid action_id and a capability")
        if self.expires_at <= self.requested_at:
            problems.append("expires_at must be after requested_at")
        decided = self.decided_at is not None
        if decided != (self.decision_actor_id is not None):
            problems.append("decided_at and decision_actor_id are set together")
        if self.status in _DECIDED and not decided:
            problems.append(f"{self.status.value} approvals record the decision")
        if self.status is ApprovalStatus.PENDING and decided:
            problems.append("PENDING approvals have no decision")
        if self.decided_at is not None and self.decided_at < self.requested_at:
            problems.append("decided_at cannot precede requested_at")
        if self.status is ApprovalStatus.REJECTED and not self.reason:
            problems.append("rejections require a reason")
        if self.status is ApprovalStatus.GRANTED and self.reason_required and not self.reason:
            problems.append("this approval requires a reason")
        if (self.status is ApprovalStatus.CONSUMED) != (self.consumed_at is not None):
            problems.append("consumed_at is set exactly when the status is CONSUMED")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def is_expired(self, now: UtcDatetime) -> bool:
        """Whether the approval's validity window has ended at ``now``."""
        return now >= self.expires_at

    def is_usable_for(self, action_digest: str, now: UtcDatetime) -> bool:
        """Whether this approval authorizes exactly ``action_digest`` at ``now``."""
        return (
            self.status is ApprovalStatus.GRANTED
            and not self.is_expired(now)
            and self.action_digest == action_digest
        )
