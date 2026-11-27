# Roadmap

No dates are committed until implementation capacity is established. Everything below
is a direction, not a promise.

## v0.2: persistent execution

Done:

- `SqliteStorage` passes `id42_work.testing.contract.StorageContractSuite`. It uses
  the standard library only.
- `PostgresStorage` (`id42-work[postgres]`, psycopg 3) passes the same contract.
  CI runs the suite against PostgreSQL 18.
- Approvals, journals, events, evidence, inputs and idempotency reservations survive
  the process.
- Crash recovery through replay. `resume` of a `RUNNING` execution left by a dead
  process replays `SUCCEEDED` actions. Capability calls left `IN_FLIGHT` become
  `UNCERTAIN` and the execution ends `FAILED` with `requires_reconciliation`. A
  `context.record` left `IN_FLIGHT` is computed again.
- A resume claim is visible across processes. SQLite stores a lease (owner, pid,
  expiry) inside `BEGIN IMMEDIATE`. PostgreSQL holds a session advisory lock, which
  ends when the connection closes.
- Crash recovery from `VALIDATING`. `resume` replays the executor to rebuild the
  output, does not call the provider for a side effect that already finished, and
  runs the validators again. A stored `context.record` or `RecordedModel` response
  is replayed and is not computed again. The execution does not enter `VALIDATING`
  a second time.
- Reconciliation of uncertain capability calls. A provider may probe by operation
  id. An operator decides with `id42-work reconcile` (`applied`, `not-applied`,
  `abandon`), under `reconcile:<work_id>`. The MCP server has no reconcile tool.
  See [Reconciliation](concepts/reconciliation.md).
- Durable `ActorAuthority` keys. A host-named file (mode `0600`) reloads the
  signing key after a restart, and `rotate_keys` keeps older keys for
  verification. The execution database does not hold the key. See
  [Security](security.md).

Still open:

- Registry-level events (`work.definition.registered` / `replaced`).

## v0.3: framework integrations

- Done: PydanticAI (`id42_work.integrations.pydantic_ai`) and the framework-neutral
  layer `id42_work.agents`, with model calls journaled by `context.record`.
- Done: an MCP server (`id42-work[mcp]`, `id42_work.mcp`) that starts and inspects
  governed executions. It does not expose capabilities as tools. Policies,
  validators and approvals stay inside `WorkRunner`. See [MCP server](mcp.md).
- Done: `id42-work approvals` decides as an operator in a separate command. The
  MCP server still has no approve or reject tool. See [Approval](concepts/approval.md).
- Next: LangGraph / LangChain (tools plus a journaled chat model).
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
