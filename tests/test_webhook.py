"""Signed GitHub deliveries submit deploys without holding the HTTP response open."""

import asyncio
import dataclasses
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, cast

import flyte
import httpx
import pytest
from flyte import Environment, remote

from flyte_agent_factory import (
    _deploy,
    _deployer,
    _github,
    _issues,
    _loop,
    _profile,
    _pull_requests,
    _webhook,
)
from tests.test_deployer import make_direction_profile, make_profile

SECRET = "offline-webhook-secret"
SHA = "a" * 40


def _with_webhook(profile):
    """`[deploy] webhook = true`: pushes to the deploy repo reach the webhook."""
    return dataclasses.replace(profile, deploy=dataclasses.replace(profile.deploy, webhook=True))


@pytest.fixture
async def intake(monkeypatch):
    profile = make_direction_profile("file:///tmp/webhook-state")
    profile = dataclasses.replace(profile, github=dataclasses.replace(profile.github, webhook_secret="hook"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    monkeypatch.setenv(_webhook.SECRET_ENV, SECRET)
    # An asyncio primitive binds to the loop it first waits on, and each test has its own loop.
    monkeypatch.setattr(_webhook, "_bodies", asyncio.Semaphore(_webhook.MAX_CONCURRENT_BODIES))
    calls: list[Any] = []
    live: list[Any] = []

    async def listall(**kwargs):
        assert kwargs["task_name"] == _deploy.DEPLOYER_TASK
        assert kwargs["in_phase"] == _deploy.LIVE_PHASES
        for run in live:
            yield run

    async def start(task):
        calls.append(task)

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listall))
    monkeypatch.setattr(remote.Task, "get", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=start))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_webhook.app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(client=client, calls=calls, live=live, profile=profile)
    await asyncio.gather(*_webhook._pending)  # nothing pending is fine: gather of none returns at once


async def deliver(intake, event="push", *, signature="valid", repo="acme/factory", branch="main", **extra):
    body = json.dumps(
        {
            "ref": f"refs/heads/{branch}",
            "after": SHA,
            "repository": {"full_name": repo, "default_branch": "main"},
            **extra,
        }
    ).encode()
    headers = {"X-GitHub-Event": event}
    if signature != "missing":
        headers["X-Hub-Signature-256"] = (
            "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
            if signature == "valid"
            else "sha256=invalid"
        )
    response = await intake.client.post("/github", content=body, headers=headers)
    await asyncio.sleep(0)
    return response


@pytest.mark.parametrize("signature", ["invalid", "missing"])
async def test_rejects_bad_signature(intake, signature):
    assert (await deliver(intake, signature=signature)).status_code == 401
    assert intake.calls == []


async def test_ping(intake):
    assert (await deliver(intake, "ping")).status_code == 200
    assert intake.calls == []


@pytest.mark.parametrize("repo", ["acme/factory", "acme/direction", "ACME/old-factory"])
async def test_engine_direction_and_alias_submit_once(intake, monkeypatch, repo):
    monkeypatch.setattr(
        _webhook,
        "PROFILE",
        dataclasses.replace(
            intake.profile, repo_aliases={"factory": ("git@github.com:acme/old-factory.git",)}
        ),
    )
    assert (await deliver(intake, repo=repo)).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert len(intake.calls) == 1
    assert intake.calls[0]["name"] == _deploy.DEPLOYER_TASK
    assert intake.calls[0]["auto_version"] == "latest"


@pytest.mark.parametrize(
    "kwargs", [{"branch": "feature"}, {"repo": "acme/elsewhere"}, {"event": "issues"}, {"deleted": True}]
)
async def test_ignores_other_events(intake, kwargs):
    assert (await deliver(intake, **kwargs)).status_code == 204
    assert intake.calls == []


async def test_live_deployer_suppresses_submission(intake):
    intake.live.append(SimpleNamespace(name="already-running"))
    assert (await deliver(intake)).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert intake.calls == []


