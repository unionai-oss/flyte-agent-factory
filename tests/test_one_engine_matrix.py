"""Single-credential roles through real routing, processes, git and bridge files.

Only cloud boundaries (Flyte dispatch, directory storage, reports, GitHub and browser
HTTP) are faked. Claude's author uses the interactive protocol; Codex uses exec/resume.
Factory MCP is an engine-independent Flyte app, using stock launch/status/direction tools.
PR publication also runs in the supervisor and consumes no model credential.
The step 1 benchmark deliberately accepts Claude arms only (_bench.plan_cases); the
Codex cell asserts rejection before launch rather than bypassing that validation.
Scoped caps are advisory until the CLI refuses the credential. With one credential,
exhaustion waits for its reset; handoff cannot choose an absent engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flyte
import pytest

from flyte_agent_factory import (
    _author,
    _bridge,
    _checkpoint,
    _contract,
    _demo_checks,
    _factory,
    _factory_base,
    _inbox,
    _local_browser,
    _meter,
    _review,
    _reviewer,
    _router,
    _session_config,
    _session_setup,
    _state,
    _supervise_support,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations

FAKES = Path(__file__).parent
pytestmark = [pytest.mark.real_planner, pytest.mark.real_contract]


def meters() -> _meter.MeterStore:
    """The store the `single_engine` fixture installed on the agent module."""
    assert _usage._meters is not None
    return _usage._meters


def account(engine):
    """The meter account of `engine`'s credential: the work token's for Claude, else the engine's own."""
    return _session_config._claude_account() if engine == "claude" else engine


def git(work, *args):
    return subprocess.check_output(["git", "-C", str(work), *args], stderr=subprocess.PIPE).decode().strip()


