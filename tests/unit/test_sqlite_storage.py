"""SQLite storage edges the shared contract does not exercise."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from _factories import DIGEST_A, T0, make_execution, make_outcome
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.state_machine import transition_execution
from id42_work.states import ExecutionStatus
from id42_work.storage import SqliteStorage
from id42_work.storage.codec import (
    dump_execution,
    dump_input,
    load_execution,
    load_input,
    load_record,
    resolve_model,
)
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope


def _edit(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    """Change rows with the stdlib driver, then close the connection.

    The connection's context manager commits the transaction and, since Python 3.12,
    leaves the connection open. An unclosed connection warns on 3.13.
    """
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(sql, params)
    finally:
        connection.close()


class Greeting(BaseModel):
    name: str


class Box:
    class Nested(BaseModel):
        n: int


def test_file_reopen_keeps_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "work.sqlite"
    with SqliteStorage(path) as storage:
        assert storage.schema_version == 2
        assert storage.path == str(path)
    reopened = SqliteStorage(path)
    try:
        assert reopened.schema_version == 2
        reopened.close()
        reopened.close()
    finally:
        reopened.close()


def test_memory_database_is_private() -> None:
    with SqliteStorage(":memory:") as first, SqliteStorage(":memory:") as second:
        assert first.path == ":memory:"
        assert second.schema_version == 2


def test_rejects_empty_path_negative_timeout_and_a_directory(tmp_path: Path) -> None:
    with pytest.raises(StorageError, match="non-empty"):
        SqliteStorage("   ")
    with pytest.raises(StorageError, match="timeout"):
        SqliteStorage(tmp_path / "work.sqlite", timeout=-1)
    with pytest.raises(StorageError, match="cannot open"):
        SqliteStorage(tmp_path)


async def test_closed_storage_refuses_callers(tmp_path: Path) -> None:
    storage = SqliteStorage(tmp_path / "work.sqlite")
    storage.close()
    with pytest.raises(StorageError, match="closed"):
        await storage.executions.get(uuid4())


def test_version_1_file_gains_the_claim_table(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    SqliteStorage(path).close()
    _edit(path, "DROP TABLE execution_claims")
    _edit(path, "DELETE FROM schema_migrations WHERE version = 2")
    reopened = SqliteStorage(path)
    try:
        assert reopened.schema_version == 2
    finally:
        reopened.close()
    connection = sqlite3.connect(path)
    try:
        found = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'execution_claims'"
        ).fetchone()
    finally:
        connection.close()
    assert found is not None


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    SqliteStorage(path).close()
    _edit(path, "UPDATE schema_migrations SET version = 9 WHERE version = 2")
    with pytest.raises(StorageError, match="newer"):
        SqliteStorage(path)


async def test_null_tenant_idempotency_round_trips(tmp_path: Path) -> None:
    storage = SqliteStorage(tmp_path / "work.sqlite")
    scope = IdempotencyScope(
        tenant_id=None, work_id="contract_work", work_version="1.0.0", idempotency_key="k"
    )
    record = IdempotencyRecord(scope=scope, input_fingerprint=DIGEST_A, execution_id=uuid4())
    try:
        assert await storage.idempotency.reserve(record) == record
        assert await storage.idempotency.get(scope) == record
        storage.close()
        reopened = SqliteStorage(tmp_path / "work.sqlite")
        try:
            assert await reopened.idempotency.get(scope) == record
        finally:
            reopened.close()
    finally:
        storage.close()


async def test_completed_execution_round_trips_its_outcome(tmp_path: Path) -> None:
    eid = uuid4()
    execution = make_execution(
        execution_id=eid,
        status=ExecutionStatus.COMPLETED,
        outcome=make_outcome(eid, accepted=True),
    )
    storage = SqliteStorage(tmp_path / "work.sqlite")
    await storage.executions.create(execution)
    storage.close()
    reopened = SqliteStorage(tmp_path / "work.sqlite")
    try:
        stored = await reopened.executions.get(eid)
    finally:
        reopened.close()
    assert stored == execution
    assert stored is not None
    assert stored.outcome is not None
    assert execution.outcome is not None
    restored = stored.outcome
    original = execution.outcome
    assert type(restored.output) is type(original.output)


async def test_corrupt_rows_raise_storage_error(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    storage = SqliteStorage(path)
    execution = make_execution()
    await storage.executions.create(execution)
    reference = await storage.inputs.put(execution.execution_id, Greeting(name="ada"))
    storage.close()

    _edit(path, "UPDATE executions SET body = ?", ("{",))
    broken = SqliteStorage(path)
    try:
        with pytest.raises(StorageError, match="not valid JSON"):
            await broken.executions.get(execution.execution_id)
    finally:
        broken.close()

    _edit(path, "UPDATE executions SET body = ?", ("[]",))
    broken = SqliteStorage(path)
    try:
        with pytest.raises(StorageError, match="not a JSON object"):
            await broken.executions.get(execution.execution_id)
    finally:
        broken.close()

    _edit(path, "UPDATE executions SET body = ?", ("{}",))
    broken = SqliteStorage(path)
    try:
        with pytest.raises(StorageError, match="invalid"):
            await broken.executions.get(execution.execution_id)
    finally:
        broken.close()

    _edit(path, "UPDATE inputs SET body = ?, model_ref = ?", ("{", "missing.module:Gone"))
    broken = SqliteStorage(path)
    try:
        with pytest.raises(StorageError, match="cannot import"):
            await broken.inputs.get(reference)
    finally:
        broken.close()


async def test_two_connections_one_update_wins(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    left = SqliteStorage(path)
    execution = make_execution()
    await left.executions.create(execution)
    right = SqliteStorage(path)
    try:
        with pytest.raises(StorageError, match="already exists"):
            await right.executions.create(execution)
        policy = transition_execution(execution, ExecutionStatus.POLICY_CHECK, at=T0)
        cancelled = transition_execution(execution, ExecutionStatus.CANCELLED, at=T0)
        await left.executions.compare_and_set(policy)
        with pytest.raises(ConcurrentModificationError):
            await right.executions.compare_and_set(cancelled)
        assert await right.executions.get(execution.execution_id) == policy
    finally:
        left.close()
        right.close()


def test_codec_rejects_bad_model_references() -> None:
    with pytest.raises(StorageError, match="invalid model reference"):
        resolve_model("no-separator")
    with pytest.raises(StorageError, match="invalid model reference"):
        resolve_model(":Bare")
    with pytest.raises(StorageError, match="invalid model reference"):
        resolve_model("id42_work.storage.codec:")
    with pytest.raises(StorageError, match="cannot import"):
        resolve_model("missing.module:Gone")
    with pytest.raises(StorageError, match="cannot import"):
        resolve_model("id42_work.storage.codec:missing")
    with pytest.raises(StorageError, match="not a pydantic model"):
        resolve_model("id42_work.storage.codec:dump_record")
    nested = f"{Box.Nested.__module__}:{Box.Nested.__qualname__}"
    assert resolve_model(nested) is Box.Nested
    ref, payload = dump_input(Box.Nested(n=3))
    assert load_input(ref, payload) == Box.Nested(n=3)
    with pytest.raises(StorageError, match="invalid"):
        load_record(Greeting, "{")


def test_codec_restores_and_rejects_outcomes() -> None:
    eid = uuid4()
    execution = make_execution(
        execution_id=eid,
        status=ExecutionStatus.COMPLETED,
        outcome=make_outcome(eid, accepted=True),
    )
    assert load_execution(dump_execution(execution)) == execution
    with pytest.raises(StorageError, match="not valid JSON"):
        load_execution("{")
    with pytest.raises(StorageError, match="not a JSON object"):
        load_execution("[]")

    raw = json.loads(dump_execution(execution))
    raw["outcome"] = []
    with pytest.raises(StorageError, match="not a JSON object"):
        load_execution(json.dumps(raw))
    raw["outcome"] = {"output": {}}
    with pytest.raises(StorageError, match="no output model"):
        load_execution(json.dumps(raw))
    raw["outcome"] = {
        "output_model": f"{Greeting.__module__}:{Greeting.__qualname__}",
        "output": {"name": 1},
    }
    with pytest.raises(StorageError, match="output is invalid"):
        load_execution(json.dumps(raw))
