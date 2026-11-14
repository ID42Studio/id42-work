# Roadmap

No dates are committed until implementation capacity is established. Everything below
is a direction, not a promise.

## v0.2: persistent execution

Done in the first slice (SQLite, no new required dependency):

- `SqliteStorage` passes `id42_work.testing.contract.StorageContractSuite`.
- Approvals, journals, events, evidence, inputs and idempotency reservations survive
  the process.
- Crash recovery through replay. `resume` of a `RUNNING` execution left by a dead
  process replays `SUCCEEDED` actions. Actions left `IN_FLIGHT` become `UNCERTAIN`
  and the execution ends `FAILED` with `requires_reconciliation`.

Still open:

- PostgreSQL storage, same contract, as an optional extra.
- Execution leases, replacing the per-process resume claim.
- Reconciliation tooling for uncertain side effects.
- Registry-level events (`work.definition.registered` / `replaced`).

## v0.3: framework integrations

- Adapters for LangGraph, PydanticAI and other agent runtimes, wrapping agent tool
  calls as `context.invoke`.
- OpenTelemetry integration (`id42-work[otel]`): the spans and attributes of specification
  12, built on the event dispatcher. Not part of 0.1.0.

## v0.4: enterprise integration

- An Odoo 18 connector as a separate package: a narrow, server-side `CapabilityProvider`
  that respects Odoo access rights, record rules and company context. Posting,
  validating and paying are distinct capabilities.
- CRM adapters, webhooks, tenant-aware policy resolvers.
- Converging ID42's internal runtime prototypes onto these contracts.

## v0.5: workflow composition

- Work dependencies, multi-step execution, compensation contracts, nested works.

## v1.0: stable contracts

Only after:

- real-world adoption and external feedback;
- API stabilization;
- an external security review;
- documented compatibility guarantees;
- validated persistent execution.
