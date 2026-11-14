"""The gate and the pre-review run each expensive thing once: pytest, the start tree's tools, mypy's cache."""

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_tests,
    _gates,
    _session_config,
    _supervise_support,
    _timeline,
)

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.real_factory_gates

PYTEST_RULE = {
    "python.md": "## TESTS-1 MUST\nEnforced: tool python -m pytest\nTimeout: 120\n",
    "engineering.md": "## TESTED-1 MUST\nEnforced: check factory:fail-to-pass\n",
}


def git(path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A sandbox-layout repo whose project `app` changed a function and the test that describes it."""
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("def answer():\n    return 1\n")
    (app / "test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 1\n"
    )
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    git(tmp_path, "add", "app", ".gitignore")
    git(tmp_path, "commit", "-m", "base")
    _gates.capture_base(tmp_path, "app")
    git(tmp_path, "config", "user.email", "app@agent-factory.localhost")
    (app / "main.py").write_text("def answer():\n    return 2\n")
    (app / "test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    git(tmp_path, "add", "app")
    git(tmp_path, "commit", "-m", "change answer")
    return tmp_path


class Timeline:
    review_round = 0

    def row(self, *args: Any) -> None:
        pass


def gate(workspace: Path, work: _gate_tests.GateWork | None = None) -> str:
    return _gates.run(workspace, "app", "", "", "DONE", PYTEST_RULE, Timeline(), "sandbox", [], work)


@pytest.fixture
def pytest_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, list[str]]]:
    """Record each pytest the gate starts, as (directory it ran in, argv), and run it for real."""
    runs: list[tuple[Path, list[str]]] = []
    real = _gate_base._run

    def counting(args: list[str], cwd: Path, **kw: Any) -> tuple[int, str]:
        if "pytest" in " ".join(args):
            runs.append((cwd, args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", counting)
    return runs


def on_head(runs: list[tuple[Path, list[str]]], workspace: Path) -> list[list[str]]:
    return [args for cwd, args in runs if workspace in cwd.parents or cwd == workspace]


def write_junit(directory: Path, head: str, *, passed: bool = True) -> None:
    """Leave what `checks.sh` leaves: both lanes' junit files and the commit they ran on."""
    outcome = "" if passed else "<failure message='assert 0'/>"
    directory.mkdir(exist_ok=True)
    (directory / "head").write_text(head + "\n")
    (directory / "fast.xml").write_text(
        "<testsuites><testsuite name='pytest' time='3.0'>"
        f"<testcase classname='test_main' name='test_answer'>{outcome}</testcase></testsuite></testsuites>"
    )
    (directory / "slow.xml").write_text(
        "<testsuites><testsuite name='pytest' time='42.5'>"
        "<testcase classname='test_other' name='test_slow'/></testsuite></testsuites>"
    )


def test_the_head_tree_is_tested_once_for_pr_005_and_eng_004(workspace, pytest_runs):
    """A pytest rule and the fail-to-pass check read one pytest run of the head tree, not two."""
    assert gate(workspace, _gate_tests.GateWork()) == ""
    assert len(on_head(pytest_runs, workspace)) == 1
    assert "--junitxml" in " ".join(on_head(pytest_runs, workspace)[0])


def test_the_gate_reads_the_pytest_run_checks_sh_made(workspace, pytest_runs, tmp_path):
    """A junit file from checks.sh, made on this commit after the checks began, replaces the head run."""
    since = time.time() - 1
    write_junit(tmp_path / "junit", git(workspace, "rev-parse", "HEAD"))
    work = _gate_tests.GateWork(junit_dir=tmp_path / "junit", checks_started=since)
    assert gate(workspace, work) == ""
    assert on_head(pytest_runs, workspace) == []
    assert work.pytest_reused
    assert work.slow_seconds == 42.5


@pytest.mark.parametrize(
    "fault", ["older than the checks", "another commit", "slow lane missing", "failing", "harness changed"]
)
def test_a_junit_file_that_cannot_stand_in_for_a_run_is_not_used(workspace, pytest_runs, tmp_path, fault):
    """An old, foreign, partial or failing run is ignored: the gate runs the tests itself."""
    head = "0" * 40 if fault == "another commit" else git(workspace, "rev-parse", "HEAD")
    write_junit(tmp_path / "junit", head, passed=fault != "failing")
    since = time.time() + 60 if fault == "older than the checks" else time.time() - 1
    if fault == "slow lane missing":
        (tmp_path / "junit" / "slow.xml").unlink()
    refused = "tests/conftest.py" if fault == "harness changed" else ""
    work = _gate_tests.GateWork(junit_dir=tmp_path / "junit", checks_started=since, reuse_refused=refused)
    assert gate(workspace, work) == ""
    assert len(on_head(pytest_runs, workspace)) == 1
    assert not work.pytest_reused


def test_the_base_run_is_kept_for_the_next_gate(workspace, pytest_runs):
    """The fail-to-pass base run depends on the start tree and the tests only, so a second gate reads it."""
    assert gate(workspace, _gate_tests.GateWork()) == ""
    first = _gate_tests.GateWork()
    assert gate(workspace, first) == ""
    base = [args for cwd, args in pytest_runs if workspace not in cwd.parents and cwd != workspace]
    assert len(base) == 1
    assert (first.base_runs, first.base_hits) == (1, 1)


def counting_tool(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Rules for a linter that logs the directory it ran in and fails when the tree has `findings.txt`."""
    log = tmp_path / "calls.log"
    script = tmp_path / "counting-lint"
    script.write_text(
        f'#!/bin/sh\nbasename "$PWD" >> {log}\n[ -s findings.txt ] && cat findings.txt && exit 1\nexit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return {"languages/checks.md": f"## LANG-901 MUST\nEnforced: tool {script}\n"}, log


def tree(path: Path, findings: str) -> Path:
    path.mkdir(parents=True)
    (path / "findings.txt").write_text(findings)
    return path


def test_a_start_trees_tool_run_is_kept_between_gate_runs(tmp_path):
    """The same start tree is judged by one tool run, however many gates look at it."""
    rules, log = counting_tool(tmp_path)
    for attempt in ("one", "two"):
        head = tree(tmp_path / attempt / "head", "a.py:1:1: E1 bad\n")
        start = tree(tmp_path / attempt / "start", "a.py:1:1: E1 bad\n")
        (result,) = _gates.standards_results(head, rules, configs=tmp_path / "c", start=start)
        assert "pre-existing" in result.output
    assert log.read_text().split() == ["head", "start", "head"]


def test_a_changed_start_tree_is_not_read_from_the_cache(tmp_path):
    """The cache is keyed by the tree's content: other content is a new run."""
    rules, log = counting_tool(tmp_path)
    for attempt, text in (("one", "a.py:1:1: E1 bad\n"), ("two", "a.py:1:1: E1 bad\na.py:2:1: E1 bad\n")):
        head = tree(tmp_path / attempt / "head", "a.py:1:1: E1 bad\n")
        start = tree(tmp_path / attempt / "start", text)
        _gates.standards_results(head, rules, configs=tmp_path / "c", start=start)
    assert log.read_text().split() == ["head", "start", "head", "start"]


def test_a_timeout_is_not_kept(tmp_path, monkeypatch):
    """A run that timed out says nothing about the tree, so the next gate tries again."""
    calls = []

    def timed_out() -> tuple[int, str]:
        calls.append(1)
        return 124, "command timed out"

    for _ in range(2):
        _gate_tests._cached_run(tmp_path, "key", "LANG-901", ["tool"], timed_out, None)
    assert len(calls) == 2


def test_tree_key_follows_content_and_ignores_tool_caches(tmp_path):
    """Equal content has one key wherever it lives; a cache a tool leaves behind does not change it."""
    one, two = tree(tmp_path / "one", "x"), tree(tmp_path / "two", "x")
    assert _gate_tests.tree_key(one) == _gate_tests.tree_key(two)
    (one / ".mypy_cache").mkdir()
    (one / ".mypy_cache" / "data.json").write_text("{}")
    assert _gate_tests.tree_key(one) == _gate_tests.tree_key(two)
    (two / "findings.txt").write_text("y")
    assert _gate_tests.tree_key(one) != _gate_tests.tree_key(two)


def test_the_cache_lives_under_home(tmp_path, monkeypatch):
    """The cache root is derived from $HOME (the container user's only writable place), not hardcoded."""
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _gate_tests.cache_root() == tmp_path / ".cache" / "factory-gate"


def test_mypy_keeps_an_incremental_cache_per_tree_kind(tmp_path, monkeypatch):
    """Head and start each get a persistent --cache-dir, so neither rebuilds the other's."""
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "mypy").write_text("#!/bin/sh\n")
    (tools / "mypy").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    commands = []

    def run(args, cwd, **kw):
        commands.append((cwd.name, args))
        return 1, "a.py:1: error: bad  [misc]"

    monkeypatch.setattr(_gate_base, "_run", run)
    head, start = tree(tmp_path / "head", ""), tree(tmp_path / "start", "")
    rules = {"languages/python.md": "## TYPES-1 MUST\nEnforced: tool mypy .\n"}
    _gates.standards_results(head, rules, configs=tmp_path / "c", start=start)
    root = _gate_tests.cache_root() / "mypy"
    assert [(cwd, args[1:3]) for cwd, args in commands] == [
        ("head", ["--cache-dir", str(root / "head")]),
        ("start", ["--cache-dir", str(root / "start")]),
    ]


@pytest.mark.parametrize(
    ("reused", "base_runs", "base_hits", "base_seconds", "expected"),
    [
        (
            True,
            1,
            1,
            0.1,
            "pre-review 212s: checks 140s (slow lane 95s), gate 60s (pytest reused), base cached",
        ),
        (False, 2, 1, 31.0, "pre-review 212s: checks 140s (slow lane 95s), gate 60s, base 31s"),
        (False, 0, 0, 0.0, "pre-review 212s: checks 140s (slow lane 95s), gate 60s"),
    ],
)
def test_the_pre_review_row_says_where_the_time_went(reused, base_runs, base_hits, base_seconds, expected):
    """One row: the total, then checks, the slow lane, the gate and the base runs."""
    work = _gate_tests.GateWork(
        pytest_reused=reused, base_runs=base_runs, base_hits=base_hits, slow_seconds=95.2
    )
    work.seconds.update(gate=60.4 + base_seconds, base=base_seconds)
    assert _supervise_support._pre_review_row(212.4, 140.2, work) == expected


def test_the_gate_reports_its_time_and_what_it_reused(workspace, tmp_path):
    """After a gate, the work object holds the numbers the pre-review turns into its row."""
    write_junit(tmp_path / "junit", git(workspace, "rev-parse", "HEAD"))
    work = _gate_tests.GateWork(junit_dir=tmp_path / "junit", checks_started=time.time() - 1)
    gate(workspace, work)
    assert work.seconds["gate"] > 0 and work.pytest_reused and work.base_runs == 1


def test_checks_sh_runs_both_lanes_at_once_and_leaves_their_junit(tmp_path):
    """Asked for the slow lane, checks.sh runs both lanes at once, each with its junit, then lints."""
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    junit = tmp_path / "junit"
    junit.mkdir()
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.PYTEST_JUNIT_ENV: str(junit),
        "AGENT_FACTORY_RUN_SLOW": "1",
    }
    subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env, check=True)
    calls = log.read_text().splitlines()
    lane_calls = [c for c in calls if "-m pytest" in c]
    assert len(lane_calls) == 2
    # No launcher (AGENT_FACTORY_TEST_LANE) and no request for a child action: both lanes run here.
    assert any(f"-m not slow --junitxml {junit}/fast.xml" in c for c in lane_calls)
    assert any(f"-m slow --junitxml {junit}/slow.xml" in c for c in lane_calls)
    assert (junit / "head").read_text().strip() == git(repo, "rev-parse", "HEAD")
    assert [c.split()[3:5] for c in calls if "ruff" in c] == [["ruff", "check"], ["ruff", "format"]]


