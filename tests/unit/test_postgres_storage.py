"""PostgreSQL edges the shared contract does not exercise."""

from collections.abc import Iterator
from datetime import timedelta
from uuid import uuid4

import pytest

from _factories import DIGEST_A, T0, make_execution, make_outcome
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.state_machine import transition_execution
from id42_work.states import ExecutionStatus
from id42_work.storage.postgres import PostgresStorage
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope
from postgres_support import configured_dsn, create_database, drop_database

pytestmark = pytest.mark.postgres


@pytest.fixture
def database() -> Iterator[tuple[str, str]]:
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    dsn, name = create_database(admin)
    try:
        yield dsn, admin
    finally:
        drop_database(admin, name)


def test_open_migrates_and_reopen_keeps_the_version(database: tuple[str, str]) -> None:
    dsn, _admin = database
    with PostgresStorage(dsn) as storage:
        assert storage.schema_version == 1
    reopened = PostgresStorage(dsn)
    try:
        assert reopened.schema_version == 1
        reopened.close()
        reopened.close()
    finally:
        reopened.close()


def test_rejects_an_empty_dsn_a_negative_timeout_and_a_closed_port() -> None:
    with pytest.raises(StorageError, match="non-empty"):
        PostgresStorage("   ")
    with pytest.raises(StorageError, match="timeout"):
        PostgresStorage("postgresql://localhost/id42", timeout=-1)
    with pytest.raises(StorageError, match="cannot open"):
        PostgresStorage("postgresql://id42:id42@127.0.0.1:1/id42", timeout=1)


async def test_closed_storage_refuses_callers(database: tuple[str, str]) -> None:
    storage = PostgresStorage(database[0])
    storage.close()
    with pytest.raises(StorageError, match="closed"):
        await storage.executions.get(uuid4())
    with pytest.raises(StorageError, match="closed"):
        await storage.claim_resume(uuid4(), "owner", now=T0, ttl=timedelta(seconds=5))


async def test_newer_schema_is_refused(database: tuple[str, str]) -> None:
    dsn = database[0]
    PostgresStorage(dsn).close()
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("UPDATE schema_migrations SET version = 9")
    with pytest.raises(StorageError, match="newer"):
        PostgresStorage(dsn)


async def test_null_tenant_and_outcome_round_trip(database: tuple[str, str]) -> None:
    storage = PostgresStorage(database[0])
    scope = IdempotencyScope(
        tenant_id=None, work_id="contract_work", work_version="1.0.0", idempotency_key="k"
    )
    record = IdempotencyRecord(scope=scope, input_fingerprint=DIGEST_A, execution_id=uuid4())
    execution_id = uuid4()
    execution = make_execution(
        execution_id=execution_id,
        status=ExecutionStatus.COMPLETED,
        outcome=make_outcome(execution_id, accepted=True),
    )
    try:
        assert await storage.idempotency.reserve(record) == record
        assert await storage.idempotency.get(scope) == record
        await storage.executions.create(execution)
        storage.close()
        reopened = PostgresStorage(database[0])
        try:
            assert await reopened.idempotency.get(scope) == record
            stored = await reopened.executions.get(execution_id)
        finally:
            reopened.close()
    finally:
        storage.close()
    assert stored == execution
    assert stored is not None
    assert stored.outcome is not None
    assert execution.outcome is not None
    assert type(stored.outcome.output) is type(execution.outcome.output)


async def test_two_connections_one_update_wins(database: tuple[str, str]) -> None:
    left = PostgresStorage(database[0])
    execution = make_execution()
    await left.executions.create(execution)
    right = PostgresStorage(database[0])
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


async def test_resume_claim_is_one_session_at_a_time(database: tuple[str, str]) -> None:
    holder = PostgresStorage(database[0])
    other = PostgresStorage(database[0])
    execution_id = uuid4()
    ttl = timedelta(seconds=30)
    try:
        assert await holder.claim_resume(execution_id, "holder", now=T0, ttl=ttl)
        assert not await holder.claim_resume(execution_id, "holder", now=T0, ttl=ttl)
        assert not await other.claim_resume(execution_id, "other", now=T0, ttl=ttl)
        await other.release_resume(execution_id, "other")
        await holder.release_resume(execution_id, "not-the-holder")
        assert not await other.claim_resume(execution_id, "other", now=T0, ttl=ttl)
        await holder.release_resume(execution_id, "holder")
        assert await other.claim_resume(execution_id, "other", now=T0, ttl=ttl)
        other.close()
        assert await holder.claim_resume(execution_id, "holder", now=T0, ttl=ttl)
    finally:
        holder.close()
        other.close()
