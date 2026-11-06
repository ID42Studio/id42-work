"""Capability declarations exchanged with providers (specification 5.7)."""

from uuid import UUID

from id42_work._model import FrozenModel
from id42_work.identifiers import ActionId, Capability, NonEmptyStr


class CapabilitySpec(FrozenModel):
    """A capability offered by a provider."""

    name: Capability
    side_effecting: bool
    description: str = ""


class CapabilityCall(FrozenModel):
    """Context passed to a provider for one capability invocation."""

    execution_id: UUID
    action_id: ActionId
    operation_id: UUID
    actor_id: NonEmptyStr
    tenant_id: NonEmptyStr | None = None