def test_checks_sh_leaves_the_slow_lane_to_ci_unless_asked(tmp_path):
    """By default only `not slow` runs here: the benchmark lane would hold a 4-CPU pod past its limit."""
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    env = {
        k: v for k, v in os.environ.items() if k not in ("AGENT_FACTORY_RUN_SLOW", "AGENT_FACTORY_SLOW_LANE")
    }
    env["PATH"] = f"{uv.parent}{os.pathsep}{os.environ['PATH']}"
    run = subprocess.run(
        ["bash", str(repo / "factory-improvement/checks.sh")],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    lane_calls = [c for c in log.read_text().splitlines() if "pytest" in c or "_slow_lane" in c]
    assert len(lane_calls) == 1 and "-m not slow" in lane_calls[0]
    assert "CI's slow job runs it" in run.stderr


def test_checks_sh_with_arguments_narrows_the_run_and_leaves_no_junit(tmp_path):
    """Extra pytest arguments mean the run is not the whole suite, so the gate must not read it."""
    repo = Path(__file__).resolve().parents[1]
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {tmp_path / "uv.log"}\n')
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.PYTEST_JUNIT_ENV: str(tmp_path),
    }
    subprocess.run(
        ["bash", str(repo / "factory-improvement/checks.sh"), "-k", "x"], cwd=repo, env=env, check=True
    )
    assert "--junitxml" not in (tmp_path / "uv.log").read_text()


