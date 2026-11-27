# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). During `0.x`, minor versions may break
the public API; breaking changes are listed explicitly.

## [Unreleased]

### Added

- MCP server: optional `session_actor` so distinct HTTP sessions are distinct
  actors. `credential_session_actor` reads `Id42-Actor-Credential`
  (`TrustedActor.to_credential`); the server still verifies the seal. HTTP
  without that header is refused. Stdio has no header and stays the factory
  actor. Omitting `session_actor` keeps the previous single pinned actor, and
  a credential header is ignored in that mode. `host_actor()` stays the factory
  actor used by `id42-work approvals --resume`. `list_works` and
  `list_executions` take `limit` (default 50, maximum 100) and an opaque cursor
  and return `next_cursor`. `ExecutionRepository.list` and
  `WorkRunner.list_executions` accept an `ExecutionListQuery`. SQLite and
  PostgreSQL apply it in SQL, with no schema migration. There is still no
  approve, reject or reconcile tool. See `docs/mcp.md`.
- `ActorAuthority` can persist its HMAC keys in a file (`key_path`, mode `0600`,
  atomic replace). `create_key_file=True` creates the file when it is missing.
  The constructor does not read the environment;
  `authority_key_path_from_env()` reads `ID42_WORK_AUTHORITY_KEY_PATH`.
  `rotate_keys()` retires the current key; retired keys still verify seals they
  produced. `TrustedActor.to_credential()` / `from_credential()` carry a seal to
  another process, which must still `verify` it. The default, with no path,
  remains an in-memory key. The execution database does not store the key.
  See `docs/security.md`.
- `id42-work reconcile list|show|applied|not-applied|abandon --factory module:callable`
  settles an `UNCERTAIN` capability call. A provider may define
  `async reconcile(self, call) -> ReconciliationProbe`; `resume` runs it when the
  hook exists. `APPLIED` is replayed, `NOT_APPLIED` retries the same operation id,
  and `UNKNOWN` leaves the call for an operator. The operator is sealed with only
  `reconcile:<work_id>`, must not be the execution's actor, and every decision is
  `RECONCILIATION_DECISION` evidence with the actor and the reason. Abandon leaves
  the execution failed. `FAILED` and `VALIDATION_FAILED` may return to `READY`
  only through this path. The MCP server still has no reconcile tool. See
  `docs/concepts/reconciliation.md`.
- `id42-work approvals list|show|approve|reject --factory module:callable` decides
  a waiting approval as an operator. The factory is the same contract as
  `mcp serve`. The operator is sealed by that factory's authority, in the
  execution's tenant, with only the approval's reviewer grant, and must not be
  the execution's actor. The reason and the actor are stored on the approval and
  on `APPROVAL_DECISION` evidence. `--resume` continues the execution as the
  factory's host actor. Rejection ends the execution `REJECTED`. The MCP server
  still has no approve or reject tool. See `docs/concepts/approval.md`.
- MCP server: optional extra `mcp` (`pip install "id42-work[mcp]"`). `id42_work.mcp`
  serves governed execution to MCP clients over stdio, or over streamable HTTP on
  `127.0.0.1`. Tools start, inspect, resume, list and cancel executions of
  registered works. Resources expose the journal, events, evidence and approvals.
  The client cannot pass an actor, a grant or an approval decision. Policies,
  validators and approval gates stay inside `WorkRunner`. The command is
  `id42-work mcp serve --factory module:callable`. See `docs/mcp.md`.
- `ExecutionRepository.list` and `WorkRunner.list_executions` return every stored
  execution, ordered by creation time then id. `WorkRunner.registered_works`
  returns the registered definitions. Neither is filtered by actor. The MCP server
  filters executions before a client sees them. No schema migration.
