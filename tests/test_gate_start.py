"""The start tree: the newest merged upstream, else HEAD with the project's own commits undone."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gate_start, _gate_tests, _gates, _standards

pytestmark = pytest.mark.real_factory_gates

NAME = "p"
PROJECT = f"{NAME}@agent-factory.localhost"
UPSTREAM = "upstream@example.com"
OTHER = "other@example.com"
LINES = "".join(f"{n}\n" for n in range(1, 21))


def _git(repo: Path, *args: str, who: str = UPSTREAM) -> str:
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_AUTHOR_NAME": who,
        "GIT_AUTHOR_EMAIL": who,
        "GIT_COMMITTER_NAME": who,
        "GIT_COMMITTER_EMAIL": who,
    }
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=repo, env=env, capture_output=True, text=True
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], who: str = PROJECT) -> str:
    for name, text in files.items():
        if text is None:
            _git(repo, "rm", "-q", name)
        else:
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
            _git(repo, "add", name)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c", who=who)
    return _git(repo, "rev-parse", "HEAD")


def _upstream(repo: Path, files: dict[str, str | None]) -> str:
    """Commit on main as upstream and publish it as `origin/main`, then return to the branch we were on."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "main")
    tip = _commit(repo, files, UPSTREAM)
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)
    _git(repo, "checkout", "-q", branch)
    return tip


def _merge(
    repo: Path, other: str = "main", resolved: dict[str, str] | None = None, who: str = UPSTREAM
) -> None:
    """Merge `other`; a conflict is resolved with `resolved`."""
    merged = subprocess.run(
        ["git", "merge", "-q", "--no-commit", "--no-ff", other],
        cwd=repo,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": who,
            "GIT_AUTHOR_EMAIL": who,
            "GIT_COMMITTER_NAME": who,
            "GIT_COMMITTER_EMAIL": who,
        },
    )
    for name, text in (resolved or {}).items():
        (repo / name).write_text(text)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-edit", "--allow-empty", "-m", f"merge {other}", who=who)
    assert merged.returncode in (0, 1)
    assert len(_git(repo, "rev-list", "--parents", "-n1", "HEAD").split()) == 3, "the merge was not made"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    base = _commit(root, {"f": LINES, "gone": "y\n", "tail": "a\n"}, UPSTREAM)
    _git(root, "update-ref", "refs/remotes/origin/main", base)
    _git(root, "checkout", "-q", "-b", "proj")
    return root, base


def _projects(root: Path, base: str) -> list[str]:
    code, output = _gates._project_commits(root, NAME, base)
    assert not code
    return output.splitlines()


def _tree(path: Path) -> dict[str, str]:
    return {str(p.relative_to(path)): p.read_text() for p in sorted(path.rglob("*")) if p.is_file()}


def _main(root: Path) -> dict[str, str]:
    names = _git(root, "ls-tree", "-r", "--name-only", "main").splitlines()
    return {n: _git(root, "show", f"main:{n}") + "\n" for n in names}


def _changes(root: Path, base: str) -> tuple[_gate_start.StartTree, list[str]]:
    """Return the start tree's upstream and the net changed paths, as `_judge_project` computes them."""
    commits = _projects(root, base)
    with _gate_start._start_tree(root, base, commits) as built:
        _, touched = _gates._changed_paths(root, commits)
        if built.upstream:
            touched = sorted({*touched, *_gate_start.list_merge_paths(root, built.upstream)})
        return _gate_start.StartTree(Path(), built.upstream), _gates._net_changes(root, built.path, touched)


def _scope(root: Path, base: str, brief: str = "May edit: f") -> tuple[_gate_base.Status, str]:
    results: dict[str, tuple[_gate_base.Status, str]] = {}
    _gates._judge_project(
        root, NAME, base, brief, "", "DONE", [], lambda r, s, o: results.__setitem__(r, (s, o)), "root"
    )
    return results["scope"]


