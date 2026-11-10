"""`standards --pytest-junit`: a pytest run CI already made stands in for the scan's own."""

from __future__ import annotations

from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gate_tests, _gates
from tests._git import git

PASSED = '<testsuites><testsuite time="1.5"><testcase classname="t" name="test_a"/></testsuite></testsuites>'
FAILED = (
    '<testsuites><testsuite time="1.5"><testcase classname="t" name="test_a"><failure message="x"/>'
    "</testcase></testsuite></testsuites>"
)
RULE = "ACME-005"


@pytest.fixture
def standards(tmp_path):
    languages = tmp_path / "standards" / "languages"
    languages.mkdir(parents=True)
    (languages / "python.md").write_text(
        f"# languages/python\n\ndetect: **/*.py\n\n## {RULE} MUST\nRun behaviour tests with pytest.\n\n"
        "Why: Evidence.\n\nCheck: python -m pytest\n\n"
        "Enforced: command python -m pytest\n"
    )
    return tmp_path / "standards"


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "lib.py").write_text('"""A library."""\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def _junit(directory: Path, project: Path, fast: str = PASSED, slow: str | None = PASSED, head=None) -> Path:
    directory.mkdir()
    (directory / "fast.xml").write_text(fast)
    if slow is not None:
        (directory / "slow.xml").write_text(slow)
    (directory / "head").write_text(head or git(project, "rev-parse", "HEAD").strip())
    return directory


@pytest.fixture
def started(monkeypatch):
    """Pytest runs the scan starts, without running any."""
    runs: list[Path] = []

    def fake(project, child_lanes=False):
        # A scan runs its lanes here: only the pre-review sends them to child actions.
        assert child_lanes is False
        runs.append(project)
        return _gate_tests.HeadRun(0, "ran", [_gate_tests.JunitCase("t", "test_a", True)])

    monkeypatch.setattr(_gate_tests, "_run_head_pytest", fake)
    return runs


def _pytest_result(project, standards, junit):
    results = _gates.standards_scan(project, standards, junit)
    return next(r for r in results if r.rule == RULE)


def test_passing_junit_for_head_stands_in_for_pytest(tmp_path, project, standards, started):
    junit = _junit(tmp_path / "junit", project)
    result = _pytest_result(project, standards, junit)
    assert result.status is _gate_base.Status.PASS
    assert not started
    assert f"reused from {junit}: 2 passed" in result.output


def test_without_junit_pytest_runs(project, standards, started):
    assert _pytest_result(project, standards, None).status is _gate_base.Status.PASS
    assert started


def test_junit_for_another_commit_is_not_reused(tmp_path, project, standards, started):
    _pytest_result(project, standards, _junit(tmp_path / "junit", project, head="0" * 40))
    assert started


def test_missing_slow_lane_is_not_reused(tmp_path, project, standards, started):
    _pytest_result(project, standards, _junit(tmp_path / "junit", project, slow=None))
    assert started


def test_failing_test_is_run_again(tmp_path, project, standards, started):
    _pytest_result(project, standards, _junit(tmp_path / "junit", project, fast=FAILED))
    assert started


def test_unparsable_junit_is_not_reused(tmp_path, project, standards, started):
    _pytest_result(project, standards, _junit(tmp_path / "junit", project, fast="<testsuites"))
    assert started


def test_cli_passes_the_directory_to_the_scan(tmp_path, project, standards, monkeypatch):
    seen = []

    def scan(*args):
        seen.append(args)
        return []

    monkeypatch.setattr(_gates, "standards_scan", scan)
    junit = _junit(tmp_path / "junit", project)
    argv = ["standards", str(project), "--standards", str(standards), "--pytest-junit", str(junit)]
    assert _gates.main(argv) == 0
    assert seen == [(project.resolve(), standards.resolve(), junit)]


def test_cli_rejects_a_junit_path_that_is_not_a_directory(tmp_path, project, standards, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _gates.main(["standards", str(project), "--pytest-junit", str(tmp_path / "missing")])
    assert exit_info.value.code == 2
    assert "not a directory" in capsys.readouterr().err


def test_launcher_text_in_a_passing_shared_run_is_not_a_missing_pytest(project, standards, monkeypatch):
    # The run already decided pytest is present: a child-lane fallback note quotes the launcher's own words.
    note = "child lane not run (exit 1: No module named pytest; missing pytest completion): ran here\n"

    def fake(project, child_lanes=False):
        return _gate_tests.HeadRun(0, note + "1 passed", [_gate_tests.JunitCase("t", "test_a", True)])

    monkeypatch.setattr(_gate_tests, "_run_head_pytest", fake)
    assert _pytest_result(project, standards, None).status is _gate_base.Status.PASS
