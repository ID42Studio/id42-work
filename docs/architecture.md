# Architecture

This document describes how the implementation maps to the engineering specification
(`SPEC.md` at the repository root, v0.1.1) and records every deviation or refinement, as the
specification requires (section 5 and 26).

## Module map (0.1.0rc2)

| Module | Contents |
|---|---|
| `id42_work.identifiers` | Formats for identifiers, capabilities, grants, action ids, versions and digests |
| `id42_work.canonical` | Canonical JSON encoding and SHA-256 digests |
| `id42_work.definitions` | `Work`, `WorkRef`, `WorkDescriptor` |
| `id42_work.policy` | `AutonomyLevel`, `ApprovalGate`, `ApprovalRequirement`, `ExecutionLimits`, `Policy`, `PolicyDecision`, `CapabilitySpec`, `CapabilityCall` |
| `id42_work.actors` | `TrustedActor`, `ActorAuthority` |
| `id42_work.states` | `ExecutionStatus`, terminal states, `TRANSITIONS` table |
| `id42_work.state_machine` | `transition_execution`, `transition_approval` (pure transitions) |
| `id42_work.policy.engine` | `PolicyEngine`, `default_gates`, `compute_gates`, `idempotency_key_required` |
| `id42_work.policy.registry` | `CapabilityRegistry` |
| `id42_work.policy.resolvers` | `StaticPolicyResolver` |
| `id42_work.error_info` | `ExecutionErrorInfo`, `ErrorCause` |
| `id42_work.registry` | `WorkRegistry`, `WorkBinding` |
| `id42_work.runner` | `WorkRunner` (public entry point): `run`, `resume`, `cancel`, `expire_due` and sync variants |
| `id42_work.approvals.manager` | `ApprovalManager`: `list_pending`, `approve`, `reject`, `expire_due` |
| `id42_work.context` | `RunContext`, `ValidatorContext` (the contexts handed to user code) |
| `id42_work._invoker` | Capability calls: checks, journal, replay, ACTION gates, provider calls (internal) |
| `id42_work._recorder` | Transitions, events, evidence, journal settlement, approval gates (internal) |
| `id42_work.outcomes.validators` | Built-in output-schema validator |
| `id42_work.events.redaction` / `.dispatcher` | `default_redactor`, `EventDispatcher` |
| `id42_work.storage.memory` | `MemoryStorage` and the in-memory repositories |
| `id42_work.storage.sqlite` | `SqliteStorage`: file-backed repositories, schema migrations, resume leases |
| `id42_work.storage.postgres` | `PostgresStorage` (optional `psycopg`): the same repositories and a session resume lock |
| `id42_work.storage.claims` | Shared checks for a resume claim (internal) |
| `id42_work.storage.codec` | JSON round-trip for records, inputs and outcome models (internal) |
| `id42_work.agents` | Framework-neutral agent tools: `agent_tools`, `tool_action_id`, `to_params`, `call_tool` |
| `id42_work.integrations.pydantic_ai` | `capability_toolset`, `capability_tools`, `RecordedModel` (extra `pydantic-ai`) |
| `id42_work.testing` | `FakeOdooProvider`, `ManualClock`; `testing.contract.StorageContractSuite` (needs pytest) |
| `id42_work.execution` | `Execution`, `ExecutionErrorInfo` |
| `id42_work.approvals` | `Approval`, `ApprovalStatus`, `compute_action_digest` |
| `id42_work.outcomes` | `Outcome`, `ValidationResult`, `Severity` |
| `id42_work.evidence` | `Evidence`, `EvidenceType` |
| `id42_work.events` | `WorkEvent`, `EventType` |
| `id42_work.journal` | `JournalEntry`, `JournalStatus`, `operation_id` |
| `id42_work.contracts` | Protocols: `Executor`, `Validator`, `ExecutionContext`, `ValidationContext`, `CapabilityProvider`, `PolicyResolver` |
| `id42_work.storage` | Storage protocols, `IdempotencyScope`, `IdempotencyRecord` |
| `id42_work.exceptions` | Exception hierarchy |

All domain models derive from one frozen base (`extra="forbid"`): records are
immutable, and unknown fields are rejected.

## Invariants enforced by the models

Some guarantees are enforced when a record is built, so no component can store an
inconsistent record:

- `Execution`: `COMPLETED` requires a verified and accepted outcome. Only `COMPLETED`
  has an accepted outcome. `FAILED` requires error details. `DENIED` requires reasons
  and holds no capabilities. `finished_at` is set exactly for terminal states.
