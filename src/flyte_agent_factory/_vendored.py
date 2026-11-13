"""Vendored upstream trees: the files of a project that are another repository's, not the project's.

A project that builds a cross-repo patch carries byte-identical copies of other repositories in its
directory. The gate rules (module size, test removal, language detection) must judge only what the project
changed in them. `<project>/vendored.toml` names each copy's upstream; a nested git checkout needs no entry,
its own history says what changed (`_target_run.nested_checkouts`).

    [[tree]]
    path = "flyte-agent-factory"   # relative to the project directory
    repo = "https://github.com/example/flyte-agent-factory"
    commit = "<full 40-hex sha>"
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import re
import subprocess
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from . import _gate_base, _gate_start, _secret_env, _target_run, _waivers

MANIFEST = "vendored.toml"

#: URL schemes a tree's `repo` may use. Tests widen it to fetch from a local `file://` upstream; nothing
#: else may, a manifest is written by the session and must not make the supervisor read local paths.
_ALLOWED_SCHEMES: tuple[str, ...] = ("https",)

_FETCH_TIMEOUT = 120
#: Why each (repo, commit) this process could not fetch failed, and when: one gate run consults the trees for
#: several rules, and an unreachable host costs minutes per attempt. A failure is retried after
#: `_UNAVAILABLE_FOR` seconds, so a long-lived process does not keep a network blip forever.
_UNAVAILABLE: dict[tuple[str, str], tuple[float, str]] = {}
_UNAVAILABLE_FOR = 600
_DEFAULT_REF = "refs/vendored/default"
_COMMIT = re.compile(r"[0-9a-fA-F]{40}")


@dataclasses.dataclass
class _Tree:
    """One manifest tree and the upstream blobs (path -> (sha, size)) at its commit."""

    prefix: PurePosixPath
    git_args: list[str]
    cwd: Path
    blobs: dict[str, tuple[str, int]]

    def strip_prefix(self, relative: str) -> str | None:
        """Return a project-relative path as upstream's repository path, None if outside this tree."""
        path = PurePosixPath(relative)
        return str(path.relative_to(self.prefix)) if path.is_relative_to(self.prefix) else None


@dataclasses.dataclass
class _Entry:
    """One valid `[[tree]]` of the manifest; `prefix` is its resolved path relative to the project."""

    path: str
    repo: str
    commit: str
    prefix: PurePosixPath


def count_lines(data: bytes) -> int:
    """Return the line count of `data` as `_module_size._sizes` counts a file: raw bytes."""
    # str.splitlines() also breaks on \x0c, \x85, U+2028 and a lone \r, and would make upstream look longer.
    return data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))


