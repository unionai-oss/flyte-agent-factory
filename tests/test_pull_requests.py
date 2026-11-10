"""A project ends in a pull request opened by the factory's GitHub App.

GitHub is faked at the HTTP edge (`httpx.MockTransport`); git is real, against a local bare remote.
"""

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flyte.io import Dir

from flyte_agent_factory import (
    _bridge,
    _github,
    _instructions,
    _profile,
    _pull_requests,
    _review,
    _reviewer,
    _session_config,
    _session_setup,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
    agent,
)
from tests._wait import wait_for
from tests.test_grants import KEY_ID, SECRET

RealGithubApp = _github.GithubApp  # the autouse fixture below swaps the module attribute
REPO = "acme/widgets"
RUN_URL = "https://console.example/v2/domain/d/project/p/runs/r123"
SHA = "a" * 40
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
BRIEF = "# Add the widget\n\nBuild the widget and make it fast.\n\nMay edit: widget/\n"
ACCEPTANCE = "1. The widget renders\n2. The widget is fast\n"


class FakeGithub:
    """The endpoints a project's PR uses. `fail` maps a path suffix to an HTTP status (or an exception)."""

    app: _github.GithubApp  # attached by the `gh` fixture
    rows: list[str]
    prs: _pull_requests.PullRequests

    def __init__(self, *, installed=True):
        self.installed = installed
        self.requests: list[httpx.Request] = []
        self.pulls: list[dict[str, Any]] = []
        self.fail: dict[str, int | Exception] = {}
        self.fail_once: dict[str, int] = {}  # path suffix -> status, for the next request only
        self.no_drafts = False

    def sent(self, method: str, suffix: str) -> list[dict[str, Any]]:
        return [
            json.loads(r.content or b"{}")
            for r in self.requests
            if r.method == method and r.url.path.endswith(suffix)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        for suffix in [x for x in self.fail_once if path.endswith(x)]:
            return httpx.Response(self.fail_once.pop(suffix), json={"message": "Bad credentials"})
        if self.no_drafts and method == "POST" and path == f"/repos/{REPO}/pulls" and body.get("draft"):
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "Draft pull requests are not supported in this repository."}],
                },
            )
        for suffix, failure in self.fail.items():
            if path.endswith(suffix):
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(failure, json={"message": "boom ghs_secret1"})
        if path == f"/repos/{REPO}/installation":
            return httpx.Response(200, json={"id": 77}) if self.installed else httpx.Response(404, json={})
        if path == "/app/installations/77/access_tokens":
            return httpx.Response(201, json={"token": "ghs_secret1", "expires_at": "2099-01-01T00:00:00Z"})
        if path == f"/repos/{REPO}":
            return httpx.Response(200, json={"default_branch": "trunk"})
        if path == f"/repos/{REPO}/pulls" and method == "GET":
            head = request.url.params["head"]
            return httpx.Response(200, json=[p for p in self.pulls if p["head_ref"] == head])
        if path == f"/repos/{REPO}/pulls" and method == "POST":
            pull = {
                "number": 5,
                "node_id": "PR_node5",
                "html_url": f"https://github.com/{REPO}/pull/5",
                "head_ref": f"acme:{body['head']}",
                **body,
            }
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)
        if path == f"/repos/{REPO}/pulls/5" and method == "PATCH":
            self.pulls[0].update(body)
            return httpx.Response(200, json=self.pulls[0])
        if path == "/graphql" or path.endswith("/comments") or "/statuses/" in path:
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": "unexpected"})


@pytest.fixture
def gh():
    fake = FakeGithub()
    client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(fake.handler))
    fake.app = RealGithubApp("12345", PRIVATE_PEM, client=client)
    fake.rows = []
    fake.prs = _pull_requests.PullRequests(
        fake.app, REPO, "widget", RUN_URL, lambda icon, text: fake.rows.append(text)
    )
    return fake


class _OfflineApp:
    def __init__(self, *args, **kwargs):
        pass

    def default_branch(self, repo):
        raise _github.GithubError("offline")


@pytest.fixture(autouse=True)
def _no_pull_requests_between_tests(monkeypatch):
    monkeypatch.setattr(_session_setup, "_pr", None)
    monkeypatch.setattr(_session_setup, "_default_branch", "main")
    monkeypatch.setattr(_session_setup, "_branch_sync_problem", "")
    monkeypatch.setattr(_github, "GithubApp", _OfflineApp)  # no test reaches api.github.com


# --- titles and bodies ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("brief", "title"),
    [
        ("# Add the widget\n\nmore", "Add the widget"),
        ("\n\n  Add the widget  \nmore", "Add the widget"),
        ("## **Add** the widget", "**Add** the widget"),
        ("", "Factory project"),
        ("x" * 500, "x" * 119 + "…"),
    ],
)
def test_title_comes_from_the_briefs_first_line(brief, title):
    assert _pull_requests.split_brief(brief)[0] == title


def test_body_has_summary_run_and_criteria_and_is_bounded():
    body = _pull_requests.pull_body(BRIEF, ACCEPTANCE, RUN_URL)
    assert "Build the widget and make it fast." in body
    assert f"Run: {RUN_URL}" in body
    assert "## Acceptance criteria" in body and "2. The widget is fast" in body
    assert "Add the widget" not in body  # the title is the title, not repeated
    huge = _pull_requests.pull_body("# t\n" + "b" * 50_000, "c" * 50_000, RUN_URL)
    assert len(huge) < 15_000 and "truncated" in huge


# --- draft PR: opened once, then updated -----------------------------------------------------------


