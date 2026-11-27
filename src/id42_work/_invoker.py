"""Capability calls made by executors and validators (specification 5.6, 8, 9)."""

import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import cast
from uuid import UUID

from pydantic import JsonValue, TypeAdapter, ValidationError

from id42_work._recorder import ExecutionRecorder
from id42_work.canonical import digest
from id42_work.context import ComputeFn, ExecutionSuspended, InvokeFn
from id42_work.events.models import EventType
from id42_work.evidence.models import EvidenceType
from id42_work.exceptions import (
    CapabilityDeniedError,
    ExecutionError,
    ExecutionFailedError,
    NonDeterministicExecutionError,
    UncertainOutcomeError,
)
from id42_work.execution import Execution, ExecutionErrorInfo
from id42_work.identifiers import ACTION_ID_PATTERN, RECORD_CAPABILITY
from id42_work.journal import JournalEntry, JournalStatus, operation_id
from id42_work.policy.capabilities import CapabilityCall
from id42_work.policy.models import ApprovalGate, PolicyDecision

_JSON = TypeAdapter(JsonValue)
_NOT_REPLAYED = object()
_ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)


@dataclass(slots=True)
class RunState:
    """Bookkeeping for one executor run."""

    used_action_ids: set[str] = field(default_factory=set[str])
    actions: int = 0
    suspended: bool = False
    violation: BaseException | None = None
    violation_reason: str = ""
    reconcile: bool = False
    side_effect_in_flight: bool = False

    def fail(self, error: BaseException, reason: str) -> BaseException:
        if self.violation is None:
            self.violation = error
            self.violation_reason = reason
        return error