async def test_the_pre_review_sends_the_slow_lane_to_a_child_action(tmp_path):
    """checks.sh gets the junit directory and asks for the slow lane in a child action."""
    checks = tmp_path / _session_config._checks_rel(_session_config._project_name)
    checks.parent.mkdir(parents=True, exist_ok=True)
    seen = tmp_path / "seen.txt"
    checks.write_text(f'echo "$AGENT_FACTORY_SLOW_LANE $AGENT_FACTORY_PYTEST_JUNIT_DIR" > {seen}')
    junit = tmp_path / "junit"
    junit.mkdir()
    await _supervise_support._run_declared_checks(tmp_path, _timeline._Timeline(), junit)
    assert seen.read_text() == f"child {junit}\n"


def _harness_repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "factory-improvement").mkdir()
    (tmp_path / "factory-improvement/checks.sh").write_text("pytest\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def _project_commit(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(
        repo,
        "-c",
        "user.email=p@agent-factory.localhost",
        "commit",
        "-m",
        "change",
        "--author",
        "p <p@agent-factory.localhost>",
    )


def test_a_round_that_leaves_the_test_harness_alone_may_reuse_checks_results(tmp_path):
    repo, base = _harness_repo(tmp_path)
    _project_commit(repo, "app.py", "x = 2\n")
    assert _gates.changed_test_harness(repo, "p", base) == []


