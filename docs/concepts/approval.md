# Approval

An `Approval` authorizes one gate of one execution for one exact action.

- **START**: before the executor runs. `params` is the validated input.
- **ACTION**: before a side-effecting capability (or one named by a requirement) is
  invoked. `params` is the action's parameters.
- **OUTCOME**: before a verified output is accepted. `params` is the output.

The `action_digest` covers:

- the execution id;
- the work id, version and definition digest;
- the gate, action id and capability;
- the canonical parameters.

Any change produces a different digest.

## Deciding

```python
pending = await runner.approvals.list_pending(execution_id)
await runner.approvals.approve(approval_id, reviewer=finance, reason="PO matched")
await runner.approvals.reject(approval_id, reviewer=finance, reason="duplicate bill")
```

The reviewer must:

- be a `TrustedActor` issued by the runner's authority;
- belong to the execution's tenant;
- hold the gate's `reviewer_capability`;
- be different from the execution's actor.

A rejection always needs a reason; a grant needs one when the requirement says so.
Every check runs before anything is written. Concurrent decisions on one approval have
exactly one winner. Each decision is recorded as `APPROVAL_DECISION` evidence.

After a START or ACTION grant, the execution is `READY`; call
`runner.resume(execution_id, actor=...)`. An OUTCOME grant completes the execution
directly. A rejection ends it `REJECTED`.

## Expiry

Approvals expire after their TTL (24 hours by default). Expiry runs lazily whenever
an approval or its execution is touched, and on `runner.expire_due()`, which hosts may
call periodically.

- An expired **pending** approval cancels the execution (`approval_expired`).
- An expired **granted** approval that was never used is retired. The next `resume`
  requests a new one.

## Persistence

In 0.1.0, pending approvals live in memory and do not survive the process.