def test_without_a_merge_the_project_commits_are_undone(repo):
    root, base = repo
    ours = _commit(root, {"f": LINES.replace("\n10\n", "\nten\n"), "new": "z\n", "gone": None})
    empty = _commit(root, {})
    commits = _projects(root, base)
    assert set(commits) == {ours, empty}
    assert _gate_start.find_upstream_tip(root, base, commits) is None
    with _gate_start._start_tree(root, base, commits) as start:
        assert start.upstream is None
        assert _tree(start.path) == {"f": LINES, "gone": "y\n", "tail": "a\n"}


def test_adjacent_hunks_start_from_the_merged_main(repo):
    """The project edits line 10, main edits line 12: git merges them, the reverse patch no longer applies."""
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root)
    commits = _projects(root, base)
    patch = _git(root, "show", "--format=", commits[0])
    reverse = subprocess.run(
        ["git", "apply", "--reverse", "--check", "-"], cwd=root, input=patch, text=True, capture_output=True
    )
    assert reverse.returncode, "the scenario must be one the reverse patch cannot undo"
    with _gate_start._start_tree(root, base, commits) as start:
        assert _tree(start.path) == _main(root)
        assert start.upstream == _git(root, "rev-parse", "main")
    assert _changes(root, base)[1] == ["f"]
    status, output = _scope(root, base)
    assert status is _gate_base.Status.PASS, output


def test_engine_as_target_shape_with_later_project_commits(repo):
    root, base = repo
    path = "tests/test_engine_limit_handoff.py"
    _commit(root, {path: LINES})
    start_of_work = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--ff-only", start_of_work)
    _git(root, "checkout", "-q", "proj")
    base = start_of_work
    _commit(root, {path: LINES.replace("\n4\n", "\nfour\n")})
    _upstream(root, {path: LINES.replace("\n6\n", "\nsix\n")})
    _merge(root, who=PROJECT)
    _commit(root, {"src": "x = 1\n"})
    _commit(root, {path: _git(root, "show", f"HEAD:{path}") + "\nmore\n"})
    with _gate_start._start_tree(root, base, _projects(root, base)) as start:
        assert _tree(start.path) == _main(root)
    assert _changes(root, base)[1] == ["src", path]


def test_a_conflict_resolved_to_mains_line_is_not_a_change(repo):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nours\n")})
    theirs = LINES.replace("\n10\n", "\ntheirs\n")
    _upstream(root, {"f": theirs})
    _merge(root, resolved={"f": theirs})
    assert _changes(root, base)[1] == []


def test_a_conflict_resolved_to_a_third_text_is_a_change(repo):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nours\n")})
    _upstream(root, {"f": LINES.replace("\n10\n", "\ntheirs\n")})
    _merge(root, resolved={"f": LINES.replace("\n10\n", "\nboth\n")})
    assert _changes(root, base)[1] == ["f"]


def test_repeated_merges_start_from_the_newest_merged_main(repo):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n2\n", "\ntwo\n")})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root)
    _commit(root, {"f": _git(root, "show", "HEAD:f").replace("\n4\n", "\nfour\n") + "\n"})
    _upstream(root, {"gone": "y2\n"})
    _merge(root)
    _commit(root, {"tail": "b\n"})
    with _gate_start._start_tree(root, base, _projects(root, base)) as start:
        assert _tree(start.path) == _main(root)
        assert start.upstream == _git(root, "rev-parse", "main")
    assert _changes(root, base)[1] == ["f", "tail"]


def test_a_merge_of_the_projects_own_branch_starts_from_the_newest_merged_main(repo):
    """Two branches of the project merged main at different points, then were merged together."""
    root, base = repo
    _git(root, "checkout", "-q", "-b", "other-clone", base)
    _commit(root, {"tail": "b\n"})
    _git(root, "checkout", "-q", "proj")
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root)
    _git(root, "checkout", "-q", "other-clone")
    _commit(root, {"gone": "y\ny\n"})
    newest = _upstream(root, {"gone2": "g\n"})
    _merge(root)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "other-clone")
    with _gate_start._start_tree(root, base, _projects(root, base)) as start:
        assert start.upstream == newest
        assert _tree(start.path) == _main(root)
    assert _changes(root, base)[1] == ["f", "gone", "tail"]


