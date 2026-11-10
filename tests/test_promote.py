"""Promotion applies a project's patches onto the target's current default branch, checks, then pushes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gates, _promote

MAKEFILE = "lint:\n\t@python check.py\n"
CHECK_OK = "import sys\nsys.exit(0)\n"
CHECK_RED = "import sys\nsys.exit('lint is red')\n"


@pytest.fixture(autouse=True)
def _project_python_first(monkeypatch):
    """The target's commands run `python ...`: it must be this interpreter, as under `uv run`."""
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")


def git(path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    return subprocess.run(cmd, cwd=path, env=env, capture_output=True, text=True, check=True).stdout.strip()


def commit(path: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)


@pytest.fixture
def world(tmp_path):
    """A bare `remote` on main, a workspace whose project `demo` holds a patch against it."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    commit(seed, {"Makefile": MAKEFILE, "check.py": CHECK_OK, "notes.txt": "one\ntwo\nthree\n"}, "base")
    git(seed, "push", "-q", "origin", "main")

    # The patch is made on a branch of the seed, as the factory would in the sandbox.
    git(seed, "checkout", "-q", "-b", "work")
    commit(seed, {"notes.txt": "one\ntwo changed\nthree\n"}, "Change line two")
    workspace = tmp_path / "workspace"
    patches = workspace / "projects/demo/patches"
    patches.mkdir(parents=True)
    git(seed, "format-patch", "-q", "-o", str(patches), "main")
    git(seed, "checkout", "-q", "main")
    return remote, seed, workspace


def promote(world, **kwargs):
    remote, _, workspace = world
    return _promote.promote(workspace, "demo", str(remote), "promote/demo", **kwargs)


def test_happy_path_pushes_the_branch(world):
    remote, _, _ = world
    report = promote(world)
    assert report.ok and report.stage == "done", report
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"
    assert git(remote, "log", "--format=%s", "main..promote/demo") == "Change line two"


def test_dry_run_does_everything_but_push(world):
    remote, _, _ = world
    report = promote(world, dry_run=True)
    assert report.ok and report.stage == "dry-run"
    assert "promote/demo" not in git(remote, "branch", "--list")


def test_conflict_stops_before_push(world):
    remote, seed, _ = world
    commit(seed, {"notes.txt": "one\ntwo other\nthree\n"}, "Conflicting change on main")
    git(seed, "push", "-q", "origin", "main")
    report = promote(world)
    assert not report.ok and report.stage == "apply"
    assert "promote/demo" not in git(remote, "branch", "--list")


def test_red_lint_stops_before_push(world):
    remote, seed, _ = world
    commit(seed, {"check.py": CHECK_RED}, "Break the target's lint")
    git(seed, "push", "-q", "origin", "main")
    report = promote(world)
    assert not report.ok and report.stage == "ci"
    assert "lint is red" in report.message
    assert "promote/demo" not in git(remote, "branch", "--list")


def test_a_ci_that_could_not_run_stops_before_push(world, monkeypatch):
    """A toolchain that cannot be installed shows nothing passes: nothing is pushed on it."""
    remote, _, _ = world
    not_run = _gates.Result("factory:target-ci", _gate_base.Status.NOT_RUN, "uv 0.13.4 cannot be installed")
    monkeypatch.setattr(_gates, "target_ci_result", lambda checkout, against_base: not_run)
    report = promote(world)
    assert not report.ok and report.stage == "ci", report
    assert "uv 0.13.4 cannot be installed" in report.message
    assert "promote/demo" not in git(remote, "branch", "--list")


def test_what_is_pushed_is_rebased_onto_the_current_default_branch(world):
    remote, seed, _ = world
    commit(seed, {"other.txt": "moved on\n"}, "Default branch moved after the patches were made")
    git(seed, "push", "-q", "origin", "main")
    assert promote(world).ok
    assert git(remote, "merge-base", "main", "promote/demo") == git(remote, "rev-parse", "main")
    assert git(remote, "show", "promote/demo:other.txt") == "moved on"
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"


def test_a_second_promotion_updates_the_branch(world):
    remote, seed, _ = world
    assert promote(world).ok
    commit(seed, {"other.txt": "moved on\n"}, "Default branch moved")
    git(seed, "push", "-q", "origin", "main")
    assert promote(world).ok
    assert git(remote, "merge-base", "main", "promote/demo") == git(remote, "rev-parse", "main")


def test_no_patches_is_reported(tmp_path):
    report = _promote.promote(tmp_path, "demo", str(tmp_path / "x.git"), "b")
    assert not report.ok and report.stage == "patches"


def test_an_empty_target_is_reported_not_raised(world, tmp_path):
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(empty))
    _, _, workspace = world

    report = _promote.promote(workspace, "demo", str(empty), "promote/demo")

    assert not report.ok and report.stage == "clone"
    assert "no default branch" in report.message


def _uncovered_line_world(tmp_path, patch=None):
    """A remote and a workspace whose patch is `patch`; by default it adds `never()`, which no test calls."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    coverage = "coverage:\n\tpython -m coverage run --branch -m pytest -q\n"
    base = {"Makefile": coverage, "calc.py": "def sign(x):\n    return 1\n", "test_calc.py": TEST}
    commit(seed, base, "base")
    git(seed, "push", "-q", "origin", "main")
    git(seed, "checkout", "-q", "-b", "work")
    never = {"calc.py": "def sign(x):\n    return 1\n\n\ndef never(x):\n    return 2\n"}
    commit(seed, patch or never, "Add never")
    workspace = tmp_path / "workspace"
    patches = workspace / "projects/demo/patches"
    patches.mkdir(parents=True)
    git(seed, "format-patch", "-q", "-o", str(patches), "main")
    return remote, workspace


def test_uncovered_changed_python_line_stops_before_push(tmp_path):
    remote, workspace = _uncovered_line_world(tmp_path)
    report = _promote.promote(workspace, "demo", str(remote), "promote/demo")
    assert not report.ok and report.stage == "coverage"
    assert "calc.py:6" in report.message
    assert "promote/demo" not in git(remote, "branch", "--list")

    (workspace / "projects/demo/SUMMARY.md").write_text("## Uncovered changed lines\n- calc.py:6 -- demo\n")
    assert _promote.promote(workspace, "demo", str(remote), "promote/demo").ok


def test_a_new_module_no_test_imports_stops_before_push(tmp_path):
    module = "def pick(x):\n    if x:\n        return 1\n    return 2\n"
    remote, workspace = _uncovered_line_world(tmp_path, {"b.py": module})
    report = _promote.promote(workspace, "demo", str(remote), "promote/demo")
    assert not report.ok and report.stage == "coverage"
    assert "b.py:1, b.py:2, b.py:3, b.py:4" in report.message
    assert "promote/demo" not in git(remote, "branch", "--list")


def test_uncovered_line_is_found_when_git_diffs_without_prefixes(tmp_path, monkeypatch):
    config = tmp_path / "gitconfig"
    config.write_text("[diff]\n\tnoprefix = true\n")
    remote, workspace = _uncovered_line_world(tmp_path)  # before the config: noprefix patches do not `am`
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    report = _promote.promote(workspace, "demo", str(remote), "promote/demo")
    assert not report.ok and report.stage == "coverage"
    assert "calc.py:6" in report.message


def test_promote_defines_no_copy_of_the_gates_functions():
    assert not {"_changed_lines", "_uncovered", "_check_ci"} & set(vars(_promote))


def test_a_failing_gh_call_is_reported_with_its_stderr(world, tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text("#!/bin/sh\necho 'gh: token expired' >&2\nexit 1\n")
    (fake / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    report = promote(world)
    assert report.ok and report.stage == "done"
    assert "gh pr create failed" in report.message and "token expired" in report.message


TEST = "from calc import sign\n\n\ndef test_sign():\n    assert sign(1) == 1\n"


def test_a_sandbox_layout_project_is_promoted_from_its_own_directory(world):
    """Outside a workspace a project works in `<name>/`, and promote finds its patches there."""
    remote, _, workspace = world
    (workspace / "projects" / "demo").rename(workspace / "demo")
    (workspace / "projects").rmdir()
    report = promote(world)
    assert report.ok and report.stage == "done", report
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"
