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
- **Sealed identities.** A `TrustedActor` is HMAC-sealed by an `ActorAuthority`.
  The default key is random, lives only in that process, and cannot be pickled.
  A host can instead pass `key_path` so the same keys are loaded after a restart.
  Strings, dicts, modified, expired, deserialized or foreign actors are refused.
- **Exact approvals.** Approvals bind to a digest of the execution, the definition,
  the gate, the action and its canonical parameters. A changed parameter invalidates
  the approval. Reviewers must hold the gate's grant, belong to the same tenant and
  differ from the execution's actor. Decisions are compare-and-set: exactly one wins.
  `id42-work approvals` seals the operator with only that grant. It does not give
  the operator the execution's capabilities, and it does not resume as that operator.
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

## MCP clients

The optional MCP server (`docs/mcp.md`) is another untrusted caller. It does not
widen what `WorkRunner` allows.

- The client cannot pass an actor id, a grant, a constraint or an approval
  decision as a tool argument. There is no approve, reject or reconcile tool. A
  waiting execution stays waiting until the host decides through
  `ApprovalConsole` or `id42-work approvals`. That command seals a different
  actor, with only the approval's reviewer grant. An uncertain capability call
  stays uncertain until `id42-work reconcile` or `ReconciliationConsole` records
  a decision. `resume_execution` does not mark it applied.
- The default server issues one actor inside the process and checks it against
  that process's `ActorAuthority` on every call. The first accepted identity is
  pinned. A credential header is ignored, so a neighbor on `127.0.0.1` cannot
  select another actor.
- A host may pass `session_actor`. `credential_session_actor` parses
  `Id42-Actor-Credential` (`TrustedActor.to_credential`) and the server verifies
  the seal, the authority, and the lifetime. On HTTP a missing credential is
  refused. It is not replaced by the factory actor. Stdio has no header and
  stays the single fallback actor. The session id is pinned to the first
  verified identity. The credential is a bearer token. Keep the server on
  loopback.
- `host_actor()` remains the factory actor. Approval and reconcile resume use
  that actor, not the session credential.
- The server shows and changes only executions with the verified actor id and
  tenant. The page is selected in storage, so another actor's row cannot occupy
  a slot in the page. A `resume:` or `cancel:` grant does not reveal or cancel
  someone else's execution through MCP. Calling `WorkRunner` directly still
  honours those grants.
- `WorkRunner.list_executions()` with no query is not filtered. Pass an
  `ExecutionListQuery`, or filter the list, before returning it to a remote
  client. The MCP tools pass that query.
- Journal results shown over MCP pass through the default redactor. Approval
  parameter previews and action digests are left out. Events stay as the runner
  stored them: identifiers and digests, not raw inputs.

HTTP listens on `127.0.0.1` only. That address is not configurable. A client on
another machine needs stdio in the same process, or a tunnel the host operates.
The default server does not add authentication of its own: anyone who can speak
to the process can act as the host actor the factory issues. With
`credential_session_actor`, HTTP calls must present a sealed credential. That
token is still a bearer secret on the loopback interface. Stdio remains one
factory actor.

## Operator UI

`id42-work web serve` (extra `web`, [Operator UI](web.md)) is a separate process
from the MCP server. It decides only by calling `ApprovalConsole` and
`ReconciliationConsole`, so the sealed grant is still only the grant for that
decision. The page shows the same redacted fields as the CLI. It does not add
an MCP tool.

Safe defaults:

- The listener must be a loopback IP. The default is `127.0.0.1:8767`. A name
  such as `localhost`, a wildcard, and an IPv4-mapped address are refused.
- The process will not start without an operator token or `--credential-auth`.
  A token comes from `--token-file`, `ID42_WORK_WEB_TOKEN_FILE` or
  `ID42_WORK_WEB_TOKEN`. The file must be a regular file, mode `0600`, not a
  symlink. The token is stored as a SHA-256 digest and compared with
  `hmac.compare_digest`. It is not logged and not written into a page.