def test_appends_at_the_end_on_both_sides_start_from_main(repo):
    root, base = repo
    _commit(root, {"tail": "a\nours\n"})
    _upstream(root, {"tail": "a\ntheirs\n"})
    _merge(root, resolved={"tail": "a\nours\ntheirs\n"})
    with _gate_start._start_tree(root, base, _projects(root, base)) as start:
        assert _tree(start.path) == _main(root)
    assert _changes(root, base)[1] == ["tail"]


def test_a_file_added_only_in_the_merge_commit_is_a_project_change(repo):
    """The merge's own content is the project's: an evil merge cannot hide a path from scope."""
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root, resolved={"f": LINES.replace("\n10\n", "\nten\n").replace("\n12\n", "\ntwelve\n")})
    _git(root, "commit", "-q", "--amend", "--no-edit")  # keep history shape; evil content follows
    (root / "evil.txt").write_text("x\n")
    _git(root, "add", "evil.txt")
    _git(root, "commit", "-q", "--amend", "--no-edit")
    assert len(_git(root, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert _changes(root, base)[1] == ["evil.txt", "f"]
    status, output = _scope(root, base)
    assert status is _gate_base.Status.FAIL
    assert "evil.txt" in output


def test_another_authors_commit_off_main_is_not_taken_as_upstream(repo):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _git(root, "checkout", "-q", "-b", "elsewhere", base)
    _commit(root, {"f": LINES.replace("\n16\n", "\nsixteen\n"), "sneaky": "project-like code\n"}, OTHER)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "elsewhere")
    commits = _projects(root, base)
    assert _gate_start.find_upstream_tip(root, base, commits) is None
    with _gate_start._start_tree(root, base, commits) as start:
        assert start.upstream is None
        assert "sneaky" in _tree(start.path)  # as before: HEAD with the project's commit undone
    # The same commit is upstream once `origin/main` has it.
    _git(root, "update-ref", "refs/remotes/origin/main", "elsewhere")
    assert _gate_start.find_upstream_tip(root, base, commits) == _git(root, "rev-parse", "elsewhere")


def test_another_authors_commit_built_on_project_work_is_not_upstream(repo):
    """OTHER committed on top of the merged project branch: nothing above the project's work is upstream,
    so no tip is trusted, and the out-of-scope path OTHER wrote is not blamed on the project."""
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"tail": "a\nb\n"})
    _merge(root)
    _commit(root, {"gone": "operator fix\n"}, OTHER)
    commits = _projects(root, base)
    assert _gate_start.find_upstream_tip(root, base, commits) is None
    with _gate_start._start_tree(root, base, commits) as start:
        assert start.upstream is None
        assert (start.path / "gone").read_text() == "operator fix\n"
    assert _changes(root, base)[1] == ["f"]
    status, output = _scope(root, base)
    assert status is _gate_base.Status.PASS, output


def test_a_tip_that_does_not_descend_from_base_is_not_upstream(repo):
    """The merged OTHER branch forked before the project's base: nothing in it is a start the project
    could have begun from, so its out-of-scope content is not blamed on the project."""
    root, fixture_base = repo
    base = _upstream(root, {"tail": "a\nb\n"})
    _git(root, "update-ref", "-d", "refs/remotes/origin/main")
    _git(root, "merge", "-q", "--ff-only", "main")
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _git(root, "checkout", "-q", "-b", "old", fixture_base)
    _commit(root, {"gone": "old fix\n"}, OTHER)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "old")
    commits = _projects(root, base)
    assert _gate_start.find_upstream_tip(root, base, commits) is None
    with _gate_start._start_tree(root, base, commits) as start:
        assert start.upstream is None
        assert (start.path / "gone").read_text() == "old fix\n"
    assert _changes(root, base)[1] == ["f"]
    status, output = _scope(root, base)
    assert status is _gate_base.Status.PASS, output


