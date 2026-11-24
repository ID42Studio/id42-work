"""Capability declarations exchanged with providers (specification 5.7)."""

from typing import Self
from uuid import UUID

from pydantic import JsonValue, model_validator

from id42_work._model import FrozenModel
from id42_work.identifiers import ActionId, Capability, NonEmptyStr


class CapabilitySpec(FrozenModel):
    """A capability offered by a provider.

    ``params_schema`` is an optional JSON Schema (an ``object`` schema) describing the
    parameters the provider accepts. Agent integrations use it to describe the
    capability as a tool. The SDK does not validate parameters against it: providers
    must still validate what they receive.
    """

    name: Capability
    side_effecting: bool
    description: str = ""
    params_schema: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        schema = self.params_schema
        if schema is not None and schema.get("type", "object") != "object":
            msg = "params_schema must describe an object (type 'object')"
            raise ValueError(msg)
        return self


class CapabilityCall(FrozenModel):
    """Context passed to a provider for one capability invocation."""

    execution_id: UUID
    action_id: ActionId
    operation_id: UUID
    actor_id: NonEmptyStr
    tenant_id: NonEmptyStr | None = None
