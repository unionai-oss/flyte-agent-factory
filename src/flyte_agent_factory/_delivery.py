"""The tree a project delivers to another repository: `promote.toml`, importing it, and checking it.

A project that `Delivers to:` another repository keeps that repository's files as ordinary tracked files in
the workspace, under `projects/<name>/<path>`. `promote.toml` (next to the project's other harness files) says
which repository, and the commit of it the files were imported from:

    repo = "https://github.com/owner/name.git"
    base = "<40-hex commit>"   # "" for a new repository
    path = "."                 # where the tree is inside the project dir

The gates (`_gates.target_results`) read this module; `_promote` imports and delivers with it. Kept apart from
`_promote` because `_promote` imports `_gates`, and from `_target_ci`, which only parses and runs nothing.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import tomllib
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from . import _secret_env
from ._git import _repo_identity

FILE = "promote.toml"
#: Files the harness keeps at a project's root. With `path = "."` they sit beside the tree's own files but are
#: not part of it: they never go to the target, and an import refuses a target that has one at its root.
HARNESS_NAMES = ("checks.sh", "SUMMARY.md", FILE)
_SHA = re.compile(r"[0-9a-f]{40}")
#: git's own empty tree: the baseline of a repository that does not exist yet, so every line is an added line.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_SHORTHAND = re.compile(r"[\w.-]+/[\w.-]+")
#: Commits in the scratch repo the gates read; nobody sees them.
_SCRATCH_IDENTITY = (
    "-c",
    "user.name=agent-factory",
    "-c",
    "user.email=agent-factory@localhost",
    "-c",
    "commit.gpgsign=false",
)


class DeliveryError(Exception):
    """The delivered tree cannot be read, imported or checked; the message says why and what to do."""


@dataclasses.dataclass(frozen=True)
class Delivery:
    """The contents of `promote.toml`. `base` is "" for a repository that does not exist yet."""

    repo: str
    base: str
    path: str = "."


def _command(
    args: tuple[str, ...], scrubbed: bool, env: dict[str, str] | None
) -> tuple[list[str], dict[str, str]]:
    if scrubbed:
        return _secret_env.git_argv(*args), _secret_env.child_env(**(env or {}))
    return ["git", *args], {**os.environ, **(env or {})}


def git(
    cwd: Path, *args: str, scrubbed: bool = False, env: dict[str, str] | None = None, timeout: int = 300
) -> str:
    """Run `git` in `cwd` and return stdout; a failure raises `DeliveryError`.

    `scrubbed` is for a repository the session wrote (the workspace the gates read): git runs without the
    pod's secrets and with `.git/config` unable to name a program. An operator's own commands, which need
    their ssh agent and credential helpers, leave it off.
    """
    argv, environ = _command(args, scrubbed, env)
    try:
        # Paths are bytes to git: surrogateescape keeps a non-UTF-8 one intact down to the file system.
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=environ,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeliveryError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode:
        raise DeliveryError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def load(project: Path) -> Delivery | None:
    """Return the project's `promote.toml`, or None if it has none (a legacy or an ordinary project)."""
    file = project / FILE
    if not file.is_file():
        return None
    try:
        data = tomllib.loads(file.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise DeliveryError(f"{FILE} is not valid TOML: {exc}") from exc
    repo, base, path = data.get("repo"), data.get("base", ""), data.get("path", ".")
    if not isinstance(repo, str) or not repo:
        raise DeliveryError(f'{FILE} needs `repo = "<url of the target repository>"`')
    if not isinstance(base, str) or (base and not _SHA.fullmatch(base)):
        raise DeliveryError(
            f'{FILE}: `base` is the 40-hex commit the tree was imported from, or "" for a new repo'
        )
    if not isinstance(path, str):
        raise DeliveryError(
            f"{FILE}: `path` is a relative directory in the project, `.` for the project itself"
        )
    return Delivery(repo, base, relative_path(path))


def _identity(repo: str) -> str:
    """`_repo_identity`, with `owner/name` (as a brief may write it) read as that GitHub repository."""
    return _repo_identity(f"https://github.com/{repo}" if _SHORTHAND.fullmatch(repo.strip()) else repo)


def require_repo(delivery: Delivery, delivers_to: str) -> None:
    """Refuse a manifest that names another repository than the brief's `Delivers to:`.

    Both are fixed before a session starts; if they disagree, the gates would judge one repository's tree
    and `deliver` would send it to another.
    """
    if _identity(delivery.repo) != _identity(delivers_to):
        raise DeliveryError(
            f"{FILE} names {delivery.repo}, but the brief says `Delivers to: {delivers_to}`: "
            "they must be the same repository (the operator re-imports, or fixes the brief)"
        )


def relative_path(path: str) -> str:
    """Return `path` as a normalised directory inside the project; refuse one that leaves it."""
    parsed = PurePosixPath(path or ".")
    if parsed.is_absolute() or ".." in parsed.parts:
        raise DeliveryError(f"path {path!r} must be a directory inside the project")
    return parsed.as_posix()


def write(project: Path, delivery: Delivery) -> None:
    """Write `promote.toml`. JSON strings are valid TOML basic strings."""
    text = "".join(f"{key} = {json.dumps(value)}\n" for key, value in dataclasses.asdict(delivery).items())
    (project / FILE).write_text(text)


def tree_dir(project: Path, delivery: Delivery) -> Path:
    """Return the directory holding the tree; a path that leaves the project through a link is refused."""
    tree = (project / delivery.path).resolve()
    if not tree.is_relative_to(project.resolve()):
        raise DeliveryError(f"path {delivery.path!r} leaves the project")
    return tree


def drop_harness(tree: Path, delivery: Delivery) -> None:
    """Remove the harness names from an exported tree, where `path = "."` put them beside its files."""
    if delivery.path == ".":
        for name in HARNESS_NAMES:
            (tree / name).unlink(missing_ok=True)


def export_tree(repo: Path, treeish: str, dest: Path, *, scrubbed: bool = False) -> None:
    """Write the files of `treeish` (a `<rev>:<path>`, `./<path>` for one relative to `repo`) into `dest`.

    Reads the stored objects and nothing else. Not `git archive` or `checkout-index`: those apply the tree's
    `.gitattributes` (`export-ignore` would drop files from an import or a delivery without a word, line
    endings would change bytes) and run any `filter` program a session's `.git/config` names.
    """
    tree = git(repo, "rev-parse", treeish, scrubbed=scrubbed).strip()
    listing = git(repo, "ls-tree", "--full-tree", "-r", "-z", tree, scrubbed=scrubbed)
    files = []
    for entry in filter(None, listing.split("\0")):
        meta, _, name = entry.partition("\t")
        mode, kind, sha = meta.split()
        parts = PurePosixPath(name).parts
        if ".." in parts or PurePosixPath(name).is_absolute():
            raise DeliveryError(f"{treeish} holds a path that leaves the tree: {name!r}")
        if kind == "blob":  # a submodule's commit has no files to write
            files.append((mode, sha, name))
    dest.mkdir(parents=True, exist_ok=True)
    argv, environ = _command(("cat-file", "--batch"), scrubbed, None)
    with subprocess.Popen(
        argv, cwd=repo, env=environ, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    ) as proc:
        questions, answers = proc.stdin, proc.stdout
        assert questions is not None and answers is not None

        def ask() -> None:
            # A thread: a large tree's answers fill the pipe before all the questions are written.
            with questions:
                questions.write("".join(f"{sha}\n" for _, sha, _ in files).encode())

        asker = threading.Thread(target=ask, daemon=True)
        asker.start()
        for mode, sha, name in files:
            header = answers.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise DeliveryError(f"cannot read {name} ({sha}) of {treeish}")
            data = answers.read(int(header[2]))
            answers.read(1)  # the newline `cat-file --batch` ends each object with
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if mode == "120000":
                path.symlink_to(os.fsdecode(data))
            else:
                path.write_bytes(data)
                path.chmod(0o755 if mode == "100755" else 0o644)
        asker.join()


def _commit_scratch(checkout: Path, message: str) -> None:
    # -f: the tree's own ignore rules must not drop a file that is tracked upstream.
    git(checkout, "add", "-A", "-f", scrubbed=True)
    git(checkout, *_SCRATCH_IDENTITY, "commit", "-q", "--allow-empty", "-m", message, scrubbed=True)


def _worktree_files(tree: Path) -> list[str]:
    """Return the tree's files as they are on disk now: tracked and untracked, not ignored, none deleted."""
    listed = git(tree, "ls-files", "-z", "--cached", "--others", "--exclude-standard", scrubbed=True)
    names = [name for name in listed.split("\0") if name]
    # A file tracked and since deleted is still listed; a directory is a nested repository.
    return [
        n for n in names if os.path.lexists(tree / n) and (os.path.islink(tree / n) or (tree / n).is_file())
    ]


def import_commit(project: Path) -> str:
    """Return the workspace commit of the operator's import, after checking `promote.toml` is what it wrote.

    That is the commit that first added `promote.toml`, not the last that touched it: a session could commit
    an edit (or a delete and re-add) with its work and so move the baseline, or the repository, past the
    review. The file must also still be what the import wrote. The scope gate blocks such an edit; the gates
    and `deliver` both check it again in case one got through. Re-importing means importing into a new
    project.
    """
    log = git(project, "log", "--diff-filter=A", "--reverse", "--format=%H", "--", FILE, scrubbed=True)
    if not log.strip():
        raise DeliveryError(f"{FILE} is not committed: commit it with the imported tree")
    added = log.split()[0]
    if git(project, "show", f"{added}:./{FILE}", scrubbed=True) != (project / FILE).read_text():
        raise DeliveryError(
            f"{FILE} is not what the import at {added[:12]} wrote: it is the operator's, a session "
            f"does not edit it. Restore it with `git checkout {added[:12]} -- {FILE}`, or have the "
            "operator re-import into a new project"
        )
    return added


@contextlib.contextmanager
def target_checkout(project: Path, delivery: Delivery) -> Iterator[Path]:
    """Yield a scratch git repo the target's own CI can be run in, built from the workspace history alone.

    Commit B is the tree as imported: the workspace commit that first added `promote.toml`, which is the
    operator's import. Not the last commit that touched it: a session could commit a comment-only edit with
    its work and so move the baseline past the work. The file must also still be what the import wrote, for
    the same reason (the scope gate blocks the edit; this makes the gates agree even if it got through).
    Commit H is the tree's files as they are now, committed or not, so the gates judge what the session
    sees. Branch `work` is at H, `origin/HEAD` at B, which is where `_target_run.target_base` looks for the
    commit the work began at. A new repository (`base = ""`) has no B: the gates compare with `EMPTY_TREE`.
    """
    tree = tree_dir(project, delivery)
    if not tree.is_dir():
        raise DeliveryError(f"the tree {delivery.path!r} is not in the project")
    relative = "" if delivery.path == "." else delivery.path
    with tempfile.TemporaryDirectory(prefix="factory-delivery-") as scratch:
        checkout = Path(scratch) / "checkout"
        checkout.mkdir()
        git(checkout, "init", "-q", "-b", "work", scrubbed=True)
        imported = import_commit(project)
        if delivery.base:
            try:
                export_tree(project, f"{imported}:./{relative}", checkout, scrubbed=True)
            except DeliveryError as exc:
                raise DeliveryError(
                    f"cannot read the tree imported at {imported[:12]} (a shallow workspace clone?): {exc}"
                ) from exc
            drop_harness(checkout, delivery)
            _commit_scratch(checkout, f"imported from {delivery.repo}@{delivery.base[:12]}")
            git(checkout, "update-ref", "refs/remotes/origin/work", "HEAD", scrubbed=True)
            git(
                checkout,
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "refs/remotes/origin/work",
                scrubbed=True,
            )
        # H replaces B's files rather than adding to them, so a file the work deleted is deleted here too.
        for entry in checkout.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for name in _worktree_files(tree):
            if delivery.path == "." and name in HARNESS_NAMES:
                continue
            target = checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tree / name, target, follow_symlinks=False)
        _commit_scratch(checkout, "the tree as it is now")
        yield checkout


