"""The work runner (specification 5.5, 5.6, 8, 9, 10).

``WorkRunner.run`` drives one execution from creation to a terminal or waiting state:

1. verify the actor, resolve the binding, validate and fingerprint the input;
2. evaluate the policy and reserve the idempotency scope;
3. create the execution and move it through the state machine, persisting every
   transition with compare-and-set and emitting exactly one event per transition;
4. apply approval gates, run the executor through a restricted context whose
   capability calls are journaled, validate the output and record evidence.

Approval decisions, resumption and cancellation arrive in milestone M4. Until a
reviewer can decide, an execution that hits a gate stays waiting: gates are never
skipped.
"""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from id42_work._invoker import ActionInvoker, RunState, make_validation_invoke
from id42_work._recorder import ExecutionRecorder
from id42_work._time import Clock, utc_now
from id42_work.actors import ActorAuthority, TrustedActor
from id42_work.approvals.models import (
    OUTCOME_ACTION_ID,
    START_ACTION_ID,
    Approval,
)
from id42_work.canonical import digest
from id42_work.context import ExecutionSuspended, RunContext, ValidatorContext
from id42_work.contracts import PolicyResolver, Validator
from id42_work.events.dispatcher import EventDispatcher
from id42_work.events.models import EventType, WorkEvent
from id42_work.events.redaction import Redactor, default_redactor
from id42_work.evidence.models import Evidence, EvidenceType
from id42_work.exceptions import (
    IdempotencyConflictError,
    InputValidationError,
    StorageError,
)
from id42_work.execution import Execution
from id42_work.journal import JournalEntry
from id42_work.outcomes.models import Outcome, Severity, ValidationResult
from id42_work.outcomes.validators import validate_output_schema
from id42_work.policy.engine import PolicyEngine
from id42_work.policy.models import ApprovalGate, PolicyDecision
from id42_work.policy.registry import CapabilityRegistry
from id42_work.registry import WorkBinding, WorkRegistry
from id42_work.states import ExecutionStatus
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope, Storage

_logger = logging.getLogger("id42_work.runner")
_JSON = TypeAdapter(JsonValue)

S = ExecutionStatus


