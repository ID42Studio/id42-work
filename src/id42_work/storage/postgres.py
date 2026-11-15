"""PostgreSQL storage (optional ``psycopg`` 3 driver).

Install it with the ``postgres`` extra. One database holds executions, journals,
approvals, events, evidence, inputs and idempotency reservations. Opening applies
any pending schema migration inside a single transaction.

Compare-and-set and ``commit_execution`` each run in one transaction, so a conflict
leaves the database unchanged. A ``threading.Lock`` serializes use of the connection
inside one process. Other processes use their own connections.

A resume claim is a session advisory lock. It lasts until this connection releases
it or closes, including when the process exits. Two live processes therefore cannot
drive the same execution. The lock id is a hash of the execution id.
"""

import contextlib
import hashlib
import math
import threading
from collections.abc import Generator, Sequence
from datetime import datetime, timedelta
from typing import Any, LiteralString, Self
from uuid import UUID

import psycopg
from psycopg import Connection, sql
from psycopg.rows import TupleRow
from pydantic import BaseModel

from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus
from id42_work.storage.claims import claim_deadline
from id42_work.storage.codec import (
    dump_execution,
    dump_input,
    dump_record,
    load_execution,
    load_input,
    load_record,
)
from id42_work.storage.protocols import IdempotencyRecord, IdempotencyScope

_SCHEMA_VERSION = 1
_NO_TENANT = ""
_REGISTRY_STREAM = ""

