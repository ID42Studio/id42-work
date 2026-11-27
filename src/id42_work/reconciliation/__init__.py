"""Reconciliation of uncertain capability calls.

Importing this package does not import the runner. The console and the service
are loaded on first use so crash recovery can import the service while the
runner module is still initializing.
"""

from typing import TYPE_CHECKING

from id42_work.reconciliation.models import ReconciliationProbe, ReconciliationVerdict

if TYPE_CHECKING:
    from id42_work.reconciliation.console import (
        ReconciliationConsole,
        ReconciliationDecision,
        ReconciliationResumeError,
        ReconciliationSnapshot,
        console_from_server,
    )
    from id42_work.reconciliation.service import ReconciliationService

__all__ = [
    "ReconciliationConsole",
    "ReconciliationDecision",
    "ReconciliationProbe",
    "ReconciliationResumeError",
    "ReconciliationService",
    "ReconciliationSnapshot",
    "ReconciliationVerdict",
    "console_from_server",
]


def __getattr__(name: str) -> object:
    """Load the console and the service without an import cycle."""
    if name in {
        "ReconciliationConsole",
        "ReconciliationDecision",
        "ReconciliationResumeError",
        "ReconciliationSnapshot",
        "console_from_server",
    }:
        from id42_work.reconciliation import console as console_module  # noqa: PLC0415

        return getattr(console_module, name)
    if name == "ReconciliationService":
        from id42_work.reconciliation.service import ReconciliationService  # noqa: PLC0415

        return ReconciliationService
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
