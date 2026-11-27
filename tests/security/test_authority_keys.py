"""Durable authority keys: restart, permissions, bad files and rotation."""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest

from _factories import T0, FixedClock, make_policy, make_work
from _runtime import INVOICE, InvoiceExecutor, SupplierProvider
from id42_work import (
    ActorAuthority,
    ApprovalStatus,
    AuthorityKeyError,
    AuthorityKeyStore,
    AuthoritySigningKey,
    AutonomyLevel,
    CapabilityRegistry,
    ExecutionStatus,
    FileAuthorityKeyStore,
    SqliteStorage,
    StaticPolicyResolver,
    TrustedActor,
    UntrustedActorError,
    WorkRegistry,
    WorkRunner,
    authority_key_path_from_env,
)
from id42_work.authority_keys import _MAX_BYTES, _MAX_KEYS, AUTHORITY_KEY_ENV
from id42_work.identifiers import approve_grant

_MARKER = "MATERIAL-THAT-MUST-NOT-APPEAR"
_WORK = "supplier_invoice_validation"
_REVIEWER_GRANT = approve_grant(_WORK)
_ACTOR_GRANTS = ("suppliers.read", "invoices.validate")


class MemoryKeyStore(AuthorityKeyStore):
    """In-memory stand-in used to force create races."""

    def __init__(self) -> None:
        self.keys: tuple[AuthoritySigningKey, ...] | None = None
        self.fail_exclusive = False
        self.winner: tuple[AuthoritySigningKey, ...] | None = None
        self.save_reason: str | None = None

    def load(self) -> tuple[AuthoritySigningKey, ...]:
        if self.keys is None:
            raise AuthorityKeyError("missing", reason="missing")
        return self.keys

    def save(self, keys: tuple[AuthoritySigningKey, ...], *, exclusive: bool = False) -> None:
        if self.save_reason is not None:
            raise AuthorityKeyError("save failed", reason=self.save_reason)
        if exclusive and (self.fail_exclusive or self.keys is not None):
            if self.winner is not None:
                self.keys = self.winner
            raise AuthorityKeyError("exists", reason="exists")
        self.keys = keys

    def rotate(self, now: datetime, *, max_ttl: timedelta) -> tuple[AuthoritySigningKey, ...]:
        del now, max_ttl
        raise AuthorityKeyError("memory", reason="memory")


