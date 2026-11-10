"""Read-only CI results for the session's own branch, served by the supervisor.

The session holds a deploy key only and must not hold GitHub App credentials, so `ci-status` drops a
request file (`_bridge.ci_status_cli`) and this service answers it with the supervisor's `GithubApp`.
Only GET calls are made, the repo and the default branch are the project's own (the request cannot
name either), and an answer file holds results only: never a token, and never a log line that looks
like one.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import _github
from ._bridge import ANSWERS, REQUESTS, _write_atomic, redact_tokens
from ._pull_requests import PullRequests

Emit = Callable[[str, str], None]

PREFIX = "ci-status-"
#: At most one GitHub round trip per this many seconds for the whole session; identical requests inside
#: it are answered from the cache, others wait their turn.
MIN_INTERVAL_S = 10.0
LOG_MAX_LINES = 200
LOG_MAX_BYTES = 64 * 1024
_SHA = re.compile(r"[0-9a-f]{7,40}")


class _Refused(Exception):
    """A request that gets an error answer; the text is for the session."""


def cut_log_tail(log: str) -> tuple[str, bool]:
    """Return the last `LOG_MAX_LINES` lines of `log`, within `LOG_MAX_BYTES`, and whether any was cut."""
    lines = log.splitlines()
    text = "\n".join(lines[-LOG_MAX_LINES:])
    raw = text.encode()
    if len(raw) > LOG_MAX_BYTES:
        # Cut mid-character at the front is possible; `replace` keeps the rest readable.
        text = raw[-LOG_MAX_BYTES:].decode("utf-8", "replace")
    return text, len(lines) > LOG_MAX_LINES or len(raw) > LOG_MAX_BYTES


class CiStatus:
    """Answers `ci-status-*.json` requests. `target()` is the project's pull-request state, or None."""

    def __init__(
        self,
        root: Path,
        target: Callable[[], PullRequests | None],
        emit: Emit,
        *,
        min_interval: float = MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.root = root
        self._target = target
        self._emit = emit
        self._min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._seen: set[str] = set()
        self._pending: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._last_call = float("-inf")
        #: (sha, job) -> (when answered, data): a repeat inside the interval costs no GitHub call.
        self._cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}

    async def run(self) -> None:
        try:
            while True:
                self.scan()
                await asyncio.sleep(1)
        finally:
            for task in self._pending:
                task.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)

    def scan(self) -> None:
        for path in sorted((self.root / REQUESTS).glob(f"{PREFIX}*.json")):
            if path.name in self._seen or (self.root / ANSWERS / path.name).exists():
                continue
            self._seen.add(path.name)
            task = asyncio.create_task(self.service(path))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def service(self, path: Path) -> None:
        answer = self.root / ANSWERS / path.name
        try:
            request = json.loads(path.read_text())
            if not isinstance(request, dict) or request.get("id") != path.stem:
                raise _Refused("request id must match its filename")
            sha, job = request.get("sha") or "", request.get("job") or 0
            if not isinstance(sha, str) or (sha and not _SHA.fullmatch(sha)):
                raise _Refused("--sha must be 7 to 40 hex digits")
            if type(job) is not int or job < 0:
                raise _Refused("--logs must be a job id")
            data = await self._answer(sha, job)
        except _Refused as exc:
            payload: dict[str, Any] = {"error": str(exc)}
        except (OSError, ValueError) as exc:
            payload = {"error": f"invalid ci-status request: {type(exc).__name__}"}
        except _github.GithubError as exc:
            self._emit("⚠️", f"ci-status {path.stem}: {exc}")
            payload = {"error": str(exc)}
        else:
            payload = {"result": data}
        # Redacted last, whatever the payload: results only ever leave through here.
        _write_atomic(answer, {"id": path.stem, **_scrub(payload)})

    async def _answer(self, sha: str, job: int) -> dict[str, Any]:
        pr = self._target()
        if pr is None:
            raise _Refused("CI status is unavailable: this project has no GitHub App access or branch")
        async with self._lock:
            # Cached under the request as asked, so `sha=""` (the branch head) is the key for the head.
            key = (sha, job)
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < self._min_interval:
                return hit[1]
            wait = self._last_call + self._min_interval - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last_call = self._clock()
            if job:
                data = await asyncio.to_thread(_read_job_log, pr, job)
            else:
                data = await asyncio.to_thread(_summarize_checks, pr, sha)
            self._last_call = self._clock()
            self._cache[key] = (self._last_call, data)
            return data


def _scrub(value: Any) -> Any:
    """`value` with credential-shaped text removed from every string in it."""
    if isinstance(value, str):
        return redact_tokens(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _summarize_checks(pr: PullRequests, sha: str) -> dict[str, Any]:
    """Workflow runs, check-runs and statuses of `sha`, or of the branch head when it is empty.

    A part the App may not read is left empty and named in `missing`, so the session knows the
    list is incomplete rather than that CI is quiet.
    """
    app, repo = pr.app, pr.repo
    if not sha:
        try:
            sha = app.default_branch_sha(repo, pr.branch)[1]
        except _github.GithubError as exc:
            if exc.status in (404, 422):
                raise _Refused(f"branch {pr.branch} is not on GitHub yet: push it first") from exc
            raise
    missing: list[str] = []

    def part(permission: str, read: Callable[[], list[_github.Json]]) -> list[_github.Json]:
        try:
            return read()
        except _github.GithubError as exc:
            if not _github.missing_permission(exc):
                raise
            missing.append(permission)
            return []

    runs = part("Actions: read", lambda: app.workflow_runs(repo, sha))
    checks = part("Checks: read", lambda: app.check_runs(repo, sha))
    statuses = part("Commit statuses: read", lambda: app.commit_statuses(repo, sha))
    return {
        "repo": repo,
        "branch": pr.branch,
        "sha": sha,
        "runs": [
            {
                "id": run["id"],
                "name": run["name"],
                "status": run["status"],
                "conclusion": run["conclusion"],
                "url": run["html_url"],
            }
            for run in runs
        ],
        # A check-run's id is the job id `--logs` takes.
        "checks": [
            {
                "id": check["id"],
                "name": check["name"],
                "status": check["status"],
                "conclusion": check["conclusion"],
                "url": check["html_url"],
            }
            for check in checks
        ],
        "statuses": [
            {"context": s["context"], "state": s["state"], "url": s.get("target_url") or ""} for s in statuses
        ],
        "missing": missing,
    }


def _read_job_log(pr: PullRequests, job: int) -> dict[str, Any]:
    try:
        log = pr.app.job_log(pr.repo, job)
    except _github.GithubError as exc:
        if _github.missing_permission(exc):
            raise _Refused(
                'job logs need the GitHub App\'s "Actions: read" permission, which it lacks'
            ) from exc
        if exc.status == 404:
            raise _Refused(f"no logs for job {job} in {pr.repo}: wrong id, or the logs have expired") from exc
        raise
    # Scrubbed before the cut: a token split by it would no longer match.
    text, truncated = cut_log_tail(pr.app.redact(redact_tokens(log)))
    return {"repo": pr.repo, "job": job, "log": text, "truncated": truncated}
