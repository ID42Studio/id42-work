"""PydanticAI agents as governed executors: tools, gates, journaled model calls, replay."""

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityDeniedError,
    CapabilityRegistry,
    ExecutionContext,
    ExecutionError,
    ExecutionStatus,
    MemoryStorage,
    Policy,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.identifiers import RECORD_CAPABILITY
from id42_work.integrations.pydantic_ai import (
    RecordedModel,
    capability_tools,
    capability_toolset,
)
from id42_work.journal import JournalStatus
from id42_work.testing import (
    INVOICE_POST,
    INVOICE_PREPARE,
    PARTNER_READ,
    FakeOdooProvider,
    ManualClock,
)

S = ExecutionStatus
REVIEW = "approve:bill_agent"


class BillRequest(BaseModel):
    invoice_number: str
    supplier: str
    amount: str


class BillResult(BaseModel):
    status: str
    move_id: int


class ScriptedLLM:
    """A deterministic stand-in for a model: picks the next step from the conversation."""

    def __init__(self, *, amount: object = "1250.00") -> None:
        self.calls = 0
        self.amount = amount

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        returns = {
            part.tool_name: part.content
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart)
        }
        if "odoo_partner_read" not in returns:
            return _call("odoo_partner_read", {"ref": "SUP-001"}, "call_read")
        if "odoo_invoice_prepare" not in returns:
            args = {
                "partner_ref": "SUP-001",
                "invoice_number": "INV-1001",
                "amount": self.amount,
                "currency": "BRL",
            }
            return _call("odoo_invoice_prepare", args, "call_prepare")
        move = returns["odoo_invoice_prepare"]
        assert isinstance(move, dict)
        (output_tool,) = info.output_tools
        return _call(
            output_tool.name, {"status": "drafted", "move_id": move["move_id"]}, "call_out"
        )


def _call(name: str, args: dict[str, Any], call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=call_id)])


class BillAgent:
    """An executor that delegates the work to a PydanticAI agent."""

    def __init__(self, llm: ScriptedLLM, *, prompt_suffix: str = "") -> None:
        self.llm = llm
        self.prompt_suffix = prompt_suffix
        self.runs = 0

    async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
        self.runs += 1
        agent = Agent(
            RecordedModel(FunctionModel(self.llm), context),
            toolsets=[capability_toolset(context)],
            output_type=BillResult,
        )
        suffix = self.prompt_suffix.format(run=self.runs)
        result = await agent.run(
            f"Draft bill {input_data.invoice_number} for {input_data.supplier}.{suffix}"
        )
        return result.output


class Setup:
    def __init__(self, autonomy: AutonomyLevel, executor: BillAgent) -> None:
        self.odoo = FakeOdooProvider()
        capabilities = CapabilityRegistry()
        capabilities.add(self.odoo)
        work = Work(
            id="bill_agent",
            version="1.0.0",
            name="Bill agent",
            input_schema=BillRequest,
            output_schema=BillResult,
            required_capabilities=frozenset({PARTNER_READ, INVOICE_PREPARE}),
        )
        registry = WorkRegistry()
        registry.register(work, executor=executor)
        policy = Policy(
            id="bills",
            version="1.0.0",
            autonomy=autonomy,
            allowed_capabilities=frozenset({PARTNER_READ, INVOICE_PREPARE, INVOICE_POST}),
        )
        self.clock = ManualClock()
        self.authority = ActorAuthority("agents", clock=self.clock)
        self.runner = WorkRunner(
            registry=registry,
            policies=StaticPolicyResolver({("bill_agent", "1.0.0"): policy}),
            capabilities=capabilities,
            actor_authority=self.authority,
            storage=MemoryStorage(),
            clock=self.clock,
        )
        self.clerk = self.issue("clerk:1", {PARTNER_READ, INVOICE_PREPARE, INVOICE_POST})
        self.reviewer = self.issue("finance:1", {REVIEW})

    def issue(self, actor_id: str, grants: set[str]) -> Any:
        return self.authority.issue(
            actor_id=actor_id, tenant_id="acme", grants=grants, ttl=timedelta(hours=1)
        )

    async def run(self) -> Any:
        return await self.runner.run(
            work_id="bill_agent",
            version="1.0.0",
            input_data={"invoice_number": "INV-1001", "supplier": "SUP-001", "amount": "1250.00"},
            actor=self.clerk,
            idempotency_key="bill-INV-1001",
        )


