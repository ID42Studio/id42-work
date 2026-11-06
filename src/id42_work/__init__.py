"""ID42 Work SDK: define work, govern execution, verify outcomes."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__: str = version("id42-work")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0"

__all__ = ["__version__"]