def _signing_key(key_id: str = "ab" * 8) -> AuthoritySigningKey:
    return AuthoritySigningKey(
        key_id=key_id,
        secret=bytes(range(32)),
        created_at=T0,
        retired_at=None,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _rewrite(path: Path, mutate: Callable[[dict[str, object]], None]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def _private_bytes(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(0o600)


def _open(path: Path, *, create: bool = False, **kwargs: object) -> ActorAuthority:
    return ActorAuthority("host-app", key_path=path, create_key_file=create, **kwargs)  # type: ignore[arg-type]


def _issue(authority: ActorAuthority, actor_id: str = "user:42") -> TrustedActor:
    return authority.issue(
        actor_id=actor_id,
        tenant_id="acme",
        grants={"suppliers.read", _REVIEWER_GRANT},
        ttl=timedelta(minutes=15),
    )


def _materials(path: Path) -> list[tuple[str, bytes, str | None]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    found: list[tuple[str, bytes, str | None]] = []
    for item in raw["keys"]:
        found.append((item["key_id"], base64.b64decode(item["secret"]), item["retired_at"]))
    return found


def _forge(material: bytes, **fields: object) -> TrustedActor:
    unsigned = TrustedActor(seal=b"\x00" * 32, **fields)  # type: ignore[arg-type]
    digest = hmac.new(material, unsigned.sealed_payload(), hashlib.sha256).digest()
    return unsigned.model_copy(update={"seal": digest})


def test_default_authority_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "keys.json"
    monkeypatch.setenv(AUTHORITY_KEY_ENV, str(path))
    authority = ActorAuthority("host-app")
    assert not path.exists()
    actor = _issue(authority)
    other = ActorAuthority("host-app", key_path=path, create_key_file=True)
    assert not other.is_trusted(actor)
    assert "seal" not in repr(authority)
    assert authority.signing_key_id != other.signing_key_id


def test_env_path_is_explicit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(AUTHORITY_KEY_ENV, raising=False)
    assert authority_key_path_from_env() is None
    assert authority_key_path_from_env({}) is None
    assert authority_key_path_from_env({AUTHORITY_KEY_ENV: "   "}) is None
    with pytest.raises(AuthorityKeyError, match="absolute") as info:
        authority_key_path_from_env({AUTHORITY_KEY_ENV: "keys.json"})
    assert info.value.reason == "relative"
    chosen = tmp_path / "keys.json"
    assert authority_key_path_from_env({AUTHORITY_KEY_ENV: str(chosen)}) == chosen


def test_new_instance_loads_the_same_keys(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    old = os.umask(0)
    try:
        first = _open(path, create=True)
        actor = _issue(first)
        key_id = first.signing_key_id
    finally:
        os.umask(old)
    assert _mode(path) == 0o600
    assert _mode(Path(str(path) + ".lock")) == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []
    second = _open(path)
    assert second.signing_key_id == key_id
    assert second.verify(actor) is actor
    assert second.verify(actor).has_grant(_REVIEWER_GRANT)
    third = _open(path, create=True)
    assert third.signing_key_id == key_id
    assert third.is_trusted(actor)


def test_missing_wrong_and_foreign_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(AuthorityKeyError, match="does not exist") as absent:
        _open(missing)
    assert absent.value.reason == "missing"
    nested = tmp_path / "absent" / "keys.json"
    with pytest.raises(AuthorityKeyError, match="directory") as directory:
        _open(nested, create=True)
    assert directory.value.reason == "missing_directory"
    with pytest.raises(AuthorityKeyError, match="different authority") as foreign:
        ActorAuthority("other-app", key_path=_created(tmp_path / "one.json"))
    assert foreign.value.reason == "mismatch"
    _private_bytes(tmp_path / "bad.json", b"\xff\xfe not json")
    with pytest.raises(AuthorityKeyError, match="not valid") as corrupt:
        _open(tmp_path / "bad.json")
    assert corrupt.value.reason == "corrupt"
    rendered = "".join(traceback.format_exception(corrupt.value))
    assert _MARKER not in rendered
    assert corrupt.value.__cause__ is None


def test_corrupt_json_does_not_echo_key_material(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    _private_bytes(path, f'{{"secret":"{_MARKER}",'.encode())
    with pytest.raises(AuthorityKeyError, match="not valid") as info:
        _open(path)
    rendered = "".join(traceback.format_exception(info.value))
    assert _MARKER not in rendered
    assert info.value.__cause__ is None
    assert info.value.reason == "corrupt"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.__setitem__("version", 2),
        lambda data: data.__setitem__("version", True),
        lambda data: data.__setitem__("authority_id", 1),
        lambda data: data.__setitem__("extra", 1),
        lambda data: data.__setitem__("keys", {}),
        lambda data: data.__setitem__("keys", []),
        lambda data: data.__setitem__("keys", [1]),
        lambda data: data["keys"][0].__setitem__("key_id", "ZZ"),
        lambda data: data["keys"][0].__setitem__("key_id", 1),
        lambda data: data["keys"][0].__setitem__("secret", 1),
        lambda data: data["keys"][0].__setitem__("secret", "aaaa"),
        lambda data: data["keys"][0].__setitem__("secret", base64.b64encode(b"short").decode()),
        lambda data: data["keys"][0].__setitem__("secret", "!!!!"),
        lambda data: data["keys"][0].__setitem__("created_at", "yesterday"),
        lambda data: data["keys"][0].__setitem__("created_at", 1),
        lambda data: data["keys"][0].__setitem__("created_at", "2026-11-06T12:00:00"),
        lambda data: data["keys"][0].__setitem__("note", "x"),
        lambda data: data["keys"][0].__setitem__("retired_at", "2020-01-01T00:00:00+00:00"),
    ],
)
def test_malformed_key_file_is_corrupt(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    path = _created(tmp_path / "keys.json")
    _rewrite(path, mutate)
    with pytest.raises(AuthorityKeyError) as info:
        _open(path)
    assert info.value.reason == "corrupt"


def test_duplicate_ids_and_current_key_count(tmp_path: Path) -> None:
    path = _created(tmp_path / "keys.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["keys"].append(dict(data["keys"][0]))
    _private_bytes(path, json.dumps(data).encode())
    with pytest.raises(AuthorityKeyError) as duplicate:
        _open(path)
    assert duplicate.value.reason == "corrupt"
    data["keys"][1]["key_id"] = "cd" * 8
    data["keys"][1]["retired_at"] = None
    _private_bytes(path, json.dumps(data).encode())
    with pytest.raises(AuthorityKeyError) as two_current:
        _open(path)
    assert two_current.value.reason == "corrupt"
    data["keys"][0]["retired_at"] = data["keys"][0]["created_at"]
    data["keys"][1]["retired_at"] = data["keys"][1]["created_at"]
    _private_bytes(path, json.dumps(data).encode())
    with pytest.raises(AuthorityKeyError) as none_current:
        _open(path)
    assert none_current.value.reason == "corrupt"


def test_too_many_keys_and_non_object(tmp_path: Path) -> None:
    path = _created(tmp_path / "keys.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    original = data["keys"][0]
    data["keys"] = []
    for index in range(_MAX_KEYS + 1):
        item = dict(original)
        item["key_id"] = f"{index:016x}"
        item["retired_at"] = None if index == 0 else original["created_at"]
        data["keys"].append(item)
    _private_bytes(path, json.dumps(data).encode())
    with pytest.raises(AuthorityKeyError) as info:
        _open(path)
    assert info.value.reason == "corrupt"
    _private_bytes(tmp_path / "list.json", b"[]")
    with pytest.raises(AuthorityKeyError):
        _open(tmp_path / "list.json")
    huge = tmp_path / "huge.json"
    _private_bytes(huge, b"x" * (_MAX_BYTES + 1))
    with pytest.raises(AuthorityKeyError) as large:
        _open(huge)
    assert large.value.reason == "corrupt"
    empty = tmp_path / "empty.json"
    _private_bytes(empty, b"")
    with pytest.raises(AuthorityKeyError) as blank:
        _open(empty)
    assert blank.value.reason == "corrupt"


def test_permissions_symlink_and_directory(tmp_path: Path) -> None:
    path = _created(tmp_path / "keys.json")
    path.chmod(0o644)
    with pytest.raises(AuthorityKeyError, match="too open") as loose:
        _open(path)
    assert loose.value.reason == "permissions"
    assert _MARKER not in str(loose.value)
    folder = tmp_path / "open"
    folder.mkdir()
    folder.chmod(0o777)
    try:
        with pytest.raises(AuthorityKeyError, match="writable by others") as opened:
            _open(folder / "keys.json", create=True)
        assert opened.value.reason == "permissions"
    finally:
        folder.chmod(0o700)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(AuthorityKeyError, match="symbolic link") as linked:
        _open(link)
    assert linked.value.reason == "symlink"
    nested = tmp_path / "as-dir"
    nested.mkdir()
    with pytest.raises(AuthorityKeyError, match="regular file") as directory:
        _open(nested)
    assert directory.value.reason == "permissions"


def test_store_rejects_a_bad_path_and_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="authority_id"):
        FileAuthorityKeyStore(tmp_path / "keys.json", "")
    with pytest.raises(ValueError, match="file"):
        FileAuthorityKeyStore(Path("."), "host-app")
    path = tmp_path / "keys.json"
    store = FileAuthorityKeyStore(path, "host-app")
    key = _signing_key()
    store.save((key,), exclusive=True)
    with pytest.raises(AuthorityKeyError, match="already exists") as info:
        store.save((key,), exclusive=True)
    assert info.value.reason == "exists"
    assert store.path == path
    loaded = store.load()
    assert loaded[0].key_id == key.key_id
    assert "secret" not in repr(loaded[0])
    assert base64.b64encode(loaded[0].secret).decode() not in repr(loaded[0])


def test_constructor_arguments() -> None:
    with pytest.raises(ValueError, match="key_path or key_store"):
        ActorAuthority("host-app", key_path="a", key_store=MemoryKeyStore())
    with pytest.raises(ValueError, match="create_key_file"):
        ActorAuthority("host-app", create_key_file=True)
    with pytest.raises(TypeError, match="AuthorityKeyStore"):
        ActorAuthority("host-app", key_store=object())  # type: ignore[arg-type]
    with pytest.raises(AuthorityKeyError, match="in-memory") as info:
        ActorAuthority("host-app").rotate_keys()
    assert info.value.reason == "memory"


def test_create_race_keeps_the_winning_key() -> None:
    winner = _signing_key("cd" * 8)
    store = MemoryKeyStore()
    store.fail_exclusive = True
    store.winner = (winner,)
    authority = ActorAuthority(
        "host-app", key_store=store, create_key_file=True, clock=FixedClock()
    )
    assert authority.signing_key_id == winner.key_id
    store.save_reason = "io"
    store.keys = None
    with pytest.raises(AuthorityKeyError, match="save failed") as info:
        ActorAuthority("host-app", key_store=store, create_key_file=True)
    assert info.value.reason == "io"
    with pytest.raises(AuthorityKeyError, match="memory") as rotated:
        authority.rotate_keys()
    assert rotated.value.reason == "memory"


def test_rotation_keeps_old_seals_and_rejects_new_ones_from_a_retired_key(tmp_path: Path) -> None:
    clock = FixedClock()
    path = tmp_path / "keys.json"
    authority = ActorAuthority(
        "host-app",
        clock=clock,
        max_ttl=timedelta(hours=1),
        key_path=path,
        create_key_file=True,
    )
    original = _issue(authority)
    first_id = authority.signing_key_id
    assert authority.rotate_keys() != first_id
    reloaded = ActorAuthority("host-app", clock=clock, max_ttl=timedelta(hours=1), key_path=path)
    assert reloaded.verify(original).has_grant(_REVIEWER_GRANT)
    fresh = _issue(reloaded, "user:7")
    assert reloaded.is_trusted(fresh)
    assert authority.is_trusted(fresh)
    assert authority.is_trusted(original)
    rows = _materials(path)
    retired = next(item for item in rows if item[2] is not None)
    current = next(item for item in rows if item[2] is None)
    assert retired[0] == first_id
    boundary = _forge(
        retired[1],
        actor_id="user:forged",
        tenant_id="acme",
        grants=frozenset({_REVIEWER_GRANT}),
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=15),
        authority_id="host-app",
    )
    assert reloaded.verify(boundary).has_grant(_REVIEWER_GRANT)
    after = _forge(
        retired[1],
        actor_id="user:later",
        tenant_id="acme",
        grants=frozenset({_REVIEWER_GRANT}),
        issued_at=T0 + timedelta(microseconds=1),
        expires_at=T0 + timedelta(minutes=15),
        authority_id="host-app",
    )
    with pytest.raises(UntrustedActorError, match="seal"):
        reloaded.verify(after)
    too_long = _forge(
        current[1],
        actor_id="user:wide",
        tenant_id="acme",
        grants=frozenset(),
        issued_at=T0,
        expires_at=T0 + timedelta(hours=1, microseconds=1),
        authority_id="host-app",
    )
    with pytest.raises(UntrustedActorError, match="seal"):
        reloaded.verify(too_long)
    assert "current" in repr(reloaded_key(path, current=True))
    assert "retired" in repr(reloaded_key(path, current=False))
    credential = original.to_credential()
    assert base64.b64encode(retired[1]).decode() not in credential
    restored = TrustedActor.from_credential(credential)
    assert reloaded.verify(restored) is restored
    assert reloaded.is_trusted(TrustedActor.from_credential("  " + credential + "\n"))


def test_rotation_drops_keys_past_max_ttl_and_refuses_to_pass_the_cap(tmp_path: Path) -> None:
    clock = FixedClock()
    path = tmp_path / "aged.json"
    authority = ActorAuthority(
        "host-app",
        clock=clock,
        max_ttl=timedelta(hours=1),
        key_path=path,
        create_key_file=True,
    )
    original_id = authority.signing_key_id
    authority.rotate_keys()
    clock.now = T0 + timedelta(hours=1)
    authority.rotate_keys()
    ids = [item["key_id"] for item in json.loads(path.read_text(encoding="utf-8"))["keys"]]
    assert original_id not in ids
    capped = ActorAuthority(
        "host-app", clock=FixedClock(), key_path=tmp_path / "cap.json", create_key_file=True
    )
    actor = _issue(capped)
    for _ in range(_MAX_KEYS - 1):
        capped.rotate_keys()
    with pytest.raises(AuthorityKeyError, match="verification window") as info:
        capped.rotate_keys()
    assert info.value.reason == "limit"
    again = ActorAuthority("host-app", clock=FixedClock(), key_path=tmp_path / "cap.json")
    assert again.is_trusted(actor)


def test_credentials_reject_malformed_input() -> None:
    bare = TrustedActor(
        actor_id="user:42",
        issued_at=T0,
        expires_at=T0 + timedelta(minutes=1),
        authority_id="host-app",
    )
    with pytest.raises(ValueError, match="credential"):
        bare.to_credential()
    authority = ActorAuthority("host-app", clock=FixedClock())
    token = _issue(authority).to_credential()
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(1)
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential("not-a-token")
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_CREDENTIAL_PREFIX + ("A" * 20_000))
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential("id42a1.!!!!")
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential("id42a1.abcde")
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential("id42a1." + base64.urlsafe_b64encode(b"[]").decode())
    body = json.loads(base64.urlsafe_b64decode(_pad(token.removeprefix("id42a1."))))
    body["grants"] = ["not a grant"]
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_token(body))
    body = json.loads(base64.urlsafe_b64decode(_pad(token.removeprefix("id42a1."))))
    body["grants"] = body["grants"] + ["odoo.invoice.post"]
    tampered = TrustedActor.from_credential(_token(body))
    with pytest.raises(UntrustedActorError, match="seal"):
        authority.verify(tampered)
    body["issued_at"] = "2026-11-06T12:00:00"
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_token(body))
    body = json.loads(base64.urlsafe_b64decode(_pad(token.removeprefix("id42a1."))))
    body["grants"] = "suppliers.read"
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_token(body))
    body = json.loads(base64.urlsafe_b64decode(_pad(token.removeprefix("id42a1."))))
    body["seal"] = "aaaa"
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_token(body))
    body["extra"] = 1
    with pytest.raises(ValueError, match="credential"):
        TrustedActor.from_credential(_token(body))
    for field, value in (
        ("actor_id", ""),
        ("actor_id", 1),
        ("tenant_id", 1),
        ("grants", [1]),
        ("issued_at", 1),
        ("issued_at", "not-a-date"),
        ("seal", "!!!!"),
        ("seal", 1),
    ):
        fresh = json.loads(base64.urlsafe_b64decode(_pad(token.removeprefix("id42a1."))))
        fresh[field] = value
        with pytest.raises(ValueError, match="credential"):
            TrustedActor.from_credential(_token(fresh))


