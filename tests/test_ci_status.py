"""`ci-status`: read-only CI results for the session's branch, served by the supervisor (no network)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from flyte_agent_factory import _bridge, _ci_status, _github, _instructions, _session_setup
from flyte_agent_factory._pull_requests import PullRequests
from tests.test_github import PRIVATE_PEM, REPO, Clock, iso

BRANCH = "claude/proj"
HEAD = "b" * 40
SESSION_TOKEN_SHAPED = "ghp_" + "A" * 36


class Github:
    """The endpoints `ci-status` reads, as a MockTransport handler; records every request."""

    def __init__(self) -> None:
        """Start with no requests, no forbidden paths and a one-line job log."""
        self.requests: list[httpx.Request] = []
        self.forbidden: set[str] = set()  # path suffixes answered "Resource not accessible by integration"
        self.log = "line\n"
        self.minted = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one GitHub API request, recording it."""
        self.requests.append(request)
        path = request.url.path
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77})
        if path == "/app/installations/77/access_tokens":
            self.minted += 1
            return httpx.Response(
                201, json={"token": "ghs_installation_secret_token", "expires_at": iso(Clock()() + 3600)}
            )
        if any(path.endswith(suffix) for suffix in self.forbidden):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if path == f"/repos/{REPO}/commits/{BRANCH}":
            return httpx.Response(200, json={"sha": HEAD})
        if path == f"/repos/{REPO}/actions/runs":
            assert request.url.params["head_sha"] in (HEAD, "c" * 40)
            return httpx.Response(
                200,
                json={
                    "workflow_runs": [
                        {
                            "id": 5,
                            "name": "ci",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.com/acme/factory/actions/runs/5",
                        }
                    ]
                },
            )
        if path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "id": 9,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://github.com/acme/factory/actions/runs/5/job/9",
                        }
                    ]
                },
            )
        if path.endswith("/status"):
            return httpx.Response(
                200, json={"statuses": [{"context": "lint", "state": "success", "target_url": None}]}
            )
        if path == f"/repos/{REPO}/actions/jobs/9/logs":
            return httpx.Response(200, text=self.log)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github():
    """Provide a fake GitHub that answers the endpoints `ci-status` reads."""
    return Github()


@pytest.fixture
def service(bridge_root, github):
    """Build a `CiStatus` service for the branch's pull request, reading the fake GitHub."""
    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(github.handler))
    app = _github.GithubApp("12345", PRIVATE_PEM, client=client, clock=Clock())
    # CiStatus reads only app, repo and branch from the pull request
    pr = cast(PullRequests, SimpleNamespace(app=app, repo=REPO, branch=BRANCH))
    return _ci_status.CiStatus(bridge_root, lambda: pr, lambda *row: None, min_interval=0)


async def ask(service: Any, bridge_root: Path, **fields: Any) -> Any:
    """Write a request as the CLI would, let the service answer it, and return the answer."""
    request_id = f"ci-status-{len(list((bridge_root / 'requests').iterdir()))}"
    _bridge._write_atomic(bridge_root / "requests" / f"{request_id}.json", {"id": request_id, **fields})
    service.scan()
    await asyncio.gather(*service._pending)
    return json.loads((bridge_root / "answers" / f"{request_id}.json").read_text())


async def test_summary_lists_runs_checks_and_statuses_of_the_branch_head(service, bridge_root):
    """The summary lists the runs, checks and statuses of the branch head."""
    answer = await ask(service, bridge_root, sha=None, job=None)
    result = answer["result"]
    assert (result["repo"], result["branch"], result["sha"]) == (REPO, BRANCH, HEAD)
    assert result["runs"][0]["conclusion"] == "failure"
    assert result["runs"][0]["url"].endswith("/actions/runs/5")
    assert result["checks"][0]["id"] == 9
    assert result["statuses"] == [{"context": "lint", "state": "success", "url": ""}]
    assert result["missing"] == []


async def test_sha_option_reads_that_commit(service, bridge_root, github):
    """`--sha` reads that commit instead of the branch head."""
    answer = await ask(service, bridge_root, sha="c" * 40, job=None)
    assert answer["result"]["sha"] == "c" * 40
    assert not any(request.url.path.endswith(f"/commits/{BRANCH}") for request in github.requests)


async def test_logs_are_capped_and_redacted(service, bridge_root, github):
    """Job logs are capped in size and redacted."""
    github.log = (
        "\n".join(f"step {n}" for n in range(500))
        + f"\nleaked {SESSION_TOKEN_SHAPED} and ghs_installation_secret_token\n"
        + "x" * 100_000
    )
    result = (await ask(service, bridge_root, sha=None, job=9))["result"]
    assert result["truncated"] is True
    assert len(result["log"].encode()) <= _ci_status.LOG_MAX_BYTES
    assert len(result["log"].splitlines()) <= _ci_status.LOG_MAX_LINES
    assert SESSION_TOKEN_SHAPED not in result["log"]


async def test_logs_keep_the_last_lines_and_scrub_tokens(service, bridge_root, github):
    """Job logs keep their last lines and scrub tokens."""
    github.log = "\n".join(f"step {n}" for n in range(300)) + f"\n{SESSION_TOKEN_SHAPED}\n"
    result = (await ask(service, bridge_root, sha=None, job=9))["result"]
    lines = result["log"].splitlines()
    assert lines[-1] == "<redacted>"
    assert lines[0] == "step 101" and len(lines) == 200
    # The installation token is not in the log text, but one that is gets `***` from the app's own redact.
    github.log = "token ghs_installation_secret_token end"
    result = (await ask(service, bridge_root, sha=None, job=9))["result"]
    assert "ghs_installation_secret_token" not in result["log"]


