"""The factory-deployer's labelled attempts and the deploy library under it. Offline.

Fakes sit at GitHub HTTP, Flyte run submission/listing, and the final subprocess boundary.
"""

import contextlib
import dataclasses
import json
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import httpx
import pytest
from flyte import remote
from flyteidl2.workflow.run_definition_pb2 import ActionStatus  # type: ignore[import-untyped]  # no py.typed marker
from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]  # no stubs installed

from flyte_agent_factory import (
    _deploy,
    _deploy_child,
    _deployer,
    _factory,
    _factory_base,
    _factory_meters,
    _github,
    _profile,
    _standards,
)
from flyte_agent_factory._deploy import DeployError, Refused
from tests.test_github import PRIVATE_PEM, Clock, FakeGithub

WORK = Path(__file__).resolve().parent / "profiles" / "work.toml"
SHA = "a" * 40
OLD_SHA = "b" * 40
DIRECTION_SHA = "d" * 40
OLD_DIRECTION_SHA = "e" * 40
#: The real runs of the engine tree's own code, before `World` replaces them with in-process ones.
REAL_CHECK_IN_TREE = _deploy.check_in_tree
REAL_DEPLOY_IN_TREE = _deploy.deploy_in_tree
PACKAGE = Path(_profile.__file__).resolve().parent


def make_profile(state_uri: str, *, auto: bool = True) -> _profile.Profile:
    text = WORK.read_text().replace(
        '[repo]\nproject = "git@example.org:team/work-sandbox.git"',
        '[repo]\nproject = "git@example.org:team/work-sandbox.git"\nfactory = '
        '"git@github.com:acme/factory.git"',
    )
    text += f"""
[deploy]
auto = {"true" if auto else "false"}

[state]
uri = "{state_uri}"

[github]
app_id = "12345"
private_key_secret = "github_key_x"
"""
    return _profile.parse(text, "work+deployer")


def make_direction_profile(state_uri: str, *, ref: str = "") -> _profile.Profile:
    text = make_profile(state_uri).text + '\n[direction]\nrepo = "git@github.com:acme/direction.git"\n'
    return _profile.parse(text + (f'ref = "{ref}"\n' if ref else ""), "work+direction")


class GitRefs:
    """Model GitHub's create-if-absent and non-forced fast-forward semantics."""

    def __init__(self):
        self.ref = None
        self.commits = {}
        self.race = False

    def _call(self, repo, method, path, **kwargs):
        if method == "GET" and "/ref/" in path:
            if self.ref is None:
                raise _github.GithubError("not found", 404)
            return {"object": {"sha": self.ref}}
        if method == "GET" and "/commits/" in path:
            # Any source commit has a tree; lock commits are the ones posted here.
            return self.commits.get(path.rsplit("/", 1)[-1], {"tree": {"sha": "tree"}})
        if method == "POST" and path.endswith("/commits"):
            sha = f"commit-{len(self.commits)}"
            self.commits[sha] = kwargs["json"]
            return {"sha": sha}
        target = kwargs["json"]["sha"]
        if self.race:
            self.ref = "other-contender"
        if method == "POST":
            if self.ref is not None:
                raise _github.GithubError("already exists", 422)
        elif method == "PATCH":
            assert kwargs["json"]["force"] is False
            if self.commits[target]["parents"] != [self.ref]:
                raise _github.GithubError("not fast-forward", 422)
        else:
            raise AssertionError((method, path))
        self.ref = target
        return {"object": {"sha": target}}


class World:
    def __init__(self, tmp_path: Path, monkeypatch, consistent=False):
        self.state = tmp_path / "state"
        self.profile = make_profile(f"file://{self.state}")
        self.clock = Clock(time.time())
        self.gh = FakeGithub(self.clock)
        self.gh.statuses = [{"context": "factory/verifier", "state": "success"}]
        # A second repository, the direction repo, answered by a second FakeGithub (which only knows
        # acme/factory, hence the path rewrite). Its default branch is "trunk"; `ref` branches are
        # in `direction_refs`.
        self.direction_gh = FakeGithub(self.clock)
        self.direction_gh.head_sha = DIRECTION_SHA
        self.direction_gh.statuses = [{"context": "direction/profile-pins", "state": "success"}]
        self.direction_refs: dict[str, str] = {}
        client = httpx.Client(base_url=_github.API, transport=httpx.MockTransport(self.handler))
        self.app = _github.GithubApp("12345", PRIVATE_PEM, client=client, clock=self.clock)
        # The git-ref lock (`RUN_LISTING_CONSISTENT=False`, production) goes through the App's
        # `_call`; FakeGithub has no git data API, so those calls go to a model of it.
        self.refs = GitRefs()
        real_call = self.app._call
        monkeypatch.setattr(
            self.app,
            "_call",
            lambda repo, method, path, **kw: (
                self.refs._call(repo, method, path, **kw)
                if "/git/" in path
                else real_call(repo, method, path, **kw)
            ),
        )
        self.said: list[str] = []
        self.live_runs: list[str] = []
        self.runs: list[SimpleNamespace] = []
        self.submissions: list[Any] = []
        self.fetched: list[tuple[str, str]] = []
        self.slot_calls: list[dict[str, Any]] = []
        self.factory_calls: list[dict[str, Any]] = []
        self.profile_checks: list[dict[str, Any]] = []
        self.slot_rc = self.factory_rc = 0
        self.tree = tmp_path / "tree"
        (self.tree / "src").mkdir(parents=True)
        self.direction_tree = tmp_path / "direction-tree"
        self.direction_tree.mkdir()
        self.direction_fetch_error: str | None = None

        @contextlib.contextmanager
        def fetched_tree(url, sha):
            self.fetched.append((url, sha))
            if "acme/direction" in url:
                if self.direction_fetch_error:
                    raise DeployError(self.direction_fetch_error)
                yield self.direction_tree
            else:
                yield self.tree

        # The fetched engine is this checkout's code: `_deploy_child`'s steps, in-process, down to the
        # deploy children (`deploy_slots`, `deploy_factory`), which are faked below.
        def check_in_tree(src, text, where, *, tree, base_env, diagnostics=None):
            self.profile_checks.append({"src": src, "text": text, "tree": tree, "base_env": base_env})
            try:
                profile = _deploy_child.load(text, where, tree)
                _deploy.check_slots(profile, [s.env for s in profile.slots])
            except (_profile.ProfileError, Refused) as exc:
                raise DeployError(f"the engine being deployed refuses {where}: {exc}") from exc
            return _deploy.planned_envs(profile)

        def deploy_in_tree(src, text, where, *, tree, project, domain, ref, base_env):
            profile = _deploy_child.load(text, where, tree)
            _deploy_child.deploy(profile, src, project=project, domain=domain, ref=ref, base_env=base_env)
            return _deploy.planned_envs(profile)

        def deploy_slots(profile, src, slots, **kw):
            self.slot_calls.append({"profile": profile, "src": src, "slots": slots, **kw})
            return self.slot_rc

        def deploy_factory(profile, src, **kw):
            self.factory_calls.append({"profile": profile, "src": src, **kw})
            return self.factory_rc

        # The apps stage always starts a child (it stops apps a profile dropped); none runs here.
        self.app_calls: list[dict[str, Any]] = []

        def deploy_apps(profile, src, **kw):
            self.app_calls.append({"profile": profile, "src": src, **kw})
            return 0

        async def listall(**kw):
            runs = list(self.runs)
            if kw.get("sort_by", ("", "desc"))[1] == "desc":
                runs.reverse()
            listed = 0
            for run in runs:
                if kw.get("in_phase") and run.phase not in kw["in_phase"]:
                    continue
                if any(run.pb2.labels.get(k) != v for k, v in kw.get("with_labels", {}).items()):
                    continue
                if listed == kw.get("limit", 100):  # the SDK's default, as the server honours it
                    return
                listed += 1
                yield run
            if kw.get("in_phase") == _deployer._LIVE:
                for name in self.live_runs:
                    yield SimpleNamespace(name=name)

        def get(name):
            for run in self.runs:
                if run.name == name:
                    return run
            raise LookupError(name)

        def say(message):
            self.said.append(message)
            return real_say(message)

        real_say = _deployer._say
        self.labels: list[dict[str, str]] = []

        async def submit(task_name, *, labels=None, **inputs):
            self.submissions.append((task_name, inputs))
            self.labels.append(labels or {})
            if task_name == _deploy.DEPLOYER_TASK:
                return SimpleNamespace(name="follow-up")
            return self.add_run("RUNNING", labels=labels, inputs=inputs)

        monkeypatch.setattr(_deployer, "SETTLE", 0)
        monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)
        monkeypatch.setattr(_deployer, "_say", say)
        monkeypatch.setattr(remote.Run, "get", get)
        monkeypatch.setattr(_deployer, "_submit", submit)
        monkeypatch.setattr(_deploy, "fetched_tree", fetched_tree)
        monkeypatch.setattr(_deploy, "check_in_tree", check_in_tree)
        monkeypatch.setattr(_deploy, "deploy_in_tree", deploy_in_tree)
        monkeypatch.setattr(_deploy, "deploy_slots", deploy_slots)
        monkeypatch.setattr(_deploy, "deploy_factory", deploy_factory)
        monkeypatch.setattr(_deploy, "deploy_apps", deploy_apps)
        monkeypatch.setattr(remote.Run, "listall", SimpleNamespace(aio=listall))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/repos/acme/direction"):
            return self.gh.handler(request)
        branch = path.removeprefix("/repos/acme/direction/commits/")
        if branch in self.direction_refs:
            return httpx.Response(200, json={"sha": self.direction_refs[branch]})
        return self.direction_gh.handler(
            httpx.Request(
                request.method, request.url.copy_with(path=path.replace("acme/direction", "acme/factory"))
            )
        )

    def add_run(self, phase, *, labels=None, inputs=None, output=None, record=None, name=None, started=True):
        async def outputs():
            if isinstance(run.output, Exception):
                raise run.output
            return (run.output,)

        run = SimpleNamespace(
            name=name or f"attempt-{len(self.runs)}",
            phase=phase,
            pb2=SimpleNamespace(
                labels=labels or {},
                # A run that never left the queue has no start time, as the server reports it.
                action=SimpleNamespace(
                    status=ActionStatus(start_time=Timestamp(seconds=1 if started else 0))
                ),
            ),
            inputs=inputs,
            output=output,
            record=record,
            error="",
            outputs=SimpleNamespace(aio=outputs),
        )
        self.runs.append(run)
        return run

    @property
    def last(self) -> dict[str, Any] | None:
        for run in reversed(self.runs):
            if run.phase == "SUCCEEDED" and isinstance(run.output, dict) and run.output.get("deployed"):
                if run.record is not None:
                    return cast(dict[str, Any], run.record)
                # The output, not the labels: a run that followed a moved head deployed a later pair.
                pair = {"sha": run.output["sha"]}
                if run.output["direction_sha"]:
                    pair["direction_sha"] = run.output["direction_sha"]
                return {**pair, "envs": run.output["envs"], "run": run.name, "deployed_at": "now"}
        return None

    @property
    def failed(self) -> dict[str, Any] | None:
        failed = []
        for run in reversed(self.runs):
            if run.phase == "SUCCEEDED" and run.output.get("deployed"):
                break
            if run.phase in _deployer._FAILED:
                failed.append(run)
        if not failed:
            return None
        latest = failed[0]
        labels = latest.pb2.labels
        pair = {"sha": labels["deploy-sha"]}
        if labels["deploy-direction"] != "none":
            pair["direction_sha"] = labels["deploy-direction"]
        return {
            **pair,
            "attempts": sum(r.pb2.labels == labels for r in failed),
            "last_error": latest.error,
            "at": "now",
        }

    def ship_profile(self, text: str) -> Path:
        """Put `profiles/<name>.toml` in the fetched tree, as a merged change to it would."""
        path = self.tree / "profiles" / f"{self.profile.name}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def ship_direction_profile(self, text: str | None = None) -> Path:
        """Put the profile in the direction repo's tree, by default the pod's own text plus a marker."""
        path = self.direction_tree / "profiles" / f"{self.profile.name}.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.profile.text + "\n# from direction\n" if text is None else text)
        return path

    def record(self, sha: str, direction_sha: str = "") -> dict[str, Any]:
        pair = {"sha": sha, **({"direction_sha": direction_sha} if direction_sha else {})}
        self.add_run(
            "SUCCEEDED",
            labels=_deployer._pair_labels(self.profile, sha, direction_sha),
            output={"deployed": True, "envs": ["factory"], "sha": sha, "direction_sha": direction_sha},
            record=pair,
        )
        return pair

    def hold(self, holder: str):
        """An older `deploy_pair` run that is live and holds the deploy lock on either path."""
        run = self.add_run(
            "RUNNING", labels=_deployer._pair_labels(self.profile, SHA, ""), name=holder, inputs={}
        )
        assert _deployer._acquire_ref(self.profile, self.app, holder, SHA)
        return run

    async def attempt(self, run) -> str:
        """Run a submitted `deploy_pair` attempt as Flyte would; what it said it did."""
        said = len(self.said)
        try:
            run.output = await _deployer.run_pair(self.profile, self.app, run.name, **run.inputs)
        except BaseException as exc:
            run.phase, run.error = "FAILED", str(exc)
            raise
        run.phase = "SUCCEEDED"
        lines = self.said[said:]
        return "; ".join(line for line in lines if line.startswith("deployed ")) or lines[-1]

    async def tick(self, run="run-me") -> str:
        message = await _deployer.run_once(self.profile, run, self.app, self.clock())
        if not message.startswith("submitted"):
            return message
        return await self.attempt(self.runs[-1])

    @property
    def deployed(self) -> bool:
        return bool(self.slot_calls or self.factory_calls)


