"""Gate basics every other gate module builds on: a check's status, running a tool, project paths."""

from __future__ import annotations

import enum
import os
import subprocess
from pathlib import Path

from . import _secret_env


class Status(enum.Enum):
    """The outcome of one check.

    A plain Enum, not an IntEnum, so a status can never be compared with a process exit code. The
    values are the labels written to standards-results.json.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    #: The check does not apply to this project and never blocks.
    NOT_APPLICABLE = "not applicable"
    #: History that cannot be judged. Visible like a gap, but never a pass.
    UNKNOWN = "unknown (legacy history)"
    #: The tool is not installed (exit 127).
    TOOL_UNAVAILABLE = "tool unavailable"
    #: The check could not be run here (a toolchain that cannot be installed), so nothing is known: neither
    #: the project's failure nor a pass. The reason is in the output.
    NOT_RUN = "not run"
    #: A FAIL the owner accepted in `waivers.toml`. It does not block.
    WAIVED = "WAIVED"

    @classmethod
    def from_exit(cls, code: int) -> Status:
        # Only exit codes: a Status passed back in would map to FAIL, so a non-blocking result would block.
        if not isinstance(code, int) or isinstance(code, (bool, Status)):
            raise TypeError(f"from_exit takes an exit code, not {type(code).__name__}")
        return cls.PASS if code == 0 else cls.TOOL_UNAVAILABLE if code == 127 else cls.FAIL

    @property
    def blocks(self) -> bool:
        return self in {Status.FAIL, Status.TOOL_UNAVAILABLE}


_SKIP_DIRS = {".git", ".claude-flyte", "node_modules", ".venv"}


#: Seconds a tool may run, unless its rule says otherwise with a `Timeout: <seconds>` line.
_TOOL_TIMEOUT = 300


#: The fail-to-pass runs of a project's tests: the limit for a real suite. A round that touched many
#: test files (a debt paydown) runs most of the suite, which took ~6 minutes for this repository.
TEST_TIMEOUT = 1200


_MAX_TOOL_TIMEOUT = 7200


_OUTPUT_TAIL = 12000


def _run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: int = _TOOL_TIMEOUT,
    tail: int | None = _OUTPUT_TAIL,
) -> tuple[int, str]:
    """Run a tool or git in the workspace, which the session wrote: never with the supervisor's secrets.

    `env` is the caller's own (`_secret_env.child_env(...)`); without one the tool gets the scrubbed
    environment of this process. Git gets `_secret_env.GIT_SAFE_CONFIG`, so `.git/config` names no
    program it runs. `tail` keeps that many trailing characters of a tool's output; None keeps all.
    """
    if args[0] == "git":
        args = _secret_env.git_argv(*args[1:])
    try:
        # Tools print whatever bytes they like; a replacement character beats a crashed gate.
        result = subprocess.run(
            args,
            cwd=cwd,
            env=_secret_env.child_env() if env is None else env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"command timed out after {timeout} seconds"
    except FileNotFoundError as exc:
        return 127, f"{exc.filename} not found"
    output = result.stdout + result.stderr
    return result.returncode, output if args[0] == "git" or tail is None else output[-tail:]


def is_workspace(checkout: Path) -> bool:
    """Return True when the default branch of `checkout` has a `projects/` directory at its root.

    That directory (the template commits `projects/.gitkeep`) is what makes a repo a workspace. It is
    read from `origin/HEAD`, not the working tree: a project on the engine repo that creates a
    `projects/` directory of its own on its branch must not change what layout it is judged by.
    """
    if not checkout.is_dir():
        return False
    code, kind = _run(["git", "cat-file", "-t", "origin/HEAD:projects"], checkout)
    return not code and kind.strip() == "tree"


def project_rel(name: str, layout: str = "sandbox") -> str:
    """Return the project's directory relative to the workspace: `.` for a root-layout project."""
    return {"root": ".", "workspace": f"projects/{name}"}.get(layout, name)


def project_dir(workspace: Path, name: str, layout: str = "sandbox") -> Path:
    """Return what the gates treat as "the project".

    That is `<workspace>/<name>` in the shared sandbox repo, `<workspace>/projects/<name>` in a workspace,
    and the whole repo for a project on any other repo (see `_git.project_layout`).
    """
    return workspace / project_rel(name, layout)


def checks_rel(name: str, layout: str = "sandbox") -> str:
    """Where the project's `checks.sh` is, relative to the workspace.

    A root-layout project has no `<name>/` directory to keep it in; the harness directory is
    git-ignored there, so the script is committed with `git add -f`.
    """
    return f".claude-flyte/{name}/checks.sh" if layout == "root" else f"{project_rel(name, layout)}/checks.sh"


def _project_files(project: Path) -> list[str]:
    """Project-relative paths, without dependency and harness trees."""
    paths: list[str] = []
    for directory, children, names in os.walk(project):
        children[:] = [n for n in children if n not in _SKIP_DIRS]
        paths.extend(str((Path(directory) / n).relative_to(project)) for n in names)
    return paths