def import_tree(
    repo: str, project: Path, *, ref: str = "", path: str = ".", new: bool = False
) -> tuple[Delivery, str]:
    """Import `repo`'s files into `project/path`, write `promote.toml`, and commit both in the enclosing repo.

    Returns the delivery and the commit message. Runs with the operator's credentials and identity, so it
    is a plain `git`, and it pushes nothing. `new`: no clone, an empty tree, `base = ""`.
    """
    path = relative_path(path)
    tree = (project / path).resolve()
    project.mkdir(parents=True, exist_ok=True)
    if not tree.is_relative_to(project.resolve()):
        raise DeliveryError(f"path {path!r} leaves the project")
    if (project / FILE).exists():
        raise DeliveryError(f"{project / FILE} exists: this project already delivers somewhere")
    taken = (
        [e.name for e in tree.iterdir() if path != "." or e.name not in HARNESS_NAMES]
        if tree.is_dir()
        else []
    )
    if taken:
        raise DeliveryError(
            f"{tree} is not empty ({', '.join(sorted(taken)[:5])}): import into a new directory"
        )
    top = Path(git(project, "rev-parse", "--show-toplevel").strip())
    base = ""
    with tempfile.TemporaryDirectory(prefix="delivery-import-") as scratch:
        if not new:
            clone = Path(scratch) / "target"
            branch = ["--branch", ref] if ref else []
            git(Path(scratch), "clone", "-q", "--no-checkout", *branch, repo, str(clone), timeout=900)
            try:
                base = git(clone, "rev-parse", "HEAD").strip()
            except DeliveryError as exc:
                raise DeliveryError(
                    f"{repo} has no commits to import (use --new for an empty target)"
                ) from exc
            if path == ".":
                clash = [
                    n for n in git(clone, "ls-tree", "--name-only", "HEAD").split() if n in HARNESS_NAMES
                ]
                if clash:
                    raise DeliveryError(
                        f"the target's root has {', '.join(clash)}, which are project harness files: "
                        "import with --path <subdirectory>"
                    )
            export_tree(clone, "HEAD", tree)
    tree.mkdir(parents=True, exist_ok=True)
    delivery = Delivery(repo, base, path)
    write(project, delivery)
    where = project.resolve().relative_to(top.resolve()).as_posix()
    message = f"import {repo}@{base[:12] or 'new'} into {where}"
    paths = [str(tree), str(project / FILE)]
    git(top, "add", "-f", "--", *paths)
    # Only these paths: whatever else the operator has staged is not part of the import.
    git(top, "commit", "-q", "-m", message, "--", *paths)
    return delivery, message
