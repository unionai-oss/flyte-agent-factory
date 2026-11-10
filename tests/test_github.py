"""GitHub App auth and commit health, against httpx.MockTransport (no network)."""

import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from flyte_agent_factory import _github

REPO = "acme/factory"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
)
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGithub:
    """Answers the endpoints `_github` uses; records requests."""

    # set by the `gh` fixture, once the client that routes to this fake exists
    app: _github.GithubApp
    clock_obj: Clock

    def __init__(self, clock, *, installed=True, ttl=3600):
        self.clock, self.installed, self.ttl = clock, installed, ttl
        self.requests: list[httpx.Request] = []
        self.minted = 0
        self.statuses: list[dict[str, Any]] = []
        self.check_runs: list[dict[str, Any]] = []
        self.check_suites: list[dict[str, Any]] = []
        self.fail_body = None
        self.checks_forbidden = False  # an App without "Checks: read" gets 403 on check-runs
        self.checks_error = None  # (status, message) for check-runs alone
        self.head_sha = "a" * 40

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.fail_body is not None and path.endswith("/status"):
            return httpx.Response(500, json={"message": self.fail_body})
        if path.startswith("/repos/acme/old-name"):  # a renamed repo: GitHub answers 301 to the new name
            return httpx.Response(301, headers={"Location": str(request.url).replace("acme/old-name", REPO)})
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77}) if self.installed else httpx.Response(404, json={})
        if path == "/app/installations/77/access_tokens":
            self.minted += 1
            return httpx.Response(
                201, json={"token": f"ghs_token{self.minted}", "expires_at": iso(self.clock() + self.ttl)}
            )
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "trunk"})
        if path == f"/repos/{REPO}/commits/trunk":
            return httpx.Response(200, json={"sha": self.head_sha})
        if path.endswith("/status"):
            return httpx.Response(200, json={"statuses": self.statuses})
        if path.endswith("/check-runs") and self.checks_error:
            return httpx.Response(self.checks_error[0], json={"message": self.checks_error[1]})
        if path.endswith("/check-runs") and self.checks_forbidden:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": self.check_runs})
        if path.endswith("/check-suites") and self.checks_forbidden:
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path.endswith("/check-suites"):
            return httpx.Response(200, json={"check_suites": self.check_suites})
        if "/statuses/" in path:
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "unexpected"})


@pytest.fixture
def gh():
    clock = Clock()
    fake = FakeGithub(clock)
    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(fake.handler))
    fake.app = _github.GithubApp("12345", PRIVATE_PEM, client=client, clock=clock)
    fake.clock_obj = clock
    return fake


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/factory.git", "acme/factory"),
        ("git@github.com:acme/factory", "acme/factory"),
        ("ssh://git@github.com/acme/factory.git", "acme/factory"),
        ("https://github.com/acme/factory.git", "acme/factory"),
        ("https://github.com/acme/factory/", "acme/factory"),
        ("https://x-access-token:secret@github.com/acme/factory.git", "acme/factory"),
        ("https://github.com/acme/my.repo.git", "acme/my.repo"),
    ],
)
def test_parse_repo(url, expected):
    assert _github.parse_repo(url) == expected


def test_parse_repo_refuses_other_hosts():
    with pytest.raises(_github.GithubError, match="not a github.com repository URL"):
        _github.parse_repo("git@gitlab.com:acme/factory.git")


