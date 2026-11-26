# Outcome and evidence

## Outcome

After the executor returns, the runner validates its output in two stages:

1. **Schema**: the output is re-validated from its data against the output schema.
   The exact type is required. A subclass, or an instance built with
   `model_construct`, does not pass.
2. **Validators**: every registered validator runs, in registration order, even after
   a failure.

A validator is any object with `validator_id`, `required` and
`async def validate(output, context) -> ValidationResult`.

- A validator that raises, or returns something other than a `ValidationResult`,
  counts as a failed check. The recorded message names only the exception type; the
  exception itself is logged on the `id42_work.runner` logger, because its text may
  quote input or output values.
- The policy's `execution_limits.timeout` also bounds the validation phase, measured
  from its start. A validator still running at the deadline counts as a failed check
  with the message `validator timed out (execution_limits.timeout)`.
- `context.invoke` gives validators read-only capabilities only.
- Validators must be idempotent. If the process stops during validation, `resume`
  runs them again. A read-only call may reach the provider a second time, because
  those calls are not journaled. A side effect that already completed is not
  repeated.

`outcome.verified` is derived, not declared: it is true when every required result
passed. Three facts are kept apart:

- *executed*: the executor returned;
- *verified*: validators passed;
- *accepted*: the execution is `COMPLETED`, with `outcome.accepted_at` set.

An unverified outcome can never be accepted.

## Evidence

Evidence records hold references and digests, not large payloads. The runner records:

| Type | When |
|---|---|
| `INPUT_REFERENCE` | At creation (input fingerprint) |
| `TOOL_RESULT` | For every completed capability call (result digest, operation id) |
| `VALIDATION_REPORT` | After validation |
| `APPROVAL_DECISION` | For every approval decision |
| Anything else | Through `context.add_evidence(...)` from the executor |

Policies can require evidence types before completion
(`evidence_requirements`). Metadata passes through the redactor.
