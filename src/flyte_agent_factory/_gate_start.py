"""The start tree a project is judged against, and the finding comparison against its results."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from . import _gate_base, _secret_env


class _GateError(Exception):
    """The start tree could not be built; the message says why."""


def _run_git(args: list[str], cwd: Path, *, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run git and return its result, whatever the exit code; a timeout is a `_GateError`."""
    try:
        return subprocess.run(
            _secret_env.git_argv(*args),
            cwd=cwd,
            env=_secret_env.child_env(),
            input=stdin,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _GateError(f"git {args[0]} timed out after {exc.timeout:g} s") from exc


def _git_bytes(args: list[str], cwd: Path) -> bytes:
    result = _run_git(args, cwd)
    if result.returncode:
        raise _GateError(f"git {args[0]} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


#: Where the mandated `git merge origin/main` takes upstream from; trusted only if the workspace has it.
_MAIN_REF = "refs/remotes/origin/main"


@dataclasses.dataclass(frozen=True)
class StartTree:
    """The tree a project is judged against.

    `upstream` is the upstream tip HEAD is compared against, or None when the tree was built by undoing
    the project's commits on HEAD. The tree is `base` with that tip merged in, which is the tip's own tree
    when `base` is on main.
    """

    path: Path
    upstream: str | None = None


def _is_ancestor(workspace: Path, ancestor: str, descendant: str) -> bool:
    # Exit 1 is the answer "no"; _git_bytes would call it a failure.
    return _run_git(["merge-base", "--is-ancestor", ancestor, descendant], workspace).returncode == 0


def find_upstream_tip(workspace: Path, base: str, commits: list[str]) -> str | None:
    """Return the newest upstream commit merged into HEAD, or None when it cannot be told.

    A project commit and the mandated merge of main can touch adjacent lines: git merges them, but the
    project commit's reverse patch no longer applies to the merged file. The merged upstream tree itself
    does not have that problem. A commit is "tainted" when it is a project commit or has a tainted parent;
    the rest is upstream. Everything HEAD differs from the upstream tip by, merge-commit content included,
    is the project's change.

    None means "not a history we can read": two unrelated upstream heads, another author's commit built on
    project work, a tip that `origin/main` does not contain (a merge's second parent is the project's
    choice, so it is not trusted), or, when `origin/main` is absent, one that does not descend from `base`.
    The caller falls back to undoing the project's commits.

    A merge whose first parent is `base` and that `origin/main` does not contain is the branch's own
    mandated merge, so it is tainted too. A resumed project's base is an earlier branch head that nothing
    on main descends from; `origin/main` containing the tip is the evidence there.
    """
    listing = _git_bytes(["rev-list", "--parents", "--topo-order", "--reverse", f"{base}..HEAD"], workspace)
    parents: dict[str, list[str]] = {}
    for line in listing.decode().splitlines():
        sha, *rest = line.split()
        parents[sha] = rest
    project = set(commits)
    start = _git_bytes(["rev-parse", "--verify", f"{base}^{{commit}}"], workspace).decode().strip()
    has_main = not _run_git(["rev-parse", "--verify", "--quiet", _MAIN_REF], workspace).returncode
    tainted: set[str] = set()
    for sha, above in parents.items():  # oldest first: parents are decided before their children
        if (
            sha in project
            or any(parent in tainted for parent in above)
            or _is_own_merge(workspace, start, sha, above, has_main=has_main)
        ):
            tainted.add(sha)
            if sha not in project and len(above) == 1:
                return None
    upstream = [sha for sha in parents if sha not in tainted]
    heads = [sha for sha in upstream if not any(sha in parents[other] for other in upstream)]
    if len(heads) != 1:
        return None
    tip = heads[0]
    if has_main:
        # origin/main vouches for the tip wherever it forked: a resumed project's base is not on main.
        return tip if _is_ancestor(workspace, tip, _MAIN_REF) else None
    # Without origin/main the only evidence a tip is upstream is that it continues `base`.
    return tip if _is_ancestor(workspace, base, tip) else None


def _is_own_merge(workspace: Path, start: str, sha: str, above: list[str], *, has_main: bool) -> bool:
    """Tell whether `sha` is the branch's mandated merge on top of `base`, not an upstream merge."""
    if len(above) < 2 or above[0] != start:
        return False
    # A merge on main (a pull request merged right after `base`) also has `base` as its first parent.
    return not (has_main and _is_ancestor(workspace, sha, _MAIN_REF))


@contextlib.contextmanager
def _start_tree(workspace: Path, base: str, commits: list[str] | None) -> Iterator[StartTree]:
    """Where the project started: `base` with the newest upstream merged in, else HEAD, its commits undone.

    The project is judged by its net change. Judging each commit against its parent blocked forever
    on a skip that was added and later removed, and pushed history cannot be rewritten. Without
    `commits` (no attribution available) the start is simply `base`.
    """
    tip = None if commits is None else find_upstream_tip(workspace, base, commits)
    with tempfile.TemporaryDirectory(prefix="factory-start-") as directory:
        checkout = Path(directory)
        if tip:
            tree, conflicted = _merge_tree(workspace, base, tip)
            _extract(workspace, tree, checkout)
            _take_upstream(workspace, checkout, tip, conflicted)
        else:
            _extract(workspace, "HEAD" if commits is not None else base, checkout)
            _reverse(workspace, checkout, commits or [])
        yield StartTree(checkout, tip)


def _merge_tree(workspace: Path, base: str, tip: str) -> tuple[str, list[str]]:
    """Return the tree of `base` with the upstream `tip` merged in, and the paths the two conflict on.

    When `base` is on `tip`'s history (a project begun from main) that is `tip`'s own tree. A resumed
    project's base is an earlier branch head: its work stays in the start, so only this run's is judged.
    """
    result = _run_git(
        ["merge-tree", "--write-tree", "--no-messages", "--name-only", "-z", base, tip], workspace
    )
    if result.returncode not in (0, 1):  # 1 is "merged with conflicts"
        raise _GateError(f"git merge-tree failed: {result.stderr.decode(errors='replace')}")
    tree, *conflicted = result.stdout.split(b"\0")  # with -z the tree id is NUL-terminated too
    return tree.decode(), [path.decode(errors="replace") for path in conflicted if path]


def _take_upstream(workspace: Path, checkout: Path, tip: str, paths: list[str]) -> None:
    """Give the conflicted `paths` upstream's text, as a resolution to main's line is not a change."""
    kept = [path for path in paths if _read_blob_id(workspace, tip, path) is not None]
    for path in set(paths) - set(kept):
        (checkout / path).unlink(missing_ok=True)
    if kept:
        _extract(workspace, tip, checkout, kept)


def _extract(workspace: Path, ref: str, checkout: Path, paths: list[str] | None = None) -> None:
    archive = _git_bytes(["archive", ref, "--", *(paths or [])], workspace)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(checkout, filter="data")


def _reverse(workspace: Path, checkout: Path, commits: list[str]) -> None:
    # Newest first, so each patch meets the lines it produced. Upstream code stays identical
    # on both sides, even after the mandated rebase and push.
    for commit in commits:
        patch = _git_bytes(
            ["show", *_secret_env.GIT_NO_DRIVERS, "--format=", "--binary", "--no-renames", commit],
            workspace,
        )
        if not patch:
            continue
        # A merge that kept upstream lines after a hunk with no trailing context breaks git's anchoring
        # of that hunk to the end of the file; without it (`--unidiff-zero`) the hunk's own lines and
        # leading context still place it.
        applied = _apply_reverse(checkout, patch)
        if applied.returncode and _apply_reverse(checkout, patch, "--unidiff-zero").returncode:
            raise _GateError("cannot reverse project changes: " + applied.stderr.decode(errors="replace"))


def _apply_reverse(checkout: Path, patch: bytes, *options: str) -> subprocess.CompletedProcess[bytes]:
    return _run_git(["apply", "--reverse", "--binary", *options], checkout, stdin=patch)


def describe_harness_fault(reason: str) -> str:
    """Return the message for a start tree the factory could not build."""
    return f"harness fault (the factory's, not the project's): cannot compute the project's changes: {reason}"


def list_changed_paths(workspace: Path, commits: list[str]) -> tuple[int, list[str]]:
    """Return the exit code and the sorted paths `commits` changed; on a failure, its output as the list."""
    paths: set[str] = set()
    for commit in commits:
        code, output = _gate_base._run(
            [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                "-z",
                commit,
            ],
            workspace,
        )
        if code:
            return code, [output]
        paths.update(p for p in output.split("\0") if p)
    return 0, sorted(paths)


def list_merge_paths(workspace: Path, upstream: str) -> list[str]:
    """Return the paths HEAD differs from `upstream` by: the project's, merge-commit content included."""
    diff = ["diff", *_secret_env.GIT_NO_DRIVERS, "--name-only", "--no-renames", "-z", upstream, "HEAD"]
    return [p for p in _git_bytes(diff, workspace).decode(errors="replace").split("\0") if p]


def add_merge_paths(workspace: Path, built: StartTree, touched: list[str]) -> list[str]:
    """Add what a merge commit changed (a conflict resolution) to the project's touched paths."""
    if not built.upstream:
        return touched
    return sorted({*touched, *list_merge_paths(workspace, built.upstream)})


def _read_blob_id(workspace: Path, rev: str, path: str) -> str | None:
    result = _run_git(["rev-parse", "--verify", "--quiet", f"{rev}:{path}"], workspace)
    return result.stdout.decode().strip() if not result.returncode else None


def find_authored_paths(
    workspace: Path, built: StartTree, base: str, commits: list[str], touched: list[str]
) -> list[str]:
    """Return the touched paths whose HEAD content a project commit wrote and the upstream tip changed too.

    Against an upstream tip such a path looks unchanged, though the project wrote it: another author's
    commit with the identical change, merged in, would otherwise hide it. A path the project changed and
    then undid (HEAD as `base`), or one where a merge kept upstream's text over the project's, is not one.
    A deletion counts too.
    """
    if not built.upstream:
        return []
    found = []
    for path in touched:
        head = _read_blob_id(workspace, "HEAD", path)
        if head == _read_blob_id(workspace, base, path) or head != _read_blob_id(
            workspace, built.upstream, path
        ):
            continue
        # Introduced: the commit left the path as HEAD has it (absent, for a deletion) and its parent did
        # not, so a state upstream reached and a later project commit only inherited is not the project's.
        # Project commits are non-merges, so `commit^` is the one parent.
        if any(
            _read_blob_id(workspace, c, path) == head != _read_blob_id(workspace, f"{c}^", path)
            for c in commits
        ):
            found.append(path)
    return found


def fault_failure_without_start(
    status: _gate_base.Status, output: str, fault: str
) -> tuple[_gate_base.Status, str]:
    """Turn a language rule's failure into a harness fault when the start tree could not be built."""
    if fault and status is _gate_base.Status.FAIL:
        message = f"harness fault: cannot compare with the starting tree: {fault}\n{output}"
        return _gate_base.Status.HARNESS_FAULT, message
    return status, output


#: ruff's concise format: `path:line:col: CODE message`.
_RUFF_CONCISE = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]+\d+) (?:\[\*\] )?(.*)$")