def test_app_jwt_claims(gh):
    claims = jwt.decode(
        gh.app.app_jwt(), PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 10 * 60
    assert claims["exp"] > NOW > claims["iat"]


def test_installation_token_is_fetched_with_the_app_jwt_and_cached(gh):
    assert gh.app.installation_token(REPO) == "ghs_token1"
    assert gh.app.installation_token(REPO) == "ghs_token1"
    assert gh.minted == 1
    lookup = gh.requests[0]
    assert lookup.headers["authorization"].startswith("Bearer ey")  # the JWT, not an installation token


def test_installation_token_is_refreshed_near_expiry(gh):
    gh.app.installation_token(REPO)
    gh.clock_obj.now = NOW + 3600 - 6 * 60  # still 6 minutes left: keep
    assert gh.app.installation_token(REPO) == "ghs_token1"
    gh.clock_obj.now = NOW + 3600 - 4 * 60  # inside the 5-minute margin: refresh
    assert gh.app.installation_token(REPO) == "ghs_token2"
    assert gh.minted == 2


def test_missing_installation_names_the_repo(gh):
    gh.installed = False
    with pytest.raises(_github.GithubError, match="not installed on acme/factory"):
        gh.app.installation_token(REPO)


def test_default_branch_sha_uses_the_installation_token(gh):
    assert gh.app.default_branch_sha(REPO) == ("trunk", "a" * 40)
    assert gh.requests[-1].headers["authorization"] == "Bearer ghs_token1"


def test_token_never_appears_in_error_text(gh):
    gh.fail_body = "bad credentials for ghs_token1"  # a server echoing the token back
    with pytest.raises(_github.GithubError) as err:
        gh.app.commit_health(REPO, "b" * 40)
    assert "ghs_token1" not in str(err.value)
    assert "HTTP 500" in str(err.value)


def test_clone_url_carries_the_token_and_redact_hides_it(gh):
    token = gh.app.installation_token(REPO)
    url = _github.clone_url(REPO, token)
    assert url == f"https://x-access-token:{token}@github.com/acme/factory.git"
    assert token not in gh.app.redact(f"fatal: unable to access '{url}'")


@pytest.mark.parametrize(
    ("statuses", "runs", "health"),
    [
        ([], [], ("none", "no statuses or check-runs")),
        ([{"context": "factory/verifier", "state": "success"}], [], "ok"),
        (
            [{"context": "ci/test", "state": "success"}],
            [{"name": "lint", "status": "completed", "conclusion": "success"}],
            "ok",
        ),
        ([], [{"name": "lint", "status": "completed", "conclusion": "skipped"}], "ok"),
        ([{"context": "factory/verifier", "state": "failure"}], [], ("failing", "factory/verifier failing")),
        ([{"context": "ci/test", "state": "error"}], [], ("failing", "ci/test failing")),
        (
            [],
            [{"name": "build", "status": "completed", "conclusion": "failure"}],
            ("failing", "build failing"),
        ),
        (
            [],
            [{"name": "build", "status": "completed", "conclusion": "timed_out"}],
            ("failing", "build failing"),
        ),
        (
            [],
            [{"name": "build", "status": "completed", "conclusion": "cancelled"}],
            ("failing", "build failing"),
        ),
        ([{"context": "ci/test", "state": "pending"}], [], ("pending", "ci/test pending")),
        ([], [{"name": "build", "status": "in_progress", "conclusion": None}], ("pending", "build pending")),
        (
            [{"context": "ci/test", "state": "pending"}],
            [{"name": "build", "status": "completed", "conclusion": "failure"}],
            ("failing", "build failing"),
        ),
    ],
)
def test_commit_health(gh, statuses, runs, health):
    gh.statuses, gh.check_runs = statuses, runs
    got = gh.app.commit_health(REPO, "b" * 40)
    assert got[0] == (health if isinstance(health, str) else health[0])
    if not isinstance(health, str):
        assert got == health


@pytest.mark.parametrize(
    ("conclusion", "health"),
    [("action_required", ("failing", "build failing")), ("stale", ("pending", "build pending"))],
)
def test_commit_health_conclusions_that_are_not_passing(gh, conclusion, health):
    gh.check_runs = [{"name": "build", "status": "completed", "conclusion": conclusion}]
    assert gh.app.commit_health(REPO, "b" * 40) == health


def _paged(gh, statuses, runs):
    """A transport that serves 100 per page by `page`, as GitHub does; records the page asked for."""
    asked = []
    inner = gh.handler

    def handler(request):
        path = request.url.path
        page = int(request.url.params.get("page", 1))
        if path.endswith(("/status", "/check-runs")):
            per = int(request.url.params["per_page"])
            asked.append((path.rsplit("/", 1)[-1], page))
            if path.endswith("/status"):
                return httpx.Response(200, json={"statuses": statuses[(page - 1) * per : page * per]})
            return httpx.Response(200, json={"check_runs": runs[(page - 1) * per : page * per]})
        return inner(request)

    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    app = _github.GithubApp("12345", PRIVATE_PEM, client=client, clock=gh.clock_obj)
    return app, asked


def _passing(n):
    return [{"name": f"job-{i}", "status": "completed", "conclusion": "success"} for i in range(n)]


def test_a_failing_check_run_past_the_first_hundred_is_found(gh):
    runs = [*_passing(100), {"name": "late", "status": "completed", "conclusion": "failure"}]
    app, asked = _paged(gh, [], runs)
    assert app.commit_health(REPO, "b" * 40) == ("failing", "late failing")
    assert ("check-runs", 2) in asked


def test_a_pending_status_past_the_first_hundred_is_found(gh):
    statuses = [{"context": f"c{i}", "state": "success"} for i in range(100)]
    statuses.append({"context": "slow", "state": "pending"})
    app, asked = _paged(gh, statuses, [])
    assert app.commit_health(REPO, "b" * 40) == ("pending", "slow pending")
    assert ("status", 2) in asked


def test_an_exact_multiple_of_a_page_asks_for_one_more_and_stops(gh):
    app, asked = _paged(gh, [], _passing(200))
    assert app.commit_health(REPO, "b" * 40)[0] == "ok"
    assert [p for name, p in asked if name == "check-runs"] == [1, 2, 3]


def _suite(status, count, slug="github-actions"):
    return {"app": {"slug": slug}, "status": status, "latest_check_runs_count": count}


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_a_running_actions_suite_holds_green_runs_pending(gh, status):
    # A dependent job's check-run does not exist until its `needs:` finish, so the runs alone look done.
    gh.check_runs = _passing(3)
    gh.check_suites = [_suite(status, 3)]
    assert gh.app.commit_health(REPO, "b" * 40) == ("pending", f"github-actions check suite {status}")


def test_a_queued_suite_with_no_runs_is_ignored(gh):
    # Another App's suite that never posts a run stays queued forever.
    gh.check_runs = _passing(3)
    gh.check_suites = [_suite("queued", 0, "other-app")]
    assert gh.app.commit_health(REPO, "b" * 40)[0] == "ok"


def test_a_just_pushed_commit_with_only_a_queued_actions_suite_is_pending_not_none(gh):
    # The push webhook can start a deploy tick before Actions has queued the first job.
    gh.statuses, gh.check_runs = [], []
    gh.check_suites = [_suite("queued", 0)]
    assert gh.app.commit_health(REPO, "b" * 40) == ("pending", "github-actions check suite queued")


def test_another_apps_queued_suite_with_no_runs_leaves_a_commit_with_nothing_none(gh):
    gh.statuses, gh.check_runs = [], []
    gh.check_suites = [_suite("queued", 0, "other-app")]
    assert gh.app.commit_health(REPO, "b" * 40) == ("none", "no statuses or check-runs")


def test_a_running_suite_does_not_hide_a_failing_run(gh):
    gh.check_runs = [{"name": "build", "status": "completed", "conclusion": "failure"}]
    gh.check_suites = [_suite("in_progress", 1)]
    assert gh.app.commit_health(REPO, "b" * 40) == ("failing", "build failing")


def test_a_completed_suite_is_ok(gh):
    gh.check_runs = _passing(3)
    gh.check_suites = [_suite("completed", 3)]
    assert gh.app.commit_health(REPO, "b" * 40)[0] == "ok"


def test_a_suite_without_an_app_still_holds_pending(gh):
    gh.check_runs = _passing(1)
    gh.check_suites = [{"status": "queued", "latest_check_runs_count": 1}]
    assert gh.app.commit_health(REPO, "b" * 40) == ("pending", "check suite queued")


def test_unreadable_check_suites_leave_health_to_the_runs(gh):
    inner = gh.handler

    def handler(request):
        if request.url.path.endswith("/check-suites"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return inner(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.check_runs = _passing(2)
    assert gh.app.commit_health(REPO, "b" * 40) == ("ok", "2 passing")


def test_a_rate_limited_suites_listing_fails_the_tick(gh):
    inner = gh.handler

    def handler(request):
        if request.url.path.endswith("/check-suites"):
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        return inner(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.check_runs = _passing(2)
    with pytest.raises(_github.GithubError):
        gh.app.commit_health(REPO, "b" * 40)


def test_a_running_suite_past_the_first_hundred_is_found(gh):
    suites = [_suite("completed", 1) for _ in range(100)] + [_suite("in_progress", 1)]
    inner = gh.handler
    asked = []

    def handler(request):
        if request.url.path.endswith("/check-suites"):
            page, per = int(request.url.params["page"]), int(request.url.params["per_page"])
            asked.append(page)
            return httpx.Response(200, json={"check_suites": suites[(page - 1) * per : page * per]})
        return inner(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.check_runs = _passing(1)
    assert gh.app.commit_health(REPO, "b" * 40) == ("pending", "github-actions check suite in_progress")
    assert 2 in asked


def test_a_timeout_is_a_github_error_not_an_httpx_one(gh):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(timeout))
    with pytest.raises(_github.GithubError, match="ReadTimeout"):
        gh.app.installation_token(REPO)


def test_a_2xx_that_is_not_json_is_a_github_error(gh):
    """A captive portal or proxy answering 200 with HTML must not escape as a JSONDecodeError."""
    handler = gh.handler

    def html_repo(request):
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(200, text="<html>sign in to the wifi</html>")
        return handler(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(html_repo))
    with pytest.raises(_github.GithubError, match="not JSON") as e:
        gh.app.default_branch(REPO)
    assert e.value.status == 200


@pytest.mark.parametrize(
    "issued",
    [
        {"token": "ghs_secret", "expires_at": None},
        {"token": "ghs_secret", "expires_at": "next tuesday"},
        {"expires_at": "2027-01-01T00:00:00Z"},
    ],
)
def test_a_malformed_token_response_is_a_github_error_without_the_token(gh, issued):
    handler = gh.handler

    def malformed(request):
        if request.url.path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json=issued)
        return handler(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(malformed))
    with pytest.raises(_github.GithubError, match="unusable installation token") as e:
        gh.app.installation_token(REPO)
    assert "ghs_secret" not in str(e.value)


def _answering(gh, path, response):
    """`gh.app` with one endpoint replaced; the rest are the fake's."""
    handler = gh.handler

    def answer(request):
        return response if request.url.path == path else handler(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(answer))
    return gh.app


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        (f"/repos/{REPO}/installation", [1, 2], "id"),
        (f"/repos/{REPO}/installation", {"id": None}, "id"),
        ("/app/installations/77/access_tokens", ["ghs_secret"], "token"),
        ("/app/installations/77/access_tokens", {"token": ["ghs_secret"], "expires_at": "x"}, "token"),
        ("/app/installations/77/access_tokens", {"token": "ghs_secret", "expires_at": 1}, "expires_at"),
    ],
)
def test_a_wrong_shaped_token_answer_is_a_github_error_naming_the_field(gh, path, body, field):
    app = _answering(gh, path, httpx.Response(200, json=body))
    with pytest.raises(_github.GithubError, match=f"`{field}`") as e:
        app.installation_token(REPO)
    assert "ghs_secret" not in str(e.value)


@pytest.mark.parametrize("body", [["trunk"], {"default_branch": None}, {}])
def test_a_wrong_shaped_repo_answer_is_a_github_error_naming_the_field(gh, body):
    app = _answering(gh, f"/repos/{REPO}", httpx.Response(200, json=body))
    with pytest.raises(_github.GithubError, match="`default_branch`"):
        app.default_branch(REPO)
    with pytest.raises(_github.GithubError, match="`default_branch`"):
        app.default_branch_sha(REPO)


@pytest.mark.parametrize("body", [["a" * 40], {"sha": None}])
def test_a_wrong_shaped_commit_answer_is_a_github_error_naming_the_field(gh, body):
    app = _answering(gh, f"/repos/{REPO}/commits/trunk", httpx.Response(200, json=body))
    with pytest.raises(_github.GithubError, match="`sha`"):
        app.default_branch_sha(REPO)


def test_set_status_truncates_the_description_and_omits_an_empty_target(gh):
    gh.app.set_status(REPO, "c" * 40, "factory/verifier", "success", "d" * 300, "")
    posted = next(r for r in gh.requests if r.url.path.endswith(f"/statuses/{'c' * 40}"))
    body = json.loads(posted.content)
    assert len(body["description"]) == 140 and "target_url" not in body


def test_unreadable_check_runs_fall_back_to_statuses_and_say_so(gh):
    # Live (deploy-on-merge u5bc4c03363f83585): the App had statuses but not checks permission, and
    # the 403 on check-runs failed every tick, so nothing was ever auto-deployed.
    gh.checks_forbidden = True
    gh.statuses = [{"context": "ci/test", "state": "success"}]
    health, detail = gh.app.commit_health(REPO, "b" * 40)
    assert health == "ok"
    assert "check-runs unreadable" in detail and "Checks: read" in detail
    gh.statuses = [{"context": "ci/test", "state": "failure"}]
    assert gh.app.commit_health(REPO, "b" * 40)[0] == "failing"
    gh.statuses = []
    health, detail = gh.app.commit_health(REPO, "b" * 40)
    assert health == "none" and "check-runs unreadable" in detail


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (500, "boom"),
        (404, "Not Found"),
        # GitHub answers a rate limit with 403 too; that is not a missing permission, so it must not
        # quietly drop check-runs (a failing check would then be deployed over)
        (403, "API rate limit exceeded for installation ID 77."),
        (403, "You have exceeded a secondary rate limit."),
    ],
)
def test_other_check_run_errors_still_raise(gh, status, message):
    gh.statuses = [{"context": "ci/test", "state": "success"}]
    gh.checks_error = (status, message)
    with pytest.raises(_github.GithubError, match=f"HTTP {status}"):
        gh.app.commit_health(REPO, "b" * 40)


