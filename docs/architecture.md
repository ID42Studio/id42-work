# Architecture

This document describes how the implementation maps to the engineering specification
([SPEC.md](../SPEC.md), v0.1.1) and records every deviation or refinement, as the
specification requires (section 5 and 26).

## Module map (as of milestone M2)

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

Grant format: a grant is either a capability name, `approve:<capability-or-work-id>`
or `resume:<work-id>`. No other prefixes are accepted.

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