@pytest.fixture(params=["claude", "codex"])
def single_engine(request, tmp_path, monkeypatch):
    engine = request.param
    for key in list(os.environ):
        if key.startswith(("AGENT_FACTORY_", "AGENT_FACTORY_", "FAKE_")) and key != "AGENT_FACTORY_PROFILE":
            monkeypatch.delenv(key)
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", _session_config.CODEX_TOKEN_ENV_VAR):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "matrix-codex" if engine == "codex" else "")
    if engine == "claude":
        monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "matrix-claude")
    assert [s.key for s in agent._credentials_secret()] == [f"matrix-{engine}"]
    if engine == "claude":
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "matrix-token")
    else:
        monkeypatch.setenv(
            _session_config.CODEX_TOKEN_ENV_VAR, '{"tokens": {"access_token": "matrix-token"}}'
        )
    for attr, name in (
        ("CONFIG_DIR", "state/claude"),
        ("STATE_ROOT", "state"),
        ("WORKSPACE", "state/workspace"),
        ("BIN_DIR", "bin"),
        ("CODEX_HOME_DIR", "state/codex"),
        ("BRIDGE_DIR", "bridge"),
        ("BASE_DIR", "base"),
    ):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        # BRIDGE_DIR is `_bridge`'s, imported into `agent`; the rest live in `_session_config`.
        monkeypatch.setattr(agent if attr == "BRIDGE_DIR" else _session_config, attr, path)
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "_project_name", "matrix")
    monkeypatch.setattr(_session_config, "_rulings", [])
    for module, attr, value in (
        (_usage, "_hard_pins", False),
        (_usage, "_author_engine", engine),
        (_usage, "_meters", _meter.MeterStore(tmp_path / "meters")),
        (_review, "_prior_findings", {}),
        (_reviewer, "_review_ledger", []),
        (_contract, "_contract", ""),
    ):
        monkeypatch.setattr(module, attr, value)
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setenv("AGENT_FACTORY_CRAFT_BLOCKING", "true")
    log = tmp_path / "engines.jsonl"
    monkeypatch.setenv("MATRIX_LOG", str(log))
    for name in ("claude", "codex"):
        path = _session_config.BIN_DIR / name
        # `codex login --with-access-token` is not an engine turn: it is neither logged nor given the
        # turn's edit commands, which would run in the caller's directory.
        login = (
            "if sys.argv[1:2] == ['login']:\n    from fake_codex import main as login\n"
            "    raise SystemExit(login(sys.argv[1:]))\n"
            if name == "codex"
            else ""
        )
        path.write_text(
            f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(FAKES)!r})\n{login}"
            f"from fake_matrix_engine import main\nraise SystemExit(main({name!r}))\n"
        )
        path.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    for key, value in {
        "CLAUDE_CONFIG_DIR": str(_session_config.CONFIG_DIR),
        "AGENT_FACTORY_WORKSPACE": str(_session_config.WORKSPACE),
        "AGENT_FACTORY_BRIDGE_DIR": str(tmp_path / "bridge"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", f"{_session_config.BIN_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("CODEX_HOME", str(_session_config.CODEX_HOME_DIR))
    _session_setup._install_bridge_commands(_session_config.BIN_DIR)
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", _session_config.STATE_ROOT / "codex-thread")
    monkeypatch.setattr(_author, "_CODEX_COMMAND", (str(_session_config.BIN_DIR / "codex"),))
    work = _session_config.WORKSPACE
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.com")
    (work / "tracked").write_text("before\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")

    class LocalDir:
        @classmethod
        async def from_local(cls, path):
            target = Path(tempfile.mkdtemp(dir=tmp_path)) / "files"
            shutil.copytree(path, target)
            return SimpleNamespace(download=lambda path: download(target, path))

    async def download(source, path):
        shutil.copytree(source, path, dirs_exist_ok=True)

    class LocalTask:
        async def aio(self, **kwargs):
            return await agent.subagent.func(**kwargs)

    async def report(*args, **kwargs):
        pass

    import flyte.report

    monkeypatch.setattr(agent, "Dir", LocalDir)
    monkeypatch.setattr(_checkpoint, "Dir", LocalDir)
    monkeypatch.setattr(agent.subagent, "override", lambda **kwargs: LocalTask())
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(flyte.report.replace, "aio", report)
    meters().mark_attempt()
    assert _usage._author_engines() == (engine,)
    rig = MatrixProject(engine, work, log, tmp_path, monkeypatch)
    yield rig
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert all(call["engine"] == engine for call in calls), calls
    assert all(r["engine"] in (None, engine) for r in _session_config._routes)


def script(monkeypatch, answer="finished", commands=()):
    monkeypatch.setenv("MATRIX_ANSWER", answer)
    monkeypatch.setenv("MATRIX_COMMANDS", json.dumps(list(commands)))


class MatrixProject:
    """Project supervisor with local storage and Flyte condition/report edges.

    Like test_codex_author.project, this keeps startup and credentials real. The
    pump and inbox also stay real so the same harness covers recovery and MCP.
    """

    def __init__(self, engine, work, log, tmp_path, monkeypatch):
        import flyte.io
        import flyte.report
        from flyteidl2.workflow import run_definition_pb2  # type: ignore[import-untyped]  # no py.typed

        self.engine, self.work, self.log = engine, work, log
        timeline_type = _timeline._Timeline
        self.timeline = timeline_type()
        self.conditions: dict[str, Any] = {}
        self.report = ""
        self.run: Any = None
        self.task: Any = None  # set by `running`
        checkpoint = tmp_path / "checkpoint"

        # test_state's fake records paths; a retry here needs a copy independent of the live tree.
        class Checkpoint:
            remote_destination = str(checkpoint)  # as `flyte.Checkpoint`; the run notes it for the factory

            def prev_exists(self):
                return checkpoint.exists()

            async def save(self, source):
                shutil.rmtree(checkpoint, ignore_errors=True)
                shutil.copytree(source, checkpoint)

            async def load(self):
                target = Path(tempfile.mkdtemp(dir=tmp_path)) / "restored"
                shutil.copytree(checkpoint, target)
                return target

        async def noop(*args, **kwargs):
            pass

        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        async def report(body, **kwargs):
            self.report = body
            if self.run is not None:
                self.run.report = body

        async def new_condition(name, **kwargs):
            future = asyncio.get_running_loop().create_future()

            async def signal(value):
                future.set_result(value)

            async def wait():
                return await future

            condition = SimpleNamespace(
                name=name,
                future=future,
                pb2=run_definition_pb2.Action(),
                wait=SimpleNamespace(aio=wait),
                signal=SimpleNamespace(aio=signal),
            )
            self.conditions[name] = condition
            return condition

        monkeypatch.setattr(_state, "_checkpoint", lambda: Checkpoint())
        # the LocalDir the fixture installed on `agent`; the module does not export `Dir` for typing
        monkeypatch.setattr(flyte.io.Dir, "from_local", vars(agent)["Dir"].from_local)
        monkeypatch.setattr(flyte, "ctx", lambda: None)
        monkeypatch.setattr(flyte.new_condition, "aio", new_condition)
        monkeypatch.setattr(flyte.report.replace, "aio", report)

        def timeline():
            self.timeline = timeline_type()
            return self.timeline

        monkeypatch.setattr(_timeline, "_Timeline", timeline)
        monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.02)
        monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
        monkeypatch.setattr(_usage, "_read_meters", noop)
        for name in ("_refresh_child_usage", "_refresh_queue"):
            monkeypatch.setattr(_supervise_support, name, noop)
        monkeypatch.setattr(_checkpoint, "_hand_over_slot", noop)
        monkeypatch.setattr(_supervise_support, "_board_loop", forever)
        monkeypatch.setattr(_usage, "_meter_loop", forever)
        for name in ("_watch_app_messages",):
            monkeypatch.setattr(_inbox, name, forever)
        monkeypatch.setattr(_checkpoint, "_checkpoint_loop", forever)
        monkeypatch.setattr(agent, "write_context", lambda *args: work / "context")

    async def until(self, check):
        async with asyncio.timeout(30):
            while not check():
                if self.task.done():
                    self.task.result()
                    raise AssertionError("project ended before the expected event")
                await asyncio.sleep(0.02)

    def received(self):
        if self.engine == "claude":
            path = Path(os.environ["FAKE_CLAUDE_LOG"])
            return path.read_text() if path.exists() else ""
        return "\n".join(p.read_text() for p in (_session_config.CODEX_HOME_DIR / "threads").glob("*"))

    @contextlib.asynccontextmanager
    async def running(self, **inputs):
        self.conditions.clear()
        self.task = asyncio.create_task(
            agent.project.func(**{"brief": "implement", "browser": False, **inputs})
        )
        try:
            yield self
        finally:
            if not self.task.done():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def send(self, message):
        await self.until(lambda: any(not c.future.done() for c in self.conditions.values()))
        condition = next(c for c in self.conditions.values() if not c.future.done())
        await condition.signal.aio(message)

    async def stop(self):
        await self.send("/stop")
        await asyncio.wait_for(self.task, 30)


def author_script(tmp_path, monkeypatch, commands):
    script(monkeypatch, commands=commands)
    steps = tmp_path / "steps.json"
    steps.write_text(json.dumps([{"when": "Read .*BRIEF.md", "run": list(commands)}]))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(steps))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "received"))