async def test_response_does_not_wait_for_flyte_and_duplicates_are_coalesced(intake, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    contexts = []

    async def start(task):
        intake.calls.append(task)
        started.set()
        await release.wait()

    def context(**kwargs):
        contexts.append(kwargs)
        return SimpleNamespace(run=SimpleNamespace(aio=start))

    monkeypatch.setattr(flyte, "with_runcontext", context)
    try:
        assert (await asyncio.wait_for(deliver(intake), timeout=30)).status_code == 202
        await asyncio.wait_for(started.wait(), timeout=30)
        assert (await asyncio.wait_for(deliver(intake), timeout=30)).status_code == 202
        assert len(intake.calls) == 1
        assert contexts[0]["annotations"]["github-sha"] == SHA
    finally:
        release.set()


@pytest.mark.parametrize("configured, minutes", [(False, 5), (True, 60)])
def test_reconcile_rate_and_environment_selection(monkeypatch, configured, minutes):
    profile = make_profile("file:///tmp/webhook-state")
    if configured:
        profile = _with_webhook(_profile.parse(profile.text + 'webhook_secret = "hook"\n', "webhook"))
    assert _deployer._reconcile_minutes(profile) == minutes
    assert ("factory-webhook" in _deploy.factory_envs(profile)) == configured
    assert ("factory-webhook" in _deploy.planned_envs(profile)) == configured


async def test_signed_payload_cannot_be_tampered_with(intake):
    signature = "sha256=" + hmac.new(SECRET.encode(), b"{}", hashlib.sha256).hexdigest()
    response = await intake.client.post(
        "/github",
        content=b'{"tampered": true}',
        headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "push"},
    )
    assert response.status_code == 401
    assert intake.calls == []


async def test_unconfigured_secret_fails_closed(intake, monkeypatch):
    monkeypatch.delenv(_webhook.SECRET_ENV)
    assert (await deliver(intake)).status_code == 503
    assert intake.calls == []


async def test_auto_false_does_not_submit(intake, monkeypatch):
    monkeypatch.setattr(
        _webhook, "PROFILE", dataclasses.replace(intake.profile, deploy=_profile.Deploy(auto=False))
    )
    assert (await deliver(intake)).status_code == 204
    assert intake.calls == []


async def test_direction_ref_overrides_default_branch(intake, monkeypatch):
    direction = dataclasses.replace(intake.profile.direction, ref="release")
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(intake.profile, direction=direction))
    assert (await deliver(intake, repo="acme/direction")).status_code == 204
    assert (await deliver(intake, repo="acme/direction", branch="release")).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert len(intake.calls) == 1


@pytest.mark.parametrize(
    ("secret", "webhook", "minutes"), [("", True, 5), ("hook", False, 5), ("hook", True, 60)]
)
def test_the_hourly_reconcile_needs_the_secret_and_a_deploy_repo_the_webhook_hears(secret, webhook, minutes):
    profile = make_profile("file:///tmp/webhook-state")
    profile = dataclasses.replace(
        profile,
        github=dataclasses.replace(profile.github, webhook_secret=secret),
        deploy=dataclasses.replace(profile.deploy, webhook=webhook),
    )
    assert _deploy.reconcile_minutes(profile) == minutes


async def test_pushes_to_the_deploy_repo_are_watched_not_the_factory_repo_when_they_differ(
    intake, monkeypatch
):
    deploy = dataclasses.replace(intake.profile.deploy, repo="https://github.com/acme/engine")
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(intake.profile, deploy=deploy))
    assert (await deliver(intake, repo="acme/factory")).status_code == 204
    assert (await deliver(intake, repo="acme/engine")).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert len(intake.calls) == 1


async def test_default_branch_is_taken_from_signed_repository(intake):
    assert (
        await deliver(
            intake,
            branch="trunk",
            repository={
                "full_name": "acme/factory",
                "default_branch": "trunk",
            },
        )
    ).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert len(intake.calls) == 1


