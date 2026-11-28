# ID42 Work SDK

**Define work. Govern execution. Verify outcomes.**

A lightweight Python SDK for reliable, governed execution of work by software systems
and AI agents. It defines a framework-independent contract for a unit of work and
enforces it at run time:

- **Deny by default.** Capabilities come from the host application, never from the
  work definition or the executor.
- **Approvals bind to exact actions.** Change a parameter and the approval no longer
  applies.
- **Verified outcomes.** An execution completes only after its output passes explicit
  validators.
- **Evidence and events.** Every step is recorded as structured data, with sensitive
  values kept out.

Core principle: **autonomy is a permission, not a capability.**

!!! warning "Release candidate"
    Version 0.1.0 is a pre-alpha release candidate. `MemoryStorage` is in-memory.
    `SqliteStorage` and `PostgresStorage` persist executions and can resume after a
    restart, including a crash during `VALIDATING`. A resume claim stops two live
    processes from driving the same execution. Authority keys stay in memory
    unless the host passes a key file. Read [Security](security.md)
    and [Storage](storage.md) before using it near real systems.

## Where to go next

- [Getting started](getting-started.md): install the SDK and run your first work.
- Concepts: [Work](concepts/work.md), [Policy](concepts/policy.md),
  [Execution](concepts/execution.md), [Approval](concepts/approval.md),
  [Outcome](concepts/outcome.md), [Journal and replay](concepts/journal.md),
  [Reconciliation](concepts/reconciliation.md).
- [AI agents](agents.md) and the [MCP server](mcp.md) (`id42-work[mcp]`).
  `id42-work approvals` and `id42-work reconcile` decide outside that server.
  The [operator UI](web.md) (`id42-work[web]`) is the same decisions in a browser.
- [Storage](storage.md): memory, SQLite or PostgreSQL, migrations, claims, crash recovery.
- [Architecture](architecture.md): module map, runtime order, deviations from the
  specification.
- [Security](security.md): threat model, guarantees and limitations.
- [Roadmap](roadmap.md).
