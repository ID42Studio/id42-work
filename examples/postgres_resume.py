"""An approval survives closing PostgreSQL, and resume does not repeat the read.

Requires the ``postgres`` extra and ``ID42_POSTGRES_DSN`` pointing at a server
whose role can create databases.

Run: ID42_POSTGRES_DSN=postgresql://id42:id42@localhost:5432/id42 python examples/postgres_resume.py
"""

import asyncio
import os
import sys
from collections.abc import Mapping
from datetime import timedelta
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
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


def open_runner(storage: PostgresStorage, notes: Notes) -> tuple[WorkRunner, ActorAuthority]:
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


def _create_database(admin_dsn: str) -> tuple[str, str]:
    name = f"id42_example_{uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return make_conninfo(admin_dsn, dbname=name), name


def _drop_database(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


async def main() -> None:
    admin = os.environ.get("ID42_POSTGRES_DSN", "").strip()
    if not admin:
        print("Set ID42_POSTGRES_DSN to a PostgreSQL connection string.", file=sys.stderr)
        raise SystemExit(1)
    dsn, name = _create_database(admin)
    try:
        first_notes = Notes()
        storage = PostgresStorage(dsn)
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

        # The process is gone. A new one opens the same database.
        second_notes = Notes()
        storage = PostgresStorage(dsn)
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

        storage = PostgresStorage(dsn)
        stored = await storage.executions.get(execution_id)
        events = await storage.events.list(execution_id)
        storage.close()
        assert stored is not None
        assert stored.status is ExecutionStatus.COMPLETED
        print(f"status:   {stored.status.value}")
        print(f"events:   {len(events)}")
        print("publish:  once, after the database was reopened")
        print("OK")
    finally:
        _drop_database(admin, name)


if __name__ == "__main__":
    asyncio.run(main())
