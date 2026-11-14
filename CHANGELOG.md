# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). During `0.x`, minor versions may break
the public API; breaking changes are listed explicitly.

## [Unreleased]

### Added

- `SqliteStorage` persists executions, journals, approvals, events, evidence, inputs
  and idempotency reservations in one SQLite file. It uses the standard library only
  and passes the storage contract. The schema is versioned and migrated on open.
- `resume` recovers a `RUNNING` execution after the process stops. Completed actions
  are replayed. An action left `IN_FLIGHT` becomes `UNCERTAIN`, and the execution
  ends `FAILED` with `requires_reconciliation`, without calling the provider again.
- `examples/sqlite_resume.py` reopens a database, keeps an approval bound to its
  digest, and resumes.

### Documentation

- `docs/storage.md` describes the backends, configuration, migrations and limits.
  Architecture deviations D42 and D43 record the SQLite adapter and crash recovery.
  PostgreSQL and execution leases stay on the roadmap.

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
