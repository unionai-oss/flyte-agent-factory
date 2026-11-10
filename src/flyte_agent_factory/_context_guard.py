"""Context guard for the coordinator session: on by default, off with `AGENT_FACTORY_CONTEXT_GUARD=0`.

The coordinator is one long Claude Code session that re-sends its whole context on every turn, so a
whole-file `cat` of a large module is paid again on each later turn. The guard is on unless the switch
says 0, false, no or off. With it on:

- the generated CLAUDE.md drops the demo-recording sections unless the launch is a demo (`_demo_kind`)
  or the brief asks for one (`wants_demo`, used by `_instructions`);
- a PreToolUse hook (`python -m flyte_agent_factory._context_guard`) denies an *unbounded* read of a large
  file and says how to read a range instead;
- the session auto-compacts at about 200K tokens instead of letting context grow to hundreds of thousands.

The hook denies and redirects rather than summarising: edits, debugging and review need the exact source.
It fails open, so a bug here can only ever let a read through, never block the session.

This module is run by `python -m`, so it must stay a statically imported `.py` (the bundle ships no other
kind).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flyte_agent_factory import _names

ENV = "AGENT_FACTORY_CONTEXT_GUARD"
BYTES_ENV = "AGENT_FACTORY_CONTEXT_GUARD_BYTES"
DEFAULT_BYTES = 24 * 1024
#: Claude Code's `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (tokens, minimum 100K). Compaction fires a little under the
#: window, so 200K lands at roughly 185-200K of context: well under the 687K a never-compacting session
#: reached, while leaving room for a normal working set after the summary.
AUTO_COMPACT_WINDOW = "200000"
AUTO_COMPACT_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"

#: The values that switch the guard off; unset, empty or anything else leaves it on.
_FALSY = {"0", "false", "no", "off"}
#: Formats the Read tool renders rather than dumps as text; their size is not what is re-sent.
_NOT_TEXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb"}
_WHOLE_FILE_READERS = {"cat", "less", "more"}
_DEMO_WORDS = re.compile(r"\b(demo|demos|video|videos|recording|recordings|walkthrough|screencast)\b", re.I)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the guard is on: always, unless `AGENT_FACTORY_CONTEXT_GUARD` is 0, false, no or off."""
    return _names.getenv(ENV, env=env).strip().lower() not in _FALSY


def wants_demo(brief: str) -> bool:
    """Whether the brief asks for a demo, a video or a walkthrough."""
    return bool(_DEMO_WORDS.search(brief))


def hook_command() -> str:
    # `sys.executable`, so the module imports in the pod whatever interpreter the supervisor runs on.
    return f"{shlex.quote(sys.executable)} -m flyte_agent_factory._context_guard"


def settings_extra(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Return the `extra` for `_auth.write_settings` that installs the hook, or None when the guard is off."""
    if not enabled(env):
        return None
    return {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Read|Bash", "hooks": [{"type": "command", "command": hook_command()}]}
            ]
        }
    }


def session_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Extra environment for the coordinator's Claude Code process."""
    return {AUTO_COMPACT_ENV: AUTO_COMPACT_WINDOW} if enabled(env) else {}


def _threshold(env: Mapping[str, str] | None = None) -> int:
    try:
        return int(_names.getenv(BYTES_ENV, env=env).strip() or DEFAULT_BYTES)
    except ValueError:
        return DEFAULT_BYTES


def _large_text_file(path: str, cwd: str, limit: int) -> int | None:
    """Return the file's size if it is a regular text-ish file over `limit` bytes, else None."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    if p.suffix.lower() in _NOT_TEXT or not p.is_file():
        return None
    size = p.stat().st_size
    return size if size > limit else None


def _bash_files(command: str) -> list[str]:
    """Return the files of a plain `cat F [G]` / `less F` / `more F`, or [] for anything else or any doubt."""
    # No shlex punctuation mode: a pipe, `;`, `&&`, a redirect or a subshell must make this not a plain read,
    # and in plain mode they stay inside tokens, so reject on the raw text instead.
    if re.search(r"[|;&<>`$()\n]", command):
        return []
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if len(words) < 2 or words[0] not in _WHOLE_FILE_READERS:
        return []
    args = words[1:]
    if any(a.startswith("-") for a in args):
        return []
    return args


def deny_reason(payload: Mapping[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    """Why this tool call is an unbounded read of a large file, or None to allow it."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    limit = _threshold(env)
    if tool == "Read":
        if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
            return None
        path = tool_input.get("file_path")
        files = [path] if isinstance(path, str) else []
    elif tool == "Bash":
        command = tool_input.get("command")
        files = _bash_files(command) if isinstance(command, str) else []
    else:
        return None
    for path in files:
        size = _large_text_file(path, cwd, limit)
        if size is not None:
            return (
                f"{path} is {size:,} bytes. The whole context is re-sent on every turn, so reading it whole "
                "costs tokens on every later turn too. Read the part you need instead: "
                f"`sed -n '1,200p' {shlex.quote(path)}`, `grep -n PATTERN {shlex.quote(path)}` to find "
                "the lines first, or the Read tool with offset and limit. For unfamiliar code, "
                "a sub-agent (or `delegate --answer` where delegation is available) can explore it and "
                "report back."
            )
    return None


def main() -> int:
    try:
        reason = deny_reason(json.load(sys.stdin))
        if reason:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": reason,
                        }
                    }
                )
            )
    # Fail open: a broken guard must never block the session. These are what bad stdin, a payload of the wrong
    # shape (AttributeError/TypeError) or an unreadable path raise; a broad `except Exception` is pinned by
    # tests/test_narrow_excepts.py to the boundary sites, and this is not one.
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
