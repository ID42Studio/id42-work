"""Child processes for crash recovery of an execution left in ``VALIDATING``.

The model classes live here so a restarted process loads the same definition
digest. ``os._exit`` skips every handler, which is the crash resume must survive.
"""

import asyncio
import contextlib
import os
import time
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, JsonValue

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    ExecutionStatus,
    Policy,
    PostgresStorage,
    Severity,
    SqliteStorage,
    StaticPolicyResolver,
    ValidationContext,
    ValidationResult,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.storage.protocols import Storage

READ = "notes.read"
PUBLISH = "notes.publish"


class Halt(BaseException):
    """Stops the parent after the execution has been stored as ``VALIDATING``."""


class NoteInput(BaseModel):
    title: str


class NoteOutput(BaseModel):
    title: str
    published: bool


class NoteExecutor:
    def __init__(self, id_path: str | None = None) -> None:
        self._id_path = id_path

    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        if self._id_path is not None:
            Path(self._id_path).write_text(str(context.execution_id), encoding="utf-8")
        await context.invoke(READ, action_id="read-note", params={"title": input_data.title})
        await context.invoke(PUBLISH, action_id="publish-note", params={"title": input_data.title})
        return NoteOutput(title=input_data.title, published=True)


class MemoryNotes:
    """Provider used by the parent. It does not touch the effect log."""

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(name=READ, side_effecting=False),
            CapabilitySpec(name=PUBLISH, side_effecting=True),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        del params, call
        if capability == READ:
            return {"title": "note"}
        return {"published": True}


class LogProvider:
    """Appends one line per call so a recovery run can show it did not repeat one."""

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(name=READ, side_effecting=False),
            CapabilitySpec(name=PUBLISH, side_effecting=True),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        del params, call
        _append(self._log_path, capability)
        if capability == READ:
            return {"title": "note"}
        return {"published": True}


class HaltValidator:
    validator_id = "halt_validation"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        del output, context
        raise Halt


class CrashValidator:
    """Exits the process on the first validation, and records the next one."""

    validator_id = "crash_validation"
    required = True

    def __init__(self, log_path: str, *, crash: bool) -> None:
        self._log_path = log_path
        self._crash = crash

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        del output, context
        if self._crash:
            os._exit(86)
        _append(self._log_path, "validated")
        return ValidationResult(
            validator_id=self.validator_id,
            passed=True,
            severity=Severity.INFO,
            message="ok",
        )


class BlockingValidator:
    """Writes one log line, then waits until ``gate/release`` exists."""

    validator_id = "block_validation"
    required = True

    def __init__(self, gate: str, log_path: str) -> None:
        self._gate = Path(gate)
        self._log_path = log_path

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        del output, context
        _append(self._log_path, "enter")
        (self._gate / "holding").write_text(str(os.getpid()), encoding="utf-8")
        while not (self._gate / "release").exists():
            await asyncio.sleep(0.02)
        return ValidationResult(
            validator_id=self.validator_id,
            passed=True,
            severity=Severity.INFO,
            message="ok",
        )


def make_work() -> Work:
    return Work(
        id="publish_note",
        version="1.0.0",
        name="Publish a note",
        input_schema=NoteInput,
        output_schema=NoteOutput,
        required_capabilities=frozenset({READ, PUBLISH}),
    )


def make_runner(
    storage: Storage,
    executor: NoteExecutor,
    validator: HaltValidator | CrashValidator | BlockingValidator,
    provider: MemoryNotes | LogProvider,
) -> tuple[WorkRunner, ActorAuthority]:
    work = make_work()
    registry = WorkRegistry()
    registry.register(work, executor=executor, validators=[validator])
    capabilities = CapabilityRegistry()
    capabilities.add(provider)
    authority = ActorAuthority("recovery-host")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {
                (work.id, work.version): Policy(
                    id="notes",
                    version="1.0.0",
                    autonomy=AutonomyLevel.AUTONOMOUS,
                    allowed_capabilities=frozenset({READ, PUBLISH}),
                )
            }
        ),
        capabilities=capabilities,
        actor_authority=authority,
        storage=storage,
    )
    return runner, authority


def open_storage(backend: str, target: str) -> SqliteStorage | PostgresStorage:
    if backend == "sqlite":
        return SqliteStorage(target)
    if backend == "postgres":
        return PostgresStorage(target)
    msg = f"unknown backend {backend!r}"
    raise RuntimeError(msg)


def _append(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
        handle.flush()
        os.fsync(handle.fileno())


class _Remember(NoteExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.execution_id: UUID | None = None

    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        self.execution_id = context.execution_id
        return await super().execute(input_data, context)


async def leave_validating(storage: Storage) -> UUID:
    """Run until the validator stops the task. The claim is released on the way out."""
    executor = _Remember()
    runner, authority = make_runner(storage, executor, HaltValidator(), MemoryNotes())
    actor = authority.issue(
        actor_id="user:42",
        tenant_id="acme",
        grants={READ, PUBLISH},
        ttl=timedelta(hours=1),
    )
    work = make_work()
    with contextlib.suppress(Halt):
        await runner.run(
            work_id=work.id,
            version=work.version,
            input_data={"title": "held"},
            actor=actor,
            idempotency_key="note-1",
        )
    if executor.execution_id is None:
        msg = "executor did not start"
        raise RuntimeError(msg)
    stored = await storage.executions.get(executor.execution_id)
    if stored is None or stored.status is not ExecutionStatus.VALIDATING:
        msg = "execution was not left VALIDATING"
        raise RuntimeError(msg)
    return executor.execution_id


def crash_process(backend: str, target: str, log_path: str, id_path: str) -> None:
    """Run until validation starts, then end the process."""
    storage = open_storage(backend, target)
    runner, authority = make_runner(
        storage,
        NoteExecutor(id_path),
        CrashValidator(log_path, crash=True),
        LogProvider(log_path),
    )
    actor = authority.issue(
        actor_id="user:42",
        tenant_id="acme",
        grants={READ, PUBLISH},
        ttl=timedelta(hours=1),
    )
    work = make_work()
    asyncio.run(
        runner.run(
            work_id=work.id,
            version=work.version,
            input_data={"title": "held"},
            actor=actor,
            idempotency_key="note-1",
        )
    )


def contend(  # noqa: PLR0917
    backend: str, target: str, execution_id: str, gate: str, log_path: str, result_path: str
) -> None:
    """Wait for ``gate/go``, resume, and write the resulting status."""
    gate_path = Path(gate)
    while not (gate_path / "go").exists():
        time.sleep(0.02)
    storage = open_storage(backend, target)
    try:
        runner, authority = make_runner(
            storage,
            NoteExecutor(),
            BlockingValidator(gate, log_path),
            LogProvider(log_path),
        )
        actor = authority.issue(
            actor_id="user:42",
            tenant_id="acme",
            grants={READ, PUBLISH},
            ttl=timedelta(hours=1),
        )
        result = asyncio.run(runner.resume(UUID(execution_id), actor=actor))
    finally:
        storage.close()
    Path(result_path).write_text(result.status.value, encoding="utf-8")