class ActionInvoker:
    """Executes ``context.invoke`` calls for one executor run (specification 5.6, 8).

    Order of checks: suspension, action id, grant, uniqueness, limits, params
    canonicalization, journal replay, ACTION gate, provider call. Security violations
    are recorded in the run state, so an executor that catches the exception still
    ends ``FAILED``.

    ``replay_only`` is validation recovery. The side effect already finished, so a
    call that is not a completed or failed journal entry is refused and the provider
    is not called. A ``context.record`` is returned from its stored result. A missing
    record, a different fingerprint, or a record with no stored result is refused
    and ``compute`` is not called.
    """

    def __init__(
        self,
        recorder: ExecutionRecorder,
        execution: Execution,
        decision: PolicyDecision,
        state: RunState,
        *,
        replay_only: bool = False,
    ) -> None:
        self._rec = recorder
        self._execution = execution
        self._eid = execution.execution_id
        self._decision = decision
        self._state = state
        self._replay_only = replay_only

    async def __call__(
        self, capability: str, action_id: str, params: Mapping[str, object]
    ) -> JsonValue:
        params_digest = self._precheck(capability, action_id, params)
        journal = self._rec.storage.executions
        entries = {e.action_id: e for e in await journal.list_journal(self._eid)}
        entry = entries.get(action_id)
        payload: dict[str, JsonValue] = {
            "action_id": action_id,
            "capability": capability,
            "operation_id": str(operation_id(self._eid, action_id)),
            "params_digest": params_digest,
        }
        if entry is not None:
            replayed = await self._replay(entry, capability, params_digest, payload)
            if replayed is not _NOT_REPLAYED:
                return cast("JsonValue", replayed)
        if self._replay_only:
            msg = (
                f"action {action_id!r} is not a completed journal entry; "
                "validation recovery will not perform it"
            )
            raise self._state.fail(
                NonDeterministicExecutionError(msg, execution_id=self._eid),
                "non_deterministic",
            )
        await self._apply_gate(capability, action_id, params, params_digest, entry)
        return await self._perform(
            capability, action_id, params, params_digest=params_digest, entry=entry, payload=payload
        )

    async def record(self, action_id: str, fingerprint: object, compute: ComputeFn) -> JsonValue:
        """Journal ``compute()``'s JSON result, or replay it (``context.record``).

        Counts toward ``max_actions``. No grant or gate applies: a recorded computation
        reaches nothing outside the executor through the SDK. A ``SUCCEEDED`` entry with
        the same fingerprint is returned. A ``FAILED`` entry raises again. An entry left
        ``IN_FLIGHT`` with the same fingerprint is computed again, because its result was
        never returned. Any other status, or a different fingerprint or capability, fails
        closed. A computation that suspends for approval leaves the entry ``IN_FLIGHT``.

        Validation recovery (``replay_only``) never calls ``compute``. A stored result is
        replayed. A missing record, a different fingerprint, or a record that has no
        stored result fails closed.
        """
        state, eid = self._state, self._eid
        fingerprint_digest = self._count(action_id, fingerprint)
        journal = self._rec.storage.executions
        entries = {e.action_id: e for e in await journal.list_journal(eid)}
        entry = entries.get(action_id)
        payload: dict[str, JsonValue] = {
            "action_id": action_id,
            "capability": RECORD_CAPABILITY,
            "operation_id": str(operation_id(eid, action_id)),
            "params_digest": fingerprint_digest,
        }
        stored = await self._stored_record(entry, action_id, fingerprint_digest, payload)
        if stored is not _NOT_REPLAYED:
            return cast("JsonValue", stored)
        if self._replay_only:
            msg = (
                f"record {action_id!r} has no stored result; "
                "validation recovery will not compute it"
            )
            raise state.fail(
                NonDeterministicExecutionError(msg, execution_id=eid), "non_deterministic"
            )
        started = self._rec.now()
        in_flight = journal_entry(
            eid,
            action_id,
            RECORD_CAPABILITY,
            fingerprint_digest,
            JournalStatus.IN_FLIGHT,
            started_at=started,
        )
        if entry is None:
            await journal.append_journal(in_flight)
        else:
            await journal.update_journal(in_flight, expected_status=entry.status)
        await self._rec.emit(self._execution, EventType.ACTION_INVOKED, payload)
        try:
            raw = await compute()
        except Exception as error:
            await self._rec.settle(in_flight, JournalStatus.FAILED, error)
            await self._rec.emit(self._execution, EventType.ACTION_FAILED, payload)
            raise
        except BaseException as error:
            # An approval gate leaves the record in flight so resume can recompute it
            # and consume the grant. Cancellation still settles the record as failed:
            # the result was never returned, and resume of a cancelled execution does
            # not retry it.
            if not _suspends(error):
                await self._rec.settle(in_flight, JournalStatus.FAILED, error)
            raise
        try:
            result = _JSON.validate_python(raw)
            result_digest = digest(result, floats="repr")
        except (ValidationError, TypeError) as error:
            await self._rec.settle(in_flight, JournalStatus.FAILED, error)
            msg = f"record {action_id!r} produced a non-JSON result"
            raise state.fail(
                ExecutionFailedError(msg, execution_id=eid), "invalid_record_result"
            ) from error
        done = JournalEntry.model_validate(
            {
                **in_flight.model_dump(),
                "status": JournalStatus.SUCCEEDED,
                "result": result,
                "result_digest": result_digest,
                "finished_at": max(self._rec.now(), started),
            }
        )
        await journal.update_journal(done, expected_status=JournalStatus.IN_FLIGHT)
        await self._rec.emit(
            self._execution, EventType.ACTION_COMPLETED, {**payload, "result_digest": result_digest}
        )
        return result

    async def _stored_record(
        self,
        entry: JournalEntry | None,
        action_id: str,
        fingerprint_digest: str,
        payload: dict[str, JsonValue],
    ) -> object:
        """Replay a finished record, or ``_NOT_REPLAYED`` when it still has to run.

        A different fingerprint fails closed. A previous failure is raised again. An
        uncertain record fails closed. An ``IN_FLIGHT`` record, or no entry, is left
        for the caller: a live run computes it, and validation recovery refuses it.
        """
        if entry is None:
            return _NOT_REPLAYED
        state, eid = self._state, self._eid
        same = entry.capability == RECORD_CAPABILITY and entry.params_digest == fingerprint_digest
        if not same:
            msg = f"replayed record {action_id!r} differs from the journal"
            raise state.fail(
                NonDeterministicExecutionError(msg, execution_id=eid), "non_deterministic"
            )
        if entry.status is JournalStatus.SUCCEEDED:
            await self._rec.emit(self._execution, EventType.ACTION_REPLAYED, payload)
            return entry.result
        if entry.status is JournalStatus.FAILED:
            msg = f"replayed record {action_id!r} failed previously"
            raise ExecutionFailedError(msg, execution_id=eid)
        if entry.status is not JournalStatus.IN_FLIGHT:
            state.reconcile = True
            msg = f"record {action_id!r} has an uncertain outcome; reconciliation required"
            raise state.fail(UncertainOutcomeError(msg, execution_id=eid), "uncertain_outcome")
        return _NOT_REPLAYED

    def _count(self, action_id: str, fingerprint: object) -> str:
        """Checks shared by ``record``: suspension, id, uniqueness, limit, fingerprint."""
        state, eid = self._state, self._eid
        if state.suspended:
            raise ExecutionSuspended
        if not valid_action_id(action_id):
            msg = f"invalid action_id {action_id!r}"
            raise state.fail(ExecutionError(msg, execution_id=eid), "invalid_action_id")
        if action_id in state.used_action_ids:
            msg = f"action_id {action_id!r} was already used in this run"
            raise state.fail(ExecutionError(msg, execution_id=eid), "duplicate_action_id")
        state.used_action_ids.add(action_id)
        state.actions += 1
        max_actions = self._decision.execution_limits.max_actions
        if max_actions is not None and state.actions > max_actions:
            msg = f"execution exceeded max_actions={max_actions}"
            raise state.fail(ExecutionFailedError(msg, execution_id=eid), "max_actions_exceeded")
        try:
            return digest(fingerprint, floats="repr")
        except TypeError as error:
            msg = f"fingerprint of record {action_id!r} cannot be canonicalized: {error}"
            raise state.fail(
                ExecutionError(msg, execution_id=eid), "invalid_record_fingerprint"
            ) from error

    def _precheck(self, capability: str, action_id: str, params: Mapping[str, object]) -> str:
        state, eid = self._state, self._eid
        if state.suspended:
            raise ExecutionSuspended
        if not valid_action_id(action_id):
            msg = f"invalid action_id {action_id!r}"
            raise state.fail(ExecutionError(msg, execution_id=eid), "invalid_action_id")
        if capability not in self._decision.granted_capabilities:
            msg = f"capability {capability!r} is not granted to this execution"
            raise state.fail(CapabilityDeniedError(msg, execution_id=eid), "capability_denied")
        if action_id in state.used_action_ids:
            msg = f"action_id {action_id!r} was already used in this run"
            raise state.fail(ExecutionError(msg, execution_id=eid), "duplicate_action_id")
        state.used_action_ids.add(action_id)
        state.actions += 1
        max_actions = self._decision.execution_limits.max_actions
        if max_actions is not None and state.actions > max_actions:
            msg = f"execution exceeded max_actions={max_actions}"
            raise state.fail(ExecutionFailedError(msg, execution_id=eid), "max_actions_exceeded")
        try:
            return digest(params)
        except TypeError as error:
            msg = f"params of action {action_id!r} cannot be canonicalized: {error}"
            raise state.fail(
                ExecutionError(msg, execution_id=eid), "invalid_action_params"
            ) from error

    async def _replay(
        self,
        entry: JournalEntry,
        capability: str,
        params_digest: str,
        payload: dict[str, JsonValue],
    ) -> object:
        state, eid = self._state, self._eid
        same = entry.params_digest == params_digest and entry.capability == capability
        if entry.status is JournalStatus.SUCCEEDED:
            if not same:
                msg = f"replayed action {entry.action_id!r} differs from the journal"
                raise state.fail(
                    NonDeterministicExecutionError(msg, execution_id=eid), "non_deterministic"
                )
            await self._rec.emit(self._execution, EventType.ACTION_REPLAYED, payload)
            return entry.result
        if entry.status is JournalStatus.FAILED:
            msg = f"replayed action {entry.action_id!r} failed previously"
            if not same:
                raise state.fail(
                    NonDeterministicExecutionError(msg, execution_id=eid), "non_deterministic"
                )
            if _retry_cancelled_read(entry, self._rec.capabilities.is_side_effecting(capability)):
                return _NOT_REPLAYED
            raise ExecutionFailedError(msg, execution_id=eid)
        if entry.status is JournalStatus.NOT_APPLIED:
            if not same:
                msg = f"replayed action {entry.action_id!r} differs from the journal"
                raise state.fail(
                    NonDeterministicExecutionError(msg, execution_id=eid), "non_deterministic"
                )
            return _NOT_REPLAYED
        if entry.status is JournalStatus.ABANDONED:
            msg = f"action {entry.action_id!r} was abandoned during reconciliation"
            raise state.fail(
                ExecutionFailedError(msg, execution_id=eid), "reconciliation_abandoned"
            )
        if entry.status is JournalStatus.IN_FLIGHT:
            await self._rec.mark_action_uncertain(self._execution, entry)
        if entry.status in {JournalStatus.IN_FLIGHT, JournalStatus.UNCERTAIN}:
            state.reconcile = True
            msg = f"action {entry.action_id!r} has an uncertain outcome; reconciliation required"
            raise state.fail(UncertainOutcomeError(msg, execution_id=eid), "uncertain_outcome")
        return _NOT_REPLAYED

    async def _apply_gate(
        self,
        capability: str,
        action_id: str,
        params: Mapping[str, object],
        params_digest: str,
        entry: JournalEntry | None,
    ) -> None:
        gate = self._decision.gate_for(ApprovalGate.ACTION, capability)
        if gate is None:
            return
        rec = self._rec
        action_digest = rec.action_digest(
            self._execution, ApprovalGate.ACTION, action_id, capability, params
        )
        if await rec.check_gate(self._execution, gate, action_id, action_digest, params):
            return
        pending = journal_entry(
            self._eid, action_id, capability, params_digest, JournalStatus.PENDING_APPROVAL
        )
        journal = rec.storage.executions
        if entry is None:
            await journal.append_journal(pending)
        else:
            await journal.update_journal(pending, expected_status=entry.status)
        self._state.suspended = True
        raise ExecutionSuspended

    async def _perform(
        self,
        capability: str,
        action_id: str,
        params: Mapping[str, object],
        *,
        params_digest: str,
        entry: JournalEntry | None,
        payload: dict[str, JsonValue],
    ) -> JsonValue:
        rec, state, eid = self._rec, self._state, self._eid
        registry = rec.capabilities
        journal = rec.storage.executions
        provider = registry.provider(capability)
        if provider is None:  # pragma: no cover - guaranteed by the policy check
            msg = f"capability {capability!r} has no provider"
            raise state.fail(CapabilityDeniedError(msg, execution_id=eid), "capability_unavailable")
        side_effecting = registry.is_side_effecting(capability)
        started = rec.now()
        in_flight = journal_entry(
            eid, action_id, capability, params_digest, JournalStatus.IN_FLIGHT, started_at=started
        )
        if entry is None:
            await journal.append_journal(in_flight)
        else:
            await journal.update_journal(in_flight, expected_status=entry.status)
        await rec.emit(self._execution, EventType.ACTION_INVOKED, payload)
        call = CapabilityCall(
            execution_id=eid,
            action_id=action_id,
            operation_id=operation_id(eid, action_id),
            actor_id=self._execution.actor_id,
            tenant_id=self._execution.tenant_id,
        )
        raw = await self._call_provider(
            provider.invoke(capability, MappingProxyType(dict(params)), call),
            in_flight,
            payload,
            side_effecting=side_effecting,
        )
        try:
            result = _JSON.validate_python(raw)
            result_digest = digest(result, floats="repr")
        except (ValidationError, TypeError) as error:
            await rec.settle(in_flight, JournalStatus.FAILED, error)
            state.reconcile = state.reconcile or side_effecting
            msg = f"capability {capability!r} returned a non-JSON result"
            raise state.fail(
                ExecutionFailedError(msg, execution_id=eid), "invalid_capability_result"
            ) from error
        done = JournalEntry.model_validate(
            {
                **in_flight.model_dump(),
                "status": JournalStatus.SUCCEEDED,
                "result": result,
                "result_digest": result_digest,
                "finished_at": max(rec.now(), started),
            }
        )
        await journal.update_journal(done, expected_status=JournalStatus.IN_FLIGHT)
        await rec.emit(
            self._execution, EventType.ACTION_COMPLETED, {**payload, "result_digest": result_digest}
        )
        await rec.add_evidence(
            eid,
            EvidenceType.TOOL_RESULT,
            source=f"capability:{capability}",
            content_digest=result_digest,
            metadata={"action_id": action_id, "operation_id": str(call.operation_id)},
        )
        return result

    async def _call_provider(
        self,
        call: Awaitable[JsonValue],
        in_flight: JournalEntry,
        payload: dict[str, JsonValue],
        *,
        side_effecting: bool,
    ) -> object:
        rec, state = self._rec, self._state
        state.side_effect_in_flight = side_effecting
        try:
            return await call
        except UncertainOutcomeError as error:
            await rec.settle(in_flight, JournalStatus.UNCERTAIN, error)
            await rec.emit(self._execution, EventType.ACTION_UNCERTAIN, payload)
            state.reconcile = True
            state.fail(error, "uncertain_outcome")
            raise
        except Exception as error:
            await rec.settle(in_flight, JournalStatus.FAILED, error)
            await rec.emit(self._execution, EventType.ACTION_FAILED, payload)
            raise
        except BaseException as error:  # cancellation or timeout while in flight
            status = JournalStatus.UNCERTAIN if side_effecting else JournalStatus.FAILED
            await rec.settle(in_flight, status, error)
            if side_effecting:
                state.reconcile = True
                await rec.emit(self._execution, EventType.ACTION_UNCERTAIN, payload)
            raise
        finally:
            state.side_effect_in_flight = False


