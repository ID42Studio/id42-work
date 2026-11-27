"""Trusted actors and the authority that issues them (specification 3.3 and 6.4).

Limitation: the seal protects against identities built from user input, deserialized
from requests or altered after issue. It does not protect against arbitrary Python code
running in the same process, which can read the authority's memory. A key file keeps
the same keys across restarts; it does not hide them from that process.
"""

import base64
import hmac
import json
import os
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import NoReturn, Self, cast

from pydantic import Field, ValidationError, model_validator

from id42_work._model import FrozenModel
from id42_work._time import Clock, UtcDatetime, to_utc, utc_now
from id42_work.authority_keys import (
    AuthorityKeyStore,
    AuthoritySigningKey,
    FileAuthorityKeyStore,
    current_signing_key,
    new_signing_key,
)
from id42_work.canonical import canonical_json
from id42_work.exceptions import AuthorityKeyError, UntrustedActorError
from id42_work.identifiers import Grant, Identifier, NonEmptyStr

_SEAL_LENGTH = 32
_CREDENTIAL_PREFIX = "id42a1."
_MAX_CREDENTIAL = 16_384
_CREDENTIAL_FIELDS = frozenset(
    {"actor_id", "authority_id", "expires_at", "grants", "issued_at", "seal", "tenant_id"}
)


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

    def to_credential(self) -> str:
        """Opaque token for this actor. It carries the seal and not the signing key.

        Another process can parse it with ``from_credential`` and trust it only
        after ``ActorAuthority.verify``. The token is not the JSON form of the
        model: that form omits the seal on purpose.
        """
        if len(self.seal) != _SEAL_LENGTH:
            msg = "actor credential is invalid"
            raise ValueError(msg)
        body = {
            "actor_id": self.actor_id,
            "authority_id": self.authority_id,
            "expires_at": self.expires_at.isoformat(),
            "grants": sorted(self.grants),
            "issued_at": self.issued_at.isoformat(),
            "seal": base64.b64encode(self.seal).decode("ascii"),
            "tenant_id": self.tenant_id,
        }
        encoded = base64.urlsafe_b64encode(canonical_json(body)).decode("ascii").rstrip("=")
        return _CREDENTIAL_PREFIX + encoded

    @classmethod
    def from_credential(cls, credential: object) -> Self:
        """Parse a token from ``to_credential``. The result is not trusted yet."""
        if not isinstance(credential, str):
            msg = "actor credential is invalid"
            raise ValueError(msg)
        text = credential.strip()
        if not text.startswith(_CREDENTIAL_PREFIX) or len(text) > _MAX_CREDENTIAL:
            msg = "actor credential is invalid"
            raise ValueError(msg)
        raw = _decode_credential(text[len(_CREDENTIAL_PREFIX) :])
        try:
            parsed: object = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            msg = "actor credential is invalid"
            raise ValueError(msg)
        body = cast("dict[str, object]", parsed)
        if frozenset(body) != _CREDENTIAL_FIELDS:
            msg = "actor credential is invalid"
            raise ValueError(msg)
        try:
            tenant_id = body["tenant_id"]
            return cls(
                actor_id=_credential_text(body["actor_id"]),
                tenant_id=None if tenant_id is None else _credential_text(tenant_id),
                grants=frozenset(_credential_grants(body["grants"])),
                issued_at=_credential_time(body["issued_at"]),
                expires_at=_credential_time(body["expires_at"]),
                authority_id=_credential_text(body["authority_id"]),
                seal=_credential_seal(body["seal"]),
            )
        except (TypeError, ValueError, ValidationError):
            msg = "actor credential is invalid"
            raise ValueError(msg) from None