@pytest.mark.parametrize("configured", [False, True])
def test_deployment_wires_webhook_and_actual_reconcile_trigger(monkeypatch, configured):
    import runpy

    profile = make_profile("file:///tmp/webhook-state")
    if configured:
        profile = _with_webhook(_profile.parse(profile.text + 'webhook_secret = "hook"\n', "webhook"))
    monkeypatch.setattr(_profile, "require", lambda: profile)
    deployed = runpy.run_path(_deployer.__file__)
    assert deployed["TRIGGER"].automation.interval_minutes == (60 if configured else 5)
    webhook_env = SimpleNamespace(name="factory-webhook", depends_on=[])
    if configured:
        _deploy.check_environments(cast(Environment, webhook_env))
        webhook = runpy.run_path(_webhook.__file__)
        env = webhook["env"]
        assert env.name == "factory-webhook"
        assert env.requires_auth is False
        assert len(env.secrets) == 1
        assert env.secrets[0].key == "hook"
        assert env.secrets[0].as_env_var == _webhook.SECRET_ENV
        assert not any(secret.key == "hook" for secret in deployed["env"].secrets)
    else:
        with pytest.raises(_deploy.Refused):
            _deploy.check_environments(cast(Environment, webhook_env))


async def test_submission_failure_is_logged_without_payload_and_next_delivery_can_retry(
    intake, monkeypatch, caplog
):
    runner = flyte.with_runcontext

    async def fail(task):
        raise OSError("sensitive payload must never be logged")

    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=fail))
    )
    assert (await deliver(intake)).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending, return_exceptions=True)
    await asyncio.sleep(0)
    assert not _webhook._pending
    assert "OSError" in caplog.text
    assert "sensitive payload" not in caplog.text
    assert SECRET not in caplog.text

    monkeypatch.setattr(flyte, "with_runcontext", runner)
    assert (await deliver(intake)).status_code == 202
    assert _webhook._pending  # a 202 always schedules the work
    await asyncio.gather(*_webhook._pending)
    assert len(intake.calls) == 1


async def test_declared_oversized_body_is_refused_before_it_is_read(intake):
    sent = []

    async def body():
        sent.append(1)
        yield b"x"

    headers = {"Content-Length": str(_webhook.MAX_BODY + 1), "X-GitHub-Event": "push"}
    response = await intake.client.post("/github", content=body(), headers=headers)
    assert response.status_code == 413
    assert sent == []
    assert intake.calls == []


async def test_undeclared_oversized_body_is_cut_off_at_the_cap(intake):
    chunk = b"x" * (1024 * 1024)
    total = _webhook.MAX_BODY // len(chunk) + 20
    sent = []

    async def body():
        # Chunked, so there is no Content-Length to refuse up front.
        for _ in range(total):
            sent.append(1)
            yield chunk

    response = await intake.client.post("/github", content=body(), headers={"X-GitHub-Event": "push"})
    assert response.status_code == 413
    # Reading stops at the cap; an unbounded read would have pulled every chunk.
    assert len(sent) <= _webhook.MAX_BODY // len(chunk) + 1 < total
    assert intake.calls == []


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_no_documentation_routes_on_the_public_endpoint(intake, path):
    assert (await intake.client.get(path)).status_code == 404


@pytest.mark.parametrize("configured", [False, True])
def test_queue_sweep_reconciles_at_the_same_rate_once_the_webhook_is_configured(monkeypatch, configured):
    # The webhook slows the deployer's backstop to hourly, but nothing event-driven dispatches the
    # queue for a run that was aborted or crashed, so the queue sweep must not follow it.
    import runpy

    from flyte_agent_factory import _factory

    profile = make_profile("file:///tmp/webhook-state")
    if configured:
        profile = _with_webhook(_profile.parse(profile.text + 'webhook_secret = "hook"\n', "webhook"))
    monkeypatch.setattr(_profile, "require", lambda: profile)
    factory = runpy.run_path(_factory.__file__)
    (trigger,) = factory["env"].tasks["factory.sweep"].triggers
    assert trigger.name == "queue_sweep"
    assert trigger.automation.interval_minutes == 5
    assert _deployer._reconcile_minutes(profile) == (60 if configured else 5)


def test_example_profile_documents_the_webhook_secret():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()
    assert "# webhook_secret = " in text
    enabled = _profile.parse(text.replace("# webhook_secret = ", "webhook_secret = "), "example")
    assert enabled.github.webhook_secret == "my_github_webhook_secret"
    assert not _profile.parse(text, "example").github.webhook_secret


# --- CI finishing on a default-branch head ----------------------------------------------------
# A push usually lands before its CI finishes, so the run it starts skips the commit as pending;
# the event that says CI finished is what starts the deploy.


