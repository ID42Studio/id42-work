"""A small, deterministic runtime environment for runner tests."""

import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, JsonValue

from _factories import FixedClock, InvoiceInput, InvoiceOutput, make_policy, make_work
from id42_work import (
    ActorAuthority,
    AutonomyLevel,
    CapabilityCall,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionContext,
    MemoryStorage,
    Policy,
    Severity,
    StaticPolicyResolver,
    TrustedActor,
    ValidationContext,
    ValidationResult,
    Validator,
    Work,
    WorkRegistry,
    WorkRunner,
)

READ = "suppliers.read"
VALIDATE = "invoices.validate"
PREPARE = "odoo.invoice.prepare"
INVOICE = {
    "invoice_number": "INV-1001",
    "supplier_id": "SUP-001",
    "currency": "BRL",
    "amount": "1250.00",
}


class SupplierProvider:
    """Read-only supplier lookups and a side-effecting invoice preparation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], CapabilityCall]] = []
        self.behaviour: Callable[[str, Mapping[str, object]], object] | None = None
        self._lock = threading.Lock()

    def capabilities(self) -> list[CapabilitySpec]:
        return [
            CapabilitySpec(name=READ, side_effecting=False),
            CapabilitySpec(name=VALIDATE, side_effecting=False),
            CapabilitySpec(name=PREPARE, side_effecting=True),
        ]

    async def invoke(
        self, capability: str, params: Mapping[str, object], call: CapabilityCall
    ) -> JsonValue:
        with self._lock:
            self.calls.append((capability, dict(params), call))
        if self.behaviour is not None:
            return self.behaviour(capability, params)  # type: ignore[return-value]
        if capability == READ:
            return {"id": str(params["supplier_id"]), "active": True}
        if capability == PREPARE:
            return {"move_id": 77, "state": "draft"}
        return {"ok": True}

    def count(self, capability: str) -> int:
        with self._lock:
            return sum(1 for c, _, _ in self.calls if c == capability)


class InvoiceExecutor:
    """Reads the supplier, optionally prepares the invoice, returns the result."""

    def __init__(self, *, prepare: bool = False) -> None:
        self.prepare = prepare
        self.runs = 0

    async def execute(self, input_data: InvoiceInput, context: ExecutionContext) -> InvoiceOutput:
        self.runs += 1
        supplier = await context.invoke(
            READ, action_id="load-supplier", params={"supplier_id": input_data.supplier_id}
        )
        assert isinstance(supplier, dict)
        status = "valid"
        if self.prepare:
            await context.invoke(
                PREPARE,
                action_id="prepare-invoice",
                params={"invoice": input_data.invoice_number, "amount": input_data.amount},
            )
            status = "prepared"
        return InvoiceOutput(
            invoice_number=input_data.invoice_number,
            supplier_id=str(supplier["id"]),
            status=status,
        )


class FunctionExecutor:
    """Executor whose body is a test-provided coroutine function."""

    def __init__(self, body: Callable[[Any, ExecutionContext], Any]) -> None:
        self._body = body
        self.runs = 0

    async def execute(self, input_data: BaseModel, context: ExecutionContext) -> BaseModel:
        self.runs += 1
        return await self._body(input_data, context)


class AmountRule:
    """Rejects non-positive amounts (reads the input from the validation context)."""

    validator_id = "amount_rule"

    def __init__(self, *, required: bool = True, minimum: Decimal = Decimal(0)) -> None:
        self.required = required
        self.minimum = minimum

    async def validate(self, output: BaseModel, context: ValidationContext) -> ValidationResult:
        data = context.input_data
        assert isinstance(data, InvoiceInput)
        passed = data.amount > self.minimum
        return ValidationResult(
            validator_id=self.validator_id,
            passed=passed,
            severity=Severity.ERROR if not passed else Severity.INFO,
            message="amount must be positive",
        )


class Env:
    """Runner wired to in-memory storage, a fixed clock and fake integrations."""

    def __init__(
        self,
        *,
        policy: Policy | None = None,
        work: Work | None = None,
        executor: Any = None,
        validators: Iterable[Validator] = (),
        grants: Iterable[str] = (READ, VALIDATE, PREPARE),
        no_policy: bool = False,
    ) -> None:
        self.clock = FixedClock()
        self.authority = ActorAuthority("host-app", clock=self.clock)
        self.provider = SupplierProvider()
        self.capabilities = CapabilityRegistry()
        self.capabilities.add(self.provider)
        self.work = work or make_work()
        self.policy = policy or make_policy(
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_capabilities=frozenset({READ, VALIDATE, PREPARE}),
        )
        self.registry = WorkRegistry()
        self.executor = executor or InvoiceExecutor()
        self.registry.register(self.work, executor=self.executor, validators=validators)
        self.storage = MemoryStorage()
        policies = {} if no_policy else {(self.work.id, self.work.version): self.policy}
        self.runner = WorkRunner(
            registry=self.registry,
            policies=StaticPolicyResolver(policies),
            capabilities=self.capabilities,
            actor_authority=self.authority,
            storage=self.storage,
            clock=self.clock,
        )
        self.actor = self.issue("user:42", grants)

    def issue(self, actor_id: str, grants: Iterable[str]) -> TrustedActor:
        return self.authority.issue(
            actor_id=actor_id, tenant_id="acme", grants=set(grants), ttl=timedelta(hours=1)
        )

    async def run(self, input_data: Any = None, **kwargs: Any):
        kwargs.setdefault("actor", self.actor)
        return await self.runner.run(
            work_id=self.work.id,
            version=self.work.version,
            input_data=INVOICE if input_data is None else input_data,
            **kwargs,
        )

    async def event_types(self, execution_id: Any) -> list[str]:
        return [e.event_type.value for e in await self.runner.events(execution_id)]