def test_short_write_is_completed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    real_write = os.write
    calls = {"n": 0}

    def short(fd: int, data: bytes | bytearray | memoryview) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, memoryview(data)[:1])
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short)
    created = _open(path, create=True)
    actor = _issue(created)
    monkeypatch.undo()
    assert calls["n"] >= 2
    assert _open(path).is_trusted(actor)


def test_zero_write_removes_the_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def zero(fd: int, data: bytes | bytearray | memoryview) -> int:
        del fd, data
        return 0

    monkeypatch.setattr(os, "write", zero)
    with pytest.raises(AuthorityKeyError, match="cannot write") as failed:
        _open(tmp_path / "zero.json", create=True)
    assert failed.value.reason == "io"
    assert not (tmp_path / "zero.json").exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_failed_replace_leaves_the_previous_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "keys.json"
    created = _open(path, create=True)
    actor = _issue(created)

    def broken_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        del src, dst
        raise OSError(errno.EIO, "replace")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(AuthorityKeyError, match="cannot write") as replaced:
        created.rotate_keys()
    assert replaced.value.reason == "io"
    monkeypatch.undo()
    assert _open(path).is_trusted(actor)
    assert list(tmp_path.glob(".*.tmp")) == []


def test_replace_write_failure_discards_the_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "keys.json"
    created = _open(path, create=True)
    actor = _issue(created)

    def zero(fd: int, data: bytes | bytearray | memoryview) -> int:
        del fd, data
        return 0

    monkeypatch.setattr(os, "write", zero)
    with pytest.raises(AuthorityKeyError, match="cannot write") as failed:
        created.rotate_keys()
    assert failed.value.reason == "io"
    monkeypatch.undo()
    assert _open(path).is_trusted(actor)
    assert list(tmp_path.glob(".*.tmp")) == []


