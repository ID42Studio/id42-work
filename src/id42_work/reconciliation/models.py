"""Verdicts a provider or an operator can give for one uncertain call."""

from enum import Enum
from typing import Self

from pydantic import JsonValue, model_validator

from id42_work._model import FrozenModel


class ReconciliationVerdict(Enum):
    """What is known about one external effect.

    ``APPLIED``, ``NOT_APPLIED`` and ``UNKNOWN`` are probe results.
    ``ABANDONED`` is an operator decision and is not a probe result.
    """

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    ABANDONED = "ABANDONED"


class ReconciliationProbe(FrozenModel):
    """What a provider learned by looking up one operation id.

    ``result`` is journaled when ``verdict`` is ``APPLIED``. A provider that only
    knows the effect happened, and not the payload, leaves ``result`` null. The
    execution then replays JSON null.
    """

    verdict: ReconciliationVerdict
    result: JsonValue = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.verdict is ReconciliationVerdict.ABANDONED:
            msg = "a probe cannot abandon an action"
            raise ValueError(msg)
        return self
