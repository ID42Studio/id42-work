# Reconciliation

A capability call left `IN_FLIGHT` or `UNCERTAIN` may already have changed an
external system. The SDK does not retry it on its own. Reconciliation is how a
host proves what happened, or closes the question.

Two paths exist. A provider may probe. An operator may decide. The MCP server
does neither.

## Provider probe

`reconcile` is not part of the `CapabilityProvider` protocol. A provider that can
look the effect up by operation id defines it anyway:

```python
async def reconcile(self, call: CapabilityCall) -> ReconciliationProbe:
    found = await self.store.get(call.operation_id)
    if found is None:
        return ReconciliationProbe(verdict=ReconciliationVerdict.NOT_APPLIED)
    return ReconciliationProbe(verdict=ReconciliationVerdict.APPLIED, result=found)
```

The method must be async and must not perform the effect. `resume` calls it when
the provider defines it.

- `APPLIED` journals `SUCCEEDED` with `result`. Replay returns that value and does
  not call `invoke`. A provider that knows the effect happened, and not the
  payload, leaves `result` null. Replay then returns JSON null.
- `NOT_APPLIED` journals `NOT_APPLIED`. Resume retries `invoke` with the same
  `operation_id`. The capability and the parameter digest must still match.
- `UNKNOWN`, a missing hook, a probe that raises, a return value that is not a
  verdict, and an `APPLIED` result that is not JSON leave the call unresolved.
  `invoke` is not called.

`resume` of a `RUNNING` execution probes `IN_FLIGHT` capability calls before it
marks anything `UNCERTAIN`. `resume` of a `FAILED` or `VALIDATION_FAILED`
execution that still has `requires_reconciliation` probes the remaining
`IN_FLIGHT` and `UNCERTAIN` capability calls. When none are left, and none was
abandoned, the execution moves to `READY` (`work.execution.reopened`) and
continues. A crash during `VALIDATING` still marks an `IN_FLIGHT` capability
call `UNCERTAIN` without probing. The next resume of that `VALIDATION_FAILED`
execution probes.

A recorded computation (`id42.record`) is not probed. It is not an external effect.

The probe records `RECONCILIATION_DECISION` evidence with `source` `provider` and
the resuming actor. It does not need the `reconcile:` grant.

## Operator decision

When a probe is missing or returns `UNKNOWN`, a person decides. The operator is
not the execution's actor. The operator holds `reconcile:<work_id>` in the
execution's tenant, and no other grant is required.

```python
console = ReconciliationConsole(runner, authority=authority, resume_actor=issue_agent)
await console.mark_applied(
    execution_id,
    action_id,
    actor_id="id42-operator",
    reason="the invoice is in the ledger",
    result={"move_id": 77},
    resume=True,
)
await console.mark_not_applied(
    execution_id, action_id, actor_id="id42-operator", reason="nothing was written"
)
await console.abandon(execution_id, action_id, actor_id="id42-operator", reason="cannot be known")
```

- **applied** journals `SUCCEEDED` with the outcome the operator supplies. JSON
  null is a supplied outcome. Resume replays it.
- **not applied** journals `NOT_APPLIED`. Resume may retry the same operation id.
  A consumed ACTION approval is not reused: a gate asks again.
- **abandon** journals `ABANDONED`, clears `requires_reconciliation`, and leaves
  the execution `FAILED` with reason `reconciliation_abandoned`. Resume does not
  reopen it.

`resume=True` continues as `resume_actor`, the host's execution actor, not as the
operator. A resume failure leaves the decision stored and raises
`ReconciliationResumeError`. Abandon has no resume.

Every accepted decision writes `RECONCILIATION_DECISION` evidence
(`action_id`, `capability`, `operation_id`, `decision`, `decision_actor_id`,
`reason`, `source`) and emits `work.action.reconciled`. The event does not carry
the reason. A second decision is refused and writes nothing. A decision beside a
live resume is refused and writes nothing. The journal compare-and-set is the
same lock on SQLite and on PostgreSQL.

The command uses the same factory as `id42-work mcp serve`. It needs the `mcp`
extra because that factory returns a `WorkMcpServer`. It is not an MCP tool.

```bash
id42-work reconcile list --factory myapp.governed:server
id42-work reconcile show EXECUTION_ID ACTION_ID --factory myapp.governed:server
id42-work reconcile applied EXECUTION_ID ACTION_ID \
  --factory myapp.governed:server --actor id42-operator \
  --reason "the invoice is in the ledger" --result '{"move_id": 77}' --resume
id42-work reconcile not-applied EXECUTION_ID ACTION_ID \
  --factory myapp.governed:server --actor id42-operator \
  --reason "nothing was written" --resume
id42-work reconcile abandon EXECUTION_ID ACTION_ID \
  --factory myapp.governed:server --actor id42-operator \
  --reason "cannot be known"
```

`list` prints capability calls that are still `UNCERTAIN` on executions that
still require reconciliation. An abandoned execution is omitted. `show` prints
one JSON object. The result appears only as `result_digest`.

The [operator UI](../web.md) (`id42-work web serve`) lists and shows the same
records and calls this console. It does not decide on its own.

`run` with the same idempotency key still returns the failed execution. Call
`resume` after the decision, or pass `--resume`.
