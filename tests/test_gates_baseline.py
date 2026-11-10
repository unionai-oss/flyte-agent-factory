"""The language gate judges a root-layout project on regressions against its starting tree."""

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _gate_base, _gate_start, _gates

pytestmark = pytest.mark.real_factory_gates

# Only a rule from a language pack is compared with the start tree.
RULES = {
    "languages/checks.md": "## LANG-901 MUST\nEnforced: tool fake-lint\n",
    "engineering.md": "## SUMMARY-1 MUST\nEnforced: check factory:language-summary\n",
}

# Captured from the real tools (ruff 0.15 and mypy 1.x on a two-line module, pytest 8 on a failing test).
RUFF_FULL = """\
D100 Missing docstring in public module
--> a.py:1:1

F401 [*] `os` imported but unused
 --> a.py:1:8
  |
1 | import os
  |        ^^
  |
help: Remove unused import: `os`

D103 Missing docstring in public function
 --> a.py:3:5
  |
3 | def f(): pass
  |     ^

Found 3 errors.
[*] 1 fixable with `--fix`.
"""
RUFF_CONCISE = """\
a.py:1:1: D100 Missing docstring in public module
a.py:1:8: F401 [*] `os` imported but unused
a.py:3:5: D103 Missing docstring in public function
Found 3 errors.
"""
MYPY = """\
a.py:2: error: Name "y" is not defined  [name-defined]
a.py:3: error: Function is missing a return type annotation  [no-untyped-def]
a.py:3: note: Use "-> None" if function does not return a value
Found 2 errors in 1 file (checked 1 source file)
"""
PYTEST = """\
=================================== FAILURES ===================================
____________________________________ test_a ____________________________________

    def test_a():
>       assert 1 == 2
E       assert 1 == 2

test_a.py:2: AssertionError
=========================== short test summary info ============================
FAILED test_a.py::test_a - assert 1 == 2
ERROR tests/test_b.py::test_two
============================== 1 failed, 1 error in 0.01s ==============================
"""
PYTEST_COLLECTION = """\
ERROR: file or directory not found: nope.py

no tests ran in 0.00s
"""
PYTEST_INTERRUPTED = """\
ERROR collecting test_a.py
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR test_a.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
"""


@pytest.mark.parametrize(
    ("output", "keys"),
    [
        (
            RUFF_FULL,
            {
                ("a.py", "D100", "Missing docstring in public module"): 1,
                ("a.py", "F401", "`os` imported but unused"): 1,
                ("a.py", "D103", "Missing docstring in public function"): 1,
            },
        ),
        (
            RUFF_CONCISE,
            {
                ("a.py", "D100", "Missing docstring in public module"): 1,
                ("a.py", "F401", "`os` imported but unused"): 1,
                ("a.py", "D103", "Missing docstring in public function"): 1,
            },
        ),
        (
            MYPY,
            {
                ("a.py", "name-defined", 'Name "y" is not defined'): 1,
                ("a.py", "no-untyped-def", "Function is missing a return type annotation"): 1,
            },
        ),
        (
            PYTEST,
            {("test_a.py::test_a", "FAILED", ""): 1, ("tests/test_b.py::test_two", "ERROR", ""): 1},
        ),
        (
            "FAILED t.py::test_sign[utf-8 - nobom] - assert -1 == 1\n",
            {("t.py::test_sign[utf-8 - nobom]", "FAILED", ""): 1},
        ),
        (
            "FAILED my dir/t.py::test_new - boom\n",
            {("my dir/t.py::test_new - boom", "FAILED", ""): 1},
        ),
        ("", {}),
        ("Found 3 errors.\n", {}),
        ("command timed out", {}),
        ("src/a.ts(1,7): error TS2322: Type 'string' is not assignable to type 'number'.\n", {}),
    ],
)
def test_finding_keys(output, keys):
    """Each tool's output reduces to its (path, code, message) keys."""
    assert dict(_gate_start.finding_keys(output)) == keys


