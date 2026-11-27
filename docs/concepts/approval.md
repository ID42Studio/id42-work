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

`ApprovalConsole` is the same decision, for a host that should not hand the model
the runner. Pass `actor_id` and the console seals an operator in the execution's
tenant with only that approval's `reviewer_capability` (by default
`approve:<work_id>`). Pass `reviewer` instead when the host already sealed one.
Exactly one of the two. `resume=True` continues the execution with `resume_actor`,
which must be the host execution actor, not the operator. A resume failure leaves
the grant stored and raises `ApprovalResumeError`.

```python
console = ApprovalConsole(runner, authority=authority, resume_actor=issue_agent)
pending = await console.list_pending()
await console.approve(
    pending[0].approval.approval_id,
    actor_id="id42-operator",
    reason="PO matched",
    resume=True,
)
await console.reject(approval_id, actor_id="id42-operator", reason="duplicate bill")
```

The command uses the same factory as `id42-work mcp serve`. It requires the `mcp`
extra because that factory returns a `WorkMcpServer`. It is not an MCP tool.

```bash
id42-work approvals list --factory myapp.governed:server
id42-work approvals show APPROVAL_ID --factory myapp.governed:server
id42-work approvals approve APPROVAL_ID \
  --factory myapp.governed:server --actor id42-operator --reason "PO matched" --resume
id42-work approvals reject APPROVAL_ID \
  --factory myapp.governed:server --actor id42-operator --reason "duplicate bill"
```

`list` prints every execution that is still `WAITING_APPROVAL` or `WAITING_REVIEW`.
It is not filtered by actor. `show` prints one JSON object, including the redacted
parameter preview. `--resume` is off unless you set it. The operator is not the
actor that resumes.

The reason and the deciding actor are stored on the approval. `APPROVAL_DECISION`
evidence records both (`reason`, `reason_provided`, `decision_actor_id`), and the
approval event records the actor. The action journal does not grow a row for the
decision: a decision is not a capability call. A second decision, a reviewer
without the grant, the execution's own actor, another tenant, and an execution
that is not waiting for that gate are refused and write nothing. Two decisions at
once still have one winner.

## Expiry

Approvals expire after their TTL (24 hours by default). Expiry runs lazily whenever
an approval or its execution is touched, and on `runner.expire_due()`, which hosts may
call periodically.

- An expired **pending** approval cancels the execution (`approval_expired`).
- An expired **granted** approval that was never used is retired. The next `resume`
  requests a new one.

## Persistence

With `MemoryStorage`, pending approvals live in the process and disappear when it
exits. `SqliteStorage` keeps them, including the digest they are bound to. After a
restart the same digest is still valid; a different digest invalidates it. See
[Storage](../storage.md).
