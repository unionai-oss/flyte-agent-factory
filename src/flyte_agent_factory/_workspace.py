"""The workspace template shipped with the engine, and creating a workspace from it.

A workspace is the one repository the factory writes to (docs/architecture-repos.md). Plain `git` through
`subprocess`. This runs from `ops/` scripts on an operator's machine, and `git` is also used in sessions by
`_push_guard.install`.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "_assets" / "workspace_template"
#: A commit needs an identity, and the operator's global one may not exist (a fresh machine, CI).
_IDENTITY = ("-c", "user.name=agent-workspace", "-c", "user.email=agent-workspace@users.noreply.github.com")


#: What GitHub puts into a repository created with its "add a README" boxes ticked. Only the README and the
#: .gitignore are replaced by the template's; the owner's LICENSE stays.
_GITHUB_INIT_FILES = frozenset({"README.md", "LICENSE", ".gitignore"})


def git(repo: Path, *args: str, timeout: int = 300, placeholder_identity: bool = True) -> str:
    """Run `git` in `repo` and return stdout; a failure or a timeout raises `RuntimeError`.

    `placeholder_identity=False` leaves the commit identity to the person's own git configuration.
    """
    try:
        proc = subprocess.run(
            ["git", *(_IDENTITY if placeholder_identity else ()), *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def build_entry_command(module: str, entry: str, leading: tuple[str, ...] = ()) -> str:
    """Return the shell line that runs `module.entry()` from a bare shell, `"$@"` appended.

    The code bundle's root is baked in as `PYTHONPATH`: the supervisor has it on `sys.path` because Flyte
    put it there, but a process started from the session's shell (or from a git hook) does not inherit
    that. `python -c` rather than `-m`: the package is already imported in the supervisor, and re-entering
    it with `-m` makes CPython warn about executing a module that is already in `sys.modules`.
    """
    bundle_root = str(Path(__file__).resolve().parent.parent)
    prefix = "".join(f"{shlex.quote(arg)} " for arg in leading)
    return (
        f"PYTHONPATH={shlex.quote(bundle_root)}${{PYTHONPATH:+:$PYTHONPATH}} "
        f"exec {shlex.quote(sys.executable)} -c "
        f"'import sys; from {module} import {entry}; sys.exit({entry}())' "
        f'{prefix}"$@"'
    )


def copy_template(dest: Path) -> None:
    """Copy the template into `dest`: absent, empty, or a fresh clone.

    A fresh clone holds `.git` and perhaps the files GitHub creates with a repository; `.git` is never
    touched.
    """
    if dest.exists():
        names = {p.name for p in dest.iterdir()} if dest.is_dir() else {dest.name}
        # Extra files are tolerated only next to a `.git`: a bare directory with a README is somebody's.
        allowed = {".git"} | _GITHUB_INIT_FILES if ".git" in names else set()
        if not dest.is_dir() or not names <= allowed:
            raise FileExistsError(f"{dest} exists and is not an empty directory or a fresh clone")
    shutil.copytree(TEMPLATE, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))


def require_identity(cwd: Path) -> None:
    """Fail clearly unless git has a commit identity: the person's own, never a placeholder."""

    def configured(key: str, env: str) -> bool:
        if os.environ.get(env):
            return True
        try:
            proc = subprocess.run(
                ["git", "config", "--get", key], cwd=cwd, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"git config --get {key} timed out after 30s") from exc
        return bool(proc.stdout.strip())

    if not (configured("user.name", "GIT_AUTHOR_NAME") and configured("user.email", "GIT_AUTHOR_EMAIL")):
        raise RuntimeError(
            'git has no commit identity: run `git config --global user.name "Your Name"` and '
            "`git config --global user.email you@example.com`, then run this again"
        )


def commit_all(repo: Path, message: str) -> None:
    """Stage everything in `repo` and commit it as the person whose git configuration this is."""
    require_identity(repo)
    git(repo, "add", "-A", placeholder_identity=False)
    git(repo, "commit", "-q", "-m", message, placeholder_identity=False)


def create(dest: Path, *, commit: bool = False) -> None:
    """Populate `dest` with the template.

    In an existing clone the repository, its remote and branch stay as they are, and nothing is committed
    unless `commit` is set. Without a `.git` this makes a fresh repository on `main` with one commit.
    """
    clone = (dest / ".git").exists()
    commit = commit or not clone
    if commit:  # before anything is written, so a missing identity leaves the directory as it was
        require_identity(dest if dest.is_dir() else dest.parent if dest.parent.is_dir() else Path.cwd())
    copy_template(dest)
    if not clone:
        git(dest, "init", "-q", "-b", "main")
    if commit:
        commit_all(dest, "Workspace from template")