def test_exclusive_create_open_failure_is_an_io_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_open = os.open

    def key_open_fails(*args: object, **kwargs: object) -> int:
        opened = str(args[0]) if args else ""
        flags = args[1] if len(args) > 1 else 0
        creating = isinstance(flags, int) and bool(flags & os.O_CREAT)
        if creating and not opened.endswith(".lock"):
            raise OSError(errno.EIO, "create")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", key_open_fails)
    with pytest.raises(AuthorityKeyError, match="cannot write") as failed:
        _open(tmp_path / "keys.json", create=True)
    assert failed.value.reason == "io"


def test_lock_open_failure_is_an_io_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_open = os.open

    def lock_fails(*args: object, **kwargs: object) -> int:
        if str(args[0]).endswith(".lock"):
            raise OSError(errno.EIO, "lock")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", lock_fails)
    with pytest.raises(AuthorityKeyError, match="cannot lock") as locked:
        _open(tmp_path / "locked.json", create=True)
    assert locked.value.reason == "io"


def test_unreadable_key_file_is_an_io_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ready = _created(tmp_path / "ready.json")
    real_open = os.open

    def read_fails(*args: object, **kwargs: object) -> int:
        target = str(args[0])
        if target.endswith("ready.json"):
            raise OSError(errno.EIO, "read")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", read_fails)
    with pytest.raises(AuthorityKeyError, match="cannot read") as unreadable:
        _open(ready)
    assert unreadable.value.reason == "io"


