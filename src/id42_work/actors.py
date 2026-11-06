"""Trusted actors and the authority that issues them (specification 3.3 and 6.4).

Limitation: the seal protects against identities built from user input, deserialized
from requests or altered after issue. It does not protect against arbitrary Python code
running in the same process, which can read the authority's memory.
"""

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import NoReturn, Self

from pydantic import Field, model_validator

from id42_work._model import FrozenModel
from id42_work._time import Clock, UtcDatetime, to_utc, utc_now
from id42_work.canonical import canonical_json
from id42_work.exceptions import UntrustedActorError
from id42_work.identifiers import Grant, Identifier, NonEmptyStr


class TrustedActor(FrozenModel):
    """Identity on whose behalf work runs or approvals are decided.

    Obtain instances only from ``ActorAuthority.issue``. Instances built any other way
    carry no valid seal and are rejected by the runner.
    """

    actor_id: NonEmptyStr
    tenant_id: NonEmptyStr | None = None
    grants: frozenset[Grant] = frozenset()
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    authority_id: Identifier
    seal: bytes = Field(default=b"", repr=False, exclude=True)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.expires_at <= self.issued_at:
            msg = "expires_at must be after issued_at"
            raise ValueError(msg)
        return self

    def has_grant(self, grant: str) -> bool:
        """Return whether this actor holds ``grant``."""
        return grant in self.grants

    def sealed_payload(self) -> bytes:
        """Canonical bytes covered by the seal (every field except the seal)."""
        return canonical_json(self.model_dump(exclude={"seal"}))


class ActorAuthority:
    """Issues and verifies sealed ``TrustedActor`` instances.

    The sealing key is random, generated at construction and kept in memory only. An
    authority cannot be pickled, so the key is not persisted by accident.
    """

    __slots__ = ("_authority_id", "_clock", "_key", "_max_ttl")

    def __init__(
        self,
        authority_id: str,
        *,
        clock: Clock = utc_now,
        max_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        probe = _AuthorityId(authority_id=authority_id)
        if max_ttl <= timedelta(0):
            msg = "max_ttl must be positive"
            raise ValueError(msg)
        self._authority_id = probe.authority_id
        self._clock = clock
        self._max_ttl = max_ttl
        self._key = secrets.token_bytes(32)

    @property
    def authority_id(self) -> str:
        """Identifier recorded in every actor this authority issues."""
        return self._authority_id

    def issue(
        self,
        *,
        actor_id: str,
        ttl: timedelta,
        tenant_id: str | None = None,
        grants: Iterable[str] = (),
    ) -> TrustedActor:
        """Issue a sealed actor. Call only after the host authenticated the identity."""
        if ttl <= timedelta(0) or ttl > self._max_ttl:
            msg = f"ttl must be positive and at most {self._max_ttl}"
            raise ValueError(msg)
        now = to_utc(self._clock())
        unsealed = TrustedActor(
            actor_id=actor_id,
            tenant_id=tenant_id,
            grants=frozenset(grants),
            issued_at=now,
            expires_at=now + ttl,
            authority_id=self._authority_id,
        )
        return unsealed.model_copy(update={"seal": self._seal(unsealed)})

    def verify(self, actor: object, *, now: datetime | None = None) -> TrustedActor:
        """Return ``actor`` if it is sealed by this authority and currently valid.

        Raises ``UntrustedActorError`` otherwise.
        """
        if not isinstance(actor, TrustedActor):
            msg = f"expected a TrustedActor issued by an ActorAuthority, got {type(actor).__name__}"
            raise UntrustedActorError(msg)
        if actor.authority_id != self._authority_id:
            msg = "actor was issued by a different authority"
            raise UntrustedActorError(msg)
        if not hmac.compare_digest(actor.seal, self._seal(actor)):
            msg = "actor seal is invalid (forged or modified actor)"
            raise UntrustedActorError(msg)
        current = to_utc(now) if now is not None else to_utc(self._clock())
        if current < actor.issued_at:
            msg = "actor is not valid yet"
            raise UntrustedActorError(msg)
        if current >= actor.expires_at:
            msg = "actor has expired"
            raise UntrustedActorError(msg)
        return actor

    def is_trusted(self, actor: object, *, now: datetime | None = None) -> bool:
        """Return whether ``verify`` would accept ``actor``."""
        try:
            self.verify(actor, now=now)
        except UntrustedActorError:
            return False
        return True

    def _seal(self, actor: TrustedActor) -> bytes:
        return hmac.new(self._key, actor.sealed_payload(), hashlib.sha256).digest()

    def __repr__(self) -> str:
        return f"ActorAuthority(authority_id={self._authority_id!r})"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        msg = "ActorAuthority cannot be pickled: its sealing key must stay in memory"
        raise TypeError(msg)


class _AuthorityId(FrozenModel):
    authority_id: Identifier
