"""PydanticAI integration (install the ``pydantic-ai`` extra).

Two pieces make a PydanticAI agent a governed executor:

- ``capability_toolset(context)`` gives the agent one tool per granted capability.
  Every call goes through ``context.invoke``: policies, approval gates, the journal and
  replay apply. Tools run one at a time (``sequential=True``) so that a tool waiting
  for approval never cancels a sibling in the middle of a side effect.
- ``RecordedModel(model, context)`` journals each model response with
  ``context.record``. When an execution resumes after an approval, the agent runs again
  from the start and gets the recorded responses back instead of calling the model, so
  it takes the same decisions and reaches the action that waited for approval.

Example::

    class InvoiceAgent:
        async def execute(self, data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
            agent = Agent(
                RecordedModel("anthropic:claude-sonnet-5-5", context),
                toolsets=[capability_toolset(context)],
                output_type=InvoiceOutput,
            )
            result = await agent.run(f"Validate invoice {data.invoice_number}")
            return result.output

Streaming (``agent.run_stream``) is not journaled and is refused.
"""

import itertools
from collections.abc import AsyncGenerator, Iterable, Iterator
from contextlib import asynccontextmanager
from typing import Any, cast

from pydantic import JsonValue, ValidationError
from pydantic_ai import RunContext, Tool
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from id42_work.agents import AgentTool, agent_tools, call_tool, tool_action_id
from id42_work.contracts import ExecutionContext
from id42_work.error_info import describe_validation_error
from id42_work.exceptions import ExecutionError

__all__ = ["RecordedModel", "capability_tools", "capability_toolset"]

_STABLE_MESSAGE_KEYS = ("kind", "instructions", "state")
_STABLE_PART_KEYS = (
    "part_kind",
    "content",
    "tool_name",
    "args",
    "tool_call_id",
    "outcome",
    "id",
    "signature",
    "provider_name",
    "provider_details",
    "tool_kind",
    "scope",
    "context_id",
    "dynamic_ref",
)


def capability_tools(
    context: ExecutionContext, *, capabilities: Iterable[str] | None = None
) -> list[Tool[Any]]:
    """One PydanticAI tool per granted capability (or per name in ``capabilities``)."""
    sequence = itertools.count(1)
    return [
        Tool.from_schema(
            _tool_function(context, tool, sequence),
            name=tool.name,
            description=tool.description,
            json_schema=dict(tool.params_schema),
            takes_ctx=True,
            sequential=True,
        )
        for tool in agent_tools(context, capabilities=capabilities)
    ]


def capability_toolset(
    context: ExecutionContext, *, capabilities: Iterable[str] | None = None
) -> FunctionToolset[Any]:
    """A ``FunctionToolset`` with ``capability_tools(context, capabilities=...)``."""
    return FunctionToolset(capability_tools(context, capabilities=capabilities))


def _tool_function(context: ExecutionContext, tool: AgentTool, sequence: Iterator[int]) -> Any:  # noqa: ANN401
    async def call(run_context: RunContext[Any], /, **arguments: Any) -> JsonValue:  # noqa: ANN401
        action_id = tool_action_id(tool.name, run_context.tool_call_id, next(sequence))
        return await call_tool(context, tool, arguments, action_id=action_id)

    call.__name__ = tool.name
    return call


class RecordedModel(WrapperModel):
    """Wraps a PydanticAI model and journals its responses with ``context.record``.

    The n-th request of a run is recorded as action ``f"{prefix}:{n}"``. Use one
    ``RecordedModel`` per agent run, created inside ``execute``, and give each agent a
    distinct ``prefix`` when an executor runs several.

    The fingerprint that detects divergence on replay covers the model id, the sampling
    settings, the tool and output schemas, and the conversation content that the model
    must see again: message kinds, instructions, state, and each part's content, tool
    name, arguments, call id, signature and provider payload. Timestamps, run ids,
    usage and the provider's reported model name are left out, because they change
    between processes without changing the conversation.
    """

    def __init__(
        self, wrapped: Model | str, context: ExecutionContext, *, prefix: str = "model"
    ) -> None:
        super().__init__(wrapped)
        if not prefix or ":" in prefix:
            msg = "prefix must be a non-empty string without ':'"
            raise ValueError(msg)
        self._context = context
        self._prefix = prefix
        self._requests = 0

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._requests += 1
        action_id = f"{self._prefix}:{self._requests}"

        async def compute() -> JsonValue:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
            return cast(
                "JsonValue", ModelMessagesTypeAdapter.dump_python([response], mode="json")[0]
            )

        stored = await self._context.record(
            action_id,
            fingerprint=_fingerprint(
                messages,
                model_id=self.model_id,
                model_settings=model_settings,
                parameters=model_request_parameters,
            ),
            compute=compute,
        )
        try:
            (restored,) = ModelMessagesTypeAdapter.validate_python([stored])
        except ValidationError as error:
            detail = describe_validation_error(error)
            msg = f"journal entry {action_id!r} does not hold a model response: {detail}"
            raise ExecutionError(msg, execution_id=self._context.execution_id) from None
        if not isinstance(restored, ModelResponse):
            msg = f"journal entry {action_id!r} does not hold a model response"
            raise ExecutionError(msg, execution_id=self._context.execution_id)
        return restored

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        msg = "streamed model responses are not journaled; use agent.run() with RecordedModel"
        raise NotImplementedError(msg)
        yield  # pragma: no cover - makes this an async generator


def _fingerprint(
    messages: list[ModelMessage],
    *,
    model_id: str,
    model_settings: ModelSettings | None,
    parameters: ModelRequestParameters,
) -> dict[str, JsonValue]:
    dumped = cast(
        "list[dict[str, Any]]", ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    )
    projected: list[JsonValue] = []
    for message in dumped:
        item: dict[str, JsonValue] = {key: message.get(key) for key in _STABLE_MESSAGE_KEYS}
        parts = cast("list[dict[str, Any]]", message.get("parts") or [])
        item["parts"] = [
            {key: part[key] for key in _STABLE_PART_KEYS if key in part} for part in parts
        ]
        projected.append(item)
    settings: JsonValue = None
    if model_settings is not None:
        settings = cast("JsonValue", dict(model_settings))
    return {
        "model_id": model_id,
        "model_settings": settings,
        "messages": projected,
        "request": {
            "output_mode": parameters.output_mode,
            "allow_text_output": parameters.allow_text_output,
            "allow_image_output": parameters.allow_image_output,
            "function_tools": [_tool_projection(tool) for tool in parameters.function_tools],
            "output_tools": [_tool_projection(tool) for tool in parameters.output_tools],
        },
    }


def _tool_projection(tool: ToolDefinition) -> JsonValue:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": cast("JsonValue", tool.parameters_json_schema),
        "kind": tool.kind,
        "strict": tool.strict,
    }