def test_a_renamed_repo_is_followed_to_its_new_name(gh):
    # The owner renamed the factory repo; until the profile names the new URL, GitHub answers 301.
    assert gh.app.default_branch_sha("acme/old-name") == ("trunk", "a" * 40)


def test_issue_comments_are_read_with_a_conditional_get():
    """A page GitHub answers 304 is the cached page: free against the App's rate limit (state step 1)."""
    clock = Clock()
    gh = FakeGithub(clock)
    comments = [{"id": n, "body": f"comment {n}"} for n in range(3)]
    seen = []

    def handler(request):
        if request.url.path == f"/repos/{REPO}/issues/12/comments":
            seen.append((request.headers.get("if-none-match"), request.url.params.get("since")))
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, json=comments, headers={"ETag": '"v1"'})
        return gh.handler(request)

    app = _github.GithubApp(
        "123",
        PRIVATE_PEM,
        client=httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler)),
        clock=clock,
    )
    assert app.issue_comments_since(REPO, 12) == comments
    assert app.issue_comments_since(REPO, 12) == comments
    assert app.issue_comments_since(REPO, 12, "2026-10-01T00:00:00+00:00") == comments
    assert seen == [(None, None), ('"v1"', None), (None, "2026-10-01T00:00:00+00:00")]