def test_two_unrelated_upstream_heads_fall_back_to_undoing_the_commits(repo):
    root, base = repo
    ours = _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"gone": "y2\n"})
    _merge(root)
    _git(root, "checkout", "-q", "-b", "side", base)
    _commit(root, {"tail": "a\nb\n"}, OTHER)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "side")
    commits = _projects(root, base)
    assert commits == [ours]
    assert _gate_start.find_upstream_tip(root, base, commits) is None
    with _gate_start._start_tree(root, base, commits) as start:
        assert _tree(start.path)["f"] == LINES


def _unreadable(repo):
    """A history with no upstream tip and a project commit that cannot be undone."""
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _commit(root, {"f": _git(root, "show", "HEAD:f").replace("\nten\n", "\nTEN\n")}, OTHER)
    return root, base


def test_a_start_tree_that_cannot_be_built_is_a_harness_fault_not_a_scope_failure(repo):
    root, base = _unreadable(repo)
    assert _gate_start.find_upstream_tip(root, base, _projects(root, base)) is None
    status, output = _scope(root, base)
    assert status is _gate_base.Status.HARNESS_FAULT
    assert "the factory's, not the project's" in output and "cannot reverse project changes" in output
    assert not status.blocks


def test_fail_to_pass_reports_a_start_tree_it_cannot_build_as_a_harness_fault(repo):
    root, base = _unreadable(repo)
    code, output = _gate_tests.fail_to_pass(
        root, NAME, base, ["f"], "", "", "DONE", _projects(root, base), None, "root"
    )
    assert code is _gate_base.Status.HARNESS_FAULT
    assert "cannot compute the project's changes" in output


def test_the_gate_records_fail_to_pass_as_a_harness_fault_when_the_start_tree_cannot_be_built(repo):
    root, base = _unreadable(repo)
    results: dict[str, tuple[_gate_base.Status, str]] = {}
    _gates._judge_project(
        root,
        NAME,
        base,
        "May edit: f",
        "",
        "DONE",
        [],
        lambda r, s, o: results.__setitem__(r, (s, o)),
        "root",
        fail_to_pass_rules=["ENG-X"],
    )
    status, output = results[_standards.FAIL_TO_PASS]
    assert status is _gate_base.Status.HARNESS_FAULT
    assert output.startswith("[ENG-X] ") and "cannot compute the project's changes" in output
    assert results["scope"][0] is _gate_base.Status.HARNESS_FAULT


@pytest.mark.parametrize("main_ref", ["absent", "forged"])
def test_an_identical_change_by_another_author_does_not_hide_the_projects_edit(repo, main_ref):
    """Another author's side commit makes the project's exact edit and is merged in: unverified (no
    `origin/main`) or with `origin/main` pointed at it, it can become the upstream tip, but the path the
    project wrote stays the project's change, as it was when the project's commits were undone."""
    root, base = repo
    _commit(root, {"g": "weakened\n"})
    _git(root, "checkout", "-q", "-b", "side", base)
    _commit(root, {"g": "weakened\n"}, OTHER)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "side")
    if main_ref == "absent":
        _git(root, "update-ref", "-d", "refs/remotes/origin/main")
    else:
        _git(root, "update-ref", "refs/remotes/origin/main", "side")
    status, output = _scope(root, base)
    assert status is _gate_base.Status.FAIL
    assert "g" in output.split("May edit: paths: ")[1]


def test_a_change_the_project_made_and_undid_is_not_counted_against_an_upstream_tip(repo):
    root, base = repo
    _commit(root, {"g": "temporary\n"})
    _commit(root, {"g": None})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root)
    built, changes = _changes(root, base)
    assert built.upstream
    assert changes == []
    assert _scope(root, base)[0] is _gate_base.Status.PASS


