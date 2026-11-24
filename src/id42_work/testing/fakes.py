"""Deterministic fakes for examples and tests. No network, no credentials.

``FakeOdooProvider`` models a narrow, server-side Odoo adapter (specification 16): each
operation is a separate capability, and side effects are deduplicated by
``operation_id``, the way a real adapter should use the SDK's idempotency keys.
"""

import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from pydantic import JsonValue

from id42_work.exceptions import UncertainOutcomeError
from id42_work.policy.capabilities import CapabilityCall, CapabilitySpec

PARTNER_READ = "odoo.partner.read"
INVOICE_READ = "odoo.invoice.read"
INVOICE_PREPARE = "odoo.invoice.prepare"
INVOICE_VALIDATE = "odoo.invoice.validate"
INVOICE_POST = "odoo.invoice.post"

FailureHook = Callable[[str, Mapping[str, object]], BaseException | None]


class ManualClock:
    """A clock that only moves when told to. Use as ``WorkRunner(clock=...)``."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by ``delta``."""
        self._now += delta


class FakeOdooProvider:
    """In-memory stand-in for an Odoo 18 invoice adapter.

    Partners ``SUP-001`` (active) and ``SUP-002`` (blocked) exist by default. Moves are
    created by ``odoo.invoice.prepare`` in state ``draft``, checked by
    ``odoo.invoice.validate`` and posted by ``odoo.invoice.post``. Repeating an
    operation with the same ``operation_id`` returns the first result without a second
    side effect.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.partners: dict[str, dict[str, JsonValue]] = {
            "SUP-001": {"ref": "SUP-001", "name": "Acme Supplies", "active": True},
            "SUP-002": {"ref": "SUP-002", "name": "Blocked Parts", "active": False},
        }
        self.moves: dict[int, dict[str, JsonValue]] = {}
        self.side_effects: list[tuple[str, str]] = []
        self._results: dict[str, JsonValue] = {}
        self._next_id = 1
        self.failure_hook: FailureHook | None = None

    def capabilities(self) -> list[CapabilitySpec]:
        move = _object_schema({"move_id": {"type": "integer"}}, ["move_id"])
        return [
            CapabilitySpec(
                name=PARTNER_READ,
                side_effecting=False,
                description="Read a supplier (partner) by its reference, e.g. SUP-001.",
                params_schema=_object_schema({"ref": {"type": "string"}}, ["ref"]),
            ),
            CapabilitySpec(
                name=INVOICE_READ,
                side_effecting=False,
                description="Read a vendor bill (move) by id.",
                params_schema=move,
            ),
            CapabilitySpec(
                name=INVOICE_PREPARE,
                side_effecting=True,
                description="Create a draft vendor bill. Returns the new move.",
                params_schema=_object_schema(
                    {
                        "partner_ref": {"type": "string"},
                        "invoice_number": {"type": "string"},
                        "amount": {"type": "string", "description": "Decimal amount, e.g. 1250.00"},
                        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                    },
                    ["partner_ref", "invoice_number", "amount", "currency"],
                ),
            ),
            CapabilitySpec(
                name=INVOICE_VALIDATE,
                side_effecting=True,
                description="Check a draft vendor bill before posting.",
                params_schema=move,
            ),
            CapabilitySpec(
                name=INVOICE_POST,
                side_effecting=True,
                description="Post a draft vendor bill to the ledger.",
                params_schema=move,
            ),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        if self.failure_hook is not None:
            failure = self.failure_hook(capability, params)
            if failure is not None:
                raise failure
        with self._lock:
            if capability == PARTNER_READ:
                return self._partner(params)
            if capability == INVOICE_READ:
                return self._move(params)
            key = str(call.operation_id)
            if key in self._results:
                return self._results[key]
            result = self._side_effect(capability, params)
            self._results[key] = result
            self.side_effects.append((capability, key))
            return result

    def count(self, capability: str) -> int:
        """Number of side effects performed for ``capability``."""
        with self._lock:
            return sum(1 for name, _ in self.side_effects if name == capability)

    def fail_with_uncertain_outcome(self, capability: str) -> None:
        """Make every call to ``capability`` raise ``UncertainOutcomeError``."""

        def hook(name: str, params: Mapping[str, object]) -> BaseException | None:
            if name == capability:
                return UncertainOutcomeError(f"{name}: connection lost after the request")
            return None

        self.failure_hook = hook

    # ------------------------------------------------------------------ operations

    def _partner(self, params: Mapping[str, object]) -> JsonValue:
        partner = self.partners.get(str(params.get("ref")))
        if partner is None:
            msg = f"partner {params.get('ref')!r} not found"
            raise LookupError(msg)
        return dict(partner)

    def _move(self, params: Mapping[str, object]) -> JsonValue:
        move = self.moves.get(_int(params.get("move_id")))
        if move is None:
            msg = f"move {params.get('move_id')!r} not found"
            raise LookupError(msg)
        return dict(move)

    def _side_effect(self, capability: str, params: Mapping[str, object]) -> JsonValue:
        if capability == INVOICE_PREPARE:
            move_id = self._next_id
            self._next_id += 1
            move: dict[str, JsonValue] = {
                "move_id": move_id,
                "partner_ref": str(params.get("partner_ref")),
                "invoice_number": str(params.get("invoice_number")),
                "amount": _amount(params.get("amount")),
                "currency": str(params.get("currency")),
                "state": "draft",
            }
            self.moves[move_id] = move
            return dict(move)
        existing = self.moves.get(_int(params.get("move_id")))
        if existing is None:
            msg = f"move {params.get('move_id')!r} not found"
            raise LookupError(msg)
        if capability == INVOICE_VALIDATE:
            problems: list[JsonValue] = []
            if Decimal(str(existing["amount"])) <= 0:
                problems.append("amount must be positive")
            return {"move_id": existing["move_id"], "valid": not problems, "problems": problems}
        if capability == INVOICE_POST:
            if existing["state"] != "draft":
                msg = f"move {existing['move_id']} is {existing['state']}, not draft"
                raise ValueError(msg)
            existing["state"] = "posted"
            return {
                "move_id": existing["move_id"],
                "state": "posted",
                "name": f"BILL/{existing['move_id']:04d}",
            }
        msg = f"unknown capability {capability!r}"
        raise LookupError(msg)


def _int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return -1


def _amount(value: object) -> str:
    try:
        return str(Decimal(str(value)))
    except InvalidOperation as error:
        msg = f"invalid amount {value!r}"
        raise ValueError(msg) from error


def _object_schema(properties: dict[str, JsonValue], required: list[str]) -> dict[str, JsonValue]:
    required_names: list[JsonValue] = list(required)
    return {
        "type": "object",
        "properties": properties,
        "required": required_names,
        "additionalProperties": False,
    }
