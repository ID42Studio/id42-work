"""Resume claims: expiry, dead processes, and a runner that cannot take one."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from _factories import T0
from _runtime import Env
from id42_work import (
    CapabilityRegistry,
    ExecutionStatus,
    MemoryStorage,
    SqliteStorage,
    StaticPolicyResolver,
    WorkRunner,
)
from id42_work.exceptions import StorageError


def _edit(path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.execute(sql, params)
    finally:
        connection.close()


async def test_memory_claim_is_exclusive_until_it_expires() -> None:
    storage = MemoryStorage()
    execution_id = uuid4()
    ttl = timedelta(seconds=30)
    assert await storage.claim_resume(execution_id, "owner-a", now=T0, ttl=ttl)
    assert not await storage.claim_resume(execution_id, "owner-b", now=T0, ttl=ttl)
    await storage.release_resume(execution_id, "owner-b")
    assert not await storage.claim_resume(execution_id, "owner-b", now=T0, ttl=ttl)
    later = T0 + timedelta(seconds=31)
    assert await storage.claim_resume(execution_id, "owner-b", now=later, ttl=ttl)
    await storage.release_resume(execution_id, "owner-b")
    assert await storage.claim_resume(execution_id, "owner-a", now=later, ttl=ttl)


@pytest.mark.parametrize(
    ("owner", "now", "ttl", "match"),
    [
        ("  ", T0, timedelta(seconds=1), "non-empty"),
        ("owner", datetime(2026, 1, 1), timedelta(seconds=1), "timezone-aware"),
        ("owner", T0, timedelta(0), "positive"),
    ],
)
async def test_claim_rejects_bad_arguments(
    owner: str, now: datetime, ttl: timedelta, match: str
) -> None:
    storage = MemoryStorage()
    with pytest.raises(StorageError, match=match):
        await storage.claim_resume(uuid4(), owner, now=now, ttl=ttl)


async def test_sqlite_claim_release_expiry_and_closed_storage(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    storage = SqliteStorage(path)
    execution_id = uuid4()
    ttl = timedelta(seconds=30)
    try:
        assert await storage.claim_resume(execution_id, "owner-a", now=T0, ttl=ttl)
        assert not await storage.claim_resume(execution_id, "owner-a", now=T0, ttl=ttl)
        await storage.release_resume(execution_id, "someone-else")
        assert not await storage.claim_resume(execution_id, "owner-b", now=T0, ttl=ttl)
        await storage.release_resume(execution_id, "owner-a")
        assert await storage.claim_resume(execution_id, "owner-b", now=T0, ttl=timedelta(seconds=1))
        assert await storage.claim_resume(
            execution_id, "owner-a", now=T0 + timedelta(seconds=2), ttl=ttl
        )
    finally:
        storage.close()
    with pytest.raises(StorageError, match="closed"):
        await storage.claim_resume(execution_id, "owner-a", now=T0, ttl=ttl)


async def test_sqlite_takes_over_a_dead_or_unreadable_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "work.sqlite"
    storage = SqliteStorage(path)
    dead = uuid4()
    future = (T0 + timedelta(hours=1)).isoformat()
    _edit(
        path,
        """
        INSERT INTO execution_claims (execution_id, owner, pid, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (str(dead), "gone", 2_147_483_646, future),
    )
    zero = uuid4()
    _edit(
        path,
        """
        INSERT INTO execution_claims (execution_id, owner, pid, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (str(zero), "gone", 0, future),
    )
    try:
        assert await storage.claim_resume(dead, "next", now=T0, ttl=timedelta(minutes=1))
        assert await storage.claim_resume(zero, "next", now=T0, ttl=timedelta(minutes=1))
        held = uuid4()
        assert await storage.claim_resume(held, "live", now=T0, ttl=timedelta(minutes=1))

        def deny(pid: int, signal: int) -> None:
            del pid, signal
            raise PermissionError

        monkeypatch.setattr("id42_work.storage.sqlite.os.kill", deny)
        assert not await storage.claim_resume(held, "other", now=T0, ttl=timedelta(minutes=1))

        def missing(pid: int, signal: int) -> None:
            del pid, signal
            raise OSError

        monkeypatch.setattr("id42_work.storage.sqlite.os.kill", missing)
        assert await storage.claim_resume(held, "other", now=T0, ttl=timedelta(minutes=1))
    finally:
        storage.close()


async def test_sqlite_rejects_a_corrupt_claim_expiry(tmp_path: Path) -> None:
    path = tmp_path / "work.sqlite"
    storage = SqliteStorage(path)
    broken = uuid4()
    naive = uuid4()
    _edit(
        path,
        "INSERT INTO execution_claims (execution_id, owner, pid, expires_at) VALUES (?, ?, ?, ?)",
        (str(broken), "x", 1, "not-a-timestamp"),
    )
    _edit(
        path,
        "INSERT INTO execution_claims (execution_id, owner, pid, expires_at) VALUES (?, ?, ?, ?)",
        (str(naive), "x", 1, "2026-01-01T00:00:00"),
    )
    try:
        with pytest.raises(StorageError, match="expiry is invalid"):
            await storage.claim_resume(broken, "next", now=T0, ttl=timedelta(minutes=1))
        with pytest.raises(StorageError, match="expiry is invalid"):
            await storage.claim_resume(naive, "next", now=T0, ttl=timedelta(minutes=1))
    finally:
        storage.close()


async def test_run_leaves_ready_when_the_storage_claim_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(
        self: MemoryStorage,
        execution_id: object,
        owner: str,
        *,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        del self, execution_id, owner, now, ttl
        return False

    monkeypatch.setattr(MemoryStorage, "claim_resume", refuse)
    env = Env()
    execution = await env.run()
    assert execution.status is ExecutionStatus.READY
    assert env.executor.runs == 0


def test_runner_rejects_a_non_positive_claim_ttl() -> None:
    env = Env()
    with pytest.raises(ValueError, match="resume_claim_ttl"):
        WorkRunner(
            registry=env.registry,
            policies=StaticPolicyResolver({}),
            capabilities=CapabilityRegistry(),
            actor_authority=env.authority,
            storage=MemoryStorage(),
            resume_claim_ttl=timedelta(0),
        )


def test_claim_deadline_normalizes_to_utc() -> None:
    from id42_work.storage.claims import claim_deadline  # noqa: PLC0415

    ahead = datetime(2026, 1, 1, 15, 0, tzinfo=timezone(timedelta(hours=2)))
    deadline = claim_deadline("owner", ahead, timedelta(minutes=5))
    assert deadline == datetime(2026, 1, 1, 13, 5, tzinfo=UTC)