async def test_autonomous_agent_completes_through_governed_tools() -> None:
    llm = ScriptedLLM()
    setup = Setup(AutonomyLevel.AUTONOMOUS, BillAgent(llm))
    execution = await setup.run()
    assert execution.status is S.COMPLETED
    assert execution.raise_for_status().output == BillResult(status="drafted", move_id=1)
    assert llm.calls == 3
    assert setup.odoo.count(INVOICE_PREPARE) == 1
    journal = await setup.runner.journal(execution.execution_id)
    assert [(e.action_id, e.capability) for e in journal] == [
        ("model:1", RECORD_CAPABILITY),
        ("tool:call_read", PARTNER_READ),
        ("model:2", RECORD_CAPABILITY),
        ("tool:call_prepare", INVOICE_PREPARE),
        ("model:3", RECORD_CAPABILITY),
    ]
    assert all(e.status is JournalStatus.SUCCEEDED for e in journal)


async def test_gated_tool_suspends_and_resume_replays_the_model() -> None:
    llm = ScriptedLLM()
    agent = BillAgent(llm)
    setup = Setup(AutonomyLevel.SUPERVISED, agent)
    waiting = await setup.run()
    assert waiting.status is S.WAITING_APPROVAL
    assert llm.calls == 2  # read, then the draft that now waits for approval
    assert setup.odoo.count(INVOICE_PREPARE) == 0
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    assert approval.capability == INVOICE_PREPARE
    assert approval.action_id == "tool:call_prepare"
    assert approval.params_preview["amount"] == "1250.00"

    await setup.runner.approvals.approve(approval.approval_id, reviewer=setup.reviewer)
    done = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    assert done.status is S.COMPLETED
    assert agent.runs == 2
    assert llm.calls == 3  # the two recorded turns were replayed, not asked again
    assert setup.odoo.count(INVOICE_PREPARE) == 1
    types = [e.event_type.value for e in await setup.runner.events(waiting.execution_id)]
    assert types.count("work.action.replayed") == 3  # model:1, tool:call_read, model:2


async def test_a_conversation_that_changes_on_replay_fails() -> None:
    llm = ScriptedLLM()
    setup = Setup(AutonomyLevel.SUPERVISED, BillAgent(llm, prompt_suffix=" Attempt {run}."))
    waiting = await setup.run()
    (approval,) = await setup.runner.approvals.list_pending(waiting.execution_id)
    await setup.runner.approvals.approve(approval.approval_id, reviewer=setup.reviewer)
    failed = await setup.runner.resume(waiting.execution_id, actor=setup.clerk)
    assert failed.status is S.FAILED
    assert failed.status_reason == "non_deterministic"
    assert setup.odoo.count(INVOICE_PREPARE) == 0


async def test_model_numbers_reach_providers_as_decimals() -> None:
    llm = ScriptedLLM(amount=1250.5)
    setup = Setup(AutonomyLevel.AUTONOMOUS, BillAgent(llm))
    execution = await setup.run()
    assert execution.status is S.COMPLETED
    assert setup.odoo.moves[1]["amount"] == "1250.5"
    (entry,) = [
        e
        for e in await setup.runner.journal(execution.execution_id)
        if e.action_id == "tool:call_prepare"
    ]
    assert entry.status is JournalStatus.SUCCEEDED


