# Storage

The runner never keeps execution state in memory by itself. It writes through a
`Storage`: executions, the action journal, approvals, events, evidence, validated
inputs and idempotency reservations.

Two implementations ship with the SDK.

| | `MemoryStorage` | `SqliteStorage` |
|---|---|---|
| Where the data lives | The Python process | One SQLite file |
| Survives process exit | No | Yes |
| Extra dependency | None | None (`sqlite3` is in the standard library) |
| Crash recovery | No | Yes, through `resume` |
| Concurrent processes | Do not share an instance | May open the same file; writers wait on SQLite |

`MemoryStorage` is the right default for tests and for examples that should not touch
a disk. `SqliteStorage` is the persistent backend. PostgreSQL, which would use the
same storage contract, is not implemented yet.

## Opening a database

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
assert storage.schema_version == 1
assert storage.path == "work.sqlite"
```

Pass that object as `storage=` to `WorkRunner`. The rest of the API (`run`, `resume`,
`cancel`, approvals) does not change.

## What is stored

Each record is one row. The body is the model's JSON form. Indexed columns
(`revision`, `status`, action id, event sequence, idempotency scope) are what
compare-and-set and the lookups use.

- **Executions** are replaced only when the stored revision is the new revision minus
  one. A lost update raises `ConcurrentModificationError` and writes nothing.
- **Journal entries** are inserted in call order. An update matches the expected
  status, the same way the in-memory repository does.
- **Events** are gapless per execution. Registry-level events (no execution id) use
  their own stream. `commit_execution` stores the execution and appends its event in
  one transaction: both commit, or neither does.
- **Approvals, evidence and inputs** follow the same rules as `MemoryStorage`.
- **Idempotency** reservations are inserted with `ON CONFLICT DO NOTHING`. The first
  writer wins. A missing tenant is stored as an empty string, which cannot be a real
  tenant id.

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

The database has a `schema_migrations` table. Opening a file applies every migration
newer than the version recorded there, inside one transaction. The current version
is 1, which creates the tables above.

A file written by a newer package (a higher version than this process knows) is
refused with `StorageError`. Downgrading the package against a migrated file is not
supported. There is no separate migration command: the next open migrates.

Adding a migration later means appending a new version to the list in
`id42_work.storage.sqlite` and including the SQL that moves a version-1 file forward.
Existing files pick it up the next time they are opened.

## Crash recovery

`resume` does two jobs.

- A `READY` execution (an approval was granted) runs the executor again from the
  start. `SUCCEEDED` actions return the journaled result and do not call the
  provider. A `FAILED` action raises again. A `PENDING_APPROVAL` action consumes a
  grant whose digest matches, or invalidates one that does not.
- A `RUNNING` execution means the previous process stopped while it was driving.
  The new process does not hold the old claim, so `resume` may continue it.

If any journal entry is still `IN_FLIGHT`, resume marks each of them `UNCERTAIN`,
emits `work.action.uncertain`, and moves the execution to `FAILED` with
`requires_reconciliation`. The provider is not called again. That is the case the
specification describes: the process died during the call, the external system may
already have accepted it, and the SDK does not retry.

If nothing is `IN_FLIGHT`, the executor runs again. Completed actions are replayed.
Work that had not started yet runs once. A policy that now denies the resuming actor
fails the execution (`policy_denied_on_resume`). `RUNNING` cannot move to `DENIED`;
the transition table has no such edge.

`run` with the same idempotency key still returns the existing execution and does
not resume it. Call `resume` yourself.

A crash while the status is `VALIDATING` leaves the execution there. The output is
not stored until validation finishes, and `resume` does not invent it. Resume of
`WAITING_APPROVAL` is unchanged: decide the approval (which moves the execution to
`READY`), then resume.

See [Journal and replay](concepts/journal.md) for the replay rules, and
`examples/sqlite_resume.py` for a restart across an approval.

## Limits

- **The resume claim is per process.** Two live processes that both call `resume` on
  the same non-terminal execution can both drive it. Execution leases, which would
  make that claim visible across processes, are not in this slice.
- **Exactly-once is still impossible.** A crash after the external system accepted a
  call and before the journal moved from `IN_FLIGHT` to `SUCCEEDED` is `UNCERTAIN`.
  Reconcile with the operation id. The SDK will not retry.
- **SQLite serializes writers.** Many processes may read. A writer waits up to
  `timeout`, then SQLite raises. The SDK wraps open failures as `StorageError`.
- **No PostgreSQL yet.** A second backend needs the optional driver, a service in
  CI, and the same 100% branch coverage. `StorageContractSuite` is the contract it
  will have to pass.
- **No reconciliation tool.** Uncertain actions are recorded. Comparing them with the
  external system is the host's job.
