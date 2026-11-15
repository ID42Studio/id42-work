# Getting started

## Install

The package is not yet published on PyPI. Install it from a clone (Python 3.11+):

```bash
pip install .
```

For development, use [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-extras --dev
uv run pytest
```

## Run the examples

```bash
uv run python examples/hello_work.py          # smallest complete flow
uv run python examples/invoice_validation.py  # denials, validation failures, idempotency
uv run python examples/human_approval.py      # approvals, rejection, changed parameters
uv run python examples/async_execution.py     # concurrency and the sync API
uv run python examples/fake_odoo_invoice.py   # gated ERP posting, uncertain outcomes
uv run python examples/sqlite_resume.py       # the same approval, reopened from SQLite
ID42_POSTGRES_DSN=postgresql://id42:id42@localhost:5432/id42 \
  uv run python examples/postgres_resume.py     # the same flow on PostgreSQL
```

Each example checks its own expectations and prints `OK`. They use synthetic data:
no credentials are needed. `sqlite_resume.py` writes a temporary SQLite file.
`postgres_resume.py` needs the `postgres` extra and a server that can create a
database. See [Storage](storage.md) to choose a backend.

## The five pieces you wire

1. **A `Work`**: the contract (id, version, input and output schemas, required
   capabilities). It contains no code.
2. **An executor**: any class with `async def execute(self, input_data, context)`. It
   reaches external systems only through `context.invoke(capability, action_id=...,
   params=...)`.
3. **Capability providers**: your adapters to external systems, registered in a
   `CapabilityRegistry`. Each declares whether a capability is side-effecting.
4. **A `PolicyResolver`**: the host's rules. `StaticPolicyResolver` maps
   `(work_id, version)` to a `Policy`, with optional per-tenant overrides.
5. **An `ActorAuthority`**: after your application authenticates a user or service,
   it calls `authority.issue(...)` to get a sealed `TrustedActor`.

Then:

```python
runner = WorkRunner(
    registry=registry,
    policies=resolver,
    capabilities=capabilities,
    actor_authority=authority,
    storage=MemoryStorage(),  # or SqliteStorage("work.sqlite")
)
execution = await runner.run(
    work_id="greet", version="1.0.0", input_data={"name": "ada"}, actor=actor
)
outcome = execution.raise_for_status()  # raises the typed error unless COMPLETED
```

## Writing executors

- Route every external call through `context.invoke`. Each call needs an `action_id`
  that is unique in the run and stable across runs.
- Keep executors deterministic given their input and the results of
  `context.invoke`. When an execution resumes after an approval, the executor runs
  again from the start and the SDK replays completed actions from its journal.
- Use `Decimal` for money. Floats are rejected wherever a value is digested (inputs,
  action parameters, approvals).
- Do not catch `BaseException`. The SDK suspends executions with a `BaseException`
  subclass. If you swallow it anyway, the execution still ends waiting for approval.

## Sync API

`run_sync`, `resume_sync`, `cancel_sync` and `runner.approvals.approve_sync` /
`reject_sync` / `list_pending_sync` call `asyncio.run`. They raise `RuntimeError`
when called from a running event loop.
