"""An AI agent as a governed executor (PydanticAI integration).

The agent reads the supplier, drafts the bill and posts it, choosing each step itself.
The SDK governs it like any executor:

- its tools are only the capabilities the execution was granted;
- posting waits for finance approval (an explicit policy requirement, even AUTONOMOUS);
- every model response is journaled, so after the approval the agent is replayed from
  its recorded turns instead of asking the model again, and continues to the post.

The model here is scripted so the example runs offline, without an API key. With a real
model, replace ``FunctionModel(ScriptedBillModel())`` by a model name such as
``"anthropic:claude-sonnet-5-5"`` and install the provider's PydanticAI extra.

Run: python examples/agent_invoice.py   (needs: pip install "id42-work[pydantic-ai]")
"""

import asyncio
import os
from datetime import timedelta

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from id42_work import (
    ActorAuthority,
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    CapabilityRegistry,
    ExecutionContext,
    ExecutionStatus,
    MemoryStorage,
    Policy,
    StaticPolicyResolver,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.integrations.pydantic_ai import RecordedModel, capability_toolset
from id42_work.testing import (
    INVOICE_POST,
    INVOICE_PREPARE,
    PARTNER_READ,
    FakeOdooProvider,
    ManualClock,
)


class BillRequest(BaseModel):
    invoice_number: str
    supplier: str
    amount: str
    currency: str


class BillResult(BaseModel):
    move_id: int
    state: str
    summary: str


class ScriptedBillModel:
    """Stands in for an LLM: decides the next tool call from the conversation so far."""

    def __init__(self) -> None:
        self.requests = 0

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        done = {
            part.tool_name: part.content
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart)
        }
        if "odoo_partner_read" not in done:
            return _call("odoo_partner_read", {"ref": "SUP-001"}, "call-read")
        if "odoo_invoice_prepare" not in done:
            args = {
                "partner_ref": "SUP-001",
                "invoice_number": "INV-1001",
                "amount": 1250.0,  # models send JSON numbers; the SDK turns them into Decimal
                "currency": "BRL",
            }
            return _call("odoo_invoice_prepare", args, "call-draft")
        move = done["odoo_invoice_prepare"]
        assert isinstance(move, dict)
        if "odoo_invoice_post" not in done:
            return _call("odoo_invoice_post", {"move_id": move["move_id"]}, "call-post")
        posted = done["odoo_invoice_post"]
        assert isinstance(posted, dict)
        (final,) = info.output_tools
        result = {"move_id": posted["move_id"], "state": posted["state"], "summary": posted["name"]}
        return _call(final.name, result, "call-final")


def check(condition: bool, message: str) -> None:
    """Fail the example (exit code 1) if an expectation does not hold."""
    if not condition:
        raise SystemExit(f"FAILED: {message}")


def _call(tool: str, args: dict[str, object], call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args, tool_call_id=call_id)])


class BillAgent:
    """The executor: builds the agent around the execution context on every run."""

    def __init__(self, model: ScriptedBillModel) -> None:
        self.model = model

    async def execute(self, input_data: BillRequest, context: ExecutionContext) -> BillResult:
        agent = Agent(
            RecordedModel(FunctionModel(self.model), context),
            toolsets=[capability_toolset(context)],
            output_type=BillResult,
            instructions="You post supplier bills in the ERP. Check the supplier first.",
        )
        prompt = (
            f"Post bill {input_data.invoice_number} from {input_data.supplier}: "
            f"{input_data.amount} {input_data.currency}."
        )
        return (await agent.run(prompt)).output


async def main() -> None:
    print("AI agent posting a bill")
    model = ScriptedBillModel()
    odoo = FakeOdooProvider()
    capabilities = CapabilityRegistry()
    capabilities.add(odoo)
    registry = WorkRegistry()
    registry.register(
        Work(
            id="post_bill_agent",
            version="1.0.0",
            name="Post a supplier bill with an agent",
            input_schema=BillRequest,
            output_schema=BillResult,
            required_capabilities=frozenset({PARTNER_READ, INVOICE_PREPARE, INVOICE_POST}),
        ),
        executor=BillAgent(model),
    )
    finance_approves_posts = ApprovalRequirement(
        gate=ApprovalGate.ACTION,
        capability=INVOICE_POST,
        reviewer_capability="approve:odoo.invoice.post",
        reason_required=True,
    )
    policy = Policy(
        id="agent_bills",
        version="1.0.0",
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({PARTNER_READ, INVOICE_PREPARE, INVOICE_POST}),
        approval_requirements=(finance_approves_posts,),
    )
    clock = ManualClock()
    authority = ActorAuthority("agent-app", clock=clock)
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver({("post_bill_agent", "1.0.0"): policy}),
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
        clock=clock,
    )

    def issue(actor_id: str, grants: set[str]):  # noqa: ANN202
        return authority.issue(
            actor_id=actor_id, tenant_id="acme", grants=grants, ttl=timedelta(hours=1)
        )

    clerk = issue("clerk:ana", {PARTNER_READ, INVOICE_PREPARE, INVOICE_POST})
    finance = issue("finance:bruno", {"approve:odoo.invoice.post"})

    waiting = await runner.run(
        work_id="post_bill_agent",
        version="1.0.0",
        input_data={
            "invoice_number": "INV-1001",
            "supplier": "SUP-001",
            "amount": "1250.00",
            "currency": "BRL",
        },
        actor=clerk,
        idempotency_key="bill-INV-1001",
    )
    print(f"- agent ran until the post: {waiting.status.value} ({waiting.status_reason})")
    print(
        f"    model requests so far: {model.requests}, drafts: {odoo.count(INVOICE_PREPARE)}, "
        f"posts: {odoo.count(INVOICE_POST)}"
    )
    check(waiting.status is ExecutionStatus.WAITING_APPROVAL, "the post must wait for finance")
    (approval,) = await runner.approvals.list_pending(waiting.execution_id)
    print(f"    finance must approve {approval.capability} {approval.params_preview}")

    await runner.approvals.approve(
        approval.approval_id, reviewer=finance, reason="matched PO-778 and goods receipt"
    )
    done = await runner.resume(waiting.execution_id, actor=clerk)
    outcome = done.raise_for_status()
    print(f"- approved and resumed: {done.status.value} -> {outcome.output.model_dump()}")
    print(f"    model requests in total: {model.requests} (recorded turns were replayed)")
    print(f"    drafts: {odoo.count(INVOICE_PREPARE)}, posts: {odoo.count(INVOICE_POST)}")
    check(model.requests == 4, "3 recorded turns replayed, 1 new model request")
    check(odoo.count(INVOICE_PREPARE) == 1, "the draft is replayed, not created twice")
    check(odoo.count(INVOICE_POST) == 1, "exactly one post")
    journal = await runner.journal(done.execution_id)
    print(f"    journal: {[(e.action_id, e.capability) for e in journal]}")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
