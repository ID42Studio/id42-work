import re
import subprocess
import sys
from importlib.resources import files

import pytest

import id42_work
from id42_work.storage.postgres import PostgresStorage


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+|\.dev\d+)?", id42_work.__version__)


def test_package_is_typed() -> None:
    assert files("id42_work").joinpath("py.typed").is_file()


def test_importing_the_package_does_not_import_mcp() -> None:
    script = (
        "import sys, id42_work; "
        "assert 'mcp' not in sys.modules; "
        "assert 'id42_work.mcp' not in sys.modules"
    )
    # The source of the package must not reference the optional server.
    source = files("id42_work").joinpath("__init__.py").read_text(encoding="utf-8")
    assert "id42_work.mcp" not in source
    assert "import mcp" not in source
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_postgres_storage_is_exported_on_demand() -> None:
    assert id42_work.PostgresStorage is PostgresStorage
    assert "PostgresStorage" in id42_work.__all__
    missing = "NotAThing"
    with pytest.raises(AttributeError, match="no attribute"):
        getattr(id42_work, missing)
