# ID42 Work SDK
## Engineering Specification — v0.1.1

**Project:** ID42 Work SDK
**Package:** `id42-work`
**Python import:** `id42_work`
**License:** Apache-2.0
**Python:** >=3.11
**Runtime dependency:** `pydantic>=2` (only). OpenTelemetry is an optional extra (`id42-work[otel]`).
**Status:** Proposed specification (revision of v0.1.0)
**Implementation and documentation language:** English
**Target release:** 0.1.0

> This document specifies the **0.1.0 release** of the package. "v0.1.1" is the
> revision number of the *specification*, not of the package.

---

## 0. Changes from specification v0.1.0

This revision resolves contradictions and undefined behaviour found in v0.1.0. Every
item below changes the public API or the state machine, so it had to be settled before
milestone M1.

| # | v0.1.0 problem | Resolution in v0.1.1 | Section |
|---|---|---|---|
| C1 | `Work` listed `executor` and `validators` as fields, while also requiring that executable code is not serialized and that executors are registered separately. | `Work` is a pure, serializable, immutable definition. Executors and validators are attached at registration time, producing a `WorkBinding`. | 3.1, 5.3 |
| C2 | The example embedded the `Policy` inside the `Work`, contradicting "the caller must not increase its own permissions by modifying the Work definition". | Policies are resolved by a host-configured `PolicyResolver`. A `Work` only *declares* required capabilities and may *lower* (never raise) autonomy through `autonomy_ceiling`. | 3.1, 3.2, 6 |
| C3 | Autonomy levels were described in prose only; it was unclear which approvals each level triggers. | Approvals happen at three named gates (`START`, `ACTION`, `OUTCOME`). A table fixes the default gates per level. Explicit policy requirements add gates and are never removed by autonomy. | 3.2, 7 |
| C4 | `GUIDED` requires review of the final outcome, but the state machine had no transition for it. | New state `WAITING_REVIEW` between `VALIDATING` and `COMPLETED`. | 4 |
| C5 | `RUNNING -> WAITING_APPROVAL` existed, but how an executor pauses and resumes was undefined. | Side effects go through `context.invoke(...)`, recorded in an **action journal**. A pending approval suspends the execution; `resume()` re-runs the executor and replays journaled actions instead of repeating them. | 5.6, 8 |
| C6 | "Untrusted identity means deny", but no mechanism to tell a trusted actor from an arbitrary object. | `TrustedActor` instances are issued and sealed by a host-configured `ActorAuthority`; the runner verifies the seal. Limits of this mechanism are documented. | 5.4, 6.4 |
| C7 | The exception hierarchy used `ValidationError`, which clashes with `pydantic.ValidationError`, while the text asked to avoid that. | Renamed to `OutcomeError` / `OutcomeValidationError`. Added `InputValidationError`, `UntrustedActorError`, `UncertainOutcomeError`. | 13 |
| C8 | Unclear whether the runner raises exceptions or returns executions with a status. | Business outcomes are returned as execution states. Exceptions are raised only for caller errors and integrity violations. `Execution.raise_for_status()` maps states to typed exceptions. | 5.5, 13 |
| C9 | The invoice example used `float` for money while recommending `Decimal`. | All monetary examples use `Decimal`; floats are rejected in digests. | 3.8, 5.1, 16 |
| C10 | Validators had no access to trusted integrations, yet the example required "no duplicate transaction detected by the trusted integration". | `ValidationContext` exposes granted **read-only** capabilities. | 9 |
| C11 | `execution_limits`, `evidence_requirements`, approval expiry and cancellation were named but not defined. | Defined in 3.2, 7.5 and 4.3. | — |
| C12 | `tests/contract` appeared in the tree without explanation; the relation to the future Work Engine and the existing `CORE-RUNTIME` was implicit. | The SDK ships a storage contract test suite (`id42_work.testing`). Section 2.3 states that the SDK defines the contracts other ID42 runtimes implement. | 2.3, 18 |

The project owner's decisions on the remaining choices are recorded in section 25.

---

## 1. Mission

Build a lightweight, open-source Python SDK for defining, governing, executing, and
verifying units of work performed by software systems and AI agents.

The SDK provides a framework-independent contract for work execution, usable by
conventional Python applications, AI agents, agent orchestration frameworks, enterprise
systems, workflow automation platforms, and custom integrations.

The SDK must not require an LLM, a web server, a message broker, a database, or a
container runtime for basic usage.

**Core principle: Autonomy is a permission, not a capability.**

Two corollaries drive the design:

1. **Executed is not verified.** An executor returning without error does not mean the
   work is done. Only an outcome that passed its validators (and, where required, human
   review) is `COMPLETED`.
2. **Permissions come from the trusted side.** The work being executed, and the code
   executing it, can never widen what they are allowed to do.

---

## 2. Scope

### 2.1 In scope for 0.1.0

1. Typed, versioned, serializable work definitions.
2. Typed execution inputs and outputs.
3. Host-resolved execution policies.
4. Capability-based authorization enforced at controlled boundaries.
5. Explicit execution state machine.
6. Human approval gates bound to exact actions.
7. Outcome validation.
8. Evidence collection.
9. Action journal and replay-based resumption.
10. In-memory persistence behind replaceable storage protocols.
11. Structured execution events.
12. Asynchronous API and a synchronous convenience API.
13. Idempotency controls.
14. Runnable examples, automated tests, developer documentation.
15. Python package distribution.

The MVP focuses on correctness and clean interfaces rather than feature count.

### 2.2 Explicit non-goals

Do not implement: an LLM framework; autonomous agent planning; multi-agent
orchestration; a general workflow language; distributed scheduling; a web dashboard;
authentication servers; enterprise RBAC administration; a database-backed engine;
Kubernetes deployment; billing or usage-based pricing; a SaaS control plane; a full Odoo
integration module; production-grade durable execution; automatic retries.

### 2.3 Relationship to other ID42 components

The SDK is the **contract layer**. It defines the domain model, the state machine, the
policy semantics, and the storage protocols.

- The future **ID42 Work Engine** (durable, multi-tenant) must implement the SDK storage
  protocols and pass the SDK contract test suite, rather than defining a parallel model.
- The existing internal `CORE-RUNTIME` prototype (planner, governance, approvals, audit)
  overlaps in concepts. Convergence is a roadmap item (section 24), not part of 0.1.0.
  The SDK must not import from it or depend on it.

---

## 3. Domain model

Seven primary concepts: **Work**, **Policy**, **TrustedActor**, **Execution**,
**Approval**, **Outcome**, **Evidence**. All domain models are immutable (frozen Pydantic
models or frozen dataclasses). State changes produce new records through repositories;
nothing mutates a record in place.

### 3.1 Work

A `Work` is a versioned, **serializable** definition of a unit of business or technical
work. It contains no executable code.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Stable logical identifier, `^[a-z][a-z0-9_.-]{1,127}$` |
| `version` | `str` | Semantic version of the definition |
| `name` | `str` | Human-readable name |
| `description` | `str` | Work description (default `""`) |
| `input_schema` | `type[BaseModel]` | Pydantic model validating input |
| `output_schema` | `type[BaseModel]` | Pydantic model validating output |
| `required_capabilities` | `frozenset[str]` | Capabilities the work needs |
| `autonomy_ceiling` | `AutonomyLevel` | Maximum autonomy this work may ever run at (default `AUTONOMOUS`) |

