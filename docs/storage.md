# Storage

The runner never keeps execution state in memory by itself. It writes through a
`Storage`: executions, the action journal, approvals, events, evidence, validated
inputs and idempotency reservations.

Three implementations ship with the SDK.

| | `MemoryStorage` | `SqliteStorage` | `PostgresStorage` |
|---|---|---|---|
| Where the data lives | The Python process | One SQLite file | One PostgreSQL database |
| Survives process exit | No | Yes | Yes |
| Extra dependency | None | None (`sqlite3` is in the standard library) | `id42-work[postgres]` (`psycopg` 3) |
| Crash recovery | No | Yes, through `resume` | Yes, through `resume` |
| Resume claim | This process only | Lease row, taken with `BEGIN IMMEDIATE` | Session advisory lock |
| Concurrent processes | Do not share an instance | May open the same file; writers wait on SQLite | Each opens its own connection |

`MemoryStorage` is the right default for tests and for examples that should not touch
a disk. `SqliteStorage` is the persistent backend with no extra dependency.
`PostgresStorage` is the same contract on PostgreSQL, for more than one host.

## Opening SQLite

```python
from id42_work import SqliteStorage

storage = SqliteStorage("work.sqlite")  # parent directories are created
# ...
storage.close()
```

Or as a context manager:

```python
with SqliteStorage("work.sqlite") as storage:
    runner = WorkRunner(..., storage=storage)
```

`timeout` (seconds, default 5) is how long SQLite waits when another process holds
the write lock. A negative timeout is rejected. `":memory:"` builds a private
database that disappears with the connection; it cannot be reopened, so it is not
useful for recovery.

Reopen the same path after a restart. Schema migrations run on open.

```python
storage = SqliteStorage("work.sqlite")
assert storage.schema_version == 2
assert storage.path == "work.sqlite"
```

## Opening PostgreSQL

```bash
pip install 'id42-work[postgres]'
```

```python
from id42_work import PostgresStorage

storage = PostgresStorage("postgresql://id42:id42@localhost:5432/work")
# ...
storage.close()
```

`timeout` (seconds, default 5) is the connection timeout. Zero waits without a
limit. A negative timeout, or a blank connection string, is rejected. The role
needs permission to create the tables in `public` the first time the database is
opened. The SDK does not create the database itself.

```python
with PostgresStorage(dsn) as storage:
    assert storage.schema_version == 1
```

Pass that object as `storage=` to `WorkRunner`. The rest of the API (`run`, `resume`,
`cancel`, approvals) does not change. `examples/postgres_resume.py` reopens a
database across an approval. It reads `ID42_POSTGRES_DSN` and creates a throwaway
database, so the role also needs `CREATEDB` for that example.

## What is stored

Each record is one row. The body is the model's JSON form. Indexed columns
(`revision`, `status`, action id, event sequence, idempotency scope) are what
compare-and-set and the lookups use.

- **Executions** are replaced only when the stored revision is the new revision minus
  one. A lost update raises `ConcurrentModificationError` and writes nothing.
  `ExecutionRepository.list` reads every execution, ordered by creation time then
  id. It is not filtered by actor. `WorkRunner.list_executions` returns that
  sequence. The MCP server filters it before a client sees it. No schema change
  is required.
- **Journal entries** are inserted in call order. An update matches the expected
  status, the same way the in-memory repository does.
- **Events** are gapless per execution. Registry-level events (no execution id) use
  their own stream. `commit_execution` stores the execution and appends its event in
  one transaction: both commit, or neither does.
- **Approvals, evidence and inputs** follow the same rules as `MemoryStorage`.
- **Idempotency** reservations are inserted with `ON CONFLICT DO NOTHING`. The first
  writer wins. A missing tenant is stored as an empty string, which cannot be a real
  tenant id.
- **Resume claims** are not part of the execution record. SQLite keeps them in
  `execution_claims`. PostgreSQL holds a session advisory lock and does not write a
  row.

