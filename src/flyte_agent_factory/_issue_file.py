"""The linked GitHub issue as a read-only file in the session's workspace.

A session has no GitHub credential and must not get one, so it cannot read the issue its project was
launched from. The supervisor holds the App, so it reads the issue (GET calls only) and writes
`.claude-flyte/ISSUE.md` beside BRIEF.md, on every start and resume. Only allowlisted authors' text is
written, framed as data (`_issues.GUIDANCE`); the file is redacted like everything else published.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import _github, _issues, _profile, _session_config
from ._bridge import redact_tokens

log = logging.getLogger(__name__)

_STATE_FILE = ".claude-flyte/issue-state.json"
MAX_COMMENTS = 100
# The factory's own milestone comments carry a hidden marker; they say nothing the session needs.
_MARKER = re.compile(r"<!-- factory-issue:[0-9a-f]{64} -->")


def _allowed(
    user: dict[str, Any], body: str, repo: str, profile: _profile.Profile, app: _github.GithubApp
) -> bool:
    maintainers = set(profile.github.maintainers)
    if _issues._human(user, maintainers):  # type: ignore[arg-type]
        return True
    # As `_issues.loop_creation`: an issue the factory's own loop opened is accepted without a human.
    return bool(
        profile.loop.enabled
        and repo.lower() == profile.loop.repo.lower()
        and user.get("type") == "Bot"
        and re.search(r"^<!-- loop-source: [A-Za-z0-9_.:-]+ -->$", body, re.M)
        and user.get("login") == app.app_login()
    )


def _section(login: str, when: str, text: str, *, new: bool = False) -> str:
    return f"### {login}, {when}{' (new since the last run)' if new else ''}\n\n{_issues._fenced(text)}\n"


def render(app: _github.GithubApp, issue_ref: str, profile: _profile.Profile, since: str) -> str:
    """Return ISSUE.md's text for `issue_ref`; `since` is the previous fetch time ("" for none)."""
    repo, number = _issues.parse_issue(issue_ref)
    issue = app.issue(repo, number)
    comments = app.comments(repo, number)
    author = issue.get("user") or {}
    body = issue.get("body") or ""
    labels = ", ".join(label["name"] for label in issue.get("labels", [])) or "none"
    allowed = _allowed(author, body, repo, profile, app)
    # The title is the author's text as much as the body is.
    title = " ".join(str(issue.get("title", "")).split()) or issue_ref
    lines = [
        f"# {title}" if allowed else f"# {issue_ref} (title withheld: not an allowlisted author)",
        "",
        f"- Issue: {issue_ref}",
        f"- URL: {issue.get('html_url') or f'https://github.com/{repo}/issues/{number}'}",
        f"- State: {issue.get('state', 'unknown')}",
        f"- Labels: {labels}",
        f"- Author: {author.get('login', 'unknown')}",
        "",
        _issues.GUIDANCE,
        "",
        "This file is read-only and rewritten by the supervisor each time the project resumes.",
        "",
        "## Body",
        "",
    ]
    lines.append(
        _issues._fenced(body)
        if allowed
        else f"Body by {author.get('login', 'unknown')} withheld: not an allowlisted author."
    )
    shown = [c for c in comments if not _MARKER.search(c.get("body") or "")]
    skipped = len(comments) - len(shown)
    cut = max(0, len(shown) - MAX_COMMENTS)
    shown = shown[cut:]
    lines += ["", "## Comments", ""]
    if cut:
        lines += [f"The {cut} oldest comments are not shown.", ""]
    if skipped:
        lines += [f"{skipped} factory status comments are not shown.", ""]
    if not shown:
        lines.append("No comments.")
    for comment in shown:
        user = comment.get("user") or {}
        login = user.get("login", "unknown")
        when = comment.get("created_at", "")
        if _issues._human(user, set(profile.github.maintainers)):  # type: ignore[arg-type]
            lines.append(_section(login, when, comment.get("body") or "", new=bool(since) and when > since))
        else:
            lines.append(f"Comment by {login}, {when}, withheld: not an allowlisted author.\n")
    text = "\n".join(lines).rstrip() + "\n"
    return redact_tokens(app.redact(text))


def _read_since(path: Path) -> str:
    try:
        return str(json.loads(path.read_text())["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def _replace(path: Path, text: str, mode: int) -> None:
    # Replaced, not rewritten in place: the target is read-only, and a reader never sees half a file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_issue_file(
    workspace: Path,
    app: _github.GithubApp | None,
    issue_ref: str,
    profile: _profile.Profile | None,
    *,
    now: datetime | None = None,
    row: Callable[[str, str], None] = lambda icon, text: None,
) -> Path | None:
    """Write the workspace's ISSUE.md and return its path; None when there is no issue or App.

    Blocking (HTTP): call it through `asyncio.to_thread`. A GitHub failure is a timeline row and the
    previous file, if any, stays.
    """
    if not (app and profile and issue_ref):
        return None
    path = workspace / _session_config._ISSUE_FILE
    state = workspace / _STATE_FILE
    fetched_at = (now or datetime.now(timezone.utc)).isoformat()
    try:
        text = render(app, issue_ref, profile, _read_since(state))
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace(path, text, 0o444)
        _replace(state, json.dumps({"fetched_at": fetched_at}) + "\n", 0o644)
    except (_github.GithubError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("Could not write %s: %s", _session_config._ISSUE_FILE, type(exc).__name__)
        row("⚠️", f"could not read issue {issue_ref} into {_session_config._ISSUE_FILE}: {type(exc).__name__}")
        return None
    return path


async def refresh(timeline: Any, app: _github.GithubApp | None) -> None:
    """Write ISSUE.md for the linked issue, on every start and resume so a resumed session sees it fresh."""
    if timeline.issues:
        await asyncio.to_thread(
            write_issue_file,
            _session_config.WORKSPACE,
            app,
            timeline.issues.issue,
            _session_config._PROFILE,
            row=timeline.row,
        )
