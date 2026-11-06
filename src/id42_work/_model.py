"""Shared base model for immutable domain records."""

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable, closed-schema base for every SDK domain model."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