@pytest.mark.parametrize(
    "path",
    [
        "factory-improvement/checks.sh",
        "tests/conftest.py",
        "pyproject.toml",
        "pytest.ini",
        "src/flyte_agent_factory/_slow_lane.py",
        "src/flyte_agent_factory/__init__.py",
        "src/flyte_agent_factory/child.py",
        "sitecustomize.py",
        "tests/usercustomize.py",
        "pytest.py",
        "lib/py.py",
        "vendor/_pytest/python.py",
        "pytest/__init__.py",
        "site/evil.pth",
        "uv.lock",
    ],
)
def test_a_round_that_changes_the_test_harness_cannot_reuse_checks_results(tmp_path, path):
    """The session writes checks.sh and conftest.py, so a junit they left is evidence only when unchanged."""
    repo, base = _harness_repo(tmp_path)
    _project_commit(repo, path, "changed\n")
    assert _gates.changed_test_harness(repo, "p", base) == [path]


def test_an_uncommitted_harness_change_also_blocks_reuse(tmp_path):
    repo, base = _harness_repo(tmp_path)
    (repo / "conftest.py").write_text("forged\n")
    assert _gates.changed_test_harness(repo, "p", base) == ["conftest.py"]


def test_every_file_checks_sh_runs_to_make_junit_is_test_harness():
    """A file checks.sh executes decides what the junit says, so changing it must disable reuse."""
    script = (ROOT / "factory-improvement/checks.sh").read_text()
    executed = {"factory-improvement/checks.sh"}
    for module in re.findall(r"-m\s+([\w.]+)", script):
        if module.split(".")[0] == "flyte_agent_factory":
            executed.add("src/" + module.replace(".", "/") + ".py")
    executed.update(re.findall(r"(?:^|\s)((?:\./)?[\w/.-]+\.(?:py|sh))\b", script, re.MULTILINE))
    # The launcher AGENT_FACTORY_TEST_LANE names runs `_slow_lane`, and a lane it cannot place runs here.
    assert _gates._TEST_HARNESS.search("src/flyte_agent_factory/_slow_lane.py")
    for path in sorted(executed):
        assert (ROOT / path.removeprefix("./")).is_file(), path
        assert _gates._TEST_HARNESS.search(path), path


def _modules_imported_by(*python_args: str) -> set[str]:
    """Return the flyte_agent_factory modules a fresh interpreter imports running `python_args`."""
    run = subprocess.run(
        [sys.executable, "-X", "importtime", *python_args],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        check=True,
    )
    names = {line.rsplit("|", 1)[-1].strip() for line in run.stderr.splitlines() if "|" in line}
    return {name for name in names if name.split(".")[0] == "flyte_agent_factory"}


def test_every_package_module_the_slow_lane_launcher_imports_is_test_harness():
    """The launcher writes slow.xml, so a module it imports first can forge the junit."""
    imported = _modules_imported_by("-m", "flyte_agent_factory._slow_lane", "--help")
    # `main` imports child lazily, before it attaches to the run.
    imported |= _modules_imported_by("-c", "import flyte_agent_factory.child")
    imported |= {"flyte_agent_factory._slow_lane", "flyte_agent_factory.child"}
    assert "flyte_agent_factory" in imported
    for module in sorted(imported):
        path = Path("src", *module.split("."))
        path = path / "__init__.py" if (ROOT / path).is_dir() else path.with_suffix(".py")
        assert (ROOT / path).is_file(), path
        assert _gates._TEST_HARNESS.search(path.as_posix()), path


def test_checks_sh_sends_both_lanes_through_the_launcher_when_asked_for_child_actions(tmp_path):
    """With AGENT_FACTORY_SLOW_LANE=child the fast lane is a child action too.

    Each lane has its own marker. The fast lane, the dearer one on a 4-CPU pod, goes on-demand when spot has
    no room and leaves the fallback to checks.sh; the slow lane tries spot, then the launcher runs it here.
    """
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    junit = tmp_path / "junit"
    junit.mkdir()
    lane = tmp_path / "test-lane"
    lane.write_text(f'#!/bin/bash\necho "lane $*" >> {log}\n')
    lane.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.PYTEST_JUNIT_ENV: str(junit),
        _gate_tests.SLOW_LANE_ENV: "child",
        _gate_tests.TEST_LANE_ENV: str(lane),
    }
    subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env, check=True)
    lane_calls = [c for c in log.read_text().splitlines() if c.startswith("lane ")]
    assert sorted(lane_calls) == sorted(
        [
            f"lane --on-demand --no-local --junit {junit}/fast.xml -m not slow",
            f"lane --junit {junit}/slow.xml",
        ]
    )
    assert not [c for c in log.read_text().splitlines() if "-m pytest" in c], "a lane ran here"