@pytest.fixture
def world(tmp_path, monkeypatch, request):
    # The git-ref lock unless a test asks for both paths with `both_paths`: it is what production uses.
    return World(tmp_path, monkeypatch, consistent=getattr(request, "param", False))


#: Run a test on the git-ref lock (production) and on the consistent-listing rule.
both_paths = pytest.mark.parametrize("world", [False, True], indirect=True, ids=["git-ref", "listing"])


# --- ticks -------------------------------------------------------------------------------------


async def test_auto_off_does_nothing(world, capsys):
    world.profile = make_profile(f"file://{world.state}", auto=False)
    assert "off" in await _deployer.run_once(world.profile, "run-me", None)
    assert not world.deployed and not world.state.exists() and not world.gh.requests
    assert "[deploy] auto off" in capsys.readouterr().out


async def test_no_change_means_no_deploy(world, capsys):
    world.record(SHA)
    assert await world.tick() == f"up to date at {SHA}"
    assert not world.deployed and world.fetched == []
    assert f"[deploy] auto up to date at {SHA}" in capsys.readouterr().out


@both_paths
async def test_a_change_deploys_that_sha_once_and_records_it(world, capsys):
    world.record(OLD_SHA)
    result = await world.tick()
    assert result.startswith(f"deployed {SHA}")
    assert [sha for _, sha in world.fetched] == [SHA]
    (slots,), (factory,) = world.slot_calls, world.factory_calls
    assert slots["ref"] == SHA and slots["src"] == world.tree / "src"
    assert slots["slots"] == ["agent-one", "agent-two"]
    assert factory["src"] == world.tree / "src"
    last = world.last
    assert last["sha"] == SHA and last["run"] == world.runs[-1].name and last["deployed_at"]
    assert {
        "agent-one",
        "sub-one",
        "agent-two",
        "sub-two",
        "factory-setup",
        "factory",
        "factory-mcp",
        "factory-deployer",
        "gardening-scan",
        # State step 1: with the App, factory-reconcile heals the issue milestones.
        "factory-reconcile",
    } == set(last["envs"])
    out = capsys.readouterr().out
    assert f"[deploy] auto deployed {SHA}" in out


async def test_first_ever_tick_deploys(world):
    await world.tick()
    assert world.last["sha"] == SHA


async def test_the_clone_url_carries_a_token_that_is_never_logged(world, capsys):
    await world.tick()
    ((url, _),) = world.fetched
    assert url == "https://x-access-token:ghs_token1@github.com/acme/factory.git"
    out = capsys.readouterr()
    assert "ghs_token1" not in out.out + out.err


