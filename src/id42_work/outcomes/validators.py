"""Built-in validators (specification 9)."""

from pydantic import BaseModel, ValidationError

from id42_work.error_info import describe_validation_error
from id42_work.outcomes.models import Severity, ValidationResult

SCHEMA_VALIDATOR_ID = "schema"
"""Id of the built-in output-schema validator. Reserved: user validators cannot use it."""


def validate_output_schema(
    output: object, output_schema: type[BaseModel]
) -> tuple[BaseModel | None, ValidationResult]:
    """Validate ``output`` against ``output_schema``.

    Instances are re-validated from their data, so an instance built with
    ``model_construct`` (which skips validation) cannot slip through. Returns the
    validated output (or ``None``) and the schema validation result.
    """
    try:
        if isinstance(output, BaseModel):
            data = output.model_dump(mode="python", warnings=False)
            validated = output_schema.model_validate(data)
            if type(output) is not output_schema:
                msg = (
                    f"executor returned {type(output).__name__}, expected {output_schema.__name__}"
                )
                return None, _failed(msg)
        else:
            validated = output_schema.model_validate(output)
    except ValidationError as error:
        summary = describe_validation_error(error)
        return None, _failed(f"output does not match {output_schema.__name__}: {summary}")
    return validated, ValidationResult(
        validator_id=SCHEMA_VALIDATOR_ID,
        passed=True,
        severity=Severity.INFO,
        message=f"output matches {output_schema.__name__}",
    )


def _failed(message: str) -> ValidationResult:
    return ValidationResult(
        validator_id=SCHEMA_VALIDATOR_ID, passed=False, severity=Severity.ERROR, message=message
    )
