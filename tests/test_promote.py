"""Delivery: the project's committed tree goes to the target as one commit by the operator, checked first."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _delivery, _gate_base, _gates, _promote, _session_setup, _target_ci
from ops import promote as promote_cli
from tests._git import git
from tests.test_delivery import (
    CHECK_OK,
    MAKEFILE,
    commit,
    make_workspace,
    operator,  # noqa: F401 -- an autouse fixture, active by being imported
    seed_target,
)
from tests.test_target_ci import assert_measured_in_the_project_env, fake_uv

CHECK_RED = "import sys\nsys.exit('lint is red')\n"
WORKFLOW = (
    "name: ci\non: [pull_request]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n      - run: make lint\n"
)
TEST = "from calc import sign\n\n\ndef test_sign():\n    assert sign(1) == 1\n"
# Inline lint: a `check.py` would itself be Python a new repository must cover.
NEW_MAKEFILE = "lint:\n\t@python -c pass\n" + "coverage:\n\tpython -m coverage run --branch -m pytest -q\n"
OPERATOR = "Op Erator <operator@example.invalid>"


def clean(_path: Path) -> tuple[int, str]:
    return 0, "clean"


@pytest.fixture(autouse=True)
def _project_python_first(monkeypatch):
    """The target's commands run `python ...`: it must be this interpreter, as under `uv run`."""
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture
def world(tmp_path):
    """A bare target on `main`, and a workspace whose project `demo` imported it and committed a change."""
    remote, head = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    commit(workspace, {"projects/demo/notes.txt": "one\ntwo changed\nthree\n"}, "Change line two")
    return remote, workspace, project


def seed(remote: Path) -> Path:
    return remote.parent / "seed"


def push_upstream(remote: Path, files: dict[str, str], message: str = "upstream") -> None:
    commit(seed(remote), files, message)
    git(seed(remote), "push", "-q", "origin", "main")


def deliver(world, **kwargs):
    remote, _, project = world
    kwargs.setdefault("scan", clean)
    return _promote.deliver(project, "promote/demo", **kwargs)


def branches(remote: Path) -> str:
    return str(git(remote, "branch", "--list"))


def test_the_happy_path_pushes_one_commit_by_the_operator(world):
    remote, _, _ = world
    report = deliver(world)
    assert report.ok and report.stage == "done", report
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"
    assert git(remote, "log", "--format=%an <%ae>|%cn <%ce>|%s", "main..promote/demo") == (
        f"{OPERATOR}|{OPERATOR}|demo: promoted from agent workspace"
    )
    # The harness files stay in the workspace.
    assert "promote.toml" not in git(remote, "ls-tree", "--name-only", "promote/demo")


def test_a_title_is_the_commit_message(world):
    remote, _, _ = world
    assert deliver(world, title="Fix line two").ok
    assert git(remote, "log", "-1", "--format=%s", "promote/demo") == "Fix line two"


def test_a_dry_run_does_everything_but_push(world):
    remote, _, _ = world
    report = deliver(world, dry_run=True)
    assert report.ok and report.stage == "dry-run", report
    assert OPERATOR in report.message and "nothing pushed" in report.message
    assert "promote/demo" not in branches(remote)


def test_a_conflict_with_the_target_stops_before_push(world):
    remote, _, _ = world
    push_upstream(remote, {"notes.txt": "one\ntwo other\nthree\n"}, "Conflicting change on main")
    report = deliver(world)
    assert not report.ok and report.stage == "apply"
    assert "notes.txt" in report.message
    assert "promote/demo" not in branches(remote)


def test_what_the_target_changed_since_the_import_is_kept_not_reverted(world):
    remote, _, _ = world
    push_upstream(remote, {"other.txt": "moved on\n"}, "Default branch moved after the import")
    assert deliver(world).ok
    assert git(remote, "merge-base", "main", "promote/demo") == git(remote, "rev-parse", "main")
    assert git(remote, "show", "promote/demo:other.txt") == "moved on"
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"
    assert git(remote, "rev-list", "--count", "main..promote/demo") == "1"


def test_a_change_the_target_already_has_is_nothing_to_promote(world):
    remote, _, _ = world
    push_upstream(remote, {"notes.txt": "one\ntwo changed\nthree\n"}, "Same change, made upstream")
    report = deliver(world)
    assert not report.ok and report.stage == "empty"
    assert "promote/demo" not in branches(remote)


def test_a_tree_unchanged_since_the_import_is_nothing_to_promote(tmp_path):
    remote, _ = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    report = _promote.deliver(workspace / "projects/demo", "b", scan=clean)
    assert not report.ok and report.stage == "empty"


def test_a_second_delivery_updates_the_branch(world):
    remote, workspace, _ = world
    assert deliver(world).ok
    push_upstream(remote, {"other.txt": "moved on\n"})
    commit(workspace, {"projects/demo/notes.txt": "one\ntwo again\nthree\n"}, "More")
    assert deliver(world).ok
    assert git(remote, "merge-base", "main", "promote/demo") == git(remote, "rev-parse", "main")
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo again\nthree"
    assert git(remote, "rev-list", "--count", "main..promote/demo") == "1"