async def test_deploy_children_never_get_the_app_key(world, monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----")  # gitleaks:allow
    await world.tick()
    assert "GITHUB_APP_PRIVATE_KEY" not in world.slot_calls[0]["base_env"]
    assert "GITHUB_APP_PRIVATE_KEY" not in world.factory_calls[0]["env"]
    assert "GITHUB_APP_PRIVATE_KEY" not in world.profile_checks[0]["base_env"]


async def test_deploy_children_never_get_the_app_key_under_flytes_second_name(world, monkeypatch):
    # Flyte mounts a secret twice: as its `as_env_var` and as `_UNION_<SECRET KEY>` (`_pull_requests`).
    monkeypatch.setenv("_UNION_GITHUB_KEY_X", "-----BEGIN PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_x")
    await world.tick()
    for env in (
        world.slot_calls[0]["base_env"],
        world.factory_calls[0]["env"],
        world.profile_checks[0]["base_env"],
    ):
        assert "_UNION_GITHUB_KEY_X" not in env and "GITHUB_TOKEN" not in env


@pytest.mark.parametrize("stage", ["slots", "factory"])
async def test_a_failed_deploy_is_not_recorded_and_raises(world, stage):
    world.record(OLD_SHA)
    setattr(world, "slot_rc" if stage == "slots" else "factory_rc", 1)
    with pytest.raises(DeployError, match=f"{stage.removesuffix('s')} deploy of {SHA} failed"):
        await world.tick()
    assert world.last == {"sha": OLD_SHA}  # next tick retries
    assert bool(world.factory_calls) == (stage == "factory")  # factory only after slots succeed


async def test_failing_status_skips_without_recording(world, capsys):
    world.gh.statuses = [
        {"context": "ci/test", "state": "success"},
        {"context": "factory/verifier", "state": "failure"},
    ]
    result = await world.tick()
    assert result == f"skipped {SHA}: factory/verifier failing"
    assert f"[deploy] auto skipped {SHA}: factory/verifier failing" in capsys.readouterr().out
    assert not world.deployed and world.last is None


async def test_failing_check_run_skips(world):
    world.gh.check_runs = [{"name": "build", "status": "completed", "conclusion": "timed_out"}]
    assert "build failing" in await world.tick()
    assert not world.deployed and world.last is None


async def test_a_failing_standards_gate_skips_the_main_commit(world):
    # The engine's own gate blocks: its check-run failing is the commit failing, whatever else passed.
    world.gh.check_runs = [
        {"name": "Tests (not slow)", "status": "completed", "conclusion": "success"},
        {"name": "Tests (slow)", "status": "completed", "conclusion": "success"},
        {"name": "Standards gate", "status": "completed", "conclusion": "failure"},
    ]
    assert await world.tick() == f"skipped {SHA}: Standards gate failing"
    assert not world.deployed and world.last is None


async def test_pending_skips_this_tick(world):
    world.gh.statuses = [{"context": "ci/test", "state": "pending"}]
    assert "ci/test pending" in await world.tick()
    assert not world.deployed and world.last is None


async def test_green_runs_with_a_running_actions_suite_skip_this_tick(world):
    # The gate job's check-run does not exist yet, so the runs alone look finished.
    world.gh.check_runs = [{"name": "test", "status": "completed", "conclusion": "success"}]
    world.gh.check_suites = [
        {"app": {"slug": "github-actions"}, "status": "in_progress", "latest_check_runs_count": 1}
    ]
    result = await world.tick()
    assert "github-actions check suite in_progress" in result and "retrying next tick" in result
    assert not world.deployed and world.last is None


async def test_no_statuses_at_all_deploys(world):
    world.gh.statuses, world.gh.check_runs = [], []
    await world.tick()
    assert len(world.slot_calls) == len(world.factory_calls) == 1
    assert world.last["sha"] == SHA


async def test_without_checks_permission_the_deploy_line_says_check_runs_were_not_read(world, capsys):
    # Live u5bc4c03363f83585: the App lacked Checks: read. Deploying on statuses alone must be visible.
    world.gh.checks_forbidden = True
    await world.tick()
    assert len(world.slot_calls) == 1
    out = capsys.readouterr().out
    assert "deploying" in out and "check-runs unreadable" in out


async def test_a_factory_repo_is_required(world):
    # The profile refuses this at parse time; the tick still checks, for a profile built another way.
    repos = {k: v for k, v in world.profile.repos.items() if k != "factory"}
    world.profile = dataclasses.replace(world.profile, repos=repos)
    with pytest.raises(DeployError, match=r"needs a \[repo\] factory"):
        await world.tick()


async def test_github_errors_fail_the_run_without_the_token(world):
    world.gh.fail_body = "boom ghs_token1"
    with pytest.raises(DeployError) as err:
        await world.tick()
    assert "ghs_token1" not in str(err.value) and "HTTP 500" in str(err.value)


@both_paths
async def test_another_live_deployer_run_holds_the_lease(world):
    # Re-pointed from the lease file: an older live `deploy_pair` holds the lock, and a younger one
    # (two ticks that each saw no pair in flight) stands down.
    world.hold("run-other")
    younger = world.add_run(
        "RUNNING",
        labels=_deployer._pair_labels(world.profile, SHA, ""),
        inputs={"sha": SHA, "direction_sha": ""},
    )
    assert await world.attempt(younger) == "lease held by run-other"
    assert not world.deployed and not world.state.exists()


# --- every env the factory owns ----------------------------------------------------------------


def test_the_deployer_deploys_every_owned_env_including_the_subagents(world):
    envs = _deploy.planned_envs(world.profile)
    assert set(envs) == {
        "agent-one",
        "sub-one",
        "agent-two",
        "sub-two",
        "factory-setup",
        "factory",
        "factory-mcp",
        "factory-deployer",
        "gardening-scan",
        # State step 1: with the App, factory-reconcile heals the issue milestones.
        "factory-reconcile",
    }
    assert set(_deploy.FACTORY_ENVS) == {"factory", "factory-mcp", "factory-deployer", "gardening-scan"}


async def test_a_tick_hands_the_slot_deploy_every_slot_and_the_factory_deploy_the_rest(world):
    await world.tick()
    assert world.slot_calls[0]["slots"] == ["agent-one", "agent-two"]
    assert len(world.factory_calls) == 1
    assert set(world.last["envs"]) == set(_deploy.planned_envs(world.profile))


def test_a_slot_deploy_names_the_slot_and_its_subagent_env_to_the_child(world, monkeypatch, tmp_path):
    monkeypatch.undo()  # the real deploy_slots, with the child process replaced
    calls = []

    def run_child(*args, **kw):
        calls.append(args)
        return 0

    monkeypatch.setattr(_deploy, "_run_child", run_child)
    assert _deploy.deploy_slots(world.profile, tmp_path, ["agent-one"], project="p", domain="d", ref="x") == 0
    (_, script, _, env, _, _, names) = calls[0]
    assert script == _deploy.SLOTS_CHILD and names == "agent-one,factory-setup,sub-one"
    assert (
        env["AGENT_FACTORY_ENV_NAME"] == "agent-one" and env["AGENT_FACTORY_SUBAGENT_ENV_NAME"] == "sub-one"
    )


async def test_a_refusal_in_a_tick_is_a_failure_not_a_system_exit(world, monkeypatch):
    def refuse(*a, **k):
        raise Refused("refusing: nope")

    monkeypatch.setattr(_deploy, "check_slots", refuse)
    with pytest.raises(DeployError, match="refusing: nope"):
        await world.tick()


# --- shape of the task -------------------------------------------------------------------------


def test_the_task_runs_every_five_minutes_on_spot():
    (trigger,) = _deployer.deploy_if_changed.triggers
    assert trigger.name == "deploy-on-merge"
    assert cast(flyte.FixedRate, trigger.automation).interval_minutes == 5
    assert trigger.auto_activate is True
    assert _deployer.env.name == "factory-deployer"
    assert _deployer.env.interruptible is True
    assert _deployer.deploy_if_changed.report is True


# --- not a project -----------------------------------------------------------------------------


def _run(name, task, phase="RUNNING"):
    meta = {"action": {"metadata": {"task": {"id": {"name": task}}}}}
    return SimpleNamespace(name=name, phase=phase, to_dict=lambda: meta)


def test_deployer_runs_are_never_live_projects(monkeypatch):
    runs = [
        _run("tick", _deploy.DEPLOYER_TASK),
        _run("attempt", _deploy.DEPLOY_PAIR),
        _run("proj", _factory_base.SLOTS[0]),
        _run("tick-queued", _deploy.DEPLOYER_TASK, "QUEUED"),
    ]
    monkeypatch.setattr(remote.Run, "listall", lambda **kw: runs)
    assert [r.name for r in _factory.live_runs()] == ["proj"]
    assert [r.name for r in _factory.operated_live()] == ["proj"]
    assert [r.name for r in _factory.recent_runs()] == ["proj"]


# --- the deploy library ------------------------------------------------------------------------


def test_deploy_slots_attempts_every_slot_and_reports_a_failure(monkeypatch, tmp_path):
    profile = _profile.load(str(WORK))
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=3 if len(calls) == 1 else 0)

    monkeypatch.setattr(subprocess, "run", run)
    rc = _deploy.deploy_slots(
        profile, tmp_path, ["agent-one", "agent-two"], project="p", domain="d", ref="abc", base_env={}
    )
    assert rc == 3 and len(calls) == 2  # the second slot is still deployed
    argv, kw = calls[0]
    assert argv[-1] == "agent-one,factory-setup,sub-one" and argv[3:5] == [str(tmp_path), "0"]
    assert kw["env"]["AGENT_FACTORY_ENV_NAME"] == "agent-one" and kw["env"]["PYTHONPATH"] == str(tmp_path)


def test_deploy_factory_runs_the_factory_child(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    profile = _profile.load(str(WORK))
    assert _deploy.deploy_factory(profile, tmp_path, project="p", domain="d", env={}, dry_run=True) == 0
    assert seen["argv"][2] is _deploy.FACTORY_CHILD and seen["argv"][4] == "1"


def git(*args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@example.org", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_fetched_tree_checks_out_exactly_the_sha(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f.txt").write_text("one")
    git("add", ".", cwd=origin)
    git("commit", "-qm", "one", cwd=origin)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    (origin / "f.txt").write_text("two")
    git("commit", "-qam", "two", cwd=origin)
    with _deploy.fetched_tree(str(origin), sha) as tree:
        assert (tree / "f.txt").read_text() == "one"
        kept = tree
    assert not kept.exists()


def test_fetched_tree_errors_never_carry_the_credential():
    url = "https://x-access-token:s3cr3t-token@127.0.0.1:1/acme/factory.git"
    with pytest.raises(DeployError) as err, _deploy.fetched_tree(url, SHA):
        pass
    assert "s3cr3t-token" not in str(err.value) and "git fetch" in str(err.value)


def test_redact_hides_url_passwords():
    assert (
        _deploy.redact("fatal: 'https://u:pw@github.com/a/b.git' x")
        == "fatal: 'https://u:***@github.com/a/b.git' x"
    )


def test_live_phases_are_phases_the_server_filter_accepts():
    # `Run.listall(in_phase=...)` maps each name to ACTION_PHASE_<name>; an unknown one raises
    # ValueError on every tick, before the deployer can decide anything.
    from flyteidl2.common import phase_pb2  # type: ignore[import-untyped]  # no py.typed marker

    from flyte_agent_factory import _deployer

    for phase in _deployer._LIVE:
        phase_pb2.ActionPhase.Value(f"ACTION_PHASE_{phase.upper()}")


def test_a_deploying_profile_carries_the_mcp_public_url(tmp_path):
    # The deployer has no deploy shell to pass FACTORY_MCP_PUBLIC_URL; without it in the profile an
    # auto-deploy would ship factory-mcp with no allowed hosts or origins.
    from pathlib import Path

    from flyte_agent_factory import _profile

    example = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()
    path = tmp_path / "deploying.toml"
    path.write_text(
        example.replace("auto = false", "auto = true").replace(
            '# factory = "git@github.com:you/factory.git"', 'factory = "git@github.com:you/factory.git"'
        )
    )
    profile = _profile.load(str(path))
    assert profile.deploy.auto
    assert profile.mcp is not None
    assert profile.mcp.public_url.startswith("https://")


# --- project listings ask the server -----------------------------------------------------------


class ServerRuns:
    """A `Run.listall` that honours `task_name`, `in_phase` and `limit` as the server does."""

    def __init__(self, runs):
        self.runs = runs  # newest first
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw):
        self.calls.append(kw)
        runs = self.runs
        if "task_name" in kw:
            runs = [r for r in runs if _factory_base._task_name(r) == kw["task_name"]]
        if "in_phase" in kw:
            runs = [r for r in runs if r.phase in kw["in_phase"]]
        return runs[: kw.get("limit", 100)]


def _busy_project(monkeypatch):
    ticks = [_run(f"tick-{i}", _deploy.DEPLOYER_TASK, "SUCCEEDED") for i in range(300)]
    server = ServerRuns([*ticks, _run("proj", _factory_base.SLOTS[0])])  # the project is older than 300 ticks
    monkeypatch.setattr(remote.Run, "listall", server)
    return server