- `Outcome`: `verified` must equal "all required validation results passed", and at
  least one required result must exist.
- `PolicyDecision`: a denial grants nothing and states a reason. An allowance names the
  policy, its version and the effective autonomy.
- `Approval`: gate, action id and capability are consistent. Decisions record who and
  when. Rejections carry a reason. `consumed_at` is set exactly when `CONSUMED`.
- `JournalEntry`: `operation_id` is derived from the execution and action ids.
  Timestamps and result digests match the status.

## Deviations and refinements

| # | Specification | Implementation | Reason |
|---|---|---|---|
| D1 | 12.1: repositories compare-and-set on the expected status | `Execution.revision` (optimistic concurrency). `ExecutionRepository.compare_and_set(updated)` succeeds only if the stored revision is `updated.revision - 1`. Approvals and journal entries still compare on status. | Two updates can share a status (e.g. replay inside `RUNNING`); a revision detects every lost update. |
| D2 | 3.4 lists "error details" | Added `Execution.policy_reasons` and `Execution.status_reason` | `DENIED`, `REJECTED` and `CANCELLED` need an explanation that is not an error. |
| D3 | 9: `ValidationResult` without a `required` flag | `ValidationResult.required` (default `True`) | Lets `Outcome` derive and enforce `verified` without knowing the validator registry. |
| D4 | 6.2: `required_gates: tuple[GateRequirement, ...]` | Reuses `ApprovalRequirement` | Identical fields; one type is simpler. |
| D5 | 8.1: `JournalEntry.result_reference` | `JournalEntry.result` (JSON value) plus `result_digest` | Replay must return the exact recorded value. Results are small JSON values, and persistent adapters store them as JSON. |
| D6 | 5.6: `invoke(...) -> Any` | Results are `pydantic.JsonValue`. Params may be any canonicalizable value (e.g. `Decimal`). | Results are journaled and replayed, so they must survive serialization unchanged. Params are only digested. |
| D7 | 12.1: storage protocols without sync or async specified | All storage protocol methods are `async` | Persistent adapters (v0.2) will be asynchronous; defining them async now avoids a breaking change. The approval manager (M4) will therefore be async, with sync wrappers like the runner. |
| D8 | 3.8: floats rejected in digests | `floats="repr"` for definition digests only | A `Work`'s JSON Schema may legitimately contain float constraints (`ge=0.5`). Payload digests (inputs, params, actions) still reject floats. |
| D9 | 3.8: no rule for `timedelta` | `timedelta` is encoded as `"<microseconds>us"` | Policies contain durations; the encoding must be exact. |
| D10 | 13: no storage subtype | `ConcurrentModificationError(StorageError)` | Compare-and-set failures need a distinct, catchable type. |
| D11 | 3.3: seal semantics | `TrustedActor.seal` is excluded from serialization. A deserialized actor is untrusted by design. | Sealed actors cannot travel through JSON; hosts issue a fresh actor per request. |
| D12 | 3.5: approval fields | Added `Approval.reason_required` | The decision rules (7.3) need it on the record itself. |
| D13 | 15: package tree | Added `identifiers.py`, `execution.py`, `_time.py`, `_model.py` | Shared formats and models needed a home that avoids import cycles. |

| D14 | 4.2: `ExecutionStateMachine.transition(...)` persists and emits an event | `transition_execution` / `transition_approval` are pure: they check the table, protected fields, time order and every model invariant, and return the next record. The runner (M3) persists it with compare-and-set and emits the event. | Pure transitions can be tested exhaustively (all 169 execution pairs, all 36 approval pairs) without storage. |
| D15 | 6.2: the decision holds gates and reasons | `PolicyDecision` also carries `idempotency_key_required` and, on `policy_error`, the `error` (`ExecutionErrorInfo`) | The runner must know whether a key is required before creating an execution (10.2), and evaluation errors must be preserved (6.3). |
| D16 | 6.3: "untrusted identity means deny" | `PolicyEngine.evaluate` verifies the actor seal itself and raises `UntrustedActorError` | One choke point: no caller can evaluate a policy for an unverified actor. |
| D17 | 16: denial reason `missing_capability:<name>` | Reasons distinguish the failing set: `missing_capability:` (actor grants), `policy_disallows_capability:`, `constraint_excludes_capability:`, `capability_unavailable:` (no provider), plus `no_policy` and `policy_error`. All missing capabilities are listed, in sorted order. | Operators need to know which side denied. |
| D18 | 3.2: sensitive capability | A capability not in the `CapabilityRegistry` counts as side-effecting | Fail closed: unknown capabilities get the strictest treatment. |