#: ruff's full format: a `CODE message` header, then ` --> path:line:col` on the next line.
_RUFF_HEADER = re.compile(r"^([A-Z]+\d+) (?:\[\*\] )?(.*)$")


_RUFF_ARROW = re.compile(r"^\s*--> (\S+?):\d+:\d+$")


#: mypy: `path:line[:col]: error: message  [code]`; its `note:` lines do not match.
_MYPY = re.compile(r"^(\S+?):\d+(?::\d+)?: error: (.*?)(?:  \[([\w-]+)\])?$")


#: A pytest node id. It holds no whitespace outside its parameter brackets, and the bracket part, which may
#: hold spaces and " - " (`test_sign[utf-8 - bom]`), runs to the first `]` that " - " or the end of the line
#: follows. The one grammar for every reader of pytest's summary (`_target_run` too).
PYTEST_ID = r"[^\s\[]+(?:\[.*?\])?"
#: A row of pytest's short test summary, which names every failing node id: the kind, then the id and its
#: message. A log line (`ERROR    asyncio:...`) pads its level with spaces and is not one.
_PYTEST_ROW = re.compile(r"^(FAILED|ERROR) (\S.*)$")
_PYTEST_SPLIT = re.compile(rf"({PYTEST_ID})((?: - .*)?)")
#: Another place a bracketed id could end: a `]` that " - " or the end of the row follows.
_ID_END = re.compile(r"\](?: - |$)")