def test_a_live_project_is_found_behind_three_hundred_deployer_runs(monkeypatch):
    server = _busy_project(monkeypatch)
    assert [r.name for r in _factory.live_runs()] == ["proj"]
    assert [r.name for r in _factory.operated_live()] == ["proj"]
    assert all(c["in_phase"] == _factory_base.SERVER_LIVE for c in server.calls)
    slot_calls = [c for c in server.calls if "task_name" in c]
    assert {c["task_name"] for c in slot_calls} == set(_factory_base.SLOTS)


def test_server_live_phases_are_phases_the_server_accepts_and_live_keeps_waiting():
    from flyteidl2.common import phase_pb2

    for phase in _factory_base.SERVER_LIVE:
        phase_pb2.ActionPhase.Value(f"ACTION_PHASE_{phase}")
    assert set(_factory_base.SERVER_LIVE) < set(_factory_base.LIVE) and "WAITING" in _factory_base.LIVE


def test_recent_runs_and_headroom_query_each_slot(monkeypatch):
    server = _busy_project(monkeypatch)
    assert [r.name for r in _factory.recent_runs()] == ["proj"]
    recent = [c for c in server.calls if "updated_at" in c]
    assert {c["task_name"] for c in recent} == set(_factory_base.SLOTS) and all(
        c["limit"] <= 20 for c in recent
    )
    server.calls.clear()
    proj = server.runs[-1]
    proj.get_report = lambda: ""
    assert _factory_meters.slot_headroom(_factory_base.SLOTS) == {s: None for s in _factory_base.SLOTS}
    # Each slot's newest runs once, plus one bounded listing of the idle-login probe (board #15).
    slot_calls = [c for c in server.calls if c["task_name"] in _factory_base.SLOTS]
    assert sorted(c["task_name"] for c in slot_calls) == sorted(_factory_base.SLOTS)
    assert all(c["limit"] <= 5 and c["sort_by"] == ("created_at", "desc") for c in slot_calls)
    probes = [c for c in server.calls if c not in slot_calls]
    assert [c["task_name"] for c in probes] == ["factory.read_login_meter"] and probes[0]["limit"] <= 100


# --- failed deploys retry a bounded number of times --------------------------------------------


async def _fail_ticks(world, n):
    world.factory_rc = 1
    for _ in range(n):
        with pytest.raises(DeployError):
            await world.tick()


@both_paths
async def test_a_failure_is_recorded_with_its_sha_and_attempts(world):
    world.record(OLD_SHA)
    await _fail_ticks(world, 1)
    failed = world.failed
    assert failed["sha"] == SHA and failed["attempts"] == 1 and failed["at"]
    assert "factory deploy of" in failed["last_error"]
    await _fail_ticks(world, 1)
    assert world.failed["attempts"] == 2
    assert world.last == {"sha": OLD_SHA}


@both_paths
async def test_the_same_sha_is_tried_three_times_then_given_up_on(world, capsys):
    await _fail_ticks(world, 3)
    assert len(world.factory_calls) == 3
    capsys.readouterr()
    for _ in range(2):  # every later tick says so, and deploys nothing
        assert await world.tick() == f"giving up on {SHA} after 3 attempts; waiting for a new commit"
    out = capsys.readouterr().out
    assert out.count(f"[deploy] auto giving up on {SHA} after 3 attempts; waiting for a new commit") == 2
    assert len(world.factory_calls) == 3 and world.failed["attempts"] == 3


@both_paths
async def test_a_new_sha_resets_the_attempts(world):
    await _fail_ticks(world, 3)
    world.gh.head_sha = "c" * 40
    world.factory_rc = 0
    assert (await world.tick()).startswith(f"deployed {'c' * 40}")
    assert world.failed is None  # a success clears the record


@both_paths
async def test_a_new_sha_after_a_failure_counts_from_one(world):
    await _fail_ticks(world, 2)
    world.gh.head_sha = "c" * 40
    await _fail_ticks(world, 1)
    assert world.failed["sha"] == "c" * 40 and world.failed["attempts"] == 1


async def test_a_failure_record_never_holds_a_credential(world, monkeypatch):
    def boom(url, sha):
        raise DeployError("git fetch failed: https://x-access-token:s3cr3t@github.com/a/b.git")

    monkeypatch.setattr(_deploy, "fetched_tree", boom)
    with pytest.raises(DeployError):
        await world.tick()
    assert "s3cr3t" not in world.failed["last_error"]


def test_deploy_pair_has_a_bounded_runtime_and_queue():
    timeout = cast(flyte.Timeout, _deployer.deploy_pair.timeout)
    assert timeout.max_runtime == timedelta(minutes=60)
    assert timeout.max_queued_time == timedelta(minutes=5)


# --- the task itself ---------------------------------------------------------------------------


async def test_a_missing_app_key_is_a_reported_failure(world, monkeypatch):
    reports = []

    async def replace(html, do_flush=False):
        reports.append(html)

    monkeypatch.setattr(_deployer, "PROFILE", world.profile)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="r")))
    monkeypatch.setattr(flyte.report, "replace", SimpleNamespace(aio=replace))
    with pytest.raises(DeployError, match="GITHUB_APP_PRIVATE_KEY"):
        await _deployer.deploy_if_changed.func()
    assert len(reports) == 1 and "failed" in reports[0] and "GITHUB_APP_PRIVATE_KEY" in reports[0]


# --- the profile and standards ship with the merged tree ---------------------------------------


async def test_the_children_get_the_merged_trees_profile(world, capsys):
    changed = world.profile.text.replace('subagent_env = "sub-one"', 'subagent_env = "sub-one-v2"', 1)
    world.ship_profile(changed)
    await world.tick()
    (slots,), (factory,) = world.slot_calls, world.factory_calls
    assert slots["profile"].slots[0].subagent_env == "sub-one-v2"
    assert factory["profile"] is slots["profile"]
    assert slots["profile"] != world.profile
    out = capsys.readouterr().out
    assert f"[deploy] auto profile: profiles/{world.profile.name}.toml from {SHA}" in out


async def test_the_standards_come_from_the_tree_too(world):
    """Kept name (#72): a profile shipped in the engine tree gets the tree's standards only when its
    `[standards] dir` names them; the engine's own `standards/` is never a default (docs/standards.md)."""
    world.ship_profile(world.profile.text + '\n[standards]\ndir = "standards"\n')
    standards = world.tree / "standards"
    standards.mkdir()
    (standards / "engineering.md").write_text("# TREE-ENGINEERING-MARKER\n")
    (standards / "security.md").write_text("# tree security\n")
    await world.tick()
    deployed = world.slot_calls[0]["profile"].deployment_text()
    payload = deployed.splitlines()[0].split('"')[1]
    assert "TREE-ENGINEERING-MARKER" in _standards.decode(payload)["engineering.md"]


async def test_an_engine_tree_profile_without_a_standards_dir_ships_no_standards(world):
    world.ship_profile(world.profile.text)
    (world.tree / "standards").mkdir()
    (world.tree / "standards" / "engineering.md").write_text("# TREE-ENGINEERING-MARKER\n")
    await world.tick()
    deployed = world.slot_calls[0]["profile"].deployment_text()
    assert _standards.decode(deployed.splitlines()[0].split('"')[1]) == {}


async def test_without_a_profile_in_the_tree_the_pod_profile_is_used(world, capsys):
    await world.tick()
    # The pod's profile, read back by the tree's code from the text it was handed.
    for call in (world.slot_calls[0], world.factory_calls[0]):
        assert call["profile"] == world.profile
        assert call["profile"].deployment_text() == world.profile.deployment_text()
    out = capsys.readouterr().out
    assert f"[deploy] auto no profiles/{world.profile.name}.toml in {SHA}; using the deployed profile" in out
    assert world.last["sha"] == SHA


@pytest.mark.parametrize("breakage", ["name = ", "[[slots]]\nenv = 1\n", "\x00not toml"])
async def test_an_invalid_tree_profile_deploys_nothing_and_records_nothing(world, breakage):
    world.record(OLD_SHA)
    world.ship_profile(world.profile.text + "\n" + breakage)
    with pytest.raises(DeployError, match=rf"profiles/{world.profile.name}.toml.*{SHA}"):
        await world.tick()
    assert not world.deployed and world.last == {"sha": OLD_SHA}


async def test_the_deployed_envs_are_the_trees(world):
    world.ship_profile(
        world.profile.text.replace('"agent-two"', '"agent-three"').replace('"sub-two"', '"sub-three"')
    )
    await world.tick()
    assert {"agent-three", "sub-three"} <= set(world.last["envs"]) and "agent-two" not in world.last["envs"]


# --- a merged profile that retires or edits nothing special ---------------------------------------


async def test_a_merged_profile_with_a_self_agent_table_deploys_its_slots_and_says_it_was_ignored(
    world, capsys
):
    # [self_agent] is retired and ignored, never a reason to stall auto-deploy; "x" is not deployed.
    world.record(OLD_SHA)
    world.ship_profile(
        world.profile.text + '\n[self_agent]\nenv = "x"\nclaude_login = "l"\ndeploy_key = "k"\n'
    )
    await world.tick()
    assert world.last["sha"] == SHA
    assert "x" not in world.slot_calls[0]["slots"]
    assert "[self_agent] was retired and is ignored" in capsys.readouterr().err


async def test_the_deployer_deploys_whatever_the_merged_slots_say(world):
    # No env is held back as "the running agent": a merged profile's slots are all deployed.
    world.ship_profile(world.profile.text.replace('"agent-two"', '"agent-own"'))
    await world.tick()
    assert world.slot_calls[0]["slots"] == ["agent-one", "agent-own"]


