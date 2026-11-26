# Journal and replay

Every `context.invoke` is journaled as an entry with a status:

- `PENDING_APPROVAL`
- `IN_FLIGHT`
- `SUCCEEDED` (with the JSON result and its digest)
- `FAILED`
- `UNCERTAIN`

Each action gets a stable
`operation_id = uuid5(NAMESPACE_ID42_WORK, f"{execution_id}:{action_id}")`. It is
passed to providers, which should forward it to the external system as an idempotency
key.

## Resumption by replay

When an execution waits for an approval, nothing stays suspended in memory. On
`resume`, the executor runs again from the start:

- a `SUCCEEDED` action with the same capability and parameters returns the recorded
  result without calling the provider;
- a `SUCCEEDED` action whose parameters changed fails the execution
  (`non_deterministic`);
- a `FAILED` action raises again;
- a `PENDING_APPROVAL` action consumes a matching grant, or invalidates a grant with a
  different digest and requests a new one.

This requires deterministic executors. In exchange, waiting holds no resources, and
the same model works with persistent storage, where a suspended coroutine cannot
survive a restart.

With `SqliteStorage` or `PostgresStorage`, `resume` also recovers an execution left
`RUNNING` because the process stopped. Another live process that already holds the
resume claim is left alone. Otherwise:

- `SUCCEEDED` actions are replayed and the provider is not called;
- a capability call still `IN_FLIGHT` becomes `UNCERTAIN`, the execution ends
  `FAILED` with `requires_reconciliation`, and the provider is not called again;
- a `context.record` still `IN_FLIGHT` is computed again. Its result was never
  returned, and capability calls it already completed are replayed. If a capability
  call is also still `IN_FLIGHT`, the execution fails and the record is not
  computed again.

See [Storage](../storage.md).

## Uncertain side effects

A provider that cannot tell whether an external side effect happened must raise
`UncertainOutcomeError`. A timeout or cancellation during a side-effecting call is
treated the same way. The action becomes `UNCERTAIN` and the execution `FAILED` with
`requires_reconciliation=True`. The SDK never retries: reconcile with the external
system using the operation id.

## Recorded computations

`context.record(action_id, fingerprint=..., compute=...)` journals the result of a
non-deterministic computation that is not a capability, such as a model call. It is
replayed like a `SUCCEEDED` action: same `action_id` and an equal fingerprint return
the stored result; a different fingerprint fails the execution as
`non_deterministic`. Its entries use the reserved marker `id42.record`. See
[AI agents](../agents.md).
