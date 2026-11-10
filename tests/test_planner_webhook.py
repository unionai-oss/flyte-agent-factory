"""Planner deliveries wake one lifespan consumer; internal ingress authenticates its sender."""

import asyncio
import dataclasses
import hashlib
import hmac
from types import SimpleNamespace
from typing import Any, cast

import flyte
from flyte import remote

from flyte_agent_factory import _deploy, _issues, _planner_runtime, _profile, _webhook
from tests.test_webhook import deliver
from tests.test_webhook import intake as intake


def planner_profile(monkeypatch, profile):
    profile = dataclasses.replace(
        profile,
        planner=_profile.Planner(admission="planner"),
        github=dataclasses.replace(profile.github, maintainers=("maintainer",)),
    )
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    return profile


async def test_issue_and_pr_events_coalesce_without_submitting_intake(intake, monkeypatch):
    planner_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("url", "team/repo")})
    for kind in ("issues", "pull_request", "issue_comment"):
        result = await deliver(
            intake,
            kind,
            repository={"full_name": "team/repo"},
            action="edited",
        )
        assert result.status_code == 202
    assert len(wakes) == 3
    assert intake.calls == []


async def test_completion_ingress_requires_internal_signature(intake, monkeypatch):
    planner_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    assert (await intake.client.post("/internal/planner")).status_code == 401
    assert wakes == []
    signature = hmac.new(b"offline-app-key", b"factory-planner-wakeup", hashlib.sha256).hexdigest()
    response = await intake.client.post("/internal/planner", headers={"X-Factory-Signature": signature})
    assert response.status_code == 202
    assert wakes == [True]


async def test_lifespan_owns_and_cancels_one_consumer(intake, monkeypatch):
    planner_profile(monkeypatch, intake.profile)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    class Consumer:
        async def run(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    def create(profile):
        calls.append(profile)
        return Consumer()

    monkeypatch.setattr(_planner_runtime, "consumer", create)
    async with _webhook._lifespan(_webhook.app):
        await asyncio.wait_for(entered.wait(), 30)
        assert len(calls) == 1
    assert cancelled.is_set()
    assert _webhook._consumer is None


def test_smoke_wait_callbacks_keep_their_issue_and_run(monkeypatch):
    from ops.probes import planner_smoke as smoke

    callbacks: list[Any] = []

    def wait_for(read, *_):
        callbacks.append(read)
        return read()

    monkeypatch.setattr(smoke, "wait_for", wait_for)
    issues = ["team/repo#1", "team/repo#2"]
    runs = [SimpleNamespace(name=f"run-{i}", url=f"url-{i}") for i in range(2)]
    monkeypatch.setattr(remote.Run, "get", lambda name: SimpleNamespace(name=name, phase="SUCCEEDED"))
    # The fakes stand in for `remote.Run`: only `name` and `url` are read.
    smoke.wait_for_projects(issues, lambda issue: [cast(remote.Run, runs[issues.index(issue)])])
    assert [read()[0].name for read in callbacks[::2]] == ["run-1", "run-0"]
    looked_up = []

    def get(name):
        looked_up.append(name)
        return SimpleNamespace(phase="SUCCEEDED")

    monkeypatch.setattr(remote.Run, "get", get)
    assert all(read() for read in callbacks[1::2])
    assert looked_up == ["run-1", "run-0"]


async def test_pr_status_wakes_planner_when_auto_deploy_is_off(intake, monkeypatch):
    profile = planner_profile(monkeypatch, intake.profile)
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(profile, deploy=_profile.Deploy(auto=False)))
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("url", "team/repo")})
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    result = await deliver(intake, "status", repo="team/repo", state="success", sha="a" * 40)
    assert result.status_code == 202
    assert wakes == [True]
    assert intake.calls == []


async def test_legacy_pr_status_wakes_reconcile_without_admitting_work(intake, monkeypatch):
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("url", "team/repo")})
    dirty = asyncio.Event()
    monkeypatch.setattr(_webhook, "_reconcile_dirty", dirty)
    result = await deliver(intake, "check_run", repo="team/repo", action="completed")
    assert result.status_code == 202
    assert dirty.is_set()
    assert intake.calls == []