@dataclasses.dataclass
class Upstreams:
    """Everything known about one project's upstream trees: manifest trees and nested git checkouts.

    `omitted_tests` are upstream's test files missing from a directory the copy has (project-relative): the
    one way to drop a test that no diff shows, so the test-removal rule fails on them.
    """

    project: Path
    trees: list[_Tree] = dataclasses.field(default_factory=list)
    nested: list[_target_run.Nested] = dataclasses.field(default_factory=list)
    problems: list[str] = dataclasses.field(default_factory=list)
    described: list[str] = dataclasses.field(default_factory=list)
    omitted_tests: list[str] = dataclasses.field(default_factory=list)

    def _nested_of(self, relative: str) -> tuple[_target_run.Nested, str] | None:
        path = self.project / relative
        for checkout in self.nested:
            if path.is_relative_to(checkout.root):
                return checkout, str(path.relative_to(checkout.root))
        return None

    def unchanged(self, relative: str) -> bool:
        """Return whether the project-relative path is a file identical to upstream's."""
        for tree in self.trees:
            if (inner := tree.strip_prefix(relative)) is not None and inner in tree.blobs:
                sha, size = tree.blobs[inner]
                path = self.project / relative
                if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                    return False
                data = path.read_bytes()
                # The blob id git would give the file, as `git hash-object --no-filters` computes it.
                return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == sha
        found = self._nested_of(relative)
        return found is not None and found[0].untouched(found[1])

    def read_upstream(self, relative: str) -> bytes | None:
        """Return upstream's content of a project-relative path, None if no known tree has it."""
        for tree in self.trees:
            if (inner := tree.strip_prefix(relative)) is not None:
                if inner not in tree.blobs:
                    return None
                argv = _secret_env.git_argv(*tree.git_args, "cat-file", "blob", tree.blobs[inner][0])
                try:
                    result = subprocess.run(
                        argv, cwd=tree.cwd, env=_env(), capture_output=True, timeout=60, check=False
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return None
                return None if result.returncode else result.stdout
        found = self._nested_of(relative)
        return found[0].bytes_at_base(found[1]) if found else None

    def count_upstream_lines(self, relative: str) -> int | None:
        """Return upstream's line count of the file, None if upstream has no such file."""
        data = self.read_upstream(relative)
        return None if data is None else count_lines(data)

    def describe(self) -> str:
        """Return a paragraph for rule output: the trees taken as upstream's, why any is not; "" if none."""
        parts = []
        if self.described:
            parts.append(
                f"Files identical to upstream are not the project's ({MANIFEST}): "
                + "; ".join(self.described)
                + "."
            )
        if self.omitted_tests:
            more = f" and {len(self.omitted_tests) - 10} more" if len(self.omitted_tests) > 10 else ""
            parts.append(
                "Upstream test files missing from a directory the copy has (a removed test): "
                f"{', '.join(self.omitted_tests[:10])}{more}."
            )
        parts.extend(self.problems)
        return " ".join(parts)


def _cache_dir_for(repo: str) -> Path:
    # Imported here: `_gate_tests` imports this module for `fail_to_pass`.
    from . import _gate_tests

    return _gate_tests.cache_root() / "vendored" / hashlib.sha256(repo.encode()).hexdigest()[:16]


def _env() -> dict[str, str]:
    # No replace refs and no user or system config: both are the session's to write (see `_waivers.git_env`).
    return _waivers.git_env()


def _git(
    args: list[str], cwd: Path, *, timeout: int = _gate_base._TOOL_TIMEOUT, tail: int | None = None
) -> tuple[int, str]:
    return _gate_base._run(["git", *args], cwd, env=_env(), timeout=timeout, tail=tail)


def _has_commit(git_args: list[str], cwd: Path, commit: str) -> bool:
    return not _git([*git_args, "cat-file", "-e", f"{commit}^{{commit}}"], cwd)[0]


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock beside the cache, so concurrent gate runs do not corrupt it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _in_trusted_base(project: Path, commit: str, trusted_base: str) -> bool:
    """Return whether `commit` is in the history of `trusted_base`, the commit the supervisor recorded.

    Only history the project started from is upstream's word without a fetch: any later commit, and any ref
    (`refs/remotes/origin/*` included), may be the session's, and a session must not be able to declare its
    own code upstream's. The anchor is the one waivers use (`_gates._trusted_base`), with the same limit: a
    session that rewrites the recorded base breaks every gate's view of what it changed, not only this one.
    """
    if not trusted_base:
        return False
    return not _git(["merge-base", "--is-ancestor", commit, trusted_base], project)[0]


def _fetch(repo: str, commit: str, *, fetch: bool = True) -> tuple[list[str], Path]:
    """Return the git arguments of the cache holding `commit` of `repo`, fetching it unless it is there.

    The commit must be in the history of `repo`'s default branch: a repository serves a commit of any branch
    by sha, and a session could otherwise name a commit it pushed to its own branch as upstream's. With
    `fetch=False` only a cache an earlier gate filled is read: nothing is created, locked or fetched.
    """
    if not fetch:
        return _cached(repo, commit)
    failed = _UNAVAILABLE.get((repo, commit))
    if failed and time.monotonic() - failed[0] < _UNAVAILABLE_FOR:
        raise _gate_start._GateError(failed[1])
    try:
        return _fetch_now(repo, commit)
    except _gate_start._GateError as exc:
        _UNAVAILABLE[repo, commit] = (time.monotonic(), str(exc))
        raise


def _on_default_ref(git_args: list[str], cwd: Path, commit: str) -> bool:
    return not _git([*git_args, "merge-base", "--is-ancestor", commit, _DEFAULT_REF], cwd)[0]


def _cached(repo: str, commit: str) -> tuple[list[str], Path]:
    cache = _cache_dir_for(repo)
    git_args = [f"--git-dir={cache}"]
    if (
        (cache / "HEAD").is_file()
        and _has_commit(git_args, cache.parent, commit)
        and _on_default_ref(git_args, cache.parent, commit)
    ):
        return git_args, cache.parent
    raise _gate_start._GateError("not fetched yet (a snapshot reads only what a gate fetched)")


def _fetch_now(repo: str, commit: str) -> tuple[list[str], Path]:
    cache = _cache_dir_for(repo)
    git_args = [f"--git-dir={cache}"]
    # https only, no credentials: a private repository is "unavailable", not a prompt that hangs the gate. No
    # automatic gc: the default branch's history is fetched without trees, which gc must not trip over.
    fetch = [
        *git_args,
        "-c",
        "protocol.allow=never",
        *(a for scheme in _ALLOWED_SCHEMES for a in ("-c", f"protocol.{scheme}.allow=always")),
        "-c",
        "credential.helper=",
        "-c",
        "gc.auto=0",
        "fetch",
        "--quiet",
    ]
    with _locked(cache.with_suffix(".lock")):
        if not (cache / "HEAD").is_file():
            code, output = _git(["init", "--bare", str(cache)], cache.parent)
            if code:
                raise _gate_start._GateError(output.strip())
        if not _has_commit(git_args, cache.parent, commit):
            # A ref keeps the commit from being collected.
            code, output = _git(
                [*fetch, "--depth", "1", repo, f"{commit}:refs/vendored/{commit}"],
                cache.parent,
                timeout=_FETCH_TIMEOUT,
            )
            if code or not _has_commit(git_args, cache.parent, commit):
                raise _gate_start._GateError(output.strip()[-500:] or f"fetch exited {code}")
        if not _on_default_ref(git_args, cache.parent, commit):
            # Commits only (`tree:0`): enough to walk the history, a few MB even for a large repository.
            code, output = _git(
                [*fetch, "--filter=tree:0", repo, f"+HEAD:{_DEFAULT_REF}"],
                cache.parent,
                timeout=_FETCH_TIMEOUT,
            )
            # The filtered fetch marks `repo` a promisor remote, from which git would then fetch any missing
            # object on demand, unbounded and mid-rule: a missing object must be an error instead.
            _git([*git_args, "config", "--remove-section", f"remote.{repo}"], cache.parent)
            _git([*git_args, "config", "--unset", "extensions.partialClone"], cache.parent)
            if code:
                raise _gate_start._GateError(output.strip()[-500:] or f"fetch exited {code}")
            if not _on_default_ref(git_args, cache.parent, commit):
                raise _gate_start._GateError(
                    f"commit {commit} is not in the history of the default branch of {repo}"
                )
    return git_args, cache.parent


def _list_blobs(git_args: list[str], cwd: Path, commit: str) -> dict[str, tuple[str, int]]:
    # `--full-tree`: from a project directory inside the repository, paths would be relative to it.
    code, output = _git([*git_args, "ls-tree", "-r", "-z", "-l", "--full-tree", commit], cwd)
    if code:
        raise _gate_start._GateError(output.strip()[-500:])
    blobs: dict[str, tuple[str, int]] = {}
    for entry in output.split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 4 and fields[1] == "blob":
            blobs[path] = (fields[2], int(fields[3]))
    return blobs


def _parse_manifest(project: Path, manifest: Path) -> tuple[list[_Entry], list[str]]:
    """Return the manifest's valid `[[tree]]` entries, and a problem for each invalid one."""
    try:
        data = tomllib.loads(manifest.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return [], [f"{MANIFEST} is not valid TOML: {exc}"]
    raw = data.get("tree", [])
    if not isinstance(raw, list):
        return [], [f"{MANIFEST}: `tree` must be an array of tables ([[tree]])"]
    entries: list[_Entry] = []
    problems: list[str] = []
    root = project.resolve()
    schemes = " or ".join(f"{s}://" for s in _ALLOWED_SCHEMES)
    for number, entry in enumerate(raw, 1):
        label = f"{MANIFEST} tree {number}"
        fields = [entry.get(k) if isinstance(entry, dict) else None for k in ("path", "repo", "commit")]
        path, repo, commit = fields
        if not (isinstance(path, str) and isinstance(repo, str) and isinstance(commit, str)):
            problems.append(f"{label}: `path`, `repo` and `commit` must all be strings")
            continue
        parts = PurePosixPath(path).parts
        try:
            # A symlink loop, a NUL or an over-long name raises here; the manifest is the session's to write.
            resolved = (project / path).resolve()
            inside = resolved.is_relative_to(root) and (project / path).is_dir()
        except (OSError, RuntimeError, ValueError) as exc:
            problems.append(f"{label}: path {path!r} cannot be read: {exc}")
            continue
        if not path or PurePosixPath(path).is_absolute() or ".." in parts:
            problems.append(f"{label}: path {path!r} must be relative to the project, without `..`")
        elif not inside:
            problems.append(f"{label}: path {path!r} is not a directory inside the project")
        elif resolved == root:
            problems.append(f"{label}: path {path!r} is the project itself; a copy is a subdirectory of it")
        elif not repo.startswith(tuple(f"{s}://" for s in _ALLOWED_SCHEMES)):
            problems.append(f"{label}: repo {repo!r} must start with {schemes}")
        elif not _COMMIT.fullmatch(commit):
            problems.append(f"{label}: commit {commit!r} must be a full 40-hex sha")
        else:
            prefix = PurePosixPath(str(resolved.relative_to(root)))
            entries.append(_Entry(path, repo, commit.lower(), prefix))
    return entries, problems


def _omitted_tests(project: Path, tree: _Tree) -> list[str]:
    """Return upstream's test files missing from a directory the copy has, project-relative."""
    # Imported here: `_gate_tests` imports this module for `fail_to_pass`.
    from . import _gate_tests

    found = []
    for inner in sorted(tree.blobs):
        relative = str(tree.prefix / inner)
        local = project / relative
        try:
            if _gate_tests._is_test(inner) and local.parent.is_dir() and not local.exists():
                found.append(relative)
        except OSError:
            continue  # an unreadable directory: its files cannot be judged missing
    return found


def load(project: Path, skip_dirs: set[str], *, trusted_base: str = "", fetch: bool = True) -> Upstreams:
    """Return the upstream trees of `project`: its manifest's, and its nested git checkouts.

    `trusted_base` is the commit the supervisor recorded when the project was created: a declared commit in
    its history is read from the workspace's own objects. Any other is fetched from the declared `repo`, or
    with `fetch=False` (a dashboard snapshot, on the supervisor's event loop) read only from the cache.
    """
    nested = _target_run.nested_checkouts(project, skip_dirs)
    manifest = project / MANIFEST
    if not manifest.is_file():
        return Upstreams(project, nested=nested)
    entries, problems = _parse_manifest(project, manifest)
    found = Upstreams(project, nested=nested, problems=problems)
    for entry in entries:
        try:
            git_args: list[str]
            local = _has_commit([], project, entry.commit)
            if local and _in_trusted_base(project, entry.commit, trusted_base):
                git_args, cwd = [], project
            else:
                git_args, cwd = _fetch(entry.repo, entry.commit, fetch=fetch)
            tree = _Tree(entry.prefix, git_args, cwd, _list_blobs(git_args, cwd, entry.commit))
            found.trees.append(tree)
            found.described.append(f"{entry.path} ({entry.repo} @ {entry.commit[:12]})")
            found.omitted_tests.extend(_omitted_tests(project, tree))
        except (_gate_start._GateError, OSError) as exc:
            found.problems.append(
                f"vendored tree {entry.path} ({entry.repo} @ {entry.commit}) is unavailable, so its files "
                f"are judged as the project's: {str(exc).strip()[-300:]}"
            )
    return found
