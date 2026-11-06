import hashlib
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest
from pydantic import BaseModel

from id42_work.canonical import canonical_json, digest, to_canonical


class Color(Enum):
    RED = "red"


class Nested(BaseModel):
    amount: Decimal
    tags: frozenset[str]


def test_exact_encoding() -> None:
    value = {
        "b": [1, True, None],
        "a": "ção",
        "d": Decimal("1250.00"),
        "t": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "u": UUID("12345678-1234-5678-1234-567812345678"),
        "e": Color.RED,
        "td": timedelta(seconds=1, microseconds=5),
        "day": date(2026, 1, 2),
    }
    assert (
        canonical_json(value)
        == (
            '{"a":"ção","b":[1,true,null],"d":"1250.00","day":"2026-01-02",'
            '"e":"red","t":"2026-01-02T03:04:05+00:00","td":"1000005us",'
            '"u":"12345678-1234-5678-1234-567812345678"}'
        ).encode()
    )


def test_digest_is_sha256_of_canonical_json() -> None:
    value = {"x": 1}
    assert digest(value) == hashlib.sha256(b'{"x":1}').hexdigest()


def test_golden_digest_is_stable_across_releases() -> None:
    value = {"amount": Decimal("1250.00"), "invoice": "INV-1001", "tags": frozenset({"b", "a"})}
    assert digest(value) == "91ce4baa8bdaa03d2f8740b8bc904312a5c0c8c0016c91c0e52ed2a73b4a8259"


def test_key_order_does_not_matter() -> None:
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_decimal_representation_matters() -> None:
    assert digest(Decimal("1250.00")) != digest(Decimal("1250.0"))


def test_same_instant_in_other_timezone_has_same_digest() -> None:
    utc = datetime(2026, 1, 1, 12, tzinfo=UTC)
    brt = utc.astimezone(timezone(timedelta(hours=-3)))
    assert digest(utc) == digest(brt)


def test_pydantic_models_and_sets() -> None:
    model = Nested(amount=Decimal("1.50"), tags=frozenset({"z", "a", "m"}))
    assert to_canonical(model) == {"amount": "1.50", "tags": ["a", "m", "z"]}


@pytest.mark.parametrize(
    "value",
    [
        1.5,
        {"amount": 0.1},
        float("nan"),
        Decimal("NaN"),
        Decimal("Infinity"),
        datetime(2026, 1, 1),
        b"raw",
        {1: "non-string key"},
        object(),
    ],
)
def test_rejected_values(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)


def test_floats_allowed_with_repr_policy() -> None:
    assert canonical_json({"ge": 0.5}, floats="repr") == b'{"ge":0.5}'
    with pytest.raises(TypeError):
        canonical_json(float("inf"), floats="repr")


def test_duplicate_keys_after_normalization_are_rejected() -> None:
    class Key(Enum):
        A = "a"

    with pytest.raises(TypeError, match="duplicate key"):
        canonical_json({Key.A: 1, "a": 2})


def test_set_digest_is_independent_of_hash_seed() -> None:
    code = (
        "from id42_work.canonical import digest;"
        "print(digest(frozenset({'alpha','beta','gamma','delta','epsilon'})))"
    )
    outputs = {
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("1", "2", "3")
    }
    assert len(outputs) == 1
