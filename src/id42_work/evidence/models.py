"""Evidence records supporting execution and verification (specification 3.7)."""

from enum import Enum
from uuid import UUID, uuid4

from pydantic import Field, JsonValue

from id42_work._model import FrozenModel
from id42_work._time import UtcDatetime
from id42_work.identifiers import NonEmptyStr, Sha256Hex


class EvidenceType(Enum):
    """Kinds of evidence the SDK records."""

    INPUT_REFERENCE = "INPUT_REFERENCE"
    TOOL_RESULT = "TOOL_RESULT"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    RECONCILIATION_DECISION = "RECONCILIATION_DECISION"
    EXTERNAL_TRANSACTION = "EXTERNAL_TRANSACTION"
    STRUCTURED_EVENT = "STRUCTURED_EVENT"
    OUTPUT_ARTIFACT = "OUTPUT_ARTIFACT"


class Evidence(FrozenModel):
    """A reference to information that supports an execution or its verification.

    Evidence stores references and digests, not large payloads. ``metadata`` must be
    redacted before it reaches this model.
    """

    evidence_id: UUID = Field(default_factory=uuid4)
    execution_id: UUID
    evidence_type: EvidenceType
    source: NonEmptyStr
    created_at: UtcDatetime
    content_reference: str | None = None
    content_digest: Sha256Hex | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