_MIGRATION_1: tuple[LiteralString, ...] = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE executions (
        execution_id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        status TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE journal_entries (
        execution_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        status TEXT NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (execution_id, action_id)
    )
    """,
    """
    CREATE INDEX journal_by_execution ON journal_entries (execution_id, ordinal)
    """,
    """
    CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL,
        status TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        body TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX approvals_by_execution ON approvals (execution_id, ordinal)
    """,
    """
    CREATE INDEX approvals_by_status ON approvals (status, ordinal)
    """,
    """
    CREATE TABLE events (
        stream_id TEXT NOT NULL,
        sequence_number INTEGER NOT NULL,
        body TEXT NOT NULL,
        PRIMARY KEY (stream_id, sequence_number)
    )
    """,
    """
    CREATE TABLE idempotency (
        tenant_key TEXT NOT NULL,
        work_id TEXT NOT NULL,
        work_version TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        input_fingerprint TEXT NOT NULL,
        execution_id TEXT NOT NULL,
        PRIMARY KEY (tenant_key, work_id, work_version, idempotency_key)
    )
    """,
    """
    CREATE TABLE evidence (
        evidence_id TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        body TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX evidence_by_execution ON evidence (execution_id, ordinal)
    """,
    """
    CREATE TABLE inputs (
        reference TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL UNIQUE,
        model_ref TEXT NOT NULL,
        body TEXT NOT NULL
    )
    """,
    """
    INSERT INTO schema_migrations (version, applied_at)
    VALUES (1, to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'))
    """,
)

_MIGRATIONS: tuple[tuple[int, tuple[LiteralString, ...]], ...] = ((_SCHEMA_VERSION, _MIGRATION_1),)


def _connect_seconds(timeout: float) -> int:
    if timeout < 0:
        msg = "timeout must be zero or positive"
        raise StorageError(msg)
    return math.ceil(timeout)


def _schema_version(conn: Connection[TupleRow]) -> int:
    found = conn.execute(
        """
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
        """
    ).fetchone()
    if found is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 0
    return int(row[0])


def _migrate(conn: Connection[TupleRow]) -> None:
    current = _schema_version(conn)
    if current > _SCHEMA_VERSION:
        msg = f"database schema version {current} is newer than this package ({_SCHEMA_VERSION})"
        raise StorageError(msg)
    for version, statements in _MIGRATIONS:
        if version <= current:
            continue
        for statement in statements:
            conn.execute(statement)


def _stream_id(execution_id: UUID | None) -> str:
    if execution_id is None:
        return _REGISTRY_STREAM
    return str(execution_id)


def _tenant_key(tenant_id: str | None) -> str:
    if tenant_id is None:
        return _NO_TENANT
    return tenant_id


def _tenant_id(tenant_key: str) -> str | None:
    if tenant_key == _NO_TENANT:
        return None
    return tenant_key


def _advisory_key(execution_id: UUID) -> int:
    """Signed 64-bit lock id. Distinct executions can theoretically share one."""
    digest = hashlib.sha256(execution_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class _PG:
    """One PostgreSQL connection and the lock that guards it."""

    __slots__ = ("_conn", "_held", "_lock")

    def __init__(self, dsn: str, timeout_seconds: int) -> None:
        self._lock = threading.Lock()
        self._held: dict[UUID, str] = {}
        try:
            self._conn: Connection[TupleRow] | None = psycopg.connect(
                dsn,
                autocommit=True,
                connect_timeout=timeout_seconds,
                application_name="id42-work",
            )
        except psycopg.Error as error:
            msg = f"cannot open postgres database: {error}"
            raise StorageError(msg) from error
        try:
            with self.transaction() as conn:
                _migrate(conn)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close the connection. A second call does nothing.

        Closing drops every advisory lock this session still holds.
        """
        with self._lock:
            conn = self._conn
            self._conn = None
            self._held.clear()
            if conn is not None:
                conn.close()

    def _open(self) -> Connection[TupleRow]:
        conn = self._conn
        if conn is None:
            msg = "postgres storage is closed"
            raise StorageError(msg)
        return conn

    @contextlib.contextmanager
    def reading(self) -> Generator[Connection[TupleRow]]:
        """Hold the process lock and yield the open connection."""
        with self._lock:
            yield self._open()

    @contextlib.contextmanager
    def transaction(self) -> Generator[Connection[TupleRow]]:
        """Run one transaction. An error rolls it back."""
        with self._lock:
            conn = self._open()
            with conn.transaction():
                yield conn

    def claim(self, execution_id: UUID, owner: str, *, now: datetime, ttl: timedelta) -> bool:
        """Take the session lock. ``now`` and ``ttl`` are validated and not stored."""
        claim_deadline(owner, now, ttl)
        with self._lock:
            conn = self._open()
            if execution_id in self._held:
                return False
            row = conn.execute(
                "SELECT pg_try_advisory_lock(%s::bigint)",
                (_advisory_key(execution_id),),
            ).fetchone()
            if row is None:  # pragma: no cover - a SELECT of a function always returns a row
                return False
            if not bool(row[0]):
                return False
            self._held[execution_id] = owner
            return True

    def release(self, execution_id: UUID, owner: str) -> None:
        """Unlock when ``owner`` holds the claim on this connection."""
        with self._lock:
            if self._held.get(execution_id) != owner:
                return
            conn = self._open()
            conn.execute(
                "SELECT pg_advisory_unlock(%s::bigint)",
                (_advisory_key(execution_id),),
            )
            del self._held[execution_id]


def _next_ordinal(conn: Connection[TupleRow], table: str, execution_id: UUID) -> int:
    # Table names are fixed literals chosen by the caller, never user input.
    query = sql.SQL("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM {} WHERE execution_id = %s").format(
        sql.Identifier(table)
    )
    row = conn.execute(query, (str(execution_id),)).fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 1
    return int(row[0])


class PostgresExecutionRepository:
    """Executions and their action journals, stored in PostgreSQL."""

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def create(self, execution: Execution) -> None:
        with self._db.transaction() as conn:
            _insert_execution(conn, execution)

    async def get(self, execution_id: UUID) -> Execution | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT body FROM executions WHERE execution_id = %s", (str(execution_id),)
            ).fetchone()
        if row is None:
            return None
        return load_execution(str(row[0]))

    async def compare_and_set(self, updated: Execution) -> None:
        with self._db.transaction() as conn:
            _replace_execution(conn, updated)

    async def append_journal(self, entry: JournalEntry) -> None:
        eid = entry.execution_id
        with self._db.transaction() as conn:
            if not _execution_exists(conn, eid):
                msg = f"execution {eid} does not exist"
                raise StorageError(msg, execution_id=eid)
            ordinal = _next_ordinal(conn, "journal_entries", eid)
            try:
                conn.execute(
                    """
                    INSERT INTO journal_entries
                        (execution_id, action_id, ordinal, status, body)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (str(eid), entry.action_id, ordinal, entry.status.value, dump_record(entry)),
                )
            except psycopg.errors.UniqueViolation as error:
                msg = f"action {entry.action_id!r} is already journaled"
                raise StorageError(msg, execution_id=eid) from error

    async def update_journal(self, entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        eid = entry.execution_id
        with self._db.transaction() as conn:
            updated = conn.execute(
                """
                UPDATE journal_entries
                SET status = %s, body = %s
                WHERE execution_id = %s AND action_id = %s AND status = %s
                RETURNING action_id
                """,
                (
                    entry.status.value,
                    dump_record(entry),
                    str(eid),
                    entry.action_id,
                    expected_status.value,
                ),
            ).fetchone()
            if updated is not None:
                return
            current = conn.execute(
                "SELECT status FROM journal_entries WHERE execution_id = %s AND action_id = %s",
                (str(eid), entry.action_id),
            ).fetchone()
            if current is None:
                msg = f"action {entry.action_id!r} is not journaled"
                raise StorageError(msg, execution_id=eid)
            msg = f"action {entry.action_id!r} is {current[0]}, expected {expected_status.value}"
            raise ConcurrentModificationError(msg, execution_id=eid)

    async def list_journal(self, execution_id: UUID) -> tuple[JournalEntry, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                """
                SELECT body FROM journal_entries
                WHERE execution_id = %s
                ORDER BY ordinal
                """,
                (str(execution_id),),
            ).fetchall()
        return tuple(load_record(JournalEntry, str(row[0])) for row in rows)


class PostgresApprovalRepository:
    """Approval records stored in PostgreSQL."""

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def create(self, approval: Approval) -> None:
        with self._db.transaction() as conn:
            ordinal = _next_ordinal(conn, "approvals", approval.execution_id)
            try:
                conn.execute(
                    """
                    INSERT INTO approvals (approval_id, execution_id, status, ordinal, body)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        str(approval.approval_id),
                        str(approval.execution_id),
                        approval.status.value,
                        ordinal,
                        dump_record(approval),
                    ),
                )
            except psycopg.errors.UniqueViolation as error:
                msg = f"approval {approval.approval_id} already exists"
                raise StorageError(msg, execution_id=approval.execution_id) from error

    async def get(self, approval_id: UUID) -> Approval | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT body FROM approvals WHERE approval_id = %s", (str(approval_id),)
            ).fetchone()
        if row is None:
            return None
        return load_record(Approval, str(row[0]))

    async def list_for_execution(
        self, execution_id: UUID, *, status: ApprovalStatus | None = None
    ) -> tuple[Approval, ...]:
        if status is None:
            query = "SELECT body FROM approvals WHERE execution_id = %s ORDER BY ordinal"
            params: tuple[str, ...] = (str(execution_id),)
        else:
            query = (
                "SELECT body FROM approvals WHERE execution_id = %s AND status = %s "
                "ORDER BY ordinal"
            )
            params = (str(execution_id), status.value)
        with self._db.reading() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(load_record(Approval, str(row[0])) for row in rows)

    async def list_by_status(self, status: ApprovalStatus) -> tuple[Approval, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                "SELECT body FROM approvals WHERE status = %s ORDER BY ordinal",
                (status.value,),
            ).fetchall()
        return tuple(load_record(Approval, str(row[0])) for row in rows)

    async def compare_and_set(self, updated: Approval, *, expected_status: ApprovalStatus) -> None:
        with self._db.transaction() as conn:
            changed = conn.execute(
                """
                UPDATE approvals
                SET status = %s, body = %s
                WHERE approval_id = %s AND status = %s
                RETURNING approval_id
                """,
                (
                    updated.status.value,
                    dump_record(updated),
                    str(updated.approval_id),
                    expected_status.value,
                ),
            ).fetchone()
            if changed is not None:
                return
            current = conn.execute(
                "SELECT status FROM approvals WHERE approval_id = %s",
                (str(updated.approval_id),),
            ).fetchone()
            if current is None:
                msg = f"approval {updated.approval_id} does not exist"
                raise StorageError(msg, execution_id=updated.execution_id)
            msg = (
                f"approval {updated.approval_id} is {current[0]}, expected {expected_status.value}"
            )
            raise ConcurrentModificationError(msg, execution_id=updated.execution_id)