- AI agents: `id42_work.agents` describes granted capabilities as model tools, and
  `id42_work.integrations.pydantic_ai` (extra `pydantic-ai`) gives PydanticAI agents a
  `capability_toolset` and a `RecordedModel` that journals model responses. A resumed
  agent replays its recorded turns, reaches the action that waited for approval and
  continues. See `docs/agents.md` and `examples/agent_invoice.py`.
- `context.record(action_id, fingerprint=, compute=)` journals any non-deterministic
  computation and replays it on resume. `context.capability_specs` exposes the granted
  capabilities' specs, and `CapabilitySpec.params_schema` describes their arguments.
- `SqliteStorage` persists executions, journals, approvals, events, evidence, inputs
  and idempotency reservations in one SQLite file. It uses the standard library only
  and passes the storage contract. The schema is versioned and migrated on open.
- `PostgresStorage` (`id42-work[postgres]`, psycopg 3) persists the same records in
  PostgreSQL and passes the same contract. The schema is migrated on open. The driver
  stays optional: importing the package does not require it.
- `resume` recovers a `RUNNING` execution after the process stops. Completed actions
  are replayed. A capability call left `IN_FLIGHT` becomes `UNCERTAIN`, and the
  execution ends `FAILED` with `requires_reconciliation`, without calling the provider
  again. A `context.record` left `IN_FLIGHT` is computed again.
- A resume claim stops two processes from driving the same execution. SQLite stores a
  lease (owner, pid, expiry) inside `BEGIN IMMEDIATE` and takes over a dead pid at
  once. PostgreSQL holds a session advisory lock until the connection closes.
  `resume_claim_ttl` (default one hour) is how long a live SQLite process keeps the
  lease. A SQLite file from the previous slice migrates to schema version 2 on open.
- `examples/sqlite_resume.py` reopens a database, keeps an approval bound to its
  digest, and resumes. `examples/postgres_resume.py` does the same on PostgreSQL.
- `resume` recovers an execution left in `VALIDATING`. The executor is replayed to
  rebuild the output and does not call the provider again for a side effect that
  already completed. A stored `context.record`, including a `RecordedModel`
  response, is replayed and `compute` is not called. A missing, mismatched or
  unfinished record fails closed as `non_deterministic` and is not computed.
  Validators run again under `execution_limits.timeout`. The execution does not
  enter `VALIDATING` a second time. A policy denial, an unsafe journal entry, or a
  replay that is not a pure replay ends `VALIDATION_FAILED` without calling the
  provider. An `IN_FLIGHT` capability call is marked `UNCERTAIN`. No schema
  migration is required.

### Changed

- The `id42.` capability namespace is reserved. `max_actions` counts `context.record`
  calls too.

### Fixed

- An executor that runs tasks in a task group and hits an approval gate now suspends
  the execution. Before, the exception group escaped `run()` and left the execution
  `RUNNING`. A group that also carries another error fails the execution instead of
  hiding that error behind the suspension. A group of only cancellation cancels the
  execution. An uncertain side effect is reconciled before the execution can wait
  for approval.
- A `context.record` left `IN_FLIGHT` by a crash is computed again. One whose
  fingerprint changed, or that shares its action id with a capability call, fails as
  non-deterministic. One left `UNCERTAIN` is not recomputed. A record whose
  computation hits an approval gate stays `IN_FLIGHT`, so resume can consume the
  grant. A read cancelled because a sibling task hit that gate is run again; a
  side-effecting call is not.
- `RecordedModel` treats a change of model id, sampling settings, tool schema,
  thinking signature or provider payload as a different conversation. A journaled
  model response that fails validation reports the location and the reason, not the
  payload.
- `context.capability_specs` hands out copies of the granted specs, so changing a
  schema on the context does not change the registry.

### Documentation

- `docs/storage.md` describes the backends, configuration, migrations, the resume
  claim and the limits. Architecture deviations D31, D42, D43 and D48 record the
  adapters, the claim and crash recovery, including a crash during `VALIDATING`.
  D44 through D47 record `context.record` and the agent integrations. D49 records
  the MCP server and `ExecutionRepository.list`. `docs/mcp.md` includes a nanobot
  0.4.2 configuration for id42-agent.