Rules:

- `(id, version)` is the identity of a definition.
- The serialized form replaces schema types with their JSON Schema and fully qualified
  import path. `Work.to_dict()` / `Work.definition_digest` must be stable across
  processes.
- `definition_digest` is the SHA-256 of the canonical JSON (section 3.8) of the
  serialized form. Every execution records the digest it was started with.
- A new version must be created when the externally visible contract changes: schemas,
  required capabilities, or autonomy ceiling.
- `autonomy_ceiling` can only **lower** the effective autonomy (section 6.2). It is the
  only autonomy-related field a work author controls.
- A `Work` has **no** policy, executor, or validator fields.

### 3.2 Policy

A `Policy` defines how and under which conditions work may execute. Policies belong to
the host application (the organization), not to the work author. They reach the runner
only through a `PolicyResolver` (section 6.1).

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Policy identifier |
| `version` | `str` | Policy version |
| `autonomy` | `AutonomyLevel` | Autonomy granted by the organization |
| `allowed_capabilities` | `frozenset[str]` | Upper bound of capabilities for works under this policy |
| `approval_requirements` | `tuple[ApprovalRequirement, ...]` | Additional mandatory approval gates |
| `execution_limits` | `ExecutionLimits` | Runtime bounds |
| `evidence_requirements` | `frozenset[EvidenceType]` | Evidence that must exist before `COMPLETED` |
| `require_idempotency_key` | `bool \| None` | Override; `None` = derived (section 10) |

#### Autonomy levels

Ordered from least to most autonomous: `MANUAL < SUPERVISED < GUIDED < AUTONOMOUS`.

Approvals happen at three **gates**:

- `START`: before the executor runs for the first time.
- `ACTION`: before a *sensitive* capability is invoked (section 5.6).
- `OUTCOME`: after validators pass, before the outcome is accepted.

A capability is *sensitive* when its provider declares it `side_effecting=True` (section
5.7) or when a policy `ApprovalRequirement` names it.

Default gates per level:

| Level | `START` | `ACTION` on sensitive capabilities | `OUTCOME` |
|---|---|---|---|
| `MANUAL` | required | required | required |
| `SUPERVISED` | — | required | — |
| `GUIDED` | — | — | required |
| `AUTONOMOUS` | — | — | — |

Rules:

- **Monotonicity.** `ApprovalRequirement` entries add gates at every autonomy level. A
  higher autonomy level never removes a gate that a requirement adds. Example: a policy
  with `autonomy=AUTONOMOUS` and
  `ApprovalRequirement(gate=ACTION, capability="payments.execute")` still requires
  approval for every payment.
- Autonomy never overrides capability restrictions. An `AUTONOMOUS` execution can do
  only what its granted capabilities allow.

```python
class ApprovalRequirement(BaseModel, frozen=True):
    gate: ApprovalGate                 # START | ACTION | OUTCOME
    capability: str | None = None      # required for ACTION, must be None otherwise
    reviewer_capability: str           # capability the reviewer must hold, e.g. "approve:payments.execute"
    reason_required: bool = False
    ttl: timedelta = timedelta(hours=24)

class ExecutionLimits(BaseModel, frozen=True):
    timeout: timedelta | None = None   # wall-clock bound per executor run
    max_actions: int | None = None     # max context.invoke calls per execution (journal replays included)
```

When no requirement covers a default gate, that gate uses
`reviewer_capability = f"approve:{work.id}"` and a TTL of 24 hours.

### 3.3 TrustedActor

The identity on whose behalf work runs or approvals are decided. It is issued by the
host through an `ActorAuthority` (section 6.4), never constructed from user input.

| Field | Type | Description |
|---|---|---|
| `actor_id` | `str` | Stable identifier from the host's identity system |
| `tenant_id` | `str \| None` | Tenant scope |
| `grants` | `frozenset[str]` | Capabilities granted to this actor by the host |
| `issued_at` / `expires_at` | `datetime` | Validity window (UTC) |
| `authority_id` | `str` | Issuing authority |
| `seal` | `bytes` | Integrity seal (opaque; excluded from `repr`, events and evidence) |

### 3.4 Execution

An attempt to complete a specific `Work` definition.

| Field | Description |
|---|---|
| `execution_id` | UUID4 |
| `work_id`, `work_version`, `definition_digest` | Definition executed |
| `policy_id`, `policy_version` | Policy resolved at creation |
| `effective_autonomy` | `min(policy.autonomy, work.autonomy_ceiling)` |
| `granted_capabilities` | Frozen at authorization (section 6.2) |
| `status` | `ExecutionStatus` |
| `actor_id`, `tenant_id` | From the `TrustedActor` |
| `input_fingerprint` | SHA-256 of canonical input |
| `input_reference` | Opaque reference; raw input is held by the input store, not in events |
| `idempotency_key` | Optional, see section 10 |
| `created_at`, `updated_at`, `finished_at` | Timezone-aware UTC |
| `outcome` | `Outcome \| None` |
| `evidence_ids` | Evidence references |
| `error` | `ExecutionErrorInfo \| None` (type, message, cause chain, `requires_reconciliation`) |
| `last_event_sequence` | Highest event sequence number |

Status values: `CREATED`, `POLICY_CHECK`, `DENIED`, `READY`, `WAITING_APPROVAL`,
`RUNNING`, `FAILED`, `VALIDATING`, `VALIDATION_FAILED`, `WAITING_REVIEW`, `COMPLETED`,
`REJECTED`, `CANCELLED`.

### 3.5 Approval

An explicit authorization decision tied to one execution, one gate, and one exact
action.

| Field | Description |
|---|---|
| `approval_id` | UUID4 |
| `execution_id` | Execution it belongs to |
| `gate` | `START`, `ACTION` or `OUTCOME` |
| `action_id` | `"__start__"`, `"__outcome__"`, or the executor's action id |
| `capability` | For `ACTION` gates |
| `action_digest` | SHA-256 binding (section 7.2) |
| `params_preview` | Redacted, human-readable parameters for the reviewer |
| `reviewer_capability` | Capability the reviewer must hold |
| `status` | `PENDING`, `GRANTED`, `REJECTED`, `EXPIRED`, `INVALIDATED`, `CONSUMED` |
| `requested_at`, `expires_at`, `decided_at`, `consumed_at` | UTC |
| `decision_actor_id` | Reviewer |
| `reason` | Required when the requirement says so, and always for rejection |

### 3.6 Outcome

| Field | Description |
|---|---|
| `execution_id` | |
| `output` | Validated instance of `output_schema` |
| `verified` | `True` only when all mandatory validators passed |
| `validation_results` | `tuple[ValidationResult, ...]` |
| `evidence_ids` | |
| `produced_at` | When the executor returned |
| `accepted_at` | When the execution reached `COMPLETED` (`None` otherwise) |

The SDK distinguishes three facts and never conflates them:

