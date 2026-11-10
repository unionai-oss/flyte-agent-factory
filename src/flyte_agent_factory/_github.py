"""GitHub App access: installation tokens, a repo's default-branch head and its commit health.

The deployer uses this to read statuses and fetch source without holding a deploy key; pull-request
work builds on the same token. HTTP is httpx and the JWT is PyJWT: both already in the lock.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar
from urllib.parse import quote

import httpx

API = "https://api.github.com"
#: A decoded GitHub REST object. GitHub's schema is large and mostly unread here, so values stay `Any`;
#: the fields the code relies on are checked where they are read (`_field`).
Json = dict[str, Any]
_T = TypeVar("_T")
#: Refresh a cached installation token this long before GitHub says it expires.
TOKEN_MARGIN = 5 * 60
#: GitHub allows an App JWT up to 10 minutes; `iat` is set back a minute for clock skew.
_JWT_BACK, _JWT_AHEAD = 60, 9 * 60
_REPO_URL = re.compile(
    r"(?:git@github\.com:|ssh://git@github\.com/|https://(?:[^@/]+@)?github\.com/)([^/]+)/(.+?)(?:\.git)?/?"
)
_FAILING_STATUS = {"failure", "error"}
_NO_PERMISSION = "Resource not accessible by integration"
# A check that needs a human (`action_required`) is not passing. A `stale` one never finished and
# will not on its own: pending, not failing.
_FAILING_CHECK = {"failure", "timed_out", "cancelled", "action_required"}
PER_PAGE = 100


def _running(suite: Json) -> bool:
    """Whether a check suite is still running: Actions' always, another App's only once it has runs."""
    if suite["status"] == "completed":
        return False
    actions = (suite.get("app") or {}).get("slug") == "github-actions"
    return actions or suite["latest_check_runs_count"] > 0


def _suite_detail(suite: Json) -> str:
    slug = (suite.get("app") or {}).get("slug")
    return f"{slug + ' ' if slug else ''}check suite {suite['status']}"


def missing_permission(exc: GithubError) -> bool:
    """Whether `exc` is the 403 for an App lacking a permission; a rate limit is a 403 too."""
    return exc.status == 403 and _NO_PERMISSION in str(exc)


class GithubError(RuntimeError):
    """A GitHub call failed. The text never contains a token. `status` is the HTTP status (0 if none)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _error_detail(response: httpx.Response) -> str:
    """GitHub's `message` plus each `errors[]` entry.

    A 422's `message` alone ("Validation Failed") hides the cause.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [str(body.get("message", ""))]
    for error in body.get("errors") or []:
        parts.append(str(error.get("message", "")) if isinstance(error, dict) else str(error))
    return "; ".join(part for part in parts if part)


def _field(body: object, name: str, kind: type[_T], what: str) -> _T:
    """`body[name]` if the 2xx body is an object holding a non-empty `kind` there, else GithubError.

    `_request` cannot require an object (listings such as `GET /pulls` are JSON arrays), so the
    callers that read a field check it here. The message names the field, never the body: a token
    response holds the token.
    """
    value = body.get(name) if isinstance(body, dict) else None
    # bool is an int subclass; `"id": true` is not an installation id.
    if not isinstance(value, kind) or isinstance(value, bool) or value == "":
        raise GithubError(f"github {what}: no usable `{name}` in the response")
    return value  # narrowed to `kind` by the isinstance check above


def parse_repo(url: str) -> str:
    """`owner/repo` from an ssh (`git@github.com:o/r.git`) or https clone URL."""
    match = _REPO_URL.fullmatch(url.strip())
    if not match:
        raise GithubError(f"not a github.com repository URL: {url!r}")
    return f"{match[1]}/{match[2]}"


def clone_url(repo: str, token: str) -> str:
    """Return the HTTPS URL git authenticates to with an installation token. Secret: never log it."""
    return f"https://x-access-token:{token}@github.com/{repo}.git"


class GithubApp:
    """One GitHub App's identity, with an installation token cached per repo."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        *,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._key = private_key
        self._http = client or httpx.Client(base_url=API, timeout=30)
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}  # repo -> (token, expiry epoch)
        #: URL with its query -> (ETag, the body it tagged), for conditional GETs (`issue_comments_since`).
        self._etags: dict[str, tuple[str, list[Json]]] = {}

    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iss": self._app_id, "iat": now - _JWT_BACK, "exp": now + _JWT_AHEAD}
        # Imported here: `_profile` imports this module for `parse_repo`, and images built before the
        # deployer carry no jwt.
        import jwt

        try:
            return jwt.encode(claims, self._key, algorithm="RS256")
        except (jwt.PyJWTError, ValueError) as exc:
            # A malformed key: the class only, as for any other GitHub failure, never key material.
            raise GithubError(f"github app key unusable: {type(exc).__name__}") from exc

    def redact(self, text: str) -> str:
        """`text` with every token this app has handed out replaced, for error output."""
        for token, _ in self._tokens.values():
            text = text.replace(token, "***")
        return text

    def _response(
        self, method: str, path: str, bearer: str, *, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        """Return the response to one call, which is not an error (a 304 is not one)."""
        try:
            response = self._http.request(
                method,
                path,
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/vnd.github+json",
                    **(headers or {}),
                },
                follow_redirects=True,  # a renamed repo answers 301 (307 for writes) to its new name
                **kwargs,
            )
        except httpx.HTTPError as exc:
            # A timeout or reset has no status; callers log GithubError, never a raw httpx one.
            raise GithubError(f"github {method} {path}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise GithubError(
                self.redact(
                    f"github {method} {path}: HTTP {response.status_code} {_error_detail(response)}".strip()
                ),
                response.status_code,
            )
        return response

    def _request(self, method: str, path: str, bearer: str, **kwargs: Any) -> Any:
        """Return the decoded body: an object, or an array for the listing endpoints (`_get_list`)."""
        response = self._response(method, path, bearer, **kwargs)
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            # A 2xx that is not JSON (a proxy or captive portal's HTML page) is a failed call like any
            # other, so every caller's GithubError handling covers it.
            raise GithubError(
                f"github {method} {path}: HTTP {response.status_code} body is not JSON", response.status_code
            ) from exc

    def installation_token(self, repo: str) -> str:
        cached = self._tokens.get(repo)
        if cached and self._clock() < cached[1] - TOKEN_MARGIN:
            return cached[0]
        app = self.app_jwt()
        try:
            installation = self._request("GET", f"/repos/{repo}/installation", app)
        except GithubError as exc:
            if "HTTP 404" in str(exc):
                raise GithubError(
                    f"the GitHub App (id {self._app_id}) is not installed on {repo}", 404
                ) from exc
            raise
        installation_id = _field(installation, "id", int, f"installation of {repo}")
        issued = self._request("POST", f"/app/installations/{installation_id}/access_tokens", app)
        what = "issued an unusable installation token"
        token = _field(issued, "token", str, what)
        expires_at = _field(issued, "expires_at", str, what)
        try:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            # The class only: the body holds the token.
            raise GithubError(
                f"github {what}: `expires_at` is not a timestamp ({type(exc).__name__})"
            ) from exc
        self._tokens[repo] = (token, expires)
        return token

    def _call(self, repo: str, method: str, path: str, **kwargs: Any) -> Any:
        """Make one call as the installation.

        On a 401 the cached token is gone (revoked or expired early): drop it and retry once with a
        fresh one.
        """
        try:
            return self._request(method, path, self.installation_token(repo), **kwargs)
        except GithubError as exc:
            if exc.status != 401:
                raise
        self._tokens.pop(repo, None)
        return self._request(method, path, self.installation_token(repo), **kwargs)

    def _get(self, repo: str, path: str, **kwargs: Any) -> Json:
        body: Json = self._call(repo, "GET", path, **kwargs)
        return body

    def _get_list(self, repo: str, path: str, **kwargs: Any) -> list[Json]:
        """`_get` for the endpoints that answer a bare JSON array."""
        body: list[Json] = self._call(repo, "GET", path, **kwargs)
        return body

    def _get_all(self, repo: str, path: str, key: str) -> list[Json]:
        """Every item under `key` of a paginated listing: pages of `PER_PAGE` until one is short."""
        items: list[Json] = []
        page = 1
        while True:
            got = self._get(repo, path, params={"per_page": PER_PAGE, "page": page})[key]
            items += got
            if len(got) < PER_PAGE:
                return items
            page += 1

    def _get_pages(self, repo: str, path: str, *, max_pages: int | None = None, **params: Any) -> list[Json]:
        """Every item of a listing that is a bare JSON array (`_get_all` needs an object holding one)."""
        items: list[Json] = []
        page = 1
        while True:
            got = self._get_list(repo, path, params={**params, "per_page": PER_PAGE, "page": page})
            items += got
            if len(got) < PER_PAGE:
                return items
            if max_pages is not None and page >= max_pages:
                logging.getLogger(__name__).warning(
                    "GitHub listing %s truncated at %s pages", path, max_pages
                )
                return items
            page += 1

    def default_branch(self, repo: str) -> str:
        return _field(self._get(repo, f"/repos/{repo}"), "default_branch", str, f"repo {repo}")

    def default_branch_sha(self, repo: str, ref: str = "") -> tuple[str, str]:
        """`(branch, sha)` of the head of branch `ref`, or of the repo's default branch when it is empty."""
        branch = ref or self.default_branch(repo)
        head = self._get(repo, f"/repos/{repo}/commits/{branch}")
        return branch, _field(head, "sha", str, f"head of {repo} {branch}")

    def commit_health(self, repo: str, sha: str) -> tuple[str, str]:
        """`("ok"|"pending"|"failing"|"none", detail)` from the commit's statuses and check-runs.

        Failing wins over pending. The detail names the context that decided it. "none" means
        nothing reported at all, which the caller treats as deployable. An App without the
        "Checks: read" permission gets 403 on check-runs; health then rests on statuses alone and
        the detail says so, rather than every tick failing.

        A check suite still running also holds the commit pending: Actions creates a dependent job's
        check-run only once its `needs:` finish, and a just-pushed commit's first runs only once its
        jobs are queued, so the runs alone look done (or absent) in between. Another App's suite with
        no runs is ignored (an App that never posts one stays queued forever); an Actions suite counts
        either way. A suites listing the App may not read is ignored too.
        """
        statuses = self._get_all(repo, f"/repos/{repo}/commits/{sha}/status", "statuses")
        try:
            runs = self._get_all(repo, f"/repos/{repo}/commits/{sha}/check-runs", "check_runs")
        except GithubError as exc:
            # Only the missing-permission 403; a rate limit is a 403 too and must fail the tick.
            if not missing_permission(exc):
                raise
            health, detail = _status_health(statuses)
            return health, f"{detail}; check-runs unreadable (the App lacks Checks: read)"
        try:
            suites = self._get_all(repo, f"/repos/{repo}/commits/{sha}/check-suites", "check_suites")
        except GithubError as exc:
            if not missing_permission(exc):
                raise
            suites = []
        running = next((suite for suite in suites if _running(suite)), None)
        if not statuses and not runs:
            if running is not None:
                return "pending", _suite_detail(running)
            return "none", "no statuses or check-runs"
        for status in statuses:
            if status["state"] in _FAILING_STATUS:
                return "failing", f"{status['context']} failing"
        for run in runs:
            if run["status"] == "completed" and run["conclusion"] in _FAILING_CHECK:
                return "failing", f"{run['name']} failing"
        for status in statuses:
            if status["state"] == "pending":
                return "pending", f"{status['context']} pending"
        for run in runs:
            if run["status"] != "completed" or run["conclusion"] == "stale":
                return "pending", f"{run['name']} pending"
        if running is not None:
            return "pending", _suite_detail(running)
        return "ok", f"{len(statuses) + len(runs)} passing"

    def workflow_runs(self, repo: str, sha: str, limit: int = 20) -> list[Json]:
        """Return the newest GitHub Actions runs on commit `sha` (needs "Actions: read")."""
        body = self._get(repo, f"/repos/{repo}/actions/runs", params={"head_sha": sha, "per_page": limit})
        return list(body["workflow_runs"])

    def check_runs(self, repo: str, sha: str) -> list[Json]:
        """Every check-run on `sha` (needs "Checks: read"). An Actions job's id is its check-run's id."""
        return self._get_all(repo, f"/repos/{repo}/commits/{sha}/check-runs", "check_runs")

    def job_log(self, repo: str, job_id: int) -> str:
        """Return the log of one Actions job (needs "Actions: read").

        GitHub answers 302 to a short-lived signed URL; `_response` follows it, and httpx drops the
        Authorization header on the cross-host hop.
        """
        path = f"/repos/{repo}/actions/jobs/{job_id}/logs"
        return self._response("GET", path, self.installation_token(repo)).text

    # --- pull requests --------------------------------------------------------------------------

    def _send(self, repo: str, method: str, path: str, **kwargs: Any) -> Json:
        body: Json = self._call(repo, method, path, **kwargs)
        return body

    def app_login(self) -> str:
        body = self._request("GET", "/app", self.app_jwt())
        return _field(body, "slug", str, "App identity") + "[bot]"

    def pull(self, repo: str, number: int) -> Json:
        return self._get(repo, f"/repos/{repo}/pulls/{number}")

    def pull_files(self, repo: str, number: int) -> list[Json]:
        return self._get_pages(repo, f"/repos/{repo}/pulls/{number}/files")

    def merge_pull(self, repo: str, number: int, sha: str) -> Json:
        # GitHub checks the head atomically; a push after our last read must not merge new code.
        return self._send(repo, "PUT", f"/repos/{repo}/pulls/{number}/merge", json={"sha": sha})

    def close_pull(self, repo: str, number: int) -> Json:
        return self._send(repo, "PATCH", f"/repos/{repo}/pulls/{number}", json={"state": "closed"})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._send(repo, "POST", f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})

    def status_at(self, repo: str, sha: str, context: str) -> str:
        # Combined statuses contain only the latest state of each context, unlike /statuses.
        statuses = self._get_all(repo, f"/repos/{repo}/commits/{sha}/status", "statuses")
        state: str = next((s["state"] for s in statuses if s["context"] == context), "pending")
        return state

    def labelled_issues(self, repo: str, label: str) -> list[Json]:
        # A removed label or an unauthorized close must not hide a switch the owner has not reset.
        found = {
            issue["number"]: issue
            for issue in self._get_pages(repo, f"/repos/{repo}/issues", labels=label, state="all")
        }
        for event in self._get_pages(repo, f"/repos/{repo}/issues/events"):
            if event["event"] == "labeled" and (event.get("label") or {}).get("name") == label:
                issue = event["issue"]
                found.setdefault(issue["number"], issue)
        return list(found.values())

    def create_issue(self, repo: str, title: str, body: str, labels: list[str] | None = None) -> Json:
        payload = {"title": title, "body": body, **({"labels": labels} if labels else {})}
        return self._send(repo, "POST", f"/repos/{repo}/issues", json=payload)

    def repo_issues(self, repo: str, *, max_pages: int | None = None) -> list[Json]:
        return [
            issue
            for issue in self._get_pages(repo, f"/repos/{repo}/issues", state="all", max_pages=max_pages)
            if "pull_request" not in issue
        ]

    def open_issues(self, repo: str, label: str) -> list[Json]:
        return [
            issue
            for issue in self._get_pages(repo, f"/repos/{repo}/issues", labels=label, state="open")
            if "pull_request" not in issue
        ]

    def search_issues(self, repo: str, marker: str) -> list[Json]:
        """Search open and closed issues, then check the literal marker (search ignores punctuation)."""
        items: list[Json] = []
        page = 1
        while True:
            response = self._get(
                repo,
                "/search/issues",
                params={
                    "q": f'repo:{repo} is:issue in:body "{marker}"',
                    "per_page": PER_PAGE,
                    "page": page,
                },
            )
            if response.get("incomplete_results"):
                raise GithubError("issue marker search returned incomplete results")
            found = response["items"]
            items.extend(issue for issue in found if marker in (issue.get("body") or ""))
            if len(found) < PER_PAGE:
                return items
            if page * PER_PAGE >= 1000:
                raise GithubError("issue marker search exceeds GitHub's result limit")
            page += 1

    def read_file(self, repo: str, path: str) -> str:
        import base64

        body = self._get(repo, f"/repos/{repo}/contents/{path}")
        if body.get("encoding") != "base64":
            raise GithubError("repository text file is not base64 encoded")
        return base64.b64decode(body["content"]).decode("utf-8")

    def issue(self, repo: str, number: int) -> Json:
        return self._get(repo, f"/repos/{repo}/issues/{number}")

    def commit(self, repo: str, sha: str) -> Json:
        return self._get(repo, f"/repos/{repo}/commits/{sha}")

    def close_issue(self, repo: str, number: int) -> None:
        self._send(repo, "PATCH", f"/repos/{repo}/issues/{number}", json={"state": "closed"})

    def commit_is_on_default_branch(self, repo: str, sha: str) -> bool:
        """Check reachability against the current default branch, not a webhook snapshot."""
        branch = self.default_branch(repo)
        try:
            comparison = self._get(repo, f"/repos/{repo}/compare/{quote(branch, safe='')}...{sha}")
        except GithubError as exc:
            # A force rewrite can make the delivered commit unavailable to compare.
            if exc.status == 404:
                return False
            raise
        return comparison.get("status") in ("identical", "behind")

    def main_commits(self, repo: str) -> list[Json]:
        """Return the last 100 default-branch commits; drift must not walk unbounded history."""
        return self._get_list(
            repo, f"/repos/{repo}/commits", params={"sha": self.default_branch(repo), "per_page": 100}
        )

    def repo_pulls(self, repo: str) -> list[Json]:
        return self._get_pages(repo, f"/repos/{repo}/pulls", state="open")

    def branch_heads(self, repo: str, names: list[str]) -> list[Json]:
        """Return the head commit of each named branch, one call each; a branch that is gone is left out.

        Never list the repository's branches: that is a page per hundred branches plus a commit read each.
        """
        found: list[Json] = []
        for name in names:
            try:
                head = self._get(repo, f"/repos/{repo}/branches/{quote(name, safe='/')}")
            except GithubError as exc:
                if exc.status == 404:
                    continue
                raise
            found.append({"name": name, "commit": head["commit"]["commit"]})
        return found

    def commit_statuses(self, repo: str, sha: str) -> list[Json]:
        """Return the latest status of each context on `sha`."""
        return self._get_all(repo, f"/repos/{repo}/commits/{sha}/status", "statuses")

    def issue_events(self, repo: str, number: int) -> list[Json]:
        return self._get_pages(repo, f"/repos/{repo}/issues/{number}/events")

    def find_pull(self, repo: str, branch: str) -> Json | None:
        """Return the open pull request whose head is `branch` in `repo` itself, if any."""
        owner = repo.split("/")[0]
        found = self._get_list(
            repo, f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
        )
        return found[0] if found else None

    def open_pulls(self, repo: str) -> list[Json]:
        """Every open pull request of `repo` with its changed files: what the planner treats as occupied."""
        found: list[Json] = []
        for pull in self._get_pages(repo, f"/repos/{repo}/pulls", state="open"):
            files = self._get_pages(repo, f"/repos/{repo}/pulls/{pull['number']}/files")
            found.append(
                {
                    "repo": repo,
                    "number": pull["number"],
                    "url": pull["html_url"],
                    "changed_files": [f["filename"] for f in files],
                }
            )
        return found

    def tracked_paths(self, repo: str) -> set[str]:
        """Return the files of the default branch, from its git tree (the same set `git ls-files` lists)."""
        branch = self.default_branch(repo)
        tree = self._get(repo, f"/repos/{repo}/git/trees/{branch}", params={"recursive": "1"})
        if tree.get("truncated"):
            # A partial list would quietly under-detect mentioned files, so say so instead.
            raise GithubError(f"git tree of {repo} {branch} is truncated; too many files to list")
        return {entry["path"] for entry in tree["tree"] if entry["type"] == "blob"}

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str, *, draft: bool) -> Json:
        payload = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        return self._send(repo, "POST", f"/repos/{repo}/pulls", json=payload)

    def update_pull(self, repo: str, number: int, title: str, body: str) -> Json:
        return self._send(repo, "PATCH", f"/repos/{repo}/pulls/{number}", json={"title": title, "body": body})

    def mark_ready(self, repo: str, node_id: str) -> None:
        """Take a pull request out of draft. REST cannot; only GraphQL can."""
        query = (
            "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
            "{ pullRequest { isDraft } } }"
        )
        result = self._send(repo, "POST", "/graphql", json={"query": query, "variables": {"id": node_id}})
        # GraphQL reports failures as HTTP 200 with an `errors` list.
        if result.get("errors"):
            messages = "; ".join(str(e.get("message", "")) for e in result["errors"])
            raise GithubError(self.redact(f"github POST /graphql: {messages}"), 200)

    def comment(self, repo: str, number: int, body: str) -> Json:
        return self._send(repo, "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})

    def update_comment(self, repo: str, comment_id: int, body: str) -> None:
        self._send(repo, "PATCH", f"/repos/{repo}/issues/comments/{comment_id}", json={"body": body})

    def delete_comment(self, repo: str, comment_id: int) -> None:
        """Delete a milestone duplicate created by this App."""
        self._send(repo, "DELETE", f"/repos/{repo}/issues/comments/{comment_id}")

    def comments(self, repo: str, number: int) -> list[Json]:
        comments: list[Json] = []
        page = 1
        while True:
            found = self._get_list(
                repo, f"/repos/{repo}/issues/{number}/comments", params={"per_page": PER_PAGE, "page": page}
            )
            comments.extend(found)
            if len(found) < PER_PAGE:
                return comments
            page += 1

    def issue_comments_since(self, repo: str, number: int, since: str = "") -> list[Json]:
        """Return the issue's comments updated at or after `since` (ISO 8601; "" for all), oldest first.

        Each page is a conditional GET against the ETag this instance last saw for it: GitHub answers
        an unchanged page 304, which does not count against the App's rate limit. So a supervisor
        that asks every minute for the life of a run costs almost nothing while its issue is quiet.
        """
        path = f"/repos/{repo}/issues/{number}/comments"
        comments: list[Json] = []
        page = 1
        while True:
            params: dict[str, Any] = {
                "per_page": PER_PAGE,
                "page": page,
                **({"since": since} if since else {}),
            }
            key = f"{path}?{sorted(params.items())}"
            etag, cached = self._etags.get(key, ("", []))

            def get(etag: str = etag, params: dict[str, Any] = params) -> httpx.Response:
                headers = {"If-None-Match": etag} if etag else {}
                return self._response(
                    "GET", path, self.installation_token(repo), headers=headers, params=params
                )

            try:
                response = get()
            except GithubError as exc:  # as `_call`: a revoked or early-expired token is retried once
                if exc.status != 401:
                    raise
                self._tokens.pop(repo, None)
                response = get()
            if response.status_code == 304:
                found = cached
            else:
                try:
                    found = response.json()
                except ValueError as exc:
                    raise GithubError(
                        f"github GET {path}: HTTP {response.status_code} body is not JSON"
                    ) from exc
                if not isinstance(found, list):
                    raise GithubError(f"github GET {path}: the comment listing is not a list")
                if response.headers.get("etag"):
                    self._etags[key] = (response.headers["etag"], found)
            comments.extend(found)
            if len(found) < PER_PAGE:
                return comments
            page += 1

    def set_status(
        self, repo: str, sha: str, context: str, state: str, description: str, target_url: str
    ) -> None:
        payload: dict[str, str] = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        self._send(repo, "POST", f"/repos/{repo}/statuses/{sha}", json=payload)


def _status_health(statuses: list[Json]) -> tuple[str, str]:
    """Health from commit statuses alone, ranked as `commit_health` ranks them."""
    if not statuses:
        return "none", "no statuses"
    for status in statuses:
        if status["state"] in _FAILING_STATUS:
            return "failing", f"{status['context']} failing"
    for status in statuses:
        if status["state"] == "pending":
            return "pending", f"{status['context']} pending"
    return "ok", f"{len(statuses)} passing"
