# AI agents

An agent is an executor that lets a model choose the next step. The SDK governs it the
same way it governs hand-written code: the agent can only use the capabilities the
execution was granted, side effects still pass through approval gates and the journal,
and the outcome is still validated before the execution completes.

PydanticAI is supported today (`pip install "id42-work[pydantic-ai]"`). The
framework-neutral layer in `id42_work.agents` is the base for other adapters
(LangGraph is next on the roadmap). A remote MCP client reaches the same runner
through `id42-work[mcp]`; see [MCP server](mcp.md). That server does not turn
capabilities into tools.

## Quick start

```python
from pydantic_ai import Agent

from id42_work.integrations.pydantic_ai import RecordedModel, capability_toolset


class BillAgent:
    async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
        agent = Agent(
            RecordedModel("anthropic:claude-sonnet-5-5", context),
            toolsets=[capability_toolset(context)],
            output_type=BillResult,
        )
        result = await agent.run(f"Post bill {input_data.invoice_number}.")
        return result.output
```

Register `BillAgent` like any executor. The complete, offline version (with a scripted
model instead of an API call) is `examples/agent_invoice.py`.

## What each piece does

**`capability_toolset(context)`** builds one tool per granted capability:

- **Names**: dots become underscores (`odoo.invoice.post` → `odoo_invoice_post`), so
  the names are valid for every major model provider.
- **Descriptions**: the tool's description and argument schema come from the
  `CapabilitySpec` (`description`, `params_schema`). Give providers good descriptions:
  they are the agent's documentation.
- **Calls**: every call goes through `context.invoke`. Policies, approval gates,
  `max_actions`, the journal and evidence apply.
- **Action ids**: the id of each call is `tool:<tool_call_id>`. The call id is part of
  the recorded model response, so it is the same on replay.
- **Numbers**: models send JSON numbers as floats, which the SDK refuses to digest.
  Tool arguments are converted to `Decimal` from their shortest representation
  (`1250.5` → `Decimal("1250.5")`) before they reach the provider.
- **No parallelism**: tools are sequential. A tool that waits for approval therefore
  never cancels a sibling in the middle of a side effect.

`capability_toolset(context, capabilities=[...])` narrows the set. Naming a capability
the execution was not granted raises `CapabilityDeniedError`.

**`RecordedModel(model, context)`** wraps any PydanticAI model and journals each
response with `context.record`. The n-th request of the run is the action
`model:<n>`.

## How approval and resume work with an agent

1. The agent runs. The model asks for `odoo_invoice_post`. The policy requires approval
   for that capability, so `context.invoke` suspends the execution. The model's
   responses so far, and the results of the tools it already used, are in the journal.
2. A reviewer approves the exact action, with its parameters. From the command line
   that is `id42-work approvals approve --actor <operator> --resume`. The operator
   is not the actor that resumes. See [Approval](concepts/approval.md).
3. `runner.resume(...)` runs the executor again from the start. `RecordedModel` returns
   the recorded responses instead of calling the model, and the tools return their
   recorded results instead of calling the providers. The agent therefore takes the
   same path, reaches the post with the same parameters, consumes the approval, and
   continues with new model calls from there.

If the conversation differs on replay, the execution fails as `non_deterministic`
and the model is not called again. A crash during validation is the same replay:
`resume` returns each stored response and does not call the model. A missing or
different recording fails closed as `VALIDATION_FAILED` (`non_deterministic`). See
[Storage](storage.md). The comparison covers the model id, the sampling
settings, the tool and output schemas, and the content the model must see again,
including thinking signatures and provider payloads such as a compaction. It leaves
out timestamps, run ids, usage and the provider's reported model name, which change
between processes without changing the conversation. A prompt that includes the
current time or a random value still fails. Keep everything the agent sees derived
from the input and from tool results.

## `context.record`

The primitive under `RecordedModel` is available to any executor:

```python
plan = await context.record(
    "plan:1",
    fingerprint={"prompt": prompt},
    compute=lambda: call_some_model(prompt),
)
```

- **First run**: `compute()` is awaited and its JSON result is stored.
- **Replay**: the same `action_id` with an equal `fingerprint` returns the stored
  result. A different fingerprint fails the execution as non-deterministic.
- **Failures**: a failed computation raises again on replay. One left `IN_FLIGHT`
  (the process died before the result was stored) is computed again, since nothing
  outside the SDK changed. One left `UNCERTAIN` is not. If a capability call is also
  still `IN_FLIGHT`, the execution fails for reconciliation and the record is not
  computed again.
- **No grant**: a recorded computation reaches nothing through the SDK by itself.
  Records count toward `max_actions` and share action ids with `invoke`.

## Limits

- **Streaming**: `agent.run_stream` is refused, because streamed responses are not
  journaled.
- **One `RecordedModel` per agent run**: create it inside `execute`. Use a distinct
  `prefix` when one executor runs several agents.
- **The journal stores full model responses**: in durable storage they are kept like
  any other action result. Treat the storage as holding whatever the model wrote.
- **`max_actions`**: it counts model calls and tool calls. Size it for the whole
  conversation.
- **Not a sandbox**: an agent can only reach external systems through its tools if
  the executor gives it nothing else. Do not hand an agent other clients or tools that
  bypass `context.invoke`. See [Security](security.md).
