"""A remote MCP client cannot bypass policy, validation or approval."""

import json
from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID

import pytest
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import CallToolResult, InputRequiredResult
from pydantic import BaseModel, JsonValue

from id42_work import (
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    ExecutionStatus,
    Policy,
    Severity,
    StaticPolicyResolver,
    ValidationResult,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.contracts import ValidationContext, Validator
from id42_work.mcp import McpCallError
from id42_work.mcp.server import WorkMcpServer
from mcp_support import hosted


class PayInput(BaseModel):
    amount: str


class PayOutput(BaseModel):
    status: str


class Ledger:
    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name="payments.execute", side_effecting=True)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        self.calls += 1
        return {"posted": True, "token": "secret-token"}


class Pay:
    async def execute(self, input_data: PayInput, context: ExecutionContext) -> PayOutput:
        await context.invoke(
            "payments.execute", action_id="pay", params={"amount": input_data.amount}
        )
        return PayOutput(status="posted")


class RejectOutput:
    validator_id = "reject_output"
    required = True

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        return ValidationResult(
            validator_id=self.validator_id,
            passed=False,
            severity=Severity.ERROR,
            message="rejected",
        )


def _pay_server(
    *,
    autonomy: AutonomyLevel = AutonomyLevel.SUPERVISED,
    validators: tuple[Validator, ...] = (),
) -> tuple[WorkMcpServer, WorkRunner, Ledger]:
    work = Work(
        id="pay",
        version="1.0.0",
        name="Pay",
        input_schema=PayInput,
        output_schema=PayOutput,
        required_capabilities=frozenset({"payments.execute"}),
    )
    registry = WorkRegistry()
    registry.register(work, executor=Pay(), validators=validators)
    ledger = Ledger()
    capabilities = CapabilityRegistry()
    capabilities.add(ledger)
    policy = Policy(
        id="payments",
        version="1.0.0",
        autonomy=autonomy,
        allowed_capabilities=frozenset({"payments.execute"}),
    )
    server, runner, _authority = hosted(
        registry=registry,
        policies=StaticPolicyResolver({("pay", "1.0.0"): policy}),
        capabilities=capabilities,
        grants={"payments.execute"},
    )
    return server, runner, ledger


@pytest.mark.asyncio
async def test_the_client_cannot_approve_or_choose_an_actor() -> None:
    server, runner, ledger = _pay_server()
    tools = {tool.name: tool.input_schema for tool in await server.server.list_tools()}
    assert set(tools) == {
        "list_works",
        "start_execution",
        "get_execution",
        "resume_execution",
        "list_executions",
        "cancel_execution",
    }
    assert "actor" not in tools["start_execution"]["properties"]
    assert "grants" not in tools["start_execution"]["properties"]
    assert set(tools["resume_execution"]["properties"]) == {"execution_id"}
    with pytest.raises(ToolError, match="Unknown tool"):
        await server.server.call_tool("approve", {"approval_id": "x"})

    started = await server.server.call_tool(
        "start_execution",
        {
            "work_id": "pay",
            "version": "1.0.0",
            "input_data": {"amount": "1250.00"},
            "idempotency_key": "bill-1",
            "actor_id": "user:root",
            "grants": ["payments.execute", "approve:pay"],
        },
    )
    assert isinstance(started, CallToolResult)
    view = started.structured_content
    assert view is not None
    assert view["status"] == "WAITING_APPROVAL"
    assert view["needs_human_decision"] is True
    assert view["actor_id"] == "user:ada"
    assert ledger.calls == 0
    assert "1250.00" not in json.dumps(view)
    execution_id = str(view["execution_id"])

    resumed = await server.server.call_tool("resume_execution", {"execution_id": execution_id})
    assert isinstance(resumed, CallToolResult)
    assert resumed.structured_content is not None
    assert resumed.structured_content["status"] == "WAITING_APPROVAL"
    assert ledger.calls == 0

    loaded = await server.server.read_resource(f"id42://executions/{execution_id}/approvals")
    assert not isinstance(loaded, InputRequiredResult)
    text = next(
        item.content
        for item in loaded
        if isinstance(item, ReadResourceContents) and isinstance(item.content, str)
    )
    approvals = json.loads(text)
    assert approvals["approvals"][0]["status"] == "PENDING"
    assert approvals["approvals"][0]["gate"] == "ACTION"
    assert "params_preview" not in approvals["approvals"][0]
    assert "1250.00" not in json.dumps(approvals)

    reviewer = server._authority.issue(
        actor_id="user:reviewer",
        grants={"approve:pay"},
        ttl=timedelta(minutes=5),
        tenant_id="acme",
    )
    pending = await runner.approvals.list_pending(UUID(execution_id))
    await runner.approvals.approve(pending[0].approval_id, reviewer=reviewer, reason="PO matched")
    completed = await server.resume_execution(execution_id)
    assert completed["status"] == "COMPLETED"
    assert ledger.calls == 1
    journal = json.loads(await server.read_journal(execution_id))
    assert journal["entries"][0]["result"]["token"] == "***"  # noqa: S105
    assert "secret-token" not in json.dumps(journal)


@pytest.mark.asyncio
async def test_validation_failure_is_not_completion() -> None:
    server, _runner, ledger = _pay_server(
        autonomy=AutonomyLevel.AUTONOMOUS, validators=(RejectOutput(),)
    )
    view = await server.start_execution(
        "pay", "1.0.0", {"amount": "10.00"}, idempotency_key="bill-2"
    )
    assert view["status"] == "VALIDATION_FAILED"
    assert view["needs_human_decision"] is False
    outcome = view["outcome"]
    assert isinstance(outcome, dict)
    assert outcome["verified"] is False
    assert outcome["accepted"] is False
    assert ledger.calls == 1


@pytest.mark.asyncio
async def test_cancel_does_not_perform_the_side_effect() -> None:
    server, _runner, ledger = _pay_server()
    started = await server.start_execution(
        "pay", "1.0.0", {"amount": "10.00"}, idempotency_key="bill-3"
    )
    cancelled = await server.cancel_execution(str(started["execution_id"]), reason="stop")
    assert cancelled["status"] == "CANCELLED"
    assert ledger.calls == 0
    again = await server.resume_execution(str(started["execution_id"]))
    assert again["status"] == "CANCELLED"
    assert ledger.calls == 0


@pytest.mark.asyncio
async def test_another_actors_execution_stays_hidden() -> None:
    server, runner, _ledger = _pay_server(autonomy=AutonomyLevel.AUTONOMOUS)
    outsider = server._authority.issue(
        actor_id="user:other",
        grants={"payments.execute"},
        ttl=timedelta(minutes=5),
        tenant_id="other",
    )
    foreign = await runner.run(
        work_id="pay",
        version="1.0.0",
        input_data={"amount": "10.00"},
        actor=outsider,
        idempotency_key="bill-foreign",
    )
    assert foreign.status is ExecutionStatus.COMPLETED
    with pytest.raises(ToolError, match="execution not found"):
        await server.server.call_tool(
            "resume_execution", {"execution_id": str(foreign.execution_id)}
        )
    with pytest.raises(ResourceNotFoundError):
        await server.server.read_resource(f"id42://executions/{foreign.execution_id}/evidence")
    with pytest.raises(McpCallError, match="execution not found"):
        await server.cancel_execution(str(foreign.execution_id))
    stored = await runner.get(foreign.execution_id)
    assert stored is not None
    assert stored.status is ExecutionStatus.COMPLETED
    visible = await server.list_executions()
    assert visible["executions"] == []