def test_ruff_full_and_concise_formats_give_the_same_keys():
    """The full and concise ruff formats give the same keys."""
    assert _gate_start.finding_keys(RUFF_FULL) == _gate_start.finding_keys(RUFF_CONCISE)


def test_line_numbers_are_not_part_of_a_key():
    """Moving a finding to another line does not change its key."""
    moved = RUFF_CONCISE.replace("a.py:3:5", "a.py:30:5")
    assert _gate_start.finding_keys(moved) == _gate_start.finding_keys(RUFF_CONCISE)


@pytest.fixture
def tool(tmp_path: Path) -> dict[str, str]:
    """Return rules for a fake linter that prints `findings.txt` and fails if there is any."""
    script = tmp_path / "fake-lint"
    script.write_text("#!/bin/sh\nif [ -s findings.txt ]; then cat findings.txt; exit 1; fi\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {**RULES, "languages/checks.md": RULES["languages/checks.md"].replace("fake-lint", str(script))}


def _tree(path: Path, findings: int | None) -> Path:
    """Make a tree whose fake-lint run reports `findings` diagnostics; None makes it pass."""
    path.mkdir()
    if findings:
        (path / "findings.txt").write_text("".join(f"a.py:{n}:1: E1 bad\n" for n in range(1, findings + 1)))
    return path


def _run(tmp_path: Path, tool: dict[str, str], head: int | None, start: int | bool | None) -> dict[str, Any]:
    results = _gates.standards_results(
        _tree(tmp_path / "head", head),
        tool,
        configs=tmp_path / "configs",
        start=None if start is False else _tree(tmp_path / "start", start),
    )
    return {r.rule: r for r in results}


@pytest.mark.parametrize(("head", "start"), [(3, 3), (2, 3)])
def test_pre_existing_failure_passes_with_the_prefix(tmp_path, tool, head, start):
    """A failure the start tree shares passes, marked pre-existing, while it does not grow."""
    results = _run(tmp_path, tool, head, start)
    assert results["LANG-901"].status is _gate_base.Status.PASS
    assert (
        f"pre-existing (the starting tree fails too: {start} findings, now {head})"
        in results["LANG-901"].output
    )
    assert "a.py:1:1: E1 bad" in results["LANG-901"].output
    assert results["factory:language-summary"].status is _gate_base.Status.PASS


def test_more_findings_than_the_start_is_a_regression(tmp_path, tool):
    """More findings than the start is a regression and fails the rule."""
    results = _run(tmp_path, tool, 4, 3)
    assert results["LANG-901"].status is _gate_base.Status.FAIL
    assert "regression: 1 finding(s) not on the starting tree:\n  a.py E1 bad" in results["LANG-901"].output
    assert results["factory:language-summary"].status is _gate_base.Status.FAIL


def test_failing_where_the_start_passed_is_a_new_failure(tmp_path, tool):
    """A tool that passed on the start and fails now is a new failure."""
    results = _run(tmp_path, tool, 1, None)
    assert results["LANG-901"].status is _gate_base.Status.FAIL
    assert "new failure" in results["LANG-901"].output


def test_unparseable_failure_stays_a_failure(tmp_path, tool):
    """A failure with no parseable finding stays a failure."""
    head = _tree(tmp_path / "head", None)
    (head / "findings.txt").write_text("something broke\n")
    start = _tree(tmp_path / "start", 2)
    (result,) = [
        r
        for r in _gates.standards_results(head, tool, configs=tmp_path / "c", start=start)
        if r.rule == "LANG-901"
    ]
    assert result.status is _gate_base.Status.FAIL


def _by_tree(monkeypatch: pytest.MonkeyPatch, head: tuple[int, str], start: tuple[int, str]) -> None:
    """Make `_run` answer `head` and `start` (each `(exit code, output)`) by the tree it runs in."""
    monkeypatch.setattr(_gate_base, "_run", lambda args, cwd, **kw: head if cwd.name == "head" else start)


def _judge(
    tmp_path: Path,
    tool: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    head: tuple[int, str],
    start: tuple[int, str],
    rule: str = "LANG-901",
) -> Any:
    _by_tree(monkeypatch, head, start)
    (head_dir, start_dir) = (tmp_path / "head", tmp_path / "start")
    head_dir.mkdir()
    start_dir.mkdir()
    results = _gates.standards_results(head_dir, tool, configs=tmp_path / "c", start=start_dir)
    return {r.rule: r for r in results}[rule]


def test_timeout_on_both_sides_still_fails(tmp_path, monkeypatch, tool):
    """A timeout on both trees is not a verdict and still fails."""
    timed_out = (124, "command timed out after 5 seconds")
    result = _judge(tmp_path, tool, monkeypatch, timed_out, timed_out)
    assert result.status is _gate_base.Status.FAIL
    assert "pre-existing" not in result.output


def test_truncated_output_does_not_hide_a_regression(tmp_path, tool):
    """Output truncation on the head run cannot hide a regression."""
    # `_run` records only the last 12000 characters; 1000 -> 2000 findings looked flat through it.
    results = _run(tmp_path, tool, 2000, 1000)
    assert results["LANG-901"].status is _gate_base.Status.FAIL
    assert "regression" in results["LANG-901"].output
    assert len(results["LANG-901"].output) < 13000 + 2000


def test_run_keeps_the_full_output_when_asked(tmp_path):
    """`_run` keeps the whole output with `tail=None` and the last 12000 characters otherwise."""
    (tmp_path / "findings.txt").write_text("x" * 20000)
    code, kept = _gate_base._run(["cat", "findings.txt"], tmp_path, tail=None)
    assert (code, len(kept)) == (0, 20000)
    assert len(_gate_base._run(["cat", "findings.txt"], tmp_path)[1]) == 12000


def test_fixing_one_finding_and_adding_another_is_a_regression(tmp_path, monkeypatch, tool):
    """Fixing one finding while adding another is still a regression."""
    swapped = RUFF_CONCISE.replace("D103 Missing docstring in public function", "E501 Line too long")
    result = _judge(tmp_path, tool, monkeypatch, (1, swapped), (1, RUFF_CONCISE))
    assert result.status is _gate_base.Status.FAIL
    assert "a.py E501 Line too long" in result.output
    assert "D103" not in result.output.split("\n", 2)[1]


def test_pytest_traceback_frames_are_not_findings(tmp_path, monkeypatch, tool):
    """Extra pytest traceback frames are not counted as findings."""
    # The same failing test, now with a second traceback frame line `test_a.py:9: AssertionError`.
    deeper = PYTEST.replace(
        "test_a.py:2: AssertionError", "test_a.py:2: AssertionError\ntest_a.py:9: AssertionError"
    )
    result = _judge(tmp_path, tool, monkeypatch, (1, deeper), (1, PYTEST))
    assert result.status is _gate_base.Status.PASS


def test_mypy_notes_are_not_findings(tmp_path, monkeypatch, tool):
    """A mypy note is not counted as a finding."""
    noisy = MYPY + 'a.py:4: note: Use "-> None" if function does not return a value\n'
    result = _judge(tmp_path, tool, monkeypatch, (1, noisy), (1, MYPY))
    assert result.status is _gate_base.Status.PASS


def test_a_new_pytest_failure_is_a_regression(tmp_path, monkeypatch, tool):
    """A test failing now that did not fail on the start is a regression."""
    more = PYTEST.replace("ERROR tests/test_b.py::test_two", "FAILED test_a.py::test_other - boom")
    result = _judge(tmp_path, tool, monkeypatch, (1, more), (1, PYTEST))
    assert result.status is _gate_base.Status.FAIL
    assert "test_a.py::test_other" in result.output


def test_new_findings_are_reported_up_to_twenty(tmp_path, monkeypatch, tool):
    """A regression lists at most twenty of the new findings, with the full count."""
    many = "".join(f"a.py:{n}:1: E{n} bad\n" for n in range(1, 31))
    result = _judge(tmp_path, tool, monkeypatch, (1, many), (1, ""))
    assert result.status is _gate_base.Status.FAIL
    assert "regression: 30 finding(s)" in result.output
    assert result.output.count("  a.py E") == 20


@pytest.mark.parametrize(
    "head",
    [(2, PYTEST_INTERRUPTED), (4, "ERROR: usage: pytest"), (5, PYTEST_COLLECTION), (1, PYTEST_COLLECTION)],
)
def test_a_run_that_is_not_a_verdict_is_never_compared(tmp_path, monkeypatch, tool, head):
    """A run that is not a verdict (timeout, no output) is never compared with the start."""
    # The start fails with the very same findings, so a comparison would pass it.
    result = _judge(tmp_path, tool, monkeypatch, head, (1, head[1]))
    assert result.status is _gate_base.Status.FAIL
    assert "pre-existing" not in result.output


def test_a_crash_stays_a_failure(tmp_path, monkeypatch, tool):
    """A Python crash is a failure even when the start failed the same tool."""
    trace = "Traceback (most recent call last):\nImportError: cannot import name 'x'\n"
    result = _judge(tmp_path, tool, monkeypatch, (1, trace), (1, RUFF_CONCISE))
    assert result.status is _gate_base.Status.FAIL


def test_only_language_rules_are_compared_with_the_start(tmp_path, monkeypatch, tool):
    """Only language-pack rules compare with the start; any other rule fails as before."""
    rules = {
        "checks.md": "## OTHER-901 MUST\nEnforced: tool "
        + tool["languages/checks.md"].split("tool ")[1].split("\n")[0]
    }
    result = _judge(tmp_path, rules, monkeypatch, (1, RUFF_CONCISE), (1, RUFF_CONCISE), rule="OTHER-901")
    assert result.status is _gate_base.Status.FAIL


def test_without_a_start_a_failure_is_unchanged(tmp_path, tool):
    """Without a start tree a failure keeps its plain verdict."""
    results = _run(tmp_path, tool, 3, False)
    assert results["LANG-901"].status is _gate_base.Status.FAIL
    assert "pre-existing" not in results["LANG-901"].output
    assert results["factory:language-summary"].status is _gate_base.Status.FAIL


@pytest.fixture
def gate_calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Collect the `start` each DONE-gate run hands to `standards_results`."""
    calls: list[Any] = []

    def record(project: Any, files: Any, **kw: Any) -> list[Any]:
        calls.append(kw.get("start"))
        return []

    monkeypatch.setattr(_gates, "standards_results", record)
    return calls


@pytest.mark.parametrize(("layout", "has_start"), [("root", True), ("sandbox", False)])
def test_done_gate_passes_the_start_tree_for_root_layout_only(project, gate_calls, layout, has_start):
    """Only the root layout hands the start tree to the DONE gate."""
    _gates.run(project, "app", "", "", "DONE", {}, _Timeline(), layout)
    assert [s is not None for s in gate_calls] == [has_start]


def test_done_gate_reports_a_start_tree_it_cannot_build(project, gate_calls, monkeypatch):
    """A failed start tree is a blocking result, not a silent fall back to judging the whole tree."""
    monkeypatch.setattr(_gates, "_project_commits", lambda *args: (128, "bad revision"))

    def refuse(*args: Any) -> Any:
        raise _gate_start._GateError("git archive failed: boom")

    monkeypatch.setattr(_gate_start, "_start_tree", refuse)
    failures = _gates.run(project, "app", "", "", "DONE", {}, _Timeline(), "root")
    assert gate_calls == [None]
    assert "git archive failed: boom" in failures
    assert "whole tree" in failures


@pytest.fixture
def project(tmp_path):
    """Create a git repository in `tmp_path` with one commit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    for args in (
        ["init"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "test"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    for args in (["add", "."], ["commit", "-m", "base"]):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)
    _gates.capture_base(tmp_path, "app")
    return tmp_path


class _Timeline:
    review_round = 0

    def row(self, *args):
        pass
