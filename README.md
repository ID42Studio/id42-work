# ID42 Work SDK

**Define work. Govern execution. Verify outcomes.**

A lightweight Python SDK for reliable, governed execution of work by software systems
and AI agents.

> **Status: 0.1.0 release candidate, pre-alpha.** Storage is in memory only: nothing
> survives the process. Not production-ready and not enterprise-ready. The package is
> not yet published on PyPI. See [Project maturity](#project-maturity).

## What it does

When software or an AI agent performs real work, such as validating an invoice,
drafting an ERP entry or posting a bill, "the function returned" is not enough. The
SDK answers four questions for every unit of work:

- **Was it allowed?** Capabilities are granted by the host application, deny by
  default, and a work definition can never widen them.
- **Did a person approve this exact action?** Approvals bind to a digest of the
  action's parameters. Approving 1,250.00 does not approve 1,300.00.
- **Is the result correct?** An execution is `COMPLETED` only after its output passes
  explicit validators. *Executed* and *verified* are different states.
- **What is the evidence?** Every step leaves structured events and evidence records.

Core principle: **autonomy is a permission, not a capability.**

## What it does not do

It is not an LLM framework, an agent planner, a multi-agent orchestrator, a workflow
language or a hosted service. It needs no LLM, database, message broker or web
server. It does not authenticate users: your application does, then hands the SDK a
sealed identity.

## Installation

From a clone of the repository (Python 3.11+):

```bash
pip install .
```

or, with [uv](https://docs.astral.sh/uv/), for development:

```bash
uv sync --all-extras --dev
```

Once published, it will be installable with `pip install id42-work`.

## Minimal example

```python
import asyncio
from collections.abc import Mapping
from datetime import timedelta

from pydantic import BaseModel, JsonValue

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    MemoryStorage,
    Policy,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)


class GreetingInput(BaseModel):
    name: str


class GreetingOutput(BaseModel):
    message: str


class Directory:  # the only way the executor reaches the outside world
    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name="directory.read", side_effecting=False)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        return {"display_name": str(params["name"]).title()}


class Greet:
    async def execute(self, data: GreetingInput, context: ExecutionContext) -> GreetingOutput:
        person = await context.invoke(
            "directory.read", action_id="lookup", params={"name": data.name}
        )
        return GreetingOutput(message=f"Hello, {person['display_name']}!")


async def main() -> None:
    work = Work(
        id="greet",
        version="1.0.0",
        name="Greet",
        input_schema=GreetingInput,
        output_schema=GreetingOutput,
        required_capabilities=frozenset({"directory.read"}),
    )
    registry = WorkRegistry()
    registry.register(work, executor=Greet())
    capabilities = CapabilityRegistry()
    capabilities.add(Directory())
    policy = Policy(
        id="greeting",
        version="1.0.0",
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({"directory.read"}),
    )
    authority = ActorAuthority("my-app")
    runner = WorkRunner(
        registry=registry,
        capabilities=capabilities,
        policies=StaticPolicyResolver({("greet", "1.0.0"): policy}),
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    # Your application authenticates the caller first, then issues a sealed actor.
    actor = authority.issue(
        actor_id="user:ada", grants={"directory.read"}, ttl=timedelta(minutes=5)
    )
    execution = await runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "ada"}, actor=actor
    )
    print(execution.status, execution.raise_for_status().output)


asyncio.run(main())
```

The full version is [examples/hello_work.py](examples/hello_work.py).

## Core concepts

| Concept | In one line |
|---|---|
| **Work** | A versioned, serializable contract: schemas, required capabilities, autonomy ceiling. No code. |
| **Policy** | Host-owned rules: autonomy level, allowed capabilities, approval requirements, limits, evidence requirements. |
| **TrustedActor** | An identity sealed by your `ActorAuthority` after you authenticated it. |
| **Execution** | One attempt to complete a work, moved through an explicit state machine. |
| **Approval** | A decision bound to one execution, one gate and the exact action digest. |
| **Outcome** | The validated output, plus whether it was verified and accepted. |
| **Evidence** | References and digests that support the execution and its verification. |

Autonomy levels decide which approval gates apply by default:

| Level | Before start | Before side effects | Before accepting the outcome |
|---|---|---|---|
| `MANUAL` | yes | yes | yes |
| `SUPERVISED` | — | yes | — |
| `GUIDED` | — | — | yes |
| `AUTONOMOUS` | — | — | — |

Policies can add gates at any level, for example "every `payments.execute` needs
finance approval, even when `AUTONOMOUS`".

## Approval example

```python
execution = await runner.run(
    work_id=..., version=..., input_data=..., actor=clerk, idempotency_key="bill-INV-1001"
)
# execution.status == WAITING_APPROVAL: nothing side-effecting has happened yet
(approval,) = await runner.approvals.list_pending(execution.execution_id)
await runner.approvals.approve(approval.approval_id, reviewer=finance, reason="PO matched")
execution = await runner.resume(execution.execution_id, actor=clerk)
```

The reviewer must be a different actor, in the same tenant, holding the gate's
reviewer grant. On resume, the executor runs again from the start. Completed actions
are replayed from a journal instead of being repeated. If the action's parameters
changed, the approval is invalidated and a new one is requested. See
[examples/human_approval.py](examples/human_approval.py) and
[examples/fake_odoo_invoice.py](examples/fake_odoo_invoice.py).

## Outcome verification example

```python
class AmountIsPositive:
    validator_id = "amount_is_positive"
    required = True

    async def validate(self, output, context) -> ValidationResult:
        return ValidationResult(
            validator_id=self.validator_id,
            passed=output.amount > 0,
            severity=Severity.ERROR,
            message="amount must be positive",
        )


registry.register(work, executor=ValidateInvoice(), validators=[AmountIsPositive()])
```

The output is first re-validated against the work's output schema, then checked by
every registered validator. A validator that raises counts as a failure. If a required
validator fails, the execution ends `VALIDATION_FAILED`: it never reaches `COMPLETED`.
See [examples/invoice_validation.py](examples/invoice_validation.py).

## Architecture overview

```text
host app ──► WorkRunner.run ──► PolicyEngine (deny by default, no LLM)
                 │                    │
                 │             PolicyResolver (host)  ActorAuthority (seal check)
                 ▼
         state machine ──► ExecutionContext.invoke ──► CapabilityProvider ──► ERP / APIs
                 │               (grant, gate, journal, replay)
                 ▼
          validators ──► Outcome ──► evidence + events ──► Storage (in-memory in 0.1)
```

Details, module map and every deviation from the specification are in
[docs/architecture.md](docs/architecture.md). The security model and its limits are in
[docs/security.md](docs/security.md).

## Project maturity

- Version 0.1.0 release candidate, licensed Apache-2.0, maintained by ID42 Studio.
- In-memory storage only: no durability, no crash recovery, one process.
- Capability enforcement covers calls made through the SDK. It cannot stop arbitrary
  Python code in the same process from calling external systems directly.
- The SDK cannot guarantee exactly-once external side effects. Uncertain results are
  flagged for reconciliation and never retried.
- No OpenTelemetry integration yet (planned); subscribe to structured events instead.
- Not reviewed by an external security auditor.

## Roadmap

No dates are committed. The next steps are persistent storage, framework adapters
(LangGraph, PydanticAI), an Odoo 18 connector package, and workflow composition. See
[docs/roadmap.md](docs/roadmap.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described
in [SECURITY.md](SECURITY.md).

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run python examples/hello_work.py
```

## License

[Apache-2.0](LICENSE). Maintained by [ID42 Studio](https://github.com/ID42Studio/id42-work).