async def author(rig, tmp_path, monkeypatch, commands, *, browser=False):
    author_script(tmp_path, monkeypatch, commands)
    async with rig.running(browser=browser):
        await rig.until(lambda: (rig.work / "edited").exists())
        assert _usage._author_engine == rig.engine
        if rig.engine == "codex":
            assert (_session_config.CODEX_HOME_DIR / "auth.json").is_file()
        await rig.stop()


async def test_author_edit(single_engine, tmp_path, monkeypatch):
    rig = single_engine
    await author(rig, tmp_path, monkeypatch, ["echo authored > edited"])
    assert (rig.work / "edited").read_text() == "authored\n"


async def test_author_start_failure_reaches_the_cell(single_engine, tmp_path, monkeypatch):
    def codex(*args, **kwargs):
        raise RuntimeError("author startup interrupted")

    async def claude(*args, **kwargs):
        raise RuntimeError("author startup interrupted")

    if single_engine.engine == "codex":
        monkeypatch.setattr(_author, "_start_codex_author", codex)
    else:
        monkeypatch.setattr(_author, "_start_session", claude)
    with pytest.raises(RuntimeError, match="author startup interrupted"):
        await author(single_engine, tmp_path, monkeypatch, ["touch edited"])


async def test_review_roles_and_repair(single_engine, tmp_path, monkeypatch):
    rig = single_engine
    timeline = rig.timeline
    snapshot = await _checkpoint._upload_workspace(rig.work)
    script(monkeypatch, "C1. The tracked file is repaired.")
    assert await _contract._freeze_contract(snapshot, "repair", "repair", timeline) == (
        "C1. The tracked file is repaired."
    )
    script(
        monkeypatch,
        "C1. The tracked file is repaired.\n"
        "FINDING: repair is missing\nEvidence: missing: edited\n"
        "BLOCKING: 1\nVERDICT: FAIL\nCONVERGING: YES",
    )
    passed, findings, converging, _ = await _review._review_round(rig.work, "repair", "repair", 1, timeline)
    assert not passed and converging is True
    assert "repair is missing" in findings
    assert all(name in findings for name, _ in _review._JUDGES)
    await author(rig, tmp_path, monkeypatch, ["echo repaired > tracked", "touch edited"])
    assert (rig.work / "tracked").read_text() == "repaired\n"
    script(monkeypatch, "VERDICT: PASS")
    passed, _, _, _ = await _review._review_round(rig.work, "repair", "repair", 2, timeline)
    assert passed
    assert [v["verdict"] for v in timeline.verdicts] == ["FAIL", "PASS"]
    snapshot = await _checkpoint._upload_workspace(rig.work)
    script(monkeypatch, "RULING [1]: KEEP -- the brief requires it")
    rulings, unresolved = await _review._arbitrate(snapshot, "repair", "repair", ["conflict"], "", timeline)
    assert rulings == {1: "KEEP -- the brief requires it"} and unresolved == {}
    assert {r["role"] for r in _session_config._routes} >= {
        "contract",
        "correctness",
        "completeness",
        "craft",
        "planner",
        "arbiter",
    }


