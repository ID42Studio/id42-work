# Execution

An `Execution` is one attempt to complete a work. It is immutable. Every change is a
new record with a higher `revision`, stored with compare-and-set, and produces exactly
one event.

```text
CREATED → POLICY_CHECK → DENIED
                       → READY → WAITING_APPROVAL → READY | REJECTED | CANCELLED
                               → RUNNING → WAITING_APPROVAL
                                         → FAILED
                                         → VALIDATING → VALIDATION_FAILED
                                                      → WAITING_REVIEW → COMPLETED | REJECTED | CANCELLED
                                                      → COMPLETED
```

Terminal states are `DENIED`, `FAILED`, `VALIDATION_FAILED`, `COMPLETED`, `REJECTED`
and `CANCELLED`. Any transition not in the table raises
`InvalidStateTransitionError`. A retry is a new execution, unless it reuses an
idempotency key, in which case the existing execution is returned.

## Returned, not raised

`run`, `resume` and `cancel` return the execution for every business result. They
raise only for caller errors and integrity problems:

- `UntrustedActorError`
- `InputValidationError`
- `WorkRegistrationError`
- `IdempotencyConflictError`
- `InvalidStateTransitionError`
- `StorageError`

`execution.raise_for_status()` returns the outcome when `COMPLETED`, and otherwise
raises the matching typed error.

## Idempotency

The scope is `(tenant, work id, version, idempotency_key)`. The same scope with the
same input returns the existing execution, whatever its state. The same scope with a
different input raises `IdempotencyConflictError`. Input fingerprints are exact: the
amounts `"1250.00"` and `"1250.0"` count as different inputs.

"Whatever its state" includes `DENIED`. A key used by a call that the policy denied
stays bound to that denied execution: retrying with the same key after granting the
missing capability returns the same `DENIED` execution. Use a new key once the cause
of a denial is fixed.

The scope does not include the actor. Another actor in the same tenant who is allowed
to run the work and submits the same key and input receives the existing execution.

If the call fails before the execution record exists, its reservation is released and
the same key can be retried. A reservation is never taken from another caller: if the
process stops between the reservation and the record, a later call waits, then raises
`StorageError`.

## Failures

`execution.error` records the error type, its message, the cause chain and
`requires_reconciliation`. The reason in `status_reason` is one of:

- `executor_error`
- `timeout`
- `capability_denied`
- `max_actions_exceeded`
- `invalid_action_id`, `duplicate_action_id`, `invalid_action_params`
- `non_deterministic`
- `uncertain_outcome`
- `invalid_capability_result`
- `cancelled_in_flight`

A violation is still recorded when the executor catches the exception.
