"""Expose granted capabilities to agent frameworks as tools (framework-neutral).

An agent never receives more than the execution was granted: tools are built from
``context.capability_specs``, which only holds granted capabilities, and every tool
call goes through ``context.invoke``, so policies, approval gates, the journal and
replay apply to agent tool calls exactly as to hand-written executor code.

Framework adapters (``id42_work.integrations``) build on this module.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal

from pydantic import JsonValue

from id42_work.contracts import ExecutionContext
from id42_work.exceptions import CapabilityDeniedError

TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"
"""Tool names accepted by the major model providers."""

_TOOL_NAME_RE = re.compile(TOOL_NAME_PATTERN)
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_.:-]")
_MAX_CALL_ID = 100

DEFAULT_PARAMS_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}
"""Schema used when a capability declares no ``params_schema``."""


@dataclass(frozen=True, slots=True)
class AgentTool:
    """A granted capability described for a model."""

    name: str
    capability: str
    description: str
    params_schema: dict[str, JsonValue]
    side_effecting: bool


def tool_name(capability: str) -> str:
    """Model-facing tool name for ``capability``: dots become underscores."""
    name = capability.replace(".", "_")
    if _TOOL_NAME_RE.fullmatch(name) is None:
        msg = f"capability {capability!r} cannot be named as a tool ({TOOL_NAME_PATTERN})"
        raise ValueError(msg)
    return name


def agent_tools(
    context: ExecutionContext, *, capabilities: Iterable[str] | None = None
) -> tuple[AgentTool, ...]:
    """Describe granted capabilities as tools, sorted by capability name.

    ``capabilities`` narrows the set; naming a capability the execution was not
    granted raises ``CapabilityDeniedError``. Two capabilities that map to the same
    tool name raise ``ValueError``.
    """
    specs = context.capability_specs
    selected = sorted(specs if capabilities is None else set(capabilities))
    tools: list[AgentTool] = []
    names: dict[str, str] = {}
    for capability in selected:
        spec = specs.get(capability)
        if spec is None:
            msg = f"capability {capability!r} is not granted to this execution"
            raise CapabilityDeniedError(msg, execution_id=context.execution_id)
        name = tool_name(capability)
        if name in names:
            msg = f"capabilities {names[name]!r} and {capability!r} map to the same tool name"
            raise ValueError(msg)
        names[name] = capability
        tools.append(
            AgentTool(
                name=name,
                capability=capability,
                description=spec.description or f"Invoke the {capability} capability.",
                params_schema=dict(spec.params_schema or DEFAULT_PARAMS_SCHEMA),
                side_effecting=spec.side_effecting,
            )
        )
    return tuple(tools)


def tool_action_id(name: str, tool_call_id: str | None, sequence: int) -> str:
    """Stable action id for one tool call.

    The model's ``tool_call_id`` is part of the journaled model response, so it is
    identical on replay. Without one, the per-run call ``sequence`` is used, which is
    stable as long as tools run one at a time.
    """
    if tool_call_id:
        safe = _UNSAFE_ID_CHARS.sub("-", tool_call_id).lstrip("._:-")[:_MAX_CALL_ID]
        if safe:
            return f"tool:{safe}"
    return f"tool:{name}:{sequence}"


def to_params(arguments: Mapping[str, object]) -> dict[str, object]:
    """Convert model-produced arguments into canonicalizable action parameters.

    JSON numbers arrive as ``float``, which the SDK refuses to digest. They become
    ``Decimal`` from their shortest representation (``1250.5`` -> ``Decimal("1250.5")``),
    which is also what money needs.
    """
    return {key: _convert(value) for key, value in arguments.items()}


def _convert(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, Mapping):
        items: Mapping[object, object] = value  # pyright: ignore[reportUnknownVariableType]
        return {str(k): _convert(v) for k, v in items.items()}
    if isinstance(value, list | tuple):
        sequence: list[object] | tuple[object, ...] = value  # pyright: ignore[reportUnknownVariableType]
        return [_convert(v) for v in sequence]
    return value


async def call_tool(
    context: ExecutionContext,
    tool: AgentTool,
    arguments: Mapping[str, object],
    *,
    action_id: str,
) -> JsonValue:
    """Invoke ``tool``'s capability through the execution context."""
    return await context.invoke(tool.capability, action_id=action_id, params=to_params(arguments))