Validated inputs and an execution's outcome keep the Pydantic class as
`module:qualname`, the same shape as a work schema reference. On load, that class is
imported. It has to be importable in the process that resumes: define input and
output models at module level, not inside a function. A class defined in the script
that is running (`__main__`) is found in that process. The database is trusted host
storage. It replaces process memory; it is not a place to load untrusted code from.

`ActorAuthority` keys are still random and stay in memory. After a restart the host
issues new actors. The execution stores the actor id, not the seal, so a new actor
with the same id and tenant can resume.

## Migrations

Each database has a `schema_migrations` table. Opening applies every migration newer
than the version recorded there, inside one transaction. There is no separate
migration command.

The two backends version their schemas independently.

| Backend | Current version | What it created |
|---|---|---|
| SQLite | 2 | Version 1 is the records above. Version 2 adds `execution_claims`. |
| PostgreSQL | 1 | The records above. The resume claim is an advisory lock, not a table. |

A database written by a newer package (a higher version than this process knows) is
refused with `StorageError`. Downgrading the package against a migrated database is
not supported.

A SQLite file written by the previous slice (version 1) gains `execution_claims` the
next time it is opened. Adding a migration later means appending a new version in
`id42_work.storage.sqlite` or `id42_work.storage.postgres`.

## Crash recovery

`resume` does three jobs.

- A `READY` execution (an approval was granted) runs the executor again from the
  start. `SUCCEEDED` actions return the journaled result and do not call the
  provider. A `FAILED` action raises again. A `PENDING_APPROVAL` action consumes a
  grant whose digest matches, or invalidates one that does not.
- A `RUNNING` execution means the previous process stopped while it was driving.
  Resume continues it only when no live claim still holds it.
- A `VALIDATING` execution means the previous process stopped after the executor
  returned, while validators were running. The side effect is already in the
  journal. Resume rebuilds the output and runs the validators again. It continues
  only when no live claim still holds the execution.

If a capability call is still `IN_FLIGHT`, resume probes it when the provider
defines `reconcile`. `APPLIED` is journaled `SUCCEEDED` and replayed. `NOT_APPLIED`
is retried with the same operation id. Otherwise resume marks the entry
`UNCERTAIN`, emits `work.action.uncertain`, and moves the execution to `FAILED`
with `requires_reconciliation`. `invoke` is not called for a call that stayed
unresolved. That is the case the specification describes: the process died during
the call, and the external system may already have accepted it. A `context.record` left
`IN_FLIGHT` is not one of those calls. Its result was never returned to the executor,
so resume computes it again and replays capability calls it already completed. A
journal update writes the status and the body in one transaction, so a crash cannot
store `SUCCEEDED` without the result.

If no capability call is `IN_FLIGHT`, the executor runs again. Completed actions are
replayed.
Work that had not started yet runs once. A policy that now denies the resuming actor
fails the execution (`policy_denied_on_resume`). `RUNNING` cannot move to `DENIED`;
the transition table has no such edge.

`run` with the same idempotency key still returns the existing execution and does
not resume it. Call `resume` yourself.

A crash during `VALIDATING` is recovered by the same `resume` call. The output is
not stored until that phase finishes, so resume does not read it back. It runs the
executor again. `SUCCEEDED` actions return the recorded result and do not call the
provider. A stored `context.record`, including a `RecordedModel` response, is
returned the same way and `compute` is not called. The executor must be
deterministic, the same contract as any other resume. An action that is not already
a completed or failed journal entry is not performed: recovery must not start a new
side effect. A `FAILED` action raises again, as on any replay. A missing record, a
different fingerprint, or a record with no stored result fails closed and is not
computed.

The replay uses `execution_limits.timeout` as its own budget. Validators then run
under a fresh budget, measured from the start of this validation phase. The
execution is already `VALIDATING`. The transition table has no
`VALIDATING -> VALIDATING` edge, so resume does not emit
`work.outcome.validation_started` again. The phase ends `VALIDATION_FAILED`,
`WAITING_REVIEW` or `COMPLETED`. That transition and its event are one commit.