async def send(intake, event: str, payload: dict[str, Any], repo="acme/factory"):
    body = json.dumps({"repository": {"full_name": repo, "default_branch": "main"}, **payload}).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
    }
    response = await intake.client.post("/github", content=body, headers=headers)
    if _webhook._pending:
        await asyncio.gather(*_webhook._pending)
    return response


def check_suite(branch="main", action="completed"):
    return {"action": action, "check_suite": {"head_branch": branch, "head_sha": SHA, "status": "completed"}}


def status(branches=("main",), state="success"):
    return {"sha": SHA, "state": state, "branches": [{"name": name} for name in branches]}


def workflow_run(branch="main", action="completed"):
    return {"action": action, "workflow_run": {"head_branch": branch, "head_sha": SHA, "status": "completed"}}


@pytest.mark.parametrize(
    "event, payload",
    [
        ("check_suite", check_suite()),
        ("status", status()),
        ("status", status(state="failure")),
        ("status", status(state="error")),
        ("status", status(branches=("feature", "main"))),
        ("workflow_run", workflow_run()),
    ],
)
async def test_ci_finishing_on_the_default_branch_starts_one_deploy(intake, event, payload):
    assert (await send(intake, event, payload)).status_code == 202
    assert len(intake.calls) == 1
    assert intake.calls[0]["name"] == _deploy.DEPLOYER_TASK


@pytest.mark.parametrize(
    "event, payload",
    [
        ("check_suite", check_suite(branch="feature")),
        ("check_suite", check_suite(action="requested")),
        ("check_suite", check_suite(branch=None)),
        ("status", status(branches=("feature",))),
        ("status", status(branches=())),
        ("status", status(state="pending")),
        ("workflow_run", workflow_run(branch="feature")),
        ("workflow_run", workflow_run(action="in_progress")),
    ],
)
async def test_ci_events_off_the_default_branch_or_not_final_start_nothing(intake, event, payload):
    assert (await send(intake, event, payload)).status_code == 204
    assert intake.calls == []


@pytest.mark.parametrize("event, payload", [("check_suite", check_suite()), ("status", status())])
async def test_ci_events_from_other_repositories_start_nothing(intake, event, payload):
    assert (await send(intake, event, payload, repo="acme/elsewhere")).status_code == 204
    assert intake.calls == []


async def test_ci_on_the_direction_ref_starts_a_deploy(intake, monkeypatch):
    direction = dataclasses.replace(intake.profile.direction, ref="release")
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(intake.profile, direction=direction))
    assert (await send(intake, "check_suite", check_suite(), repo="acme/direction")).status_code == 204
    response = await send(intake, "check_suite", check_suite(branch="release"), repo="acme/direction")
    assert response.status_code == 202
    assert len(intake.calls) == 1


async def test_ci_events_are_signed_like_pushes(intake):
    body = json.dumps({"repository": {"full_name": "acme/factory", "default_branch": "main"}, **status()})
    response = await intake.client.post(
        "/github",
        content=body.encode(),
        headers={"X-GitHub-Event": "status", "X-Hub-Signature-256": "sha256=invalid"},
    )
    assert response.status_code == 401
    assert intake.calls == []


async def test_ci_events_coalesce_with_a_pending_submission(intake, monkeypatch):
    release = asyncio.Event()

    async def start(task):
        intake.calls.append(task)
        await release.wait()

    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=start))
    )
    try:
        assert (await deliver(intake)).status_code == 202
        await asyncio.sleep(0)
        for event, payload in [("check_suite", check_suite()), ("status", status())]:
            body = json.dumps(
                {"repository": {"full_name": "acme/factory", "default_branch": "main"}, **payload}
            )
            signature = "sha256=" + hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
            response = await intake.client.post(
                "/github",
                content=body.encode(),
                headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature},
            )
            assert response.status_code == 202
        assert len(intake.calls) == 1
    finally:
        release.set()


