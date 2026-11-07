"""Serializable descriptions of errors and their cause chains."""

from typing import Self

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
        current = error.__cause__ or error.__context__
        while current is not None and id(current) not in seen and len(causes) < _MAX_CAUSES:
            seen.add(id(current))
            causes.append(ErrorCause(type=type(current).__name__, message=str(current)))
            current = current.__cause__ or current.__context__
        return cls(
            type=type(error).__name__,
            message=str(error),
            causes=tuple(causes),
            requires_reconciliation=requires_reconciliation,
        )