- `--credential-auth` accepts a `TrustedActor` credential only when this
  process's `ActorAuthority.verify` accepts it. If a token is also configured,
  the token must still match. The credential does not widen the grant: the
  console seals a new actor with only the decision grant.
- Reject and abandon show a confirmation step before the console is called.
  Approve, applied and not-applied do not.
- Every POST needs the session cookie and a CSRF token, and `Origin` or
  `Referer` must match the request host. Cookies are `HttpOnly` and
  `SameSite=Strict`. Responses set `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, `Cache-Control: no-store` and a content security policy
  with no scripts. Access logs are off.

A browser on another machine needs a tunnel the host operates. Do not put the
token in a URL.

## Authority keys

`ActorAuthority("host-app")` still generates a new in-memory key. That key dies
with the process. Pass a file when a later process must accept seals from this
one, including a reviewer credential checked by `id42-work approvals` or
`id42-work reconcile` after the MCP server has exited:

```python
from pathlib import Path
from id42_work import ActorAuthority, authority_key_path_from_env

path = authority_key_path_from_env() or Path("/var/lib/id42-work/authority.keys")
authority = ActorAuthority("host-app", key_path=path, create_key_file=True)
```

The constructor does not read the environment. `authority_key_path_from_env`
reads `ID42_WORK_AUTHORITY_KEY_PATH` and accepts only an absolute path. Create
the directory yourself, mode `0700`, before the first process. The key file is
created mode `0600`. A symlink, a file that grants access to group or other, and
a world-writable directory are refused. Writes are atomic. Nothing in the SDK
logs or prints the key bytes.

`rotate_keys()` retires the current key and signs later actors with a new one.
Seals made by the retired key still verify when the actor was issued at or
before that retirement and its lifetime is within `max_ttl`. Every process that
shares the file must use the same `max_ttl`. Do not rotate from two processes at
once if they are not on a filesystem that honors the lock file beside the keys.

`actor.to_credential()` is the seal-bearing token. It is not `model_dump`: the
model JSON omits the seal. `TrustedActor.from_credential` only parses.
`authority.verify` is still what makes the actor trusted. The token is a bearer
credential. Keep it as private as the key file.

Approval and reconciliation records are not signed again. They stay in the
execution database, which is trusted host storage. The key file lets a new
process verify the reviewer who was allowed to decide; it does not detect a
change to a row in that database.

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
- never retries a side effect automatically, whether the effect is financial or not,
  until reconciliation records that it was not applied. A read that was cancelled
  because a sibling task hit an approval gate is run again on resume; a
  side-effecting call in that situation stays `UNCERTAIN`.

Treat at-least-once delivery and uncertain outcomes as normal cases in integrations.
An operator settles one with `reconcile:<work_id>`, through `ReconciliationConsole`
or `id42-work reconcile`. The operator is not the execution's actor. The decision,
the actor and the reason are evidence. The MCP server has no reconcile tool. A
provider probe is optional and must not perform the effect.

**Determinism.** Resumption replays the executor. Executors must be deterministic
given their input and the results of `context.invoke`. A non-deterministic completed
action is detected and fails the execution.

**Persistence.** `MemoryStorage` is scoped to one process. Executions, approvals,
journals and events disappear when that process exits.

`SqliteStorage` writes the same records to a file. `PostgresStorage` writes them to
PostgreSQL. After a restart, `resume` replays completed actions. A capability call
left `IN_FLIGHT` is marked `UNCERTAIN` and is not retried. A `context.record` left
`IN_FLIGHT` during a `RUNNING` execution is computed again, because its result was
never returned. An execution left in `VALIDATING` is recovered without calling the
provider again: the output is rebuilt from the journal, a stored record is replayed,
and a new side effect or a missing record is refused. A resume claim
stops a second live process from driving the same execution: a SQLite lease row, or
a PostgreSQL session advisory lock that ends when the connection closes. There is
no distributed transaction with the external system. The database is trusted host
storage. Actor seals and authority keys are not in it. Without a key file the
host issues new actors after a restart. With `key_path`, a later process loads
the same keys and can verify a credential the host kept. See below.

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