def test_checks_sh_runs_the_slow_lane_here_when_asked_and_keeps_the_ask_from_the_suite(tmp_path):
    """AGENT_FACTORY_SLOW_LANE_HERE=1: the fast lane still goes to the launcher; the slow lane runs here.

    The variable is not passed on, so the suite's own runs of checks.sh see the default.
    """
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$* here=${{AGENT_FACTORY_SLOW_LANE_HERE:-}}" >> {log}\n')
    uv.chmod(0o755)
    junit = tmp_path / "junit"
    junit.mkdir()
    lane = tmp_path / "test-lane"
    lane.write_text(f'#!/bin/bash\necho "lane $*" >> {log}\n')
    lane.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.PYTEST_JUNIT_ENV: str(junit),
        _gate_tests.SLOW_LANE_ENV: "child",
        _gate_tests.TEST_LANE_ENV: str(lane),
        "AGENT_FACTORY_SLOW_LANE_HERE": "1",
    }
    subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env, check=True)
    calls = log.read_text().splitlines()
    assert [c for c in calls if c.startswith("lane ")] == [
        f"lane --on-demand --no-local --junit {junit}/fast.xml -m not slow"
    ]
    here = [c for c in calls if "-m pytest" in c]
    assert len(here) == 1 and f"-m slow --junitxml {junit}/slow.xml" in here[0], here
    uv_calls = [c for c in calls if not c.startswith("lane ")]
    assert uv_calls and all(c.endswith(" here=") for c in uv_calls), uv_calls


def test_checks_sh_runs_the_fast_lane_here_after_two_rounds_without_capacity(tmp_path):
    """A launcher that cannot place the fast lane (exit 75, --no-local) is asked twice; then it runs here."""
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    junit = tmp_path / "junit"
    junit.mkdir()
    lane = tmp_path / "test-lane"
    # Like the launcher: exit 75 only when told not to run the lane here; otherwise it runs it (a no-op).
    lane.write_text(f'#!/bin/bash\necho "lane $*" >> {log}\n[[ " $* " != *" --no-local "* ]] || exit 75\n')
    lane.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.PYTEST_JUNIT_ENV: str(junit),
        _gate_tests.SLOW_LANE_ENV: "child",
        _gate_tests.TEST_LANE_ENV: str(lane),
    }
    run = subprocess.run(
        ["bash", str(repo / "factory-improvement/checks.sh")],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines()
    fast = f"lane --on-demand --no-local --junit {junit}/fast.xml -m not slow"
    assert [c for c in calls if c.startswith("lane ") and "-m not slow" in c] == [fast, fast]
    here = [c for c in calls if "-m pytest" in c]
    assert len(here) == 1 and f"-m not slow --junitxml {junit}/fast.xml" in here[0], here
    assert "fast lane: running here, the child action could not be placed" in run.stderr


def test_checks_sh_fails_when_the_fast_lane_fails_in_its_child_action(tmp_path):
    """A failing lane in a child action fails the checks; it is not retried or run again here."""
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    lane = tmp_path / "test-lane"
    lane.write_text(f'#!/bin/bash\necho "lane $*" >> {log}\nexit 1\n')
    lane.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.SLOW_LANE_ENV: "child",
        _gate_tests.TEST_LANE_ENV: str(lane),
    }
    run = subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env)
    assert run.returncode == 1
    calls = log.read_text().splitlines()
    assert len([c for c in calls if c.startswith("lane ") and "-m not slow" in c]) == 1
    assert not [c for c in calls if "-m not slow" in c and "-m pytest" in c]


def test_checks_sh_runs_both_lanes_here_when_no_launcher_is_given(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f'#!/bin/bash\necho "$*" >> {log}\n')
    uv.chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if k != _gate_tests.TEST_LANE_ENV},
        "PATH": f"{uv.parent}{os.pathsep}{os.environ['PATH']}",
        _gate_tests.SLOW_LANE_ENV: "child",
    }
    subprocess.run(["bash", str(repo / "factory-improvement/checks.sh")], cwd=repo, env=env, check=True)
    ran = log.read_text().splitlines()
    assert [c for c in ran if "-m not slow" in c] and [c for c in ran if "-m slow" in c]


def _child_lane_project(tmp_path: Path, *, lock: bool = True, tests: bool = True, slow: bool = True) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    marker = '[tool.pytest.ini_options]\nmarkers = ["slow: slow tests"]\n' if slow else ""
    (project / "pyproject.toml").write_text(marker or "[project]\nname = 'p'\n")
    if lock:
        (project / "uv.lock").write_text("")
    if tests:
        (project / "tests").mkdir()
    return project