@pytest.mark.parametrize("answer_only", [False, True], ids=["patch", "research"])
async def test_delegation(single_engine, bridge_root, monkeypatch, capsys, answer_only):
    rig = single_engine
    script(
        monkeypatch,
        "research answer" if answer_only else "patch ready",
        []
        if answer_only
        else [
            "tar xzf repo.tar.gz",
            "echo delegated > repo/tracked",
            "git -C repo diff --binary > factory.patch",
        ],
    )
    supervisor = Delegations(
        bridge_root,
        rig.work,
        _session_setup._delegate_worker(rig.timeline),
        _usage._delegation_engine(rig.timeline),
        rig.timeline.row,
    )
    assert _bridge.delegate_cli((["--answer"] if answer_only else []) + ["do the work"]) == 0
    job = capsys.readouterr().out.strip()
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert _bridge.delegate_cli(["wait", job, "--timeout", "0"]) == 0
    output = capsys.readouterr().out
    if answer_only:
        assert "research answer" in output
    else:
        patch = rig.work / ".claude-flyte/delegations" / job / "factory.patch"
        assert "delegated" in patch.read_text()
        git(rig.work, "apply", str(patch))
        assert (rig.work / "tracked").read_text() == "delegated\n"


# Owner rule 2: only a refusal ("limited") defers the one engine; a full window ("100", "stale-full") is
# a hint and the call is made. This deferred on "100" and "stale-full", and had no "limited" case.
@pytest.mark.parametrize("case", ["99", "100", "scoped", "stale", "stale-full", "limited"])
async def test_capacity_and_reset(single_engine, monkeypatch, case):
    rig = single_engine
    now = time.time()
    full = case in ("100", "stale-full", "limited")
    meter = _meter.Meter(
        account(rig.engine),
        "fake-usage",
        now - (2 * _router.STALE_AFTER_S if case.startswith("stale") else 0),
        100 if full else 99,
        now + 3600,
        scoped=[_meter.Scoped("model", 100, now + 3600)] if case == "scoped" else [],
        limited_until=now + 3600 if case == "limited" else None,
    )
    meters().update(meter)
    script(monkeypatch, "capacity answer")
    if case == "limited":
        before = rig.log.read_text() if rig.log.exists() else ""
        with pytest.raises(_router.RouterDeferred):
            await _reviewer._call_reviewer(purpose="correctness", brief="check", answer_only=True)
        decision = _session_config._routes[-1]
        assert decision["deferred"] and decision["retry_at"] == now + 3600
        assert (rig.log.read_text() if rig.log.exists() else "") == before
        # Move the refusal and the window past their reset without sleeping for an hour.
        meter.limited_until = meter.five_hour_resets_at = now - 1
    answer, _ = await _reviewer._call_reviewer(purpose="correctness", brief="check", answer_only=True)
    assert answer == "capacity answer"
    if case == "stale":
        own = next(c for c in _session_config._routes[-1]["candidates"] if c["engine"] == rig.engine)
        assert own["state"] == "unknown"