| D19 | Milestones M3/M4 | M3 ships `run()` end to end, including gates: the runner creates the pending approval and suspends. Approval decisions, `resume()` (replay) and `cancel()` arrive in M4. | Gates are never skipped. Until M4, a gated execution simply stays waiting. |
| D20 | 11: `work.definition.registered` / `replaced` events | Not emitted yet | `WorkRegistry` is synchronous and holds no storage; registry events need a registry-level event sink (planned). |
| D21 | 5.5: `runner.evidence(...)`, `runner.events(...)` | `get`, `events`, `evidence`, `journal` and `approvals_for` are `async` | Storage is asynchronous (D7). |
| D22 | 5.5: create the execution, then evaluate the policy | The policy is evaluated before the execution is created; `CREATED -> POLICY_CHECK -> READY/DENIED` then records that decision | The idempotency-key requirement depends on the decision (10.2) and must fail before any record exists. |
| D23 | 9: evidence requirements checked after validators | When an OUTCOME gate applies, `APPROVAL_DECISION` is excluded from that check | The review decision itself produces that evidence. |
| D24 | 3.8: floats rejected | Capability results may contain floats (digested with `floats="repr"`). Action params, inputs and gate digests still reject floats. | Results are replayed, not authorized; external systems commonly return floats. |
| D25 | 5.6: invoke errors fail the execution | Violations (ungranted capability, invalid or duplicate action id, non-canonical params, `max_actions`, non-deterministic replay, uncertain outcome) are recorded in the run state, so the execution ends `FAILED` even if the executor catches the exception | An executor must not be able to hide a security violation. |
| D26 | 3.4: `last_event_sequence` | Sequence number of the event emitted by the latest transition | Updating the record for every event would double the writes. |
| D27 | 11: event list | Added `work.action.failed` | A provider error that the executor handles is not an uncertain outcome. |
| D28 | 9: validator capability calls | Not journaled; the provider receives `action_id="validation:<id>"` | Validators are read-only and are not replayed. |

| D29 | 5.8: `runner.approvals.approve(...)` is synchronous | `ApprovalManager` methods are `async`, with `approve_sync`, `reject_sync` and `list_pending_sync` | Storage is asynchronous (D7). |
| D30 | 5.8: `cancel(...)` authorization not specified | `cancel` is allowed for the execution's own actor or a same-tenant actor holding the new grant prefix `cancel:<work_id>` | Operators need to cancel without being able to resume or approve. |
| D31 | 5.8: `resume` | A per-process set plus a storage claim. SQLite writes a lease row (owner, pid, expiry) inside `BEGIN IMMEDIATE`. PostgreSQL takes a session advisory lock. A second driver, in this process or another, gets the execution unchanged. A dead SQLite pid can be taken at once; a live one is kept until release or `resume_claim_ttl` (default one hour). The PostgreSQL lock ends when the connection closes. | Two live processes must not drive one execution. A crash has to leave the claim free without waiting out the TTL. |
| D32 | 7: events of a decision | START/ACTION grant: `work.approval.granted`, then the transition `WAITING_APPROVAL -> READY` (`work.execution.authorized`, reason `approval_granted`). OUTCOME grant: `granted`, `consumed`, then `WAITING_REVIEW -> COMPLETED`. Rejection: `work.approval.rejected`, then `work.execution.rejected`. | One event per transition, plus the approval's own events. |
| D33 | 5.8: whose grants count on resume | The policy is evaluated with the resuming actor's grants. The execution keeps its original `actor_id`, and the context and providers see that actor. | The resuming actor must be authorized now; the work's identity must stay stable for replay. |
| D34 | 12.1: `ApprovalRepository` | Added `list_by_status(status)` | `expire_due` must find due approvals across executions. |
| D35 | 7: OUTCOME acceptance | Before completing, the manager recomputes the outcome digest and refuses if it differs from the approval | Detects an outcome altered after review was requested. |

| D36 | 15: one `id42_work.testing` module | Split into `testing.fakes` (no test dependencies) and `testing.contract` (needs `pytest` and `pytest-asyncio`) | Fakes are useful in examples and applications without test tooling. |
| D37 | 15: examples are standalone files | The invoice examples share `examples/_invoice.py` (schemas, executor, validators, wiring) | Avoids five copies of the same setup; each example still runs with `python examples/<name>.py`. |