def test_delete_duplicate_comment_accepts_204_and_refreshes_auth():
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(401 if sum(m == "DELETE" for m, _ in calls) == 1 else 204)
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        return httpx.Response(201, json={"token": "token", "expires_at": iso(NOW + 3600)})

    app = _github.GithubApp(
        "1",
        PRIVATE_PEM,
        client=httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle)),
        clock=Clock(),
    )
    app.delete_comment(REPO, 42)
    assert calls.count(("DELETE", f"/repos/{REPO}/issues/comments/42")) == 2


def _app_answering(routes):
    def handle(request):
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 77})
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={"token": "token", "expires_at": iso(NOW + 3600)})
        return httpx.Response(200, json=routes[request.url.path, request.url.params.get("page", "1")])

    return _github.GithubApp(
        "1",
        PRIVATE_PEM,
        client=httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle)),
        clock=Clock(),
    )


def test_open_pulls_lists_every_page_with_their_changed_files():
    pulls = [{"number": n, "html_url": f"https://github.com/{REPO}/pull/{n}"} for n in range(1, 102)]
    routes = {
        (f"/repos/{REPO}/pulls", "1"): pulls[:100],
        (f"/repos/{REPO}/pulls", "2"): pulls[100:],
        (f"/repos/{REPO}/pulls/1/files", "1"): [{"filename": "a.py"}, {"filename": "b/c.py"}],
    }
    for n in range(2, 102):
        routes[f"/repos/{REPO}/pulls/{n}/files", "1"] = []
    found = _app_answering(routes).open_pulls(REPO)
    assert len(found) == 101
    assert found[0] == {
        "repo": REPO,
        "number": 1,
        "url": f"https://github.com/{REPO}/pull/1",
        "changed_files": ["a.py", "b/c.py"],
    }