def parse_pytest_row(line: str) -> tuple[str, str] | None:
    """Return a pytest summary row's kind and key, or None when the line is not such a row.

    The key is the node id when the row shows where it ends: no whitespace outside its brackets, a test file
    or a test in one (`.py`, `::`), and no later `]` the message could start after instead. Otherwise it is
    the whole row after the kind (`test_x[a] - b]`, a path with a space), which equals only the same row: a
    test that cannot be told apart from another is never taken for it.
    """
    if not (row := _PYTEST_ROW.match(line)):
        return None
    kind, rest = row[1], row[2]
    if split := _PYTEST_SPLIT.fullmatch(rest):
        test, message = split[1], split[2]
        name = test.split("[", 1)[0]
        if ("::" in name or name.endswith(".py")) and not ("[" in test and _ID_END.search(message)):
            return kind, test
    return kind, rest


#: A tool that did not get to judge the tree: pytest's usage, collection and "nothing ran" outcomes.
_NOT_A_VERDICT = ("Interrupted:", "error during collection", "no tests ran")


#: The exit code ruff, mypy and pytest use for "ran fine and found problems".
_FINDINGS_EXIT = 1


def finding_keys(output: str) -> Counter[tuple[str, str, str]]:
    """Return the findings in a tool's output as a multiset of (path, code, message) keys.

    Line numbers are not part of a key, because they shift when unrelated code moves. tsc's
    `file(1,7): error` format is not parsed, so an output with no recognised finding gives an empty
    multiset, which the caller treats as "cannot tell".
    """
    keys: Counter[tuple[str, str, str]] = Counter()
    header = None
    for line in output.splitlines():
        if match := _RUFF_CONCISE.match(line):
            keys[match[1], match[2], match[3]] += 1
        elif (arrow := _RUFF_ARROW.match(line)) and header:
            keys[arrow[1], header[1], header[2]] += 1
        elif match := _MYPY.match(line):
            keys[match[1], match[3] or "", match[2]] += 1
        elif found := parse_pytest_row(line):
            keys[found[1], found[0], ""] += 1
        header = _RUFF_HEADER.match(line)
    return keys


