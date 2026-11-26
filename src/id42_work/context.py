"""Concrete execution and validation contexts handed to user code (specification 5.6, 9).

The contexts hold no reference to repositories, registries, the approval manager or the
actor authority: every capability call goes through a function supplied by the runner.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from uuid import UUID

from pydantic import BaseModel, JsonValue

from id42_work.evidence.models import EvidenceType
from id42_work.policy.capabilities import CapabilitySpec

InvokeFn = Callable[[str, str, Mapping[str, object]], Awaitable[JsonValue]]
"""``(capability, action_id, params) -> result``, provided by the runner."""

ComputeFn = Callable[[], Awaitable[JsonValue]]
"""A non-deterministic computation whose JSON result ``context.record`` journals."""

RecordFn = Callable[[str, object, ComputeFn], Awaitable[JsonValue]]
"""``(action_id, fingerprint, compute) -> result``, provided by the runner."""


class ExecutionSuspended(BaseException):
    """Internal signal: the execution waits for an approval.

    Derives from ``BaseException`` so that ``except Exception`` in executor code does not
    swallow it. The runner also records the suspension independently, so swallowing
    it with ``except BaseException`` does not bypass the gate either.
    """


class PendingEvidence:
    """Evidence requested by an executor, persisted by the runner after the run."""

    __slots__ = ("content_digest", "content_reference", "evidence_type", "metadata")

    def __init__(
        self,
        evidence_type: EvidenceType,
        content_reference: str | None,
        content_digest: str | None,
        metadata: Mapping[str, JsonValue],
    ) -> None:
        self.evidence_type = evidence_type
        self.content_reference = content_reference
        self.content_digest = content_digest
        self.metadata = dict(metadata)


class RunContext:
    """The ``ExecutionContext`` implementation."""

    def __init__(
        self,
        *,
        execution_id: UUID,
        work_id: str,
        work_version: str,
        actor_id: str,
        tenant_id: str | None,
        granted_capabilities: frozenset[str],
        invoke: InvokeFn,
        new_evidence_id: Callable[[], UUID],
        record: RecordFn,
        capability_specs: Mapping[str, CapabilitySpec],
    ) -> None:
        self._execution_id = execution_id
        self._work_id = work_id
        self._work_version = work_version
        self._actor_id = actor_id
        self._tenant_id = tenant_id
        self._granted = granted_capabilities
        self._invoke = invoke
        self._record = record
        self._specs = MappingProxyType(
            {
                name: spec.model_copy(deep=True)
                for name, spec in capability_specs.items()
                if name in granted_capabilities
            }
        )
        self._new_evidence_id = new_evidence_id
        self._evidence: list[tuple[UUID, PendingEvidence]] = []
        self._logger = logging.getLogger(f"id42_work.work.{work_id}")

    @property
    def execution_id(self) -> UUID:
        return self._execution_id

    @property
    def work_id(self) -> str:
        return self._work_id

    @property
    def work_version(self) -> str:
        return self._work_version

    @property
    def actor_id(self) -> str:
        return self._actor_id

    @property
    def tenant_id(self) -> str | None:
        return self._tenant_id

    @property
    def granted_capabilities(self) -> frozenset[str]:
        return self._granted

    @property
    def capability_specs(self) -> Mapping[str, CapabilitySpec]:
        return self._specs

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    async def invoke(
        self, capability: str, *, action_id: str, params: Mapping[str, object]
    ) -> JsonValue:
        return await self._invoke(capability, action_id, params)

    async def record(self, action_id: str, *, fingerprint: object, compute: ComputeFn) -> JsonValue:
        return await self._record(action_id, fingerprint, compute)

    def add_evidence(
        self,
        evidence_type: EvidenceType,
        *,
        content_reference: str | None = None,
        content_digest: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UUID:
        evidence_id = self._new_evidence_id()
        pending = PendingEvidence(evidence_type, content_reference, content_digest, metadata or {})
        self._evidence.append((evidence_id, pending))
        return evidence_id

    def drain_evidence(self) -> list[tuple[UUID, PendingEvidence]]:
        """Return and clear evidence added by the executor (runner use only)."""
        drained, self._evidence = self._evidence, []
        return drained


class ValidatorContext:
    """The ``ValidationContext`` implementation."""

    def __init__(
        self,
        *,
        execution_id: UUID,
        work_id: str,
        work_version: str,
        input_data: BaseModel,
        invoke: InvokeFn,
    ) -> None:
        self._execution_id = execution_id
        self._work_id = work_id
        self._work_version = work_version
        self._input_data = input_data
        self._invoke = invoke

    @property
    def execution_id(self) -> UUID:
        return self._execution_id

    @property
    def work_id(self) -> str:
        return self._work_id

    @property
    def work_version(self) -> str:
        return self._work_version

    @property
    def input_data(self) -> BaseModel:
        return self._input_data

    async def invoke(
        self, capability: str, *, action_id: str, params: Mapping[str, object]
    ) -> JsonValue:
        return await self._invoke(capability, action_id, params)
