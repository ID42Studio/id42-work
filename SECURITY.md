# Security policy

## Supported versions

Only the latest `0.x` release receives security fixes. Version 0.1.0 is a pre-alpha
release candidate: read [docs/security.md](docs/security.md) for its guarantees and
documented limitations before using it near real systems.

## Reporting a vulnerability

Do **not** open a public issue for security problems.

- Prefer the repository's private vulnerability reporting (GitHub, "Security" tab),
  when available.
- Otherwise, email **contato@id42.studio** with the subject `SECURITY: id42-work`.

Include the affected version, a description, steps to reproduce and the impact you
expect. We aim to acknowledge reports within five business days and will keep you
informed until the issue is resolved. Please give us reasonable time to fix the
problem before disclosing it.

## Scope

In scope: authorization bypasses, approval forgery or reuse, identity forging,
capability escalation, idempotency failures that cause duplicate side effects, and
leaks of sensitive data into events, evidence or logs.

Out of scope: the limitations documented in `docs/security.md`, such as code running
in the same process bypassing the SDK, or in-memory storage losing state.
