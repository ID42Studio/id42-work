import re
from importlib.resources import files

import id42_work


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+|\.dev\d+)?", id42_work.__version__)


def test_package_is_typed() -> None:
    assert files("id42_work").joinpath("py.typed").is_file()
