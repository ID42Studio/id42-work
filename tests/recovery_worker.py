"""Child process that dies inside a side-effecting capability call.

The recovery tests import this module, so the model classes stay importable after the
child exits. ``os._exit`` skips every handler, which is the crash resume must survive.
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, JsonValue

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    Policy,
    SqliteStorage,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)

READ = "notes.read"
PUBLISH = "notes.publish"


class NoteInput(BaseModel):
    title: str


class NoteOutput(BaseModel):
    title: str
    published: bool


class LogProvider:
    """Appends one line per call. Optionally exits the process inside publish."""

    def __init__(self, log_path: str, *, crash: bool) -> None:
        self._log_path = log_path
        self._crash = crash

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(name=READ, side_effecting=False),
            CapabilitySpec(name=PUBLISH, side_effecting=True),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        del params, call
        with open(self._log_path, "a", encoding="utf-8") as handle:
            handle.write(f"{capability}\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self._crash and capability == PUBLISH:
            os._exit(86)
        if capability == READ:
            return {"title": "note"}
        return {"published": True}


class NoteExecutor:
    def __init__(self, id_path: str) -> None:
        self._id_path = id_path

    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        Path(self._id_path).write_text(str(context.execution_id), encoding="utf-8")
        found = await context.invoke(
            READ, action_id="read-note", params={"title": input_data.title}
        )
        await context.invoke(PUBLISH, action_id="publish-note", params={"title": input_data.title})
        assert isinstance(found, dict)
        return NoteOutput(title=str(found["title"]), published=True)


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
    database: str, log_path: str, *, crash: bool, id_path: str
) -> tuple[WorkRunner, ActorAuthority, SqliteStorage]:
    """Wire a runner against ``database``. ``crash`` ends the process inside publish."""
    storage = SqliteStorage(database)
    work = make_work()
    registry = WorkRegistry()
    registry.register(work, executor=NoteExecutor(id_path))
    capabilities = CapabilityRegistry()
    capabilities.add(LogProvider(log_path, crash=crash))
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
    return runner, authority, storage


def crash_process(database: str, log_path: str, id_path: str) -> None:
    """Run until publish starts, then end the process."""
    runner, authority, _storage = make_runner(database, log_path, crash=True, id_path=id_path)
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
            input_data={"title": "hello"},
            actor=actor,
            idempotency_key="note-1",
        )
    )


if __name__ == "__main__":
    crash_process(sys.argv[1], sys.argv[2], sys.argv[3])