def test_tracked_paths_are_the_blobs_of_the_default_branch_tree():
    tree = {"truncated": False, "tree": [{"path": "a.py", "type": "blob"}, {"path": "d", "type": "tree"}]}
    routes = {
        (f"/repos/{REPO}", "1"): {"default_branch": "main"},
        (f"/repos/{REPO}/git/trees/main", "1"): tree,
    }
    assert _app_answering(routes).tracked_paths(REPO) == {"a.py"}
    routes[f"/repos/{REPO}/git/trees/main", "1"] = {**tree, "truncated": True}
    with pytest.raises(_github.GithubError, match="truncated"):
        _app_answering(routes).tracked_paths(REPO)


def test_loop_reads_paginated_files_status_and_switch_history():
    routes = {
        (f"/repos/{REPO}/pulls/42", "1"): {"head": {"sha": "head"}},
        (f"/repos/{REPO}/pulls/42/files", "1"): [{"filename": f"f{i}"} for i in range(100)],
        (f"/repos/{REPO}/pulls/42/files", "2"): [{"filename": "last", "previous_filename": "old"}],
        (f"/repos/{REPO}/commits/head/status", "1"): {
            "statuses": [{"context": "factory/verifier", "state": "success"}]
        },
        (f"/repos/{REPO}/issues", "1"): [{"number": 9, "state": "closed"}],
        (f"/repos/{REPO}/issues/9/events", "1"): [{"event": "closed"}],
        (f"/repos/{REPO}/issues/events", "1"): [],
    }
    app = _app_answering(routes)
    assert app.pull(REPO, 42)["head"]["sha"] == "head"
    assert app.pull_files(REPO, 42)[-1]["previous_filename"] == "old"
    assert app.status_at(REPO, "head", "factory/verifier") == "success"
    assert app.status_at(REPO, "head", "absent") == "pending"
    assert app.labelled_issues(REPO, "loop-kill") == [{"number": 9, "state": "closed"}]
    assert app.issue_events(REPO, 9) == [{"event": "closed"}]