@pytest.mark.parametrize("main_ref", ["absent", "forged"])
def test_an_identical_deletion_by_another_author_does_not_hide_the_projects_deletion(repo, main_ref):
    root, base = repo
    _commit(root, {"gone": None})
    _git(root, "checkout", "-q", "-b", "side", base)
    _commit(root, {"gone": None}, OTHER)
    _git(root, "checkout", "-q", "proj")
    _merge(root, "side")
    if main_ref == "absent":
        _git(root, "update-ref", "-d", "refs/remotes/origin/main")
    else:
        _git(root, "update-ref", "refs/remotes/origin/main", "side")
    status, output = _scope(root, base)
    assert status is _gate_base.Status.FAIL
    assert "gone" in output.split("May edit: paths: ")[1]


def _judged(root: Path, base: str) -> dict[str, tuple[_gate_base.Status, str]]:
    results: dict[str, tuple[_gate_base.Status, str]] = {}
    _gates._judge_project(
        root,
        NAME,
        base,
        "May edit: f",
        "",
        "DONE",
        [],
        lambda r, s, o: results.__setitem__(r, (s, o)),
        "root",
        fail_to_pass_rules=["ENG-X"],
    )
    return results


def _assert_only_a_harness_fault(results: dict[str, tuple[_gate_base.Status, str]]) -> None:
    for rule in ("scope", _standards.FAIL_TO_PASS):
        status, output = results[rule]
        assert status is _gate_base.Status.HARNESS_FAULT, (rule, output)
        assert "harness fault" in output and "the factory's, not the project's" in output


def test_a_failing_diff_tree_is_a_harness_fault(repo, monkeypatch):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    real = _gate_base._run

    def run(args, cwd, *rest, **kwargs):
        if "diff-tree" in args:
            return 128, "fatal: bad object"
        return real(args, cwd, *rest, **kwargs)

    monkeypatch.setattr(_gate_base, "_run", run)
    results = _judged(root, base)
    _assert_only_a_harness_fault(results)
    assert "bad object" in results["scope"][1]


@pytest.mark.parametrize("verb", ["merge-base", "apply", "archive"])
def test_a_git_timeout_in_the_start_tree_is_a_harness_fault(repo, monkeypatch, verb):
    root, base = repo
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    _upstream(root, {"f": LINES.replace("\n12\n", "\ntwelve\n")})
    _merge(root)
    if verb == "apply":  # only the undo path applies patches: make the upstream untrusted
        _git(root, "update-ref", "refs/remotes/origin/main", base)
    real = subprocess.run

    def run(args, *rest, **kwargs):
        if verb in args:
            raise subprocess.TimeoutExpired(args, 60)
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    _assert_only_a_harness_fault(_judged(root, base))


def test_a_corrupt_object_is_a_harness_fault(repo):
    root, base = repo
    ours = _commit(root, {"d/x": "x\n", "f": LINES.replace("\n10\n", "\nten\n")})
    _commit(root, {"d/x": None})
    tree = _git(root, "rev-parse", f"{ours}:d")
    obj = root / ".git" / "objects" / tree[:2] / tree[2:]
    obj.chmod(0o644)
    obj.write_bytes(b"garbage")
    results = _judged(root, base)
    _assert_only_a_harness_fault(results)


def test_a_failing_commit_listing_is_a_harness_fault(repo, monkeypatch):
    root, base = repo
    monkeypatch.setattr(_gates, "_project_commits", lambda *args: (128, "bad revision"))
    results = _judged(root, base)
    _assert_only_a_harness_fault(results)
    assert "bad revision" in results["scope"][1]


def test_the_start_tree_directory_is_removed_when_the_gate_is_interrupted(repo, tmp_path, monkeypatch):
    root, base = repo
    scratch = tmp_path / "scratch"  # the shared temp dir holds other workers' start trees
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    with pytest.raises(RuntimeError), _gate_start._start_tree(root, base, _projects(root, base)):
        assert list(scratch.glob("factory-start-*"))
        raise RuntimeError("interrupted")
    assert not list(scratch.glob("factory-start-*"))


