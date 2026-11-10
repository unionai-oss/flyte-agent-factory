"""The gate's ruff rules skip `.claude-flyte/`, as its own file walks do, even where git does not ignore it.

Other projects' harness files are committed with `git add -f`, so a CI checkout has them and nothing ignores
them there. A `lint` rule runs ruff with `--isolated`, which reads no config file, so only the gate can
exclude them.
"""

from __future__ import annotations

import pytest

from flyte_agent_factory import _gate_base, _gates
from tests._git import git

RULE = "ACME-007"
BLIND = "def f():\n    try:\n        return 1\n    except Exception:\n        return 2\n"


@pytest.fixture
def standards(tmp_path):
    directory = tmp_path / "standards"
    directory.mkdir()
    (directory / "engineering.md").write_text(
        f"# engineering\n\n## {RULE} MUST\nCatch narrow exceptions.\n\nWhy: Evidence.\n\nCheck: ruff.\n\n"
        "Enforced: lint ruff BLE001\n"
    )
    return directory


def _project(tmp_path, files: dict[str, str]):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "lib.py").write_text('"""A library."""\n')
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-f", ".")
    git(repo, "commit", "-m", "init")
    return repo


def _status(project, standards):
    [result] = [r for r in _gates.standards_scan(project, standards) if r.rule == RULE]
    return result


def test_a_tracked_harness_file_is_not_linted(tmp_path, standards):
    project = _project(tmp_path, {".claude-flyte/other/probe.py": BLIND})
    result = _status(project, standards)
    assert result.status is _gate_base.Status.PASS, result.output


def test_the_same_code_outside_the_harness_directory_still_fails(tmp_path, standards):
    project = _project(tmp_path, {"app/probe.py": BLIND})
    result = _status(project, standards)
    assert result.status is _gate_base.Status.FAIL
    assert "BLE001" in result.output and "app/probe.py" in result.output
