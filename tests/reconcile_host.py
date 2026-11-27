"""Factory for reconcile command tests.

Importable as ``reconcile_host:server``. A later call returns the same server, so
the command sees executions this process already started.
"""

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
    UncertainOutcomeError,
    Work,
    WorkRegistry,
    WorkRunner,
)
from id42_work.mcp import WorkMcpServer

ACTOR_ID = "id42-agent"
TENANT_ID = "local"
WORK_ID = "note"
WORK_VERSION = "1.0.0"
NOTES_APPEND = "notes.append"

_STATE: dict[str, object] = {}


class NoteInput(BaseModel):
    text: str


class NoteOutput(BaseModel):
    stored: bool


class Notes:
    """Side-effecting provider. The first append is uncertain; later ones succeed."""

    def __init__(self) -> None:
        self.calls = 0
        self._seen: set[object] = set()

    def capabilities(self) -> list[CapabilitySpec]:
        return [CapabilitySpec(name=NOTES_APPEND, side_effecting=True)]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        del capability
        self.calls += 1
        if call.operation_id not in self._seen:
            self._seen.add(call.operation_id)
            raise UncertainOutcomeError("the note store did not answer")
        return {"stored": True, "text": str(params["text"])}

    async def reconcile(self, call: CapabilityCall) -> str:
        """Present, so resume can probe, and unable to prove the write."""
        del call
        raise RuntimeError("the note store cannot be queried")


class Append:
    async def execute(self, input_data: NoteInput, context: ExecutionContext) -> NoteOutput:
        await context.invoke(
            NOTES_APPEND, action_id="append-note", params={"text": input_data.text}
        )
        return NoteOutput(stored=True)


def reset() -> None:
    """Drop the cached server so the next test starts from empty storage."""
    _STATE.clear()


def break_resume() -> None:
    """Make the cached host actor fail the next resume."""
    _STATE["broken_resume"] = True


def calls() -> int:
    """How many times the cached provider attempted an append."""
    provider = _STATE.get("provider")
    if isinstance(provider, Notes):
        return provider.calls
    return 0


def server() -> WorkMcpServer:
    """Host factory. The sealed execution actor is ``id42-agent`` and cannot reconcile."""
    cached = _STATE.get("server")
    if isinstance(cached, WorkMcpServer):
        return cached
    provider = Notes()
    registry = WorkRegistry()
    registry.register(
        Work(
            id=WORK_ID,
            version=WORK_VERSION,
            name="Append a note",
            input_schema=NoteInput,
            output_schema=NoteOutput,
            required_capabilities=frozenset({NOTES_APPEND}),
            autonomy_ceiling=AutonomyLevel.AUTONOMOUS,
        ),
        executor=Append(),
    )
    capabilities = CapabilityRegistry()
    capabilities.add(provider)
    authority = ActorAuthority("reconcile-host")
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {
                (WORK_ID, WORK_VERSION): Policy(
                    id="note",
                    version=WORK_VERSION,
                    autonomy=AutonomyLevel.AUTONOMOUS,
                    allowed_capabilities=frozenset({NOTES_APPEND}),
                )
            }
        ),
        capabilities=capabilities,
        actor_authority=authority,
        storage=MemoryStorage(),
    )

    def issue() -> TrustedActor:
        if _STATE.get("broken_resume") is True:
            return authority.issue(
                actor_id="id42-stranger",
                tenant_id=TENANT_ID,
                grants=frozenset(),
                ttl=timedelta(minutes=5),
            )
        return authority.issue(
            actor_id=ACTOR_ID,
            tenant_id=TENANT_ID,
            grants=frozenset({NOTES_APPEND}),
            ttl=timedelta(hours=1),
        )

    built = WorkMcpServer(runner, authority=authority, issue_actor=issue)
    _STATE["server"] = built
    _STATE["provider"] = provider
    return built


def not_a_server() -> None:
    """Factory that does not return a WorkMcpServer."""
