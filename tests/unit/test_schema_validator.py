from _factories import InvoiceOutput
from id42_work.outcomes.validators import SCHEMA_VALIDATOR_ID, validate_output_schema


def test_valid_instance_and_dict() -> None:
    output = InvoiceOutput(invoice_number="I", supplier_id="S", status="valid")
    model, result = validate_output_schema(output, InvoiceOutput)
    assert model == output
    assert result.passed
    assert result.validator_id == SCHEMA_VALIDATOR_ID
    model, result = validate_output_schema(output.model_dump(), InvoiceOutput)
    assert model == output


def test_constructed_invalid_instance_is_rejected() -> None:
    forged = InvoiceOutput.model_construct(invoice_number="I")
    model, result = validate_output_schema(forged, InvoiceOutput)
    assert model is None
    assert not result.passed