async def test_refusal_wait_crash_recovers_on_the_same_engine(single_engine, tmp_path, monkeypatch):
    import signal

    rig = single_engine
    author_script(tmp_path, monkeypatch, [])
    monkeypatch.setenv("MATRIX_AUTHOR_LIMIT", "1")
    async with rig.running():
        await rig.until(lambda: rig.timeline._phase.get("kind") == "limited")
        await rig.until(lambda: "Read .claude-flyte/BRIEF.md" in rig.received())
        proc = rig.timeline.proc
        assert not hasattr(proc, "on_limit"), "single-engine startup must not install handoff"
        reset_at = meters().latest[rig.engine].limited_until
        assert reset_at is not None
        assert reset_at > time.time()
        before = rig.received()
        await asyncio.sleep(0.15)
        assert rig.received() == before, "no retries before the reset"
        assert not list((_session_config.STATE_ROOT / "author-handoffs").glob("*.json"))
        if rig.engine == "claude":
            os.killpg(proc._proc.pid, signal.SIGKILL)
        else:
            # Codex has no live process between turns; a later turn can die while paused.
            monkeypatch.delenv("MATRIX_AUTHOR_LIMIT")
            proc._env["FAKE_CODEX_CRASH"] = "1"
            await rig.send("direction interrupted by crash")
        with pytest.raises(RuntimeError, match="retrying from the last checkpoint"):
            await asyncio.wait_for(rig.task, 30)
    # The reset arrives while Flyte schedules the retry.
    wall_time = time.time
    offset = reset_at + 61 - wall_time()
    monkeypatch.setattr(time, "time", lambda: wall_time() + offset)
    monkeypatch.delenv("MATRIX_AUTHOR_LIMIT", raising=False)
    author_script(tmp_path, monkeypatch, ["echo recovered > edited"])
    async with rig.running():
        # `>` creates the file before echo writes it: wait for the content, not the name.
        await rig.until(
            lambda: (rig.work / "edited").is_file() and (rig.work / "edited").read_text() == "recovered\n"
        )
        assert _usage._author_engine == rig.engine
        if rig.engine == "codex":
            assert rig.timeline.proc.thread_id == proc.thread_id
        assert any("resumed from checkpoint" in text for _, _, text in rig.timeline._rows)
        await rig.stop()
    assert (rig.work / "edited").read_text() == "recovered\n"


async def test_pull_request(single_engine, monkeypatch):
    import httpx

    from flyte_agent_factory import _pull_requests
    from tests.test_pull_requests import (
        BRIEF,
        PRIVATE_PEM,
        REPO,
        RUN_URL,
        FakeGithub,
        RealGithubApp,
    )

    rig = single_engine
    fake = FakeGithub()
    with httpx.Client(
        transport=httpx.MockTransport(fake.handler), base_url="https://api.github.com"
    ) as client:
        app = RealGithubApp("12345", PRIVATE_PEM, client=client)
        prs = _pull_requests.PullRequests(app, REPO, "matrix", RUN_URL, rig.timeline.row)
        monkeypatch.setattr(_session_setup, "_pr", prs)
        await _supervise_support._publish_pull_request(rig.work, BRIEF, rig.timeline)
        assert fake.sent("POST", "/pulls")[0]["head"] == "factory/matrix"
        assert rig.timeline.pr_url.endswith("/pull/5")
        await _supervise_support._post_verdict(rig.work, rig.timeline, git(rig.work, "rev-parse", "HEAD"), 2)
        assert fake.sent("POST", "/graphql")


