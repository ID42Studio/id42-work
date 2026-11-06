import pickle
from datetime import datetime, timedelta

import pytest
from _factories import T0
from pydantic import ValidationError

from id42_work import ActorAuthority, TrustedActor, UntrustedActorError


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def authority(clock: FakeClock) -> ActorAuthority:
    return ActorAuthority("host-app", clock=clock)


def _issue(authority: ActorAuthority) -> TrustedActor:
    return authority.issue(
        actor_id="user:42",
        tenant_id="acme",
        grants={"suppliers.read", "approve:payments.execute"},
        ttl=timedelta(minutes=15),
    )


def test_issued_actor_verifies(authority: ActorAuthority) -> None:
    actor = _issue(authority)
    assert authority.verify(actor) is actor
    assert authority.is_trusted(actor)
    assert actor.has_grant("suppliers.read")
    assert actor.expires_at - actor.issued_at == timedelta(minutes=15)


@pytest.mark.parametrize("candidate", ["user:42", {"actor_id": "user:42"}, None, 42])
def test_non_actor_values_are_untrusted(authority: ActorAuthority, candidate: object) -> None:
    with pytest.raises(UntrustedActorError, match="expected a TrustedActor"):
        authority.verify(candidate)


def test_self_built_actor_is_untrusted(authority: ActorAuthority) -> None:
    forged = TrustedActor(
        actor_id="user:42",
        grants=frozenset({"payments.execute"}),
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=5),
        authority_id="host-app",
        seal=b"\x00" * 32,
    )
    with pytest.raises(UntrustedActorError, match="seal"):
        authority.verify(forged)


def test_escalated_grants_break_the_seal(authority: ActorAuthority) -> None:
    actor = _issue(authority)
    escalated = actor.model_copy(update={"grants": actor.grants | {"payments.execute"}})
    with pytest.raises(UntrustedActorError, match="seal"):
        authority.verify(escalated)


def test_extended_expiry_breaks_the_seal(authority: ActorAuthority) -> None:
    actor = _issue(authority)
    extended = actor.model_copy(update={"expires_at": actor.expires_at + timedelta(days=1)})
    assert not authority.is_trusted(extended)


def test_deserialized_actor_has_no_seal(authority: ActorAuthority) -> None:
    actor = _issue(authority)
    assert "seal" not in actor.model_dump()
    restored = TrustedActor.model_validate_json(actor.model_dump_json())
    assert not authority.is_trusted(restored)


def test_other_authority_with_same_id_is_rejected(clock: FakeClock) -> None:
    first = ActorAuthority("host-app", clock=clock)
    second = ActorAuthority("host-app", clock=clock)
    with pytest.raises(UntrustedActorError, match="seal"):
        second.verify(_issue(first))
    other = ActorAuthority("other-app", clock=clock)
    with pytest.raises(UntrustedActorError, match="different authority"):
        other.verify(_issue(first))


def test_validity_window(authority: ActorAuthority, clock: FakeClock) -> None:
    actor = _issue(authority)
    with pytest.raises(UntrustedActorError, match="not valid yet"):
        authority.verify(actor, now=T0 - timedelta(seconds=1))
    clock.now = T0 + timedelta(minutes=15)
    with pytest.raises(UntrustedActorError, match="expired"):
        authority.verify(actor)


def test_ttl_bounds(authority: ActorAuthority) -> None:
    with pytest.raises(ValueError, match="ttl"):
        authority.issue(actor_id="a", ttl=timedelta(0))
    with pytest.raises(ValueError, match="ttl"):
        authority.issue(actor_id="a", ttl=timedelta(days=2))
    with pytest.raises(ValueError, match="max_ttl"):
        ActorAuthority("x", max_ttl=timedelta(0))
    with pytest.raises(ValidationError):
        ActorAuthority("Not Valid")


def test_secrets_do_not_leak(authority: ActorAuthority) -> None:
    actor = _issue(authority)
    assert "seal" not in repr(actor)
    assert repr(authority) == "ActorAuthority(authority_id='host-app')"
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(authority)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TrustedActor(
            actor_id="a",
            issued_at=datetime(2026, 1, 1),
            expires_at=datetime(2026, 1, 2),
            authority_id="host-app",
        )
    with pytest.raises(ValidationError, match="after issued_at"):
        TrustedActor(actor_id="a", issued_at=T0, expires_at=T0, authority_id="host-app")


def test_default_clock_is_utc() -> None:
    authority = ActorAuthority("host-app")
    assert authority.authority_id == "host-app"
    actor = authority.issue(actor_id="a", ttl=timedelta(minutes=1))
    assert actor.issued_at.utcoffset() == timedelta(0)
    assert authority.is_trusted(actor)
