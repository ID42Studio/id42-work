"""Builders for MCP server tests."""

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
    """Read-only provider whose result can carry a sensitive key."""

    def __init__(self, result: JsonValue | None = None) -> None:
        self.calls = 0
        self.result: JsonValue = {"display_name": "Ada"} if result is None else result

    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name="directory.read", side_effecting=False)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        self.calls += 1
        return self.result


class Greet:
    async def execute(self, input_data: GreetingInput, context: ExecutionContext) -> GreetingOutput:
        person = await context.invoke(
            "directory.read", action_id="lookup", params={"name": input_data.name}
        )
        assert isinstance(person, dict)
        return GreetingOutput(message=f"Hello, {person['display_name']}!")


def hosted(
    *,
    registry: WorkRegistry,
    policies: StaticPolicyResolver,
    capabilities: CapabilityRegistry,
    grants: set[str],
    actor_id: str = "user:ada",
    tenant_id: str = "acme",
) -> tuple[WorkMcpServer, WorkRunner, ActorAuthority]:
    """A server whose actor is re-issued by the host on every call."""
    authority = ActorAuthority("mcp-test")
    runner = WorkRunner(
        registry=registry,
        policies=policies,
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    def issue() -> TrustedActor:
        return authority.issue(
            actor_id=actor_id,
            grants=grants,
            ttl=timedelta(hours=1),
            tenant_id=tenant_id,
        )

    server = WorkMcpServer(runner, authority=authority, issue_actor=issue)
    return server, runner, authority


def greeting_server(
    *,
    autonomy: AutonomyLevel = AutonomyLevel.AUTONOMOUS,
    grants: set[str] | None = None,
    result: JsonValue | None = None,
) -> tuple[WorkMcpServer, WorkRunner, Directory]:
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
    provider = Directory(result)
    capabilities = CapabilityRegistry()
    capabilities.add(provider)
    policy = Policy(
        id="greeting",
        version="1.0.0",
        autonomy=autonomy,
        allowed_capabilities=frozenset({"directory.read"}),
    )
    server, runner, _authority = hosted(
        registry=registry,
        policies=StaticPolicyResolver({("greet", "1.0.0"): policy}),
        capabilities=capabilities,
        grants={"directory.read"} if grants is None else grants,
    )
    return server, runner, provider


def server() -> WorkMcpServer:
    """Factory used by CLI tests (``mcp_support:server``)."""
    built, _runner, _provider = greeting_server()
    return built