def test_benchmark_arm(single_engine, monkeypatch, tmp_path):
    from flyte_agent_factory import _bench
    from tests.test_bench_flow import SHA, World, snapshot, started

    rig = single_engine
    world = World(monkeypatch)
    if rig.engine == "codex":
        with pytest.raises(ValueError, match="step 1 benchmark arms are Claude-only"):
            _bench.plan_cases("r1", SHA, [], 1, {"AUTHOR_ENGINE": rig.engine}, "opus")
        assert world.fake.created == []
        assert not rig.log.exists()
        return
    case = _bench.plan_cases("r1", SHA, [], 1, {"AUTHOR_ENGINE": rig.engine}, "opus")[0]
    case["env"] = "bench-0123456-1"

    def run_author():
        created = world.fake.created[-1]
        for key, value in created["context"]["env_vars"].items():
            monkeypatch.setenv(key, value)
        asyncio.run(author(rig, tmp_path, monkeypatch, ["echo benchmark > edited"]))

    world.fake.after_create = run_author
    started(world, case, snapshot("review"), snapshot("done", scores=[2]))
    _, _, harness = world.drive(case)
    assert harness["outcome"] == "accepted" and harness["review_passed"]
    assert (rig.work / "edited").read_text() == "benchmark\n"
    (created,) = world.fake.created
    assert created["context"]["env_vars"]["AGENT_FACTORY_COORDINATOR_ENGINE"] == rig.engine


async def _no_voice_model(*args):
    return None


async def test_browser_tool_for_author(single_engine, tmp_path, monkeypatch):
    from tests.test_codex_browser import FakeMcp

    rig = single_engine
    server = FakeMcp()
    try:

        class Browser:
            client_headers = {"Origin": "http://localhost:8931"}

            def __init__(self, **kwargs):
                pass

            async def start(self):
                return server.endpoint

            async def stop(self):
                pass

        monkeypatch.setattr(_local_browser, "available", lambda: True)
        monkeypatch.setattr(_local_browser, "LocalBrowser", Browser)
        monkeypatch.setattr(_demo_checks, "_fetch_voice_model", _no_voice_model)
        monkeypatch.setenv("FAKE_CODEX_MCP", "1")
        monkeypatch.setenv("MATRIX_BROWSER", "1")
        await author(rig, tmp_path, monkeypatch, ["touch edited"], browser=True)
        assert [r["method"] for r in server.requests] == ["initialize", "tools/list", "tools/call"]
        assert all(r["headers"]["Origin"] == "http://localhost:8931" for r in server.requests)
    finally:
        server.close()