async def test_concurrent_unsigned_bodies_are_bounded(intake, monkeypatch):
    # The per-request cap alone lets N concurrent requests buffer N x 25 MB before any is rejected.
    monkeypatch.setattr(_webhook, "BODY_WAIT", 0.05)
    chunk = b"x" * (1024 * 1024)
    chunks_per_body = 20  # under the cap, so only the concurrency limit can refuse them
    requests = _webhook.MAX_CONCURRENT_BODIES + 4
    gate = asyncio.Event()
    read = [0] * requests
    reading = peak = 0

    def body(index):
        async def stream():
            nonlocal reading, peak
            reading += 1
            peak = max(peak, reading)
            try:
                for n in range(chunks_per_body):
                    read[index] += 1
                    yield chunk
                    if n == 0:
                        await gate.wait()  # hold the first readers open while the others arrive
            finally:
                reading -= 1

        return stream()

    async def post(index):
        headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=invalid"}
        return await intake.client.post("/github", content=body(index), headers=headers)

    posts = [asyncio.create_task(post(i)) for i in range(requests)]
    await asyncio.sleep(0.3)  # the first readers hold their slots; every other request has given up
    gate.set()
    statuses = [(await task).status_code for task in posts]
    assert sorted(statuses) == [401] * _webhook.MAX_CONCURRENT_BODIES + [503] * (
        requests - _webhook.MAX_CONCURRENT_BODIES
    )
    assert peak <= _webhook.MAX_CONCURRENT_BODIES
    refused = [n for n, status in zip(read, statuses, strict=True) if status == 503]
    assert refused == [0] * len(refused)  # a refused body is never read
    assert sum(read) * len(chunk) <= _webhook.MAX_BODY * _webhook.MAX_CONCURRENT_BODIES
    assert intake.calls == []


async def test_a_body_slot_frees_after_its_request(intake):
    for _ in range(_webhook.MAX_CONCURRENT_BODIES + 2):
        assert (await deliver(intake, signature="invalid")).status_code == 401
    assert (await deliver(intake)).status_code == 202


async def test_a_slow_body_cannot_hold_a_slot_past_the_read_deadline(intake, monkeypatch):
    # qa: two connections trickling a byte every few seconds held both slots for days, so every real
    # delivery got 503. The read has a deadline (GitHub's own delivery timeout), then the slot is freed.
    monkeypatch.setattr(_webhook, "BODY_WAIT", 0.05)
    monkeypatch.setattr(_webhook, "BODY_READ_DEADLINE", 0.3)
    stop = asyncio.Event()

    async def trickle():
        while not stop.is_set():
            yield b"x"
            await asyncio.sleep(0.05)

    async def slow():
        headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=invalid"}
        return await intake.client.post("/github", content=trickle(), headers=headers)

    slowpokes = [asyncio.create_task(slow()) for _ in range(_webhook.MAX_CONCURRENT_BODIES)]
    # Probe with a bad signature (it frees its slot at once) until both slots are held; a 503 is the only
    # way to observe that, and the slowpokes hold them for BODY_READ_DEADLINE.
    for _ in range(1000):
        if (await deliver(intake, signature="invalid")).status_code == 503:
            break
        await asyncio.sleep(0.01)
    assert (await deliver(intake)).status_code == 503  # both slots held right now
    assert sorted([(await task).status_code for task in slowpokes]) == [408] * _webhook.MAX_CONCURRENT_BODIES
    stop.set()
    assert (await deliver(intake)).status_code == 202  # the slots were freed at the deadline


# --- Issue intake -----------------------------------------------------------------------------
# The same signed endpoint hands allowlisted issue, comment and closed-PR events to factory-issues.


async def send_issue(payload, signature=True, *, event="issues", secret="test-secret", body=None):
    body = json.dumps(payload).encode() if body is None else body
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": f"sha256={digest}" if signature else "bad"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_webhook.app), base_url="http://test"
    ) as client:
        return await client.post("/github", content=body, headers=headers)


@pytest.fixture
def submitted(monkeypatch):
    """Issue intake configured, with each `factory-issues.process` submission recorded."""
    monkeypatch.setenv(_webhook.SECRET_ENV, "test-secret")
    monkeypatch.setattr(_webhook, "_bodies", asyncio.Semaphore(_webhook.MAX_CONCURRENT_BODIES))
    github = dataclasses.replace(_webhook.PROFILE.github, maintainers=("owner",))
    monkeypatch.setattr(
        _webhook,
        "PROFILE",
        dataclasses.replace(_webhook.PROFILE, state=_profile.State("file:///tmp/test-state"), github=github),
    )
    calls = []

    async def run(task, **inputs):
        calls.append((task, inputs))

    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=run))
    )
    # The deployed task, looked up by name: the app cannot build and upload a code bundle itself.
    monkeypatch.setattr(remote.Task, "get", lambda **kwargs: kwargs)
    return calls


