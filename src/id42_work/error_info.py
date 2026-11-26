"""Serializable descriptions of errors and their cause chains."""

from typing import Self, cast

from pydantic import ValidationError

from id42_work._model import FrozenModel
from id42_work.identifiers import NonEmptyStr

_MAX_CAUSES = 10


class ErrorCause(FrozenModel):
    """One link of an exception cause chain."""

    type: NonEmptyStr
    message: str


class ExecutionErrorInfo(FrozenModel):
    """Serializable description of why an execution failed."""

    type: NonEmptyStr
    message: str
    causes: tuple[ErrorCause, ...] = ()
    requires_reconciliation: bool = False

    @classmethod
    def from_exception(cls, error: BaseException, *, requires_reconciliation: bool = False) -> Self:
        """Describe ``error`` and its ``__cause__`` / ``__context__`` chain."""
        causes: list[ErrorCause] = []
        seen: set[int] = {id(error)}
        pending: list[BaseException] = []
        first = error.__cause__ or error.__context__
        if first is not None:
            pending.append(first)
        while pending and len(causes) < _MAX_CAUSES:
            current = pending.pop(0)
            if id(current) in seen:
                continue
            seen.add(id(current))
            causes.append(ErrorCause(type=type(current).__name__, message=str(current)))
            if isinstance(current, BaseExceptionGroup):
                grouped = cast("BaseExceptionGroup[BaseException]", current)
                pending = [*grouped.exceptions, *pending]
            nxt = current.__cause__ or current.__context__
            if nxt is not None:
                pending.append(nxt)
        return cls(
            type=type(error).__name__,
            message=str(error),
            causes=tuple(causes),
            requires_reconciliation=requires_reconciliation,
        )


def describe_validation_error(error: ValidationError) -> str:
    """Summarize a Pydantic error by location and reason, never by input value."""
    parts = [
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False)
    ]
    return "; ".join(parts)