async def test_a_request_cannot_choose_another_repo(service, bridge_root, github):
    """A request cannot choose another repo than the session's."""
    await ask(service, bridge_root, repo="evil/other", branch="main", sha=None, job=None)
    await ask(service, bridge_root, repo="evil/other", sha=None, job=9)
    paths = [r.url.path for r in github.requests if not r.url.path.startswith("/app")]
    assert paths and all(path.startswith(f"/repos/{REPO}/") for path in paths)


@pytest.mark.parametrize("sha", ["../../evil/repo", "main", "abc", "g" * 40])
async def test_a_sha_that_is_not_a_commit_is_refused_before_any_call(service, bridge_root, github, sha):
    """A sha that is not a commit is refused before any GitHub call."""
    answer = await ask(service, bridge_root, sha=sha, job=None)
    assert "--sha" in answer["error"]
    assert github.requests == []


async def test_only_get_requests_are_made(service, bridge_root, github):
    """Only GET requests are made."""
    await ask(service, bridge_root, sha=None, job=None)
    await ask(service, bridge_root, sha=None, job=9)
    assert {r.method for r in github.requests if not r.url.path.startswith("/app")} == {"GET"}


@pytest.mark.parametrize(
    ("forbidden", "missing"),
    [
        ("/actions/runs", ["Actions: read"]),
        ("/check-runs", ["Checks: read"]),
        ("/status", ["Commit statuses: read"]),
    ],
)
async def test_a_missing_permission_falls_back_and_says_what_is_missing(
    service, bridge_root, github, forbidden, missing
):
    """A missing permission falls back to what is readable and says what is missing."""
    github.forbidden = {forbidden}
    result = (await ask(service, bridge_root, sha=None, job=None))["result"]
    assert result["missing"] == missing
    assert sum(bool(result[key]) for key in ("runs", "checks", "statuses")) == 2


async def test_logs_without_actions_read_name_the_permission(service, bridge_root, github):
    """Logs without the Actions read permission name the permission."""
    github.forbidden = {"/logs"}
    answer = await ask(service, bridge_root, sha=None, job=9)
    assert "Actions: read" in answer["error"]


async def test_a_branch_not_pushed_yet_says_so(service, bridge_root, github):
    """A branch that is not pushed yet says so."""
    service._target().branch = "claude/unpushed"
    answer = await ask(service, bridge_root, sha=None, job=None)
    assert "push it first" in answer["error"]


async def test_no_token_or_key_reaches_an_answer_file(service, bridge_root, github):
    """No token or key reaches an answer file."""
    github.log = f"{SESSION_TOKEN_SHAPED}\n"
    await ask(service, bridge_root, sha=None, job=None)
    await ask(service, bridge_root, sha=None, job=9)
    await ask(service, bridge_root, sha="zz", job=None)
    assert github.minted == 1
    for path in (bridge_root / "answers").iterdir():
        text = path.read_text()
        assert "ghs_installation_secret_token" not in text
        assert SESSION_TOKEN_SHAPED not in text
        assert "PRIVATE KEY" not in text


async def test_repeat_requests_are_cached_and_others_wait_for_the_interval(bridge_root, github):
    """Repeat requests are served from the cache, and others wait for the minimum interval."""
    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(github.handler))
    pr = cast(
        PullRequests,
        SimpleNamespace(
            app=_github.GithubApp("1", PRIVATE_PEM, client=client, clock=Clock()), repo=REPO, branch=BRANCH
        ),
    )
    now = [0.0]
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    service = _ci_status.CiStatus(
        bridge_root, lambda: pr, lambda *row: None, min_interval=10, clock=lambda: now[0], sleep=sleep
    )
    await ask(service, bridge_root, sha=None, job=None)
    calls = len(github.requests)
    await ask(service, bridge_root, sha=None, job=None)
    assert len(github.requests) == calls and slept == []
    await ask(service, bridge_root, sha=None, job=9)
    assert slept == [10]


async def test_without_a_project_branch_the_answer_is_an_error(bridge_root):
    """Without a project branch the answer is an error."""
    service = _ci_status.CiStatus(bridge_root, lambda: None, lambda *row: None)
    answer = await ask(service, bridge_root, sha=None, job=None)
    assert "unavailable" in answer["error"]


async def test_the_cli_round_trip_prints_text_and_json(service, bridge_root, capsys):
    """The CLI round trip prints text, and JSON on request."""

    async def cli(*args: str) -> int:
        task = asyncio.create_task(asyncio.to_thread(_bridge.ci_status_cli, list(args)))
        while not task.done():
            service.scan()
            await asyncio.sleep(0.05)
        return int(await task)

    assert await cli("--timeout", "120") == 0
    out = capsys.readouterr().out
    assert "ci: failure" in out and "9 pytest: failure" in out and "lint: success" in out
    assert await cli("--json", "--timeout", "120") == 0
    assert json.loads(capsys.readouterr().out)["sha"] == HEAD
    assert await cli("--logs", "9", "--timeout", "120") == 0
    assert "job 9 of" in capsys.readouterr().out


async def test_the_ask_bridge_leaves_ci_status_requests_to_this_service(bridge_root):
    """The ask bridge leaves `ci-status` requests to this service."""
    _bridge._write_atomic(bridge_root / "requests" / "ci-status-1.json", {"id": "ci-status-1"})
    bridge = _bridge.Bridge(bridge_root)
    bridge._scan()
    assert not bridge._pending and not bridge.questions


def test_the_session_is_told_about_the_command_and_gets_a_shim(tmp_path):
    """The session is told about the command and gets a shim for it."""

    assert "`ci-status`" in _instructions._project_memory(False, name="proj")
    _session_setup._install_bridge_commands(tmp_path)
    assert ' ci-status "$@"' in (tmp_path / "ci-status").read_text()
