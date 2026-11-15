# Contributing

Thank you for helping improve the ID42 Work SDK. The project is maintained by ID42
Studio and accepts external contributions under the Apache-2.0 license.

## Before you start

- For anything beyond a small fix, open an issue first so the design can be discussed.
- Read `SPEC.md` and `docs/architecture.md`. Changes that conflict with the
  specification must state the trade-off in `docs/architecture.md`.
- Security-sensitive changes (policy, approvals, identities, journal, redaction) need
  tests in `tests/security/`.
- Do not relax a security requirement to simplify an example.

## Development

```bash
uv sync --all-extras --dev
uv run pytest                  # unit, integration, security, concurrency, contract, examples
# Branch coverage of PostgresStorage needs a server:
# ID42_POSTGRES_DSN=postgresql://id42:id42@localhost:5432/id42 uv run pytest --cov --cov-fail-under=100
uv run ruff check .
uv run ruff format --check .
uv run pyright                 # strict on src/
```

Every public API needs documentation and tests. Coverage of `src/` is 100%; keep it
that way with meaningful tests, not just line coverage.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
  `docs:`, `test:`, `refactor:`, `chore:`).
- Keep pull requests focused. Fill in the template, including the security checklist.
- CI must pass before merge.
- Add a line to `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Versioning

The project follows Semantic Versioning. During `0.x`, minor versions may break the
public API; breaking changes are listed in the changelog.

How to configure trusted publishing and cut a release is in
[docs/releasing.md](docs/releasing.md).

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