class WorkRunner:
    """Runs registered works under host-resolved policies."""

    def __init__(
        self,
        *,
        registry: WorkRegistry,
        policies: PolicyResolver,
        capabilities: CapabilityRegistry,
        actor_authority: ActorAuthority,
        storage: Storage,
        clock: Clock = utc_now,
        redactor: Redactor = default_redactor,
        dispatcher: EventDispatcher | None = None,
        idempotency_wait: timedelta = timedelta(seconds=2),
    ) -> None:
        self._registry = registry
        self._authority = actor_authority
        self._idempotency_wait = idempotency_wait
        self._rec = ExecutionRecorder(
            storage=storage,
            capabilities=capabilities,
            clock=clock,
            redactor=redactor,
            dispatcher=dispatcher or EventDispatcher(),
        )
        self._engine = PolicyEngine(
            resolver=policies, capabilities=capabilities, actor_authority=actor_authority
        )

    # ------------------------------------------------------------------ public API

    @property
    def dispatcher(self) -> EventDispatcher:
        """Subscribe here to receive events after they are persisted."""
        return self._rec.dispatcher

    async def run(
        self,
        *,
        work_id: str,
        version: str,
        input_data: BaseModel | Mapping[str, object],
        actor: object,
        idempotency_key: str | None = None,
        constraints: Iterable[str] | None = None,
    ) -> Execution:
        """Start (or, for a repeated idempotency key, return) an execution.

        Returns the execution in its resulting state for every business outcome.
        Raises ``UntrustedActorError``, ``WorkRegistrationError``,
        ``InputValidationError`` or ``IdempotencyConflictError`` before any execution
        is created; raises ``StorageError`` on storage failures.
        """
        trusted = self._authority.verify(actor, now=self._rec.now())
        binding = self._registry.get(work_id, version)
        work = binding.work
        validated = _validate_input(binding, input_data)
        fingerprint = _fingerprint(validated)
        scope = _scope(trusted, work_id, version, idempotency_key)

        decision = self._engine.evaluate(
            work, trusted, constraints=constraints, now=self._rec.now()
        )
        if decision.allowed and decision.idempotency_key_required and scope is None:
            msg = f"work {work.ref} has side-effecting capabilities: an idempotency_key is required"
            raise InputValidationError(msg)

        execution_id = uuid4()
        if scope is not None:
            existing = await self._reserve(scope, fingerprint, execution_id)
            if existing is not None:
                return existing

        input_reference = await self._rec.storage.inputs.put(execution_id, validated)
        now = self._rec.now()
        execution = Execution(
            execution_id=execution_id,
            work_id=work.id,
            work_version=work.version,
            definition_digest=binding.definition_digest,
            actor_id=trusted.actor_id,
            tenant_id=trusted.tenant_id,
            input_fingerprint=fingerprint,
            input_reference=input_reference,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            last_event_sequence=1,
        )
        await self._rec.storage.executions.create(execution)
        await self._rec.emit(
            execution,
            EventType.EXECUTION_CREATED,
            {
                "work_id": work.id,
                "work_version": work.version,
                "definition_digest": binding.definition_digest,
                "input_fingerprint": fingerprint,
                "idempotent": scope is not None,
            },
        )
        await self._rec.add_evidence(
            execution_id,
            EvidenceType.INPUT_REFERENCE,
            source="runner",
            content_reference=input_reference,
            content_digest=fingerprint,
        )
        execution = await self._rec.transition(
            execution,
            S.POLICY_CHECK,
            EventType.POLICY_EVALUATED,
            {
                "allowed": decision.allowed,
                "reasons": list(decision.reasons),
                "policy_id": decision.policy_id,
                "policy_version": decision.policy_version,
                "effective_autonomy": (
                    decision.effective_autonomy.value if decision.effective_autonomy else None
                ),
            },
        )
        if not decision.allowed:
            return await self._rec.transition(
                execution,
                S.DENIED,
                EventType.EXECUTION_DENIED,
                {"reasons": list(decision.reasons)},
                reason="policy_denied",
                policy_id=decision.policy_id,
                policy_version=decision.policy_version,
                policy_reasons=decision.reasons,
                error=decision.error,
            )
        execution = await self._rec.transition(
            execution,
            S.READY,
            EventType.EXECUTION_AUTHORIZED,
            {
                "granted_capabilities": _json_list(sorted(decision.granted_capabilities)),
                "gates": [
                    {"gate": g.gate.value, "capability": g.capability}
                    for g in decision.required_gates
                ],
            },
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            effective_autonomy=decision.effective_autonomy,
            granted_capabilities=decision.granted_capabilities,
        )
        return await self._drive(execution, binding, validated, decision, trusted)

    def run_sync(
        self,
        *,
        work_id: str,
        version: str,
        input_data: BaseModel | Mapping[str, object],
        actor: object,
        idempotency_key: str | None = None,
        constraints: Iterable[str] | None = None,
    ) -> Execution:
        """Synchronous ``run``. Refuses to run inside an active event loop."""
        _ensure_no_running_loop("run_sync")
        return asyncio.run(
            self.run(
                work_id=work_id,
                version=version,
                input_data=input_data,
                actor=actor,
                idempotency_key=idempotency_key,
                constraints=constraints,
            )
        )

    async def get(self, execution_id: UUID) -> Execution | None:
        """The current record of an execution."""
        return await self._rec.storage.executions.get(execution_id)

    async def events(self, execution_id: UUID) -> tuple[WorkEvent, ...]:
        """Events of an execution, in sequence order."""
        return await self._rec.storage.events.list(execution_id)

    async def evidence(self, execution_id: UUID) -> tuple[Evidence, ...]:
        """Evidence recorded for an execution."""
        return await self._rec.storage.evidence.list(execution_id)

    async def journal(self, execution_id: UUID) -> tuple[JournalEntry, ...]:
        """Action journal of an execution."""
        return await self._rec.storage.executions.list_journal(execution_id)

    async def approvals_for(self, execution_id: UUID) -> tuple[Approval, ...]:
        """Approvals requested for an execution."""
        return await self._rec.storage.approvals.list_for_execution(execution_id)

    # ------------------------------------------------------------------ driving

    async def _drive(
        self,
        execution: Execution,
        binding: WorkBinding,
        validated: BaseModel,
        decision: PolicyDecision,
        actor: TrustedActor,
    ) -> Execution:
        """Run from ``READY`` to a terminal or waiting state."""
        start_gate = decision.gate_for(ApprovalGate.START)
        if start_gate is not None:
            start_digest = self._rec.action_digest(
                execution, ApprovalGate.START, START_ACTION_ID, None, validated
            )
            if not await self._rec.check_gate(
                execution, start_gate, START_ACTION_ID, start_digest, validated
            ):
                return await self._rec.transition(
                    execution,
                    S.WAITING_APPROVAL,
                    EventType.EXECUTION_SUSPENDED,
                    {"gate": ApprovalGate.START.value},
                    reason="awaiting_start_approval",
                )

        execution = await self._rec.transition(
            execution, S.RUNNING, EventType.EXECUTION_STARTED, {}
        )
        state = RunState()
        context = RunContext(
            execution_id=execution.execution_id,
            work_id=execution.work_id,
            work_version=execution.work_version,
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            granted_capabilities=decision.granted_capabilities,
            invoke=ActionInvoker(self._rec, execution, decision, actor, state),
            new_evidence_id=uuid4,
        )
        output: object = None
        failure: BaseException | None = None
        failure_reason = "executor_error"
        limit = decision.execution_limits.timeout
        timeout_cm = asyncio.timeout(limit.total_seconds() if limit else None)
        try:
            async with timeout_cm:
                output = await binding.executor.execute(validated, context)
        except ExecutionSuspended:
            state.suspended = True
        except TimeoutError as error:
            failure = error
            failure_reason = "timeout" if timeout_cm.expired() else "executor_error"
        except asyncio.CancelledError as error:
            await self._rec.persist_executor_evidence(execution, context)
            await self._rec.fail(
                execution,
                error,
                "cancelled_in_flight",
                reconcile=state.reconcile or state.side_effect_in_flight,
            )
            raise
        except Exception as error:  # noqa: BLE001 - every executor error fails the execution
            failure = error
        await self._rec.persist_executor_evidence(execution, context)

        if state.violation is not None:
            return await self._rec.fail(
                execution, state.violation, state.violation_reason, reconcile=state.reconcile
            )
        if state.suspended:
            return await self._rec.transition(
                execution,
                S.WAITING_APPROVAL,
                EventType.EXECUTION_SUSPENDED,
                {"gate": ApprovalGate.ACTION.value},
                reason="awaiting_action_approval",
            )
        if failure is not None:
            if timeout_cm.expired() and failure_reason == "executor_error":
                failure_reason = "timeout"
            return await self._rec.fail(
                execution,
                failure,
                failure_reason,
                reconcile=state.reconcile or state.side_effect_in_flight,
            )
        return await self._validate(execution, binding, validated, decision, output)

    async def _validate(
        self,
        execution: Execution,
        binding: WorkBinding,
        validated: BaseModel,
        decision: PolicyDecision,
        output: object,
    ) -> Execution:
        execution = await self._rec.transition(
            execution, S.VALIDATING, EventType.VALIDATION_STARTED, {}
        )
        produced_at = self._rec.now()
        output_model, schema_result = validate_output_schema(output, binding.work.output_schema)
        results = [schema_result]
        if output_model is not None:
            context = ValidatorContext(
                execution_id=execution.execution_id,
                work_id=execution.work_id,
                work_version=execution.work_version,
                input_data=validated,
                invoke=make_validation_invoke(self._rec, execution, decision),
            )
            for validator in binding.validators:
                results.append(await _run_validator(validator, output_model, context))

        report_id = await self._rec.add_evidence(
            execution.execution_id,
            EvidenceType.VALIDATION_REPORT,
            source="runner",
            content_digest=digest([r.model_dump(mode="json") for r in results]),
            metadata={
                "validators": [r.validator_id for r in results],
                "failed": [r.validator_id for r in results if not r.passed],
            },
        )
        results = [
            r.model_copy(update={"evidence_ids": (*r.evidence_ids, report_id)}) for r in results
        ]
        failed_ids: list[JsonValue] = [r.validator_id for r in results if not r.passed]

        if output_model is None:
            return await self._rec.transition(
                execution,
                S.VALIDATION_FAILED,
                EventType.VALIDATION_FAILED,
                {"verified": False, "failed_validators": failed_ids},
                reason="output_schema_invalid",
            )

        verified = all(r.passed for r in results if r.required)
        outcome = Outcome(
            execution_id=execution.execution_id,
            output=output_model,
            verified=verified,
            validation_results=tuple(results),
            evidence_ids=(report_id,),
            produced_at=produced_at,
        )
        if not verified:
            return await self._rec.transition(
                execution,
                S.VALIDATION_FAILED,
                EventType.VALIDATION_FAILED,
                {"verified": False, "failed_validators": failed_ids},
                reason="validation_failed",
                outcome=outcome,
            )

        outcome_gate = decision.gate_for(ApprovalGate.OUTCOME)
        present = {
            e.evidence_type for e in await self._rec.storage.evidence.list(execution.execution_id)
        }
        missing = set(decision.evidence_requirements) - present
        if outcome_gate is not None:
            missing.discard(EvidenceType.APPROVAL_DECISION)
        if missing:
            names = ",".join(sorted(m.value for m in missing))
            return await self._rec.transition(
                execution,
                S.VALIDATION_FAILED,
                EventType.VALIDATION_FAILED,
                {
                    "verified": True,
                    "missing_evidence": _json_list(sorted(m.value for m in missing)),
                },
                reason=f"evidence_missing:{names}",
                outcome=outcome,
            )

        if outcome_gate is not None:
            try:
                outcome_digest = self._rec.action_digest(
                    execution, ApprovalGate.OUTCOME, OUTCOME_ACTION_ID, None, output_model
                )
            except TypeError as error:
                _logger.warning("output cannot be digested for review", exc_info=error)
                return await self._rec.transition(
                    execution,
                    S.VALIDATION_FAILED,
                    EventType.VALIDATION_FAILED,
                    {"verified": True, "failed_validators": []},
                    reason="output_not_canonical",
                    outcome=outcome,
                )
            await self._rec.check_gate(
                execution, outcome_gate, OUTCOME_ACTION_ID, outcome_digest, output_model
            )
            return await self._rec.transition(
                execution,
                S.WAITING_REVIEW,
                EventType.REVIEW_REQUESTED,
                {"verified": True},
                reason="awaiting_outcome_review",
                outcome=outcome,
            )

        await self._rec.emit(execution, EventType.VALIDATION_PASSED, {"verified": True})
        accepted = _accept(outcome, self._rec.now())
        return await self._rec.transition(
            execution, S.COMPLETED, EventType.EXECUTION_COMPLETED, {}, outcome=accepted
        )

    async def _reserve(
        self, scope: IdempotencyScope, fingerprint: str, execution_id: UUID
    ) -> Execution | None:
        """Reserve ``scope`` for ``execution_id``; return the owner's execution otherwise."""
        record = await self._rec.storage.idempotency.reserve(
            IdempotencyRecord(scope=scope, input_fingerprint=fingerprint, execution_id=execution_id)
        )
        if record.execution_id == execution_id:
            return None
        if record.input_fingerprint != fingerprint:
            msg = (
                f"idempotency key {scope.idempotency_key!r} was already used "
                f"with a different input for {scope.work_id}@{scope.work_version}"
            )
            raise IdempotencyConflictError(msg, execution_id=record.execution_id)
        deadline = asyncio.get_running_loop().time() + self._idempotency_wait.total_seconds()
        while True:
            existing = await self._rec.storage.executions.get(record.execution_id)
            if existing is not None:
                return existing
            if asyncio.get_running_loop().time() >= deadline:
                msg = "the execution owning this idempotency key is still being created"
                raise StorageError(msg, execution_id=record.execution_id)
            await asyncio.sleep(0.005)