- **Execution succeeded:** the executor returned without error.
- **Outcome verified:** the output passed all mandatory validators.
- **Outcome accepted:** the execution reached `COMPLETED` (verified, evidence
  requirements met, and `OUTCOME` gate approved when required).

### 3.7 Evidence

| Field | Description |
|---|---|
| `evidence_id` | UUID4 |
| `execution_id` | |
| `evidence_type` | `INPUT_REFERENCE`, `TOOL_RESULT`, `VALIDATION_REPORT`, `APPROVAL_DECISION`, `EXTERNAL_TRANSACTION`, `STRUCTURED_EVENT`, `OUTPUT_ARTIFACT` |
| `source` | Component that produced it (e.g. `runner`, `validator:invoice_rules`, `capability:odoo.invoice.post`) |
| `created_at` | UTC |
| `content_reference` | URI or opaque key; the SDK does not store large payloads |
| `content_digest` | SHA-256 of the canonical content, when content is available |
| `metadata` | Redacted mapping |

Evidence the runner records automatically: the input reference, every journaled action
result (`TOOL_RESULT`, or `EXTERNAL_TRANSACTION` when the provider returns an external
id), every validation report, and every approval decision.

### 3.8 Canonical JSON and digests

All fingerprints and digests use one canonical encoding:

- Pydantic `model_dump(mode="json")`, then `json.dumps(sort_keys=True,
  separators=(",", ":"), ensure_ascii=False)`, UTF-8, SHA-256, lowercase hex.
- `Decimal` values are encoded by their exact string representation. `Decimal("1250.00")`
  and `Decimal("1250.0")` therefore produce **different** digests. This is deliberate:
  a representation difference fails closed (idempotency conflict or approval
  invalidation) instead of being silently treated as equal.
- Datetimes must be timezone-aware and are encoded in UTC ISO 8601.
- Floats are rejected in digested payloads (`TypeError`), so money cannot be digested
  as a float.

---

## 4. Execution state machine

### 4.1 States and transitions

```text
CREATED
   |
   v
POLICY_CHECK ──────────────────────────────> DENIED
   |
   v
READY ─────────────────────────────────────> DENIED      (policy re-check on resume fails)
   |  \
   |   └──> WAITING_APPROVAL ──> REJECTED
   |              |  ^
   |              |  └────────────────────── RUNNING     (ACTION gate pending)
   |              v
   |            READY                        (approval granted)
   v
RUNNING ──────────────────────────────────> FAILED
   |
   v
VALIDATING ───────────────────────────────> VALIDATION_FAILED
   |
   ├──> WAITING_REVIEW ──> REJECTED
   |          |
   |          v
   └──────> COMPLETED
```

Explicit transition table (the implementation must use exactly this table):

| From | To | Guard |
|---|---|---|
| `CREATED` | `POLICY_CHECK` | — |
| `POLICY_CHECK` | `DENIED` | Policy decision denies |
| `POLICY_CHECK` | `READY` | Policy decision allows |
| `READY` | `WAITING_APPROVAL` | A `START` gate has no usable approval (first run, or its grant expired before resume) |
| `READY` | `RUNNING` | Policy re-check allows and all pending gates are satisfied |
| `READY` | `DENIED` | Policy re-check on resume denies |
| `WAITING_APPROVAL` | `READY` | Applicable approval `GRANTED` |
| `WAITING_APPROVAL` | `REJECTED` | Approval `REJECTED` |
| `RUNNING` | `WAITING_APPROVAL` | `context.invoke` hit an `ACTION` gate without usable approval |
| `RUNNING` | `FAILED` | Executor error, timeout, limit exceeded, uncertain side effect |
| `RUNNING` | `VALIDATING` | Executor returned |
| `VALIDATING` | `VALIDATION_FAILED` | Output schema, a mandatory validator, or evidence requirements failed |
| `VALIDATING` | `WAITING_REVIEW` | Verified and an `OUTCOME` gate applies |
| `VALIDATING` | `COMPLETED` | Verified, evidence requirements met, no `OUTCOME` gate |
| `WAITING_REVIEW` | `COMPLETED` | `OUTCOME` approval `GRANTED` |
| `WAITING_REVIEW` | `REJECTED` | `OUTCOME` approval `REJECTED` |
| `CREATED`, `READY`, `WAITING_APPROVAL`, `WAITING_REVIEW` | `CANCELLED` | Host cancellation, or a pending approval expired (section 7.5) |

Terminal states: `DENIED`, `FAILED`, `VALIDATION_FAILED`, `COMPLETED`, `REJECTED`,
`CANCELLED`. No transition leaves a terminal state. Retrying requires a new execution.

### 4.2 Invariants

- Every transition goes through `ExecutionStateMachine.transition(...)`, which checks
  the table, persists through `ExecutionRepository.compare_and_set(...)` (expected
  current status), and emits exactly one event. Any other path to change status is a
  bug.
- A forbidden transition raises `InvalidStateTransitionError(from_, to)`.
- `FAILED` is never represented as `COMPLETED`. `COMPLETED` implies
  `outcome.verified is True`.
- A policy failure is never converted into a successful outcome.

### 4.3 Cancellation

- Allowed from `CREATED`, `READY`, `WAITING_APPROVAL`, `WAITING_REVIEW`.
- `RUNNING` is not cancellable in 0.1.0. If the hosting task is cancelled
  (`asyncio.CancelledError`) while the executor runs, the execution moves to `FAILED`
  with reason `cancelled_in_flight`. `requires_reconciliation` is `True` if a
  side-effecting action was in flight. The `CancelledError` is then re-raised.
- Cancelling a waiting execution marks its pending approvals `INVALIDATED`.

---

## 5. Public API

The API prioritizes clarity and typing. The implementation may refine names, but must
record deviations in `docs/architecture.md`.

### 5.1 Define work

```python
from decimal import Decimal

from pydantic import BaseModel

from id42_work import Work


class InvoiceInput(BaseModel):
    invoice_number: str
    supplier_id: str
    currency: str
    amount: Decimal


class InvoiceOutput(BaseModel):
    invoice_number: str
    supplier_id: str
    status: str


work = Work(
    id="supplier_invoice_validation",
    version="1.0.0",
    name="Supplier Invoice Validation",
    input_schema=InvoiceInput,
    output_schema=InvoiceOutput,
    required_capabilities=frozenset({"suppliers.read", "invoices.validate"}),
)
```

This defines a contract only. It neither authorizes anything nor runs anything.

### 5.2 Implement an executor

Executors are independent of the SDK domain models and are expressed as protocols.

```python
from typing import Protocol, TypeVar

InputT = TypeVar("InputT", bound=BaseModel, contravariant=True)
OutputT = TypeVar("OutputT", bound=BaseModel, covariant=True)


class Executor(Protocol[InputT, OutputT]):
    async def execute(self, input_data: InputT, context: ExecutionContext) -> OutputT: ...
```

**Determinism contract.** Because of resumption by replay (section 8), an executor must
be deterministic given its input and the results returned by `context.invoke`. Anything
non-deterministic (I/O, clock, randomness, LLM calls) must go through `context.invoke`,
so its result is journaled and replayed. This requirement is documented prominently, and
the runner detects violations (section 8.3).

