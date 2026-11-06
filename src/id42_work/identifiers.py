"""Identifier formats shared by the domain model.

Every identifier that participates in authorization or digests has a strict format so
that two spellings of the same name cannot exist.
"""

import re
from typing import Annotated

from pydantic import StringConstraints

_SEGMENT = r"[a-z][a-z0-9_-]*"

IDENTIFIER_PATTERN = rf"^{_SEGMENT}(?:\.{_SEGMENT})*$"
"""Work, policy and validator identifiers, e.g. ``supplier_invoice_validation``."""

CAPABILITY_PATTERN = IDENTIFIER_PATTERN
"""Capability names, e.g. ``odoo.invoice.post``."""

GRANT_PREFIXES: tuple[str, ...] = ("approve", "resume")
"""Prefixes for grants that are not capabilities themselves."""

GRANT_PATTERN = rf"^(?:(?:{'|'.join(GRANT_PREFIXES)}):)?{_SEGMENT}(?:\.{_SEGMENT})*$"
"""Actor grants: a capability, ``approve:<target>`` or ``resume:<work_id>``."""

ACTION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
"""Executor action ids. Cannot start with ``_``, which is reserved for gate actions."""

SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
"""Semantic Versioning 2.0.0."""

SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"

Identifier = Annotated[str, StringConstraints(pattern=IDENTIFIER_PATTERN, max_length=128)]
Capability = Annotated[str, StringConstraints(pattern=CAPABILITY_PATTERN, max_length=128)]
Grant = Annotated[str, StringConstraints(pattern=GRANT_PATTERN, max_length=136)]
ActionId = Annotated[str, StringConstraints(pattern=ACTION_ID_PATTERN)]
SemVer = Annotated[str, StringConstraints(pattern=SEMVER_PATTERN, max_length=64)]
Sha256Hex = Annotated[str, StringConstraints(pattern=SHA256_HEX_PATTERN)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=256)]

_CAPABILITY_RE = re.compile(CAPABILITY_PATTERN)


def is_capability(name: str) -> bool:
    """Return whether ``name`` is a well-formed capability name."""
    return len(name) <= 128 and _CAPABILITY_RE.fullmatch(name) is not None  # noqa: PLR2004


def approve_grant(target: str) -> str:
    """Grant that allows deciding approvals for ``target`` (a capability or work id)."""
    return f"approve:{target}"


def resume_grant(work_id: str) -> str:
    """Grant that allows resuming executions of ``work_id`` started by another actor."""
    return f"resume:{work_id}"