def test_intake_task_name_matches_the_deployed_task():
    assert _webhook.ISSUES_TASK == _webhook.process.name


async def test_signed_request_only_enqueues(submitted, monkeypatch):
    monkeypatch.setattr(_issues, "accept_event", lambda *args: {"kind": "launch"})
    assert (await send_issue({"action": "labeled"})).status_code == 202
    assert len(submitted) == 1
    task, inputs = submitted[0]
    assert task == {
        "name": _webhook.ISSUES_TASK,
        "auto_version": "latest",
        "project": _webhook.PROJECT,
        "domain": _webhook.DOMAIN,
    }
    assert inputs == {"event": "issues", "payload": json.dumps({"action": "labeled"})}
    assert (await send_issue({}, False)).status_code == 401
    assert len(submitted) == 1


async def test_body_cap_json_and_ignored_event(submitted):
    assert (await send_issue({}, body=b"x" * (_webhook.MAX_BODY + 1))).status_code == 413
    assert (await send_issue({}, body=b"not-json")).status_code == 400
    # The receiver answers ping with 200 and an event it ignores with 204.
    assert (await send_issue({}, event="ping")).status_code == 200
    assert (await send_issue({})).status_code == 204
    assert submitted == []


async def test_fail_closed_without_secret(submitted, monkeypatch):
    monkeypatch.delenv(_webhook.SECRET_ENV)
    assert (await send_issue({})).status_code == 503
    assert submitted == []


async def test_issue_events_are_ignored_without_maintainers(submitted, monkeypatch):
    github = dataclasses.replace(_webhook.PROFILE.github, maintainers=())
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(_webhook.PROFILE, github=github))
    monkeypatch.setattr(_issues, "accept_event", lambda *args: pytest.fail("intake is off"))
    # Not even parsed: with intake off an issue event is as unrelated as any other.
    assert (await send_issue({}, body=b"not-json")).status_code == 204
    assert submitted == []


async def test_malformed_issue_payload_is_refused(submitted, monkeypatch):
    def accept(*args):
        raise TypeError("issue is not an object")

    monkeypatch.setattr(_issues, "accept_event", accept)
    assert (await send_issue({"issue": "x"})).status_code == 400
    assert submitted == []


async def test_accepted_issue_without_state_fails_closed(submitted, monkeypatch):
    monkeypatch.setattr(_issues, "accept_event", lambda *args: {"kind": "launch"})
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(_webhook.PROFILE, state=_profile.State("")))
    assert (await send_issue({"action": "opened"})).status_code == 503
    assert submitted == []


async def test_failed_intake_submission_is_a_failed_delivery_without_details(submitted, monkeypatch, caplog):
    monkeypatch.setattr(_issues, "accept_event", lambda *args: {"kind": "direction"})

    async def broken(task, **inputs):
        raise RuntimeError("token=secret-detail")

    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=broken))
    )
    with caplog.at_level("ERROR", logger=_webhook.__name__):
        assert (await send_issue({"action": "created"}, event="issue_comment")).status_code == 503
    assert "RuntimeError" in caplog.text
    assert "secret-detail" not in caplog.text


async def test_slow_intake_submission_answers_within_the_delivery_timeout(submitted, monkeypatch):
    monkeypatch.setattr(_issues, "accept_event", lambda *args: {"kind": "launch"})
    monkeypatch.setattr(_webhook, "INTAKE_SUBMIT_DEADLINE", 0.05)

    async def slow(task, **inputs):
        await asyncio.sleep(5)

    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=slow))
    )
    assert (await send_issue({"action": "opened"})).status_code == 503


async def test_issue_intake_does_not_need_auto_deploy(submitted, monkeypatch):
    monkeypatch.setattr(
        _webhook, "PROFILE", dataclasses.replace(_webhook.PROFILE, deploy=_profile.Deploy(auto=False))
    )
    monkeypatch.setattr(_issues, "accept_event", lambda *args: {"kind": "pull_closed"})
    assert (await send_issue({"action": "closed"}, event="pull_request")).status_code == 202
    assert len(submitted) == 1


