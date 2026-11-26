"""Protocols implemented by applications and integrations (specification 5 and 9).

Executors, validators, capability providers and policy resolvers are plain classes that
satisfy these protocols structurally; they never subclass SDK types.
"""

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, JsonValue

from id42_work.actors import TrustedActor
from id42_work.definitions import Work
from id42_work.evidence.models import EvidenceType
from id42_work.outcomes.models import ValidationResult
from id42_work.policy.capabilities import CapabilityCall, CapabilitySpec
from id42_work.policy.models import Policy

InputT_contra = TypeVar("InputT_contra", bound=BaseModel, contravariant=True)
OutputT_co = TypeVar("OutputT_co", bound=BaseModel, covariant=True)


class ExecutionContext(Protocol):
    """Restricted view of an execution handed to an executor.

    It exposes only the granted capabilities. It never exposes registries,
    repositories, the approval manager or the actor authority.
    """

    @property
    def execution_id(self) -> UUID: ...

    @property
    def work_id(self) -> str: ...

    @property
    def work_version(self) -> str: ...

    @property
    def actor_id(self) -> str: ...

    @property
    def tenant_id(self) -> str | None: ...

    @property
    def granted_capabilities(self) -> frozenset[str]: ...

    @property
    def capability_specs(self) -> Mapping[str, CapabilitySpec]:
        """Copies of the granted capabilities' specs (descriptions, parameter schemas)."""
        ...

    @property
    def logger(self) -> logging.Logger: ...

    async def invoke(
        self, capability: str, *, action_id: str, params: Mapping[str, object]
    ) -> JsonValue:
        """Invoke a granted capability through its provider.

        ``action_id`` must be unique within one executor run and stable across
        replays. ``params`` must be canonicalizable (no floats). Results are
        JSON-compatible values and are replayed from the journal on resume.
        """
        ...

    async def record(
        self,
        action_id: str,
        *,
        fingerprint: object,
        compute: Callable[[], Awaitable[JsonValue]],
    ) -> JsonValue:
        """Journal the JSON result of a non-deterministic computation, such as a model call.

        The first run awaits ``compute()`` and stores its result. A replay with the same
        ``action_id`` and an equal ``fingerprint`` returns the stored result without
        calling ``compute``. A different fingerprint, or an entry that is not this
        record, fails the execution as non-deterministic. An entry left ``IN_FLIGHT``
        with the same fingerprint is computed again. Validation recovery does not:
        it replays a stored result and fails closed, without calling ``compute``, when
        that result is missing. ``fingerprint`` must be canonicalizable (floats are
        allowed). No grant is needed: ``record`` reaches nothing outside the executor
        by itself.
        """
        ...

    def add_evidence(
        self,
        evidence_type: EvidenceType,
        *,
        content_reference: str | None = None,
        content_digest: str | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> UUID:
        """Record executor-produced evidence and return its id."""
        ...


class ValidationContext(Protocol):
    """Context handed to validators: read-only capabilities only."""

    @property
    def execution_id(self) -> UUID: ...

    @property
    def work_id(self) -> str: ...

    @property
    def work_version(self) -> str: ...

    @property
    def input_data(self) -> BaseModel: ...

    async def invoke(
        self, capability: str, *, action_id: str, params: Mapping[str, object]
    ) -> JsonValue:
        """Invoke a granted capability whose spec has ``side_effecting=False``."""
        ...


class Executor(Protocol[InputT_contra, OutputT_co]):
    """Implementation of a work definition.

    Executors must be deterministic given their input and the results returned by
    ``context.invoke``: on resume the executor runs again from the start and journaled
    actions are replayed. Route every non-deterministic operation (I/O, clock,
    randomness, LLM calls) through ``context.invoke``.

    Do not catch ``BaseException`` in executors: the SDK suspends executions waiting
    for approval with a ``BaseException`` subclass, as ``asyncio`` does for
    cancellation.
    """

    async def execute(self, input_data: InputT_contra, context: ExecutionContext) -> OutputT_co: ...


class Validator(Protocol):
    """Checks an output. Exceptions raised by a validator count as a failed check.

    Validators must be idempotent. ``validate`` may run again when ``resume``
    recovers an execution left in ``VALIDATING``, and ``context.invoke`` can call
    only read-only capabilities. A second run must not depend on the first having
    finished, and it must not cause an external side effect.
    """

    @property
    def validator_id(self) -> str: ...

    @property
    def required(self) -> bool: ...

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult: ...


class CapabilityProvider(Protocol):
    """The only path from executors to external systems through the SDK."""

    def capabilities(self) -> Iterable[CapabilitySpec]: ...

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        """Perform the capability.

        Forward ``call.operation_id`` to the external system as its idempotency key
        when supported. Raise ``UncertainOutcomeError`` when it cannot be determined
        whether the side effect happened.
        """
        ...


class PolicyResolver(Protocol):
    """Host-side source of policies. Returning ``None`` denies the execution."""

    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None: ...