class PostgresEventRepository:
    """Append-only event streams stored in PostgreSQL."""

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def append(self, event: WorkEvent) -> None:
        with self._db.transaction() as conn:
            _require_next(conn, event)
            _insert_event(conn, event)

    async def last_sequence(self, execution_id: UUID | None) -> int:
        with self._db.reading() as conn:
            return _last_sequence(conn, execution_id)

    async def list(self, execution_id: UUID | None) -> tuple[WorkEvent, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                """
                SELECT body FROM events
                WHERE stream_id = %s
                ORDER BY sequence_number
                """,
                (_stream_id(execution_id),),
            ).fetchall()
        return tuple(load_record(WorkEvent, str(row[0])) for row in rows)


class PostgresIdempotencyRepository:
    """Idempotency reservations stored in PostgreSQL.

    A missing tenant is stored as an empty string. Tenant ids cannot be empty, so the
    sentinel does not collide with a real tenant.
    """

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def reserve(self, record: IdempotencyRecord) -> IdempotencyRecord:
        scope = record.scope
        key = (
            _tenant_key(scope.tenant_id),
            scope.work_id,
            scope.work_version,
            scope.idempotency_key,
        )
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO idempotency (
                    tenant_key, work_id, work_version, idempotency_key,
                    input_fingerprint, execution_id
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (*key, record.input_fingerprint, str(record.execution_id)),
            )
            row = conn.execute(
                """
                SELECT tenant_key, work_id, work_version, idempotency_key,
                       input_fingerprint, execution_id
                FROM idempotency
                WHERE tenant_key = %s AND work_id = %s AND work_version = %s
                  AND idempotency_key = %s
                """,
                key,
            ).fetchone()
        if row is None:  # pragma: no cover - the insert or the previous row is visible
            msg = "idempotency reservation disappeared"
            raise StorageError(msg)
        return _idempotency_record(row)

    async def release(self, scope: IdempotencyScope, *, execution_id: UUID) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                DELETE FROM idempotency
                WHERE tenant_key = %s AND work_id = %s AND work_version = %s
                  AND idempotency_key = %s AND execution_id = %s
                """,
                (
                    _tenant_key(scope.tenant_id),
                    scope.work_id,
                    scope.work_version,
                    scope.idempotency_key,
                    str(execution_id),
                ),
            )

    async def get(self, scope: IdempotencyScope) -> IdempotencyRecord | None:
        with self._db.reading() as conn:
            row = conn.execute(
                """
                SELECT tenant_key, work_id, work_version, idempotency_key,
                       input_fingerprint, execution_id
                FROM idempotency
                WHERE tenant_key = %s AND work_id = %s AND work_version = %s
                  AND idempotency_key = %s
                """,
                (
                    _tenant_key(scope.tenant_id),
                    scope.work_id,
                    scope.work_version,
                    scope.idempotency_key,
                ),
            ).fetchone()
        if row is None:
            return None
        return _idempotency_record(row)


class PostgresEvidenceRepository:
    """Evidence records stored in PostgreSQL."""

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def add(self, evidence: Evidence) -> None:
        with self._db.transaction() as conn:
            ordinal = _next_ordinal(conn, "evidence", evidence.execution_id)
            try:
                conn.execute(
                    """
                    INSERT INTO evidence (evidence_id, execution_id, ordinal, body)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        str(evidence.evidence_id),
                        str(evidence.execution_id),
                        ordinal,
                        dump_record(evidence),
                    ),
                )
            except psycopg.errors.UniqueViolation as error:
                msg = f"evidence {evidence.evidence_id} already exists"
                raise StorageError(msg, execution_id=evidence.execution_id) from error

    async def list(self, execution_id: UUID) -> tuple[Evidence, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                """
                SELECT body FROM evidence
                WHERE execution_id = %s
                ORDER BY ordinal
                """,
                (str(execution_id),),
            ).fetchall()
        return tuple(load_record(Evidence, str(row[0])) for row in rows)