def test_issue_tasks_ship_with_the_webhook_and_hold_the_app_key_alone(monkeypatch):
    import runpy

    profile = make_profile("file:///tmp/webhook-state")
    assert "factory-issues" not in _deploy.factory_envs(profile)
    profile = _profile.parse(profile.text + 'webhook_secret = "hook"\nmaintainers = ["owner"]\n', "webhook")
    assert profile.github.private_key_secret
    assert {"factory-webhook", "factory-issues"} <= _deploy.factory_envs(profile)
    monkeypatch.setattr(_profile, "require", lambda: profile)
    webhook = runpy.run_path(_webhook.__file__)
    app, issues = webhook["env"], webhook["issues_env"]
    assert app.depends_on == [issues]
    assert issues.name == "factory-issues"
    _deploy.check_environments(app)  # the dependency walk admits factory-issues with the webhook
    # The public app mounts only the webhook secret; the App's private key only the intake tasks.
    assert [(s.key, s.as_env_var) for s in app.secrets] == [("hook", _webhook.SECRET_ENV)]
    assert [s.key for s in issues.secrets] == [profile.github.private_key_secret]


def test_webhook_without_maintainers_deploys_no_issue_tasks(monkeypatch):
    """Intake off: no `factory-issues`, so no five-minute outbox drain eats the webhook's run saving."""
    import runpy

    profile = _profile.parse(
        make_profile("file:///tmp/webhook-state").text + 'webhook_secret = "hook"\n', "w"
    )
    assert "factory-webhook" in _deploy.factory_envs(profile)
    assert "factory-issues" not in _deploy.factory_envs(profile)
    monkeypatch.setattr(_profile, "require", lambda: profile)
    webhook = runpy.run_path(_webhook.__file__)
    assert webhook["env"].depends_on == []
    _deploy.check_environments(webhook["env"])


async def test_marked_loop_creation_reaches_private_intake_for_authentication(submitted, monkeypatch):
    profile = dataclasses.replace(_webhook.PROFILE, loop=_profile.Loop(enabled=True, repo="acme/factory"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    payload = {
        "action": "opened",
        "repository": {"full_name": "acme/factory"},
        "sender": {"login": "factory[bot]", "type": "Bot"},
        "issue": {
            "number": 17,
            "state": "open",
            "labels": [{"name": "factory"}],
            "user": {"login": "factory[bot]", "type": "Bot"},
            "body": "<!-- loop-source: lesson:abc -->",
        },
    }
    assert (await send_issue(payload)).status_code == 202
    assert len(submitted) == 1


async def test_loop_status_is_signed_and_independent_of_auto_deploy(intake, monkeypatch):
    profile = dataclasses.replace(
        intake.profile,
        deploy=_profile.Deploy(auto=False),
        loop=_profile.Loop(enabled=True, repo="acme/factory"),
    )
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    calls = []

    async def submit(sha):
        calls.append(sha)

    monkeypatch.setattr(_webhook, "_submit_loop", submit, raising=False)
    payload = {"sha": SHA, "state": "success", "context": "factory/verifier"}
    assert (await send(intake, "status", payload)).status_code == 202
    assert calls == [SHA]
    for change in ({"context": "other"}, {"state": "pending"}):
        assert (await send(intake, "status", {**payload, **change})).status_code == 204
    assert (await send(intake, "status", payload, repo="acme/other")).status_code == 204
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(profile, loop=_profile.Loop()))
    assert (await send(intake, "status", payload)).status_code == 204
    assert calls == [SHA]


async def test_loop_wakeup_adopts_each_pr_head_once(monkeypatch):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    profile = dataclasses.replace(_webhook.PROFILE, loop=_profile.Loop(enabled=True, repo="acme/factory"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    pulls: list[dict[str, Any]] = [
        {"number": 1, "head": {"sha": SHA}},
        {"number": 2, "head": {"sha": SHA}},
        {"number": 3, "head": {"sha": "b" * 40}},
    ]
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda secret: "offline")
    monkeypatch.setattr(
        _github, "GithubApp", lambda *args: SimpleNamespace(_get_pages=lambda *args, **kwargs: pulls)
    )
    monkeypatch.setattr(_loop, "loop_pull", lambda app, repo, pull: True)
    runs: dict[str, Any] = {}
    calls: list[Any] = []

    async def get(*, name, **kwargs):
        if name not in runs:
            raise ConnectError(Code.NOT_FOUND, "missing")
        return runs[name]

    def context(**kwargs):
        async def start(task, **inputs):
            calls.append(inputs)
            assert kwargs["name"] not in runs
            runs[kwargs["name"]] = SimpleNamespace(name=kwargs["name"])
            return runs[kwargs["name"]]

        return SimpleNamespace(run=SimpleNamespace(aio=start))

    monkeypatch.setattr(remote.Run, "get", SimpleNamespace(aio=get))
    monkeypatch.setattr(remote.Task, "get", lambda **kwargs: kwargs)
    monkeypatch.setattr(flyte, "with_runcontext", context)
    for _ in range(4):
        await _webhook.wake_loop(SHA)
    assert calls == [{"pr_number": 1}, {"pr_number": 2}]
    pulls[0]["head"]["sha"] = "b" * 40
    await _webhook.wake_loop("b" * 40)
    assert calls == [{"pr_number": 1}, {"pr_number": 2}, {"pr_number": 1}, {"pr_number": 3}]


async def test_concurrent_loop_submission_adopts_name_conflict(monkeypatch):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    run: Any = SimpleNamespace(name="loop-existing")
    reads = []

    async def get(**kwargs):
        reads.append(kwargs)
        if len(reads) == 1:
            raise ConnectError(Code.NOT_FOUND, "missing")
        return run

    async def start(task, **inputs):
        raise ConnectError(Code.ALREADY_EXISTS, "concurrent submission")

    monkeypatch.setattr(remote.Run, "get", SimpleNamespace(aio=get))
    monkeypatch.setattr(remote.Task, "get", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        flyte, "with_runcontext", lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=start))
    )
    assert await _webhook._named_loop_run(run.name, "factory-loop.loop_run", pr_number=1) is run
    assert reads[0] == reads[1]