### 5.3 Register

```python
registry = WorkRegistry()
registry.register(
    work,
    executor=InvoiceExecutor(),
    validators=[InvoiceRules()],          # Validator instances, mandatory by default
)
```

- Registration creates an immutable `WorkBinding(work, executor, validators)`.
- Registering an existing `(id, version)` raises `WorkRegistrationError`, unless
  `replace=True`. Replacement emits `work.definition.replaced`. Executions that recorded
  a different `definition_digest` cannot be resumed (`WorkDefinitionError`).
- An output-schema validator (`validator_id="schema"`) is always added first and cannot
  be removed.

### 5.4 Configure the runner

```python
authority = ActorAuthority(authority_id="host-app")       # holds an in-memory sealing key

policies = StaticPolicyResolver({
    ("supplier_invoice_validation", "1.0.0"): Policy(
        id="invoice_review",
        version="1.0.0",
        autonomy=AutonomyLevel.GUIDED,
        allowed_capabilities=frozenset({"suppliers.read", "invoices.validate"}),
    ),
})

capabilities = CapabilityRegistry()
capabilities.add(FakeSupplierProvider())                  # provides "suppliers.read"

runner = WorkRunner(
    registry=registry,
    policies=policies,
    capabilities=capabilities,
    actor_authority=authority,
    storage=MemoryStorage(),                               # bundles the four repositories
)

# The host issues actors after authenticating them with its own identity system.
actor = authority.issue(
    actor_id="user:42",
    tenant_id="acme",
    grants=frozenset({"suppliers.read", "invoices.validate"}),
    ttl=timedelta(minutes=15),
)
```

### 5.5 Run and inspect

```python
execution = await runner.run(
    work_id="supplier_invoice_validation",
    version="1.0.0",
    input_data={"invoice_number": "INV-1001", "supplier_id": "SUP-001",
                "currency": "BRL", "amount": "1250.00"},
    actor=actor,
    idempotency_key="invoice-SUP-001-INV-1001",
)

execution.status                 # ExecutionStatus.WAITING_REVIEW  (GUIDED → OUTCOME gate)
execution.is_terminal            # False
execution.outcome.verified       # True
execution.outcome.output         # InvoiceOutput(...)
runner.evidence(execution.execution_id)   # tuple[Evidence, ...]
runner.events(execution.execution_id)     # tuple[WorkEvent, ...]
execution.raise_for_status()     # raises ApprovalRequiredError while waiting
```

`run()` performs, in order:

1. Verify the actor seal and validity window. On failure, raise `UntrustedActorError`.
   No execution is created.
2. Resolve the `WorkBinding`. Unknown work raises `WorkRegistrationError`.
3. Validate input against `input_schema`. On failure, raise `InputValidationError`
   (wrapping the Pydantic error as `__cause__`). No execution is created.
4. Reserve idempotency (section 10). This may return an existing execution.
5. Create the execution (`CREATED`), then evaluate policy (`POLICY_CHECK` → `READY` or
   `DENIED`).
6. Apply the `START` gate if required (`WAITING_APPROVAL`).
7. Run the executor (`RUNNING`) under limits, routing capability calls through the
   context.
8. Validate the output (`VALIDATING`) and run validators.
9. Check evidence requirements, then apply the `OUTCOME` gate if required.
10. Finalize the state and return the execution.

**Return vs raise.** `run()` and `resume()` *return* the execution for every business
result: `DENIED`, `WAITING_APPROVAL`, `FAILED`, `VALIDATION_FAILED`, `WAITING_REVIEW`,
`COMPLETED`. They *raise* only for caller errors and integrity violations:
`UntrustedActorError`, `InputValidationError`, `WorkRegistrationError`,
`IdempotencyConflictError`, `InvalidStateTransitionError`, `StorageError`.
`raise_for_status()` maps each non-`COMPLETED` state to its typed exception for callers
that prefer exceptions.

**Sync API.** `runner.run_sync(...)` and `resume_sync(...)` call `asyncio.run(...)`. If
called while an event loop is already running in the current thread, they raise
`RuntimeError` with a message pointing to the async API. They never start hidden threads
or nested loops.

### 5.6 Execution context and capability invocation

```python
class InvoiceExecutor:
    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        supplier = await context.invoke(
            "suppliers.read",
            action_id="load-supplier",
            params={"supplier_id": input_data.supplier_id},
        )
        ...
        return InvoiceOutput(invoice_number=input_data.invoice_number,
                             supplier_id=supplier["id"], status="valid")
```

`ExecutionContext` exposes only:

- `execution_id`, `work_id`, `work_version`, `tenant_id`, `actor_id` (read-only);
- `granted_capabilities: frozenset[str]`;
- `invoke(capability, *, action_id, params) -> Any`;
- `logger` (structured, with redaction);
- `add_evidence(...)` for executor-produced artifacts.

It does **not** expose the registry, repositories, approval manager, actor authority,
or other providers.

`invoke` behaviour:

1. `capability` not in `granted_capabilities` → `CapabilityDeniedError` (execution goes
   to `FAILED`, reason `capability_denied`).
2. `action_id` must be unique within one executor run. Reusing an `action_id` in the
   same run raises `ExecutionError`.
3. If the journal already holds a completed entry for `action_id`: compare digests.
   - Equal → return the recorded result **without calling the provider** (replay).
   - Different → `NonDeterministicExecutionError` (`FAILED`), section 8.3.
4. If the capability is sensitive and an `ACTION` gate applies:
   - a `GRANTED`, unexpired approval with an equal `action_digest` exists → mark it
     `CONSUMED` and continue;
   - a non-consumed approval exists with a different digest → mark it `INVALIDATED`
     (event `work.approval.invalidated`) and request a new one;
   - otherwise → create a `PENDING` approval, record a journal entry `PENDING`, and
     suspend: the runner moves `RUNNING → WAITING_APPROVAL` and `run()` returns.
5. Call the provider with an `operation_id` (section 10.3). Record the journal entry
   and evidence, then return the result.

Suspension is implemented by an internal control-flow exception that executors must not
catch. It derives from `BaseException`, the way `asyncio.CancelledError` does, so that
a broad `except Exception` in user code does not swallow it. This is documented on the
`Executor` protocol.

### 5.7 Capability providers

Capability enforcement happens at **controlled integration boundaries**: providers are
the only way an executor reaches external systems through the SDK.

```python
class CapabilitySpec(BaseModel, frozen=True):
    name: str                       # "odoo.invoice.post"
    side_effecting: bool            # True → sensitive, idempotency key required
    description: str = ""

class CapabilityProvider(Protocol):
    def capabilities(self) -> Iterable[CapabilitySpec]: ...
    async def invoke(self, capability: str, params: Mapping[str, Any],
                     call: CapabilityCall) -> Any: ...
    # CapabilityCall carries execution_id, action_id, operation_id, tenant_id, actor_id.
```

- `CapabilityRegistry.add` rejects two providers declaring the same capability.
- A work requiring a capability that no provider supplies is denied at policy check
  (`reasons=("capability_unavailable:<name>",)`).
