# Roadmap

No dates are committed until implementation capacity is established. Everything below
is a direction, not a promise.

## v0.2: persistent execution

- PostgreSQL storage that passes `id42_work.testing.contract.StorageContractSuite`.
- Durable approvals, journals and event history.
- Execution leases, replacing the per-process resume claim.
- Crash recovery through replay. Actions left `IN_FLIGHT` become `UNCERTAIN`.
- Reconciliation tooling for uncertain side effects.
- Registry-level events (`work.definition.registered` / `replaced`).

## v0.3: framework integrations

- Adapters for LangGraph, PydanticAI and other agent runtimes, wrapping agent tool
  calls as `context.invoke`.
- Better OpenTelemetry support (spans and attributes from specification 12).

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