# ---------------------------------------------------------------------- helpers


def _ensure_no_running_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    msg = f"{name}() cannot be called from a running event loop; await the async method instead"
    raise RuntimeError(msg)


def _validate_input(
    binding: WorkBinding, input_data: BaseModel | Mapping[str, object]
) -> BaseModel:
    schema = binding.work.input_schema
    data: object = (
        input_data.model_dump(mode="python", warnings=False)
        if isinstance(input_data, BaseModel)
        else input_data
    )
    try:
        return schema.model_validate(data)
    except ValidationError as error:
        msg = f"input does not match {schema.__name__}: {error}"
        raise InputValidationError(msg) from error


def _fingerprint(validated: BaseModel) -> str:
    try:
        return digest(validated)
    except TypeError as error:
        msg = f"input cannot be fingerprinted ({error}); use Decimal or int instead of float"
        raise InputValidationError(msg) from error


def _scope(
    actor: TrustedActor, work_id: str, version: str, idempotency_key: str | None
) -> IdempotencyScope | None:
    if idempotency_key is None:
        return None
    try:
        return IdempotencyScope(
            tenant_id=actor.tenant_id,
            work_id=work_id,
            work_version=version,
            idempotency_key=idempotency_key,
        )
    except ValidationError as error:
        msg = "idempotency_key must be a non-empty string of at most 256 characters"
        raise InputValidationError(msg) from error


async def _run_validator(
    validator: Validator, output: BaseModel, context: ValidatorContext
) -> ValidationResult:
    try:
        result = await validator.validate(output, context)
    except Exception as error:  # noqa: BLE001 - a raising validator is a failed check
        return ValidationResult(
            validator_id=validator.validator_id,
            passed=False,
            severity=Severity.ERROR,
            required=validator.required,
            message=f"validator raised {type(error).__name__}: {error}",
        )
    if not isinstance(result, ValidationResult):  # pyright: ignore[reportUnnecessaryIsInstance]
        return ValidationResult(
            validator_id=validator.validator_id,
            passed=False,
            severity=Severity.ERROR,
            required=validator.required,
            message=f"validator returned {type(result).__name__}, expected ValidationResult",
        )
    return result.model_copy(
        update={"validator_id": validator.validator_id, "required": validator.required}
    )


def _accept(outcome: Outcome, at: datetime) -> Outcome:
    fields = {name: getattr(outcome, name) for name in Outcome.model_fields}
    fields["accepted_at"] = max(at, outcome.produced_at)
    return Outcome.model_validate(fields)


def _json_list(values: Iterable[str]) -> list[JsonValue]:
    return list(values)