@both_paths
async def test_an_unexpected_failure_still_counts_towards_the_attempt_cap(world, monkeypatch):
    # An attempt is counted before the deploy starts, so a crash, a kill at max_runtime or a
    # preemption -- after which nothing of ours runs -- cannot retry forever.
    def explode(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(_deploy, "deploy_slots", explode)
    world.record(OLD_SHA)
    for _ in range(_deployer.MAX_ATTEMPTS):
        with pytest.raises(DeployError, match="disk full"):
            await world.tick()
    assert world.failed["attempts"] == _deployer.MAX_ATTEMPTS
    assert "giving up" in await world.tick()


def test_the_profile_imports_without_pyjwt():
    # Images built before the deployer have no jwt; parsing a profile must not need it.
    import subprocess
    import sys

    code = (
        "import sys; sys.modules['jwt'] = None\n"
        "from flyte_agent_factory import _profile, _github\n"
        "print(_github.parse_repo('git@github.com:o/r.git'))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip()


async def test_a_refusal_is_recorded_with_its_reason(world, monkeypatch):
    def refuse(*_a, **_k):
        raise _deploy.Refused("refusing: test refusal")

    monkeypatch.setattr(_deploy, "check_slots", refuse)
    world.record(OLD_SHA)
    with pytest.raises(_deploy.DeployError, match="test refusal"):
        await world.tick()
    assert "test refusal" in world.failed["last_error"]


async def test_no_object_storage_call_is_made(world, monkeypatch):
    import flyte.storage

    def refuse(*args, **kwargs):
        raise AssertionError("deployer used object storage")

    for name in ("exists", "get_stream", "put_stream", "get_underlying_filesystem"):
        monkeypatch.setattr(flyte.storage, name, refuse)
    await world.tick()
    assert world.last["sha"] == SHA


def test_the_deployer_image_can_import_the_mcp_app_it_deploys():
    # Live (deploy-on-merge u556ef509c346e751): deploying factory-mcp in the deployer pod failed with
    # "mcp is not installed. Please install 'flyte[mcp]'": the agent image has the bare SDK.
    from flyte_agent_factory import _deployer

    packages = [
        p for layer in cast(flyte.Image, _deployer.env.image)._layers for p in getattr(layer, "packages", ())
    ]
    for name in ("mcp", "starlette", "uvicorn"):
        assert any(
            p == name or p.startswith((f"{name}>", f"{name}=", f"{name}<", f"{name}[")) for p in packages
        ), name


# --- [direction]: the profile comes from a separate repository ---------------------------------


@pytest.fixture
def directed(world):
    """A world whose profile has [direction], with a valid profile in the direction repo."""
    world.profile = make_direction_profile(f"file://{world.state}")
    world.ship_direction_profile()
    return world


def test_direction_defaults_and_validation():
    profile = make_direction_profile("file:///s")
    assert profile.direction == _profile.Direction(
        "git@github.com:acme/direction.git", f"profiles/{profile.name}.toml", ""
    )
    pinned = make_direction_profile("file:///s", ref="release")
    assert pinned.direction is not None
    assert pinned.direction.ref == "release"
    assert make_profile("file:///s").direction is None


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('repo = "git@gitlab.com:acme/direction.git"', "must be a github.com repository"),
        ('repo = "git@github.com:acme/d.git"\nprofile = "../x.toml"', "path inside the repository"),
        ('repo = "git@github.com:acme/d.git"\nprofile = "/etc/x.toml"', "path inside the repository"),
        ('repo = "git@github.com:acme/d.git"\nbranch = "x"', "unknown key"),
        ('profile = "profiles/x.toml"', "missing key 'repo'"),
    ],
)
def test_direction_is_validated(table, message):
    with pytest.raises(_profile.ProfileError, match=message):
        _profile.parse(make_profile("file:///s").text + f"\n[direction]\n{table}\n", "t")


def test_direction_requires_auto():
    text = make_profile("file:///s", auto=False).text + '\n[direction]\nrepo = "git@github.com:acme/d.git"\n'
    with pytest.raises(_profile.ProfileError, match=r"\[direction\] requires \[deploy\] auto"):
        _profile.parse(text, "t")


async def test_a_new_pair_deploys_and_records_both_shas(directed, capsys):
    directed.record(OLD_SHA)
    result = await directed.tick()
    assert result.startswith(f"deployed {SHA} with direction {DIRECTION_SHA}")
    assert [sha for _, sha in directed.fetched] == [SHA, DIRECTION_SHA]
    assert directed.last["sha"] == SHA and directed.last["direction_sha"] == DIRECTION_SHA
    assert directed.failed is None


async def test_only_the_direction_moving_deploys(directed):
    directed.record(SHA, OLD_DIRECTION_SHA)
    await directed.tick()
    assert len(directed.slot_calls) == 1 and directed.slot_calls[0]["ref"] == SHA
    assert directed.last["direction_sha"] == DIRECTION_SHA


async def test_only_the_engine_moving_deploys(directed):
    directed.record(OLD_SHA, DIRECTION_SHA)
    await directed.tick()
    assert len(directed.slot_calls) == 1


async def test_the_same_pair_is_up_to_date(directed, capsys):
    directed.record(SHA, DIRECTION_SHA)
    assert await directed.tick() == f"up to date at {SHA} with direction {DIRECTION_SHA}"
    assert not directed.deployed and directed.fetched == []


async def test_an_old_last_json_without_direction_sha_deploys_once(directed):
    directed.record(SHA)
    await directed.tick()
    assert len(directed.slot_calls) == 1 and directed.last["direction_sha"] == DIRECTION_SHA
    await directed.tick()
    assert len(directed.slot_calls) == 1


async def test_the_deployed_slots_and_factory_get_the_direction_profile_text(directed):
    directed.ship_profile(directed.profile.text + "\n# from the engine tree\n")  # must lose
    await directed.tick()
    (slots,), (factory,) = directed.slot_calls, directed.factory_calls
    assert slots["profile"] is factory["profile"]
    assert slots["profile"].text.endswith("# from direction\n")
    # What lands in a pod is the direction text, with the engine tree's code.
    assert slots["profile"].deployment_text().endswith("# from direction\n")
    assert slots["src"] == directed.tree / "src"


async def test_the_standards_default_to_the_engine_tree_and_a_relative_dir_resolves_there(directed):
    """The name is kept (the review contract forbids removing a test); the old behaviour is gone.

    The engine tree's `standards/` is never read: the direction repository's `standards/` is the default and
    a relative `[standards] dir` resolves beside the profile, in the direction repository.
    """
    for tree, marker in (
        (directed.tree, "ENGINE-TREE-MARKER"),
        (directed.direction_tree, "DIRECTION-MARKER"),
    ):
        (tree / "standards").mkdir()
        (tree / "standards" / "engineering.md").write_text(f"# {marker}\n")
        (tree / "standards" / "security.md").write_text("# s\n")
    await directed.tick()
    files = _standards.decode(
        directed.slot_calls[0]["profile"].deployment_text().splitlines()[0].split('"')[1]
    )
    assert "DIRECTION-MARKER" in files["engineering.md"]
    assert "ENGINE-TREE-MARKER" not in files["engineering.md"]

    own = directed.direction_tree / "own"
    own.mkdir()
    (own / "engineering.md").write_text("# OWN-DIR-MARKER\n")
    (own / "security.md").write_text("# s\n")
    directed.ship_direction_profile(directed.profile.text + '\n[standards]\ndir = "own"\n')
    directed.runs.clear()
    await directed.tick()
    files = _standards.decode(
        directed.slot_calls[1]["profile"].deployment_text().splitlines()[0].split('"')[1]
    )
    assert "OWN-DIR-MARKER" in files["engineering.md"]


async def test_the_ref_picks_the_direction_branch(directed):
    directed.profile = make_direction_profile(f"file://{directed.state}", ref="release")
    directed.ship_direction_profile()  # the direction file carries the same [direction] (ref included)
    directed.direction_refs["release"] = "f" * 40
    await directed.tick()
    assert directed.last["direction_sha"] == "f" * 40
    assert [sha for _, sha in directed.fetched] == [SHA, "f" * 40]


async def test_an_unreadable_direction_fails_the_tick_and_deploys_nothing(directed):
    directed.direction_fetch_error = "git fetch of dddd failed: not found"
    with pytest.raises(DeployError, match="git fetch"):
        await directed.tick()
    assert not directed.deployed and directed.last is None
    assert directed.failed["sha"] == SHA and directed.failed["direction_sha"] == DIRECTION_SHA
    assert directed.failed["attempts"] == 1 and "git fetch" in directed.failed["last_error"]


async def test_a_direction_head_that_cannot_be_resolved_fails_the_tick(directed):
    directed.direction_gh.installed = False
    with pytest.raises(DeployError, match="direction repo acme/direction cannot be read, nothing deployed"):
        await directed.tick()
    assert not directed.deployed and directed.last is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (None, "is not in the direction repository"),
        ("name = 'x'\nnot toml [", "not valid TOML"),
        ("bogus = 1\n", "unknown top-level key"),
    ],
)
async def test_an_absent_or_invalid_direction_profile_fails_the_tick(directed, text, message):
    path = directed.direction_tree / "profiles" / f"{directed.profile.name}.toml"
    if text is None:
        path.unlink()
    elif text.startswith("bogus"):  # a valid profile with a key before its first table
        path.write_text(text + directed.profile.text)
    else:
        path.write_text(text)
    with pytest.raises(DeployError, match=message):
        await directed.tick()
    assert not directed.deployed and directed.last is None  # never the pod's profile
    assert directed.failed["attempts"] == 1


@pytest.mark.parametrize("change", ["drop", "elsewhere"])
async def test_a_direction_profile_that_changes_its_own_direction_fails_the_tick(directed, change):
    # qa: a direction file without [direction] deployed the deployer with direction off; every later direction
    # commit was then ignored with no error. A different [direction] would silently follow another repo.
    text = directed.profile.text
    start = text.index("[direction]")
    end = text.find("\n[", start + 1)
    table = text[start:] if end == -1 else text[start:end]
    if change == "drop":
        shipped = text.replace(table, "")
    else:
        shipped = text.replace(table, table.replace("acme/direction.git", "acme/elsewhere.git"))
    directed.ship_direction_profile(shipped)
    with pytest.raises(DeployError, match=r"\[direction\]"):
        await directed.tick()
    assert not directed.deployed