| D38 | 12, 14: optional OpenTelemetry integration (`id42-work[otel]`) | Not in 0.1.0. Structured events and `EventDispatcher` subscribers are the extension point; the `otel` extra was removed so that it promises nothing. | Deferred to v0.3 (roadmap) to keep the release candidate honest. |
| D39 | 3.2: `timeout` bounds the executor run; 9: a raising validator is a failed check | `timeout` also bounds the validation phase (a separate budget, from its start). A validator still running at the deadline fails its check. A raising validator's result names only the exception type; the exception is logged. | Without a bound, one hanging validator left the execution in `VALIDATING` forever. Exception text may quote input or output values. |
| D40 | 12.1: `IdempotencyRepository` is `reserve, get` | Added `release(scope, execution_id)`. It drops the reservation only when that execution still owns it. | A call that fails before the execution exists must not pin the key. A waiter never releases a reservation it does not own. An input stored for the failed attempt is left behind; nothing references it. |
| D41 | 12.2: an execution update and an event append are separate atomic operations | `Storage.commit_execution` stores the execution and appends its event, both or neither. `last_event_sequence` is the sequence that was appended. A sequence race retries; a lost revision does not. | Reading the sequence, then writing the execution, then appending the event could record a sequence the event did not get. |
| D42 | 12.3: no durable persistence in 0.1.0 | `SqliteStorage` and `PostgresStorage` persist executions, journals, approvals, events, evidence, inputs and idempotency reservations, with optimistic concurrency and a versioned schema migrated on open. PostgreSQL is the `postgres` extra (`psycopg` 3). `MemoryStorage` is unchanged. Nothing beyond Pydantic is required. | Hosts need crash recovery. SQLite stays free of extra dependencies; PostgreSQL is opt-in and passes `StorageContractSuite`. |
| D43 | 5.8 and 8.3: `resume` continues a suspended execution | `resume` also recovers a `RUNNING` execution left by a dead process. A capability call left `IN_FLIGHT` becomes `UNCERTAIN` and the execution `FAILED` with `requires_reconciliation`; the provider is not called. A `context.record` left `IN_FLIGHT` is computed again, unless a capability call is also in flight. With no in-flight capability call, the executor is replayed. A policy denial from `RUNNING` ends `FAILED` (`policy_denied_on_resume`), because the transition table has no `RUNNING -> DENIED`. | The suspended coroutine is gone. The journal is what survived. A record's result was never returned, so recomputing it does not repeat a governed side effect. `READY -> DENIED` is unchanged. Recovery of a crash during `VALIDATING` is D48. |
| D44 | 5.6: the executor context exposes `invoke` and `add_evidence` | Added `context.record(action_id, fingerprint=, compute=)` and `context.capability_specs` (granted capabilities only). `CapabilitySpec` gains an optional `params_schema`. | Agents need non-deterministic model calls journaled to stay replayable (spec 5.2 asks for it), and tools need descriptions and argument schemas. |
| D45 | 8.1: every journal entry names a granted capability | `record` entries use the reserved marker `id42.record`; providers cannot declare capabilities under `id42.` | Records are not grantable and must never collide with a real capability. |
| D46 | 5.6: `max_actions` counts `context.invoke` calls | It also counts `context.record` calls | Model calls are what makes an agent run long; the limit must bound them. |
| D47 | 5.2: executors must not catch `BaseException` | An exception group that holds only the suspension signal, or that signal plus cancellation of its siblings, suspends the execution. A group of only cancellation is cancellation. A group that also holds another error fails the execution, so that error is not discarded. `KeyboardInterrupt` and `SystemExit` propagate. An uncertain side effect is reconciled before the execution is allowed to wait for approval. | Without this, a group escaped `run()` and left the execution `RUNNING`, or a suspension hid a second failure. |
| D48 | 4.1, 5.8 and 9: a crash during `VALIDATING` | `resume` replays the executor to rebuild the output and runs the validators again under `execution_limits.timeout`. Journaled capability calls are not performed again. A stored `context.record`, including a `RecordedModel` response, is returned from the journal and `compute` is not called. A missing record, a different fingerprint, or a record with no stored result fails closed and is not computed. There is no second `VALIDATING` transition and no second `work.outcome.validation_started`. A policy denial, an `IN_FLIGHT` or `UNCERTAIN` capability call, a `PENDING_APPROVAL` action, or a replay that is not a pure replay ends `VALIDATION_FAILED`. An `IN_FLIGHT` capability call is still marked `UNCERTAIN`, and `requires_reconciliation` is stored on the error. An `IN_FLIGHT` record is not marked `UNCERTAIN`. The replay has its own timeout budget, separate from validation, as the first run does (D39). | The output is not stored until validation finishes. Validators are read-only and must be idempotent (spec 9), so running them again does not repeat the side effect. A model response is already in the journal (D44), so calling the provider again would not be a replay. The transition table has no `VALIDATING -> FAILED`, `VALIDATING -> DENIED` or `VALIDATING -> VALIDATING`. |

