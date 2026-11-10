"""A project on a repo that is not the profile's sandbox works at the repo root.

Its harness files live under `.claude-flyte/<name>/`, never in a `<name>/` directory at the root.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _gate_tests,
    _gates,
    _git,
    _instructions,
    _session_config,
    _supervise_support,
)

SANDBOX = "git@example.org:team/work-sandbox.git"
FACTORY = "git@example.org:team/factory.git"


# --- the layout decision ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        SANDBOX,
        "",  # no repo given: the default, the sandbox
        "git@example.org:team/work-sandbox",  # `.git` is optional
        "git@example.org:team/work-sandbox.git/",
        "ssh://git@example.org/team/work-sandbox.git",
        "https://example.org/team/work-sandbox.git",
        "https://EXAMPLE.org/team/work-sandbox",
    ],
)
def test_same_repository_in_any_spelling_is_the_sandbox(repo):
    assert _git.project_layout(repo, SANDBOX) == "sandbox"


@pytest.mark.parametrize(
    "repo", [FACTORY, "git@example.org:team/other.git", "git@example.org:other/work-sandbox.git"]
)
def test_any_other_repository_is_worked_at_its_root(repo):
    assert _git.project_layout(repo, SANDBOX) == "root"


def test_without_a_profile_every_repository_is_the_sandbox():
    assert _git.project_layout(FACTORY, "") == "sandbox"


def test_the_agent_decides_from_its_repo_and_profile(monkeypatch):
    monkeypatch.setattr(_session_config, "_project_name", "proj")  # an unnamed project is always the sandbox
    monkeypatch.setattr(_session_config, "_project_repo", FACTORY)
    assert _session_config._layout() == "root"
    assert _session_config._PROFILE is not None
    monkeypatch.setattr(_session_config, "_project_repo", _session_config._PROFILE.repo)
    assert _session_config._layout() == "sandbox"


# --- paths -----------------------------------------------------------------------------------


def test_checks_path_per_layout():
    assert _session_config._checks_rel("proj") == "proj/checks.sh"
    assert _session_config._checks_rel("proj", "sandbox") == "proj/checks.sh"
    assert _session_config._checks_rel("proj", "root") == ".claude-flyte/proj/checks.sh"
    assert _gate_base.checks_rel("proj", "root") == ".claude-flyte/proj/checks.sh"
    assert _gate_base.checks_rel("proj", "sandbox") == "proj/checks.sh"


@pytest.mark.parametrize("name", ["", ".", "../up", "/abs"])
def test_unsafe_names_keep_the_root_script_in_both_layouts(name):
    assert _session_config._checks_rel(name, "root") == _session_config._CHECKS_SCRIPT


# --- guidance --------------------------------------------------------------------------------


def test_root_guidance_names_the_harness_path_and_forced_add():
    memory = _instructions._project_memory(False, name="factory-improvement", layout="root")
    assert "`.claude-flyte/factory-improvement/checks.sh`" in memory
    assert "git add -f" in memory
    assert "`factory-improvement/checks.sh`" not in memory
    assert "repository root" in memory and "do not create `factory-improvement/`" in memory


def test_sandbox_guidance_is_unchanged():
    memory = _instructions._project_memory(False, name="proj")
    assert "`proj/checks.sh`" in memory
    assert "git add -f" not in memory
    assert memory == _instructions._project_memory(False, name="proj", layout="sandbox")


# --- preflight -------------------------------------------------------------------------------


def _git_run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def root_repo(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    _git_run(work, "init", "-b", "main")
    _git_run(work, "config", "user.email", "t@example.com")
    _git_run(work, "config", "user.name", "t")
    (work / "src").mkdir()
    (work / "src/a.py").write_text("x = 1\n")
    _git_run(work, "add", "src")
    _git_run(work, "commit", "-m", "init")
    # What the harness does to every project clone.
    (work / ".git/info/exclude").write_text("/.claude-flyte/\n")
    monkeypatch.setattr(_session_config, "_project_name", "proj")
    monkeypatch.setattr(_session_config, "_project_repo", FACTORY)
    criteria = work / ".claude-flyte/proj/ACCEPTANCE.md"
    criteria.parent.mkdir(parents=True)
    criteria.write_text("- [ ] it works\n")
    return work


@pytest.mark.asyncio
async def test_preflight_reports_an_untracked_root_layout_checks_script(root_repo):
    script = root_repo / ".claude-flyte/proj/checks.sh"
    script.write_text("exit 0\n")
    problem = await _supervise_support._done_preflight(root_repo)
    assert ".claude-flyte/proj/checks.sh" in problem and "git add -f" in problem

    _git_run(root_repo, "add", "-f", ".claude-flyte/proj/checks.sh")
    assert "uncommitted" in await _supervise_support._done_preflight(root_repo)  # staged, not yet committed
    _git_run(root_repo, "commit", "-m", "checks")
    assert await _supervise_support._done_preflight(root_repo) == ""


@pytest.mark.asyncio
async def test_preflight_still_ignores_the_rest_of_the_harness_directory(root_repo):
    (root_repo / ".claude-flyte/proj/notes.md").write_text("scratch\n")
    assert await _supervise_support._done_preflight(root_repo) == ""


def test_gates_uncommitted_sees_the_root_layout_script_only(root_repo):
    (root_repo / ".claude-flyte/proj/notes.md").write_text("scratch\n")
    assert _gates._uncommitted(root_repo, "proj", "root") == []
    (root_repo / ".claude-flyte/proj/checks.sh").write_text("exit 0\n")
    assert _gates._uncommitted(root_repo, "proj", "root") == [".claude-flyte/proj/checks.sh"]
    assert _gates._uncommitted(root_repo, "proj") == []  # sandbox: unchanged


@pytest.mark.asyncio
async def test_declared_checks_run_the_root_layout_script(root_repo, monkeypatch):
    script = root_repo / ".claude-flyte/proj/checks.sh"
    script.write_text("echo ran-from-$(basename $PWD)\nexit 3\n")

    class Timeline:
        def phase(self, *_):
            pass

        def activity(self, *_):
            pass

        def row(self, *_):
            pass

    # a fake that only carries the three rows the checks write
    problem = await _supervise_support._run_declared_checks(root_repo, cast(Any, Timeline()))
    assert problem
    assert (root_repo / ".claude-flyte/proj/checks.log").read_text().strip() == "ran-from-work"


# --- the fail-to-pass gate on a repo laid out at its root ------------------------------------


@pytest.fixture
def factory_like(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/lib.py").write_text("def answer():\n    return 1\n")
    (tmp_path / "tests/test_lib.py").write_text(
        "from lib import answer\n\ndef test_answer():\n    assert answer() == 1\n"
    )
    git("add", ".")
    git("commit", "-m", "base")
    _gates.capture_base(tmp_path, "proj")
    git("config", "user.email", "proj@agent-factory.localhost")
    return tmp_path, (tmp_path / ".claude-flyte/proj/base-commit").read_text(), git


def test_fail_to_pass_root_layout_passes_when_base_fails_and_head_passes(factory_like):
    workspace, base, _ = factory_like
    (workspace / "src/lib.py").write_text("def answer():\n    return 2\n")
    (workspace / "tests/test_lib.py").write_text(
        "from lib import answer\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    code, result = _gate_tests.fail_to_pass(
        workspace, "proj", base, ["src/lib.py", "tests/test_lib.py"], "", "", "DONE", layout="root"
    )
    assert code == 0 and "head tests pass; base tests fail" in result


def test_fail_to_pass_root_layout_rejects_tests_that_pass_on_base(factory_like):
    workspace, base, _ = factory_like
    code, result = _gate_tests.fail_to_pass(
        workspace, "proj", base, ["tests/test_lib.py"], "", "", "DONE", layout="root"
    )
    assert code == 1 and "observed exit 0" in result


def test_fail_to_pass_root_layout_runs_non_python_tests_through_the_harness_checks(factory_like):
    workspace, base, _ = factory_like
    (workspace / "tests/check.sh").write_text('test "$(cat src/value.txt 2>/dev/null)" = 2\n')
    (workspace / "src/value.txt").write_text("2\n")
    script = workspace / ".claude-flyte/proj/checks.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("bash tests/check.sh\n")
    code, result = _gate_tests.fail_to_pass(
        workspace, "proj", base, ["tests/check.sh", "src/value.txt"], "", "", "DONE", layout="root"
    )
    assert code == 0, result
    script.unlink()
    code, result = _gate_tests.fail_to_pass(
        workspace, "proj", base, ["tests/check.sh", "src/value.txt"], "", "", "DONE", layout="root"
    )
    assert code == 127 and ".claude-flyte/proj/checks.sh" in result


def test_project_files_are_the_repo_files_at_the_root(factory_like):
    workspace, _, _ = factory_like
    assert _gate_base.project_dir(workspace, "proj", "root") == workspace
    assert _gate_base.project_dir(workspace, "proj", "sandbox") == workspace / "proj"
    assert "src/lib.py" in _gate_base._project_files(_gate_base.project_dir(workspace, "proj", "root"))


# --- scope in the root layout ----------------------------------------------------------------


def _root_scope(paths, name, brief):
    return _gate_scope.scope_violations(paths, name, brief, None, "root")


def test_root_layout_project_name_grants_no_scope_even_when_it_is_a_real_directory():
    brief = "May edit: src/pkg"
    assert _root_scope(["src/other/b.py"], "src", brief) == ["src/other/b.py"]
    assert _root_scope(["src/pkg/x.py"], "src", brief) == []
    assert _root_scope(["tests/test_a.py"], "tests", brief) == ["tests/test_a.py"]


@pytest.mark.parametrize("path", ["newroot.py", "newdir/x.py", "fi/stuff.py", "src/other.py"])
def test_root_layout_blocks_everything_outside_the_may_edit_paths(path):
    assert _root_scope([path], "fi", "May edit: src/pkg") == [path]
    assert _gate_scope.scope_flagged([path], "fi", "May edit: src/pkg", None, "root") == []


def test_root_layout_allows_harness_state_and_its_own_checks_script_path():
    assert _root_scope([".claude-flyte/fi/checks.sh", ".claude/settings.json"], "fi", "May edit: src") == []


def test_root_brief_without_may_edit_blocks_every_edit():
    assert _root_scope(["a.py", "src/a.py"], "fi", "do things") == ["a.py", "src/a.py"]


def test_sandbox_scope_is_unchanged_by_the_layout_argument():
    assert _gate_scope.scope_violations(["app/main.py", "other/b.py"], "app", "", None, "sandbox") == [
        "other/b.py"
    ]
    assert _gate_scope.scope_flagged(["newroot.py"], "app", "") == ["newroot.py"]


def test_root_guidance_says_scope_is_exactly_the_may_edit_paths():
    memory = _instructions._project_memory(False, name="fi", layout="root")
    assert "exactly" in memory and "`May edit:`" in memory


def test_root_scope_message_names_the_brief_paths(factory_like):
    workspace, base, git = factory_like
    (workspace / "src/lib.py").write_text("def answer():\n    return 2\n")
    git("add", ".")
    git("-c", "user.email=proj@agent-factory.localhost", "commit", "-m", "work")
    rows = {}
    _gates._judge_project(
        workspace,
        "proj",
        base,
        "May edit: tests",
        "",
        "DONE",
        [],
        lambda r, c, o: rows.update({r: (c, o)}),
        "root",
    )
    status, output = rows["scope"]
    assert status is _gate_base.Status.FAIL and "outside the brief's May edit: paths: src/lib.py" in output


# --- repository identity ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "  git@example.org:team/work-sandbox.git  ",
        "git@EXAMPLE.org:Team/Work-Sandbox.git",
        "https://example.org/team/work-sandbox.git?ref=x",
        "https://example.org/team/work-sandbox#frag",
        "https://example.org//team//work-sandbox.git",
        "https://user:p@ss@example.org/team/work-sandbox.git",
        "ssh://user:p@ss@example.org:2222/team/work-sandbox",
    ],
)
def test_identity_normalises_case_query_fragment_slashes_and_credentials(repo):
    assert _git.project_layout(repo, SANDBOX) == "sandbox"


def test_whitespace_only_repo_is_no_repo():
    assert _git.project_layout("   ", FACTORY) == "sandbox"


@pytest.mark.parametrize(
    "spelling", ["file:///srv/git/p", "/srv/git/p", "/srv/git/p.git", "file:///SRV//git/p/"]
)
def test_local_paths_and_file_urls_are_one_repository(spelling):
    assert _git.project_layout(spelling, "/srv/git/p") == "sandbox"
    assert _git.project_layout("/srv/git/other", "file:///srv/git/p") == "root"


# --- layout with an unsafe project name ------------------------------------------------------


@pytest.mark.parametrize("name", ["", ".", "../up", "/abs"])
def test_unsafe_project_name_is_sandbox_layout_whatever_the_repo(monkeypatch, name):
    monkeypatch.setattr(_session_config, "_project_name", name)
    monkeypatch.setattr(_session_config, "_project_repo", FACTORY)
    assert _session_config._layout() == "sandbox"
    assert _session_config._checks_rel(name, _session_config._layout()) == _session_config._CHECKS_SCRIPT


def test_unsafe_name_does_not_borrow_the_repos_root_acceptance_file(tmp_path, monkeypatch):
    (tmp_path / "ACCEPTANCE.md").write_text("- [ ] the repo's own\n")
    monkeypatch.setattr(_session_config, "_project_name", "")
    monkeypatch.setattr(_session_config, "_project_repo", FACTORY)
    # Sandbox behaviour for an unsafe name is what it always was: the legacy root file migrates.
    assert _session_config._layout() == "sandbox"
    assert _session_config._acceptance_path(tmp_path).read_text() == "- [ ] the repo's own\n"


# --- sandbox text is unchanged ---------------------------------------------------------------

SNAPSHOTS = Path(__file__).parent / "snapshots"


@pytest.mark.parametrize(
    ("snapshot", "kwargs"),
    [
        ("plain", {"browser_enabled": False, "name": "proj"}),
        ("browser", {"browser_enabled": True, "name": "other-proj"}),
        ("delegate", {"browser_enabled": False, "name": "proj", "delegation_available": True}),
    ],
)
def test_sandbox_memory_matches_the_text_before_the_root_layout_change(snapshot, kwargs, monkeypatch):
    # The API-key section appears only in a pod that mounts the key; the snapshots are rendered without it.
    monkeypatch.delenv("PROJECT_ANTHROPIC_API_KEY", raising=False)
    # The snapshots predate the context guard, so they are the guard-off text (#92 turned it on by default).
    monkeypatch.setenv("AGENT_FACTORY_CONTEXT_GUARD", "0")
    # Snapshots were rendered by `_project_memory` as of the commit before the root layout existed.
    expected = (SNAPSHOTS / f"sandbox_memory_{snapshot}.txt").read_text()
    assert _instructions._project_memory(**kwargs) == expected
    assert _instructions._project_memory(**kwargs, layout="sandbox") == expected


def test_root_guidance_does_not_tell_the_project_to_keep_its_own_directory():
    memory = _instructions._project_memory(False, name="fi", layout="root")
    assert "keep your own directory" not in memory
    assert "keep upstream" in memory and "outside your `May edit:` paths" in memory


# --- gates.run: shellcheck and messages ------------------------------------------------------


def _shellcheck_args(tmp_path, monkeypatch, layout):
    seen: list[list[str]] = []

    def fake_run(command: list[str], cwd: Path, **kw: object) -> tuple[int, str]:
        seen.append(command)
        return 0, ""

    monkeypatch.setattr(shutil, "which", lambda c: "/bin/" + c)
    monkeypatch.setattr(_gate_base, "_run", fake_run)

    class Timeline:
        def row(self, *_):
            pass

    rule = "## T.1 MUST\nWhy: w\nCheck: c\nEnforced: tool shellcheck\n"
    _gates.run(tmp_path, "proj", "", "", "", {"languages/shell.md": rule}, Timeline(), layout)
    return next(c[1:] for c in seen if Path(c[0]).name == "shellcheck")


@pytest.mark.real_factory_gates
def test_shellcheck_lints_the_root_layout_checks_script_but_not_other_harness_files(tmp_path, monkeypatch):
    for rel in (".claude-flyte/proj/checks.sh", ".claude-flyte/other/checks.sh", "run.sh"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("echo hi\n")
    args = _shellcheck_args(tmp_path, monkeypatch, "root")
    assert sorted(args) == [".claude-flyte/proj/checks.sh", "run.sh"]


@pytest.mark.real_factory_gates
def test_shellcheck_in_the_sandbox_lints_the_project_directory_only(tmp_path, monkeypatch):
    for rel in ("proj/checks.sh", "proj/.claude-flyte/x.sh", "run.sh"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("echo hi\n")
    assert _shellcheck_args(tmp_path, monkeypatch, "sandbox") == ["checks.sh"]


def test_missing_checks_script_message_per_layout(factory_like):
    workspace, base, _ = factory_like
    (workspace / "tests/check.sh").write_text("true\n")
    for layout, message in (("sandbox", "no project checks.sh to run non-Python tests"), ("root", None)):
        code, result = _gate_tests.fail_to_pass(
            workspace, "proj", base, ["tests/check.sh"], "", "", "DONE", layout=layout
        )
        assert code == 127
        assert result == (message or "no project .claude-flyte/proj/checks.sh to run non-Python tests")


def test_an_uppercase_git_suffix_is_the_same_repository():
    from flyte_agent_factory import _git

    assert _git.project_layout("https://github.com/o/r.GIT", "git@github.com:o/r.git") == "sandbox"
