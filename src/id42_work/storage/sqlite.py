"""SQLite storage (stdlib ``sqlite3``).

One file holds executions, journals, approvals, events, evidence, inputs and
idempotency reservations. Opening the file applies any pending schema migration.
Writers take an immediate transaction, so a compare-and-set either commits in full
or leaves the database unchanged. A ``threading.Lock`` serializes use of the
connection inside one process; other processes are serialized by SQLite itself.

``:memory:`` builds a private database that dies with the connection. Crash recovery
needs a file path.

The runner's resume claim is still per process. Two live processes must not drive
the same execution; execution leases are a later step.
"""

import contextlib
import os
import sqlite3
import threading
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Self
from uuid import UUID

from pydantic import BaseModel

from id42_work.approvals.models import Approval, ApprovalStatus
from id42_work.events.models import WorkEvent
from id42_work.evidence.models import Evidence
from id42_work.exceptions import ConcurrentModificationError, StorageError
from id42_work.execution import Execution
from id42_work.journal import JournalEntry, JournalStatus
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

_MIGRATION_1: tuple[str, ...] = (
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
    VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    """,
)

_MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((_SCHEMA_VERSION, _MIGRATION_1),)


def _database_name(path: str | os.PathLike[str]) -> str:
    text = os.fspath(path)
    if not text.strip():
        msg = "sqlite path must be a non-empty path"
        raise StorageError(msg)
    if text == ":memory:":
        return text
    file_path = Path(text)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    return str(file_path)


def _schema_version(conn: sqlite3.Connection) -> int:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if found is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 0
    return int(row[0])


def _migrate(conn: sqlite3.Connection) -> None:
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


class _DB:
    """One SQLite connection and the lock that guards it."""

    __slots__ = ("_conn", "_lock")

    def __init__(self, database: str, timeout: float) -> None:
        self._lock = threading.Lock()
        try:
            self._conn: sqlite3.Connection | None = sqlite3.connect(
                database,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            msg = f"cannot open sqlite database: {error}"
            raise StorageError(msg) from error
        self._conn.execute("PRAGMA foreign_keys = ON").fetchall()
        self._conn.execute("PRAGMA journal_mode = WAL").fetchall()
        try:
            with self.transaction() as conn:
                _migrate(conn)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close the connection. A second call does nothing."""
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            msg = "sqlite storage is closed"
            raise StorageError(msg)
        return conn

    @contextlib.contextmanager
    def reading(self) -> Generator[sqlite3.Connection]:
        """Hold the process lock and yield the open connection."""
        with self._lock:
            yield self._open()

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Run one immediate transaction. An error rolls it back."""
        with self._lock:
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise


def _next_ordinal(conn: sqlite3.Connection, table: str, execution_id: UUID) -> int:
    # Table names are fixed literals chosen by the caller, never user input.
    query = f"SELECT COALESCE(MAX(ordinal), 0) + 1 FROM {table} WHERE execution_id = ?"  # noqa: S608
    row = conn.execute(query, (str(execution_id),)).fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 1
    return int(row[0])


class SqliteExecutionRepository:
    """Executions and their action journals, stored in SQLite."""

    def __init__(self, db: _DB) -> None:
        self._db = db

    async def create(self, execution: Execution) -> None:
        with self._db.transaction() as conn:
            _insert_execution(conn, execution)

    async def get(self, execution_id: UUID) -> Execution | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT body FROM executions WHERE execution_id = ?", (str(execution_id),)
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
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(eid), entry.action_id, ordinal, entry.status.value, dump_record(entry)),
                )
            except sqlite3.IntegrityError as error:
                msg = f"action {entry.action_id!r} is already journaled"
                raise StorageError(msg, execution_id=eid) from error

    async def update_journal(self, entry: JournalEntry, *, expected_status: JournalStatus) -> None:
        eid = entry.execution_id
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE journal_entries
                SET status = ?, body = ?
                WHERE execution_id = ? AND action_id = ? AND status = ?
                """,
                (
                    entry.status.value,
                    dump_record(entry),
                    str(eid),
                    entry.action_id,
                    expected_status.value,
                ),
            )
            if cursor.rowcount == 1:
                return
            current = conn.execute(
                "SELECT status FROM journal_entries WHERE execution_id = ? AND action_id = ?",
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
                WHERE execution_id = ?
                ORDER BY ordinal
                """,
                (str(execution_id),),
            ).fetchall()
        return tuple(load_record(JournalEntry, str(row[0])) for row in rows)


class SqliteApprovalRepository:
    """Approval records stored in SQLite."""

    def __init__(self, db: _DB) -> None:
        self._db = db

    async def create(self, approval: Approval) -> None:
        with self._db.transaction() as conn:
            ordinal = _next_ordinal(conn, "approvals", approval.execution_id)
            try:
                conn.execute(
                    """
                    INSERT INTO approvals (approval_id, execution_id, status, ordinal, body)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(approval.approval_id),
                        str(approval.execution_id),
                        approval.status.value,
                        ordinal,
                        dump_record(approval),
                    ),
                )
            except sqlite3.IntegrityError as error:
                msg = f"approval {approval.approval_id} already exists"
                raise StorageError(msg, execution_id=approval.execution_id) from error

    async def get(self, approval_id: UUID) -> Approval | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT body FROM approvals WHERE approval_id = ?", (str(approval_id),)
            ).fetchone()
        if row is None:
            return None
        return load_record(Approval, str(row[0]))

    async def list_for_execution(
        self, execution_id: UUID, *, status: ApprovalStatus | None = None
    ) -> tuple[Approval, ...]:
        query = "SELECT body FROM approvals WHERE execution_id = ?"
        params: list[str] = [str(execution_id)]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY ordinal"
        with self._db.reading() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(load_record(Approval, str(row[0])) for row in rows)

    async def list_by_status(self, status: ApprovalStatus) -> tuple[Approval, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                "SELECT body FROM approvals WHERE status = ? ORDER BY ordinal",
                (status.value,),
            ).fetchall()
        return tuple(load_record(Approval, str(row[0])) for row in rows)

    async def compare_and_set(self, updated: Approval, *, expected_status: ApprovalStatus) -> None:
        with self._db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, body = ?
                WHERE approval_id = ? AND status = ?
                """,
                (
                    updated.status.value,
                    dump_record(updated),
                    str(updated.approval_id),
                    expected_status.value,
                ),
            )
            if cursor.rowcount == 1:
                return
            current = conn.execute(
                "SELECT status FROM approvals WHERE approval_id = ?",
                (str(updated.approval_id),),
            ).fetchone()
            if current is None:
                msg = f"approval {updated.approval_id} does not exist"
                raise StorageError(msg, execution_id=updated.execution_id)
            msg = (
                f"approval {updated.approval_id} is {current[0]}, expected {expected_status.value}"
            )
            raise ConcurrentModificationError(msg, execution_id=updated.execution_id)


class SqliteEventRepository:
    """Append-only event streams stored in SQLite."""

    def __init__(self, db: _DB) -> None:
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
                WHERE stream_id = ?
                ORDER BY sequence_number
                """,
                (_stream_id(execution_id),),
            ).fetchall()
        return tuple(load_record(WorkEvent, str(row[0])) for row in rows)