The M3 note on expiry is resolved: an expired PENDING approval cancels its execution
(reason `approval_expired`), as specification 7.5 requires. An expired GRANTED approval
is retired, and the next `resume` requests a new one.

Grant format: a grant is either a capability name, `approve:<capability-or-work-id>`,
`resume:<work-id>` or `cancel:<work-id>`. No other prefixes are accepted.

Schema references in `WorkDescriptor` use `module:qualname`. Classes defined inside
functions are accepted, but their references are not importable.

## Policy evaluation (M2)

`PolicyEngine.evaluate(work, actor, constraints=None)` runs these steps:

1. Verify the actor seal and its validity window. On failure, raise
   `UntrustedActorError`.
2. Resolve the policy. `None` produces `no_policy`. A resolver exception or a non-`Policy`
   value produces `policy_error`, with the error preserved.
3. For each required capability `c` (sorted), check `c ∈ actor.grants`,
   `c ∈ policy.allowed_capabilities`, `c ∈ constraints` (when given) and that a
   provider supplies `c`. Every failure becomes a reason.
4. If allowed, compute `effective_autonomy = min(policy.autonomy, work.autonomy_ceiling)`,
   set `granted_capabilities = work.required_capabilities` exactly, and compute the gates:
   - START for `MANUAL`;
   - ACTION, for each required side-effecting capability, for `MANUAL` and `SUPERVISED`;
   - OUTCOME for `MANUAL` and `GUIDED`;
   - then the policy's explicit requirements, which apply at every level and replace
     the default reviewer settings of the same gate.
5. Any unexpected exception during steps 2 to 4 produces `policy_error`.

Default gates use the reviewer grant `approve:<work_id>` and a 24-hour TTL.

## Runtime (M3)

`WorkRunner.run` follows this order:

1. Verify the actor. Resolve the binding. Validate the input (instances are
   re-validated from their data). Fingerprint it.
2. Evaluate the policy. Enforce the idempotency-key requirement. Reserve the scope.
   For an existing scope, return the existing execution (or raise
   `IdempotencyConflictError`). If this call fails before the execution exists, it
   releases the reservation it owns (D40).
3. Store the input. Create the execution (`CREATED`) together with its creation
   event. Record `INPUT_REFERENCE` evidence. Move to `POLICY_CHECK`, then `DENIED`
   or `READY`.
4. START gate: consume a matching granted approval, or request one and move to
   `WAITING_APPROVAL`.
5. `RUNNING`: execute under `asyncio.timeout`. Every `context.invoke` goes through
   `ActionInvoker`, in this order:
   - suspension check;
   - action-id format;
   - grant;
   - uniqueness;
   - `max_actions`;
   - params digest;
   - journal replay;
   - ACTION gate;
   - journal `IN_FLIGHT`;
   - provider call;
   - journal `SUCCEEDED`, `FAILED` or `UNCERTAIN`;
   - `TOOL_RESULT` evidence.
6. Precedence after the executor returns: a recorded violation means `FAILED`.
   A suspension means `WAITING_APPROVAL` only when nothing is uncertain. An
   exception means `FAILED` (reason `timeout`, `executor_error` and so on) and
   still reconciles when a side effect was in flight. An uncertain outcome with
   no exception means `FAILED` with `requires_reconciliation`, so the execution
   does not wait for approval over an uncertain side effect. Otherwise the run
   moves to `VALIDATING`.
7. Validation runs the schema validator (re-validating the output, exact type
   required), then the registered validators, all within one `timeout` budget (D39).
   A `VALIDATION_REPORT` evidence record is
   written. The run ends `VALIDATION_FAILED`, `WAITING_REVIEW` (OUTCOME gate) or
   `COMPLETED`.

