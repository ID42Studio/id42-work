from datetime import timedelta

from _factories import FixedClock, make_policy, make_work
from id42_work import ActorAuthority, Policy, StaticPolicyResolver


def _actor(tenant: str | None):
    return ActorAuthority("host-app", clock=FixedClock()).issue(
        actor_id="user:1", tenant_id=tenant, ttl=timedelta(minutes=5)
    )


def test_default_and_tenant_policies() -> None:
    default = make_policy()
    acme = make_policy(id="acme_review")
    key = ("supplier_invoice_validation", "1.0.0")
    policies: dict[tuple[str, str], Policy] = {key: default}
    resolver = StaticPolicyResolver(policies, tenant_policies={("acme", *key): acme})
    work = make_work()
    assert resolver.resolve(work, _actor("acme")) is acme
    assert resolver.resolve(work, _actor("globex")) is default
    assert resolver.resolve(work, _actor(None)) is default
    assert resolver.resolve(make_work(version="2.0.0"), _actor("acme")) is None
    policies.clear()  # mappings are copied at construction
    assert resolver.resolve(work, _actor(None)) is default