def test_red_lint_stops_before_push(world):
    remote, _, _ = world
    push_upstream(remote, {"check.py": CHECK_RED}, "Break the target's lint")
    report = deliver(world)
    assert not report.ok and report.stage == "ci"
    assert "lint is red" in report.message
    assert "promote/demo" not in branches(remote)


def test_a_ci_that_could_not_run_stops_before_push(world, monkeypatch):
    """A toolchain that cannot be installed shows nothing passes: nothing is pushed on it."""
    remote, _, _ = world
    not_run = _gates.Result("factory:target-ci", _gate_base.Status.NOT_RUN, "uv 0.13.4 cannot be installed")
    monkeypatch.setattr(_gates, "target_ci_result", lambda checkout, against_base: not_run)
    report = deliver(world)
    assert not report.ok and report.stage == "ci", report
    assert "uv 0.13.4 cannot be installed" in report.message
    assert "promote/demo" not in branches(remote)


def test_a_project_without_promote_toml_is_reported(tmp_path):
    workspace = make_workspace(tmp_path)
    report = _promote.deliver(workspace / "projects", "b", scan=clean)
    assert not report.ok and report.stage == "project" and "promote.py import" in report.message


def test_a_target_that_lost_the_import_commit_says_to_reimport(world, tmp_path):
    (tmp_path / "other").mkdir()
    other, _ = seed_target(tmp_path / "other", {"a.txt": "unrelated history\n"})
    report = deliver(world, to=str(other))
    assert not report.ok and report.stage == "base" and "re-import" in report.message


def test_an_empty_target_is_reported_not_raised(world, tmp_path):
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(empty))
    report = deliver(world, to=str(empty))
    assert not report.ok and report.stage == "clone"
    assert "no default branch" in report.message


def _edit_manifest(world, **changes):
    _, workspace, project = world
    delivery = _delivery.load(project)
    assert delivery is not None
    _delivery.write(project, dataclasses.replace(delivery, **changes))
    git(workspace, "commit", "-qam", "edit promote.toml with the work")


def test_a_project_commit_that_moves_the_base_is_refused_and_pushes_nothing(world):
    remote = world[0]
    push_upstream(remote, {"README.md": "upstream change\n"})
    _edit_manifest(world, base=str(git(seed(remote), "rev-parse", "HEAD")).strip())
    report = deliver(world)
    assert not report.ok and report.stage == "project" and "re-import" in report.message, report
    assert "promote/demo" not in branches(remote)


def test_a_project_commit_that_changes_the_repo_is_refused_and_pushes_nothing(world, tmp_path):
    (tmp_path / "fork").mkdir()
    fork, _ = seed_target(tmp_path / "fork")
    _edit_manifest(world, repo=str(fork))
    report = deliver(world)
    assert not report.ok and report.stage == "project", report
    assert "promote/demo" not in branches(fork) and "promote/demo" not in branches(world[0])


# -- refusals: nothing is pushed --------------------------------------------------------------


def _no_identity(world, monkeypatch, tmp_path):
    empty = tmp_path / "no-identity"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    return {}


def _dirty(world, monkeypatch, tmp_path):
    (world[2] / "notes.txt").write_text("edited after the review\n")
    return {}


def _untracked(world, monkeypatch, tmp_path):
    (world[2] / "scratch.txt").write_text("not committed\n")
    return {}


def _conflict(world, monkeypatch, tmp_path):
    push_upstream(world[0], {"notes.txt": "one\ntwo other\nthree\n"})
    return {}


def _empty(world, monkeypatch, tmp_path):
    push_upstream(world[0], {"notes.txt": "one\ntwo changed\nthree\n"})
    return {}


def _secret(world, monkeypatch, tmp_path):
    return {"scan": lambda path: (1, "notes.txt:2: a secret")}


def _scanner_missing(world, monkeypatch, tmp_path):
    return {"scan": lambda path: (2, "gitleaks is not installed")}


def _ci_fails(world, monkeypatch, tmp_path):
    push_upstream(world[0], {"check.py": CHECK_RED})
    return {}


def _unsigned_commit(world, monkeypatch, tmp_path):
    push_upstream(world[0], {".github/dco.yml": "require:\n  members: false\n"})
    real = _promote._build_branch

    def without_the_trailer(checkout, *args, **kwargs):
        head = real(checkout, *args, **kwargs)
        git(checkout, "commit", "--amend", "-q", "-m", "no trailer")
        return head

    monkeypatch.setattr(_promote, "_build_branch", without_the_trailer)
    return {}


@pytest.mark.parametrize(
    ("setup", "stage"),
    [
        (_no_identity, "identity"),
        (_dirty, "project"),
        (_untracked, "project"),
        (_conflict, "apply"),
        (_empty, "empty"),
        (_secret, "scan"),
        (_scanner_missing, "scan"),
        (_ci_fails, "ci"),
        (_unsigned_commit, "signoff"),
    ],
)
def test_delivery_refuses_and_pushes_nothing(world, monkeypatch, tmp_path, setup, stage):
    kwargs = setup(world, monkeypatch, tmp_path)
    report = deliver(world, **kwargs)
    assert not report.ok and report.stage == stage, report
    assert "promote/demo" not in branches(world[0])


