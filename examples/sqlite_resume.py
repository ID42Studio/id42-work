"""An approval survives closing the database, and resume does not repeat the read.

Run: python examples/sqlite_resume.py
"""

import asyncio
import tempfile
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
    ExecutionStatus,
    Policy,
    SqliteStorage,
    StaticPolicyResolver,
    TrustedActor,
    Work,
    WorkRegistry,
    WorkRunner,
)

WORK_ID = "publish_note"
READ = "notes.read"
PUBLISH = "notes.publish"


class NoteInput(BaseModel):
    text: str


class NoteOutput(BaseModel):
    published: bool


class Notes:
    """Counts each capability call. A new process starts the counters at zero."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(name=READ, side_effecting=False),
            CapabilitySpec(name=PUBLISH, side_effecting=True),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        self.calls[capability] = self.calls.get(capability, 0) + 1
        if capability == READ:
            return {"length": len(str(params["text"]))}
        return {"published": True}


class PublishExecutor:
    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        await context.invoke(READ, action_id="read-note", params={"text": input_data.text})
        await context.invoke(PUBLISH, action_id="publish-note", params={"text": input_data.text})
        return NoteOutput(published=True)


def open_runner(storage: SqliteStorage, notes: Notes) -> tuple[WorkRunner, ActorAuthority]:
    """A fresh process: new runner and authority, same database and work contract."""
    work = Work(
        id=WORK_ID,
        version="1.0.0",
        name="Publish a note",
        input_schema=NoteInput,
        output_schema=NoteOutput,
        required_capabilities=frozenset({READ, PUBLISH}),
    )
    registry = WorkRegistry()
    registry.register(work, executor=PublishExecutor())
    policy = Policy(
        id="notes_policy",
        version="1.0.0",
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_capabilities=frozenset({READ, PUBLISH}),
    )
    capabilities = CapabilityRegistry()
    capabilities.add(notes)
    authority = ActorAuthority("notes-app")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver({(WORK_ID, "1.0.0"): policy}),
        capabilities=capabilities,
        actor_authority=authority,
        storage=storage,
    )
    return runner, authority


def issue(authority: ActorAuthority, actor_id: str, grants: set[str]) -> TrustedActor:
    return authority.issue(actor_id=actor_id, grants=grants, ttl=timedelta(minutes=5))


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "work.sqlite"
        first_notes = Notes()
        storage = SqliteStorage(path)
        runner, authority = open_runner(storage, first_notes)
        clerk = issue(authority, "user:ada", {READ, PUBLISH})
        waiting = await runner.run(
            work_id=WORK_ID,
            version="1.0.0",
            input_data={"text": "ship the storage guide"},
            actor=clerk,
            idempotency_key="note-1",
        )
        assert waiting.status is ExecutionStatus.WAITING_APPROVAL
        assert first_notes.calls.get(PUBLISH, 0) == 0
        (pending,) = await runner.approvals.list_pending(waiting.execution_id)
        digest = pending.action_digest
        execution_id = waiting.execution_id
        approval_id = pending.approval_id
        storage.close()

        # The process is gone. A new one opens the same file.
        second_notes = Notes()
        storage = SqliteStorage(path)
        runner, authority = open_runner(storage, second_notes)
        (still_pending,) = await runner.approvals.list_pending(execution_id)
        assert still_pending.approval_id == approval_id
        assert still_pending.action_digest == digest
        reviewer = issue(authority, "user:bruno", {f"approve:{WORK_ID}"})
        await runner.approvals.approve(approval_id, reviewer=reviewer)
        clerk = issue(authority, "user:ada", {READ, PUBLISH})
        done = await runner.resume(execution_id, actor=clerk)
        outcome = done.raise_for_status()
        assert outcome.output.model_dump() == {"published": True}
        assert second_notes.calls.get(READ, 0) == 0  # replayed from the journal
        assert second_notes.calls.get(PUBLISH, 0) == 1
        storage.close()

        storage = SqliteStorage(path)
        stored = await storage.executions.get(execution_id)
        events = await storage.events.list(execution_id)
        storage.close()
        assert stored is not None
        assert stored.status is ExecutionStatus.COMPLETED
        print(f"status:   {stored.status.value}")
        print(f"events:   {len(events)}")
        print("publish:  once, after the file was reopened")
        print("OK")


if __name__ == "__main__":
    asyncio.run(main())
