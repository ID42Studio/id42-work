# ID42 Work SDK

**Define work. Govern execution. Verify outcomes.**

A lightweight Python SDK for reliable, governed execution of work by software systems
and AI agents.

> **Status: pre-alpha, under construction.** The core contracts, the policy engine,
> the in-memory runtime, approvals and resumption exist. Examples, the fake Odoo
> integration and the full documentation are still missing. The package is not published on PyPI. The design is specified in
> [SPEC.md](SPEC.md) (specification v0.1.1); implementation notes are in
> [docs/architecture.md](docs/architecture.md).

## The idea

When software or an AI agent performs real work, such as validating an invoice,
preparing an ERP entry or posting a payment, "the function returned" is not enough. You
need to know:

- **Was it allowed?** Capabilities are granted by the host application, default-deny,
  and can never be widened by the work itself.
- **Did a human approve the exact action?** Approvals bind to a digest of the action's
  parameters. Approving 1,250.00 does not approve 1,300.00.
- **Is the result actually correct?** An execution is `COMPLETED` only when its output
  passes explicit validators. *Executed* and *verified* are different states.
- **What is the evidence?** Every step leaves structured events and evidence records.

Core principle: **autonomy is a permission, not a capability.**

## What it is not

The SDK is not an LLM framework, an agent planner, a workflow engine, or a hosted
service. It needs no LLM, database, broker or web server.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv build
```

## License

[Apache-2.0](LICENSE). Maintained by [ID42 Studio](https://id42studio.github.io/).