def test_loop_writes_use_the_installation_token_and_pin_the_head(gh):
    requests = []
    original = gh.handler

    def handler(request):
        if request.url.path.endswith(("/merge", "/labels", "/pulls/42")):
            requests.append(request)
            return httpx.Response(200, json={"merged": True})
        if request.url.path == "/app":
            return httpx.Response(200, json={"slug": "factory"})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    assert gh.app.app_login() == "factory[bot]"
    assert gh.app.merge_pull(REPO, 42, "head")["merged"]
    gh.app.add_labels(REPO, 42, ["loop:merged"])
    gh.app.close_pull(REPO, 42)
    assert [r.method for r in requests] == ["PUT", "POST", "PATCH"]
    assert [json.loads(r.content) for r in requests] == [
        {"sha": "head"},
        {"labels": ["loop:merged"]},
        {"state": "closed"},
    ]
    assert all(r.headers["authorization"] == "Bearer ghs_token1" for r in requests)


def test_removed_kill_label_is_still_discoverable():
    issue = {"number": 9, "state": "closed", "labels": []}
    app = _app_answering(
        {
            (f"/repos/{REPO}/issues", "1"): [],
            (f"/repos/{REPO}/issues/events", "1"): [
                {"event": "labeled", "label": {"name": "loop-kill"}, "issue": issue}
            ],
        }
    )
    assert app.labelled_issues(REPO, "loop-kill") == [issue]


def test_loop_can_update_its_decision_comment(gh):
    path = f"/repos/{REPO}/issues/comments/8"
    original = gh.handler
    requests = []

    def handler(request):
        if request.url.path == path:
            requests.append(request)
            return httpx.Response(200, json={"id": 8})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.app.update_comment(REPO, 8, "stale")
    request = requests[0]
    assert request.method == "PATCH" and json.loads(request.content) == {"body": "stale"}


def test_backlog_search_checks_exact_marker_and_all_states():
    marker = "<!-- loop-source: lesson:abc -->"
    app = _app_answering(
        {
            ("/search/issues", "1"): {
                "items": [
                    {"number": 1, "body": marker, "state": "closed"},
                    {"number": 2, "body": "lesson abc"},
                ],
                "incomplete_results": False,
            },
        }
    )
    assert [row["number"] for row in app.search_issues(REPO, marker)] == [1]