async def test_a_direction_profile_naming_the_same_repo_over_https_is_the_same_direction(directed):
    text = directed.profile.text
    directed.ship_direction_profile(
        text.replace("git@github.com:acme/direction.git", "https://github.com/acme/direction")
    )
    await directed.tick()
    assert directed.deployed


async def test_a_direction_profile_for_another_name_fails_the_tick(directed):
    directed.ship_direction_profile(
        directed.profile.text.replace(f'name = "{directed.profile.name}"', 'name = "other"')
    )
    with pytest.raises(DeployError, match="is profile 'other'"):
        await directed.tick()
    assert not directed.deployed


async def test_failures_are_counted_per_pair(directed):
    directed.direction_fetch_error = "boom"
    for _ in range(_deployer.MAX_ATTEMPTS):
        with pytest.raises(DeployError):
            await directed.tick()
    assert directed.failed["attempts"] == _deployer.MAX_ATTEMPTS
    assert "giving up" in await directed.tick()
    # A new direction commit is a new pair: the count starts again.
    directed.direction_gh.head_sha = "9" * 40
    with pytest.raises(DeployError):
        await directed.tick()
    assert directed.failed["attempts"] == 1


async def test_a_failing_direction_commit_blocks_the_deploy(directed):
    directed.direction_gh.statuses = [{"context": "direction/profile-pins", "state": "failure"}]
    result = await directed.tick()
    assert result == f"skipped direction {DIRECTION_SHA}: direction/profile-pins failing"
    assert not directed.deployed and directed.fetched == [] and directed.failed is None


async def test_a_pending_direction_commit_waits(directed):
    directed.direction_gh.statuses = [{"context": "direction/profile-pins", "state": "pending"}]
    assert "direction/profile-pins pending; retrying next tick" in await directed.tick()
    assert not directed.deployed


async def test_a_direction_commit_without_statuses_deploys(directed):
    directed.direction_gh.statuses = []
    await directed.tick()
    assert directed.deployed


async def test_a_failing_engine_commit_still_blocks_with_a_direction(directed):
    directed.gh.statuses = [{"context": "factory/verifier", "state": "failure"}]
    assert (await directed.tick()).startswith(f"skipped {SHA}")
    assert not directed.deployed


async def test_without_direction_the_tree_profile_and_pod_fallback_are_unchanged(world):
    await world.tick()
    deployed = world.slot_calls[0]["profile"]  # no profile in the tree: the pod's
    assert deployed == world.profile and deployed.deployment_text() == world.profile.deployment_text()
    assert set(world.last) == {"sha", "deployed_at", "envs", "run"}
    assert [url for url, _ in world.fetched if "direction" in url] == []


# --- a push that lands during a deploy ----------------------------------------------------------

NEW_SHA = "c" * 40


@both_paths
async def test_a_head_that_moves_during_the_deploy_is_deployed_by_the_same_run(world, monkeypatch):
    # The webhook drops a push while this run is live, so the run itself must pick it up.
    def push_during_deploy(profile, src, **kw):
        world.factory_calls.append({"profile": profile, "src": src, **kw})
        world.gh.head_sha = NEW_SHA
        return 0

    world.record(OLD_SHA)
    monkeypatch.setattr(_deploy, "deploy_factory", push_during_deploy)
    result = await world.tick()
    assert [call["ref"] for call in world.slot_calls] == [SHA, NEW_SHA]
    assert world.last["sha"] == NEW_SHA and world.last["run"] == world.runs[-1].name
    assert f"deployed {SHA}" in result and f"deployed {NEW_SHA}" in result
    assert not world.state.exists()


@pytest.mark.parametrize("consistent", [False, True], ids=["git-ref", "listing"])
async def test_a_direction_head_that_moves_during_the_deploy_is_deployed_by_the_same_run(
    directed, monkeypatch, consistent
):
    monkeypatch.setattr(_deployer, "RUN_LISTING_CONSISTENT", consistent)

    def push_during_deploy(profile, src, **kw):
        directed.factory_calls.append({"profile": profile, "src": src, **kw})
        directed.direction_gh.head_sha = OLD_DIRECTION_SHA
        return 0

    monkeypatch.setattr(_deploy, "deploy_factory", push_during_deploy)
    await directed.tick()
    assert len(directed.factory_calls) == 2
    assert directed.last == {**directed.last, "sha": SHA, "direction_sha": OLD_DIRECTION_SHA}


@both_paths
async def test_a_head_that_keeps_moving_is_followed_at_most_three_times(world, monkeypatch):
    heads = iter(c * 40 for c in "123456789")

    def push_during_deploy(profile, src, **kw):
        world.factory_calls.append({"profile": profile, "src": src, **kw})
        world.gh.head_sha = next(heads)
        return 0

    monkeypatch.setattr(_deploy, "deploy_factory", push_during_deploy)
    await world.tick()
    assert len(world.factory_calls) == _deployer.MAX_DEPLOYS_PER_RUN == 3
    # The fourth head is left for the next trigger, which finds it not yet deployed.
    assert world.last["sha"] == "2" * 40 != world.gh.head_sha


@both_paths
async def test_a_moved_head_with_failing_ci_is_left_for_the_tick(world, monkeypatch):
    # A follow-up passes the tick's own checks: here the new head's CI is failing.
    def push_during_deploy(profile, src, **kw):
        world.factory_calls.append({"profile": profile, "src": src, **kw})
        world.gh.head_sha = NEW_SHA
        world.gh.statuses = [{"context": "factory/verifier", "state": "failure"}]
        return 0

    monkeypatch.setattr(_deploy, "deploy_factory", push_during_deploy)
    await world.tick()
    assert [call["ref"] for call in world.slot_calls] == [SHA]
    assert world.last["sha"] == SHA


@both_paths
async def test_an_unmoved_head_after_a_deploy_ends_the_run(world):
    world.record(OLD_SHA)
    result = await world.tick()
    assert len(world.factory_calls) == 1
    assert result.startswith(f"deployed {SHA}")


# --- a profile key released together with the code that reads it -------------------------------
#
# The running deployer is the previous engine build. Everything that decides what is deployed (which
# profile keys exist, the slot recipes, the factory envs, the child scripts) must be the code being
# deployed, or a key released with its code is refused, or deployed without what it adds.

WIDGET = '\n[widget]\ncolour = "blue"\n'

# Appended to the copied engine's `_profile.py`: a release that adds [widget], which sets a slot
# recipe variable. Written against this checkout's code, as a real release would be.
_WIDGET_PROFILE = """

def _widget(self):
    return tomllib.loads(self.text).get("widget")


Profile.widget = property(_widget)
_deploy_env_without_widget = Profile.deploy_env


def _deploy_env_with_widget(self, slot):
    env = _deploy_env_without_widget(self, slot)
    if self.widget:
        env["CLAUDE_WIDGET"] = self.widget["colour"]
    return env


Profile.deploy_env = _deploy_env_with_widget
"""

# ... and to its `_deploy.py`: [widget] also enables a factory environment.
_WIDGET_ENVS = """

_factory_envs_without_widget = factory_envs


def factory_envs(profile):
    return _factory_envs_without_widget(profile) | ({"factory-widget"} if profile.widget else set())
"""
_WIDGET_CHILD_ANCHOR = "check_environments(*envs)\n# flyte.deploy"
_WIDGET_CHILD = """
FACTORY_CHILD = FACTORY_CHILD.replace(
    "check_environments(*envs)\\n# flyte.deploy",
    "if _deployer.PROFILE.widget:\\n"
    "    envs += (flyte.TaskEnvironment(name='factory-widget'),)\\n"
    "check_environments(*envs)\\n# flyte.deploy",
)
"""

# Put into every deploy child of the copied engine: no Flyte backend, and a log of what each deployed.
_FLYTE_STUB = """
SLOTS_CHILD, FACTORY_CHILD, APPS_CHILD = (
    child.replace("import os, sys, flyte\\n", "import os, sys, flyte\\n" + _FLYTE_STUB, 1)
    for child in (SLOTS_CHILD, FACTORY_CHILD, APPS_CHILD)
)
"""
_FLYTE_STUB_BODY = r'''
_FLYTE_STUB = """
import json as _json
flyte.init_from_api_key = lambda **kw: None


class _App:
    endpoint = "https://factory.example"

    def watch(self, wait_for):
        return self


class _Deployed:
    def __init__(self, name):
        self.name, self.deployed_app = name, _App()

    def get_name(self):
        return self.name


class _Deployment:
    def __init__(self, envs):
        self.envs, pending = {}, list(envs)
        while pending:
            env = pending.pop()
            self.envs[env.name] = _Deployed(env.name)
            pending.extend(env.depends_on)

    def summary_repr(self):
        return ""


def _stub_deploy(*envs, dry_run):
    deployment = _Deployment(envs)
    with open(os.environ["TEST_DEPLOY_LOG"], "a") as log:
        entry = {"envs": sorted(deployment.envs), "widget": os.environ.get("CLAUDE_WIDGET")}
        log.write(_json.dumps(entry) + "\\n")
    return [deployment]


flyte.deploy = _stub_deploy


# The control plane has no app: the apps stage finds nothing to stop.
class _NoApps:

    @staticmethod
    def listall(limit=100):
        return []

    @staticmethod
    def get(name):
        from connectrpc.code import Code
        from connectrpc.errors import ConnectError

        raise ConnectError(Code.NOT_FOUND, name)


import flyte.remote as _remote
_remote.App = _NoApps
"""
'''