def _junit_for(path: str, name: str) -> None:
    Path(path).write_text(
        f"<testsuites><testsuite name='pytest' time='1'><testcase classname='t' name='{name}'/>"
        "</testsuite></testsuites>"
    )


class LaneProcesses(list[dict[str, Any]]):
    """Each process the gate starts for the head run, as a dict; a launcher exits `launcher_code`."""

    launcher_code = 0
    not_run_output = "slow lane: no spot capacity within 0:02:00\n"
    #: What the run-image lookup prints and exits with; its calls are kept apart from the lanes'.
    image_lookup: tuple[int, str] = (0, "DEBUG controller noise\nimage=registry.example/agent:run-image\n")
    lookups: list[dict[str, Any]]


@pytest.fixture
def lane_processes(monkeypatch: pytest.MonkeyPatch) -> LaneProcesses:
    """Fake `_gate_base._run`: launchers and local pytests both write the lane's junit, unless not run."""
    calls = LaneProcesses()
    calls.lookups = []

    def fake(args: list[str], cwd: Path, **kw: Any) -> tuple[int, str]:
        if "-c" in args:
            calls.lookups.append({"args": args, "cwd": cwd, "env": kw.get("env") or {}})
            return calls.image_lookup
        calls.append({"args": args, "cwd": cwd, "env": kw.get("env") or {}})
        if "flyte_agent_factory._slow_lane" in args:
            junit = args[args.index("--junit") + 1]
            if calls.launcher_code == _SLOW_NOT_RUN:
                return _SLOW_NOT_RUN, calls.not_run_output
            code = calls.launcher_code
            Path(args[args.index("--result") + 1]).write_text(json.dumps({"completed": True, "code": code}))
        else:
            junit = next(a.split("=", 1)[1] for a in args if a.startswith("--junitxml="))
            code = 0
        _junit_for(junit, "slow" if "slow" in args else "fast")
        return code, "ok\n"

    monkeypatch.setattr(_gate_base, "_run", fake)
    # Inside a run, which the image lookup needs; a fresh cache, so each test sees its own lookup.
    monkeypatch.setenv("FLYTE_INTERNAL_EXECUTION_ID", "run-under-test")
    monkeypatch.setattr(_gate_tests, "_RUN_IMAGES", {})
    return calls


_SLOW_NOT_RUN = 75


def test_child_lanes_send_both_lanes_through_the_deployed_launcher(tmp_path, lane_processes):
    import flyte_agent_factory

    project = _child_lane_project(tmp_path)

    run = _gate_tests._run_head_pytest(project, child_lanes=True)

    assert run is not None and run.cases is not None and {c.name for c in run.cases} == {"fast", "slow"}
    assert len(lane_processes) == 2
    package_root = str(Path(flyte_agent_factory.__file__).resolve().parent.parent)
    lanes = []
    for call in lane_processes:
        args = call["args"]
        assert args[:4] == [sys.executable, "-P", "-m", "flyte_agent_factory._slow_lane"]
        assert "--no-local" in args and "--junit" in args
        assert call["cwd"] == project
        assert call["env"]["PYTHONPATH"] == package_root and str(project) not in call["env"]["PYTHONPATH"]
        assert call["env"][_gate_tests.SLOW_LANE_ENV] == "child"
        lanes.append(args[args.index("-m", 4) + 1])
    assert sorted(lanes) == ["not slow", "slow"]


def test_a_lane_the_launcher_did_not_run_runs_here_and_says_why(tmp_path, lane_processes):
    project = _child_lane_project(tmp_path)
    lane_processes.launcher_code = _SLOW_NOT_RUN

    run = _gate_tests._run_head_pytest(project, child_lanes=True)

    assert run is not None and run.code == 0 and run.cases is not None and len(run.cases) == 2
    local = [c["args"] for c in lane_processes if "flyte_agent_factory._slow_lane" not in c["args"]]
    assert len(local) == 2 and all(a[1:3] == ["-m", "pytest"] for a in local)
    assert run.output.count("child lane not run (") == 2 and "no spot capacity" in run.output


def test_the_reason_is_the_launchers_own_line_not_the_controller_log_ahead_of_it(tmp_path, lane_processes):
    project = _child_lane_project(tmp_path)
    lane_processes.launcher_code = _SLOW_NOT_RUN
    lane_processes.not_run_output = (
        "2026-11-07T03:47:54Z DEBUG flyte_controller_base::informer: processing\n"
        "slow lane: running locally, cannot attach to a Flyte run (no run context)\n"
        "slow lane: --no-local, so it was not run here (exit 75)\n"
    )

    run = _gate_tests._run_head_pytest(project, child_lanes=True)

    assert run is not None
    assert run.output.count("child lane not run (slow lane: running locally, cannot attach") == 2
    assert "DEBUG" not in run.output