class SqliteIdempotencyRepository:
    """Idempotency reservations stored in SQLite.

    A missing tenant is stored as an empty string. Tenant ids cannot be empty, so the
    sentinel does not collide with a real tenant.
    """

    def __init__(self, db: _DB) -> None:
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
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (*key, record.input_fingerprint, str(record.execution_id)),
            )
            row = conn.execute(
                """
                SELECT tenant_key, work_id, work_version, idempotency_key,
                       input_fingerprint, execution_id
                FROM idempotency
                WHERE tenant_key = ? AND work_id = ? AND work_version = ? AND idempotency_key = ?
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
                WHERE tenant_key = ? AND work_id = ? AND work_version = ?
                  AND idempotency_key = ? AND execution_id = ?
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
                WHERE tenant_key = ? AND work_id = ? AND work_version = ? AND idempotency_key = ?
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


class SqliteEvidenceRepository:
    """Evidence records stored in SQLite."""

    def __init__(self, db: _DB) -> None:
        self._db = db

    async def add(self, evidence: Evidence) -> None:
        with self._db.transaction() as conn:
            ordinal = _next_ordinal(conn, "evidence", evidence.execution_id)
            try:
                conn.execute(
                    """
                    INSERT INTO evidence (evidence_id, execution_id, ordinal, body)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        str(evidence.evidence_id),
                        str(evidence.execution_id),
                        ordinal,
                        dump_record(evidence),
                    ),
                )
            except sqlite3.IntegrityError as error:
                msg = f"evidence {evidence.evidence_id} already exists"
                raise StorageError(msg, execution_id=evidence.execution_id) from error

    async def list(self, execution_id: UUID) -> tuple[Evidence, ...]:
        with self._db.reading() as conn:
            rows = conn.execute(
                """
                SELECT body FROM evidence
                WHERE execution_id = ?
                ORDER BY ordinal
                """,
                (str(execution_id),),
            ).fetchall()
        return tuple(load_record(Evidence, str(row[0])) for row in rows)


