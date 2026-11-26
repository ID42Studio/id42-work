from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue, ValidationError

from id42_work import CapabilityDeniedError, CapabilitySpec
from id42_work.agents import (
    DEFAULT_PARAMS_SCHEMA,
    AgentTool,
    agent_tools,
    call_tool,
    to_params,
    tool_action_id,
    tool_name,
)


class FakeContext:
    def __init__(self, specs: list[CapabilitySpec]) -> None:
        self.capability_specs = {spec.name: spec for spec in specs}
        self.execution_id = uuid4()
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def invoke(self, capability: str, *, action_id: str, params: Any) -> JsonValue:
        self.calls.append((capability, action_id, dict(params)))
        return {"ok": True}


READ = CapabilitySpec(
    name="odoo.partner.read",
    side_effecting=False,
    description="Read a partner",
    params_schema={"type": "object", "properties": {"ref": {"type": "string"}}},
)
POST = CapabilitySpec(name="odoo.invoice.post", side_effecting=True)


def test_tools_describe_granted_capabilities() -> None:
    tools = agent_tools(FakeContext([POST, READ]))  # type: ignore[arg-type]
    assert [t.name for t in tools] == ["odoo_invoice_post", "odoo_partner_read"]
    post, read = tools
    assert post.description == "Invoke the odoo.invoice.post capability."
    assert post.params_schema == DEFAULT_PARAMS_SCHEMA
    assert post.side_effecting
    assert read.params_schema == READ.params_schema
    assert not read.side_effecting


def test_narrowing_and_denial() -> None:
    context = FakeContext([POST, READ])
    (only,) = agent_tools(context, capabilities=["odoo.partner.read"])  # type: ignore[arg-type]
    assert only.capability == "odoo.partner.read"
    with pytest.raises(CapabilityDeniedError):
        agent_tools(context, capabilities=["payments.execute"])  # type: ignore[arg-type]


def test_tool_name_collisions_and_limits() -> None:
    a = CapabilitySpec(name="a_b.c", side_effecting=False)
    b = CapabilitySpec(name="a.b_c", side_effecting=False)
    with pytest.raises(ValueError, match="same tool name"):
        agent_tools(FakeContext([a, b]))  # type: ignore[arg-type]
    assert tool_name("odoo.invoice.post") == "odoo_invoice_post"
    with pytest.raises(ValueError, match="cannot be named"):
        tool_name("x" * 65)


@pytest.mark.parametrize(
    ("call_id", "expected"),
    [
        ("call_1", "tool:call_1"),
        ("toolu_01AbC", "tool:toolu_01AbC"),
        ("id with spaces/and#symbols", "tool:id-with-spaces-and-symbols"),
        ("__leading", "tool:leading"),
        ("x" * 300, "tool:" + "x" * 100),
        (None, "tool:odoo_partner_read:3"),
        ("", "tool:odoo_partner_read:3"),
        ("::", "tool:odoo_partner_read:3"),
    ],
)
def test_tool_action_ids_are_stable_and_valid(call_id: str | None, expected: str) -> None:
    assert tool_action_id("odoo_partner_read", call_id, 3) == expected


def test_floats_become_decimals_recursively() -> None:
    converted = to_params(
        {"amount": 1250.5, "flag": True, "count": 3, "lines": [{"price": 0.1}, (2.5, "x")]}
    )
    assert converted == {
        "amount": Decimal("1250.5"),
        "flag": True,
        "count": 3,
        "lines": [{"price": Decimal("0.1")}, [Decimal("2.5"), "x"]],
    }
    assert type(converted["flag"]) is bool


async def test_call_tool_goes_through_invoke() -> None:
    context = FakeContext([READ])
    tool = AgentTool(
        name="odoo_partner_read",
        capability="odoo.partner.read",
        description="",
        params_schema={},
        side_effecting=False,
    )
    result = await call_tool(context, tool, {"score": 0.5}, action_id="tool:call_1")  # type: ignore[arg-type]
    assert result == {"ok": True}
    assert context.calls == [("odoo.partner.read", "tool:call_1", {"score": Decimal("0.5")})]


def test_params_schema_must_describe_an_object() -> None:
    with pytest.raises(ValidationError, match="object"):
        CapabilitySpec(name="x.read", side_effecting=False, params_schema={"type": "array"})
    assert CapabilitySpec(name="x.read", side_effecting=False, params_schema={}).params_schema == {}