def test_child_lanes_run_on_the_runs_own_image_looked_up_once(tmp_path, lane_processes):
    """In the supervisor `FLYTE_INTERNAL_IMAGE` names the plain Flyte image, where most of a suite errors."""
    project = _child_lane_project(tmp_path)

    _gate_tests._run_head_pytest(project, child_lanes=True)

    assert len(lane_processes.lookups) == 1
    lookup = lane_processes.lookups[0]
    assert lookup["args"][:3] == [sys.executable, "-P", "-c"] and lookup["cwd"] == project
    assert [c["env"]["FLYTE_INTERNAL_IMAGE"] for c in lane_processes] == [
        "registry.example/agent:run-image"
    ] * 2


def test_without_the_runs_image_both_lanes_run_here_and_say_why(tmp_path, lane_processes):
    project = _child_lane_project(tmp_path)
    lane_processes.image_lookup = (1, "FileNotFoundError: No run context file found\n")

    run = _gate_tests._run_head_pytest(project, child_lanes=True)

    assert run is not None and run.code == 0
    assert lane_processes and all("flyte_agent_factory._slow_lane" not in c["args"] for c in lane_processes)
    assert "child lanes not used (the run's image could not be looked up)" in run.output


def test_a_launcher_that_reports_pytests_own_result_is_used_as_is(tmp_path, lane_processes):
    project = _child_lane_project(tmp_path)
    lane_processes.launcher_code = 1

    run = _gate_tests._run_head_pytest(project, child_lanes=True)

    assert run is not None and run.code == 1
    assert all("flyte_agent_factory._slow_lane" in c["args"] for c in lane_processes)


@pytest.mark.parametrize("missing", ["lock", "tests", "slow"])
def test_without_child_lanes_or_their_preconditions_the_lanes_run_here(tmp_path, lane_processes, missing):
    project = _child_lane_project(
        tmp_path, lock=missing != "lock", tests=missing != "tests", slow=missing != "slow"
    )

    _gate_tests._run_head_pytest(project, child_lanes=True)

    assert lane_processes and all("flyte_agent_factory._slow_lane" not in c["args"] for c in lane_processes)


def test_child_lanes_are_off_by_default(tmp_path, lane_processes):
    _gate_tests._run_head_pytest(_child_lane_project(tmp_path))

    assert len(lane_processes) == 2
    assert all("flyte_agent_factory._slow_lane" not in c["args"] for c in lane_processes)
    assert _gate_tests.GateWork().child_lanes is False


def test_head_pytest_passes_child_lanes_on(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        _gate_tests, "_run_head_pytest", lambda project, child_lanes=False: seen.append(child_lanes)
    )

    _gate_tests.GateWork(share_pytest=True, child_lanes=True).head_pytest(tmp_path)

    assert seen == [True]


def test_the_pre_review_runs_the_gates_own_pytest_in_child_lanes():
    source = (ROOT / "src/flyte_agent_factory/_supervise.py").read_text()
    construction = source[source.index("_gate_tests.GateWork(") :].split("\n                        )", 1)[0]

    assert "child_lanes=True" in construction


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (1, "No module named flyte_agent_factory._slow_lane\n"),
        (1, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'dependency'\n"),
        (1, "No module named pytest\n"),
        (127, "launcher not found\n"),
        (124, "command timed out after 1200 seconds\n"),
        (-9, "launcher killed\n"),
        (70, "launcher setup failed\n"),
        (0, "launcher exited without reporting\n"),
    ],
)
def test_launcher_infrastructure_failure_runs_local_pytest(
    tmp_path, monkeypatch, lane_processes, code, output
):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            return code, output
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 0
    assert result.cases is not None and {case.name for case in result.cases} == {"fast", "slow"}
    assert len(lane_processes) == 2
    assert result.output.count("child lane not run (") == 2
    assert output.strip().splitlines()[-1] in result.output


def test_launcher_os_error_runs_local_pytest(tmp_path, monkeypatch, lane_processes):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            raise PermissionError("launcher cannot execute")
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 0 and result.cases is not None
    assert len(lane_processes) == 2
    assert "launcher cannot execute" in result.output


@pytest.mark.parametrize("report", ["", "<broken", "<testsuites/>"])
def test_launcher_without_pytest_evidence_runs_here(tmp_path, monkeypatch, lane_processes, report):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            Path(args[args.index("--junit") + 1]).write_text(report)
            return 1, "launcher failed before pytest\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 0
    assert result.cases is not None and {case.name for case in result.cases} == {"fast", "slow"}
    assert len(lane_processes) == 2


