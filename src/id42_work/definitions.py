"""Work definitions (specification 3.1).

A ``Work`` is a pure, immutable contract: identity, schemas, required capabilities and
an autonomy ceiling. It holds no executor, validator or policy. Its serializable form
is a ``WorkDescriptor``, whose canonical digest identifies the definition.
"""

from typing import Annotated

from pydantic import BaseModel, Field, JsonValue, StringConstraints

from id42_work._model import FrozenModel
from id42_work.canonical import digest
from id42_work.identifiers import Capability, Identifier, NonEmptyStr, SemVer
from id42_work.policy.models import AutonomyLevel


class WorkRef(FrozenModel):
    """Identity of a work definition: ``(id, version)``."""

    id: Identifier
    version: SemVer

    def __str__(self) -> str:
        return f"{self.id}@{self.version}"


class SchemaDescriptor(FrozenModel):
    """Serializable reference to a Pydantic schema."""

    ref: NonEmptyStr
    json_schema: dict[str, JsonValue]


class WorkDescriptor(FrozenModel):
    """Serializable form of a ``Work``; contains no executable code."""

    id: Identifier
    version: SemVer
    name: NonEmptyStr
    description: str
    input_schema: SchemaDescriptor
    output_schema: SchemaDescriptor
    required_capabilities: tuple[Capability, ...]
    autonomy_ceiling: AutonomyLevel

    @property
    def definition_digest(self) -> str:
        """SHA-256 of the canonical JSON of this descriptor."""
        return digest(self, floats="repr")


def _describe_schema(schema: type[BaseModel]) -> SchemaDescriptor:
    return SchemaDescriptor(
        ref=f"{schema.__module__}:{schema.__qualname__}",
        json_schema=schema.model_json_schema(mode="validation"),
    )


class Work(FrozenModel):
    """A versioned definition of a unit of business or technical work."""

    id: Identifier
    version: SemVer
    name: NonEmptyStr
    description: Annotated[str, StringConstraints(max_length=4096)] = ""
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    required_capabilities: frozenset[Capability] = Field(default_factory=frozenset[Capability])
    autonomy_ceiling: AutonomyLevel = AutonomyLevel.AUTONOMOUS

    @property
    def ref(self) -> WorkRef:
        """The ``(id, version)`` identity of this definition."""
        return WorkRef(id=self.id, version=self.version)

    def describe(self) -> WorkDescriptor:
        """Return the serializable form of this definition."""
        return WorkDescriptor(
            id=self.id,
            version=self.version,
            name=self.name,
            description=self.description,
            input_schema=_describe_schema(self.input_schema),
            output_schema=_describe_schema(self.output_schema),
            required_capabilities=tuple(sorted(self.required_capabilities)),
            autonomy_ceiling=self.autonomy_ceiling,
        )

    @property
    def definition_digest(self) -> str:
        """SHA-256 identifying this exact definition (stable across processes)."""
        return self.describe().definition_digest
