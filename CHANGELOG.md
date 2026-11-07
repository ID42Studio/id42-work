# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). During `0.x`, minor versions may break
the public API; breaking changes are listed explicitly.

## [Unreleased]

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
