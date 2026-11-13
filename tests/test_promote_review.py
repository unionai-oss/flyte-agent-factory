"""Promotion review fixes: foreign branches, `--new` reruns, submodules, closed PRs, DCO warning, timeouts."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _delivery, _promote
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
from tests.test_promote import (
    NEW_MAKEFILE,
    OPERATOR,
    _project_python_first,  # noqa: F401 -- an autouse fixture, active by being imported
    clean,
    deliver,
    world,  # noqa: F401 -- a fixture of test_promote
)


def foreign_branch(remote: Path, name: str = "release-1.0") -> str:
    """Put a commit by someone else on `name` of the bare target; return its sha."""
    other = remote.parent / "other"
    git(remote.parent, "clone", "-q", str(remote), str(other))
    git(other, "checkout", "-q", "-b", name)
    commit(other, {"theirs.txt": "someone's work\n"}, "Their commit")
    git(
        other,
        "-c",
        "user.name=Else",
        "-c",
        "user.email=else@example.invalid",
        "commit",
        "-q",
        "--amend",
        "--reset-author",
        "--no-edit",
    )
    git(other, "push", "-q", "origin", name)
    return str(git(remote, "rev-parse", name))


# -- 1. an existing target branch is not overwritten ----------------------------------------------


def test_a_foreign_branch_is_refused_and_left_alone(world):  # noqa: F811 -- `world` is the imported fixture
    remote, _, project = world
    tip = foreign_branch(remote)
    report = _promote.deliver(project, "release-1.0", scan=clean)
    assert not report.ok and report.stage == "branch", report
    assert "release-1.0" in report.message and tip[:10] in report.message and "--replace" in report.message
    assert git(remote, "rev-parse", "release-1.0") == tip


def test_a_dry_run_reports_the_refusal_too(world):  # noqa: F811 -- `world` is the imported fixture
    remote, _, project = world
    foreign_branch(remote)
    report = _promote.deliver(project, "release-1.0", scan=clean, dry_run=True)
    assert not report.ok and report.stage == "branch", report


def test_replace_allows_replacing_a_foreign_branch(world):  # noqa: F811 -- `world` is the imported fixture
    remote, _, project = world
    foreign_branch(remote)
    report = _promote.deliver(project, "release-1.0", scan=clean, replace=True)
    assert report.ok and report.stage == "done", report
    assert git(remote, "show", "release-1.0:notes.txt") == "one\ntwo changed\nthree"
    assert git(remote, "rev-list", "--count", "main..release-1.0") == "1"


def test_a_rerun_replaces_its_own_previous_promotion_without_replace(world):  # noqa: F811 -- `world` is the imported fixture
    remote, workspace, project = world
    assert deliver(world).ok
    first = git(remote, "rev-parse", "promote/demo")
    commit(workspace, {"projects/demo/notes.txt": "one\ntwo again\nthree\n"}, "More")
    assert deliver(world).ok
    assert git(remote, "rev-parse", "promote/demo") != first


def test_a_branch_by_another_author_over_the_head_is_foreign(world, monkeypatch):  # noqa: F811 -- `world` is the imported fixture
    remote, _, project = world
    assert deliver(world).ok
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.email")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "someone.else@example.invalid")
    tip = git(remote, "rev-parse", "promote/demo")
    report = deliver(world)
    assert not report.ok and report.stage == "branch", report
    assert git(remote, "rev-parse", "promote/demo") == tip


def test_a_branch_that_moved_between_check_and_push_is_not_overwritten(world, monkeypatch):  # noqa: F811 -- `world` is the imported fixture
    remote, _, _ = world
    real = _promote._push
    theirs = []

    def racing_push(*args, **kwargs):
        theirs.append(foreign_branch(remote, "promote/demo"))
        return real(*args, **kwargs)

    monkeypatch.setattr(_promote, "_push", racing_push)
    report = deliver(world)
    assert not report.ok and report.stage == "push", report
    assert git(remote, "rev-parse", "promote/demo") == theirs[0]


def test_the_deliver_command_has_a_replace_flag(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_deliver(*args: object, **kwargs: object) -> _promote.Report:
        calls.append(kwargs)
        return _promote.Report(True, "done", "x")

    monkeypatch.setattr(_promote, "deliver", fake_deliver)
    assert promote_cli.main(["deliver", "proj", "br", "--replace"]) == 0
    assert calls[0]["replace"] is True


# -- 2. every subprocess call has a timeout --------------------------------------------------------


def test_no_subprocess_call_in_promote_lacks_a_timeout():
    tree = ast.parse(Path(_promote.__file__).read_text())
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr != "TimeoutExpired"
        and not any(kw.arg == "timeout" for kw in node.keywords)
    ]
    assert not bare, f"subprocess calls without timeout= at lines {bare}"


def test_the_operator_is_read_with_a_timeout_and_a_hang_counts_as_unset(monkeypatch):
    def hang(*args, **kwargs):
        assert kwargs.get("timeout")
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(_promote._Refuse) as caught:
        _promote._operator()
    assert caught.value.stage == "identity"


# -- 3. a --new project is delivered once ---------------------------------------------------------


def test_a_new_project_delivered_twice_is_refused_the_second_time(tmp_path):
    remote = tmp_path / "new.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/n"
    _delivery.import_tree(str(remote), project, new=True)
    commit(
        workspace,
        {
            "projects/n/app.py": "x = 1\n",
            "projects/n/test_app.py": "import app\n\n\ndef test_app():\n    assert app.x == 1\n",
            "projects/n/Makefile": NEW_MAKEFILE,
        },
    )
    assert _promote.deliver(project, "main", scan=clean).ok
    first = git(remote, "rev-parse", "main")
    report = _promote.deliver(project, "main", scan=clean)
    assert not report.ok and report.stage == "new", report
    assert "re-import" in report.message
    assert git(remote, "rev-parse", "main") == first


# -- 4. submodules survive -------------------------------------------------------------------------


def test_a_targets_submodule_is_not_deleted_by_the_promotion(tmp_path):
    (tmp_path / "subhome").mkdir()
    sub, _ = seed_target(tmp_path / "subhome", {"lib.txt": "lib\n"})
    remote, _ = seed_target(tmp_path)
    seeded = tmp_path / "seed"
    git(seeded, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    git(seeded, "commit", "-q", "-m", "Add a submodule")
    git(seeded, "push", "-q", "origin", "main")
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    _delivery.import_tree(str(remote), project)
    commit(workspace, {"projects/demo/added.txt": "new\n"}, "Add a file")
    report = _promote.deliver(project, "promote/demo", scan=clean)
    assert report.ok, report
    entry = git(remote, "ls-tree", "promote/demo", "vendor/sub")
    assert entry.startswith("160000 commit ") and entry.endswith("\tvendor/sub")
    assert git(remote, "diff", "--name-status", "main", "promote/demo") == "A\tadded.txt"


# -- 5. a closed or merged PR is not edited --------------------------------------------------------


def _gh(tmp_path: Path, monkeypatch, listed: str) -> Path:
    log = tmp_path / "gh.log"
    gh = tmp_path / "bin" / "gh"
    gh.parent.mkdir()
    gh.write_text(
        f'#!/bin/sh\necho "$@" >> "{log}"\n'
        f'[ "$2" = list ] && {{ echo "{listed}"; exit 0; }}\n'
        '[ "$2" = view ] && { echo https://example.invalid/pull/1; exit 0; }\n'
        "echo https://example.invalid/pull/9\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{gh.parent}{os.pathsep}{os.environ['PATH']}")
    return log


def test_a_closed_pull_request_for_the_branch_is_not_edited(world, tmp_path, monkeypatch):  # noqa: F811 -- `world` is the imported fixture
    log = _gh(tmp_path, monkeypatch, listed="")
    report = deliver(world)
    assert report.ok and report.url == "https://example.invalid/pull/9", report
    rows = log.read_text().splitlines()
    remote = world[0]
    listed = [
        row for row in rows if row.startswith(f"pr list --repo {remote} --head promote/demo --state open ")
    ]
    assert len(listed) == 1 and "--json url,headRepositoryOwner --jq map(select(" in listed[0]
    assert not any(row.startswith(("pr edit", "pr view")) for row in rows)
    assert any(row.startswith("pr create") for row in rows)


def test_an_open_pull_request_for_the_branch_is_edited(world, tmp_path, monkeypatch):  # noqa: F811 -- `world` is the imported fixture
    log = _gh(tmp_path, monkeypatch, listed="https://example.invalid/pull/4")
    report = deliver(world)
    assert report.ok and report.url == "https://example.invalid/pull/4", report
    rows = log.read_text().splitlines()
    assert any(row.startswith("pr edit https://example.invalid/pull/4") for row in rows)
    assert not any(row.startswith("pr create") for row in rows)


# -- 6. --no-dco on a target that asks for DCO says so ---------------------------------------------


@pytest.mark.parametrize("dry_run", [True, False])
def test_no_dco_on_a_dco_target_warns_that_its_check_will_fail(tmp_path, dry_run):
    remote, _ = seed_target(
        tmp_path,
        {"Makefile": MAKEFILE, "check.py": CHECK_OK, "n.txt": "a\n", ".github/dco.yml": "x: 1\n"},
    )
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    commit(workspace, {"projects/demo/n.txt": "b\n"})
    report = _promote.deliver(workspace / "projects/demo", "b", dco=False, scan=clean, dry_run=dry_run)
    assert report.ok, report
    assert (
        "warning: the target asks for DCO sign-off (.github/dco.yml); "
        "--no-dco left the commit unsigned, so its DCO check will fail"
    ) in report.message


def test_no_dco_on_a_target_without_dco_does_not_warn(world):  # noqa: F811 -- `world` is the imported fixture
    assert "warning" not in deliver(world, dco=False).message


def test_a_dco_target_signed_as_asked_does_not_warn(tmp_path):
    remote, _ = seed_target(
        tmp_path, {"Makefile": MAKEFILE, "check.py": CHECK_OK, "n.txt": "a\n", "DCO": "x\n"}
    )
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    commit(workspace, {"projects/demo/n.txt": "b\n"})
    report = _promote.deliver(workspace / "projects/demo", "b", scan=clean)
    assert report.ok and "warning" not in report.message and OPERATOR


# -- 7. the comment above `_SCAN_PUBLIC` ----------------------------------------------------------


def test_the_scan_public_comment_sits_above_scan_public():
    lines = Path(_promote.__file__).read_text().splitlines()
    comment = next(i for i, line in enumerate(lines) if line.startswith("#: `ops/scan_public.py`"))
    assert lines[comment + 1].startswith("_SCAN_PUBLIC")


def test_only_an_open_pull_request_from_the_pushed_to_owner_is_picked(tmp_path, monkeypatch):
    """Keep only the pushed-to owner's open PR: another fork's same-named branch is someone else's."""
    log = _gh(tmp_path, monkeypatch, listed="")
    _promote._open_pull_request(
        tmp_path, "https://github.com/acme/tgt.git", "me", "main", "feat", "title", "body", pushed_to="fork"
    )
    listed = [row for row in log.read_text().splitlines() if row.startswith("pr list")]
    assert listed == [
        "pr list --repo acme/tgt --head feat --state open --json url,headRepositoryOwner --jq "
        'map(select(.headRepositoryOwner.login == "me"))[0].url // empty'
    ]
