from datetime import timedelta
from typing import Any

import pytest

from _factories import FixedClock, StaticCapabilities, make_policy, make_work
from id42_work import (
    ActorAuthority,
    ApprovalGate,
    ApprovalRequirement,
    AutonomyLevel,
    CapabilityRegistry,
    Policy,
    PolicyDecision,
    PolicyEngine,
    StaticPolicyResolver,
    TrustedActor,
    UntrustedActorError,
    Work,
)
from id42_work.policy.engine import compute_gates, default_gates

READ = "suppliers.read"
VALIDATE = "invoices.validate"
POST = "odoo.invoice.post"
KEY: tuple[str, str] = ("supplier_invoice_validation", "1.0.0")


class Env:
    def __init__(self, policy: Policy | None, *, grants: set[str] | None = None) -> None:
        self.authority = ActorAuthority("host-app", clock=FixedClock())
        self.registry = CapabilityRegistry()
        self.registry.add(StaticCapabilities((READ, False), (VALIDATE, False), (POST, True)))
        policies = {KEY: policy} if policy else {}
        self.engine = PolicyEngine(
            resolver=StaticPolicyResolver(policies),
            capabilities=self.registry,
            actor_authority=self.authority,
        )
        self.actor = self.authority.issue(
            actor_id="user:42",
            tenant_id="acme",
            grants=grants if grants is not None else {READ, VALIDATE, POST},
            ttl=timedelta(minutes=10),
        )

    def evaluate(self, work: Work | None = None, **kwargs: Any) -> PolicyDecision:
        return self.engine.evaluate(work or make_work(), self.actor, **kwargs)


def test_allowed_decision() -> None:
    decision = Env(make_policy(autonomy=AutonomyLevel.GUIDED)).evaluate()
    assert decision.allowed
    assert decision.policy_id == "invoice_review"
    assert decision.granted_capabilities == {READ, VALIDATE}
    assert decision.effective_autonomy is AutonomyLevel.GUIDED
    assert decision.reasons == ()
    assert not decision.idempotency_key_required


def test_no_policy_denies() -> None:
    decision = Env(None).evaluate()
    assert not decision.allowed
    assert decision.reasons == ("no_policy",)


def test_every_missing_capability_is_reported() -> None:
    policy = make_policy(allowed_capabilities=frozenset({READ}))
    work = make_work(required_capabilities=frozenset({READ, VALIDATE, "ghost.read"}))
    env = Env(policy, grants={READ})
    decision = env.evaluate(work, constraints={READ, VALIDATE})
    assert not decision.allowed
    assert decision.reasons == (
        "missing_capability:ghost.read",
        "policy_disallows_capability:ghost.read",
        "constraint_excludes_capability:ghost.read",
        "capability_unavailable:ghost.read",
        "missing_capability:invoices.validate",
        "policy_disallows_capability:invoices.validate",
    )
    assert decision.policy_id == "invoice_review"
    assert decision.granted_capabilities == frozenset()


def test_constraints_narrow_but_never_widen() -> None:
    env = Env(make_policy())
    assert env.evaluate(constraints=None).allowed
    assert env.evaluate(constraints={READ, VALIDATE, POST}).allowed
    denied = env.evaluate(constraints={READ})
    assert denied.reasons == ("constraint_excludes_capability:invoices.validate",)


def test_work_without_capabilities_is_allowed_under_a_policy() -> None:
    decision = Env(make_policy()).evaluate(make_work(required_capabilities=frozenset()))
    assert decision.allowed
    assert decision.granted_capabilities == frozenset()


@pytest.mark.parametrize(
    ("policy_level", "ceiling", "expected"),
    [
        (AutonomyLevel.AUTONOMOUS, AutonomyLevel.AUTONOMOUS, AutonomyLevel.AUTONOMOUS),
        (AutonomyLevel.AUTONOMOUS, AutonomyLevel.SUPERVISED, AutonomyLevel.SUPERVISED),
        (AutonomyLevel.MANUAL, AutonomyLevel.AUTONOMOUS, AutonomyLevel.MANUAL),
        (AutonomyLevel.GUIDED, AutonomyLevel.GUIDED, AutonomyLevel.GUIDED),
    ],
)
def test_effective_autonomy_is_the_minimum(
    policy_level: AutonomyLevel, ceiling: AutonomyLevel, expected: AutonomyLevel
) -> None:
    decision = Env(make_policy(autonomy=policy_level)).evaluate(make_work(autonomy_ceiling=ceiling))
    assert decision.effective_autonomy is expected


@pytest.mark.parametrize(
    ("level", "gates"),
    [
        (AutonomyLevel.MANUAL, {ApprovalGate.START, ApprovalGate.ACTION, ApprovalGate.OUTCOME}),
        (AutonomyLevel.SUPERVISED, {ApprovalGate.ACTION}),
        (AutonomyLevel.GUIDED, {ApprovalGate.OUTCOME}),
        (AutonomyLevel.AUTONOMOUS, set()),
    ],
)
def test_default_gates_table(level: AutonomyLevel, gates: set[ApprovalGate]) -> None:
    assert default_gates(level) == gates


def _work_with_post() -> Work:
    return make_work(required_capabilities=frozenset({READ, POST}))


