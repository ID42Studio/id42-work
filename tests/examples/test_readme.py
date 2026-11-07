"""The README's minimal example must run as written."""

import re
import subprocess
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


def test_readme_minimal_example_runs() -> None:
    match = re.search(r"## Minimal example\n\n```python\n(.*?)```", README.read_text(), re.S)
    assert match is not None
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", match.group(1)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Hello, Ada!" in result.stdout