BASE = "def base():\n    return 0\n"
OURS = "\n\ndef ours():\n    return 1\n"
THEIRS = "\n\ndef theirs():\n    return 2\n"
ASSIGNS = "".join(f"line{n} = {n}\n" for n in range(1, 11))


@pytest.fixture
def modules(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    base = _commit(root, {"mod.py": BASE, "lines.py": ASSIGNS, "gone.py": "y = 1\n"}, UPSTREAM)
    _git(root, "checkout", "-q", "-b", "project")
    return root, base


def _merge_main(root: Path, theirs: dict[str, str | None], resolved: dict[str, str]) -> None:
    """Commit `theirs` on main, merge it into the project branch and resolve with `resolved`."""
    _upstream(root, theirs)
    _merge(root, resolved=resolved)


def test_without_a_merge_the_project_commits_are_reversed(modules):
    root, base = modules
    ours = _commit(root, {"mod.py": BASE + OURS, "new.py": "z = 1\n", "gone.py": None})
    empty = _commit(root, {})
    with _gate_start._start_tree(root, base, [empty, ours]) as start:
        assert _tree(start.path) == {"gone.py": "y = 1\n", "lines.py": ASSIGNS, "mod.py": BASE}


def test_a_merge_that_appends_after_a_project_hunk_at_the_end_still_reverses_it(modules):
    """The project appended at the end of a file, main appended too, and the merge kept both, ours first.

    The start keeps the upstream lines and the project's other changes, later ones too, undone.
    """
    root, base = modules
    ours = _commit(root, {"mod.py": BASE + OURS, "lines.py": ASSIGNS + "extra = 1\n"})
    _merge_main(root, {"mod.py": BASE + THEIRS}, {"mod.py": BASE + OURS + THEIRS})
    late = _commit(root, {"lines.py": ASSIGNS + "extra = 2\n"})
    with _gate_start._start_tree(root, base, [late, ours]) as start:
        assert _tree(start.path) == {"gone.py": "y = 1\n", "lines.py": ASSIGNS, "mod.py": BASE + THEIRS}


def test_a_merge_that_joined_upstream_lines_to_a_project_hunk_starts_from_the_merged_upstream(modules):
    """main edited the line next to the project's: only the merge's resolution separates them, so the
    start is the merged upstream, without the project's later commits either."""
    root, base = modules
    ours = _commit(root, {"lines.py": ASSIGNS.replace("line5 = 5", "line5 = 50"), "new.py": "z\n"})
    upstream = ASSIGNS.replace("line4 = 4", "line4 = 40")
    _merge_main(root, {"lines.py": upstream}, {"lines.py": upstream.replace("line5 = 5", "line5 = 50")})
    late = _commit(root, {"mod.py": BASE + OURS})
    with _gate_start._start_tree(root, base, [late, ours]) as start:
        assert _tree(start.path) == _main(root)
        assert (start.path / "lines.py").read_text() == upstream


def test_the_newest_merge_on_the_branch_gives_the_start(modules):
    root, base = modules
    ours = _commit(root, {"lines.py": ASSIGNS.replace("line5 = 5", "line5 = 50")})
    _merge_main(root, {"gone.py": "y = 2\n"}, {})
    upstream = ASSIGNS.replace("line4 = 4", "line4 = 40")
    _merge_main(root, {"lines.py": upstream}, {"lines.py": upstream.replace("line5 = 5", "line5 = 50")})
    with _gate_start._start_tree(root, base, [ours]) as start:
        assert _tree(start.path) == _main(root)


def test_a_project_commit_that_does_not_reverse_without_a_merge_is_still_refused(modules):
    """With no merge there is no upstream to start from: a later edit to the project's line is reported."""
    root, base = modules
    ours = _commit(root, {"lines.py": ASSIGNS.replace("line5 = 5", "line5 = 50")})
    _commit(root, {"lines.py": ASSIGNS.replace("line5 = 5", "line5 = 51")}, OTHER)
    with pytest.raises(_gate_start._GateError, match="^cannot reverse project changes: "):
        with _gate_start._start_tree(root, base, [ours]):
            pass


def test_a_hunk_at_the_end_that_no_longer_ends_the_file_is_undone_without_trailing_context(modules):
    """No upstream tip to start from, and another author appended after the project's last line: the
    reverse patch no longer anchors to the end of the file, and the retry places it by its own lines."""
    root, base = modules
    ours = _commit(root, {"mod.py": BASE + OURS})
    _commit(root, {"mod.py": BASE + OURS + THEIRS}, OTHER)
    assert _gate_start.find_upstream_tip(root, base, [ours]) is None
    patch = _git(root, "show", "--format=", ours) + "\n"
    strict = subprocess.run(
        ["git", "apply", "--reverse", "--check", "-"], cwd=root, input=patch, text=True, capture_output=True
    )
    assert strict.returncode, "the scenario must be one only the retry can undo"
    with _gate_start._start_tree(root, base, [ours]) as start:
        assert start.upstream is None
        assert _tree(start.path) == {"gone.py": "y = 1\n", "lines.py": ASSIGNS, "mod.py": BASE + THEIRS}


def test_a_resumed_project_is_judged_on_this_runs_changes_only(repo):
    """The base is an earlier branch head, not on main; mandated merges of main follow, one adjacent to a
    project hunk. The start is the base with upstream merged in, so the project is judged on this run's
    edits."""
    root, _ = repo
    earlier = LINES.replace("\n2\n", "\ntwo\n")
    _commit(root, {"f": earlier, "resumed": "earlier run\n"})
    base = _git(root, "rev-parse", "HEAD")
    _upstream(root, {"tail": "b\n"})
    _merge(root, who=PROJECT)
    _commit(root, {"f": earlier.replace("\n10\n", "\nten\n")})
    tip = _upstream(root, {"f": LINES.replace("\n11\n", "\neleven\n")})
    ours = earlier.replace("\n10\n", "\nten\n").replace("\n11\n", "\neleven\n")
    _merge(root, resolved={"f": ours}, who=PROJECT)
    commits = _projects(root, base)
    assert _gate_start.find_upstream_tip(root, base, commits) == tip
    with _gate_start._start_tree(root, base, commits) as start:
        assert start.upstream == tip
        expected = {**_main(root), "f": earlier.replace("\n11\n", "\neleven\n"), "resumed": "earlier run\n"}
        assert _tree(start.path) == expected
    assert _changes(root, base)[1] == ["f"]
    assert _scope(root, base, "May edit: f")[0] is _gate_base.Status.PASS
    status, output = _scope(root, base, "May edit: nothing")
    assert status is _gate_base.Status.FAIL
    assert "f" in output and "resumed" not in output


def test_a_resumed_project_with_a_conflicting_mandated_merge_passes_in_scope_and_fails_out_of_scope(repo):
    """The review's reproduction: the earlier run's, this run's and upstream's lines all meet in `f`."""
    root, _ = repo
    _commit(root, {"f": LINES.replace("2\n", "2 earlier\n")})
    base = _git(root, "rev-parse", "HEAD")
    _upstream(root, {"tail": "b\n"})
    _merge(root, who=PROJECT)
    earlier = LINES.replace("2\n", "2 earlier\n")
    _commit(root, {"f": earlier.replace("10\n", "10 ours\n")})
    _upstream(root, {"f": LINES.replace("11\n", "11 theirs\n")})
    _merge(
        root, resolved={"f": earlier.replace("10\n", "10 ours\n").replace("11\n", "11 theirs\n")}, who=PROJECT
    )
    assert _scope(root, base, "May edit: f") == (_gate_base.Status.PASS, "within the project")
    status, output = _scope(root, base, "May edit: nothing")
    assert status is _gate_base.Status.FAIL and output.endswith(": f"), output


def test_a_resumed_base_conflicting_with_upstream_is_judged_on_the_resolution(repo):
    """Lines the earlier run and upstream both changed: resolved to upstream's text is no change of this
    run, resolved to a third text is one; neither is a harness fault."""
    root, _ = repo
    earlier = LINES.replace("\n2\n", "\ntwo\n")
    _commit(root, {"f": earlier})
    base = _git(root, "rev-parse", "HEAD")
    _upstream(root, {"f": LINES.replace("\n2\n", "\nzwei\n")})
    _merge(root, resolved={"f": LINES.replace("\n2\n", "\nzwei\n")}, who=PROJECT)
    _commit(root, {"new": "z\n"})
    assert _changes(root, base)[1] == ["new"]
    assert _scope(root, base, "May edit: new")[0] is _gate_base.Status.PASS
    _commit(root, {"f": LINES.replace("\n2\n", "\ndeux\n")})
    assert _changes(root, base)[1] == ["f", "new"]
    status, output = _scope(root, base, "May edit: new")
    assert status is _gate_base.Status.FAIL and output.endswith(": f"), output


def test_a_resumed_base_whose_file_upstream_deleted_is_judged_on_the_resolution(repo):
    """Upstream deleted a file the earlier run kept; keeping it in the merge is this run's change."""
    root, _ = repo
    _commit(root, {"gone": "y\nz\n"})
    base = _git(root, "rev-parse", "HEAD")
    _upstream(root, {"gone": None})
    _merge(root, resolved={"gone": "y\nz\n"}, who=PROJECT)
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    assert _changes(root, base)[1] == ["f", "gone"]
    status, output = _scope(root, base)
    assert status is _gate_base.Status.FAIL and "gone" in output, output


def test_a_mandated_merge_before_the_first_commit_is_not_an_upstream_head(repo):
    """The branch merges main before its first commit: that merge's first parent is `base`, and it is the
    project's, so the newest main commit stays the only upstream head."""
    root, base = repo
    _upstream(root, {"tail": "b\n"})
    _merge(root, who=PROJECT)
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    tip = _upstream(root, {"f": LINES.replace("\n11\n", "\neleven\n")})
    _merge(
        root, resolved={"f": LINES.replace("\n10\n", "\nten\n").replace("\n11\n", "\neleven\n")}, who=PROJECT
    )
    assert _gate_start.find_upstream_tip(root, base, _projects(root, base)) == tip
    assert _scope(root, base) == (_gate_base.Status.PASS, "within the project")


def test_an_upstream_merge_on_top_of_base_is_upstream(repo):
    """A pull request merged on main right after `base` has `base` as its first parent too; main contains
    it, so it is upstream and the newest main commit is still the tip."""
    root, base = repo
    _git(root, "checkout", "-q", "-b", "pr", "main")
    _commit(root, {"tail": "b\n"}, UPSTREAM)
    _git(root, "checkout", "-q", "main")
    _merge(root, "pr")
    _git(root, "update-ref", "refs/remotes/origin/main", _git(root, "rev-parse", "HEAD"))
    _git(root, "checkout", "-q", "proj")
    _commit(root, {"f": LINES.replace("\n10\n", "\nten\n")})
    tip = _upstream(root, {"f": LINES.replace("\n11\n", "\neleven\n")})
    _merge(
        root, resolved={"f": LINES.replace("\n10\n", "\nten\n").replace("\n11\n", "\neleven\n")}, who=PROJECT
    )
    assert _gate_start.find_upstream_tip(root, base, _projects(root, base)) == tip
    assert _scope(root, base) == (_gate_base.Status.PASS, "within the project")
    status, output = _scope(root, base, "May edit: nothing")
    assert status is _gate_base.Status.FAIL and output.endswith(": f"), output
