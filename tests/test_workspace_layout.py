"""A repo with a `projects/` directory is a workspace: a project works in `projects/<name>/` and nowhere else.

The third layout, beside `sandbox` (the profile's shared repo) and `root` (any other repo).
"""

import importlib.abc
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _delivery,
    _gate_base,
    _gate_scope,
    _gates,
    _git,
    _inbox,
    _instructions,
    _session_config,
    _timeline,
)
from tests.test_delivery import seed_target

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


# --- a delivering project: the target's tree is in the workspace --------------------------------

BRIEF = "Add clamp to calc.\nDelivers to: acme/widgets\n"
CALC = "def sign(x):\n    return -1 if x < 0 else 1\n"
TEST_CALC = "from calc import sign\n\n\ndef test_sign():\n    assert sign(1) == 1\n"
COVERAGE = "coverage:\n\tpython -m coverage run --branch -m pytest -q\n"
DOUBLE = CALC + "\n\ndef double(x):\n    return 2 * x\n"
LINT = "lint:\n\t@python check.py\n"
TARGET_FILES = {
    "calc.py": CALC,
    "test_calc.py": TEST_CALC,
    "check.py": "import sys\nsys.exit(0)\n",
    "Makefile": LINT + COVERAGE,
}


@pytest.fixture
def operator(monkeypatch, tmp_path_factory):
    """`import` commits as the operator: give it an identity, and no other git configuration.

    The target's commands run `python ...`: it must be this interpreter, as under `uv run`.
    """
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text("[user]\n\tname = Op Erator\n\temail = operator@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def make_workspace(tmp_path, layout):
    """A workspace (`projects/proj/`) or the shared sandbox repo (`proj/`) with one project directory."""
    rel = "projects/proj" if layout == "workspace" else "proj"
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "NOTES.md").write_text("n\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path / rel


@pytest.fixture(params=["workspace", "sandbox"])
def imported(request, tmp_path, operator):
    """A project that imported the target's files, then committed `double()`: no nested checkout anywhere."""
    (tmp_path / "upstream").mkdir()
    target, _ = seed_target(tmp_path / "upstream", TARGET_FILES)
    project = make_workspace(tmp_path, request.param)
    _delivery.import_tree(str(target), project, path="widgets")
    (project / "widgets/calc.py").write_text(DOUBLE)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "add double")
    return project, target


def results_of(project, repo=None):
    """The gate results; the brief names the manifest's own repository unless `repo` says another."""
    delivery = _delivery.load(project)
    assert delivery is not None
    delivers = repo or delivery.repo
    brief = f"Add clamp to calc.\nDelivers to: {delivers}\n"
    return {r.rule: r for r in _gates.target_results(project, brief)}


def test_an_imported_tree_gets_the_target_gates_from_workspace_history_alone(imported):
    project, _ = imported
    assert not list(project.rglob(".git")), "no nested checkout: only the workspace's own history"
    results = results_of(project)
    assert list(results) == ["factory:target-ci", "factory:target-coverage"]
    assert results["factory:target-ci"].status is _gate_base.Status.PASS


def test_the_coverage_is_judged_against_the_import_base(imported):
    """Only `double()` is the project's change: `sign()` was imported, covered or not."""
    project, _ = imported
    result = results_of(project)["factory:target-coverage"]
    assert result.status is _gate_base.Status.FAIL
    assert "calc.py:6" in result.output and "calc.py:2" not in result.output
    (project / "SUMMARY.md").write_text("## Uncovered changed lines\n- calc.py:6 -- demo\n")
    assert results_of(project)["factory:target-coverage"].status is _gate_base.Status.PASS


def test_uncommitted_work_is_what_the_gates_judge(imported):
    project, _ = imported
    (project / "widgets/calc.py").write_text(CALC)  # `double()` taken out again, not committed
    assert results_of(project)["factory:target-coverage"].status is _gate_base.Status.PASS