@pytest.mark.parametrize("code", range(6))
def test_child_pytest_results_and_junit_are_preserved(tmp_path, monkeypatch, lane_processes, code):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            junit = Path(args[args.index("--junit") + 1])
            Path(args[args.index("--result") + 1]).write_text(json.dumps({"completed": True, "code": code}))
            failure = "<failure message='assertion failed'/>" if code == 1 else ""
            junit.write_text(
                "<testsuites><testsuite name='pytest'>"
                f"<testcase classname='tests.test_child' name='child'>{failure}</testcase>"
                "</testsuite></testsuites>"
            )
            return code, "child pytest output\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == code
    assert result.cases is not None and len(result.cases) == 2
    assert all(case.passed == (code != 1) for case in result.cases)
    assert not lane_processes
    assert "child pytest output" in result.output and "ran here" not in result.output


@pytest.mark.parametrize("code", [0, 1, 5])
def test_local_fallback_uses_only_its_own_result_and_report(tmp_path, monkeypatch, lane_processes, code):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            _junit_for(args[args.index("--junit") + 1], "stale child")
            return 70, "launcher infrastructure failed\n"
        if "pytest" in args:
            return code, "local pytest output\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == code
    assert result.cases is None
    assert "local pytest output" in result.output and "launcher infrastructure failed" in result.output


def test_child_lane_with_no_selected_tests_preserves_empty_junit(tmp_path, monkeypatch, lane_processes):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            Path(args[args.index("--junit") + 1]).write_text(
                "<testsuites><testsuite name='pytest' tests='0'/></testsuites>"
            )
            Path(args[args.index("--result") + 1]).write_text(json.dumps({"completed": True, "code": 5}))
            return 5, "no tests selected\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 5 and result.cases == []
    assert not lane_processes
    assert "ran here" not in result.output


@pytest.mark.parametrize("code", range(6))
@pytest.mark.parametrize("launcher_code", [0, 1, 124, -9])
@pytest.mark.parametrize("report", [None, "<broken"])
def test_completed_child_without_junit_keeps_pytest_code(
    tmp_path, monkeypatch, lane_processes, code, launcher_code, report
):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            Path(args[args.index("--result") + 1]).write_text(json.dumps({"completed": True, "code": code}))
            if report is not None:
                Path(args[args.index("--junit") + 1]).write_text(report)
            return launcher_code, "pytest completed; report unavailable: No module named pytest in output\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == code and result.cases is None
    assert not lane_processes
    assert "ran here" not in result.output


@pytest.mark.parametrize(
    "record",
    [
        "",
        "{",
        "[]",
        "null",
        "{}",
        '{"completed": false, "code": 1}',
        '{"completed": true}',
        '{"completed": true, "code": true}',
        '{"completed": true, "code": "1"}',
        '{"completed": true, "code": 70}',
        '{"completed": 1, "code": 1}',
        '{"completed": true, "code": -1}',
    ],
)
def test_invalid_child_completion_falls_back_even_with_junit(tmp_path, monkeypatch, lane_processes, record):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            Path(args[args.index("--result") + 1]).write_text(record)
            _junit_for(args[args.index("--junit") + 1], "untrusted")
            return 1, "launcher stopped\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 0
    assert result.cases is not None and {c.name for c in result.cases} == {"fast", "slow"}
    assert len(lane_processes) == 2
    assert "invalid pytest completion" in result.output


@pytest.mark.parametrize(
    ("fast", "slow", "expected"), [(0, 5, 0), (5, 0, 0), (1, 5, 1), (5, 4, 4), (5, 5, 5)]
)
def test_completed_child_code5_aggregation(tmp_path, monkeypatch, lane_processes, fast, slow, expected):
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            code = slow if args[-1] == "slow" else fast
            Path(args[args.index("--result") + 1]).write_text(json.dumps({"completed": True, "code": code}))
            return code, "pytest finished\n"
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == expected and result.cases is None
    assert not lane_processes


def test_real_pytest_failure_without_junit_is_not_rerun(tmp_path, monkeypatch, lane_processes):
    from flyte_agent_factory import _slow_lane

    test_file = tmp_path / "test_fails.py"
    test_file.write_text("def test_failure():\n    assert False\n")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", str(test_file)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1 and "1 failed" in proc.stdout
    monkeypatch.setenv(_slow_lane.ENV_VAR, "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    fake_run = _gate_base._run

    def run(args, cwd, **kw):
        if "flyte_agent_factory._slow_lane" in args:
            code = _slow_lane.main(
                args[args.index("flyte_agent_factory._slow_lane") + 1 :],
                submit=lambda args, spot: (proc.returncode, proc.stdout, ""),
            )
            return code, proc.stdout
        return fake_run(args, cwd, **kw)

    monkeypatch.setattr(_gate_base, "_run", run)
    result = _gate_tests._run_head_pytest(_child_lane_project(tmp_path), child_lanes=True)

    assert result is not None and result.code == 1 and result.cases is None
    assert "1 failed" in result.output and "ran here" not in result.output
    assert not lane_processes
