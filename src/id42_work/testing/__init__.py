"""Testing helpers: deterministic fakes and a storage contract suite.

``id42_work.testing.contract`` requires ``pytest`` and ``pytest-asyncio``; it is not
imported here so that the fakes stay usable without them.
"""

from id42_work.testing.fakes import (
    INVOICE_POST,
    INVOICE_PREPARE,
    INVOICE_READ,
    INVOICE_VALIDATE,
    PARTNER_READ,
    FakeOdooProvider,
    ManualClock,
)

__all__ = [
    "INVOICE_POST",
    "INVOICE_PREPARE",
    "INVOICE_READ",
    "INVOICE_VALIDATE",
    "PARTNER_READ",
    "FakeOdooProvider",
    "ManualClock",
]