def _policy_with_post(**overrides: Any) -> Policy:
    return make_policy(allowed_capabilities=frozenset({READ, POST}), **overrides)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (
            AutonomyLevel.MANUAL,
            [(ApprovalGate.START, None), (ApprovalGate.ACTION, POST), (ApprovalGate.OUTCOME, None)],
        ),
        (AutonomyLevel.SUPERVISED, [(ApprovalGate.ACTION, POST)]),
        (AutonomyLevel.GUIDED, [(ApprovalGate.OUTCOME, None)]),
        (AutonomyLevel.AUTONOMOUS, []),
    ],
)
def test_gates_per_level_only_cover_side_effecting_capabilities(
    level: AutonomyLevel, expected: list[tuple[ApprovalGate, str | None]]
) -> None:
    decision = Env(_policy_with_post(autonomy=level)).evaluate(_work_with_post())
    assert [g.key for g in decision.required_gates] == expected
    for gate in decision.required_gates:
        assert gate.reviewer_capability == "approve:supplier_invoice_validation"
        assert gate.ttl == timedelta(hours=24)


def test_explicit_requirements_are_monotonic_and_override_defaults() -> None:
    payment = ApprovalRequirement(
        gate=ApprovalGate.ACTION,
        capability=POST,
        reviewer_capability="approve:odoo.invoice.post",
        reason_required=True,
        ttl=timedelta(hours=1),
    )
    review = ApprovalRequirement(gate=ApprovalGate.OUTCOME, reviewer_capability="approve:finance")
    for level in AutonomyLevel:
        policy = _policy_with_post(autonomy=level, approval_requirements=(payment, review))
        decision = Env(policy).evaluate(_work_with_post())
        assert decision.gate_for(ApprovalGate.ACTION, POST) == payment
        assert decision.gate_for(ApprovalGate.OUTCOME) == review
    assert Env(_policy_with_post()).evaluate(_work_with_post()).gate_for(ApprovalGate.START) is None


def test_requirements_for_unrequired_capabilities_are_ignored() -> None:
    unrelated = ApprovalRequirement(
        gate=ApprovalGate.ACTION, capability="payments.execute", reviewer_capability="approve:p"
    )
    decision = Env(make_policy(approval_requirements=(unrelated,))).evaluate()
    assert decision.required_gates == ()


def test_explicitly_named_read_capability_becomes_sensitive() -> None:
    requirement = ApprovalRequirement(
        gate=ApprovalGate.ACTION, capability=READ, reviewer_capability="approve:suppliers.read"
    )
    decision = Env(make_policy(approval_requirements=(requirement,))).evaluate()
    assert decision.gate_for(ApprovalGate.ACTION, READ) == requirement


def test_gate_order_is_deterministic() -> None:
    env = Env(None)
    work = make_work(required_capabilities=frozenset({POST, "z.write", "a.write"}))
    policy = make_policy(autonomy=AutonomyLevel.MANUAL)
    gates = compute_gates(work, policy, AutonomyLevel.MANUAL, env.registry)
    assert [g.key for g in gates] == [
        (ApprovalGate.START, None),
        (ApprovalGate.ACTION, "a.write"),
        (ApprovalGate.ACTION, POST),
        (ApprovalGate.ACTION, "z.write"),
        (ApprovalGate.OUTCOME, None),
    ]


@pytest.mark.parametrize(
    ("override", "work_caps", "expected"),
    [
        (None, {READ}, False),
        (None, {READ, POST}, True),
        (True, {READ}, True),
        (False, {READ, POST}, False),
    ],
)
def test_idempotency_key_requirement(
    override: bool | None, work_caps: set[str], expected: bool
) -> None:
    policy = _policy_with_post(require_idempotency_key=override)
    decision = Env(policy).evaluate(make_work(required_capabilities=frozenset(work_caps)))
    assert decision.allowed
    assert decision.idempotency_key_required is expected


def test_untrusted_actor_raises_instead_of_deciding() -> None:
    env = Env(make_policy())
    with pytest.raises(UntrustedActorError):
        env.engine.evaluate(make_work(), "user:42")
    with pytest.raises(UntrustedActorError, match="expired"):
        env.engine.evaluate(make_work(), env.actor, now=env.actor.expires_at)


class RaisingResolver:
    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None:
        raise RuntimeError("policy store unavailable")


class WrongTypeResolver:
    def resolve(self, work: Work, actor: TrustedActor) -> Policy | None:
        return {"autonomy": "AUTONOMOUS"}  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("resolver", "error_type"),
    [(RaisingResolver(), "RuntimeError"), (WrongTypeResolver(), "TypeError")],
)
def test_resolver_errors_deny(
    resolver: Any, error_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    env = Env(None)
    engine = PolicyEngine(
        resolver=resolver, capabilities=env.registry, actor_authority=env.authority
    )
    decision = engine.evaluate(make_work(), env.actor)
    assert not decision.allowed
    assert decision.reasons == ("policy_error",)
    assert decision.error is not None
    assert decision.error.type == error_type
    assert "policy evaluation failed" in caplog.text


def test_decision_shape_invariants() -> None:
    from id42_work import ExecutionErrorInfo  # noqa: PLC0415

    error = ExecutionErrorInfo(type="RuntimeError", message="x")
    with pytest.raises(ValueError, match="evaluation error"):
        PolicyDecision(
            allowed=True,
            effective_autonomy=AutonomyLevel.MANUAL,
            policy_id="p",
            policy_version="1.0.0",
            error=error,
        )
    with pytest.raises(ValueError, match="idempotency"):
        PolicyDecision(allowed=False, reasons=("x",), idempotency_key_required=True)