def _suspends(error: BaseException) -> bool:
    """Whether ``error`` is an approval suspension, including one inside a task group."""
    if isinstance(error, ExecutionSuspended):
        return True
    if not isinstance(error, BaseExceptionGroup):
        return False
    grouped = cast("BaseExceptionGroup[BaseException]", error)
    return grouped.subgroup(ExecutionSuspended) is not None


def _retry_cancelled_read(entry: JournalEntry, side_effecting: bool) -> bool:
    """Whether a failed entry is a read cancelled by a sibling approval gate.

    A side-effecting call is not retried: the external system may already have
    accepted it. A ``FAILED`` entry always carries an error.
    """
    error = cast(ExecutionErrorInfo, entry.error)
    return error.type == "CancelledError" and not side_effecting


def valid_action_id(action_id: object) -> bool:
    """Whether ``action_id`` is a well-formed executor action id."""
    return isinstance(action_id, str) and _ACTION_ID_RE.fullmatch(action_id) is not None


def journal_entry(
    execution_id: UUID,
    action_id: str,
    capability: str,
    params_digest: str,
    status: JournalStatus,
    *,
    started_at: datetime | None = None,
) -> JournalEntry:
    return JournalEntry(
        execution_id=execution_id,
        action_id=action_id,
        capability=capability,
        params_digest=params_digest,
        operation_id=operation_id(execution_id, action_id),
        status=status,
        started_at=started_at,
    )


def make_validation_invoke(
    recorder: ExecutionRecorder, execution: Execution, decision: PolicyDecision
) -> InvokeFn:
    eid = execution.execution_id

    async def invoke(capability: str, action_id: str, params: Mapping[str, object]) -> JsonValue:
        if capability not in decision.granted_capabilities:
            msg = f"capability {capability!r} is not granted to this execution"
            raise CapabilityDeniedError(msg, execution_id=eid)
        if recorder.capabilities.is_side_effecting(capability):
            msg = f"validators may only use read-only capabilities, not {capability!r}"
            raise CapabilityDeniedError(msg, execution_id=eid)
        provider = recorder.capabilities.provider(capability)
        if provider is None or not valid_action_id(action_id):
            msg = f"capability {capability!r} or action_id {action_id!r} is invalid"
            raise CapabilityDeniedError(msg, execution_id=eid)
        validation_action = f"validation:{action_id}"
        call = CapabilityCall(
            execution_id=eid,
            action_id=validation_action,
            operation_id=operation_id(eid, validation_action),
            actor_id=execution.actor_id,
            tenant_id=execution.tenant_id,
        )
        return _JSON.validate_python(
            await provider.invoke(capability, MappingProxyType(dict(params)), call)
        )

    return invoke