- A provider that cannot determine whether an external side effect happened must raise
  `UncertainOutcomeError`. The execution becomes `FAILED` with
  `requires_reconciliation=True`. The SDK never retries automatically.

### 5.8 Resume, approve, cancel

```python
pending = runner.approvals.list_pending(execution_id=execution.execution_id)

runner.approvals.approve(pending[0].approval_id, reviewer=reviewer_actor, reason="ok")
# or: runner.approvals.reject(approval_id, reviewer=..., reason="amount mismatch")

execution = await runner.resume(execution.execution_id, actor=actor)
await runner.cancel(execution.execution_id, actor=actor, reason="duplicate request")
```

- Approving a `START` or `ACTION` gate moves the execution `WAITING_APPROVAL → READY`.
  The host then calls `resume()`. The SDK has no background scheduler.
- Approving an `OUTCOME` gate moves `WAITING_REVIEW → COMPLETED` directly.
- `resume()` requires a trusted actor holding the same `actor_id` and `tenant_id` as the
  execution, or the grant `resume:<work_id>`. It re-evaluates policy against the current
  policy, the current actor grants, and the recorded `definition_digest`. This catches
  grants revoked while the execution waited.

---

## 6. Policy engine

The policy engine is deterministic and must never call an LLM.

### 6.1 Policy resolution

```python
class PolicyResolver(Protocol):
    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None: ...
```

`StaticPolicyResolver` (mapping keyed by `(work_id, version)` with optional tenant
overrides) ships in 0.1.0. `None` means deny (`reasons=("no_policy",)`).

### 6.2 Decision

```python
class PolicyDecision(BaseModel, frozen=True):
    allowed: bool
    effective_autonomy: AutonomyLevel | None
    granted_capabilities: frozenset[str]
    required_gates: tuple[GateRequirement, ...]
    reasons: tuple[str, ...]
    policy_id: str | None
    policy_version: str | None
```

Let `R = work.required_capabilities`, `A = actor.grants`,
`P = policy.allowed_capabilities`, and `E = constraints` (an optional per-call set
passed by the host to `run(..., constraints=...)`; when omitted, it imposes no extra
restriction). Then:

- `allowed` is true only if `R ⊆ A ∩ P ∩ E`, every capability in `R` has a provider, and
  no rule below denies.
- `granted_capabilities = R`. The execution receives exactly what the work requires,
  never more, even when the actor holds more.
- `effective_autonomy = min(policy.autonomy, work.autonomy_ceiling)`.
- `required_gates` combines the default gates of `effective_autonomy` with the policy's
  `ApprovalRequirement`s (section 3.2).

### 6.3 Mandatory rules

- Default deny.
- Missing capability means deny. Every missing capability is listed in `reasons`.
- Unknown policy means deny.
- A policy evaluation error means deny. The exception is preserved as evidence and
  `reasons=("policy_error",)`.
- An untrusted or expired actor means deny (raised before an execution exists).
- An expired approval is never usable.
- Changed action parameters invalidate the approval.
- An execution cannot grant itself capabilities: `granted_capabilities` is frozen in
  the execution record at authorization. Executors have no API to modify it.
- The work author cannot raise autonomy: only `autonomy_ceiling` exists, and it is a
  minimum.

### 6.4 Trusted actors and their limits

- `ActorAuthority` generates a random sealing key in memory at construction
  (`secrets.token_bytes(32)`). `issue(...)` returns a `TrustedActor` whose `seal` is
  HMAC-SHA256 over its canonical fields. The key is never exposed through the public
  API, events, evidence, or `repr`.
- The runner accepts only `TrustedActor` instances whose seal verifies against its
  configured authority and whose validity window contains "now". Strings, dicts,
  re-constructed or modified actors fail with `UntrustedActorError`.

**Documented limitation:** this protects against accidental or confused-deputy misuse
through public APIs: user-supplied identifiers, actors deserialized from requests, and
actors built by agent-generated code that never received a sealed instance. It does
**not** protect against arbitrary Python code running in the same process, which can
read process memory or call external systems directly. The SDK cannot sandbox Python.
Real enforcement of external effects therefore belongs in capability providers and in
the target systems' own access control (for example, Odoo record rules).
`docs/security.md` must state this plainly.

---

## 7. Human approval

### 7.1 Model

The SDK records approval requests and decisions. It does not authenticate reviewers;
the host does, by issuing a `TrustedActor` for the reviewer through the same
`ActorAuthority`.

### 7.2 Action binding

```text
action_digest = sha256(canonical_json({
    "execution_id": ..., "work_id": ..., "work_version": ..., "definition_digest": ...,
    "gate": ..., "action_id": ..., "capability": ..., "params": ...
}))
```

- For `START`, `params` is the canonical input. For `OUTCOME`, it is the canonical
  output.
- Approving `amount=1250.00` therefore does not authorize `amount=1300.00`. When the
  replayed action differs, the old approval is `INVALIDATED` and a new one is requested.

### 7.3 Decision rules

`approve()` and `reject()` require all of:

- `reviewer` is a valid `TrustedActor` from the runner's authority;
- `reviewer.grants` contains the gate's `reviewer_capability`;
- `reviewer.actor_id != execution.actor_id` (no self-approval, not configurable in
  0.1.0);
- `reviewer.tenant_id == execution.tenant_id`;
- the approval is `PENDING` and not expired;
- `reason` is provided when the requirement demands it, and always on rejection.

Violations raise `ApprovalError` subclasses and record nothing. There is no API that
approves by execution id alone. Executors cannot reach the approval manager (section
5.6).

### 7.4 Approval state changes

`PENDING → GRANTED | REJECTED | EXPIRED | INVALIDATED`, and `GRANTED → CONSUMED |
EXPIRED | INVALIDATED`. Every change is an atomic compare-and-set on the approval
repository and emits an event. Each decision is also recorded as `APPROVAL_DECISION`
evidence.

### 7.5 Expiry

Expiry is evaluated lazily, on any operation that touches the approval (`list_pending`,
decide, `resume`, `invoke`), and by `runner.expire_due(now=None)`, which the host may
call periodically. The SDK never starts a timer.

- A `PENDING` approval that expires moves to `EXPIRED`. Its execution moves to
  `CANCELLED` with reason `approval_expired`.
- A `GRANTED` approval that expires before it is consumed moves to `EXPIRED`. At the
  next `resume`, the gate is unsatisfied again and a fresh approval is requested.

### 7.6 MVP limitation

In-memory approvals live in one Python process and do not survive termination.
Durability requires a persistent storage adapter (roadmap v0.2).

---

## 8. Action journal and resumption

### 8.1 Journal

Every `context.invoke` produces a `JournalEntry(execution_id, action_id, capability,
params_digest, status, result_reference, result_digest, operation_id, started_at,
finished_at)`. Status is one of `PENDING_APPROVAL`, `IN_FLIGHT`, `SUCCEEDED`, `FAILED`,
or `UNCERTAIN`. The journal is stored through `ExecutionRepository`.

### 8.2 Resumption by replay