class PostgresInputStore:
    """Validated inputs stored beside the execution, not inside events."""

    def __init__(self, db: _PG) -> None:
        self._db = db

    async def put(self, execution_id: UUID, data: BaseModel) -> str:
        reference = f"postgres://inputs/{execution_id}"
        model_ref, payload = dump_input(data)
        with self._db.transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO inputs (reference, execution_id, model_ref, body)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (reference, str(execution_id), model_ref, payload),
                )
            except psycopg.errors.UniqueViolation as error:
                msg = f"input for execution {execution_id} already stored"
                raise StorageError(msg, execution_id=execution_id) from error
        return reference

    async def get(self, reference: str) -> BaseModel | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT model_ref, body FROM inputs WHERE reference = %s", (reference,)
            ).fetchone()
        if row is None:
            return None
        return load_input(str(row[0]), str(row[1]))


class PostgresStorage:
    """PostgreSQL repositories for ``WorkRunner``.

    ``dsn`` is a libpq connection string or URL. ``timeout`` is how long, in seconds,
    to wait while opening the connection. Zero waits without a limit.

    The role needs permission to create tables in ``public`` on an empty database.
    Use :meth:`close` or a ``with`` block so the session, and any resume claim it
    holds, is released.

    Requires the ``postgres`` extra (``psycopg`` 3).
    """

    def __init__(self, dsn: str, *, timeout: float = 5.0) -> None:
        if not dsn.strip():
            msg = "postgres dsn must be a non-empty string"
            raise StorageError(msg)
        self._db = _PG(dsn, _connect_seconds(timeout))
        self.executions = PostgresExecutionRepository(self._db)
        self.approvals = PostgresApprovalRepository(self._db)
        self.events = PostgresEventRepository(self._db)
        self.idempotency = PostgresIdempotencyRepository(self._db)
        self.evidence = PostgresEvidenceRepository(self._db)
        self.inputs = PostgresInputStore(self._db)

    @property
    def schema_version(self) -> int:
        """Schema version currently applied to this database."""
        with self._db.reading() as conn:
            return _schema_version(conn)

    def close(self) -> None:
        """Close the database. Further use raises ``StorageError``."""
        self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def claim_resume(
        self, execution_id: UUID, owner: str, *, now: datetime, ttl: timedelta
    ) -> bool:
        """Try to reserve ``execution_id`` for ``owner`` on this connection.

        The reservation is a session advisory lock. It does not expire while the
        connection stays open. ``ttl`` is checked and otherwise ignored.
        """
        return self._db.claim(execution_id, owner, now=now, ttl=ttl)

    async def release_resume(self, execution_id: UUID, owner: str) -> None:
        """Drop the reservation when this connection holds it for ``owner``."""
        self._db.release(execution_id, owner)

    async def commit_execution(
        self, updated: Execution, event: WorkEvent, *, create: bool = False
    ) -> None:
        """Store ``updated`` and append ``event`` in one transaction."""
        if event.execution_id != updated.execution_id:
            msg = "event does not belong to the execution"
            raise StorageError(msg, execution_id=updated.execution_id)
        with self._db.transaction() as conn:
            _require_next(conn, event)
            if create:
                _insert_execution(conn, updated)
            else:
                _replace_execution(conn, updated)
            _insert_event(conn, event)


