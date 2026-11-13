"""The start tree: HEAD with the project's own commits reversed, after a merge from main too."""

import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_start

BASE = "def base():\n    return 0\n"
OURS = "\n\ndef ours():\n    return 1\n"
THEIRS = "\n\ndef theirs():\n    return 2\n"
LINES = "".join(f"line{n} = {n}\n" for n in range(1, 11))


IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@x.localhost", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str | None], message: str) -> str:
    for name, text in files.items():
        if text is None:
            _git(repo, "rm", "-q", name)
        else:
            (repo / name).write_text(text)
            _git(repo, "add", name)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    base = _commit(root, {"mod.py": BASE, "lines.py": LINES, "gone.py": "y = 1\n"}, "base")
    _git(root, "checkout", "-q", "-b", "project")
    return root, base


def _merge_main(root: Path, theirs: dict[str, str | None], resolved: dict[str, str]) -> None:
    """Commit `theirs` on main, merge it into the project branch and resolve with `resolved`."""
    _git(root, "checkout", "-q", "main")
    _commit(root, theirs, "main")
    _git(root, "checkout", "-q", "project")
    # A conflict stops the merge before its commit, which the resolution below makes.
    merge = ["git", *IDENTITY, "merge", "-q", "--no-commit", "--no-ff", "main"]
    subprocess.run(merge, cwd=root, capture_output=True, check=False)
    for name, text in resolved.items():
        (root / name).write_text(text)
        _git(root, "add", name)
    _git(root, "commit", "-q", "-m", "merge main")
    assert len(_git(root, "rev-list", "--parents", "-n1", "HEAD").split()) == 3, "the merge was not made"


def _main_tree(root: Path) -> dict[str, str]:
    names = _git(root, "ls-tree", "-r", "--name-only", "main").splitlines()
    return {name: _git(root, "show", f"main:{name}") + "\n" for name in names}


def _tree(start: Path) -> dict[str, str]:
    return {str(p.relative_to(start)): p.read_text() for p in sorted(start.rglob("*")) if p.is_file()}


def test_without_a_merge_the_project_commits_are_reversed(repo):
    root, base = repo
    ours = _commit(root, {"mod.py": BASE + OURS, "new.py": "z = 1\n", "gone.py": None}, "ours")
    empty = _commit(root, {}, "nothing")
    with _gate_start._start_tree(root, base, [empty, ours]) as start:
        assert _tree(start) == {"gone.py": "y = 1\n", "lines.py": LINES, "mod.py": BASE}


def test_a_merge_that_appends_after_a_project_hunk_at_the_end_still_reverses_it(repo):
    """The project appended at the end of a file, main appended too, and the merge kept both, ours first.

    The reverse patch no longer ends the file; it is still placed by its own lines, and the start keeps
    the upstream lines and the project's other changes undone.
    """
    root, base = repo
    ours = _commit(root, {"mod.py": BASE + OURS, "lines.py": LINES + "extra = 1\n"}, "ours")
    _merge_main(root, {"mod.py": BASE + THEIRS}, {"mod.py": BASE + OURS + THEIRS})
    late = _commit(root, {"lines.py": LINES + "extra = 2\n"}, "later")
    with _gate_start._start_tree(root, base, [late, ours]) as start:
        assert _tree(start) == {"gone.py": "y = 1\n", "lines.py": LINES, "mod.py": BASE + THEIRS}


def test_a_merge_that_joined_upstream_lines_to_a_project_hunk_starts_from_the_merged_upstream(repo):
    """main edited the line next to the project's: only the merge's resolution separates them, so the
    start is the upstream side of the newest merge, without the project's later commits either."""
    root, base = repo
    ours = _commit(root, {"lines.py": LINES.replace("line5 = 5", "line5 = 50"), "new.py": "z\n"}, "ours")
    upstream = LINES.replace("line4 = 4", "line4 = 40")
    _merge_main(root, {"lines.py": upstream}, {"lines.py": upstream.replace("line5 = 5", "line5 = 50")})
    late = _commit(root, {"mod.py": BASE + OURS}, "later")
    with _gate_start._start_tree(root, base, [late, ours]) as start:
        assert _tree(start) == _main_tree(root)
        assert (start / "lines.py").read_text() == upstream


def test_the_newest_merge_on_the_branch_gives_the_start(repo):
    root, base = repo
    ours = _commit(root, {"lines.py": LINES.replace("line5 = 5", "line5 = 50")}, "ours")
    _merge_main(root, {"gone.py": "y = 2\n"}, {})
    upstream = LINES.replace("line4 = 4", "line4 = 40")
    _merge_main(root, {"lines.py": upstream}, {"lines.py": upstream.replace("line5 = 5", "line5 = 50")})
    with _gate_start._start_tree(root, base, [ours]) as start:
        assert _tree(start) == _main_tree(root)


def test_a_project_commit_that_does_not_reverse_without_a_merge_is_still_refused(repo):
    """With no merge there is no upstream to start from: a later edit to the project's line is reported."""
    root, base = repo
    ours = _commit(root, {"lines.py": LINES.replace("line5 = 5", "line5 = 50")}, "ours")
    _commit(root, {"lines.py": LINES.replace("line5 = 5", "line5 = 51")}, "someone else")
    with pytest.raises(_gate_start._GateError, match="^cannot reverse project changes: "):
        with _gate_start._start_tree(root, base, [ours]):
            pass
