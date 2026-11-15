"""Child process that resumes one execution and waits inside the executor.

The concurrency test starts two of these. The work classes live here so both
processes load the same definition digest.
"""

import asyncio
import contextlib
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    ExecutionContext,
    ExecutionStatus,
    Executor,
    Policy,
    PostgresStorage,
    SqliteStorage,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.policy.registry import CapabilityRegistry
from id42_work.storage.protocols import Storage


class Halt(BaseException):
    """Stops the parent after the execution has been stored as ``RUNNING``."""


class NoteInput(BaseModel):
    title: str


class NoteOutput(BaseModel):
    title: str


def make_work() -> Work:
    return Work(
        id="hold_note",
        version="1.0.0",
        name="Hold a note",
        input_schema=NoteInput,
        output_schema=NoteOutput,
    )


def make_runner(
    storage: Storage, executor: Executor[NoteInput, NoteOutput]
) -> tuple[WorkRunner, ActorAuthority]:
    work = make_work()
    registry = WorkRegistry()
    registry.register(work, executor=executor)
    authority = ActorAuthority("claim-host")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {
                (work.id, work.version): Policy(
                    id="hold",
                    version="1.0.0",
                    autonomy=AutonomyLevel.AUTONOMOUS,
                    allowed_capabilities=frozenset(),
                )
            }
        ),
        capabilities=CapabilityRegistry(),
        actor_authority=authority,
        storage=storage,
    )
    return runner, authority


class _Stop:
    def __init__(self) -> None:
        self.execution_id: UUID | None = None

    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        del input_data
        self.execution_id = context.execution_id
        raise Halt


class BlockingExecutor:
    """Writes one log line, then waits until ``gate/release`` exists."""

    def __init__(self, gate: str, log_path: str) -> None:
        self._gate = Path(gate)
        self._log = Path(log_path)

    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        del context
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(f"enter {os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        (self._gate / "holding").write_text(str(os.getpid()), encoding="utf-8")
        while not (self._gate / "release").exists():
            await asyncio.sleep(0.02)
        return NoteOutput(title=input_data.title)


def open_storage(backend: str, target: str) -> SqliteStorage | PostgresStorage:
    if backend == "sqlite":
        return SqliteStorage(target)
    if backend == "postgres":
        return PostgresStorage(target)
    msg = f"unknown backend {backend!r}"
    raise RuntimeError(msg)


async def leave_running(storage: Storage) -> UUID:
    """Run until the executor stops the task. The claim is released on the way out."""
    stop = _Stop()
    runner, authority = make_runner(storage, stop)
    actor = authority.issue(actor_id="user:42", tenant_id="acme", ttl=timedelta(hours=1))
    work = make_work()
    with contextlib.suppress(Halt):
        await runner.run(
            work_id=work.id,
            version=work.version,
            input_data={"title": "held"},
            actor=actor,
        )
    if stop.execution_id is None:
        msg = "executor did not start"
        raise RuntimeError(msg)
    stored = await storage.executions.get(stop.execution_id)
    if stored is None or stored.status is not ExecutionStatus.RUNNING:
        msg = "execution was not left RUNNING"
        raise RuntimeError(msg)
    return stop.execution_id


def contend(  # noqa: PLR0917
    backend: str, target: str, execution_id: str, gate: str, log_path: str, result_path: str
) -> None:
    """Wait for ``gate/go``, resume, and write the resulting status."""
    gate_path = Path(gate)
    while not (gate_path / "go").exists():
        time.sleep(0.02)
    storage = open_storage(backend, target)
    try:
        runner, authority = make_runner(storage, BlockingExecutor(gate, log_path))
        actor = authority.issue(actor_id="user:42", tenant_id="acme", ttl=timedelta(hours=1))
        result = asyncio.run(runner.resume(UUID(execution_id), actor=actor))
    finally:
        storage.close()
    Path(result_path).write_text(result.status.value, encoding="utf-8")