def test_the_default_scan_refuses_an_owner_literal_in_the_change(world, monkeypatch, tmp_path):
    literals = tmp_path / "literals.txt"
    literals.write_text("acme-private-person\n")
    monkeypatch.setenv("FACTORY_OWNER_LITERALS", str(literals))
    commit(world[1], {"projects/demo/notes.txt": "one\nthanks to acme-private-person\nthree\n"}, "Name them")
    report = _promote.deliver(world[2], "promote/demo")
    assert not report.ok and report.stage == "scan", report
    assert "notes.txt:2" in report.message
    assert "promote/demo" not in branches(world[0])


def test_only_what_the_promotion_adds_is_scanned(world, monkeypatch, tmp_path):
    """Upstream files the promotion does not touch can name anyone; they must not block it."""
    scanned: list[list[str]] = []

    def record(path: Path) -> tuple[int, str]:
        scanned.append(sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()))
        return 0, "clean"

    assert deliver(world, scan=record).ok
    # The change, then the pull request's title and body: both leave as public text.
    assert scanned == [["notes.txt"], ["pull-request.md"]]


def test_the_pull_request_title_and_summary_are_scanned_before_anything_is_pushed(
    world, monkeypatch, tmp_path
):
    literals = tmp_path / "literals.txt"
    literals.write_text("acme-private-person\n")
    monkeypatch.setenv("FACTORY_OWNER_LITERALS", str(literals))
    commit(world[1], {"projects/demo/SUMMARY.md": "Reviewed with acme-private-person\n"}, "Summarise")
    report = _promote.deliver(world[2], "promote/demo")
    assert not report.ok and report.stage == "scan", report
    assert "pull request" in report.message
    assert "promote/demo" not in branches(world[0])

    commit(world[1], {"projects/demo/SUMMARY.md": "Reviewed\n"}, "Summarise plainly")
    report = _promote.deliver(world[2], "promote/demo", title="Thanks acme-private-person")
    assert not report.ok and report.stage == "scan", report
    assert "promote/demo" not in branches(world[0])


def test_a_relative_project_path_is_resolved_first(world, monkeypatch):
    monkeypatch.chdir(world[1])
    report = _promote.deliver(Path("projects/demo"), "promote/demo", scan=clean)
    assert report.ok, report


def test_delivery_leaves_the_workspace_as_it_found_it(world):
    _, workspace, _ = world
    before = git(workspace, "status", "--porcelain", "--ignored"), git(workspace, "rev-parse", "HEAD")
    assert deliver(world).ok
    assert (
        git(workspace, "status", "--porcelain", "--ignored"),
        git(workspace, "rev-parse", "HEAD"),
    ) == before


def test_a_sandbox_layout_project_is_delivered_from_its_own_directory(tmp_path):
    remote, _ = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "demo")
    commit(workspace, {"demo/notes.txt": "one\ntwo changed\nthree\n"})
    report = _promote.deliver(workspace / "demo", "promote/demo", scan=clean)
    assert report.ok and report.stage == "done", report
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo changed\nthree"


def test_a_tree_in_a_subdirectory_goes_to_the_targets_root(tmp_path):
    remote, _ = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project, path="upstream")
    commit(workspace, {"projects/demo/upstream/notes.txt": "sub\n", "projects/demo/checks.sh": "exit 0\n"})
    assert _promote.deliver(project, "b", scan=clean).ok
    assert git(remote, "show", "b:notes.txt") == "sub"
    assert "checks.sh" not in git(remote, "ls-tree", "-r", "--name-only", "b")


# -- the bootstrap: a tree with no promote.toml, from `ops/public_tree.py build` ------------------


def test_a_built_tree_is_delivered_with_base_and_to(tmp_path):
    remote, head = seed_target(tmp_path)
    tree = tmp_path / "built"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    commit(tree, {"Makefile": MAKEFILE, "check.py": CHECK_OK, "notes.txt": "one\ntwo\nthree\nfour\n"}, "tree")
    report = _promote.deliver(
        tree, "engine", to=str(remote), base=head[:7], title="Bring the engine", scan=clean
    )
    assert report.ok, report
    assert git(remote, "show", "engine:notes.txt") == "one\ntwo\nthree\nfour"
    assert git(remote, "log", "-1", "--format=%s", "engine") == "Bring the engine"


def test_a_new_repository_is_pushed_as_its_first_branch(tmp_path):
    remote = tmp_path / "new.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/n"
    _delivery.import_tree(str(remote), project, new=True)
    commit(
        workspace,
        {
            "projects/n/app.py": "x = 1\n",
            # A new repository's whole tree is changed code: its Python is tested, as the gate demands.
            "projects/n/test_app.py": "import app\n\n\ndef test_app():\n    assert app.x == 1\n",
            "projects/n/Makefile": NEW_MAKEFILE,
        },
    )
    report = _promote.deliver(project, "main", scan=clean)
    assert report.ok and report.stage == "done" and not report.url, report
    assert git(remote, "show", "main:app.py") == "x = 1"
    assert git(remote, "log", "--format=%an", "main") == "Op Erator"