def test_backlog_create_uses_installation_token(gh):
    app = gh.app
    original = gh.handler
    calls = []

    def handler(request):
        if request.method == "POST" and request.url.path == f"/repos/{REPO}/issues":
            calls.append(request)
            return httpx.Response(201, json={"number": 1})
        return original(request)

    app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    assert app.create_issue(REPO, "Improve", "contract", ["loop"])["number"] == 1
    assert json.loads(calls[0].content) == {"title": "Improve", "body": "contract", "labels": ["loop"]}


@pytest.mark.parametrize(
    "status, expected", [("identical", True), ("behind", True), ("ahead", False), ("diverged", False)]
)
def test_commit_reachability_reads_current_default(gh, status, expected):
    original = gh.handler

    def handle(request):
        if request.url.path == f"/repos/{REPO}/compare/trunk...delivered":
            return httpx.Response(200, json={"status": status})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle))
    assert gh.app.commit_is_on_default_branch(REPO, "delivered") is expected
    assert any(request.url.path == f"/repos/{REPO}" for request in gh.requests)


def test_drift_commit_history_is_one_bounded_default_branch_page(gh):
    original = gh.handler
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == f"/repos/{REPO}/commits":
            assert request.url.params["sha"] == "trunk"
            assert request.url.params["per_page"] == "100"
            return httpx.Response(200, json=[{"sha": str(n)} for n in range(100)])
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle))
    assert len(gh.app.main_commits(REPO)) == 100
    assert len([request for request in requests if request.url.path == f"/repos/{REPO}/commits"]) == 1


@pytest.mark.parametrize("http_status", [404, 403, 500])
def test_commit_reachability_handles_a_removed_commit_but_preserves_api_errors(gh, http_status):
    original = gh.handler

    def handle(request):
        if "/compare/" in request.url.path:
            return httpx.Response(http_status, json={"message": "unavailable"})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle))
    if http_status == 404:
        assert gh.app.commit_is_on_default_branch(REPO, "removed") is False
    else:
        with pytest.raises(_github.GithubError) as caught:
            gh.app.commit_is_on_default_branch(REPO, "removed")
        assert caught.value.status == http_status


def test_drift_branch_heads_cost_one_call_per_named_branch(gh):
    original = gh.handler
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path.startswith(f"/repos/{REPO}/branches/"):
            name = request.url.path.removeprefix(f"/repos/{REPO}/branches/")
            if name == "gone":
                return httpx.Response(404, json={"message": "Branch not found"})
            commit = {"message": f"on {name}", "committer": {"date": "2026-01-01T00:00:00Z"}}
            return httpx.Response(200, json={"name": name, "commit": {"sha": "s", "commit": commit}})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle))
    heads = gh.app.branch_heads(REPO, ["claude/work", "gone"])
    assert [head["name"] for head in heads] == ["claude/work"]
    assert heads[0]["commit"]["message"] == "on claude/work"
    assert f"/repos/{REPO}/branches" not in requests
    assert [path for path in requests if "/branches/" in path] == [
        f"/repos/{REPO}/branches/claude/work",
        f"/repos/{REPO}/branches/gone",
    ]


def test_commit_statuses_are_the_latest_per_context(gh):
    gh.statuses = [{"context": "factory/board-drift", "state": "success", "description": "d"}]
    assert gh.app.commit_statuses(REPO, "a" * 40) == gh.statuses


@pytest.mark.parametrize("pages", [2, 10])
def test_bounded_issue_listing_filters_prs_and_logs_cap(gh, caplog, pages):
    calls = []
    original = gh.handler

    def handle(request):
        if request.url.path == f"/repos/{REPO}/issues":
            calls.append(request)
            assert request.url.params["state"] == "all"
            assert request.url.params["per_page"] == "100"
            page = int(request.url.params["page"])
            rows = [{"number": 100 * page + n, "state": "closed"} for n in range(100)]
            rows[0]["pull_request"] = {}
            return httpx.Response(200, json=rows if page < pages or pages == 10 else [])
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handle))
    issues = gh.app.repo_issues(REPO, max_pages=10)
    assert len(calls) == pages
    assert all("pull_request" not in issue for issue in issues)
    assert ("truncated" in caplog.text) is (pages == 10)