def test_the_checkout_of_a_project_from_before_imports_still_passes_the_gates(tmp_path, operator):
    """A nested git checkout of the target is the legacy layout: it keeps working, without a patch gate."""
    project = make_workspace(tmp_path, "workspace")
    checkout = project / "widgets"
    checkout.mkdir()
    for name, text in TARGET_FILES.items():
        (checkout / name).write_text(text)
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.email", "t@example.com")
    git(checkout, "config", "user.name", "t")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "upstream")
    git(checkout, "checkout", "-b", "feature")
    (checkout / "calc.py").write_text(DOUBLE)
    git(checkout, "commit", "-am", "add double")
    brief = "Add clamp to calc.\nDelivers to: acme/widgets at widgets\n"

    results = {r.rule: r for r in _gates.target_results(project, brief)}

    assert list(results) == ["factory:target-ci", "factory:target-coverage"]
    assert "calc.py:6" in results["factory:target-coverage"].output


def test_no_tree_and_no_checkout_names_the_import_command(tmp_path):
    results = _gates.target_results(tmp_path, BRIEF)
    assert [r.rule for r in results] == ["factory:target-ci"]
    assert results[0].status is _gate_base.Status.FAIL
    assert "python ops/promote.py import acme/widgets" in results[0].output


def test_a_promote_toml_that_is_not_committed_fails_the_gate_with_the_remedy(tmp_path):
    project = make_workspace(tmp_path, "workspace")
    (project / "promote.toml").write_text(f'repo = "acme/widgets"\nbase = "{"a" * 40}"\n')
    result = results_of(project, "acme/widgets")["factory:target-ci"]
    assert result.status is _gate_base.Status.FAIL and "not committed" in result.output


def test_a_malformed_promote_toml_fails_the_gate(tmp_path):
    (tmp_path / "promote.toml").write_text("repo = [\n")
    results = _gates.target_results(tmp_path, BRIEF)
    assert [r.rule for r in results] == ["factory:target-ci"]
    assert results[0].status is _gate_base.Status.FAIL and "promote.toml" in results[0].output


def test_an_ordinary_project_has_no_target_gates(imported):
    project, _ = imported
    assert _gates.target_results(project, "An ordinary brief.\n") == []


def test_patches_are_retired():
    assert not hasattr(_gates, "_target_patches_result")


def test_the_gate_records_the_target_checks_for_a_delivery_project(imported, tmp_path):
    project, _ = imported
    workspace = project.parent.parent if project.parent.name == "projects" else project.parent
    _gates.capture_base(workspace, "proj")
    layout = "workspace" if project.parent.name == "projects" else "sandbox"
    delivery = _delivery.load(project)
    assert delivery is not None
    brief = f"Add clamp to calc.\nDelivers to: {delivery.repo}\n"
    _gates.run(workspace, "proj", brief, "", "DONE", {}, _timeline._Timeline(), layout)
    recorded = json.loads((workspace / ".claude-flyte/proj/standards-results.json").read_text())
    statuses = {r["rule"]: r["status"] for r in recorded}
    assert statuses["factory:target-ci"] == "PASS"
    assert statuses["factory:target-coverage"] == "FAIL"
    assert "factory:target-patches" not in statuses


def test_delivery_instructions_say_where_the_tree_is_and_who_imports_it(monkeypatch):
    monkeypatch.setattr(_session_config, "_project_brief", BRIEF)
    for layout, where in (("workspace", "projects/proj/"), ("sandbox", "proj/")):
        memory = _instructions._project_memory(False, name="proj", layout=layout)
        assert f"`{where}promote.toml`" in memory and f"`{where}<path>`" in memory
        assert "python ops/promote.py import" in memory and "Never clone the target" in memory
        assert "format-patch" not in memory and "patches/" not in memory
    monkeypatch.setattr(_session_config, "_project_brief", "An ordinary brief.\n")
    assert "promote.toml" not in _instructions._project_memory(False, name="proj", layout="workspace")


# --- the manifest is the operator's: the session cannot move the baseline or the target -----------


def test_a_manifest_naming_another_repository_than_delivers_to_is_refused(imported):
    project, _ = imported
    results = results_of(project, "acme/elsewhere")
    assert list(results) == ["factory:target-ci"]
    assert results["factory:target-ci"].status is _gate_base.Status.FAIL
    assert "acme/elsewhere" in results["factory:target-ci"].output
    assert "promote.toml" in results["factory:target-ci"].output


