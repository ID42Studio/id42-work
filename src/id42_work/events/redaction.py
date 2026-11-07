"""Redaction of sensitive values in payloads, logs and previews (specification 11)."""

import re
from collections.abc import Callable, Mapping

from pydantic import JsonValue

REDACTED = "***"

SENSITIVE_KEY_PATTERN = re.compile(r"(?i)(pass|secret|token|key|authorization|cookie|credential)")
"""Keys whose values the default redactor masks."""

Redactor = Callable[[Mapping[str, JsonValue]], dict[str, JsonValue]]
"""Returns a redacted copy of a JSON-compatible mapping."""


def default_redactor(data: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Mask values whose key matches ``SENSITIVE_KEY_PATTERN``, recursively."""
    return {key: _redact(key, value) for key, value in data.items()}


def _redact(key: str, value: JsonValue) -> JsonValue:
    if SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, dict):
        return default_redactor(value)
    if isinstance(value, list):
        return [default_redactor(v) if isinstance(v, dict) else v for v in value]
    return value
