## What and why

<!-- What does this change do, and which problem does it solve? Link the issue. -->

## Checklist

- [ ] Tests added or updated (`uv run pytest` passes, coverage of `src/` stays at 100%)
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` pass
- [ ] Public API changes are documented (docstrings, `docs/`, `README.md`)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for user-visible changes
- [ ] Deviations from `SPEC.md` are recorded in `docs/architecture.md`

## Security

- [ ] This change does not touch policies, approvals, identities, the journal, events or redaction
- [ ] Or: it does, and `tests/security/` covers the new behaviour
