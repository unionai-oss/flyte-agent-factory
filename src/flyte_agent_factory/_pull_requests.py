"""A project's pull request, opened and kept current as the factory's GitHub App.

The supervisor owns this; the Claude session only ever pushes the branch (with the deploy key). The
App's key and tokens are kept out of the session's and sub-agents' environments, but the session runs
as the supervisor's user, so this is not isolation until it runs as a separate user.
Nothing here may stop a run: GitHub being down or misconfigured costs the PR, not the project,
so every call is guarded and reports one timeline row instead of raising.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from typing import NotRequired, TypedDict, TypeVar, cast

from . import _github, _issues
from ._bridge import redact_tokens
from ._errors import REMOTE_ERRORS

#: The App's key as mounted on a slot. The supervisor reads it once and removes it from the environment.
KEY_ENV_VAR = "GITHUB_APP_PRIVATE_KEY"
#: Flyte/Union injects every mounted secret a second time, as `<prefix><SECRET KEY UPPER>`; the prefix
#: is this variable, `_UNION_` when unset.
SECRETS_PREFIX_ENV_VAR = "FLYTE_SECRETS_ENV_PREFIX"
DEFAULT_SECRETS_PREFIX = "_UNION_"
#: Variables that could carry GitHub credentials into the session or a sub-agent. The App's tokens
#: are held in the supervisor's memory; these names are kept out of the environments it hands to children
#: (the same user can still read /proc, so this is not a security boundary).
CREDENTIAL_ENV_VARS = (KEY_ENV_VAR, "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_INSTALLATION_TOKEN")
#: The commit status the factory's verdict is posted under.
STATUS_CONTEXT = "factory/verifier"
_TITLE_MAX = 120
_SUMMARY_MAX = 1500
_CRITERIA_MAX = 10_000  # GitHub's body limit is 65536 characters
_COMMENT_MAX = 60_000  # the same limit applies to a comment


_T = TypeVar("_T")


class _Pull(TypedDict):
    number: int
    node_id: str
    html_url: str
    draft: NotRequired[bool]


def branch_name(project: str) -> str:
    return f"factory/{project}"


def prefixed_key_env_var(secret_key: str, env: Mapping[str, str]) -> str:
    """Return the second name Flyte mounts the secret `secret_key` under ("" when there is no secret)."""
    if not secret_key:
        return ""
    return f"{env.get(SECRETS_PREFIX_ENV_VAR, DEFAULT_SECRETS_PREFIX)}{secret_key.upper()}"


def credential_env_vars(secret_key: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Every variable name that may hold the App's key or a token, given the profile's key secret."""
    prefixed = prefixed_key_env_var(secret_key, env)
    return CREDENTIAL_ENV_VARS + ((prefixed,) if prefixed else ())


def take_app_key(secret_key: str = "") -> str:
    """Return the App's private key, removed from `os.environ` under both of its names.

    This keeps it out of the environment of anything spawned afterwards. It does not clear
    `/proc/<pid>/environ`, which still shows the startup environment to the same user, so it is not
    a security boundary by itself.
    """
    prefixed = os.environ.pop(prefixed_key_env_var(secret_key, os.environ), "") if secret_key else ""
    return os.environ.pop(KEY_ENV_VAR, "") or prefixed


def _truncate(text: str, limit: int, note: str) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"\n\n… ({note})"


def split_brief(brief: str) -> tuple[str, str]:
    """`(title, rest)`: the brief's first non-empty line (without `#`s) and what follows it."""
    lines = brief.strip().splitlines()
    for index, line in enumerate(lines):
        title = re.sub(r"^[#>\s]+", "", line).strip()
        if title:
            if len(title) > _TITLE_MAX:
                title = title[: _TITLE_MAX - 1].rstrip() + "…"
            return title, "\n".join(lines[index + 1 :]).strip()
    return "Factory project", ""


def pull_body(brief: str, acceptance: str, run_url: str, *, issue: str = "", repo: str = "") -> str:
    _, rest = split_brief(brief)
    parts = []
    if rest:
        summary = _truncate(rest, _SUMMARY_MAX, "brief truncated")
        # Truncation may cut an issue's original fence; keep closing keywords outside quoted input.
        parts.append(_issues._fenced(summary) if issue else summary)
    if run_url:
        parts.append(f"Run: {run_url}")
    if acceptance.strip():
        parts.append(
            "## Acceptance criteria\n\n"
            + _truncate(acceptance, _CRITERIA_MAX, "truncated; see ACCEPTANCE.md")
        )
    if issue:
        target, number = _issues.parse_issue(issue)
        parts.append(f"Closes #{number}" if target.lower() == repo.lower() else f"Closes {target}#{number}")
    return "\n\n".join(parts)