def real_engine(world, monkeypatch, *, knows: tuple[str, ...] = (), widget: bool = False, child: bool = True):
    """Make the fetched engine a copy of this package, run for real down to `flyte.deploy` (stubbed).

    `knows` adds top-level keys its `parse` accepts; `widget` makes it the release that adds [widget],
    and `child=False` leaves the factory env [widget] enables out of its factory child (a broken release).
    """
    package = world.tree / "src" / "flyte_agent_factory"
    shutil.copytree(PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    profile_py, deploy_py = package / "_profile.py", package / "_deploy.py"
    source = profile_py.read_text()
    anchor = '            "board",\n'
    assert source.count(anchor) == 1  # the known top-level keys in `parse`
    known = "".join(f'            "{key}",\n' for key in (*knows, *(("widget",) if widget else ())))
    profile_py.write_text(source.replace(anchor, anchor + known) + (_WIDGET_PROFILE if widget else ""))
    deploy_source = deploy_py.read_text()
    assert deploy_source.count(_WIDGET_CHILD_ANCHOR) == 1
    if widget:
        deploy_source += _WIDGET_ENVS + (_WIDGET_CHILD if child else "")
    deploy_py.write_text(deploy_source + _FLYTE_STUB_BODY + _FLYTE_STUB)
    world.deploy_log = world.tree.parent / "deploy-log.jsonl"
    monkeypatch.setenv("TEST_DEPLOY_LOG", str(world.deploy_log))
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "unused")
    monkeypatch.setattr(_deploy, "check_in_tree", REAL_CHECK_IN_TREE)
    monkeypatch.setattr(_deploy, "deploy_in_tree", REAL_DEPLOY_IN_TREE)


def deploys(world) -> list[dict[str, Any]]:
    """What the copied engine's deploy children deployed, in order."""
    path = world.deploy_log
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def record_pair(world: World, sha: str, direction_sha: str) -> dict[str, Any]:
    return world.record(sha, direction_sha)


async def test_a_new_key_that_adds_an_env_and_a_slot_variable_deploys_both(directed, monkeypatch, capsys):
    real_engine(directed, monkeypatch, widget=True)
    record_pair(directed, OLD_SHA, DIRECTION_SHA)
    directed.ship_direction_profile(directed.profile.text + WIDGET)
    result = await directed.tick()
    assert result.startswith(f"deployed {SHA} with direction {DIRECTION_SHA}")
    slots, (factory,) = deploys(directed)[:-1], deploys(directed)[-1:]
    assert [s["envs"] for s in slots] == [
        ["agent-one", "factory-setup", "sub-one"],
        ["agent-two", "factory-setup", "sub-two"],
    ]
    assert all(s["widget"] == "blue" for s in slots)  # the new recipe variable, from the new code
    assert "factory-widget" in factory["envs"]  # the new env, from the new factory child
    # The successful run reports what the children deployed, including newly introduced envs.
    reported = sorted({env for d in deploys(directed) for env in d["envs"]})
    assert directed.last["envs"] == reported and "factory-widget" in reported
    assert directed.last["sha"] == SHA and directed.failed is None
    assert "widget" in capsys.readouterr().err  # the running deployer says what it did not read
    assert (await directed.tick()).startswith("up to date")
    assert len(deploys(directed)) == 3


async def test_a_new_key_in_the_engine_trees_own_profile_deploys_with_that_engine(world, monkeypatch):
    real_engine(world, monkeypatch, widget=True)
    world.record(OLD_SHA)
    world.ship_profile(world.profile.text + WIDGET)
    await world.tick()
    assert [d["widget"] for d in deploys(world)[:2]] == ["blue", "blue"]
    assert "factory-widget" in world.last["envs"] and world.last["sha"] == SHA


async def test_a_typo_key_the_engine_being_deployed_does_not_know_deploys_nothing(directed, monkeypatch):
    real_engine(directed, monkeypatch, widget=True)
    last = record_pair(directed, OLD_SHA, OLD_DIRECTION_SHA)
    directed.ship_direction_profile(directed.profile.text + WIDGET.replace("widget", "widgte"))
    with pytest.raises(DeployError, match=r"unknown top-level key\(s\): widgte"):
        await directed.tick()
    assert deploys(directed) == [] and directed.last == last
    assert directed.failed["attempts"] == 1 and "widgte" in directed.failed["last_error"]


async def test_an_unknown_key_with_the_engine_unchanged_deploys_nothing(directed, monkeypatch):
    # Only the direction moved: the engine to deploy is the running one, which does not know the key.
    real_engine(directed, monkeypatch)
    last = record_pair(directed, SHA, OLD_DIRECTION_SHA)
    directed.ship_direction_profile(directed.profile.text + WIDGET)
    with pytest.raises(DeployError, match=r"unknown top-level key\(s\): widget"):
        await directed.tick()
    assert deploys(directed) == [] and directed.last == last


async def test_an_env_the_engine_plans_but_does_not_deploy_fails_and_is_not_recorded(directed, monkeypatch):
    real_engine(directed, monkeypatch, widget=True, child=False)
    last = record_pair(directed, OLD_SHA, DIRECTION_SHA)
    directed.ship_direction_profile(directed.profile.text + WIDGET)
    with pytest.raises(DeployError, match="did not deploy factory-widget"):
        await directed.tick()
    assert directed.last == last


async def test_with_an_unknown_key_the_deployers_own_tables_are_still_validated(directed):
    text = directed.profile.text.replace("[direction]\n", "[direction]\nbogus = 1\n") + WIDGET
    directed.ship_direction_profile(text)
    with pytest.raises(DeployError, match=r"unknown key\(s\) in \[direction\]: bogus"):
        await directed.tick()
    assert not directed.deployed and directed.profile_checks == []


# --- running the engine tree's own code --------------------------------------------------------


def stub_tree(tmp_path: Path, child: str) -> Path:
    """A tree whose `_deploy_child` is `child`; returns its src/."""
    package = tmp_path / "stub" / "src" / "flyte_agent_factory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "_deploy_child.py").write_text(child)
    return package.parent


def check(src: Path, text: str = "", base_env: dict[str, str] | None = None) -> list[str]:
    return REAL_CHECK_IN_TREE(src, text, "the profile", tree=None, base_env=base_env or {})


