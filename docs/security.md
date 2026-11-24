# Security

This page states what the SDK guarantees, what it cannot guarantee, and what was
reviewed for the 0.1.0 release candidate. Read it before connecting the SDK to systems
that move money or data.

## Threat model

The SDK assumes:

- **Trusted:** the host application. It authenticates people and services, configures
  policies and capability providers, and holds the `ActorAuthority`.
- **Untrusted:** the work's inputs and the instructions that reach an AI agent.
- **Semi-trusted:** executor and validator code. It may be generated or
  model-driven, so it is constrained through what the SDK hands it.

The SDK defends against:

- an executor, agent or caller obtaining capabilities the host did not grant;
- a work definition raising its own autonomy or permissions;
- identities built from user input, deserialized from requests or altered after
  issue;
- approving one action and executing another;
- self-approval and cross-tenant approval;
- executing twice because a request was retried;
- completing an execution whose output was not verified;
- leaking raw inputs, outputs or credentials into events.

## Guarantees (within the SDK's public API)

- **Deny by default.** No policy, a missing capability, a resolver error or an
  untrusted actor all deny. A denial never becomes a success.
- **Capability intersection.** An execution is granted exactly the work's required
  capabilities, and only if every one of them is in the actor's grants, in the
  policy's allowed set, in the per-call constraints (when given), and offered by a
  registered provider.
- **Sealed identities.** A `TrustedActor` is HMAC-sealed by an `ActorAuthority` whose
  random key lives only in memory and cannot be pickled. Strings, dicts, modified,
  expired, deserialized or foreign actors are refused.
- **Exact approvals.** Approvals bind to a digest of the execution, the definition,
  the gate, the action and its canonical parameters. A changed parameter invalidates
  the approval. Reviewers must hold the gate's grant, belong to the same tenant and
  differ from the execution's actor. Decisions are compare-and-set: exactly one wins.
- **Violations stick.** Using an ungranted capability, a malformed or duplicate
  action id, non-canonical parameters, exceeding `max_actions` or a non-deterministic
  replay fails the execution, even if the executor catches the exception. An executor
  that swallows the suspension signal still ends waiting for approval.
- **Read-only validators.** Validators can only invoke granted capabilities that are
  not side-effecting.
- **Verified completion.** `COMPLETED` requires a verified outcome. This is enforced
  by the model itself, not only by the runner.
- **No secrets in events.** Event payloads carry identifiers and digests, never raw
  inputs, outputs, parameters or provider results. Evidence metadata and approval
  previews pass through the redactor. Validation error messages report the field and
  the reason, not the value.
- **Quiet by default.** The `id42_work` logger has a `NullHandler`: the library logs
  nothing unless the application configures logging.

## AI agents

- **Tools are only granted capabilities.** `capability_toolset` builds tools from the
  execution's granted capabilities. Asking for any other capability raises before the
  agent runs, and every tool call is checked again by `context.invoke`.
- **Approval binds to what the model asked for.** A gated tool call is approved with
  its exact parameters. If the model asks for something else on replay, the old
  approval no longer applies.
- **Prompt injection stays inside the grant.** Text the agent reads (inputs, tool
  results, documents) can steer the model, but not past the policy: the model can only
  call granted tools, and gated tools still wait for a person.
- **Not a sandbox.** An agent is only constrained if the executor gives it nothing
  else. Do not hand an agent HTTP clients, database handles or other tools that bypass
  `context.invoke`.
- **The journal holds model responses.** `RecordedModel` stores each response so it
  can be replayed. In durable storage, they are kept like any other action result.
- **`context.record` needs no grant.** By itself it reaches nothing outside the
  executor: it only stores what the executor's own code computed.

## Limitations

**In-process trust.** The SDK cannot sandbox Python. Code running in the same process
can read the authority's key from memory, call external systems directly, or bypass
the runner entirely. Capability enforcement covers calls that go through
`context.invoke` and registered providers. The real enforcement point for external
effects must also exist in the provider and in the target system's own access control
(for example, Odoo access rights, record rules and company context). The SDK adds
control; it does not replace those.

**Exactly-once is impossible.** A process can fail after an external system accepted
a request and before the SDK recorded it. The SDK therefore:

- passes a stable `operation_id` to providers (forward it as the external idempotency
  key);
- marks interrupted or ambiguous side effects `UNCERTAIN`, with
  `requires_reconciliation=True`;
- never retries automatically, whether the effect is financial or not.

Treat at-least-once delivery and uncertain outcomes as normal cases in integrations.

**Determinism.** Resumption replays the executor. Executors must be deterministic
given their input and the results of `context.invoke`. A non-deterministic completed
action is detected and fails the execution.

**Persistence.** `MemoryStorage` is scoped to one process. Executions, approvals,
journals and events disappear when that process exits.

`SqliteStorage` writes the same records to a file. `PostgresStorage` writes them to
PostgreSQL. After a restart, `resume` replays completed actions and marks an
`IN_FLIGHT` action `UNCERTAIN` instead of calling the provider again. A resume claim
stops a second live process from driving the same execution: a SQLite lease row, or
a PostgreSQL session advisory lock that ends when the connection closes. There is
no distributed transaction with the external system. The database is trusted host
storage. Actor seals are not in it; the host issues new actors after a restart.

**What the records may contain.**

- `Execution.error` stores exception messages as raised by executors and providers.
  Keep secrets out of exception messages.
- Approval `params_preview` shows the action's parameters to reviewers, so they can
  decide. Keys matching the redaction pattern are masked; amounts and identifiers are
  not.
- `Outcome.output` is stored as returned.

**Redaction is pattern-based.** The default redactor masks values whose key matches
`pass`, `secret`, `token`, `key`, `authorization`, `cookie` or `credential`. Supply a
`redactor=` to `WorkRunner` for stricter rules.

**No LLM in authorization.** The SDK never asks a model whether something is allowed.
An LLM-based validator is possible as custom code. Do not make it the only required
validator for high-risk work.

## Review for 0.1.0rc2

An internal review by the maintainers, not an external audit. Checked:

- Every rule in specification 6.3 has a dedicated test (`tests/unit/test_policy_engine.py`,
  `tests/security/`).
- All 169 execution status pairs and 36 approval status pairs are tested against the
  transition tables.
- Escalation attempts are covered by tests:
  - a work requesting extra capabilities;
  - a work raising its autonomy;
  - an approval grant used as a capability;
  - an unregistered capability;
  - forged, modified, foreign and expired actors;
  - self-approval, cross-tenant approval, approval by execution id;
  - altered actions, expired and consumed approvals;
  - side effects from validators;
  - an executor hiding violations or swallowing suspension.
- Concurrency: duplicate requests from 25 tasks and 8 threads produce one execution
  and one side effect. Concurrent approval decisions have one winner.
- Source scan: no `eval`, `exec`, `pickle` loading, `subprocess` or dynamic imports in
  the package. Log calls carry identifiers and reasons only. The SQLite codec, added
  after this review, imports the Pydantic class named on a stored record. That file
  is trusted host storage.
- Fixed during the review: validation error messages echoed input values (now
  location and reason only), and the library could print warnings without logging
  configuration (now a `NullHandler`).

Report vulnerabilities as described in `SECURITY.md` at the repository root.
