import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def run(tmp_path):
    """Run `python -m logstats` from the project root; returns the CompletedProcess."""

    def _run(*args, stdin=None):
        return subprocess.run(
            [sys.executable, "-m", "logstats", *args],
            cwd=ROOT,
            input=stdin,
            capture_output=True,
            check=False,
        )

    return _run


@pytest.fixture
def log(tmp_path):
    """Write lines to a plain log file and return its path as a string."""

    def _log(*lines, name="access.log"):
        path = tmp_path / name
        path.write_bytes(("\n".join(lines) + "\n").encode())
        return str(path)

    return _log