async def test_completion_waits_for_terminal_phase_before_waking(intake, monkeypatch):
    import json

    planner_profile(monkeypatch, intake.profile)
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: None))
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    dirty = asyncio.Event()
    monkeypatch.setattr(_webhook, "_reconcile_dirty", dirty)
    terminal = asyncio.Event()

    async def wait(name):
        assert name == "finished-project"
        await terminal.wait()

    monkeypatch.setattr(_webhook, "_wait_for_run", wait)
    body = json.dumps({"run": "finished-project"}).encode()
    signature = hmac.new(b"offline-app-key", b"factory-planner-wakeup" + body, hashlib.sha256).hexdigest()
    response = await intake.client.post(
        "/internal/planner", content=body, headers={"X-Factory-Signature": signature}
    )
    assert response.status_code == 202
    await asyncio.sleep(0)
    assert not dirty.is_set()
    terminal.set()
    await asyncio.gather(*_webhook._completion_tasks)
    assert dirty.is_set()


async def test_reconcile_consumer_retains_a_wake_during_an_active_run(intake, monkeypatch):
    dirty = asyncio.Event()
    monkeypatch.setattr(_webhook, "_reconcile_dirty", dirty)
    submitted: asyncio.Queue[Any] = asyncio.Queue()
    release = asyncio.Event()

    async def listall(**kwargs):
        assert kwargs["task_name"] == _deploy.RECONCILE_TASK
        if False:
            yield  # type: ignore[unreachable]  # makes this an async generator that yields nothing

    async def submit(task):
        assert task["name"] == _deploy.RECONCILE_TASK
        run = SimpleNamespace(name="repair")
        await submitted.put(run)
        return run

    async def wait(name):
        await release.wait()

    monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listall))
    monkeypatch.setattr(
        flyte,
        "with_runcontext",
        lambda **kwargs: SimpleNamespace(run=SimpleNamespace(aio=submit)),
    )
    monkeypatch.setattr(_webhook, "_wait_for_run", wait)
    worker = asyncio.create_task(_webhook._reconcile_events())
    try:
        dirty.set()
        await asyncio.wait_for(submitted.get(), 30)
        dirty.set()
        dirty.set()
        await asyncio.sleep(0)
        assert submitted.empty()
        release.set()
        await asyncio.wait_for(submitted.get(), 30)
        await asyncio.sleep(0)
        assert submitted.empty()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_legacy_completion_submits_repair_with_its_run(intake, monkeypatch):
    calls = []
    monkeypatch.setattr(
        flyte,
        "with_runcontext",
        lambda **kwargs: SimpleNamespace(run=lambda task, **inputs: calls.append((task, inputs))),
    )
    _planner_runtime.notify(intake.profile, "", "completed-project")
    assert calls == [
        (
            {
                "name": _deploy.RECONCILE_TASK,
                "auto_version": "latest",
                "project": _webhook.PROJECT,
                "domain": _webhook.DOMAIN,
            },
            {"completion_run": "completed-project"},
        )
    ]


async def test_planner_timer_is_an_hourly_safety_net(monkeypatch):
    # An organization board, whose moves arrive as events (user boards keep a shorter poll, below).
    board = _profile.Board(owner="someone", owner_type="org", number=1, pat_secret="pat")
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(_webhook.PROFILE, board=board))
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    try:
        await _webhook._planner_timer()
    except asyncio.CancelledError:
        pass
    assert sleeps == [3600]


async def test_completion_signature_covers_the_run_name(intake, monkeypatch):
    import json

    planner_profile(monkeypatch, intake.profile)
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: None))
    monkeypatch.setenv(_deploy.GITHUB_KEY_ENV, "offline-app-key")
    body = json.dumps({"run": "project-a"}).encode()
    signature = hmac.new(b"offline-app-key", b"factory-planner-wakeup" + body, hashlib.sha256).hexdigest()
    response = await intake.client.post(
        "/internal/planner",
        content=json.dumps({"run": "project-b"}).encode(),
        headers={"X-Factory-Signature": signature},
    )
    assert response.status_code == 401
    assert not _webhook._completion_tasks