# -- contribution rules --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "files",
    [
        {".github/dco.yml": "require:\n  members: false\n"},
        {"DCO": "Developer Certificate of Origin\n"},
        {"CONTRIBUTING.md": "# Contributing\n\nSign every commit: Developer Certificate of Origin.\n"},
        {"docs/CONTRIBUTING.md": "Commits need a Signed-off-by line.\n"},
        {".github/CONTRIBUTING.rst": "Please DCO sign your work.\n"},
        {".github/workflows/dco.yml": "name: check\non: [pull_request]\njobs: {}\n"},
        {".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - uses: tim-actions/dco@master\n"},
    ],
)
def test_contribution_rules_detect_a_dco(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert _promote.contribution_rules(tmp_path) == _promote.Rules(True)


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"CONTRIBUTING.md": "Be kind. Run the tests.\n"},
        {".github/workflows/ci.yml": "name: ci\njobs:\n  a:\n    steps:\n      - run: docker build .\n"},
        {"README.md": "Developer Certificate of Origin\n"},
    ],
)
def test_contribution_rules_find_no_dco_otherwise(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    assert _promote.contribution_rules(tmp_path) == _promote.Rules(False)


def test_contribution_rules_report_a_cla_and_conventional_commits_without_a_trailer(tmp_path):
    (tmp_path / "CONTRIBUTING.md").write_text("Sign the CLA first. We use Conventional Commits.\n")
    rules = _promote.contribution_rules(tmp_path)
    assert not rules.dco
    assert [("CLA" in n, "Conventional Commits" in n) for n in rules.notices] == [
        (True, False),
        (False, True),
    ]


@pytest.mark.parametrize(
    ("target_files", "flag", "signed"),
    [
        ({".github/dco.yml": "x: 1\n"}, None, True),
        ({".github/dco.yml": "x: 1\n"}, False, False),
        ({}, None, False),
        ({}, True, True),
    ],
)
def test_the_dco_flags_override_what_the_target_asks(tmp_path, target_files, flag, signed):
    remote, _ = seed_target(
        tmp_path, {"Makefile": MAKEFILE, "check.py": CHECK_OK, "n.txt": "a\n", **target_files}
    )
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    commit(workspace, {"projects/demo/n.txt": "b\n"})
    assert _promote.deliver(workspace / "projects/demo", "b", dco=flag, scan=clean).ok
    trailer = f"Signed-off-by: {OPERATOR}"
    assert git(remote, "log", "-1", "--format=%B", "b").count(trailer) == (1 if signed else 0)


def test_a_cla_is_a_notice_in_the_report_and_adds_no_trailer(tmp_path):
    remote, _ = seed_target(
        tmp_path,
        {"Makefile": MAKEFILE, "check.py": CHECK_OK, "n.txt": "a\n", "CONTRIBUTING.md": "Sign the CLA.\n"},
    )
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    commit(workspace, {"projects/demo/n.txt": "b\n"})
    report = _promote.deliver(workspace / "projects/demo", "b", scan=clean)
    assert report.ok and "CLA" in report.message
    assert "Signed-off-by" not in git(remote, "log", "-1", "--format=%B", "b")


def test_unsigned_commits_are_the_ones_without_a_sign_off_by_their_author(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"a": "1"}, "base")
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"a": "2"}, f"signed\n\nSigned-off-by: {OPERATOR}")
    commit(repo, {"a": "3"}, "signed by someone else\n\nSigned-off-by: Other <other@example.invalid>")
    commit(repo, {"a": "4"}, "unsigned")
    unsigned = _promote._unsigned(repo, base)
    assert [git(repo, "log", "-1", "--format=%s", sha) for sha in unsigned] == [
        "unsigned",
        "signed by someone else",
    ]
    assert _promote._unsigned(repo, git(repo, "rev-parse", "HEAD~1")) == unsigned[:1]


# -- end to end ----------------------------------------------------------------------------------


