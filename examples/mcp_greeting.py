"""Serve a governed greeting over MCP.

The process that runs ``id42-work mcp serve`` is the host: it registers the work,
the policy and the actor. The MCP client never supplies an identity or an approval.

Run the execution once, without opening a transport:

    python examples/mcp_greeting.py

Serve it to a client on stdio (the default) or on loopback HTTP:

    id42-work mcp serve --factory examples.mcp_greeting:server
    id42-work mcp serve --factory examples.mcp_greeting:server --http --port 8766
"""

import asyncio
from collections.abc import Mapping
from datetime import timedelta

from pydantic import BaseModel, JsonValue

from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    MemoryStorage,
    Policy,
    StaticPolicyResolver,
    TrustedActor,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.mcp import WorkMcpServer


class GreetingInput(BaseModel):
    name: str


class GreetingOutput(BaseModel):
    message: str


class Directory:
    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name="directory.read", side_effecting=False)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        return {"display_name": str(params["name"]).title()}


class Greet:
    async def execute(self, input_data: GreetingInput, context: ExecutionContext) -> GreetingOutput:
        person = await context.invoke(
            "directory.read", action_id="lookup", params={"name": input_data.name}
        )
        assert isinstance(person, dict)
        return GreetingOutput(message=f"Hello, {person['display_name']}!")


def server() -> WorkMcpServer:
    """Host factory. The sealed actor is issued here, not by the MCP client."""
    work = Work(
        id="greet",
        version="1.0.0",
        name="Greet",
        input_schema=GreetingInput,
        output_schema=GreetingOutput,
        required_capabilities=frozenset({"directory.read"}),
    )
    registry = WorkRegistry()
    registry.register(work, executor=Greet())
    capabilities = CapabilityRegistry()
    capabilities.add(Directory())
    authority = ActorAuthority("greeting-host")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {
                ("greet", "1.0.0"): Policy(
                    id="greeting",
                    version="1.0.0",
                    autonomy=AutonomyLevel.AUTONOMOUS,
                    allowed_capabilities=frozenset({"directory.read"}),
                )
            }
        ),
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    def issue() -> TrustedActor:
        return authority.issue(
            actor_id="user:ada",
            grants={"directory.read"},
            ttl=timedelta(minutes=5),
        )

    return WorkMcpServer(runner, authority=authority, issue_actor=issue)


async def main() -> None:
    hosted = server()
    execution = await hosted.start_execution("greet", "1.0.0", {"name": "ada lovelace"})
    outcome = execution["outcome"]
    assert isinstance(outcome, dict)
    output = outcome["output"]
    assert isinstance(output, dict)
    print(execution["status"], output["message"])
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