def board_profile(monkeypatch, profile):
    profile = planner_profile(monkeypatch, profile)
    profile = dataclasses.replace(profile, board=_profile.Board("acme", "org", 1, "board-pat"))
    monkeypatch.setattr(_webhook, "PROFILE", profile)
    return profile


def board_event(intake, action, **extra):
    return deliver(intake, "projects_v2_item", action=action, organization={"login": "acme"}, **extra)


async def test_board_item_moved_to_ready_wakes_planner_once(intake, monkeypatch):
    board_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    status = {"field_value": {"field_name": "Status", "from": {"name": "Backlog"}, "to": {"name": "Ready"}}}
    result = await board_event(intake, "edited", changes=status)
    assert result.status_code == 202
    assert wakes == [True]
    assert intake.calls == []


async def test_board_item_events_that_can_launch_wake_planner(intake, monkeypatch):
    board_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    for action in ("created", "reordered", "converted", "restored"):
        assert (await board_event(intake, action)).status_code == 202
    assert len(wakes) == 4


async def test_unrelated_board_events_do_not_wake_planner(intake, monkeypatch):
    board_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    size = {"field_value": {"field_name": "Size", "from": {"name": "S"}, "to": {"name": "M"}}}
    assert (await board_event(intake, "edited", changes=size)).status_code == 204
    assert (await board_event(intake, "edited", changes={})).status_code == 204
    assert (await board_event(intake, "deleted")).status_code == 204
    other = await deliver(
        intake, "projects_v2_item", action="created", organization={"login": "someone-else"}
    )
    assert other.status_code == 204
    assert wakes == []


async def test_issue_events_that_can_make_an_item_launchable_wake_planner_once(intake, monkeypatch):
    planner_profile(monkeypatch, intake.profile)
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("url", "team/repo")})
    for action in ("labeled", "unlabeled", "opened", "reopened", "edited"):
        wakes.clear()
        result = await deliver(intake, "issues", repo="team/repo", action=action)
        assert result.status_code == 202
        assert wakes == [True]


def test_a_user_owned_board_keeps_the_three_minute_planner_poll():
    # GitHub sends projects_v2_item events for organization boards only.
    board = _profile.Board(owner="someone", owner_type="user", number=1, pat_secret="pat")
    # fakes of the profile: only `board` is read
    user: Any = SimpleNamespace(board=board)
    org: Any = SimpleNamespace(board=dataclasses.replace(board, owner_type="org"))
    assert _webhook.planner_interval_s(user) == 180
    assert _webhook.planner_interval_s(cast(Any, SimpleNamespace(board=None))) == 180
    assert _webhook.planner_interval_s(org) == _deploy.RECONCILE_MINUTES * 60


async def test_a_configured_board_ignores_other_boards_of_its_organization(intake, monkeypatch):
    profile = board_profile(monkeypatch, intake.profile)
    board = dataclasses.replace(profile.board, node_id="PVT_ours")
    monkeypatch.setattr(_webhook, "PROFILE", dataclasses.replace(profile, board=board))
    wakes = []
    monkeypatch.setattr(_webhook, "_consumer", SimpleNamespace(wake=lambda: wakes.append(True)))
    status = {"field_value": {"field_name": "Status"}}
    other = await board_event(intake, "edited", changes=status, projects_v2_item={"project_node_id": "PVT_x"})
    assert other.status_code == 204 and wakes == []
    ours = await board_event(
        intake, "edited", changes=status, projects_v2_item={"project_node_id": "PVT_ours"}
    )
    assert ours.status_code == 202 and wakes == [True]


def test_board_node_id_is_an_optional_profile_field():
    text = open("tests/profiles/work.toml").read()
    if "[board]" not in text:
        text += '\n[board]\nowner = "acme"\nowner_type = "org"\nnumber = 1\npat_secret = "board-pat"\n'
    with_id = text.replace("[board]\n", '[board]\nnode_id = "PVT_ours"\n', 1)
    board, board_with_id = _profile.parse(text).board, _profile.parse(with_id).board
    assert board is not None and board_with_id is not None
    assert board.node_id == ""
    assert board_with_id.node_id == "PVT_ours"