class ActorAuthority:
    """Issues and verifies sealed ``TrustedActor`` instances.

    With no key file, the sealing key is random and lives only in this process.
    Pass ``key_path`` (or ``key_store``) to load the same keys after a restart.
    ``create_key_file`` writes a new file when the path is missing; otherwise a
    missing file is an error. The constructor does not read the environment.

    ``rotate_keys`` retires the current key. A retired key still verifies actors
    it sealed at or before retirement, for at most ``max_ttl``. An authority
    cannot be pickled.
    """

    __slots__ = (
        "_authority_id",
        "_by_id",
        "_clock",
        "_current_id",
        "_lock",
        "_max_ttl",
        "_store",
    )

    def __init__(
        self,
        authority_id: str,
        *,
        clock: Clock = utc_now,
        max_ttl: timedelta = timedelta(hours=24),
        key_path: str | os.PathLike[str] | None = None,
        key_store: AuthorityKeyStore | None = None,
        create_key_file: bool = False,
    ) -> None:
        probe = _AuthorityId(authority_id=authority_id)
        if max_ttl <= timedelta(0):
            msg = "max_ttl must be positive"
            raise ValueError(msg)
        if key_path is not None and key_store is not None:
            msg = "pass key_path or key_store, not both"
            raise ValueError(msg)
        supplied = None if key_store is None else _require_store(key_store)
        if create_key_file and key_path is None and supplied is None:
            msg = "create_key_file requires key_path or key_store"
            raise ValueError(msg)
        self._authority_id = probe.authority_id
        self._clock = clock
        self._max_ttl = max_ttl
        self._lock = threading.Lock()
        if key_path is not None:
            self._store: AuthorityKeyStore | None = FileAuthorityKeyStore(
                key_path, probe.authority_id
            )
        else:
            self._store = supplied
        if self._store is None:
            self._install((new_signing_key(to_utc(clock())),))
        else:
            self._open(self._store, create=create_key_file)

    @property
    def authority_id(self) -> str:
        """Identifier recorded in every actor this authority issues."""
        return self._authority_id

    @property
    def signing_key_id(self) -> str:
        """Id of the key that signs new actors. Not a secret."""
        with self._lock:
            self._reload()
            return self._current_id

    def rotate_keys(self) -> str:
        """Retire the current key and return the id of the key that now signs.

        Actors already sealed by the retired key still verify when they were
        issued at or before retirement and their lifetime is within ``max_ttl``.
        An in-memory authority has no file to update.
        """
        if self._store is None:
            raise AuthorityKeyError(
                "an in-memory authority has no durable key to rotate",
                reason="memory",
            )
        with self._lock:
            keys = self._store.rotate(to_utc(self._clock()), max_ttl=self._max_ttl)
            self._install(keys)
            return self._current_id

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
        with self._lock:
            self._reload()
            seal = self._by_id[self._current_id].sign(unsealed.sealed_payload())
        return unsealed.model_copy(update={"seal": seal})

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
        with self._lock:
            self._reload()
            if not self._seal_ok(actor):
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

    def _open(self, store: AuthorityKeyStore, *, create: bool) -> None:
        try:
            keys = store.load()
        except AuthorityKeyError as exc:
            if exc.reason != "missing" or not create:
                raise
            fresh = new_signing_key(to_utc(self._clock()))
            try:
                store.save((fresh,), exclusive=True)
            except AuthorityKeyError as created:
                if created.reason != "exists":
                    raise
                keys = store.load()
            else:
                keys = (fresh,)
        self._install(keys)

    def _reload(self) -> None:
        """Load the key file. Caller holds ``_lock``."""
        if self._store is None:
            return
        self._install(self._store.load())

    def _install(self, keys: tuple[AuthoritySigningKey, ...]) -> None:
        current = current_signing_key(keys)
        self._by_id = {key.key_id: key for key in keys}
        self._current_id = current.key_id

    def _seal_ok(self, actor: TrustedActor) -> bool:
        payload = actor.sealed_payload()
        ok = False
        for key in self._by_id.values():
            mac_ok = hmac.compare_digest(actor.seal, key.sign(payload))
            window_ok = self._signed_by(key, actor)
            ok |= mac_ok and window_ok
        return ok

    def _signed_by(self, key: AuthoritySigningKey, actor: TrustedActor) -> bool:
        if actor.expires_at - actor.issued_at > self._max_ttl:
            return False
        if key.retired_at is None:
            return True
        return actor.issued_at <= key.retired_at

    def __repr__(self) -> str:
        return f"ActorAuthority(authority_id={self._authority_id!r})"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        msg = "ActorAuthority cannot be pickled"
        raise TypeError(msg)


def _require_store(value: object) -> AuthorityKeyStore:
    if not isinstance(value, AuthorityKeyStore):
        msg = "key_store must be an AuthorityKeyStore"
        raise TypeError(msg)
    return value


def _decode_credential(body: str) -> bytes:
    padding = "=" * (-len(body) % 4)
    try:
        return base64.urlsafe_b64decode(body + padding)
    except ValueError:
        return b""


def _credential_text(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        msg = "actor credential is invalid"
        raise ValueError(msg)
    return raw


def _credential_grants(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        msg = "actor credential is invalid"
        raise ValueError(msg)
    grants: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str):
            msg = "actor credential is invalid"
            raise ValueError(msg)
        grants.append(item)
    return tuple(grants)


def _credential_time(raw: object) -> datetime:
    if not isinstance(raw, str):
        msg = "actor credential is invalid"
        raise ValueError(msg)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is None:
        msg = "actor credential is invalid"
        raise ValueError(msg)
    return to_utc(parsed)


def _credential_seal(raw: object) -> bytes:
    seal = b""
    if isinstance(raw, str):
        try:
            seal = base64.b64decode(raw, validate=True)
        except ValueError:
            seal = b""
    if len(seal) != _SEAL_LENGTH:
        msg = "actor credential is invalid"
        raise ValueError(msg)
    return seal


class _AuthorityId(FrozenModel):
    authority_id: Identifier
