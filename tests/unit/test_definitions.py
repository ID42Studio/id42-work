import json
import subprocess
import sys
import textwrap

import pytest
from pydantic import BaseModel, ValidationError

from _factories import InvoiceInput, InvoiceOutput, make_work
from id42_work import AutonomyLevel, Work, WorkDescriptor, WorkRef


def test_work_is_a_pure_contract() -> None:
    work = make_work()
    assert work.ref == WorkRef(id="supplier_invoice_validation", version="1.0.0")
    assert str(work.ref) == "supplier_invoice_validation@1.0.0"
    assert work.autonomy_ceiling is AutonomyLevel.AUTONOMOUS
    assert not {"policy", "executor", "validators"} & set(Work.model_fields)


def test_work_is_immutable() -> None:
    work = make_work()
    with pytest.raises(ValidationError):
        work.version = "2.0.0"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "Bad Id"},
        {"version": "1.0"},
        {"name": ""},
        {"input_schema": dict},
        {"required_capabilities": frozenset({"Not.Valid"})},
        {"unexpected": True},
    ],
)
def test_invalid_definitions_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_work(**overrides)


def test_descriptor_round_trips_through_json() -> None:
    descriptor = make_work().describe()
    assert "class" not in descriptor.model_dump_json()
    restored = WorkDescriptor.model_validate_json(descriptor.model_dump_json())
    assert restored == descriptor
    assert restored.definition_digest == descriptor.definition_digest
    assert descriptor.input_schema.ref.endswith(":InvoiceInput")
    assert descriptor.required_capabilities == ("invoices.validate", "suppliers.read")


def test_digest_ignores_capability_order() -> None:
    a = make_work(required_capabilities=frozenset({"a.read", "b.read", "c.read"}))
    b = make_work(required_capabilities=frozenset({"c.read", "a.read", "b.read"}))
    assert a.definition_digest == b.definition_digest


class OtherOutput(BaseModel):
    invoice_number: str
    status: str


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": "1.1.0"},
        {"required_capabilities": frozenset({"suppliers.read"})},
        {"autonomy_ceiling": AutonomyLevel.GUIDED},
        {"output_schema": OtherOutput},
        {"description": "changed"},
    ],
)
def test_any_contract_change_changes_the_digest(overrides: dict[str, object]) -> None:
    assert make_work(**overrides).definition_digest != make_work().definition_digest


def test_schema_with_float_constraints_has_a_digest() -> None:
    from pydantic import Field  # noqa: PLC0415

    class Scored(BaseModel):
        score: float = Field(ge=0.5)

    work = make_work(input_schema=Scored, output_schema=InvoiceOutput)
    assert len(work.definition_digest) == 64


def test_digest_is_stable_across_processes() -> None:
    code = textwrap.dedent(
        """
        from decimal import Decimal
        from pydantic import BaseModel
        from id42_work import Work

        class In(BaseModel):
            amount: Decimal
            tags: frozenset[str]

        class Out(BaseModel):
            ok: bool

        work = Work(id="w", version="1.0.0", name="W", input_schema=In, output_schema=Out,
                    required_capabilities=frozenset({"x.read", "y.read", "z.write"}))
        print(work.definition_digest)
        """
    )
    digests = {
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            env={"PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "42")
    }
    assert len(digests) == 1
    assert len(digests.pop()) == 64


def test_descriptor_json_is_plain_data() -> None:
    data = json.loads(make_work().describe().model_dump_json())
    assert data["input_schema"]["json_schema"]["title"] == "InvoiceInput"
    assert InvoiceInput.__name__ in data["input_schema"]["ref"]