class SqliteInputStore:
    """Validated inputs stored beside the execution, not inside events."""

    def __init__(self, db: _DB) -> None:
        self._db = db

    async def put(self, execution_id: UUID, data: BaseModel) -> str:
        reference = f"sqlite://inputs/{execution_id}"
        model_ref, payload = dump_input(data)
        with self._db.transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO inputs (reference, execution_id, model_ref, body)
                    VALUES (?, ?, ?, ?)
                    """,
                    (reference, str(execution_id), model_ref, payload),
                )
            except sqlite3.IntegrityError as error:
                msg = f"input for execution {execution_id} already stored"
                raise StorageError(msg, execution_id=execution_id) from error
        return reference

    async def get(self, reference: str) -> BaseModel | None:
        with self._db.reading() as conn:
            row = conn.execute(
                "SELECT model_ref, body FROM inputs WHERE reference = ?", (reference,)
            ).fetchone()
        if row is None:
            return None
        return load_input(str(row[0]), str(row[1]))


class SqliteStorage:
    """File-backed repositories for ``WorkRunner``.

    ``path`` is a filesystem path. Parent directories are created. Pass
    ``":memory:"`` only for a throwaway database. ``timeout`` is how long SQLite
    waits, in seconds, when another process holds the write lock.

    Use :meth:`close` or a ``with`` block so the file is not left locked.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 5.0) -> None:
        if timeout < 0:
            msg = "timeout must be zero or positive"
            raise StorageError(msg)
        database = _database_name(path)
        self._path = database
        self._db = _DB(database, timeout)
        self.executions = SqliteExecutionRepository(self._db)
        self.approvals = SqliteApprovalRepository(self._db)
        self.events = SqliteEventRepository(self._db)
        self.idempotency = SqliteIdempotencyRepository(self._db)
        self.evidence = SqliteEvidenceRepository(self._db)
        self.inputs = SqliteInputStore(self._db)

    @property
    def path(self) -> str:
        """Filesystem path, or ``":memory:"``."""
        return self._path

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


def _execution_exists(conn: sqlite3.Connection, execution_id: UUID) -> bool:
    row = conn.execute(
        "SELECT 1 FROM executions WHERE execution_id = ?", (str(execution_id),)
    ).fetchone()
    return row is not None


def _insert_execution(conn: sqlite3.Connection, execution: Execution) -> None:
    eid = execution.execution_id
    if execution.revision != 0:
        msg = "new executions must have revision 0"
        raise StorageError(msg, execution_id=eid)
    try:
        conn.execute(
            """
            INSERT INTO executions (execution_id, revision, status, body)
            VALUES (?, ?, ?, ?)
            """,
            (str(eid), execution.revision, execution.status.value, dump_execution(execution)),
        )
    except sqlite3.IntegrityError as error:
        msg = f"execution {eid} already exists"
        raise StorageError(msg, execution_id=eid) from error


def _replace_execution(conn: sqlite3.Connection, updated: Execution) -> None:
    eid = updated.execution_id
    cursor = conn.execute(
        """
        UPDATE executions
        SET revision = ?, status = ?, body = ?
        WHERE execution_id = ? AND revision = ?
        """,
        (
            updated.revision,
            updated.status.value,
            dump_execution(updated),
            str(eid),
            updated.revision - 1,
        ),
    )
    if cursor.rowcount == 1:
        return
    current = conn.execute(
        "SELECT revision FROM executions WHERE execution_id = ?", (str(eid),)
    ).fetchone()
    if current is None:
        msg = f"execution {eid} does not exist"
        raise StorageError(msg, execution_id=eid)
    msg = f"execution {eid} is at revision {current[0]}, update expects {updated.revision - 1}"
    raise ConcurrentModificationError(msg, execution_id=eid)


def _last_sequence(conn: sqlite3.Connection, execution_id: UUID | None) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) FROM events WHERE stream_id = ?",
        (_stream_id(execution_id),),
    ).fetchone()
    if row is None:  # pragma: no cover - an aggregate query always returns a row
        return 0
    return int(row[0])


def _require_next(conn: sqlite3.Connection, event: WorkEvent) -> None:
    expected = _last_sequence(conn, event.execution_id) + 1
    if event.sequence_number != expected:
        msg = f"expected sequence {expected}, got {event.sequence_number}"
        raise ConcurrentModificationError(msg, execution_id=event.execution_id)


def _insert_event(conn: sqlite3.Connection, event: WorkEvent) -> None:
    conn.execute(
        """
        INSERT INTO events (stream_id, sequence_number, body)
        VALUES (?, ?, ?)
        """,
        (_stream_id(event.execution_id), event.sequence_number, dump_record(event)),
    )


def _idempotency_record(row: Sequence[object]) -> IdempotencyRecord:
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