## [0.1.0rc2] - 2026-11-14

### Fixed

- An idempotency key reserved by a call that fails before the execution exists is
  released, so the same key can be retried. A reservation owned by another execution
  is left in place.
- A transition and its event are stored together. `last_event_sequence` is the
  sequence of that event even when another event is appended at the same time.
- The validation phase is now bounded by `execution_limits.timeout`. A hanging
  validator previously kept the execution in `VALIDATING` indefinitely; it now counts
  as a failed check.
- A raising validator no longer copies the exception text into the recorded
  `ValidationResult`, which could expose input or output values. The message names the
  exception type, and the exception is logged instead.

### Changed

- Vulnerability and conduct reports go to talkid42studio@gmail.com.
- Project links point at the GitHub repository. The documentation link is the
  `docs/` tree until the MkDocs site is published.
- A version tag such as `v0.1.0rc2` publishes to PyPI with trusted publishing. A
  manual workflow run publishes only to TestPyPI and stores no token.

### Documentation

- Idempotency: a key reused after a denial returns the same `DENIED` execution, and
  the scope is shared by every actor of a tenant.
- `docs/releasing.md` describes how to configure the trusted publishers and publish.

## [0.1.0rc1] - 2026-11-07

First release candidate. Pre-alpha: in-memory storage only; see `docs/security.md`.

### Added

- Repository foundation: packaging, CI, linting, formatting, type checking and test
  configuration (milestone M0).
- Engineering specification v0.1.1 (`SPEC.md`).
- Core contracts (milestone M1): `Work`, `Policy`, `TrustedActor` and `ActorAuthority`,
  `Execution`, `Approval`, `Outcome`, `Evidence`, `WorkEvent`, `JournalEntry`,
  canonical JSON and digests, executor/validator/provider/resolver protocols, storage
  protocols, and the exception hierarchy.
- `docs/architecture.md` with the module map and deviations from the specification.
- Policy and state management (milestone M2): deterministic `PolicyEngine` (default
  deny, capability intersection, autonomy minimum, gate computation, idempotency-key
  requirement), `CapabilityRegistry`, `StaticPolicyResolver`, the execution and
  approval transition tables, and pure transition functions.
- Security test suite for capability escalation, autonomy escalation and untrusted
  identities.
- Execution runtime (milestone M3): `WorkRegistry`, `WorkRunner` (async and sync),
  restricted execution and validation contexts, action journal, ACTION/START/OUTCOME
  gates that suspend executions, output-schema and custom validators, evidence
  records, structured and redacted events with an `EventDispatcher`, idempotency with
  conflict detection, and `MemoryStorage`.
- Integration, concurrency and runtime security tests.
- Approvals and resumption (milestone M4): `ApprovalManager` with reviewer checks
  (sealed identity, grant, tenant, no self-approval, reasons), single-winner decisions,
  `APPROVAL_DECISION` evidence and lazy or swept expiry; `WorkRunner.resume` with
  policy re-evaluation and journal replay, where changed parameters invalidate the
  approval; `WorkRunner.cancel` with approval invalidation; the `cancel:<work_id>` grant;
  sync wrappers.
- Testing helpers (milestone M5): `id42_work.testing` with `FakeOdooProvider`,
  `ManualClock` and the published `StorageContractSuite`.
- Runnable examples covering every scenario of the specification, run in CI.
- User documentation (getting started, concepts, security model, roadmap) built with
  MkDocs in strict mode; governance files; PyPI trusted-publishing workflow.

### Not included

- OpenTelemetry integration (specification 12): deferred to v0.3.
- Persistent storage, crash recovery and execution leases: v0.2.

### Security

- Validation error messages no longer echo input or output values.
- The library attaches a `NullHandler` and emits no log output unless the application
  configures logging.