def _execution_exists(conn: Connection[TupleRow], execution_id: UUID) -> bool:
    row = conn.execute(
        "SELECT 1 FROM executions WHERE execution_id = %s", (str(execution_id),)
    ).fetchone()
    return row is not None


def _insert_execution(conn: Connection[TupleRow], execution: Execution) -> None:
    eid = execution.execution_id
    if execution.revision != 0:
        msg = "new executions must have revision 0"
        raise StorageError(msg, execution_id=eid)
    try:
        conn.execute(
            """
            INSERT INTO executions (execution_id, revision, status, body)
            VALUES (%s, %s, %s, %s)
            """,
            (str(eid), execution.revision, execution.status.value, dump_execution(execution)),
        )
    except psycopg.errors.UniqueViolation as error:
        msg = f"execution {eid} already exists"
        raise StorageError(msg, execution_id=eid) from error


def _replace_execution(conn: Connection[TupleRow], updated: Execution) -> None:
    eid = updated.execution_id
    changed = conn.execute(
        """
        UPDATE executions
        SET revision = %s, status = %s, body = %s
        WHERE execution_id = %s AND revision = %s
        RETURNING execution_id
        """,
        (
            updated.revision,
            updated.status.value,
            dump_execution(updated),
            str(eid),
            updated.revision - 1,
        ),
    ).fetchone()
    if changed is not None:
        return
    current = conn.execute(
        "SELECT revision FROM executions WHERE execution_id = %s", (str(eid),)
    ).fetchone()
    if current is None:
        msg = f"execution {eid} does not exist"
        raise StorageError(msg, execution_id=eid)
    msg = f"execution {eid} is at revision {current[0]}, update expects {updated.revision - 1}"
    raise ConcurrentModificationError(msg, execution_id=eid)


def _last_sequence(conn: Connection[TupleRow], execution_id: UUID | None) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) FROM events WHERE stream_id = %s",
        (_stream_id(execution_id),),
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 0
    return int(row[0])


def _require_next(conn: Connection[TupleRow], event: WorkEvent) -> None:
    expected = _last_sequence(conn, event.execution_id) + 1
    if event.sequence_number != expected:
        msg = f"expected sequence {expected}, got {event.sequence_number}"
        raise ConcurrentModificationError(msg, execution_id=event.execution_id)


def _insert_event(conn: Connection[TupleRow], event: WorkEvent) -> None:
    conn.execute(
        """
        INSERT INTO events (stream_id, sequence_number, body)
        VALUES (%s, %s, %s)
        """,
        (_stream_id(event.execution_id), event.sequence_number, dump_record(event)),
    )


def _idempotency_record(row: Sequence[Any]) -> IdempotencyRecord:
    tenant_key, work_id, work_version, idempotency_key, fingerprint, execution_id = (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        str(row[5]),
    )
    return IdempotencyRecord(
        scope=IdempotencyScope(
            tenant_id=_tenant_id(tenant_key),
            work_id=work_id,
            work_version=work_version,
            idempotency_key=idempotency_key,
        ),
        input_fingerprint=fingerprint,
        execution_id=UUID(execution_id),
    )
