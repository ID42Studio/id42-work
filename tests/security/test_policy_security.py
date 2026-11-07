"""Attempts to obtain more authority than the trusted side granted (specification 6.3)."""

from datetime import timedelta

import pytest

from _factories import FixedClock, StaticCapabilities, make_policy, make_work
from id42_work import (
    ActorAuthority,
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    CapabilityRegistry,
    PolicyEngine,
    StaticPolicyResolver,
    TrustedActor,
    UntrustedActorError,
)

READ = "suppliers.read"
VALIDATE = "invoices.validate"
PAY = "payments.execute"
KEY: tuple[str, str] = ("supplier_invoice_validation", "1.0.0")


@pytest.fixture
def authority() -> ActorAuthority:
    return ActorAuthority("host-app", clock=FixedClock())


@pytest.fixture
def engine(authority: ActorAuthority) -> PolicyEngine:
    registry = CapabilityRegistry()
    registry.add(StaticCapabilities((READ, False), (VALIDATE, False), (PAY, True)))
    payment_gate = ApprovalRequirement(
        gate=ApprovalGate.ACTION, capability=PAY, reviewer_capability="approve:payments.execute"
    )
    policy = make_policy(
        autonomy=AutonomyLevel.GUIDED,
        allowed_capabilities=frozenset({READ, VALIDATE, PAY}),
        approval_requirements=(payment_gate,),
    )
    return PolicyEngine(
        resolver=StaticPolicyResolver({KEY: policy}),
        capabilities=registry,
        actor_authority=authority,
    )


def _actor(authority: ActorAuthority, *grants: str) -> TrustedActor:
    return authority.issue(actor_id="agent:7", grants=grants, ttl=timedelta(minutes=5))


def test_work_cannot_request_its_way_into_a_capability(
    engine: PolicyEngine, authority: ActorAuthority
) -> None:
    greedy = make_work(required_capabilities=frozenset({READ, VALIDATE, PAY}))
    decision = engine.evaluate(greedy, _actor(authority, READ, VALIDATE))
    assert not decision.allowed
    assert decision.reasons == ("missing_capability:payments.execute",)


def test_execution_receives_only_what_the_work_requires(
    engine: PolicyEngine, authority: ActorAuthority
) -> None:
    decision = engine.evaluate(make_work(), _actor(authority, READ, VALIDATE, PAY))
    assert decision.granted_capabilities == {READ, VALIDATE}
    assert PAY not in decision.granted_capabilities


def test_work_cannot_raise_autonomy_above_policy(
    engine: PolicyEngine, authority: ActorAuthority
) -> None:
    decision = engine.evaluate(
        make_work(autonomy_ceiling=AutonomyLevel.AUTONOMOUS), _actor(authority, READ, VALIDATE)
    )
    assert decision.effective_autonomy is AutonomyLevel.GUIDED
    assert decision.gate_for(ApprovalGate.OUTCOME) is not None


def test_policy_gates_survive_maximum_autonomy(authority: ActorAuthority) -> None:
    registry = CapabilityRegistry()
    registry.add(StaticCapabilities((PAY, True)))
    gate = ApprovalRequirement(
        gate=ApprovalGate.ACTION, capability=PAY, reviewer_capability="approve:payments.execute"
    )
    policy = make_policy(
        autonomy=AutonomyLevel.AUTONOMOUS,
        allowed_capabilities=frozenset({PAY}),
        approval_requirements=(gate,),
    )
    engine = PolicyEngine(
        resolver=StaticPolicyResolver({KEY: policy}),
        capabilities=registry,
        actor_authority=authority,
    )
    work = make_work(required_capabilities=frozenset({PAY}))
    decision = engine.evaluate(work, _actor(authority, PAY))
    assert decision.effective_autonomy is AutonomyLevel.AUTONOMOUS
    assert decision.gate_for(ApprovalGate.ACTION, PAY) == gate


def test_unregistered_capability_is_denied_even_when_granted(authority: ActorAuthority) -> None:
    policy = make_policy(allowed_capabilities=frozenset({"shell.exec"}))
    engine = PolicyEngine(
        resolver=StaticPolicyResolver({KEY: policy}),
        capabilities=CapabilityRegistry(),
        actor_authority=authority,
    )
    work = make_work(required_capabilities=frozenset({"shell.exec"}))
    decision = engine.evaluate(work, _actor(authority, "shell.exec"))
    assert decision.reasons == ("capability_unavailable:shell.exec",)


def test_approval_grants_are_not_capabilities(
    engine: PolicyEngine, authority: ActorAuthority
) -> None:
    work = make_work(required_capabilities=frozenset({PAY}))
    decision = engine.evaluate(work, _actor(authority, "approve:payments.execute"))
    assert not decision.allowed


@pytest.mark.parametrize(
    "candidate",
    [
        "agent:7",
        {"actor_id": "agent:7", "grants": [READ, VALIDATE]},
        None,
    ],
)
def test_identity_from_untrusted_input_is_refused(engine: PolicyEngine, candidate: object) -> None:
    with pytest.raises(UntrustedActorError):
        engine.evaluate(make_work(), candidate)


def test_escalated_or_foreign_actor_is_refused(
    engine: PolicyEngine, authority: ActorAuthority
) -> None:
    actor = _actor(authority, READ)
    escalated = actor.model_copy(update={"grants": frozenset({READ, VALIDATE})})
    with pytest.raises(UntrustedActorError):
        engine.evaluate(make_work(), escalated)
    foreign = ActorAuthority("host-app", clock=FixedClock()).issue(
        actor_id="agent:7", grants={READ, VALIDATE}, ttl=timedelta(minutes=5)
    )
    with pytest.raises(UntrustedActorError):
        engine.evaluate(make_work(), foreign)