def test_the_same_repository_spelled_differently_is_accepted(tmp_path, operator):
    (tmp_path / "upstream").mkdir()
    target, _ = seed_target(tmp_path / "upstream", TARGET_FILES)
    project = make_workspace(tmp_path, "workspace")
    _delivery.import_tree(str(target), project, path="widgets")
    assert list(results_of(project, f"file://{target}/")) == ["factory:target-ci", "factory:target-coverage"]


def test_a_comment_only_edit_of_promote_toml_cannot_erase_the_review_diff(imported, tmp_path):
    """The attack: commit a comment-only manifest edit with the source change, so the newest manifest commit
    (the old baseline) already contains the work and nothing is left to review."""
    project, _ = imported
    before = results_of(project)["factory:target-coverage"]
    assert before.status is _gate_base.Status.FAIL and "calc.py:6" in before.output

    manifest = project / "promote.toml"
    manifest.write_text(manifest.read_text() + "# harmless\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "tidy the manifest")

    for rule, result in results_of(project).items():
        assert result.status is _gate_base.Status.FAIL, (rule, result.output)
    assert "promote.toml" in results_of(project)["factory:target-ci"].output


def test_a_value_change_of_the_manifest_base_is_refused_too(imported, tmp_path):
    project, _ = imported
    delivery = _delivery.load(project)
    assert delivery is not None
    _delivery.write(project, _delivery.Delivery(delivery.repo, "b" * 40, delivery.path))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "move the base")
    result = results_of(project)["factory:target-ci"]
    assert result.status is _gate_base.Status.FAIL and "promote.toml" in result.output


def test_an_uncommitted_manifest_edit_is_refused(imported):
    project, _ = imported
    (project / "promote.toml").write_text((project / "promote.toml").read_text() + "# x\n")
    assert results_of(project)["factory:target-ci"].status is _gate_base.Status.FAIL


def test_deleting_and_re_adding_the_manifest_does_not_move_the_base(imported, tmp_path):
    project, _ = imported
    original = (project / "promote.toml").read_text()
    (project / "promote.toml").unlink()
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "drop the manifest")
    (project / "promote.toml").write_text(original)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "put it back")
    # Same bytes, same base: the diff is still the one against the import.
    result = results_of(project)["factory:target-coverage"]
    assert result.status is _gate_base.Status.FAIL and "calc.py:6" in result.output


# --- a new repository has the empty tree as its baseline -----------------------------------------

# `ok.py` is tested so that pytest has something to run; `never()` is the untested change. The lint is inline:
# a `check.py` would itself be Python the new repository must cover.
NEW_FILES = {
    "app.py": "def never(x):\n    return x\n",
    "ok.py": "def ok():\n    return 1\n",
    "test_ok.py": "from ok import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
    "Makefile": "lint:\n\t@python -c pass\n" + COVERAGE,
}


@pytest.fixture
def new_project(tmp_path, operator):
    project = make_workspace(tmp_path, "workspace") / "tree"
    _delivery.import_tree("https://example.invalid/new.git", project, new=True)
    for name, text in NEW_FILES.items():
        (project / name).write_text(text)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "start")
    return project


def test_a_new_repository_must_cover_all_its_python(new_project):
    result = results_of(new_project)["factory:target-coverage"]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "app.py:1" in result.output and "app.py:2" in result.output
    assert "merge-base" not in result.output


def test_a_new_repository_honours_an_explicit_summary_exception(new_project):
    (new_project / "SUMMARY.md").write_text(
        "## Uncovered changed lines\n- app.py:1 -- demo\n- app.py:2 -- demo\n"
    )
    assert results_of(new_project)["factory:target-coverage"].status is _gate_base.Status.PASS


def test_a_new_repository_whose_python_is_tested_passes(new_project, tmp_path):
    test = "from app import never\n\n\ndef test_never():\n    assert never(1) == 1\n"
    (new_project / "test_app.py").write_text(test)
    assert results_of(new_project)["factory:target-coverage"].status is _gate_base.Status.PASS
