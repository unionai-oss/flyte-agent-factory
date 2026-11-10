"""The workspace template shipped with the engine, and creating a workspace from it.

A workspace is the one repository the factory writes to (docs/architecture-repos.md). Plain `git` through
`subprocess`. This runs from `ops/` scripts on an operator's machine, and `git` is also used in sessions by
`_push_guard.install`.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent / "_assets" / "workspace_template"
#: A commit needs an identity, and the operator's global one may not exist (a fresh machine, CI).
_IDENTITY = ("-c", "user.name=agent-workspace", "-c", "user.email=agent-workspace@users.noreply.github.com")


def git(repo: Path, *args: str, timeout: int = 300) -> str:
    """Run `git` in `repo` and return stdout; a failure or a timeout raises `RuntimeError`."""
    try:
        proc = subprocess.run(
            ["git", *_IDENTITY, *args], cwd=repo, capture_output=True, text=True, timeout=timeout
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
    """Copy the template into `dest`, which must be absent or empty."""
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FileExistsError(f"{dest} exists and is not an empty directory")
    shutil.copytree(TEMPLATE, dest, dirs_exist_ok=True)


def commit_all(repo: Path, message: str) -> None:
    """Stage everything in `repo` and commit it."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def create(dest: Path) -> None:
    """Make `dest` a git repository on `main` holding the template in one commit."""
    copy_template(dest)
    git(dest, "init", "-q", "-b", "main")
    commit_all(dest, "Workspace from template")