Validators are read-only. They may call only capabilities with
`side_effecting=False`, and those calls are not journaled, so a recovery run may
call a read-only provider again. A validator must be safe to run more than once.
A validator that raises, or that is still running at the deadline, is a failed
check, as on the first run.

`VALIDATING` cannot move to `FAILED` or `DENIED`. When recovery cannot safely
finish, it fails closed to `VALIDATION_FAILED` and does not call the provider:

- the resuming actor's policy now denies the work (`policy_denied_on_resume`);
- the journal has an `IN_FLIGHT` or `UNCERTAIN` capability call
  (`uncertain_outcome`; the `IN_FLIGHT` call is marked `UNCERTAIN`, and
  `requires_reconciliation` is set);
- the journal has a `PENDING_APPROVAL` action, the replay asks for an action that
  was not completed, or a `context.record` is missing, mismatched or unfinished
  (`non_deterministic`). An unfinished record is not computed and is not marked
  `UNCERTAIN`;
- the replay raises, or exceeds the execution timeout (`executor_error` or
  `timeout`).

Resume of `WAITING_APPROVAL` is unchanged: decide the approval (which moves the
execution to `READY`), then resume.

See [Journal and replay](concepts/journal.md) for the replay rules,
`examples/sqlite_resume.py` for a restart across an approval, and
`examples/postgres_resume.py` for the same flow on PostgreSQL.

## Resume claims

Two layers stop a second driver.

1. Inside one runner, a set of execution ids. A second `resume` in that process
   returns the execution unchanged.
2. In the storage, a claim other processes can see. `run` and `resume` take it
   before they drive and release it when they return, including when the body
   raises.

On SQLite the claim is a row written inside `BEGIN IMMEDIATE`. The row stores the
owner, the process id, and an expiry (`WorkRunner` passes `resume_claim_ttl`, one
hour by default). The next caller takes the row immediately when that pid is no
longer running. A live pid keeps it until release or until the TTL passes. After the
TTL, another process may take the claim even if the first process is still alive, so
keep the TTL longer than the work's own timeout. Closing the file does not delete
the row; the runner releases it, and a dead pid is the signal that a crash left it
behind.

On PostgreSQL the claim is `pg_try_advisory_lock` on the session. It lasts until
`release_resume` or until the connection closes, which includes `close()` and a
process exit. The TTL is validated and not stored: a live session is not stolen on
a timer. The lock id is a hash of the execution id, so two executions could
theoretically share a lock. That collision is not something the SDK detects.

`MemoryStorage` keeps the same methods in the process. It has no pid and no file.
Two runners that share one instance still cannot both hold the claim.

If `run` finds the claim already taken, it leaves the new execution in `READY` and
returns it. That is the window after the execution is visible and before this
process reserved it.

## Limits

- **Exactly-once is still impossible.** A crash after the external system accepted a
  capability call and before the journal moved from `IN_FLIGHT` to `SUCCEEDED` is
  `UNCERTAIN` unless a provider probe can prove otherwise. `NOT_APPLIED` is the
  only result that retries the same operation id. See
  [Reconciliation](concepts/reconciliation.md).
- **SQLite serializes writers.** Many processes may read. A writer waits up to
  `timeout`, then SQLite raises. The SDK wraps open failures as `StorageError`.
- **A hung SQLite process can be stolen after `resume_claim_ttl`.** PostgreSQL does
  not expire a claim while the session is open.
- **Validation recovery does not repeat a finished side effect.** The output is
  rebuilt from the journal. A new side effect is refused. Read-only validator calls
  may run again, because they are not journaled.
- **Reconciliation is a separate decision.** `id42-work reconcile` and
  `ReconciliationConsole` record it. The MCP server has no reconcile tool. An
  abandoned call stays failed.