async def test_factory_mcp_launch_status_direction(single_engine, queue_state, monkeypatch, tmp_path):
    import dataclasses

    from flyte._initialize import _InitConfig, init_config_context
    from flyte.ai.mcp._tools import build_tool_functions

    from flyte_agent_factory import _factory_mcp, _profile
    from tests.fake_flyte import FakeFlyte
    from tests.test_codex_author import _profile_text

    rig = single_engine
    profile = _profile.parse(_profile_text('codex_token = "matrix-codex"\n' if rig.engine == "codex" else ""))
    profile = dataclasses.replace(profile, state=_factory_base.PROFILE.state)
    (slot,) = profile.slots
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    world = FakeFlyte(monkeypatch, _factory, fake_headroom=False)
    tools = build_tool_functions(_factory_mcp.app)
    outputs = {}
    author_script(tmp_path, monkeypatch, ["echo launched > edited"])
    loop = asyncio.get_running_loop()

    def execute_project():
        created = world.created[-1]
        assert created["task"] == slot.task
        rig.run = world.runs[created["name"]]

        def start():
            env = {**profile.deploy_env(slot), **created["context"].get("env_vars", {})}
            if rig.engine == "codex":
                # The single-engine pod holds no Claude token; the profile's deploy env names one.
                env.pop("AGENT_FACTORY_CLAUDE_TOKEN_SECRET")
            for key, value in env.items():
                monkeypatch.setenv(key, value)
            rig.task = asyncio.create_task(agent.project.func(**created["inputs"]))

        loop.call_soon_threadsafe(start)

    world.after_create = execute_project

    async def run_task(task, **inputs):
        name = str(task)
        target: Any = {"factory.launch": _factory.launch, "factory.status": _factory.status}[name]
        outputs[name] = json.loads(await target.func(**inputs))
        return SimpleNamespace(name=f"tool-{len(outputs)}", url="https://console.example/tool")

    async def get_condition(name, **kwargs):
        assert kwargs["run_name"] == rig.run.name
        return rig.conditions[name]

    monkeypatch.setattr(flyte.run, "aio", run_task)
    monkeypatch.setattr(flyte.remote.Condition, "get", SimpleNamespace(aio=get_condition))
    # The launched task asks for a browser by default; the fake image has none.
    monkeypatch.setattr(_local_browser, "available", lambda: False)

    def no_browser():
        raise RuntimeError("no browser app in the fake image")

    monkeypatch.setattr(_session_setup, "browser_endpoint", no_browser)
    config = _InitConfig(root_dir=tmp_path, project=_factory.PROJECT, domain=_factory.DOMAIN)
    try:
        with init_config_context(config):
            launched = await tools["run_task"](
                _factory.DOMAIN,
                _factory.PROJECT,
                "factory.launch",
                {"brief": "build", "name": "matrix-project"},
            )
            assert launched["name"] == "tool-1"
            result = outputs["factory.launch"]
            assert result["status"] == "started"
            assert rig.run is world.runs[result["run"]]
            await rig.until(lambda: (rig.work / "edited").exists() and bool(rig.conditions))
            assert _usage._author_engines() == (rig.engine,)
            assert _usage._author_engine == rig.engine
            await tools["run_task"](
                _factory.DOMAIN,
                _factory.PROJECT,
                "factory.status",
                {"name": "matrix-project"},
            )
            status = outputs["factory.status"]
            assert status["name"] == "matrix-project"
            assert status["phase"] == "building"
            condition = next(iter(rig.conditions.values()))
            sent = await tools["signal_condition"](condition.name, result["run"], "repair this finding")
            assert sent["signaled"]
            await rig.until(lambda: "New direction arrived from Flyte" in rig.received())
            assert (rig.work / ".claude-flyte/inbox/001.md").read_text() == "repair this finding\n"
            assert "Union: repair this finding" in _inbox._directions
            await rig.stop()
    finally:
        if rig.task is not None:
            if not rig.task.done():
                rig.task.cancel()
            await asyncio.gather(rig.task, return_exceptions=True)


@pytest.mark.parametrize("scoped", [False, True], ids=["credential", "scoped-cap"])
async def test_cli_refusal_waits_then_resumes_at_reset(single_engine, monkeypatch, scoped):
    rig = single_engine
    now = time.time()
    meters().update(
        _meter.Meter(
            account(rig.engine),
            "fake-usage",
            now,
            20,
            now + 3600,
            scoped=[_meter.Scoped("model", 100, now + 3600)] if scoped else [],
        )
    )
    script(monkeypatch, "resumed answer")
    monkeypatch.setenv("MATRIX_LIMIT", "1")
    with pytest.raises(_router.RouterDeferred):
        await _reviewer._call_reviewer(purpose="planner", brief="plan", answer_only=True)
    assert len(rig.log.read_text().splitlines()) == 1
    meter = meters().latest[rig.engine]
    assert meter.limited_until is not None and meter.limited_until > now
    with pytest.raises(_router.RouterDeferred):
        await _reviewer._call_reviewer(purpose="planner", brief="plan", answer_only=True)
    assert len(rig.log.read_text().splitlines()) == 1, "waiting must not retry a CLI or use an absent engine"
    monkeypatch.delenv("MATRIX_LIMIT")
    meter.limited_until = now - 1
    meter.five_hour_resets_at = now - 1
    answer, _ = await _reviewer._call_reviewer(purpose="planner", brief="plan", answer_only=True)
    assert answer == "resumed answer"
    assert len(rig.log.read_text().splitlines()) == 2