class PullRequests:
    """One project's PR against one repo. `emit(icon, text)` writes a timeline row."""

    def __init__(
        self,
        app: _github.GithubApp,
        repo: str,
        project: str,
        run_url: str,
        emit: Callable[[str, str], None],
        issue: str = "",
    ) -> None:
        self.app, self.repo, self.run_url, self.emit = app, repo, run_url, emit
        self.issue = issue
        self.branch = branch_name(project)
        self.url = ""
        self._number = 0
        self._node_id = ""
        self._drafts = True  # cleared when the repo turns out not to support draft pull requests

    def _guard(self, what: str, call: Callable[[], _T]) -> _T | None:
        """Run one GitHub call; on any failure log a row naming the repo and return None."""
        try:
            return call()
        except _github.GithubError as exc:
            self.emit("⚠️", f"{what} failed for {self.repo}: {exc}")
        except REMOTE_ERRORS as exc:  # never let GitHub take the run down; type only, no payload
            self.emit("⚠️", f"{what} failed for {self.repo}: {type(exc).__name__}")
        return None

    def _create(self, base: str, title: str, body: str) -> _Pull:
        """Open the PR as a draft; on a repo that cannot have drafts, open it ready and remember that."""
        if self._drafts:
            try:
                # _github is typed separately; its PR responses carry these GitHub fields.
                return cast(
                    _Pull, self.app.create_pull(self.repo, self.branch, base, title, body, draft=True)
                )
            except _github.GithubError as exc:
                text = str(exc).lower()
                if exc.status != 422 or "draft" not in text or "not supported" not in text:
                    raise
                self._drafts = False
        # _github is typed separately; its PR responses carry these GitHub fields.
        return cast(_Pull, self.app.create_pull(self.repo, self.branch, base, title, body, draft=False))

    def _remember(self, pull: _Pull) -> None:
        self._number, self._node_id, self.url = pull["number"], pull["node_id"], pull["html_url"]
        if "draft" in pull:  # what GitHub says, not what this process assumed (a resumed run cannot know)
            self._drafts = bool(pull["draft"])

    def publish(self, brief: str, acceptance: str) -> str:
        """Open the draft PR for the project's branch, or refresh the open one.

        Returns its URL ("" on failure).
        """
        # Every write to GitHub passes `redact_tokens` here, in the class that owns them, so no caller can
        # forget it: the brief and ACCEPTANCE.md are text the session and the owner wrote.
        title, body = (
            redact_tokens(split_brief(brief)[0]),
            redact_tokens(pull_body(brief, acceptance, self.run_url, issue=self.issue, repo=self.repo)),
        )

        def run() -> _Pull:
            existing = self.app.find_pull(self.repo, self.branch)
            if existing:
                # _github is typed separately; updates return the same PR shape as creation.
                return cast(_Pull, self.app.update_pull(self.repo, existing["number"], title, body))
            base = self.app.default_branch(self.repo)
            return self._create(base, title, body)

        pull = self._guard("pull request update", run)
        if pull:
            self._remember(pull)
            self.emit("🔀", f"pull request {self.url}")
        return self.url

    def _known(self) -> bool:
        """Whether the PR is known, looking for it if this process has not opened it (a resumed run)."""
        if not self._number:
            # _github is typed separately; lookup returns a PR or None.
            pull = cast(
                _Pull | None,
                self._guard("pull request lookup", lambda: self.app.find_pull(self.repo, self.branch)),
            )
            if pull:
                self._remember(pull)
        return bool(self._number)

    def passed(self, sha: str, rounds: int, comment: str) -> None:
        """Review passed: ready for review, the verdict as a comment, `factory/verifier` success on `sha`."""
        if self._known():
            if self._drafts:
                self._guard("mark ready for review", lambda: self.app.mark_ready(self.repo, self._node_id))
            self._guard(
                "verdict comment", lambda: self.app.comment(self.repo, self._number, redact_tokens(comment))
            )
        self._status(sha, "success", f"review passed in {rounds} round(s)")

    def failed(self, sha: str, description: str) -> None:
        """Review failed or escalated: `factory/verifier` failure on `sha`. The PR stays a draft."""
        self._status(sha, "failure", redact_tokens(description))

    def _status(self, sha: str, state: str, description: str) -> None:
        if not sha:
            self.emit("⚠️", f"{STATUS_CONTEXT} {state} not posted for {self.repo}: no commit to attach it to")
            return
        self._guard(
            f"{STATUS_CONTEXT} status",
            lambda: self.app.set_status(self.repo, sha, STATUS_CONTEXT, state, description, self.run_url),
        )


def verdict_comment(
    rounds: int, sha: str, run_url: str, criteria: Sequence[Mapping[str, str]], results: dict[str, str]
) -> str:
    """Return the PR comment for a pass: rounds, the criteria that passed, and the link to the report."""
    lines = [f"Independent review **passed** in {rounds} round(s) on `{sha[:12]}`."]
    passed = [c for c in criteria if results.get(c["id"]) == "PASS"]
    if passed:
        lines += ["", "Criteria passed:", *(f"- {c['id']}: {c['text']}" for c in passed)]
    if run_url:
        lines += ["", f"Report: {run_url}"]
    return _truncate("\n".join(lines), _COMMENT_MAX, "truncated; see the report")
