"""The smallest complete example: one read-only capability, no human gates.

Run: python examples/hello_work.py
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
    Work,
    WorkRegistry,
    WorkRunner,
)


class GreetingInput(BaseModel):
    name: str


class GreetingOutput(BaseModel):
    message: str


class Directory:
    """A capability provider: the only way the executor reaches the outside world."""

    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name="directory.read", side_effecting=False)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        return {"display_name": str(params["name"]).title()}


class GreetExecutor:
    async def execute(self, input_data: GreetingInput, context: ExecutionContext) -> GreetingOutput:
        person = await context.invoke(
            "directory.read", action_id="lookup", params={"name": input_data.name}
        )
        assert isinstance(person, dict)
        return GreetingOutput(message=f"Hello, {person['display_name']}!")


async def main() -> None:
    # 1. Define the work: a contract, with no code and no permissions.
    work = Work(
        id="greet",
        version="1.0.0",
        name="Greet a person",
        input_schema=GreetingInput,
        output_schema=GreetingOutput,
        required_capabilities=frozenset({"directory.read"}),
    )
    registry = WorkRegistry()
    registry.register(work, executor=GreetExecutor())

    # 2. The host decides what is allowed.
    policy = Policy(
        id="greeting_policy",
        version="1.0.0",
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({"directory.read"}),
    )
    capabilities = CapabilityRegistry()
    capabilities.add(Directory())
    authority = ActorAuthority("hello-app")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver({("greet", "1.0.0"): policy}),
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    # 3. The host authenticates the caller, then issues a sealed actor.
    actor = authority.issue(
        actor_id="user:ada", grants={"directory.read"}, ttl=timedelta(minutes=5)
    )

    # 4. Run and verify.
    execution = await runner.run(
        work_id="greet", version="1.0.0", input_data={"name": "ada lovelace"}, actor=actor
    )
    outcome = execution.raise_for_status()
    print(f"status:   {execution.status.value}")
    print(f"output:   {outcome.output.model_dump()}")
    print(f"verified: {outcome.verified}")
    print(f"events:   {[e.event_type.value for e in await runner.events(execution.execution_id)]}")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