`resume()` runs the executor again from the start with the same validated input.
Already `SUCCEEDED` actions return their recorded results without calling the provider.
Execution continues past the point where it suspended.

**Trade-off (documented):** replay requires deterministic executors (section 5.2), which
is a real constraint for authors. It was chosen over keeping the suspended coroutine
alive because:

- (a) it does not hold resources while a human decides;
- (b) the same model works unchanged with persistent storage in v0.2, where a
  suspended coroutine cannot survive a restart;
- (c) it is how changed parameters are detected.

### 8.3 Non-determinism detection

- A replayed action with a different `params_digest` raises
  `NonDeterministicExecutionError`, and the execution moves to `FAILED`. This is
  different from an `ACTION` approval invalidation: invalidation applies only to actions
  that never completed.
- A journal entry left `IN_FLIGHT` when resume starts (the process failed mid-call)
  moves to `UNCERTAIN`. The execution moves to `FAILED` with
  `requires_reconciliation=True`. It is never retried automatically.

---

## 9. Outcome validation

```python
class Validator(Protocol):
    validator_id: str
    required: bool          # default True; False = advisory

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult: ...


class ValidationResult(BaseModel, frozen=True):
    validator_id: str
    passed: bool
    severity: Severity      # ERROR | WARNING | INFO
    message: str
    evidence_ids: tuple[UUID, ...] = ()
```

- `ValidationContext` exposes the validated input, the output, work and execution
  identifiers, and `invoke()` restricted to granted capabilities whose spec has
  `side_effecting=False`. Validators cannot cause side effects through the SDK.
- Order: built-in `schema` validator, then registered validators in registration order.
  All validators run, even after a failure, so the report is complete.
- A validator that raises counts as `passed=False, severity=ERROR`. The exception is
  kept as the cause in the validation report. Fail closed.
- The outcome is `verified` only if every `required=True` validator passed.
- Each run produces one `VALIDATION_REPORT` evidence record.
- After validators, evidence requirements are checked. Missing evidence types lead to
  `VALIDATION_FAILED` with reason `evidence_missing:<type>`.
- LLM-based validators are allowed as custom validators. The core neither ships nor
  requires one, and the documentation recommends against using one as the sole required
  validator for high-risk work.

---

## 10. Idempotency

### 10.1 Scope and behaviour

The idempotency scope is `(tenant_id, work_id, work_version, idempotency_key)`.

- Same scope and same `input_fingerprint`: return the existing execution as it is, in
  any state. It is never run again. A waiting execution is resumed only through an
  explicit `resume()`.
- Same scope and a different fingerprint: raise `IdempotencyConflictError`.
- Reservation is atomic: `IdempotencyRepository.reserve(scope, fingerprint,
  execution_id) -> Reservation | ExistingExecution`.

### 10.2 When a key is required

A key is required, and `run()` raises `InputValidationError` without one, when any
capability in `R` is `side_effecting`, unless the policy sets
`require_idempotency_key=False` explicitly. Read-only works may omit it. Without a key,
there is no deduplication.

### 10.3 Propagation to integrations

Each action gets `operation_id = uuid5(NAMESPACE_ID42_WORK,
f"{execution_id}:{action_id}")`. It is stable across replays and passed to providers.
Providers must forward it to external systems as their idempotency key when the system
supports one.

### 10.4 Limits (documented)

Exactly-once execution of arbitrary external side effects cannot be guaranteed. A
process may fail after the external system accepted a request and before the journal
recorded it. The SDK therefore:

- propagates stable operation ids;
- marks interrupted actions `UNCERTAIN` and requires reconciliation;
- never retries automatically, financial or not;
- documents the at-least-once and uncertain-execution scenarios in
  `docs/security.md`.

---

## 11. Events

Every state transition and every approval or journal change emits one `WorkEvent`.

| Field | Description |
|---|---|
| `event_id` | UUID4 |
| `event_type` | See below |
| `schema_version` | `"1"` |
| `execution_id` | |
| `sequence_number` | Gapless, monotonically increasing per execution, starting at 1 |
| `timestamp` | UTC |
| `actor_reference` | `actor_id` only (never grants, seal, or tokens) |
| `payload` | Redacted mapping |

Event types:

```text
work.definition.registered       work.definition.replaced
work.execution.created           work.policy.evaluated
work.execution.denied            work.execution.authorized
work.approval.requested          work.approval.granted
work.approval.rejected           work.approval.expired
work.approval.invalidated        work.approval.consumed
work.execution.started           work.execution.resumed
work.action.invoked              work.action.replayed
work.action.completed            work.action.uncertain
work.execution.suspended         work.execution.failed
work.outcome.validation_started  work.outcome.validation_passed
work.outcome.validation_failed   work.outcome.review_requested
work.execution.completed         work.execution.rejected
work.execution.cancelled
```

(`work.definition.*` events are registry-level and use the `execution_id=None` stream.)

**Redaction.** Payloads never include raw input, raw output, action params, provider
results, credentials, tokens, or prompts. They carry digests and references. Hosts may
add a `Redactor` (callable mapping → mapping) applied to logs, event payloads, evidence
metadata and `params_preview`. The default redactor masks keys matching
`(?i)(pass|secret|token|key|authorization|cookie|credential)`.

`EventDispatcher` delivers events synchronously to registered subscribers *after* they
are persisted. A subscriber exception is logged and never affects the execution.

---

## 12. Persistence

### 12.1 Protocols

```text
ExecutionRepository    create, get, compare_and_set(status), append_journal, update_journal
ApprovalRepository     create, get, list_pending, compare_and_set(status)
EventRepository        append (assigns sequence atomically), list(execution_id)
IdempotencyRepository  reserve, get
EvidenceRepository     add, list(execution_id)
InputStore             put(execution_id, input) -> reference, get(reference)
```

`MemoryStorage` in `id42_work.storage.memory` bundles in-memory implementations of all
of them.

### 12.2 Concurrency model (documented)

- Scope: one Python process. A `MemoryStorage` instance must not be shared across
  processes.
- Each repository guards its state with one `threading.Lock`. Critical sections contain
  no `await`, so the same instance is safe from one asyncio event loop and from multiple
  threads each running its own loop.
- Execution creation, idempotency reservation, approval status changes, execution
  status changes and event appends are each one atomic operation (compare-and-set). No
  read-then-write.
- Concurrent `run()` calls with the same idempotency scope produce one execution. Every
  caller receives that execution. Exactly one caller drives it; the others get the
  snapshot at the time of their call.

### 12.3 Limitation

No claim of crash recovery, distributed consistency, or durable persistence is made for
0.1.0.

---

## 13. Exception model

```text
WorkError
├── WorkDefinitionError
├── WorkRegistrationError
├── InputValidationError            # wraps pydantic.ValidationError as __cause__
├── PolicyError
│   ├── PolicyDeniedError
│   ├── CapabilityDeniedError
│   └── UntrustedActorError
├── ApprovalError
│   ├── ApprovalRequiredError
│   ├── ApprovalRejectedError
│   ├── ApprovalExpiredError
│   └── ApprovalNotAllowedError     # reviewer lacks grant, self-approval, wrong tenant
├── ExecutionError
│   ├── ExecutionFailedError
│   ├── ExecutionCancelledError
│   ├── InvalidStateTransitionError
│   ├── IdempotencyConflictError
│   ├── NonDeterministicExecutionError
│   └── UncertainOutcomeError
├── OutcomeError
│   └── OutcomeValidationError
└── StorageError
```