Every transition is persisted together with its event (`commit_execution`, D41),
then subscribers are notified. The stream stays gapless. Other events, such as
action and approval events, are appended on their own.

Interrupted side effects: cancellation or timeout during a side-effecting provider
call marks the action `UNCERTAIN` and the execution `requires_reconciliation`. The SDK
never retries.

## Approvals and resumption (M4)

- `approve` and `reject` verify the reviewer before writing anything:
  - the reviewer is a sealed actor from the same tenant;
  - the reviewer holds the gate's `reviewer_capability`;
  - the reviewer is not the execution's actor;
  - the approval is still `PENDING` and not expired;
  - the execution is waiting on this gate;
  - a reason is present when required (always for rejections).

  Only then is the approval updated with compare-and-set (exactly one concurrent
  decision wins) and `APPROVAL_DECISION` evidence recorded.
- `resume` runs these steps:
  1. verify the actor and its right to resume;
  2. expire due approvals;
  3. require an unchanged definition digest and reload the stored input;
  4. claim the execution (another driver receives the execution unchanged);
  5. branch on the status:
     - `READY`: re-evaluate the policy and run the executor again from the start;
     - `RUNNING`: recover as in D43;
     - `VALIDATING`: recover as in D48;
     - any other status: return the execution unchanged.

  Journaled `SUCCEEDED` actions are replayed (an action that differs fails with
  `non_deterministic`). Journaled `FAILED` actions raise again. A `PENDING_APPROVAL`
  action on a `READY` resume consumes a matching grant, or invalidates a grant whose
  digest differs and requests a new one. On a `VALIDATING` resume that action is not
  performed.
- `cancel` moves `CREATED`, `READY`, `WAITING_APPROVAL` or `WAITING_REVIEW` to
  `CANCELLED` and invalidates open approvals. A running execution cannot be cancelled
  in 0.1.0.

## Persistent storage

`SqliteStorage` and `PostgresStorage` implement the same repositories as
`MemoryStorage`. Writers use one transaction, so compare-and-set and
`commit_execution` either commit in full or leave the database unchanged. The schema
version is applied on open. SQLite is at version 2 (`execution_claims`). PostgreSQL
is at version 1; its resume claim is a session advisory lock, not a row. See
[Storage](storage.md) for configuration, migrations, the claim and the recovery rules.

`resume` of a `RUNNING` execution (D43):

1. The definition digest and the stored input are checked, as for a `READY` resume.
2. The resume claim is taken (D31). If another driver holds it, the execution is
   returned unchanged.
3. If the journal has an `IN_FLIGHT` capability call, each one is marked `UNCERTAIN`
   and the execution fails with `requires_reconciliation`. The executor does not run.
   A `context.record` left `IN_FLIGHT` is not marked uncertain.
4. Otherwise the policy is evaluated again. A denial fails the execution. An
   allowance replays the executor from the start without repeating `SUCCEEDED`
   actions and without transitioning `RUNNING -> RUNNING`. An `IN_FLIGHT` record is
   computed again.

`resume` of a `VALIDATING` execution (D48):

1. The definition digest and the stored input are checked, as for a `READY` resume.
2. The resume claim is taken (D31). If another driver holds it, the execution is
   returned unchanged.
3. An `IN_FLIGHT` capability call is marked `UNCERTAIN`. An `IN_FLIGHT` or
   `UNCERTAIN` capability call, or a `PENDING_APPROVAL` entry, fails the validation
   closed. The executor does not run and the provider is not called. An `IN_FLIGHT`
   record is not marked `UNCERTAIN`.
4. Otherwise the policy is evaluated again. A denial ends `VALIDATION_FAILED`
   (`policy_denied_on_resume`).
5. The executor is replayed, under the execution timeout, only to rebuild the output.
   A capability call that is not already a completed or failed journal entry does not
   reach the provider. A stored `context.record` is returned and `compute` is not
   called. A missing record, a different fingerprint, or a record with no stored
   result ends `VALIDATION_FAILED` (`non_deterministic`) and is not computed. A
   violation, a timeout or an exception ends `VALIDATION_FAILED`.
6. A record that is still `IN_FLIGHT` after that replay fails the validation closed.
   It is not computed.
7. Validators run again under a fresh timeout budget. The phase ends
   `VALIDATION_FAILED`, `WAITING_REVIEW` or `COMPLETED`. That transition and its
   event are one commit. `work.outcome.validation_started` is not emitted again.

A reconciliation tool for uncertain side effects is still open.
