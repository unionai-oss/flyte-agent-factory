"""Owner waivers: `waivers.toml` at the workspace root lets the owner accept a failing check for a while.

The file is read from a committed ref, never the working tree: the session can write the tree (or hide a file
from git), so only what the owner committed counts. `_gate_scope._classify_scope` also blocks a commit that
touches it. A waiver is always the owner's decision, and `link` points at where the owner made it.

The session can run any git command in the workspace, so nothing in the workspace's `.git` (remotes, refs,
`refs/replace`, config) may decide which commit is read. `load_owner` fetches the default branch of the
repository URL the supervisor holds into a throwaway bare repository outside the workspace. Only when the
supervisor has no URL (tests, local fixtures; a deployment always has one) does the gate read the round's
base commit, taken from the supervisor's in-memory record (`_gates.capture_base`). After a supervisor restart
that record is gone and the only one left is `.claude-flyte/<name>/base-commit`, which the session can write:
that case is as trustworthy as that file, and is a known limit.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path

from . import _gate_base, _secret_env

FILE = "waivers.toml"


@dataclasses.dataclass(frozen=True)
class Waiver:
    check: str
    project: str
    expires: datetime.date
    link: str
    reason: str = ""


def _entry(index: int, raw: object) -> Waiver:
    def bad(message: str) -> ValueError:
        return ValueError(f"{FILE} entry {index}: {message}")

    if not isinstance(raw, dict):
        raise bad("must be a table")
    for field in ("check", "project", "expires", "link"):
        if field not in raw:
            raise bad(f"missing {field}")
    for field in ("check", "project", "link", "reason"):
        if field in raw and not isinstance(raw[field], str):
            raise bad(f"{field} must be a string")
    # datetime is a subclass of date: a time of day would make "through the end of that day" ambiguous.
    expires = raw["expires"]
    if not isinstance(expires, datetime.date) or isinstance(expires, datetime.datetime):
        raise bad("expires must be a TOML date such as 2026-11-30")
    if not raw["link"].startswith("https://"):
        raise bad("link must be https")
    return Waiver(raw["check"], raw["project"], expires, raw["link"], raw.get("reason", ""))


class OwnerCopyUnavailable(Exception):
    """The owner's repository could not be read, so there are no waivers (never a session-controlled ref)."""


def _env() -> dict[str, str]:
    """Return the environment for reading waivers: no replace refs, and no user or system git config.

    The supervisor's credentials come through the environment (`GIT_SSH_COMMAND`, a token in the URL), so
    nothing is lost; `~/.gitconfig` is the session's to write, and so is every config in the workspace.
    """
    return {
        **_secret_env.child_env(),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def load_owner(repo_url: str) -> list[Waiver]:
    """Load the waivers on the default branch of `repo_url`, fetched into a scratch repository.

    Raises OwnerCopyUnavailable when the fetch fails and ValueError when the file is unreadable.
    """
    with tempfile.TemporaryDirectory(prefix="factory-waivers-") as scratch:
        for args in (
            ["git", "init", "--quiet", "--bare"],
            ["git", "fetch", "--quiet", "--depth", "1", repo_url, "HEAD"],
        ):
            code, output = _gate_base._run(args, Path(scratch), env=_env())
            if code:
                # The URL can carry an access token.
                output = output.replace(repo_url, "<repo url>").strip()
                raise OwnerCopyUnavailable(f"git {args[1]} exited {code}: {output}")
        return load(Path(scratch), "FETCH_HEAD")


def load(workspace: Path, ref: str) -> list[Waiver]:
    """Load the waivers committed at `ref`; no such file has none, an unreadable one raises ValueError."""
    if not ref:
        return []
    code, _ = _gate_base._run(["git", "cat-file", "-e", f"{ref}:{FILE}"], workspace, env=_env())
    if code:
        return []
    code, text = _gate_base._run(["git", "show", f"{ref}:{FILE}"], workspace, env=_env())
    if code:
        raise ValueError(f"{FILE}: git show {ref}:{FILE} exited {code}: {text.strip()}")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{FILE}: {exc}") from exc
    entries = data.get("waiver", [])
    if not isinstance(entries, list):
        raise ValueError(f"{FILE}: waiver must be an array of tables ([[waiver]])")
    return [_entry(i, raw) for i, raw in enumerate(entries)]


def matching(waivers: list[Waiver], check: str, project: str, rule_ids: Iterable[str] = ()) -> list[Waiver]:
    """Waivers for `check` in `project`; `rule_ids` are the owner's rule IDs mapped to a built-in check."""
    names = {check, *rule_ids}
    return [w for w in waivers if w.check in names and w.project in {project, "*"}]


def applicable(
    waivers: list[Waiver], check: str, project: str, today: datetime.date, rule_ids: Iterable[str] = ()
) -> Waiver | None:
    """Return the unexpired waiver for `check` in `project`, if any; it holds through its `expires` day.

    A waiver names the check (`factory:module-size`) or any rule ID mapped to it, as `rule_ids`.
    """
    return next((w for w in matching(waivers, check, project, rule_ids) if today <= w.expires), None)
