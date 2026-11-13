"""Scope: which changed paths a project may touch, as ok, flagged or blocked."""

from __future__ import annotations

import re
import shlex
from pathlib import Path, PurePosixPath

from . import _delivery, _gardening, _waivers

#: Harness files whose every byte is the harness's.
_HARNESS_FILES = {".mcp.json"}
#: Root files every project shares; editing one is editing someone else's work.
_SHARED_ROOT_FILES = {"README.md", "CLAUDE.md", "AGENTS.md", "DESIGN.md"}
_SHARED_CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}
#: The owner's rules, read by the factory and never written by it.
STANDARDS_DIR = "standards"
STANDARDS_MESSAGE = "standards/ is the owner's; edit it by hand, not from a project"


def _glob_match(path: str, pattern: str) -> bool:
    """`**` crosses directories; `*` and `?` stay within one (PurePath.full_match needs Python 3.13)."""
    if not any(c in pattern for c in "*?["):
        return False
    regex = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    regex = regex.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(regex, path) is not None


def _manifest_path(name: str, layout: str) -> str:
    """Where the project's `promote.toml` is in the repository.

    The operator's import wrote it: it names the target and the commit the review's diff starts at, so a
    session that could edit it could choose its own diff.
    """
    prefix = {"workspace": f"projects/{name}/", "sandbox": f"{name}/"}.get(layout, "")
    return prefix + _delivery.FILE


def _classify_scope(
    path: str, name: str, brief: str, owned: set[str] | None = None, layout: str = "sandbox"
) -> str:
    """`ok`, `flag` (advisory) or `block` for one changed path.

    `waivers.toml`, `gardening.toml`, everything under `standards/` and the project's `promote.toml` are the
    owner's (or the operator's) and always block, whatever the brief permits.

    In the root layout scope is exactly the brief's `May edit:` paths plus harness state: `<name>/`
    grants nothing there, because the project name may be a real top-level directory (`src`). The
    workspace layout is the same with `projects/<name>/` added: the rest of `projects/` (the
    `.gitkeep`, other projects) and everything outside it is blocked, never merely flagged.
    """
    if path in (_waivers.FILE, _gardening.FILE) or PurePosixPath(path).parts[:1] == (STANDARDS_DIR,):
        return "block"
    if path == _manifest_path(name, layout):
        return "block"
    permissions = " ".join(
        re.findall(r"^[ \t]*(?:[-*][ \t]*)?May edit:[ \t]*(.*)$", brief, re.MULTILINE | re.IGNORECASE)
    )
    try:
        named = shlex.split(permissions.replace(",", " "))
    except ValueError:
        named = []  # Malformed quoting must not grant scope accidentally.
    allowed = [
        str(PurePosixPath(p.strip("`")))
        for p in named
        if ".." not in PurePosixPath(p).parts and not PurePosixPath(p).is_absolute()
    ]
    parts = PurePosixPath(path).parts
    if (
        any(path == a or path.startswith(a + "/") or _glob_match(path, a) for a in allowed)
        or parts[0] in {".claude", ".claude-flyte"}  # the harness's own state, and its `checks.sh`
        or path in _HARNESS_FILES  # a harness-owned file is never the project's to be blamed for
    ):
        return "ok"
    if layout == "root":
        return "block"
    if layout == "workspace":
        return "ok" if parts[:2] == ("projects", name) and len(parts) > 2 else "block"
    if path == name or path.startswith(name + "/"):
        return "ok"
    if len(parts) > 1:
        return "block" if owned is None or parts[0] in owned else "flag"
    shared = (
        parts[0].startswith(".")
        or parts[0] in _SHARED_ROOT_FILES
        or Path(parts[0]).suffix in _SHARED_CONFIG_SUFFIXES
    )
    return "block" if shared else "flag"


def scope_violations(
    changes: list[str], name: str, brief: str, owned: set[str] | None = None, layout: str = "sandbox"
) -> list[str]:
    """Edits to owned directories or shared root files without explicit permission."""
    return [p for p in changes if _classify_scope(p, name, brief, owned, layout) == "block"]


def scope_flagged(
    changes: list[str], name: str, brief: str, owned: set[str] | None = None, layout: str = "sandbox"
) -> list[str]:
    """Other out-of-scope files: worth a reviewer's glance, not worth bouncing DONE."""
    return [p for p in changes if _classify_scope(p, name, brief, owned, layout) == "flag"]