def test_a_tree_without_the_package_is_never_checked_by_the_installed_copy(world, tmp_path, monkeypatch):
    # The installed copy accepts this profile; only the guard stops it vouching for a tree it is not.
    assert _deploy_child.load(world.profile.deployment_text(), "t", None) == world.profile
    # An interpreter with a copy of the package installed: this checkout's `src`, after the tree's own path.
    installed = Path(_deploy_child.__file__).resolve().parents[1]
    python = tmp_path / "python"
    python.write_text(f'#!/bin/sh\nPYTHONPATH="$PYTHONPATH:{installed}" exec {sys.executable} "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(python))
    (tmp_path / "empty").mkdir()
    with pytest.raises(DeployError, match="refuses the profile: imported .*, not the engine tree at"):
        check(tmp_path / "empty", world.profile.deployment_text())


@pytest.mark.parametrize("stdout", ["", "garbage", '{"envs": []}', "[1, 2]", '["ok", ""]'])
def test_a_check_that_prints_no_list_of_names_is_a_deploy_error(tmp_path, stdout):
    src = stub_tree(tmp_path, f"print({stdout!r})\n")
    with pytest.raises(DeployError, match="not a list of environment names"):
        check(src)


def test_a_check_that_crashes_reports_the_end_of_its_stderr(tmp_path):
    lines = [
        "Traceback (most recent call last):",
        "  ProfileError: unknown top-level key(s): widgte",
        "  x",
        "  y",
    ]
    src = stub_tree(tmp_path, f"import sys\nprint({chr(10).join(lines)!r}, file=sys.stderr)\nsys.exit(3)\n")
    with pytest.raises(DeployError, match="widgte"):
        check(src)


def test_a_check_reports_the_whole_message_the_child_wrote(world, monkeypatch):
    real_engine(world, monkeypatch)
    text = world.profile.text + WIDGET + "\n[gadget]\nx = 1\n"
    with pytest.raises(DeployError) as exc:
        _deploy.check_in_tree(world.tree / "src", text, "the profile", tree=world.tree, base_env={})
    assert (
        str(exc.value)
        == "the engine being deployed refuses the profile: unknown top-level key(s): gadget, widget"
    )


def test_the_tree_child_gets_only_the_tree_on_its_path_and_no_profile_selection_or_app_key(tmp_path):
    src = stub_tree(
        tmp_path,
        "import json, os, sys\n"
        "names = ('AGENT_FACTORY_PROFILE', 'AGENT_FACTORY_PROFILE_TOML',\n"
        "         'GITHUB_APP_PRIVATE_KEY', 'PYTHONPATH')\n"
        "print(json.dumps([f'{n}={os.environ.get(n)}' for n in names] + [f'stdin={sys.stdin.read()}']))\n",
    )
    base = {
        "AGENT_FACTORY_PROFILE": "/elsewhere/mine.toml",
        "AGENT_FACTORY_PROFILE_TOML": "name = 'pod'",
        "GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",
        "PYTHONPATH": "/elsewhere/src",
        "PATH": "/usr/bin:/bin",
    }
    assert check(src, "name = 'given'", base) == [
        "AGENT_FACTORY_PROFILE=None",
        "AGENT_FACTORY_PROFILE_TOML=None",
        "GITHUB_APP_PRIVATE_KEY=None",
        f"PYTHONPATH={src}",
        "stdin=name = 'given'",
    ]


def test_a_hung_tree_child_is_killed_with_what_it_started(tmp_path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    src = stub_tree(
        tmp_path,
        "import subprocess, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(subprocess.Popen(['sleep', '60']).pid))\n"
        "time.sleep(60)\n",
    )
    monkeypatch.setattr(_deploy, "CHECK_TIMEOUT", 2)
    with pytest.raises(DeployError, match="check in the engine tree did not finish in"):
        check(src)
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while Path(f"/proc/{pid}").exists() and "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]:
        assert time.monotonic() < deadline, "the deploy child's own child outlived the timeout"
        time.sleep(0.1)


def test_a_deploy_that_reports_nothing_is_a_deploy_error(tmp_path):
    src = stub_tree(tmp_path, "")
    with pytest.raises(DeployError, match=f"deploy of {SHA} reported no environment deployed"):
        _deploy.deploy_in_tree(
            src, "", "p", tree=None, project="p", domain="d", ref=SHA, base_env={"PATH": "/usr/bin:/bin"}
        )


@pytest.mark.parametrize(
    "output",
    [
        "{not json",
        "",
        [1, 2],
        "abc",
        None,
        {},
        {"deployed": "true", "envs": ["factory"]},
        {"deployed": True, "envs": None},
        {"deployed": True, "envs": []},
        OSError("unreadable"),
    ],
)
async def test_an_unreadable_success_output_is_treated_as_unknown(world, output):
    world.add_run("SUCCEEDED", labels=_deployer._pair_labels(world.profile, SHA, ""), output=output)
    result = await world.tick()
    assert result.startswith(f"deployed {SHA}")
    assert world.last["sha"] == SHA


async def test_an_unreadable_newest_output_falls_back_to_the_previous_deploy(world):
    world.record(SHA)
    world.add_run(
        "SUCCEEDED", labels=_deployer._pair_labels(world.profile, OLD_SHA, ""), output=OSError("unreadable")
    )
    assert await world.tick() == f"up to date at {SHA}"
    assert not world.deployed


# --- apps, secrets and failure reasons ---------------------------------------------------------


def test_the_deployer_image_carries_kubernetes_for_the_personal_browsers_pod_template():
    packages = [
        p for layer in cast(flyte.Image, _deployer.env.image)._layers for p in getattr(layer, "packages", ())
    ]
    assert any(p.startswith("kubernetes>=") for p in packages)


def test_a_pair_run_has_a_report():
    assert _deployer.deploy_pair.report is True


def _missing(world, monkeypatch, secrets):
    def check_in_tree(src, text, where, *, tree, base_env, diagnostics=None):
        raise _deploy.MissingSecrets("missing Flyte secrets in p/d: browser_personal_token", secrets)

    monkeypatch.setattr(_deploy, "check_in_tree", check_in_tree)


async def test_missing_secrets_end_the_attempt_without_a_release_or_a_failure(world, monkeypatch):
    needs = {"browser_personal_token": ["claude-browser-personal"]}
    _missing(world, monkeypatch, needs)
    world.record(OLD_SHA)
    await world.tick()
    run = world.runs[-1]
    assert run.phase == "SUCCEEDED"
    assert run.output == {"deployed": False, "waiting_for_secrets": needs}
    assert any("browser_personal_token" in line for line in world.said)
    assert not world.deployed and world.last["sha"] == OLD_SHA  # not a release
    assert (
        await _deployer._failed_attempts(world.profile, _deployer._pair_labels(world.profile, SHA, ""), None)
        == 0
    )
    release = await _deployer.deployed_pair(world.profile)
    assert release is not None and release[1] == (OLD_SHA, "")


async def test_the_next_tick_names_the_secrets_and_still_submits_an_attempt(world, monkeypatch):
    _missing(world, monkeypatch, {"browser_personal_token": ["claude-browser-personal"]})
    await world.tick()
    message = await _deployer.run_once(world.profile, "run-2", world.app, world.clock())
    assert message.startswith(f"submitted {SHA}") and "waiting for secrets: browser_personal_token" in message


def test_the_pair_report_names_the_secrets_it_waits_for():
    waiting = {"browser_personal_token": ["claude-browser-personal"]}
    text = _deployer._describe_outcome({"deployed": False, "waiting_for_secrets": waiting})
    assert "browser_personal_token (needed by claude-browser-personal)" in text
    assert "deployed a, b" in _deployer._describe_outcome({"deployed": True, "envs": ["a", "b"]})


@pytest.mark.parametrize("populated", [False, True])
async def test_standards_diagnostics_survive_real_fetched_engine_deploy(world, monkeypatch, populated):
    real_engine(world, monkeypatch)
    text = world.profile.text
    if populated:
        directory = world.tree / "standards"
        directory.mkdir()
        (directory / "engineering.md").write_text("# captured rules\n")
        (directory / "security.md").write_text("# security rules\n")
        text += '\n[standards]\ndir = "standards"\n'
    world.ship_profile(text)
    await world.tick()
    output = world.runs[-1].output
    assert output["deployed"] is True
    assert output["standards"]["configured"] is populated
    report = _deployer._report_html(_deployer._describe_outcome(output))
    assert ("no standards configured" in report) is (not populated)
    assert ("no standards configured" in json.dumps(output)) is (not populated)


async def test_old_fetched_engine_without_standards_metadata_is_explicit(world, monkeypatch):
    real_engine(world, monkeypatch)
    child = world.tree / "src/flyte_agent_factory/_deploy_child.py"
    source = child.read_text()
    # Releases before standards metadata accept no extra CLI arguments and only print the env list.
    anchor = "            report = os.environ.get(_deploy.STANDARDS_REPORT_ENV)"
    if anchor in source:
        start = source.index(anchor)
        end = source.index("            print(json.dumps(_deploy.planned_envs(profile)))", start)
        child.write_text(source[:start] + source[end:])
    await world.tick()
    assert world.runs[-1].output["standards"]["configured"] is None
    report = _deployer._describe_outcome(world.runs[-1].output)
    assert "standards metadata unavailable" in report
    assert "no standards configured" not in report


@pytest.mark.parametrize("metadata", [None, "not JSON", "[]", '{"configured": "false"}'])
def test_check_in_tree_missing_standards_metadata_preserves_stdout_protocol(tmp_path, monkeypatch, metadata):
    def run_in_tree(src, args, *, text, base_env, timeout, capture):
        if metadata is not None:
            Path(base_env[_deploy.STANDARDS_REPORT_ENV]).write_text(metadata)
        return 0, 'progress\n["factory"]\n', ""

    monkeypatch.setattr(_deploy, "_run_in_tree", run_in_tree)
    diagnostics: dict[str, Any] = {}
    assert _deploy.check_in_tree(
        tmp_path, "profile", "test", tree=None, base_env={}, diagnostics=diagnostics
    ) == ["factory"]
    assert diagnostics["configured"] is None
    assert "no standards configured" not in json.dumps(diagnostics)


def test_deploy_child_metadata_captures_uncaptured_profile_from_its_origin(tmp_path, monkeypatch, capsys):
    path = tmp_path / "engine/profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(WORK.read_text() + '\n[standards]\ndir = "standards"\n')
    profile = _profile.load(str(path))
    assert not profile.standards_payload
    directory = tmp_path / "engine/standards"
    directory.mkdir()
    for name in ("engineering.md", "security.md"):
        (directory / name).write_text("# origin rules\n")
    # Shipped profiles normally carry a bundle; exercise the metadata branch for one without it.
    monkeypatch.setattr(_deploy_child, "load", lambda *_args: profile)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: profile.text))
    monkeypatch.setattr(_deploy_child, "collect_planned_secrets", lambda *_args: {})
    monkeypatch.setattr(_deploy_child, "find_missing_secrets", lambda *_args: {})
    report = tmp_path / "standards.json"
    monkeypatch.setenv(_deploy.STANDARDS_REPORT_ENV, str(report))
    error = tmp_path / "error"
    assert (
        _deploy_child.main(
            ["check", "--where", str(path), "--error", str(error), "--src", str(PACKAGE.parent)]
        )
        == 0
    )
    assert json.loads(report.read_text()) == {"configured": True}
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == _deploy.planned_envs(profile)
    assert not error.exists()


@pytest.mark.parametrize("populated", [True, False])
def test_direction_capture_log_and_bundle_survive_fetched_tree(tmp_path, monkeypatch, capsys, populated):
    profile = make_direction_profile(f"file://{tmp_path}/state")
    assert profile.direction is not None
    origin = tmp_path / "direction"
    path = origin / profile.direction.profile
    path.parent.mkdir(parents=True)
    path.write_text(profile.text)
    if populated:
        (origin / "standards").mkdir()
        (origin / "standards/engineering.md").write_text("private direction rules")
        (origin / "standards/security.md").write_text("private security rules")
    git("init", "-q", "-b", "main", cwd=origin)
    git("add", ".", cwd=origin)
    git("commit", "-qm", "direction", cwd=origin)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()
    monkeypatch.setattr(_github, "clone_url", lambda *_args: str(origin))
    fetched = []
    real_fetched_tree = _deploy.fetched_tree

    @contextlib.contextmanager
    def fetched_tree(url, ref):
        with real_fetched_tree(url, ref) as tree:
            fetched.append(tree)
            yield tree

    monkeypatch.setattr(_deploy, "fetched_tree", fetched_tree)
    app = cast(_github.GithubApp, SimpleNamespace(installation_token=lambda _repo: "unused-token"))
    shipped = _deployer._direction_profile(profile, app, sha)
    assert fetched and not fetched[0].exists()
    deployed = _profile.parse(shipped.deployment_text(), _profile.TOML_ENV)
    files = _standards.decode(deployed.standards_payload)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if "direction standards captured" in line]
    if populated:
        assert files == {
            "engineering.md": "private direction rules",
            "security.md": "private security rules",
        }
        assert len(lines) == 1
        assert "acme/direction" in lines[0] and profile.direction.profile in lines[0]
        assert sha in lines[0] and "2 files" in lines[0]
    else:
        assert files == {}
        assert not lines
        assert "no standards configured" in captured.err
    assert "private direction rules" not in captured.out
    assert "private security rules" not in captured.out
    assert "unused-token" not in captured.out
