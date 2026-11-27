"""Every runnable example must succeed (specification 15, 18)."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
NAMES = sorted(p.name for p in EXAMPLES.glob("*.py") if not p.name.startswith("_"))


def test_all_specified_examples_exist() -> None:
    assert NAMES == [
        "agent_invoice.py",
        "async_execution.py",
        "fake_odoo_invoice.py",
        "hello_work.py",
        "human_approval.py",
        "invoice_validation.py",
        "mcp_greeting.py",
        "postgres_resume.py",
        "sqlite_resume.py",
    ]


@pytest.mark.parametrize("name", NAMES)
def test_example_runs(name: str) -> None:
    if name == "postgres_resume.py" and not os.environ.get("ID42_POSTGRES_DSN", "").strip():
        pytest.skip("ID42_POSTGRES_DSN is not set")
    if name == "mcp_greeting.py" and importlib.util.find_spec("mcp") is None:
        pytest.skip("mcp extra is not installed")
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(EXAMPLES / name)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1] == "OK"
    assert result.stderr == ""  # the library logs nothing unless the application asks