- No SDK exception is named `ValidationError`.
- Wrapping always uses `raise ... from original`. Generic catch-and-convert that drops
  the cause is forbidden.
- `Execution.raise_for_status()` mapping: `DENIED` → `PolicyDeniedError`;
  `WAITING_APPROVAL` / `WAITING_REVIEW` → `ApprovalRequiredError`; `REJECTED` →
  `ApprovalRejectedError`; `FAILED` → `ExecutionFailedError` (or the recorded specific
  subclass); `VALIDATION_FAILED` → `OutcomeValidationError`; `CANCELLED` →
  `ExecutionCancelledError`; `COMPLETED` → returns the outcome.

---

## 14. Observability

- Structured logging through `logging.getLogger("id42_work")`, with extra fields
  `work.id`, `work.version`, `execution.id`, `execution.status`, and redaction applied.
- Optional OpenTelemetry (`id42_work.integrations.opentelemetry`, extra `otel`) with
  spans `work.execute`, `work.policy.evaluate`, `work.approval.check`,
  `work.executor.run`, `work.action.invoke`, `work.outcome.validate`, and attributes
  `work.id`, `work.version`, `execution.id`, `execution.status`, `policy.id`,
  `policy.decision`, `outcome.verified`.
- The SDK must import and run correctly without OpenTelemetry installed. Sensitive
  inputs are never telemetry attributes.

---

## 15. Package structure

```text
id42-work/
├── .github/workflows/{ci.yml, publish.yml}
├── .github/ISSUE_TEMPLATE/, pull_request_template.md
├── docs/
│   ├── index.md, getting-started.md, architecture.md, security.md, roadmap.md
│   └── concepts/{work, policy, execution, approval, outcome, journal}.md
├── examples/
│   ├── hello_work.py
│   ├── invoice_validation.py
│   ├── human_approval.py
│   ├── async_execution.py
│   └── fake_odoo_invoice.py
├── src/id42_work/
│   ├── __init__.py            # public API re-exports only
│   ├── definitions.py         # Work, WorkBinding
│   ├── actors.py              # TrustedActor, ActorAuthority
│   ├── context.py             # ExecutionContext, ValidationContext
│   ├── registry.py
│   ├── runner.py              # WorkRunner
│   ├── states.py              # ExecutionStatus, transition table, state machine
│   ├── journal.py
│   ├── canonical.py           # canonical JSON, digests
│   ├── exceptions.py
│   ├── policy/{models, engine, resolvers, capabilities}.py
│   ├── approvals/{models, manager}.py
│   ├── outcomes/{models, validators}.py
│   ├── evidence/models.py
│   ├── events/{models, dispatcher, redaction}.py
│   ├── storage/{protocols, memory}.py
│   ├── testing/               # storage contract test suite + fakes for users
│   └── integrations/opentelemetry.py
├── tests/{unit, integration, security, concurrency, contract}/
├── pyproject.toml, README.md, LICENSE, CONTRIBUTING.md, CODE_OF_CONDUCT.md,
│   SECURITY.md, CHANGELOG.md, mkdocs.yml
```

Create files only when they have content. Do not generate empty scaffolding to match
this tree.

---

## 16. Reference example

The scenario is deterministic and uses no AI: **validate a supplier invoice before
preparing an ERP operation.**

```json
{"invoice_number": "INV-1001", "supplier_id": "SUP-001", "currency": "BRL", "amount": "1250.00"}
```

`amount` is a JSON string, parsed into `Decimal`. Fake supplier records and synthetic
data are used throughout. No real ERP credentials are needed.

Each scenario must be runnable and print the final status, the event types, and the
evidence types:

| Scenario | Expected terminal or waiting state |
|---|---|
| Successful execution (`AUTONOMOUS`) | `COMPLETED` |
| Successful execution (`GUIDED`) | `WAITING_REVIEW` → approve → `COMPLETED` |
| No policy for the work | `DENIED` (`no_policy`) |
| Actor lacks a capability | `DENIED` (`missing_capability:invoices.validate`) |
| Pending approval (`SUPERVISED`, `odoo.invoice.prepare` side-effecting) | `WAITING_APPROVAL` |
| Approval rejected | `REJECTED` |
| Approved amount 1250.00, replay with 1300.00 | old approval `INVALIDATED`, new one `PENDING` |
| Self-approval attempt | `ApprovalNotAllowedError`, state unchanged |
| Invalid outcome (negative amount slips through executor) | `VALIDATION_FAILED` |
| Idempotent retry | same `execution_id`, provider called once |
| Conflicting idempotency key | `IdempotencyConflictError` |
| Executor exception | `FAILED`, cause preserved |
| Provider raises `UncertainOutcomeError` | `FAILED`, `requires_reconciliation=True` |

---

## 17. Odoo integration direction

Do not create a full Odoo connector in the core package. The future connector
(separate package, Odoo 18 first) will be a `CapabilityProvider` exposing narrow,
explicit capabilities:

```text
odoo.partner.read        side_effecting=False
odoo.invoice.read        side_effecting=False
odoo.invoice.prepare     side_effecting=True
odoo.invoice.validate    side_effecting=True
odoo.invoice.post        side_effecting=True
```

- Reading never implies posting or paying. Each is a separate capability, grant and
  approval.
- Prefer a narrow server-side adapter (explicit methods, Odoo access rights, record
  rules, company context) over exposing generic model execution to agents.
- The SDK adds control on top of Odoo's security model. It never replaces it.
- `operation_id` is forwarded so the Odoo side can deduplicate.

0.1.0 ships only `FakeOdooProvider` (in examples and in `id42_work.testing`).

---

## 18. Testing strategy

pytest, with `pytest-asyncio` (or `anyio`).

- **Unit:** registration and digests, canonical JSON (Decimal and float rules), input
  and output schema validation, policy decisions (each rule in 6.3), capability
  intersection, gate computation per autonomy level and with requirements, every
  allowed transition, approval lifecycle, expiry, journal replay, validators
  (raising, advisory, ordering), evidence metadata, idempotency, error classification
  and cause preservation, `raise_for_status` mapping.
- **Integration:** every scenario in section 16 end to end with `MemoryStorage`.
- **Security:** execute without authorization; escalate capabilities through the
  `Work` (adding to `required_capabilities` or `autonomy_ceiling`); invoke an ungranted
  capability from an executor; approve an altered action; reuse expired or consumed
  approvals; self-approval; cross-tenant approval; forged, modified or expired actors;
  plain-string actors; reuse idempotency keys with different input; leave terminal
  states; side effects from validators; executor swallowing the suspension signal with
  `except Exception`; redaction of secrets in events, logs and evidence.
- **Concurrency:** N concurrent `run()` calls with the same scope create one execution
  and call the provider once, from both asyncio tasks and threads. Concurrent `approve()`
  and `reject()` on one approval: exactly one wins.