def test_loop_webhook_deploys_private_wakeup_with_planner(monkeypatch):
    import runpy

    profile = dataclasses.replace(
        _webhook.PROFILE,
        loop=_profile.Loop(enabled=True, repo="acme/factory"),
        planner=_profile.Planner(admission="planner"),
    )
    monkeypatch.setattr(_profile, "require", lambda: profile)
    webhook = runpy.run_path(_webhook.__file__)
    assert webhook["issues_env"] in webhook["env"].depends_on
    assert "factory-issues" in _deploy.factory_envs(profile)
    assert webhook["wake_loop"].name == "factory-issues.wake_loop"


async def test_loop_status_on_default_branch_also_wakes_deployer(intake, monkeypatch):
    profile = dataclasses.replace(intake.profile, loop=_profile.Loop(enabled=True, repo="acme/factory"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    loop_heads = []

    async def submit(sha):
        loop_heads.append(sha)

    monkeypatch.setattr(_webhook, "_submit_loop", submit)
    payload = {**status(), "context": "factory/verifier"}
    assert (await send(intake, "status", payload)).status_code == 202
    assert loop_heads == [SHA]
    assert len(intake.calls) == 1
    assert intake.calls[0]["name"] == _deploy.DEPLOYER_TASK


async def test_loop_wakeup_ignores_a_pr_that_shares_the_head_but_is_not_the_loops(monkeypatch):
    profile = dataclasses.replace(_webhook.PROFILE, loop=_profile.Loop(enabled=True, repo="acme/factory"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    pulls = [{"number": 1, "head": {"sha": SHA}}, {"number": 2, "head": {"sha": SHA}}]
    monkeypatch.setattr(_pull_requests, "take_app_key", lambda secret: "offline")
    monkeypatch.setattr(
        _github, "GithubApp", lambda *args: SimpleNamespace(_get_pages=lambda *args, **kwargs: pulls)
    )
    monkeypatch.setattr(_loop, "loop_pull", lambda app, repo, pull: pull["number"] == 2)
    started = []

    async def start(name, task, **inputs):
        started.append(inputs)

    monkeypatch.setattr(_webhook, "_named_loop_run", start)
    await _webhook.wake_loop(SHA)
    assert started == [{"pr_number": 2}]