async def test_tools_cover_only_granted_capabilities() -> None:
    seen: dict[str, object] = {}

    class Inspect:
        async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
            tools = capability_tools(context)
            seen["names"] = sorted(t.name for t in tools)
            seen["sequential"] = all(t.sequential for t in tools)
            try:
                capability_tools(context, capabilities=[INVOICE_POST])
            except CapabilityDeniedError:
                seen["denied"] = True
            narrowed = capability_tools(context, capabilities=[PARTNER_READ])
            seen["narrowed"] = [t.name for t in narrowed]
            return BillResult(status="inspected", move_id=0)

    setup = Setup(AutonomyLevel.AUTONOMOUS, Inspect())  # type: ignore[arg-type]
    assert (await setup.run()).status is S.COMPLETED
    assert seen == {
        "names": ["odoo_invoice_prepare", "odoo_partner_read"],  # not odoo_invoice_post
        "sequential": True,
        "denied": True,
        "narrowed": ["odoo_partner_read"],
    }


async def test_model_errors_are_recorded_as_failed_actions() -> None:
    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ConnectionError("model endpoint unreachable")

    class Broken(BillAgent):
        async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
            agent = Agent(RecordedModel(FunctionModel(broken), context), output_type=BillResult)
            return (await agent.run("hi")).output

    setup = Setup(AutonomyLevel.AUTONOMOUS, Broken(ScriptedLLM()))
    execution = await setup.run()
    assert execution.status is S.FAILED
    (entry,) = await setup.runner.journal(execution.execution_id)
    assert (entry.action_id, entry.status) == ("model:1", JournalStatus.FAILED)


async def test_text_answers_round_trip_through_the_journal() -> None:
    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="ok")])

    class Texty(BillAgent):
        async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
            agent = Agent(RecordedModel(FunctionModel(answer), context, prefix="summary"))
            text = (await agent.run("summarize")).output
            return BillResult(status=text, move_id=0)

    setup = Setup(AutonomyLevel.AUTONOMOUS, Texty(ScriptedLLM()))
    execution = await setup.run()
    assert execution.raise_for_status().output == BillResult(status="ok", move_id=0)
    (entry,) = await setup.runner.journal(execution.execution_id)
    assert entry.action_id == "summary:1"


async def test_streaming_is_refused() -> None:
    class Streaming(BillAgent):
        async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
            agent = Agent(RecordedModel(FunctionModel(stream_function=_never), context))
            async with agent.run_stream("hi") as stream:
                await stream.get_output()
            return BillResult(status="unreachable", move_id=0)

    setup = Setup(AutonomyLevel.AUTONOMOUS, Streaming(ScriptedLLM()))
    execution = await setup.run()
    assert execution.status is S.FAILED
    assert execution.error is not None
    assert execution.error.type == "NotImplementedError"
    assert "not journaled" in execution.error.message


async def _never(
    messages: list[ModelMessage], info: AgentInfo
) -> AsyncIterator[str]:  # pragma: no cover
    yield "never"


@pytest.mark.parametrize("prefix", ["", "a:b"])
def test_recorded_model_prefix_is_validated(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        RecordedModel(FunctionModel(ScriptedLLM()), object(), prefix=prefix)  # type: ignore[arg-type]


def test_amount_decimal_from_float_is_exact() -> None:
    assert Decimal(repr(1250.5)) == Decimal("1250.5")


async def test_journal_entry_that_is_not_a_response_is_refused() -> None:
    request = ModelMessagesTypeAdapter.dump_python(
        [ModelRequest(parts=[UserPromptPart(content="hi")])], mode="json"
    )[0]

    class Tampered:
        execution_id = None

        async def record(self, action_id: str, *, fingerprint: object, compute: Any) -> Any:
            return request

    model = RecordedModel(FunctionModel(ScriptedLLM()), Tampered())  # type: ignore[arg-type]
    agent = Agent(model)
    with pytest.raises(ExecutionError, match="does not hold a model response"):
        await agent.run("hi")