def test_short_read_is_corrupt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ready = _created(tmp_path / "ready.json")

    def short_read(fd: int, size: int) -> bytes:
        del fd, size
        return b""

    monkeypatch.setattr(os, "read", short_read)
    with pytest.raises(AuthorityKeyError, match="not valid") as partial:
        _open(ready)
    assert partial.value.reason == "corrupt"


async def test_restarted_process_verifies_the_previous_approval(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite"
    key_path = tmp_path / "keys.json"
    authority = ActorAuthority("host-app", key_path=key_path, create_key_file=True)
    storage = SqliteStorage(database)
    registry = WorkRegistry()
    registry.register(make_work(), executor=InvoiceExecutor())
    capabilities = CapabilityRegistry()
    capabilities.add(SupplierProvider())
    runner = WorkRunner(
        registry=registry,
        policies=StaticPolicyResolver(
            {(_WORK, "1.0.0"): make_policy(autonomy=AutonomyLevel.MANUAL)}
        ),
        capabilities=capabilities,
        actor_authority=authority,
        storage=storage,
    )
    actor = authority.issue(
        actor_id="user:42", tenant_id="acme", grants=set(_ACTOR_GRANTS), ttl=timedelta(minutes=30)
    )
    reviewer = authority.issue(
        actor_id="finance:ada",
        tenant_id="acme",
        grants={_REVIEWER_GRANT},
        ttl=timedelta(minutes=30),
    )
    execution = await runner.run(work_id=_WORK, version="1.0.0", input_data=INVOICE, actor=actor)
    assert execution.status is ExecutionStatus.WAITING_APPROVAL
    pending = await runner.approvals.list_pending(execution.execution_id)
    decided = await runner.approvals.approve(
        pending[0].approval_id, reviewer=reviewer, reason="checked"
    )
    assert decided.status is ApprovalStatus.GRANTED
    approval_id = decided.approval_id
    reviewer_path = _write_credential(tmp_path / "reviewer.cred", reviewer)
    actor_path = _write_credential(tmp_path / "actor.cred", actor)
    storage.close()

    restarted = ActorAuthority("host-app", key_path=key_path)
    assert restarted.verify(TrustedActor.from_credential(reviewer_path.read_text())).has_grant(
        _REVIEWER_GRANT
    )
    assert restarted.verify(TrustedActor.from_credential(actor_path.read_text())).has_grant(
        "suppliers.read"
    )
    wrong = ActorAuthority("host-app", key_path=tmp_path / "other.json", create_key_file=True)
    assert not wrong.is_trusted(reviewer)

    tests = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = tests + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(tests) / "authority_key_worker.py"),
            str(key_path),
            str(reviewer_path),
            str(actor_path),
            str(database),
            str(approval_id),
            _REVIEWER_GRANT,
            ",".join(_ACTOR_GRANTS),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "verified"
    assert _MARKER not in result.stdout
    assert _MARKER not in result.stderr


def _created(path: Path) -> Path:
    _open(path, create=True)
    return path


def _write_credential(path: Path, actor: TrustedActor) -> Path:
    path.write_text(actor.to_credential(), encoding="utf-8")
    path.chmod(0o600)
    return path


def _pad(body: str) -> bytes:
    return (body + "=" * (-len(body) % 4)).encode()


def _token(body: dict[str, object]) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "id42a1." + raw


def reloaded_key(path: Path, *, current: bool) -> AuthoritySigningKey:
    keys = FileAuthorityKeyStore(path, "host-app").load()
    if current:
        return next(key for key in keys if key.retired_at is None)
    return next(key for key in keys if key.retired_at is not None)


_CREDENTIAL_PREFIX = "id42a1."


def test_source_does_not_log_or_print() -> None:
    for name in ("authority_keys.py", "actors.py"):
        source = files("id42_work").joinpath(name).read_text(encoding="utf-8")
        assert "print(" not in source
        assert "logging." not in source
