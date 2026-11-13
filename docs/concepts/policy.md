# Policy

A `Policy` belongs to the host application. The runner obtains it from a
`PolicyResolver`; a work definition never carries one.

| Field | Meaning |
|---|---|
| `autonomy` | `MANUAL`, `SUPERVISED`, `GUIDED` or `AUTONOMOUS`. |
| `allowed_capabilities` | Upper bound for works under this policy. |
| `approval_requirements` | Extra gates. They apply at every autonomy level. |
| `execution_limits` | `timeout` (applied separately to the executor run and to the validation phase) and `max_actions`. |
| `evidence_requirements` | Evidence types that must exist before completion. |
| `require_idempotency_key` | Override. By default, a key is required when any required capability is side-effecting. |

## Decision

`PolicyEngine.evaluate` is deterministic and never calls an LLM:

1. The actor's seal and validity window are verified. Otherwise `UntrustedActorError`
   is raised.
2. No policy means deny (`no_policy`). A resolver error means deny (`policy_error`).
3. Each required capability must be in the actor's grants, the policy's allowed set,
   the per-call constraints (when given), and the capability registry. Every failure
   is reported: `missing_capability:`, `policy_disallows_capability:`,
   `constraint_excludes_capability:` or `capability_unavailable:`.
4. The execution is granted exactly the required capabilities, never more.
5. `effective_autonomy = min(policy.autonomy, work.autonomy_ceiling)`.

## Gates

| Level | START | ACTION (side-effecting capabilities) | OUTCOME |
|---|---|---|---|
| `MANUAL` | yes | yes | yes |
| `SUPERVISED` | — | yes | — |
| `GUIDED` | — | — | yes |
| `AUTONOMOUS` | — | — | — |

Default gates require the reviewer grant `approve:<work_id>`. An `ApprovalRequirement`
can name another reviewer grant, require a reason and set a TTL. A capability unknown
to the registry counts as side-effecting.

## Grants

An actor's grants are capability names plus three prefixed forms:
`approve:<target>`, `resume:<work_id>` and `cancel:<work_id>`. An approval grant is
not a capability: `approve:payments.execute` does not allow executing payments.