- **Contract:** `id42_work.testing.StorageContractSuite` runs against `MemoryStorage` in
  CI and is published for third-party storage adapters.

Targets:

- ≥ 90% line coverage of `src/id42_work` (excluding `integrations/`).
- 100% branch coverage of the policy engine and of approval decision rules.
- Every forbidden transition is tested (generated from the table).
- Every example runs in CI.

Coverage is an indicator, not a substitute for meaningful tests.

---

## 19. Developer experience

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright            # strict mode on src/
uv run python examples/hello_work.py
```

Standard `pip install` from a built wheel must work. Do not claim the package is
available on PyPI until publication is complete and verified.

---

## 20. Documentation

README headline:

> # ID42 Work SDK
> **Define work. Govern execution. Verify outcomes.**
> A lightweight Python SDK for reliable, governed execution of work by software systems
> and AI agents.

The README must cover, in this order: what it does; what it does not do; installation;
a minimal example; core concepts; an approval example; an outcome verification example;
an architecture overview; project maturity; roadmap; contributing; license.

`docs/security.md` must cover at least:

- the in-process trust limitation (6.4);
- enforcement only at capability boundaries;
- idempotency and uncertain outcomes (10.4);
- the determinism requirement and replay (8.2);
- in-memory-only persistence;
- the redaction defaults.

Do not describe 0.1.0 as enterprise-ready or production-grade.

---

## 21. Open-source governance

Apache-2.0 unless the owner decides otherwise. Include LICENSE, CONTRIBUTING.md,
CODE_OF_CONDUCT.md, SECURITY.md (private vulnerability reporting), CHANGELOG.md
(Keep a Changelog), issue templates, a PR template, and a release workflow.

- Semantic Versioning. During `0.x`, minor versions may break the API; breaking changes
  are listed in the CHANGELOG.
- Conventional commits.
- Passing CI is required for merge.
- PyPI trusted publishing (OIDC). No credentials in the repository.
- Governance: a project maintained by ID42 Studio, accepting external contributions.
  Not presented as a foundation or independent association.

---

## 22. Milestones

| Milestone | Deliverables | Acceptance |
|---|---|---|
| **M0 — Foundation** | Project, packaging, CI (lint, format, pyright, tests, build), license, initial README | Clean install; wheel builds; CI green |
| **M1 — Contracts** | `Work`, `Policy`, `TrustedActor`/`ActorAuthority`, `Execution`, `Approval`, `Outcome`, `Evidence`, canonical JSON, protocols, exceptions | Serialization and digest stability tests; versioning tests |
| **M2 — Policy and states** | Policy engine and resolvers, capability registry, gate computation, state machine | Default deny verified; every forbidden transition rejected; capability escalation tests pass |
| **M3 — Runtime** | Registry, runner, context, journal, in-memory storage, events, idempotency, sync API | Deterministic work completes; concurrent duplicates handled; failures recorded with causes |
| **M4 — Approval and verification** | Approval manager, gates, expiry, replay with invalidation, validators, evidence requirements, `WAITING_REVIEW` | Approval cannot be bypassed; changed action invalidates approval; invalid outcomes never reach `COMPLETED` |
| **M5 — Examples and release** | All section 16 scenarios, fake Odoo provider, docs, contract suite, security review, 0.1.0 RC | Examples run in CI; docs match behaviour; known limitations published |

---

## 23. Acceptance criteria for 0.1.0

- [ ] Work definitions are typed, versioned, serializable, digest-stable, and free of executable code.
- [ ] Executors and validators are attached at registration, independent of domain models.
- [ ] Policies come only from the host's `PolicyResolver`; a work cannot raise its own permissions or autonomy.
- [ ] Policies are deterministic and fail closed on every rule in 6.3.
- [ ] Effective capabilities equal `R`, and are allowed only when `R ⊆ A ∩ P ∩ E`.
- [ ] Capability checks are enforced in `ExecutionContext.invoke` and `ValidationContext.invoke`.
- [ ] Only sealed `TrustedActor`s from the configured authority are accepted.
- [ ] The state machine matches the table in 4.1; every forbidden transition raises.
- [ ] Gates per autonomy level match 3.2; requirements are monotonic.
- [ ] Approvals bind to action digests; changed parameters invalidate them; self-approval and cross-tenant approval are refused.
- [ ] Suspended executions resume by replay without repeating journaled actions.
- [ ] Interrupted actions become `UNCERTAIN`, are never retried, and require reconciliation.
- [ ] Failed validation, missing evidence or rejected review cannot produce `COMPLETED`.
- [ ] Events are structured, gapless per execution, and redacted.
- [ ] Evidence is recorded for inputs, actions, validations and approvals.
- [ ] Idempotency conflicts are detected; concurrent duplicates create one execution.
- [ ] Async API works; sync API refuses to run inside an active event loop.
- [ ] No LLM provider and no database are required.
- [ ] The fake Odoo example works.
- [ ] Documentation is in English and states every limitation listed in section 20.
- [ ] Tests pass; coverage targets met; package builds; release artifacts are ready.

---

## 24. Roadmap (no dates)

- **v0.2 — Persistent execution:** PostgreSQL storage passing the contract suite,
  durable approvals and journal, crash recovery via replay, reconciliation tooling,
  execution leases.
- **v0.3 — Framework integrations:** LangGraph, PydanticAI, external agent runtimes
  (wrapping agent tool calls as `context.invoke`), OpenTelemetry improvements.
- **v0.4 — Enterprise integration:** Odoo 18 connector package, CRM adapters, webhooks,
  tenant-aware policy resolvers. Evaluate converging `CORE-RUNTIME` governance onto
  SDK contracts.
- **v0.5 — Workflow composition:** work dependencies, multi-step execution,
  compensation contracts, nested works.
- **v1.0 — Stable contracts:** only after real-world adoption, external feedback, API
  stabilization, an external security review, documented compatibility guarantees, and
  validated persistent execution.

---

## 25. Owner decisions (resolved 2026-11-06)

1. **Resumption model:** replay with an action journal (section 8.2). Executors must be
   deterministic; keeping suspended coroutines in memory was rejected because it does
   not carry over to persistent execution.
2. **Self-approval:** forbidden with no override in 0.1.0. A configurable two-person
   rule may be revisited after real-world feedback.
3. **Idempotency key requirement:** derived from `side_effecting` capabilities, with a
   per-policy override (section 10.2).
4. **PyPI name:** `id42-work` was unregistered on PyPI on 2026-11-06. It is not
   reserved; register it with the first trusted-publishing release.
5. **License:** Apache-2.0.

---

## 26. Final engineering instructions

Implement incrementally, starting with deterministic execution and strong contracts.
Do not build an agent framework, and do not add infrastructure the first release does
not need. Prioritize reliability, security, testability and developer experience. Use
type annotations everywhere (pyright strict on `src/`). Every public API has
documentation and tests. Do not invent production capabilities. Do not relax security
requirements to simplify examples. If a design decision conflicts with this
specification, record the trade-off in `docs/architecture.md` before implementing it.

**ID42 Work SDK — Define work. Govern execution. Verify outcomes.**
