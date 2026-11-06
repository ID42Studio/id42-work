# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). During `0.x`, minor versions may break
the public API; breaking changes are listed explicitly.

## [Unreleased]

### Added

- Repository foundation: packaging, CI, linting, formatting, type checking and test
  configuration (milestone M0).
- Engineering specification v0.1.1 (`SPEC.md`).
- Core contracts (milestone M1): `Work`, `Policy`, `TrustedActor` and `ActorAuthority`,
  `Execution`, `Approval`, `Outcome`, `Evidence`, `WorkEvent`, `JournalEntry`,
  canonical JSON and digests, executor/validator/provider/resolver protocols, storage
  protocols, and the exception hierarchy.
- `docs/architecture.md` with the module map and deviations from the specification.
