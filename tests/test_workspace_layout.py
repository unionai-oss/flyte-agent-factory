"""A repo with a `projects/` directory is a workspace: a project works in `projects/<name>/` and nowhere else.

The third layout, beside `sandbox` (the profile's shared repo) and `root` (any other repo).
"""

import importlib.abc
import json
import os
import subprocess

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _gates,
    _git,
    _inbox,
    _instructions,
    _session_config,
    _timeline,
)

pytestmark = pytest.mark.real_factory_gates

SANDBOX = "git@example.org:team/work-sandbox.git"
OTHER = "git@example.org:team/workspace.git"


def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def make_origin(tmp_path, files):
    """A bare-bones origin on `main` holding `files`, cloned the way the harness clones it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    for rel in files:
        (origin / rel).parent.mkdir(parents=True, exist_ok=True)
        (origin / rel).write_text("x\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-m", "init")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    git(clone, "config", "user.email", "t@example.com")
    git(clone, "config", "user.name", "t")
    return clone


# --- the layout decision ---------------------------------------------------------------------


def test_a_repo_with_a_projects_directory_at_its_default_branch_is_a_workspace(tmp_path):
    clone = make_origin(tmp_path, ["projects/.gitkeep", "README.md"])
    assert _git.project_layout(OTHER, SANDBOX, _gate_base.is_workspace(clone)) == "workspace"


def test_the_workspace_marker_beats_the_sandbox_name(tmp_path):
    clone = make_origin(tmp_path, ["projects/.gitkeep"])
    assert _git.project_layout(SANDBOX, SANDBOX, _gate_base.is_workspace(clone)) == "workspace"


def test_the_sandbox_has_top_level_project_dirs_and_no_projects_directory(tmp_path):
    clone = make_origin(tmp_path, ["app/main.py", "tool/checks.sh", "standards/x.md", "README.md"])
    assert _git.project_layout(SANDBOX, SANDBOX, _gate_base.is_workspace(clone)) == "sandbox"


def test_the_engine_repo_without_projects_stays_root(tmp_path):
    clone = make_origin(tmp_path, ["src/a.py", "tests/test_a.py"])
    assert _git.project_layout(OTHER, SANDBOX, _gate_base.is_workspace(clone)) == "root"


def test_a_projects_directory_made_on_a_branch_does_not_make_a_workspace(tmp_path):
    clone = make_origin(tmp_path, ["src/a.py"])
    git(clone, "checkout", "-b", "factory/x")
    (clone / "projects").mkdir()
    (clone / "projects/a.py").write_text("x\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "adds projects/")
    assert _git.project_layout(OTHER, SANDBOX, _gate_base.is_workspace(clone)) == "root"


def test_a_file_named_projects_is_not_a_workspace(tmp_path):
    clone = make_origin(tmp_path, ["projects"])
    assert _git.project_layout(OTHER, SANDBOX, _gate_base.is_workspace(clone)) == "root"


def test_without_a_checkout_the_layout_is_decided_by_name_alone(tmp_path):
    missing = _gate_base.is_workspace(tmp_path / "missing")
    assert missing is False
    assert _git.project_layout(SANDBOX, SANDBOX, missing) == "sandbox"
    assert _git.project_layout(OTHER, SANDBOX, missing) == "root"
    assert _git.project_layout(OTHER, SANDBOX) == "root"


def test_the_agent_asks_the_checkout(tmp_path, monkeypatch):
    clone = make_origin(tmp_path, ["projects/.gitkeep"])
    monkeypatch.setattr(_session_config, "WORKSPACE", clone)
    monkeypatch.setattr(_session_config, "_project_name", "proj")
    monkeypatch.setattr(_session_config, "_project_repo", OTHER)
    assert _session_config._layout() == "workspace"
    assert _session_config._project_dir() == clone / "projects/proj"


# --- paths -----------------------------------------------------------------------------------


def test_workspace_paths(tmp_path):
    assert _gate_base.project_dir(tmp_path, "proj", "workspace") == tmp_path / "projects/proj"
    assert _gate_base.checks_rel("proj", "workspace") == "projects/proj/checks.sh"
    assert _session_config._checks_rel("proj", "workspace") == "projects/proj/checks.sh"
    assert _gate_base.project_rel("proj", "workspace") == "projects/proj"
    assert _gate_base.project_rel("proj", "sandbox") == "proj"
    assert _gate_base.project_rel("proj", "root") == "."


def test_the_acceptance_file_stays_in_the_harness_directory():
    assert _session_config._acceptance_rel("proj") == ".claude-flyte/proj/ACCEPTANCE.md"


# --- scope -----------------------------------------------------------------------------------


def scope(paths, brief="", name="proj"):
    return _gate_scope.scope_violations(paths, name, brief, None, "workspace")


def test_the_project_directory_is_in_scope_by_default():
    assert scope(["projects/proj/a.py", "projects/proj/sub/b.py", "projects/proj/checks.sh"]) == []


def test_the_gitkeep_and_other_projects_are_out_of_scope():
    paths = ["projects/.gitkeep", "projects/other/a.py", "projects/proj2/a.py", "projects/proj"]
    assert scope(paths[:3]) == paths[:3]
    # A file named like the project is not its directory.
    assert scope(["projects/proj"]) == ["projects/proj"]


@pytest.mark.parametrize("path", ["proj/a.py", "newroot.py", "src/a.py", "README.md", "standards/x.md"])
def test_everything_outside_projects_is_blocked_not_flagged(path):
    assert scope([path]) == [path]
    assert _gate_scope.scope_flagged([path], "proj", "", None, "workspace") == []


def test_harness_state_is_in_scope_and_may_edit_still_widens_it():
    assert scope([".claude-flyte/proj/checks.sh", ".claude/settings.json"]) == []
    assert scope(["docs/a.md", "projects/other/a.py"], "May edit: docs/**") == ["projects/other/a.py"]


def test_the_owners_files_block_even_inside_projects():
    assert scope(["waivers.toml"], "May edit: waivers.toml") == ["waivers.toml"]


def test_sandbox_and_root_scope_are_unchanged():
    assert _gate_scope.scope_violations(["app/x.py", "projects/x.py"], "app", "", None, "sandbox") == [
        "projects/x.py"
    ]
    assert _gate_scope.scope_flagged(["newroot.py"], "app", "") == ["newroot.py"]
    assert _gate_scope.scope_violations(["app/x.py"], "app", "", None, "root") == ["app/x.py"]


# --- instructions ----------------------------------------------------------------------------


def test_workspace_guidance_names_the_project_directory():
    memory = _instructions._project_memory(False, name="proj", layout="workspace")
    assert "`projects/proj/checks.sh`" in memory
    assert "`projects/proj/`" in memory
    assert "git add -f" not in memory
    assert "repository root" not in memory.split("This project works in")[1].split("\n")[0]


def test_workspace_guidance_differs_from_the_other_layouts():
    memories = {
        layout: _instructions._project_memory(False, name="proj", layout=layout)
        for layout in ("sandbox", "root", "workspace")
    }
    assert len(set(memories.values())) == 3
    assert "projects/proj" not in memories["sandbox"]


# --- the gate --------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path):
    clone = make_origin(tmp_path, ["projects/.gitkeep", "projects/other/a.py", "README.md"])
    (clone / ".git/info/exclude").write_text("/.claude-flyte/\n")
    _gates.capture_base(clone, "proj")
    return clone


def commit(path, rel, message="work"):
    full = path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("y\n")
    git(path, "add", "-f", rel)
    git(path, "-c", f"user.email={_gates.commit_identity('proj')}", "commit", "-m", message)


def test_uncommitted_sees_this_project_only(workspace):
    (workspace / "projects/proj").mkdir()
    (workspace / "projects/proj/new.py").write_text("x\n")
    (workspace / "projects/stray.py").write_text("x\n")
    (workspace / "projects/other/b.py").write_text("x\n")
    assert _gates._uncommitted(workspace, "proj", "workspace") == ["projects/proj/new.py"]


def test_a_checks_script_in_the_project_directory_is_not_ignored_state(workspace):
    (workspace / "projects/proj").mkdir()
    (workspace / "projects/proj/checks.sh").write_text("true\n")
    assert _gates._uncommitted(workspace, "proj", "workspace") == ["projects/proj/checks.sh"]
    assert _gates.untracked_checks(workspace, "proj", "workspace") is False


def judge(workspace, brief=""):
    base = _gates.resolve_base(workspace, "proj")
    rows = {}
    _gates._judge_project(
        workspace, "proj", base, brief, "", "DONE", [], lambda r, c, o: rows.update({r: (c, o)}), "workspace"
    )
    return rows


def test_scope_passes_for_work_inside_the_project_directory(workspace):
    commit(workspace, "projects/proj/lib.py")
    rows = judge(workspace)
    assert rows["attribution"][0] is _gate_base.Status.PASS
    assert rows["scope"][0] is _gate_base.Status.PASS


def test_scope_fails_for_the_gitkeep_and_other_projects(workspace):
    commit(workspace, "projects/proj/lib.py")
    commit(workspace, "projects/.gitkeep")
    commit(workspace, "projects/other/a.py")
    status, output = judge(workspace)["scope"]
    assert status is _gate_base.Status.FAIL
    assert "projects/.gitkeep" in output and "projects/other/a.py" in output
    assert "projects/proj/lib.py" not in output
    assert "May edit" in output


def test_attribution_looks_at_the_project_directory_when_nothing_is_committed(workspace):
    rows = judge(workspace)
    assert rows["attribution"][0] is _gate_base.Status.NOT_APPLICABLE


# --- the prompt does not mention the Claude app ------------------------------------------------


def test_the_direction_prompt_says_how_direction_arrives():
    loader = _inbox.__loader__
    assert isinstance(loader, importlib.abc.InspectLoader)
    source = loader.get_source(_inbox.__name__)
    assert source is not None
    prompt = source.split("### Send direction to this project")[1].split("```")[0]
    assert "Claude app" not in prompt
    assert "flyte signal" in source


# --- patches for promote ---------------------------------------------------------------------

BRIEF = "Add clamp to calc.\nDelivers to: acme/widgets at widgets\n"
CALC = "def sign(x):\n    return -1 if x < 0 else 1\n"


@pytest.fixture(params=["workspace", "sandbox"])
def delivery(request, tmp_path):
    """A workspace (or sandbox) project whose target checkout has one commit on `feature`."""
    layout = request.param
    rel = "projects/proj" if layout == "workspace" else "proj"
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "NOTES.md").write_text("n\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    checkout = tmp_path / rel / "widgets"
    checkout.mkdir()
    (checkout / "calc.py").write_text(CALC)
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.email", "t@example.com")
    git(checkout, "config", "user.name", "t")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "upstream")
    git(checkout, "checkout", "-b", "feature")
    (checkout / "calc.py").write_text(CALC + "\n\ndef double(x):\n    return 2 * x\n")
    git(checkout, "commit", "-am", "add double")
    return tmp_path / rel, checkout


def patches_result(project):
    results = {r.rule: r for r in _gates.target_results(project, BRIEF)}
    return results["factory:target-patches"]


def format_patches(checkout, project):
    git(checkout, "format-patch", "-o", str(project / "patches"), "main..HEAD")


def test_missing_patches_directory_fails(delivery):
    project, _ = delivery
    result = patches_result(project)
    assert result.status is _gate_base.Status.FAIL
    assert "patches" in result.output


def test_empty_patches_directory_fails(delivery):
    project, _ = delivery
    (project / "patches").mkdir()
    assert patches_result(project).status is _gate_base.Status.FAIL


def test_patches_that_apply_cleanly_pass(delivery):
    project, checkout = delivery
    format_patches(checkout, project)
    result = patches_result(project)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "1 patch" in result.output


def test_the_check_leaves_the_checkout_untouched(delivery):
    project, checkout = delivery
    format_patches(checkout, project)
    before = git(checkout, "status", "--porcelain=v1", "--branch"), git(checkout, "rev-parse", "HEAD")
    patches_result(project)
    assert (
        git(checkout, "status", "--porcelain=v1", "--branch"),
        git(checkout, "rev-parse", "HEAD"),
    ) == before


def test_patches_that_do_not_apply_to_the_base_fail(delivery):
    project, checkout = delivery
    format_patches(checkout, project)
    patch = project / "patches/0001-add-double.patch"
    # A context line the base does not have: git has nothing to merge it with.
    patch.write_text(patch.read_text().replace(" def sign(x):", " def sign(y):"))
    result = patches_result(project)
    assert result.status is _gate_base.Status.FAIL
    assert "git am" in result.output


def test_a_patch_that_is_not_a_patch_fails(delivery):
    project, _ = delivery
    (project / "patches").mkdir()
    (project / "patches/0001-nothing.patch").write_text("not a patch\n")
    assert patches_result(project).status is _gate_base.Status.FAIL


def test_patches_missing_commits_since_the_base_fail(delivery):
    project, checkout = delivery
    format_patches(checkout, project)
    (checkout / "extra.py").write_text("x = 3\n")
    git(checkout, "add", "extra.py")
    git(checkout, "commit", "-m", "add extra")
    result = patches_result(project)
    assert result.status is _gate_base.Status.FAIL
    assert "do not rebuild" in result.output


def test_no_checkout_means_the_ci_check_reports_it_and_no_patch_check_runs(tmp_path):
    results = _gates.target_results(tmp_path, BRIEF)
    assert [r.rule for r in results] == ["factory:target-ci"]


def test_an_ordinary_project_has_no_patch_check(delivery):
    project, _ = delivery
    assert _gates.target_results(project, "An ordinary brief.\n") == []


def test_the_gate_records_the_patch_check_for_a_delivery_project(delivery, tmp_path):
    project, checkout = delivery
    workspace = project.parent.parent if project.parent.name == "projects" else project.parent
    format_patches(checkout, project)
    git(workspace, "config", "user.email", "t@example.com")
    _gates.capture_base(workspace, "proj")
    layout = "workspace" if project.parent.name == "projects" else "sandbox"
    _gates.run(workspace, "proj", BRIEF, "", "DONE", {}, _timeline._Timeline(), layout)
    recorded = json.loads((workspace / ".claude-flyte/proj/standards-results.json").read_text())
    assert {r["rule"]: r["status"] for r in recorded}["factory:target-patches"] == "PASS"


def test_delivery_instructions_ask_for_the_patches(monkeypatch):
    monkeypatch.setattr(_session_config, "_project_brief", BRIEF)
    for layout, where in (("workspace", "projects/proj/patches/"), ("sandbox", "proj/patches/")):
        memory = _instructions._project_memory(False, name="proj", layout=layout)
        assert "git format-patch" in memory and f"`{where}`" in memory
        assert "factory:target-patches" in memory
    monkeypatch.setattr(_session_config, "_project_brief", "An ordinary brief.\n")
    assert "git format-patch" not in _instructions._project_memory(False, name="proj", layout="workspace")