def test_the_first_done_opens_a_draft_pr_from_the_project_branch(gh):
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == f"https://github.com/{REPO}/pull/5"
    (created,) = gh.sent("POST", "/pulls")
    assert created["head"] == "factory/widget"
    assert created["base"] == "trunk"  # the repo's default branch, not a guess
    assert created["draft"] is True
    assert created["title"] == "Add the widget"
    assert RUN_URL in created["body"] and "The widget renders" in created["body"]
    # Authenticated as the installation, not as the App.
    assert gh.requests[-1].headers["authorization"] == "Bearer ghs_secret1"


def test_a_second_done_updates_the_open_pr_and_never_opens_another(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    gh.prs.publish("# A better title\n\nnew summary", ACCEPTANCE + "3. new\n")
    assert len(gh.sent("POST", "/pulls")) == 1
    (patched,) = gh.sent("PATCH", "/pulls/5")
    assert patched["title"] == "A better title" and "3. new" in patched["body"]
    assert gh.pulls[0]["draft"] is True  # an update does not undraft


def test_a_resumed_run_finds_the_existing_pr_by_head(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    fresh = _pull_requests.PullRequests(gh.app, REPO, "widget", RUN_URL, lambda *_: None)
    fresh.publish(BRIEF, ACCEPTANCE)
    assert len(gh.sent("POST", "/pulls")) == 1
    (query,) = [r for r in gh.requests if r.method == "GET" and r.url.path.endswith("/pulls")][-1:]
    assert query.url.params["head"] == "acme:factory/widget" and query.url.params["state"] == "open"


# --- review verdict ------------------------------------------------------------------------------


def test_the_pr_title_body_and_verdict_comment_carry_no_credential(gh):
    live = "a-live-granted-value-0123456789"
    source = lambda: [live]  # noqa: E731 -- one value, and the same object must be unregistered
    _bridge.register_redaction_source(source)
    try:
        brief = f"# Fix aws_secret_access_key={SECRET}\n\nSummary.\n"
        acceptance = f"1. Never prints {live} or {KEY_ID}\n"
        gh.prs.publish(brief, acceptance)
        gh.prs.passed(SHA, 1, f"verdict: {live} {KEY_ID}")
        gh.prs.failed(SHA, f"escalated: {live}")
    finally:
        _bridge.unregister_redaction_source(source)
    (created,) = gh.sent("POST", "/pulls")
    (posted,) = gh.sent("POST", "/issues/5/comments")
    statuses = gh.sent("POST", f"/statuses/{SHA}")
    sent = [created["title"], created["body"], posted["body"], *(st["description"] for st in statuses)]
    for text in sent:
        assert live not in text and SECRET not in text and KEY_ID not in text
    assert all("<redacted" in text for text in (created["title"], created["body"], posted["body"]))
    assert "<redacted" in statuses[-1]["description"]


def test_a_pass_marks_ready_comments_and_succeeds_on_the_reviewed_sha(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    comment = _pull_requests.verdict_comment(
        2,
        SHA,
        RUN_URL,
        [{"id": "C1", "text": "The widget renders"}, {"id": "C2", "text": "The widget is fast"}],
        {"C1": "PASS", "C2": "FAIL"},
    )
    gh.prs.passed(SHA, 2, comment)

    (graphql,) = gh.sent("POST", "/graphql")
    assert "markPullRequestReadyForReview" in graphql["query"]
    assert graphql["variables"] == {"id": "PR_node5"}  # the node_id; REST has no such call
    (posted,) = gh.sent("POST", "/issues/5/comments")
    assert "passed** in 2 round(s)" in posted["body"] and RUN_URL in posted["body"]
    assert "C1: The widget renders" in posted["body"] and "C2" not in posted["body"]
    (status,) = gh.sent("POST", f"/statuses/{SHA}")
    assert status["state"] == "success" and status["context"] == "factory/verifier"
    assert status["target_url"] == RUN_URL
    assert "2 round" in status["description"] and len(status["description"]) <= 140
    assert not [r for r in gh.requests if r.method == "PUT"]  # never merged


def test_a_failure_posts_failure_on_the_sha_and_leaves_the_pr_a_draft(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    gh.prs.failed(SHA, "review did not converge in 3 rounds " + "x" * 200)
    (status,) = gh.sent("POST", f"/statuses/{SHA}")
    assert status["state"] == "failure" and status["context"] == "factory/verifier"
    assert len(status["description"]) == 140
    assert not gh.sent("POST", "/graphql")


def test_a_pass_after_a_restart_finds_the_pr_it_did_not_open(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    fresh = _pull_requests.PullRequests(gh.app, REPO, "widget", RUN_URL, lambda *_: None)
    fresh.passed(SHA, 1, "ok")
    assert gh.sent("POST", "/graphql")[0]["variables"] == {"id": "PR_node5"}


# --- GitHub failing never stops the run --------------------------------------------------------------


def test_a_5xx_on_publish_is_a_row_naming_the_repo_and_status_without_the_token(gh):
    gh.fail["/pulls"] = 503
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == ""
    (row,) = gh.rows
    assert REPO in row and "HTTP 503" in row
    assert "ghs_secret1" not in row


def test_a_timeout_is_a_row_not_an_exception(gh):
    gh.fail["/pulls"] = httpx.ReadTimeout("slow")
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == ""
    assert any(REPO in row and "ReadTimeout" in row for row in gh.rows)


def test_each_verdict_call_is_independent(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    gh.fail["/graphql"] = 502
    gh.fail["/comments"] = 500
    gh.prs.passed(SHA, 1, "ok")
    assert gh.sent("POST", f"/statuses/{SHA}")[0]["state"] == "success"  # still posted
    assert sum("HTTP 502" in r for r in gh.rows) == 1 and sum("HTTP 500" in r for r in gh.rows) == 1


def test_graphql_errors_in_a_200_are_failures(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    original = gh.handler

    def handler(request):
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"errors": [{"message": "not allowed"}]})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.prs.passed(SHA, 1, "ok")
    assert any("not allowed" in r for r in gh.rows)


def test_a_missing_installation_is_a_clear_row_naming_the_repo(gh):
    gh.installed = False
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == ""
    assert any(f"not installed on {REPO}" in row for row in gh.rows)


def test_a_bad_private_key_is_a_row_not_a_crash(gh):
    gh.app._key = "not a pem"
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == ""
    assert len(gh.rows) == 1 and REPO in gh.rows[0]


# --- credentials stay in the supervisor ----------------------------------------------------------------


def test_the_session_env_never_holds_the_app_key_or_a_token():
    env = _session_setup._session_env(
        {
            "GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",  # gitleaks:allow
            "GITHUB_TOKEN": "ghs_a",
            "GH_TOKEN": "ghs_b",
            "GITHUB_INSTALLATION_TOKEN": "ghs_c",
            "PATH": "/usr/bin",
            # The session pushes with the key file `install_deploy_key` wrote, not with the value.
            "GIT_DEPLOY_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----",
        }
    )
    assert env == {"PATH": "/usr/bin"}


def test_the_supervisor_takes_the_key_out_of_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "pem")
    assert _pull_requests.take_app_key() == "pem"
    assert "GITHUB_APP_PRIVATE_KEY" not in os.environ
    assert _pull_requests.take_app_key() == ""


PREFIXED = "_UNION_MY_GITHUB_APP_PRIVATE_KEY"  # Flyte also injects each secret as <prefix><KEY UPPER>


def _example_profile(monkeypatch):
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))


def test_the_session_env_also_drops_the_prefixed_copy_of_the_app_key(monkeypatch):
    _example_profile(monkeypatch)
    env = _session_setup._session_env(
        {"PATH": "/usr/bin", PREFIXED: "fake-pem", "GITHUB_APP_PRIVATE_KEY": "fake-pem"}
    )
    assert env == {"PATH": "/usr/bin"}


def test_the_prefix_comes_from_flyte_secrets_env_prefix(monkeypatch):
    _example_profile(monkeypatch)
    env = {"FLYTE_SECRETS_ENV_PREFIX": "_X_", "_X_MY_GITHUB_APP_PRIVATE_KEY": "fake-pem", PREFIXED: "kept"}
    assert _session_setup._session_env(env) == {"FLYTE_SECRETS_ENV_PREFIX": "_X_", PREFIXED: "kept"}


def test_the_supervisor_takes_both_copies_of_the_key_out_of_the_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-pem")
    monkeypatch.setenv(PREFIXED, "fake-pem")
    assert _pull_requests.take_app_key("my_github_app_private_key") == "fake-pem"
    assert "GITHUB_APP_PRIVATE_KEY" not in os.environ and PREFIXED not in os.environ


def test_a_key_that_only_arrived_prefixed_is_still_taken(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setenv(PREFIXED, "fake-pem")
    assert _pull_requests.take_app_key("my_github_app_private_key") == "fake-pem"
    assert PREFIXED not in os.environ


@pytest.mark.asyncio
async def test_no_child_environment_holds_either_copy_of_the_key(monkeypatch, tmp_path):
    _example_profile(monkeypatch)
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fake-pem")
    monkeypatch.setenv(PREFIXED, "fake-pem")
    assert _session_config._PROFILE is not None
    _pull_requests.take_app_key(_session_config._PROFILE.github.private_key_secret)
    for env in (
        _session_setup._session_env(dict(os.environ)),
        _session_setup._session_env(_session_setup._export_runtime_env()),
    ):
        assert "GITHUB_APP_PRIVATE_KEY" not in env and PREFIXED not in env
    assert not [v for v in os.environ.values() if v == "fake-pem"]


def test_declared_checks_and_sub_agent_processes_start_from_the_filtered_env():
    import inspect

    # Every place the supervisor builds an environment for a child goes through `_session_env`.
    for func in (agent.subagent.func, _supervise_support._run_declared_checks, agent.project.func):
        assert "_session_env(" in inspect.getsource(func)


def test_the_sub_agent_task_mounts_no_github_secret():
    secrets = cast("list[Any]", agent.subagent.secrets or [])  # the task declares a list of Secret objects
    keys = [getattr(s, "key", s) for s in secrets]
    assert not [k for k in keys if "github" in str(k).lower()]
    assert "GITHUB_APP_PRIVATE_KEY" not in [getattr(s, "as_env_var", "") for s in secrets]


# --- the profile mounts the key on slots --------------------------------------------------------------

EXAMPLE = Path(__file__).resolve().parents[1] / "profiles" / "example.toml"


def test_a_slot_mounts_the_app_key_only_when_the_profile_has_github():
    with_github = _profile.parse(EXAMPLE.read_text(), "example.toml")
    assert with_github.github.configured
    assert with_github.agent_secrets.split(",")[-1] == "my_github_app_private_key=GITHUB_APP_PRIVATE_KEY"
    text = EXAMPLE.read_text().replace('private_key_secret = "my_github_app_private_key"\n', "")
    without = _profile.parse(text.replace('app_id = "123456"\n', ""), "example.toml")
    assert not without.github.configured
    assert "GITHUB_APP_PRIVATE_KEY" not in without.agent_secrets


def test_the_deploy_refusal_checks_still_pass_with_the_key_mounted():
    from flyte_agent_factory import _deploy

    profile = _profile.parse(EXAMPLE.read_text(), "example.toml")
    for slot in profile.slots:
        _deploy.check_recipe(profile, _deploy.recipe_env(profile, slot.env, {}))


def test_the_agent_image_carries_pyjwt_with_crypto():
    from flyte_agent_factory._images import agent_image

    for browser in (False, True):
        packages = [
            p for layer in agent_image(browser=browser)._layers for p in getattr(layer, "packages", ())
        ]
        assert any(p.startswith("pyjwt[crypto]") for p in packages)


# --- the branch and what the session is told -------------------------------------------------------------


def test_branch_names_are_factory_slash_project():
    assert _pull_requests.branch_name("widget") == "factory/widget"


def test_guidance_names_the_project_branch_and_merges_the_default_branch():
    text = _instructions._project_memory(
        False, name="widget", branch="factory/widget", default_branch="trunk"
    )
    assert "`factory/widget`" in text
    assert _instructions._push_command("factory/widget", "trunk") in text
    assert "git merge --no-edit origin/trunk" in text
    assert "git push -u origin HEAD:refs/heads/factory/widget" in text
    for forbidden in ("--force", "rebase", "origin/main", "git push origin HEAD"):
        assert forbidden not in text
    assert "supervisor opens a draft pull request" in text


def test_guidance_without_github_is_what_it_always_was():
    text = _instructions._project_memory(False, name="widget")
    assert "git fetch origin && git rebase origin/main && git push origin HEAD" in text
    # No project branch (`factory/<name>`); the package path `flyte_agent_factory/` is not a branch.
    assert not re.search(r"(?<![\w])factory/", text)
    assert "force-with-lease" not in text and "--no-edit" not in text
    assert text == _instructions._project_memory(False, name="widget", branch="", default_branch="trunk")


def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """A bare `origin` whose default branch is `trunk`, with one commit."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "trunk")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("hi\n")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(seed), str(bare))
    git(bare, "symbolic-ref", "HEAD", "refs/heads/trunk")
    return bare


def clone_to(tmp_path, remote, name):
    work = tmp_path / name
    git(tmp_path, "clone", str(remote), str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "t")
    return work


@pytest.mark.asyncio
async def test_a_new_project_branches_from_the_default_branch(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "factory/widget"
    assert git(work, "rev-parse", "HEAD") == git(work, "rev-parse", "origin/trunk")
    assert _session_setup._default_branch == "trunk"


@pytest.mark.asyncio
async def test_a_resumed_project_keeps_the_branch_from_the_remote(tmp_path, remote, monkeypatch):
    first = clone_to(tmp_path, remote, "first")
    git(first, "checkout", "-b", "factory/widget")
    (first / "widget.py").write_text("x = 1\n")
    git(first, "add", ".")
    git(first, "commit", "-m", "work")
    git(first, "push", "-u", "origin", "factory/widget")

    second = clone_to(tmp_path, remote, "second")  # a pod with nothing but the clone
    monkeypatch.setattr(_session_config, "WORKSPACE", second)
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    assert (second / "widget.py").is_file()
    assert git(second, "rev-parse", "--abbrev-ref", "@{u}") == "origin/factory/widget"
    # Already there: a second call changes nothing.
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())


@pytest.mark.asyncio
async def test_preflight_checks_the_project_branch_is_pushed(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    monkeypatch.setattr(
        _session_setup,
        "_pr",
        _pull_requests.PullRequests(cast(Any, None), REPO, "widget", "", lambda *_: None),
    )

    problem = await _supervise_support._done_preflight(work)
    assert "branch `factory/widget` has no upstream" in problem
    assert _instructions._push_command("factory/widget", "trunk") in problem
    assert "--force" not in problem and "rebase" not in problem

    git(work, "push", "-u", "origin", "factory/widget")
    (work / "more.txt").write_text("more\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "more")
    assert "1 commit(s) are not pushed" in await _supervise_support._done_preflight(work)
    git(work, "push")
    assert "not pushed" not in await _supervise_support._done_preflight(work)


@pytest.mark.asyncio
async def test_preflight_without_github_says_what_it_always_said(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    git(work, "checkout", "-b", "somebranch")
    problem = await _supervise_support._done_preflight(work)
    assert (
        "branch `somebranch` has no upstream, so nothing is pushed: "
        "`git fetch origin && git rebase origin/main && git push -u origin HEAD`."
    ) in problem


@pytest.mark.asyncio
async def test_start_pull_requests_is_off_without_the_profile_table(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(
        _session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text().split("[github]")[0], "x.toml")
    )
    await _session_setup._start_pull_requests("git@github.com:acme/widgets.git", "pem", _timeline._Timeline())
    assert _session_setup._pr is None
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"  # still on the default branch


@pytest.mark.asyncio
async def test_start_pull_requests_switches_branch_when_configured(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))
    timeline = _timeline._Timeline()
    await _session_setup._start_pull_requests("git@github.com:acme/widgets.git", PRIVATE_PEM, timeline)
    assert _session_setup._pr is not None and _session_setup._pr.repo == "acme/widgets"
    assert timeline.branch == "factory/widget"
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "factory/widget"


@pytest.mark.asyncio
async def test_a_missing_key_or_a_non_github_repo_leaves_the_project_as_it_was(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))
    timeline = _timeline._Timeline()
    await _session_setup._start_pull_requests("git@github.com:acme/widgets.git", "", timeline)
    await _session_setup._start_pull_requests("git@gitlab.com:acme/widgets.git", PRIVATE_PEM, timeline)
    assert _session_setup._pr is None
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "trunk"


# --- the supervisor loop: DONE -> draft PR -> review -> verdict ----------------------------------------


@pytest.fixture
def done_project(tmp_path, remote, gh, monkeypatch):
    """A pushed project branch whose checks pass, with `gh` wired in as the App."""
    from tests.test_nudge import _Proc, _Queue

    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_project_brief", BRIEF)
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.001)
    monkeypatch.setattr(_session_config, "_applicable_standards", dict)
    git(work, "checkout", "-b", "factory/widget")
    (work / "widget").mkdir()
    (work / "widget/checks.sh").write_text("exit 0\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "work")
    git(work, "push", "-u", "origin", "factory/widget")
    criteria = work / _session_config._acceptance_rel("widget")
    criteria.parent.mkdir(parents=True, exist_ok=True)
    criteria.write_text(ACCEPTANCE)
    monkeypatch.setattr(_session_setup, "_pr", gh.prs)

    async def flush(*args, **kwargs):
        return None

    monkeypatch.setattr(_timeline._Timeline, "flush", flush)
    return work, _Queue(), _Proc(), _timeline._Timeline()


async def run_until(work, bridge, proc, timeline, done):
    stop = asyncio.Event()
    task = asyncio.create_task(_supervise._pump(bridge, proc, timeline, stop, workspace=work, brief=BRIEF))
    try:
        await wait_for(done)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


@pytest.mark.asyncio
async def test_a_passing_review_readies_the_pr_and_succeeds_on_the_reviewed_sha(
    done_project, gh, monkeypatch
):
    work, bridge, proc, timeline = done_project
    head = git(work, "rev-parse", "HEAD")
    timeline.run_url = RUN_URL
    timeline.row("📝", "C1: PASS the widget renders")

    async def review(*args):
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    bridge.push("DONE: delivered")
    await run_until(work, bridge, proc, timeline, lambda: gh.sent("POST", f"/statuses/{head}"))

    assert gh.sent("POST", "/pulls")[0]["draft"] is True
    assert gh.sent("POST", "/graphql") and gh.sent("POST", "/issues/5/comments")
    (status,) = gh.sent("POST", f"/statuses/{head}")
    assert status["state"] == "success"
    assert timeline.pr_url == f"https://github.com/{REPO}/pull/5"
    assert "C1: The widget renders" in gh.sent("POST", "/issues/5/comments")[0]["body"]
    data = json.loads(timeline.render().split('id="factory-status">')[1].split("</script>")[0])
    assert data["deliverables"]["pr"] == timeline.pr_url


@pytest.mark.asyncio
async def test_an_escalated_review_posts_failure_and_never_readies(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    head = git(work, "rev-parse", "HEAD")

    async def review(*args):
        return False, "FINDING: broken", False, "circling"

    monkeypatch.setattr(_review, "_review_round", review)
    monkeypatch.setattr(_session_config, "_max_rounds", lambda: 1)
    bridge.push("DONE: delivered")
    await run_until(work, bridge, proc, timeline, lambda: len(gh.sent("POST", f"/statuses/{head}")) >= 1)

    states = [s["state"] for s in gh.sent("POST", f"/statuses/{head}")]
    assert states and set(states) == {"failure"}
    assert "did not converge" in gh.sent("POST", f"/statuses/{head}")[-1]["description"]
    assert not gh.sent("POST", "/graphql") and not gh.sent("POST", "/issues/5/comments")


@pytest.mark.asyncio
async def test_github_down_never_blocks_review(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    gh.fail["/pulls"] = 503
    gh.fail["/statuses/" + git(work, "rev-parse", "HEAD")] = httpx.ConnectTimeout("down")
    reviewed = []

    async def review(*args):
        reviewed.append(args)
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    bridge.push("DONE: delivered")
    await run_until(
        work, bridge, proc, timeline, lambda: any("passed your work" in m for m in proc.submitted)
    )

    assert reviewed
    assert any("passed your work" in m for m in proc.submitted)  # the session was told it passed
    rows = "\n".join(gh.rows)  # `PullRequests` reports through the timeline's `row` in production
    assert REPO in rows and "HTTP 503" in rows and "ConnectTimeout" in rows
    assert timeline.pr_url == ""


@pytest.mark.asyncio
async def test_without_github_a_done_review_touches_no_pull_request_code(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    monkeypatch.setattr(_session_setup, "_pr", None)

    async def review(*args):
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    bridge.push("DONE: delivered")
    await run_until(
        work, bridge, proc, timeline, lambda: any("passed your work" in m for m in proc.submitted)
    )
    assert gh.requests == [] and timeline.pr_url == ""


# --- the guided push: merge-based, writes only the project branch ---------------------------------------


def run_guidance(work, default="trunk", branch="factory/widget"):
    """Run the command the session is told to run, in `work`."""
    command = _instructions._push_command(branch, default)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["sh", "-c", command], check=False, cwd=work, env=env, capture_output=True, text=True
    )


def commit_file(work, name):
    (work / name).write_text(name + "\n")
    git(work, "add", ".")
    git(work, "commit", "-m", name)
    return git(work, "rev-parse", "HEAD")


def remote_files(remote, ref):
    return set(git(remote, "ls-tree", "-r", "--name-only", ref).split())


@pytest.fixture
def project_work(tmp_path, remote, monkeypatch):
    work = clone_to(tmp_path, remote, "work")
    monkeypatch.setattr(_session_config, "WORKSPACE", work)
    return work


@pytest.mark.asyncio
async def test_guidance_survives_a_checkpoint_restore_without_losing_remote_work(project_work, remote):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    w1 = commit_file(work, "w1.txt")
    assert run_guidance(work).returncode == 0
    commit_file(work, "w2.txt")
    assert run_guidance(work).returncode == 0
    git(work, "reset", "--hard", w1)  # the workspace snapshot predates w2
    commit_file(work, "w3.txt")
    result = run_guidance(work)
    assert result.returncode == 0, result.stderr
    assert {"w1.txt", "w2.txt", "w3.txt"} <= remote_files(remote, "factory/widget")


@pytest.mark.asyncio
async def test_guided_push_keeps_a_commit_a_human_pushed_to_the_branch(tmp_path, project_work, remote):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    commit_file(work, "mine.txt")
    assert run_guidance(work).returncode == 0
    human = clone_to(tmp_path, remote, "human")
    git(human, "checkout", "factory/widget")
    commit_file(human, "human.txt")
    git(human, "push", "origin", "factory/widget")
    commit_file(work, "mine2.txt")
    result = run_guidance(work)
    assert result.returncode == 0, result.stderr
    assert {"mine.txt", "mine2.txt", "human.txt"} <= remote_files(remote, "factory/widget")


@pytest.mark.asyncio
async def test_guided_push_recreates_a_branch_deleted_after_its_pr_merged(project_work, remote):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    commit_file(work, "one.txt")
    assert run_guidance(work).returncode == 0
    git(remote, "branch", "-D", "factory/widget")  # "delete head branch" after the merge
    commit_file(work, "two.txt")
    result = run_guidance(work)
    assert result.returncode == 0, result.stderr
    assert {"one.txt", "two.txt"} <= remote_files(remote, "factory/widget")


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["detached", "on-default"])
async def test_guided_push_never_updates_the_default_branch(project_work, remote, position):
    work = project_work
    trunk_before = git(remote, "rev-parse", "trunk")
    if position == "detached":
        git(work, "checkout", "--detach")
    commit_file(work, "stray.txt")
    result = run_guidance(work)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "trunk") == trunk_before
    assert "stray.txt" in remote_files(remote, "factory/widget")


@pytest.mark.parametrize("branch", ["factory/widget"])
def test_no_guidance_text_can_force_push(branch):
    for text in (
        _instructions._project_memory(False, name="widget", branch=branch, default_branch="trunk"),
        _instructions._push_command(branch, "trunk"),
    ):
        assert "--force" not in text and " -f " not in text and "+HEAD" not in text


# --- resuming: a local branch behind (or apart from) its remote is brought up to date -------------------


@pytest.mark.asyncio
async def test_a_resumed_branch_behind_its_remote_is_fast_forwarded(tmp_path, project_work, remote):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    commit_file(work, "a.txt")
    assert run_guidance(work).returncode == 0
    other = clone_to(tmp_path, remote, "other")  # a later pod pushed more
    git(other, "checkout", "factory/widget")
    commit_file(other, "b.txt")
    git(other, "push", "origin", "factory/widget")

    timeline = _timeline._Timeline()
    assert await _session_setup._checkout_project_branch("factory/widget", timeline)
    assert (work / "b.txt").is_file()
    assert not _session_setup._branch_sync_problem


@pytest.mark.asyncio
async def test_a_resumed_branch_that_diverged_is_merged(tmp_path, project_work, remote):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    commit_file(work, "a.txt")
    assert run_guidance(work).returncode == 0
    other = clone_to(tmp_path, remote, "other")
    git(other, "checkout", "factory/widget")
    commit_file(other, "b.txt")
    git(other, "push", "origin", "factory/widget")
    commit_file(work, "c.txt")  # local work the remote has not seen

    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    assert (work / "b.txt").is_file() and (work / "c.txt").is_file()
    assert not _session_setup._branch_sync_problem


@pytest.mark.asyncio
async def test_a_resume_that_cannot_catch_up_says_so_in_a_row_and_in_the_guidance(
    tmp_path, project_work, remote
):
    work = project_work
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    commit_file(work, "a.txt")
    assert run_guidance(work).returncode == 0
    other = clone_to(tmp_path, remote, "other")
    git(other, "checkout", "factory/widget")
    (other / "a.txt").write_text("theirs\n")
    git(other, "commit", "-am", "conflicting")
    git(other, "push", "origin", "factory/widget")
    (work / "a.txt").write_text("mine\n")
    git(work, "commit", "-am", "also conflicting")

    timeline = _timeline._Timeline()
    assert await _session_setup._checkout_project_branch("factory/widget", timeline)
    assert git(work, "status", "--porcelain") == ""  # the failed merge was aborted, not left half done
    rows = " ".join(text for _, _, text in timeline._rows)
    assert "could not bring factory/widget up to date" in rows
    assert _session_setup._branch_sync_problem
    text = _instructions._project_memory(
        False,
        name="widget",
        branch="factory/widget",
        default_branch="trunk",
        branch_problem=_session_setup._branch_sync_problem,
    )
    assert "could not bring factory/widget up to date" in text


# --- the branch's base and the default branch ------------------------------------------------------------


def point_remote_head_elsewhere(remote, tmp_path):
    side = clone_to(tmp_path, remote, "side")
    git(side, "checkout", "-b", "side")
    commit_file(side, "side.txt")
    git(side, "push", "origin", "side")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/side")


@pytest.mark.asyncio
async def test_a_new_branch_starts_from_the_default_branch_not_the_clones_head(
    tmp_path, project_work, remote
):
    work = project_work
    git(work, "checkout", "-b", "scratch")
    commit_file(work, "scratch.txt")
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline(), "trunk")
    assert git(work, "rev-parse", "HEAD") == git(work, "rev-parse", "origin/trunk")
    assert not (work / "scratch.txt").exists()
    with pytest.raises(subprocess.CalledProcessError):
        git(work, "rev-parse", "--abbrev-ref", "@{u}")  # no upstream until the first guided push


@pytest.mark.asyncio
async def test_the_default_branch_comes_from_the_remote_head_when_the_app_does_not_say(
    tmp_path, project_work, remote
):
    point_remote_head_elsewhere(remote, tmp_path)
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    assert _session_setup._default_branch == "side"


@pytest.mark.asyncio
async def test_the_default_branch_from_the_app_wins_over_the_remote_head(tmp_path, project_work, remote):
    point_remote_head_elsewhere(remote, tmp_path)
    assert await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline(), "trunk")
    assert _session_setup._default_branch == "trunk"
    assert not (project_work / "side.txt").exists()


@pytest.mark.asyncio
async def test_the_default_branch_falls_back_to_main(project_work, monkeypatch):
    # Only when nothing knows: the remote cannot be asked and the clone recorded no origin/HEAD.
    git(project_work, "remote", "set-url", "origin", str(project_work / "nowhere"))
    git(project_work, "symbolic-ref", "-d", "refs/remotes/origin/HEAD")
    await _session_setup._checkout_project_branch("factory/widget", _timeline._Timeline())
    assert _session_setup._default_branch == "main"


@pytest.mark.asyncio
async def test_start_pull_requests_asks_the_app_for_the_default_branch(
    tmp_path, project_work, remote, gh, monkeypatch
):
    point_remote_head_elsewhere(remote, tmp_path)  # the remote says `side`; the App says `trunk`
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))
    monkeypatch.setattr(_github, "GithubApp", lambda *args, **kwargs: gh.app)
    await _session_setup._start_pull_requests(
        "git@github.com:acme/widgets.git", PRIVATE_PEM, _timeline._Timeline()
    )
    assert _session_setup._default_branch == "trunk"
    assert git(project_work, "rev-parse", "HEAD") == git(project_work, "rev-parse", "origin/trunk")


@pytest.mark.asyncio
async def test_a_non_json_default_branch_answer_falls_back_to_git(project_work, gh, monkeypatch):
    # A proxy's HTML page with a 200: the App's answer is unusable, so git names the default branch.
    handler = gh.handler

    def html_repo(request):
        if request.url.path == f"/repos/{REPO}":
            return httpx.Response(200, text="<html>proxy login</html>")
        return handler(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(html_repo))
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))
    monkeypatch.setattr(_github, "GithubApp", lambda *args, **kwargs: gh.app)
    await _session_setup._start_pull_requests(
        "git@github.com:acme/widgets.git", PRIVATE_PEM, _timeline._Timeline()
    )
    assert _session_setup._pr is not None and _session_setup._pr.branch == "factory/widget"
    origin_head = git(project_work, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    assert _session_setup._default_branch == origin_head.removeprefix("origin/")


@pytest.mark.asyncio
async def test_a_failed_branch_checkout_keeps_the_branch_and_the_pr_not_the_default_branch(
    project_work, gh, monkeypatch
):
    monkeypatch.setattr(_github, "GithubApp", lambda *args, **kwargs: gh.app)
    git(project_work, "branch", "factory")  # a ref `factory` blocks creating `factory/widget`
    monkeypatch.setattr(_session_config, "_project_name", "widget")
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(EXAMPLE.read_text(), "example.toml"))
    timeline = _timeline._Timeline()
    await _session_setup._start_pull_requests("git@github.com:acme/widgets.git", PRIVATE_PEM, timeline)
    assert _session_setup._pr is not None and _session_setup._pr.branch == "factory/widget"
    rows = " ".join(text for _, _, text in timeline._rows)
    assert "could not switch to factory/widget" in rows and "pull requests are off" not in rows
    text = _instructions._project_memory(
        False, name="widget", branch=_session_setup._pr.branch, default_branch=_session_setup._default_branch
    )
    assert "HEAD:refs/heads/factory/widget" in text


@pytest.mark.asyncio
async def test_the_verdict_goes_on_the_commit_that_was_snapshotted_for_review(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    at_done = git(work, "rev-parse", "HEAD")
    committed = []

    async def meters_then_a_late_commit(_timeline):
        # The session commits and pushes after DONE, before the snapshot is taken.
        committed.append(commit_file(work, "late.txt"))
        git(work, "push")

    async def snapshot(_path):
        return object()

    async def reviewer(**_):
        return "fine\nVERDICT: PASS", []

    monkeypatch.setattr(_usage, "_read_meters", meters_then_a_late_commit)
    monkeypatch.setattr(Dir, "from_local", snapshot)
    monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)
    bridge.push("DONE: delivered")
    await run_until(
        work,
        bridge,
        proc,
        timeline,
        lambda: gh.sent("POST", f"/statuses/{committed[0]}") if committed else False,
    )

    (status,) = gh.sent("POST", f"/statuses/{committed[0]}")
    assert status["state"] == "success"
    assert not gh.sent("POST", f"/statuses/{at_done}")
    assert committed[0][:12] in gh.sent("POST", "/issues/5/comments")[0]["body"]


@pytest.mark.asyncio
async def test_a_rejected_round_that_also_stops_converging_posts_failure_once(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    head = git(work, "rev-parse", "HEAD")

    async def review(*args):
        return False, "FINDING: broken", False, "circling"

    monkeypatch.setattr(_review, "_review_round", review)
    monkeypatch.setattr(_session_config, "_max_rounds", lambda: 1)
    bridge.push("DONE: delivered")
    await run_until(
        work, bridge, proc, timeline, lambda: any("Review has now failed" in m for m in proc.submitted)
    )
    (status,) = gh.sent("POST", f"/statuses/{head}")
    assert status["state"] == "failure" and "did not converge" in status["description"]


@pytest.mark.asyncio
async def test_an_ordinary_rejected_round_still_posts_failure(done_project, gh, monkeypatch):
    work, bridge, proc, timeline = done_project
    head = git(work, "rev-parse", "HEAD")

    async def review(*args):
        return False, "FINDING: broken", True, ""

    monkeypatch.setattr(_review, "_review_round", review)
    bridge.push("DONE: delivered")
    await run_until(work, bridge, proc, timeline, lambda: any("Not done yet" in m for m in proc.submitted))
    (status,) = gh.sent("POST", f"/statuses/{head}")
    assert status["state"] == "failure" and "rejected round 1" in status["description"]


# --- repositories without draft pull requests ------------------------------------------------------------


def test_a_repo_without_draft_support_gets_a_ready_pr_and_ready_for_review_is_a_no_op(gh):
    gh.no_drafts = True
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == f"https://github.com/{REPO}/pull/5"
    drafts = [c["draft"] for c in gh.sent("POST", "/pulls")]
    assert drafts == [True, False]  # tried a draft once, then retried without
    gh.prs.passed(SHA, 1, "ok")
    assert not gh.sent("POST", "/graphql")  # nothing to take out of draft
    assert gh.sent("POST", "/issues/5/comments") and gh.sent("POST", f"/statuses/{SHA}")
    assert gh.rows == [f"pull request https://github.com/{REPO}/pull/5"]


def test_the_lack_of_draft_support_is_remembered_for_the_next_create(gh):
    gh.no_drafts = True
    gh.prs.publish(BRIEF, ACCEPTANCE)
    gh.pulls.clear()
    fresh_url = gh.prs.publish(BRIEF, ACCEPTANCE)
    assert fresh_url
    assert [c["draft"] for c in gh.sent("POST", "/pulls")] == [True, False, False]


def test_a_resumed_run_reads_draft_state_from_the_pr_instead_of_assuming_one(gh):
    # A new process does not know the repo refused drafts; the PR it finds says whether it is one.
    gh.no_drafts = True
    gh.prs.publish(BRIEF, ACCEPTANCE)
    resumed = _pull_requests.PullRequests(
        gh.app, REPO, "widget", RUN_URL, lambda icon, text: gh.rows.append(text)
    )
    resumed.passed(SHA, 1, "ok")
    assert not gh.sent("POST", "/graphql")
    assert gh.sent("POST", f"/statuses/{SHA}")


def test_other_422s_are_not_retried_and_say_why(gh):
    original = gh.handler

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(
                422,
                json={
                    "message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for acme:factory/widget."}],
                },
            )
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    assert gh.prs.publish(BRIEF, ACCEPTANCE) == ""
    (row,) = gh.rows
    assert "HTTP 422" in row and "Validation Failed" in row and "already exists" in row


def test_errors_given_as_plain_strings_are_kept_too(gh):
    original = gh.handler

    def handler(request):
        if request.url.path.endswith("/statuses/" + SHA):
            return httpx.Response(422, json={"message": "Validation Failed", "errors": ["bad state"]})
        return original(request)

    gh.app._http = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(handler))
    gh.prs.failed(SHA, "x")
    assert "bad state" in gh.rows[0]


# --- tokens and bounded text ----------------------------------------------------------------------------


def test_a_401_drops_the_cached_token_and_retries_once(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    tokens_before = len(gh.sent("POST", "/access_tokens"))
    gh.fail_once["/comments"] = 401  # GitHub revoked the installation token early
    gh.prs.passed(SHA, 1, "ok")
    assert len(gh.sent("POST", "/issues/5/comments")) == 2  # the 401, then the retry
    assert len(gh.sent("POST", "/access_tokens")) == tokens_before + 1  # a fresh token for the retry
    assert not [r for r in gh.rows if "401" in r]


def test_a_persistent_401_is_retried_only_once(gh):
    gh.prs.publish(BRIEF, ACCEPTANCE)
    gh.fail["/comments"] = 401
    gh.prs.passed(SHA, 1, "ok")
    assert len(gh.sent("POST", "/issues/5/comments")) == 2
    assert sum("HTTP 401" in r for r in gh.rows) == 1


def test_the_verdict_comment_is_bounded_with_a_note():
    criteria = [{"id": f"C{i}", "text": "t" * 500} for i in range(400)]
    comment = _pull_requests.verdict_comment(1, SHA, RUN_URL, criteria, {c["id"]: "PASS" for c in criteria})
    assert len(comment) <= 60_000 + 200 and "truncated" in comment
    assert comment.startswith("Independent review **passed**")


async def test_the_clones_own_origin_head_is_the_default_branch_when_the_remote_cannot_be_asked(
    tmp_path, monkeypatch
):
    import subprocess

    def git(*args, cwd=tmp_path):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "trunk", "origin.git")
    origin = tmp_path / "origin.git"
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i", cwd=origin)
    git("clone", "-q", str(origin), "ws")
    ws = tmp_path / "ws"
    git("remote", "set-url", "origin", str(tmp_path / "gone.git"), cwd=ws)  # ls-remote now fails
    monkeypatch.setattr(_session_config, "WORKSPACE", ws)
    assert await _session_setup._remote_default_branch() == "trunk"


def test_linked_issue_is_closed_by_the_published_pr_body(gh):
    gh.prs.issue = f"{REPO}#12"
    gh.prs.publish(BRIEF, ACCEPTANCE)
    assert "Closes #12" in gh.sent("POST", "/pulls")[0]["body"]