def _fake_gh(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n[ "$2" = list ] && exit 0\necho https://example.invalid/pull/7\n'
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_end_to_end_import_edit_dry_run_then_push_and_pull_request(tmp_path, monkeypatch):
    """The whole path against a local bare target with a DCO and a CI workflow; nothing is merged."""
    remote, _ = seed_target(
        tmp_path,
        {
            "Makefile": MAKEFILE,
            "check.py": CHECK_OK,
            "notes.txt": "one\ntwo\nthree\n",
            ".github/dco.yml": "require:\n  members: false\n",
            ".github/workflows/ci.yml": WORKFLOW,
        },
    )
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    push_upstream(remote, {"upstream.txt": "arrived after the import\n"}, "Upstream moves on")
    commit(workspace, {"projects/demo/notes.txt": "one\ntwo edited\nthree\n"}, "Edit line two")
    log = _fake_gh(tmp_path, monkeypatch)
    head = git(remote, "rev-parse", "main")

    dry = _promote.deliver(project, "promote/demo", dry_run=True)  # the real scanner and the target's own CI
    assert dry.ok and dry.stage == "dry-run", dry
    assert f"by {OPERATOR}" in dry.message and "sign-off: yes" in dry.message
    assert "scan clean" in dry.message and "CI PASS" in dry.message and "nothing pushed" in dry.message
    assert "promote/demo" not in branches(remote) and not log.exists()

    done = _promote.deliver(project, "promote/demo")
    assert done.ok and done.stage == "done" and done.url == "https://example.invalid/pull/7", done
    assert git(remote, "rev-list", "--count", f"{head}..promote/demo") == "1"
    assert git(remote, "rev-parse", "promote/demo~1") == head
    assert git(remote, "log", "-1", "--format=%an <%ae>", "promote/demo") == OPERATOR
    body = git(remote, "log", "-1", "--format=%B", "promote/demo")
    assert body.count("Signed-off-by:") == 1 and f"Signed-off-by: {OPERATOR}" in body
    assert git(remote, "show", "promote/demo:upstream.txt") == "arrived after the import"
    assert git(remote, "show", "promote/demo:notes.txt") == "one\ntwo edited\nthree"
    calls = log.read_text().splitlines()
    assert any(
        call.startswith(f"pr create --repo {remote} --head promote/demo --base main") for call in calls
    )
    assert not any("merge" in call for call in calls)


def test_the_fork_receives_the_branch_and_the_pull_request_names_its_owner(tmp_path, monkeypatch):
    remote, _ = seed_target(tmp_path)
    fork = tmp_path / "fork.git"
    git(tmp_path, "clone", "-q", "--bare", str(remote), str(fork))
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    commit(workspace, {"projects/demo/notes.txt": "one\ntwo changed\nthree\n"})
    log = _fake_gh(tmp_path, monkeypatch)

    report = _promote.deliver(project, "promote/demo", fork=str(fork), scan=clean)

    assert report.ok, report
    assert "promote/demo" in branches(fork) and "promote/demo" not in branches(remote)
    assert "pr create --repo" in log.read_text()


def test_a_failing_gh_call_is_reported_with_its_stderr(world, tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gh").write_text("#!/bin/sh\necho 'gh: token expired' >&2\nexit 1\n")
    (fake / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    report = deliver(world)
    assert report.ok and report.stage == "done"
    assert "gh pr create failed" in report.message and "token expired" in report.message


def test_without_gh_the_report_has_no_url_for_a_local_target(world, monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    report = deliver(world)
    assert report.ok and "open a pull request there" in report.url


def test_without_a_pull_request_the_report_carries_its_body_for_the_operator(world, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    report = deliver(world, fixes=("acme/calc#7",))
    assert report.ok and "Fixes acme/calc#7" in report.message, report


def test_an_opened_pull_request_does_not_repeat_its_body_in_the_report(world, tmp_path, monkeypatch):
    _fake_gh(tmp_path, monkeypatch)
    report = deliver(world, fixes=("acme/calc#7",))
    assert report.ok and "Fixes acme/calc#7" not in report.message, report


def test_the_github_compare_url_names_the_fork_owner(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    url, problem = _promote._open_pull_request(
        Path("."), "https://github.com/Acme/Widgets.git", "someone", "main", "b", "t", "body"
    )
    assert url == "https://github.com/acme/widgets/compare/main...someone:b?expand=1" and not problem


def test_a_fork_that_is_not_on_github_is_named_as_where_the_branch_went(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    url, _ = _promote._open_pull_request(
        Path("."), "file:///srv/target.git", "", "main", "b", "t", "body", pushed_to="file:///srv/fork.git"
    )
    assert url == "branch b pushed to file:///srv/fork.git; open a pull request there"


# -- uncovered changed lines ---------------------------------------------------------------------


def _coverage_world(tmp_path, change: dict[str, str] | None = None):
    """A target and a project whose change adds `never()`, which no test calls (unless `change` says else)."""
    coverage = "coverage:\n\tpython -m coverage run --branch -m pytest -q\n"
    remote, _ = seed_target(
        tmp_path, {"Makefile": coverage, "calc.py": "def sign(x):\n    return 1\n", "test_calc.py": TEST}
    )
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    never = {"calc.py": "def sign(x):\n    return 1\n\n\ndef never(x):\n    return 2\n"}
    commit(workspace, {f"projects/demo/{k}": v for k, v in (change or never).items()}, "Add never")
    return remote, workspace, project


def test_uncovered_changed_python_line_stops_before_push(tmp_path):
    remote, workspace, project = _coverage_world(tmp_path)
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert "calc.py:6" in report.message
    assert "promote/demo" not in branches(remote)

    commit(workspace, {"projects/demo/SUMMARY.md": "## Uncovered changed lines\n- calc.py:6 -- demo\n"})
    assert _promote.deliver(project, "promote/demo", scan=clean).ok


def test_a_test_that_execs_a_phantom_file_does_not_stop_the_coverage_stage(tmp_path):
    # The engine's docs test execs code compiled as `quickstart-storage-probe`: coverage has no source for it.
    phantom = TEST + "\n\ndef test_phantom():\n    exec(compile('x = 1', 'phantom-probe', 'exec'), {})\n"
    calc = "def sign(x):\n    return 1\n\n\ndef never(x):\n    return 2\n"
    remote, _, project = _coverage_world(tmp_path, {"calc.py": calc, "test_calc.py": phantom})
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert report.message == "changed lines not covered: calc.py:6"
    assert "promote/demo" not in branches(remote)


def test_a_fallback_coverage_run_without_pytest_cov_says_what_to_install(tmp_path, monkeypatch):
    remote, _, project = _coverage_world(tmp_path)
    monkeypatch.setattr(
        _target_ci,
        "coverage_command",
        lambda checkout: _target_ci.Command(_target_ci.FALLBACK_COVERAGE, "default"),
    )
    monkeypatch.setattr(
        _gates, "run_target_command", lambda command, cwd: "error: unrecognized arguments: --cov --cov-branch"
    )
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert "pip install pytest-cov" in report.message
    assert "promote/demo" not in branches(remote)


def test_a_new_module_no_test_imports_stops_before_push(tmp_path):
    module = "def pick(x):\n    if x:\n        return 1\n    return 2\n"
    remote, _, project = _coverage_world(tmp_path, {"b.py": module})
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert "b.py:1, b.py:2, b.py:3, b.py:4" in report.message
    assert "promote/demo" not in branches(remote)


def test_uncovered_line_is_found_when_git_diffs_without_prefixes(tmp_path, monkeypatch):
    remote, _, project = _coverage_world(tmp_path)
    config = Path(os.environ["GIT_CONFIG_GLOBAL"])
    config.write_text(config.read_text() + "[diff]\n\tnoprefix = true\n")
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert "calc.py:6" in report.message


def _new_target(tmp_path, summary: str = ""):
    """A `--new` import delivering `never()`, which no test calls, to a repository that has no commits."""
    remote = tmp_path / "new.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/n"
    _delivery.import_tree(str(remote), project, new=True)
    files = {
        "app.py": "def never(x):\n    return x\n",
        # Something tested, so pytest has a test to run; `never()` is the untested change.
        "ok.py": "def ok():\n    return 1\n",
        "test_ok.py": "from ok import ok\n\n\ndef test_ok():\n    assert ok() == 1\n",
        "Makefile": NEW_MAKEFILE,
    }
    if summary:
        files["SUMMARY.md"] = summary
    commit(workspace, {f"projects/n/{k}": v for k, v in files.items()})
    return remote, project


def test_a_new_repository_with_an_untested_python_function_is_refused(tmp_path):
    remote, project = _new_target(tmp_path)
    report = _promote.deliver(project, "main", scan=clean)
    assert not report.ok and report.stage == "coverage", report
    assert "app.py:1" in report.message and "app.py:2" in report.message
    assert "main" not in branches(remote)


def test_a_new_repository_honours_an_explicit_summary_exception(tmp_path):
    summary = "## Uncovered changed lines\n- app.py:1 -- demo\n- app.py:2 -- demo\n"
    remote, project = _new_target(tmp_path, summary)
    report = _promote.deliver(project, "main", scan=clean)
    assert report.ok, report
    assert git(remote, "show", "main:app.py").startswith("def never")


def test_promote_defines_no_copy_of_the_gates_functions():
    assert not {"_changed_lines", "_uncovered", "_check_ci"} & set(vars(_promote))


# -- sessions never sign off ----------------------------------------------------------------------


def test_sessions_never_sign_off(monkeypatch):
    """A sign-off is the operator's statement about their own commit: nothing a session runs adds one."""
    from flyte_agent_factory import _instructions

    forbidden = ("Signed-off-by", "--signoff", "commit -s", "format.signOff")
    for module in (_session_setup, _instructions):
        text = Path(str(module.__file__)).read_text()
        assert not [word for word in forbidden if word in text], module.__name__

    monkeypatch.setattr(os, "environ", {"PATH": "/usr/bin"})
    _session_setup._export_runtime_env()
    git_env = {k: v for k, v in os.environ.items() if k.startswith("GIT_")}
    assert git_env and not [k for k, v in git_env.items() if "sign" in k.lower() or "sign" in v.lower()]


def test_the_pull_request_body_says_which_public_issues_it_fixes(tmp_path, monkeypatch):
    remote, _ = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    commit(
        workspace,
        {"projects/demo/notes.txt": "one\ntwo changed\nthree\n", "projects/demo/SUMMARY.md": "Edit\n"},
    )
    log = _fake_gh(tmp_path, monkeypatch)

    report = _promote.deliver(project, "promote/demo", fixes=["acme/widget#12", "acme/widget#13"], scan=clean)

    assert report.ok, report
    text = log.read_text()
    assert "Edit" in text and "Fixes acme/widget#12\nFixes acme/widget#13" in text


@pytest.mark.parametrize("ref", ["widget#12", "acme/widget", "acme/widget#x", "#12", "acme/widget#12 extra"])
def test_a_malformed_fixes_reference_stops_before_anything_is_pushed(world, ref):
    report = deliver(world, fixes=[ref])
    assert not report.ok and report.stage == "fixes" and ref in report.message, report
    assert "promote/demo" not in branches(world[0])


def test_the_factorys_harness_directory_never_leaves_the_workspace(world):
    commit(world[1], {"projects/demo/.claude-flyte/demo/ACCEPTANCE.md": "criteria\n"}, "Harness inside")
    report = deliver(world)
    assert not report.ok and report.stage == "harness", report
    assert ".claude-flyte/demo/ACCEPTANCE.md" in report.message
    assert "promote/demo" not in branches(world[0])


def test_a_reported_file_outside_the_checkout_is_not_a_changed_file(tmp_path):
    # A target without a coverage `source` reports what it imported from elsewhere too (a PYTHONPATH
    # directory, site-packages): not the change, so not a crash either.
    report = {"files": {"/usr/lib/x.py": {"missing_lines": [1]}, "a.py": {"missing_lines": [2]}}}
    assert _gates.uncovered_changed_lines(report, tmp_path, {"a.py": {1, 2}}) == {"a.py:2"}


def test_coverage_is_read_with_the_python_that_measured_it(tmp_path, monkeypatch):
    # The fallback measures with `python -m pytest --cov` from PATH; `coverage json` must be that python.
    remote, workspace, project = _coverage_world(tmp_path)
    calls = []
    real = _promote._run

    def run(argv, cwd, **kwargs):
        calls.append(argv)
        return real(argv, cwd, **kwargs)

    monkeypatch.setattr(_promote, "_run", run)
    _promote.deliver(project, "promote/demo", scan=clean)
    assert [argv[0] for argv in calls if argv[1:4] == ["-m", "coverage", "json"]] == ["python"]


def test_a_uv_target_without_pytest_cov_is_measured_and_read_in_its_own_env(tmp_path, monkeypatch):
    # The target's `uv.lock` environment lacks pytest-cov, and so does the python on PATH: it is installed
    # into the project's own environment, where both the measurement and `coverage json` run.
    remote, _ = seed_target(
        tmp_path,
        {"uv.lock": "version = 1\n", "calc.py": "def sign(x):\n    return 1\n", "test_calc.py": TEST},
    )
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    never = "def sign(x):\n    return 1\n\n\ndef never(x):\n    return 2\n"
    commit(workspace, {"projects/demo/calc.py": never}, "Add never")
    log = tmp_path / "uv.log"
    fake_uv(tmp_path / "bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage", report
    assert "calc.py:6" in report.message
    assert_measured_in_the_project_env(log)


def test_a_file_the_targets_tests_leave_behind_does_not_turn_the_verdict_into_a_crash(world, monkeypatch):
    # A process the target's own tests start and leave running can still be writing into the scratch
    # checkout when promotion cleans it up (the engine's suite did): the verdict stands, the leftover stays.
    import errno

    # tempfile cleans up through `shutil.rmtree` (its `_shutil` is this module).
    real = shutil.rmtree

    def racing_rmtree(path, ignore_errors=False, onexc=None, **kwargs):
        real(path, ignore_errors=ignore_errors, onexc=onexc, **kwargs)
        failure = OSError(errno.ENOTEMPTY, "Directory not empty", str(path))
        try:
            raise failure
        except OSError:
            onexc(os.rmdir, str(path), failure)  # as shutil calls it: inside the handler of the error

    monkeypatch.setattr(shutil, "rmtree", racing_rmtree)
    report = deliver(world, dry_run=True)
    assert report.ok, report


# -- stages that refuse or fail, each reported with its stage ------------------------------------


def test_a_failed_git_command_is_a_refusal_at_its_stage(tmp_path):
    with pytest.raises(_promote._Refuse, match="not a git repository") as refusal:
        _promote._git(tmp_path, "clone", "rev-parse", "HEAD")
    assert refusal.value.stage == "clone"


def test_a_failing_coverage_command_that_is_not_the_fallback_is_reported_as_it_is(tmp_path, monkeypatch):
    remote, _, project = _coverage_world(tmp_path)
    monkeypatch.setattr(_gates, "run_target_command", lambda command, cwd: "make: coverage failed (--cov)")
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert report.message == "make: coverage failed (--cov)"
    assert "promote/demo" not in branches(remote)


def test_a_failing_coverage_reader_is_reported_with_its_output(tmp_path, monkeypatch):
    remote, _, project = _coverage_world(tmp_path)
    calls = []

    def run_target_command(command, cwd):
        calls.append(command.command)
        return "" if len(calls) == 1 else "No data to report." + "x" * 5000

    monkeypatch.setattr(_gates, "run_target_command", run_target_command)
    monkeypatch.setattr(
        _target_ci, "coverage_report_command", lambda measured, out: _target_ci.Command("read it", "default")
    )
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "coverage"
    assert report.message.startswith("`coverage json` failed:\n") and report.message.endswith("x" * 3000)
    assert calls[-1] == "read it" and len(report.message) == len("`coverage json` failed:\n") + 3000
    assert "promote/demo" not in branches(remote)


def test_a_coverage_json_that_cannot_be_written_is_reported_with_its_exit_code(tmp_path, monkeypatch):
    remote, _, project = _coverage_world(tmp_path)
    commands = []

    def run(argv, cwd, **kwargs):
        commands.append(argv[1:4])
        return 1, "No data to report."

    monkeypatch.setattr(_promote, "_run", run)
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert commands == [["-m", "coverage", "json"]], "nothing else runs before `coverage json` fails"
    assert not report.ok and report.stage == "coverage"
    assert report.message == "`coverage json` exited 1:\nNo data to report."
    assert "promote/demo" not in branches(remote)


def test_a_missing_public_scanner_is_a_scan_failure_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(_promote, "_SCAN_PUBLIC", tmp_path / "no_such_scanner.py")
    code, output = _promote._scan_public(tmp_path)
    assert code == 2 and "cannot find" in output and "no_such_scanner.py" in output


def _gh_that_knows_the_pull_request(tmp_path: Path, monkeypatch, edit_exit: int) -> Path:
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n'
        '[ "$2" = list ] && { echo https://example.invalid/pull/3; exit 0; }\n'
        f'[ "$2" = edit ] && {{ echo "gh: edit refused" >&2; exit {edit_exit}; }}\n'
        "exit 9\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_a_pull_request_that_exists_is_edited_not_created(world, tmp_path, monkeypatch):
    log = _gh_that_knows_the_pull_request(tmp_path, monkeypatch, edit_exit=0)
    report = deliver(world)
    assert report.ok and report.url == "https://example.invalid/pull/3", report
    rows = log.read_text().splitlines()
    assert any(row.startswith("pr edit https://example.invalid/pull/3 --title") for row in rows)
    assert not any(row.startswith("pr create") for row in rows)


def test_a_pull_request_that_cannot_be_edited_is_reported_with_the_link(world, tmp_path, monkeypatch):
    _gh_that_knows_the_pull_request(tmp_path, monkeypatch, edit_exit=1)
    report = deliver(world)
    assert report.ok and report.url == "https://example.invalid/pull/3", report
    assert "gh pr edit failed (1)" in report.message and "edit refused" in report.message


def test_a_cherry_pick_that_fails_without_conflicts_is_reported_as_it_failed(world, monkeypatch):
    remote, _, _ = world
    real = _delivery.git

    def git_that_cannot_pick(cwd, *args, **kwargs):
        if args[0] == "cherry-pick":
            raise _delivery.DeliveryError("git cherry-pick failed (128): fatal: bad object")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(_delivery, "git", git_that_cannot_pick)
    report = deliver(world)
    assert not report.ok and report.stage == "apply"
    assert report.message == "git cherry-pick failed (128): fatal: bad object"
    assert "promote/demo" not in branches(remote)


def test_a_malformed_promote_toml_stops_delivery_at_the_project_stage(world):
    remote, _, project = world
    (project / "promote.toml").write_text("not toml [")
    report = deliver(world)
    assert not report.ok and report.stage == "project" and "not valid TOML" in report.message
    assert "promote/demo" not in branches(remote)


def test_a_committed_tree_that_cannot_be_read_stops_delivery_at_the_project_stage(tmp_path):
    remote, _ = seed_target(tmp_path)
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project, path="upstream")
    git(workspace, "rm", "-rq", "projects/demo/upstream")
    git(workspace, "commit", "-qm", "remove the tree")
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert not report.ok and report.stage == "project"
    assert report.message.startswith("the committed tree is unreadable:")
    assert "promote/demo" not in branches(remote)


def test_a_tree_with_no_files_is_nothing_to_promote(tmp_path):
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/n"
    _delivery.import_tree("https://example.invalid/new.git", project, new=True)
    report = _promote.deliver(project, "promote/n", scan=clean)
    assert not report.ok and report.stage == "empty"
    assert report.message == "nothing to promote: the tree has no files"


def test_a_push_the_target_rejects_is_reported_with_its_output(world):
    remote, _, _ = world
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\necho 'no pushes today' >&2\nexit 1\n")
    hook.chmod(0o755)
    report = deliver(world)
    assert not report.ok and report.stage == "push"
    assert report.message.startswith("git push failed (1):") and "no pushes today" in report.message
    assert "promote/demo" not in branches(remote)


# -- the command line ----------------------------------------------------------------------------


def test_the_import_command_imports_commits_and_says_where_the_base_is(world, capsys):
    remote, workspace, _ = world
    project = workspace / "projects/cli"
    assert promote_cli.main(["import", str(remote), str(project)]) == 0
    delivery = _delivery.load(project)
    assert delivery is not None and delivery.base
    out = capsys.readouterr().out
    assert f"base {delivery.base}; push the workspace to keep it." in out
    assert (project / "notes.txt").is_file()


def test_the_import_command_of_a_new_repository_says_so(tmp_path, capsys):
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/cli"
    assert promote_cli.main(["import", "https://example.invalid/new.git", str(project), "--new"]) == 0
    assert "base (new repository); push the workspace" in capsys.readouterr().out


def test_the_import_command_reports_a_refusal_on_stderr_and_exits_1(world, capsys):
    remote, _, project = world  # already imported: a second import into it is refused
    assert promote_cli.main(["import", str(remote), str(project)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("import: ") and "already delivers" in captured.err


def test_the_deliver_command_passes_its_flags_on_and_exits_by_the_report(monkeypatch, capsys):
    calls = []

    def fake_deliver(project, branch, **kwargs):
        calls.append((project, branch, kwargs))
        return _promote.Report(True, "done", "pushed it", "https://example.invalid/pull/1")

    monkeypatch.setattr(_promote, "deliver", fake_deliver)
    argv = ["deliver", "proj", "br", "--to", "T", "--fork", "F", "--title", "Ti", "--base", "B", "--no-dco"]
    assert promote_cli.main([*argv, "--fixes", "a/b#1", "--fixes", "a/b#2", "--dry-run"]) == 0
    assert calls == [
        (
            Path("proj"),
            "br",
            {
                "to": "T",
                "fork": "F",
                "title": "Ti",
                "base": "B",
                "dco": False,
                "fixes": ["a/b#1", "a/b#2"],
                "dry_run": True,
                "replace": False,
            },
        )
    ]
    assert capsys.readouterr().out == "done: pushed it\nhttps://example.invalid/pull/1\n"


def test_the_deliver_command_exits_1_when_the_promotion_stopped(monkeypatch, capsys):
    monkeypatch.setattr(_promote, "deliver", lambda *args, **kwargs: _promote.Report(False, "scan", "no"))
    assert promote_cli.main(["deliver", "proj", "br"]) == 1
    assert capsys.readouterr().out == "scan: no\n"
