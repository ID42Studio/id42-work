# Work

A `Work` is a versioned, immutable, serializable definition of a unit of work.

| Field | Meaning |
|---|---|
| `id`, `version` | Identity. `version` is Semantic Versioning. |
| `name`, `description` | Human-readable text. |
| `input_schema`, `output_schema` | Pydantic models. |
| `required_capabilities` | Capabilities the work needs. Requesting a capability authorizes nothing. |
| `autonomy_ceiling` | The most autonomy this work may ever run with. It can only lower the policy's level. |

A `Work` holds no executor, validators or policy. Executors and validators are
attached with `WorkRegistry.register(work, executor=..., validators=[...])`. Policies
come from the host's `PolicyResolver`.

`work.describe()` returns a `WorkDescriptor` (plain data, schemas as JSON Schema).
`work.definition_digest` is the SHA-256 of its canonical JSON. It is stable across
processes and changes whenever anything in the definition changes. Every execution
records the digest it started with. An execution cannot be resumed after its
definition was replaced.

Create a new version whenever the external contract changes: schemas, required
capabilities or autonomy ceiling.
