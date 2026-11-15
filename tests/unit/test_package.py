import re
from importlib.resources import files

import pytest

import id42_work
from id42_work.storage.postgres import PostgresStorage


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+|\.dev\d+)?", id42_work.__version__)


def test_package_is_typed() -> None:
    assert files("id42_work").joinpath("py.typed").is_file()


def test_postgres_storage_is_exported_on_demand() -> None:
    assert id42_work.PostgresStorage is PostgresStorage
    assert "PostgresStorage" in id42_work.__all__
    missing = "NotAThing"
    with pytest.raises(AttributeError, match="no attribute"):
        getattr(id42_work, missing)