def _judge_against_start(code: int, output: str, start_code: int, start_output: str) -> tuple[int, str]:
    """Judge a failed tool run against the same run on the project's starting tree.

    Debt the start already had is not the project's to fix, but it must not grow: the run fails if any
    finding key occurs more often than it did on the start. It also fails when the run is not an ordinary
    findings run (a timeout, a crash, pytest's exit 2-5), or when no finding is recognised in the run
    (nothing to compare), so a timeout on both sides is never "pre-existing". Pass the tool's full
    output, which a truncated tail would make look flat; the verdict line is kept in front of the
    recorded tail.
    """
    tail = output[-_gate_base._OUTPUT_TAIL :]
    if code != _FINDINGS_EXIT or any(marker in output for marker in _NOT_A_VERDICT):
        return code, tail
    if start_code == 0:
        return code, f"new failure: the starting tree passes\n{tail}"
    before, now = finding_keys(start_output), finding_keys(output)
    if not now:
        return code, tail
    new = now - before
    if new:
        shown = "\n".join(f"  {path} {kind} {message}".rstrip() for path, kind, message in list(new)[:20])
        return code, f"regression: {new.total()} finding(s) not on the starting tree:\n{shown}\n{tail}"
    return (
        0,
        f"pre-existing (the starting tree fails too: {before.total()} findings, now {now.total()})\n{tail}",
    )
