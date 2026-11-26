# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). During `0.x`, minor versions may break
the public API; breaking changes are listed explicitly.

## [Unreleased]

### Added

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
  claim and the limits. Architecture deviations D31, D42 and D43 record the adapters,
  the claim and crash recovery. Recovering a crash during `VALIDATING` stays open:
  the output is not stored until that phase finishes.

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
