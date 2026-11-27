"""Errors an MCP client is allowed to see."""


class McpCallError(Exception):
    """A call the client can see. ``found`` is false when the execution is hidden."""

    def __init__(self, message: str, *, found: bool = True) -> None:
        super().__init__(message)
        self.found = found
