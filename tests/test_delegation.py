import asyncio
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import flyte
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _bridge,
    _checkpoint,
    _instructions,
    _reviewer,
    _session_config,
    _session_setup,
    _subagent_run,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations, snapshot
from tests._wait import wait_for

#: For the tests that never reach the worker, engine or emit callback of a supervisor.
UNUSED: Any = None


@pytest.mark.parametrize("exhausted_during_upload", [False, True])
async def test_production_delegation_routes_on_the_stored_meter_and_a_mid_run_refusal(
    repo, bridge_root, tmp_path, monkeypatch, exhausted_during_upload
):
    import time
    from pathlib import Path

    from flyte_agent_factory import _meter

    store = _meter.MeterStore(tmp_path / "meters")
    now = time.time()
    calls = []
    store.latest["codex"] = _meter.Meter("codex", "app-server", now, 0, now + 3600)

    async def upload(path):
        if exhausted_during_upload:
            store.note_limit("codex", now + 3600)
        return path

    class Output:
        async def download(self, path):
            Path(path).mkdir()
            (Path(path) / "factory.patch").write_text("")

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        return "done", Output()

    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "{}")
    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(Dir, "from_local", upload)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    timeline = _timeline._Timeline()
    supervisor = Delegations(
        bridge_root,
        repo,
        _session_setup._delegate_worker(timeline),
        _usage._delegation_engine(timeline),
        timeline.row,
    )
    await supervisor.service({"id": "delegate-fresh", "brief": "implement"})
    answer = json.loads((bridge_root / "answers/delegate-fresh.json").read_text())
    assert answer["state"] == "done", answer
    # Codex has no usage probe: only its stored reading (room left) and a refusal seen mid-run route.
    assert calls == (["claude"] if exhausted_during_upload else ["codex"])
    assert [record["engine"] for record in _session_config._routes] == (
        ["codex", "claude"] if exhausted_during_upload else ["codex"]
    )
    assert all(record["reason"] for record in _session_config._routes)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "tracked").write_text("before")
    (root / ".gitignore").write_text("ignored\n.claude-flyte/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def test_snapshot(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    (repo / "tracked").write_text("after")
    (repo / "new").write_text("new")
    (repo / "ignored").write_text("secret")
    target = tmp_path / "snapshot"
    target.mkdir()
    base = snapshot(repo, target)
    assert base != head
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == "M tracked\n?? new"
    with tarfile.open(target / "repo.tar.gz") as archive:
        assert "repo/ignored" not in archive.getnames()
        archive.extractall(tmp_path / "unpacked", filter="data")
    unpacked = tmp_path / "unpacked/repo"
    assert git(unpacked, "rev-parse", "HEAD") == base
    assert git(unpacked, "status", "--porcelain") == ""
    assert (unpacked / "tracked").read_text() == "after"
    assert (unpacked / "new").read_text() == "new"
    (unpacked / "tracked").write_text("worker change")
    patch = subprocess.check_output(["git", "-C", str(unpacked), "diff", base])
    subprocess.run(["git", "-C", str(repo), "apply", "--check"], input=patch, check=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Author checkpoints local work before three-way apply")
    subprocess.run(["git", "-C", str(repo), "apply", "--3way", "--check"], input=patch, check=True)


@pytest.mark.asyncio
async def test_round_trip_and_failure(repo, bridge_root, capsys):
    async def worker(target, brief, engine, *, answer_only=False, on_engine=None):
        assert "tar xzf repo.tar.gz" in brief
        assert engine == "codex"
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("diff --git a/new b/new\n")
        return "changed new"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    assert _bridge.delegate_cli(["implement it"]) == 0
    job_id = capsys.readouterr().out.strip()
    assert _bridge.delegate_cli(["status"]) == 0
    assert f"{job_id} queued" in capsys.readouterr().out
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    output = capsys.readouterr().out
    assert "changed new" in output
    assert str(repo / ".claude-flyte/delegations" / job_id / "factory.patch") in output
    assert _bridge.delegate_cli(["status"]) == 0
    assert f"{job_id} done" in capsys.readouterr().out

    async def broken(*args, **kwargs):
        raise RuntimeError("worker unavailable")

    supervisor.worker = broken
    _bridge.delegate_cli(["another task"])
    failed = capsys.readouterr().out.strip()
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert _bridge.delegate_cli(["wait", failed]) == 1
    assert "worker unavailable" in capsys.readouterr().err
    assert "worker unavailable" in (supervisor.jobs / failed / "error.txt").read_text()


@pytest.mark.asyncio
async def test_cap(repo, bridge_root):
    release = asyncio.Event()
    active = 0
    maximum = 0

    async def worker(target, *args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await release.wait()
        active -= 1
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("")
        return "done"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *args: None, 2)
    tasks = [
        asyncio.create_task(supervisor.service({"id": f"delegate-{i}", "brief": "fix"})) for i in range(4)
    ]
    await wait_for(lambda: active == 2)
    assert active == 2
    release.set()
    await asyncio.gather(*tasks)
    assert maximum == 2


def test_restart(repo, bridge_root):
    _bridge._write_atomic(
        bridge_root / "requests/delegate-stale.json", {"id": "delegate-stale", "brief": "fix"}
    )
    _bridge._write_atomic(bridge_root / "answers/delegate-stale.json", {"state": "running"})
    supervisor = Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert (supervisor.jobs / "delegate-stale/error.txt").read_text() == "supervisor restarted"
    assert _bridge.delegate_cli(["wait", "delegate-stale"]) == 1
    assert json.loads((bridge_root / "answers/delegate-stale.json").read_text())["state"] == "failed"


def test_engine_and_instructions(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_DELEGATE_ENGINE", raising=False)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "configured")
    # Delegates stay on the builder side, Claude, unless policy pins them (owner, inbox 038 rule 4);
    # this used to expect codex. A pin to codex still holds while Codex is configured.
    assert _usage._engine_for("delegate") == "claude"
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    assert _usage._engine_for("delegate") == "codex"
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "claude")
    assert _usage._engine_for("delegate") == "claude"
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    assert _usage._engine_for("delegate") == "claude"
    assert "Implementation coordinator" in _instructions._project_memory(False, delegation_available=True)
    assert "Your own subagents first" not in _instructions._project_memory(False, delegation_available=True)
    assert "Implementation coordinator" not in _instructions._project_memory(False)


def test_file_submission_and_harness_ignore(repo, bridge_root, tmp_path, capsys):
    brief = tmp_path / "brief.md"
    brief.write_text("standalone task")
    assert _bridge.delegate_cli(["--file", str(brief)]) == 0
    job_id = capsys.readouterr().out.strip()
    assert json.loads((bridge_root / "requests" / f"{job_id}.json").read_text())["brief"] == "standalone task"
    _session_setup._exclude_harness(repo)
    output = repo / ".claude-flyte/delegations/job/factory.patch"
    output.parent.mkdir(parents=True)
    output.write_text("private worker output")
    assert git(repo, "check-ignore", str(output)) == str(output)


def test_wrapper_only_when_supervised(tmp_path):
    _session_setup._install_bridge_commands(tmp_path)
    assert not (tmp_path / "delegate").exists()
    _session_setup._install_bridge_commands(tmp_path, delegation_available=True)
    wrapper = tmp_path / "delegate"
    assert wrapper.stat().st_mode & 0o111
    assert "delegate" in wrapper.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit, expected", [(False, "haiku (low)"), (True, "chosen (high)")])
async def test_author_uses_deploy_policy(monkeypatch, explicit, expected):
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_MODEL", "haiku")
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_EFFORT", "low")
    headers = {}

    class HeaderRecorded(Exception):
        pass

    class Timeline:
        def header(self, key, value):
            headers[key] = value

        async def flush(self, **kwargs):
            raise HeaderRecorded

    monkeypatch.setattr(_session_config, "_project_name", _session_config._project_name)
    monkeypatch.setattr(_timeline, "_Timeline", Timeline)
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    kwargs: dict[str, Any] = {"model": "chosen", "effort": "high"} if explicit else {}
    with pytest.raises(HeaderRecorded):
        await agent.project.func("brief", **kwargs)
    assert headers["model"] == expected


def test_snapshot_dirty_base_merges(repo, tmp_path):
    (repo / "tracked").write_text("dirty\none\ntwo\nthree\n" + "unchanged\n" * 12 + "end\n")
    target = tmp_path / "snapshot"
    target.mkdir()
    base = snapshot(repo, target)
    with tarfile.open(target / "repo.tar.gz") as archive:
        archive.extractall(tmp_path / "unpacked", filter="data")
    worker = tmp_path / "unpacked/repo"
    (worker / "tracked").write_text((worker / "tracked").read_text().replace("dirty", "worker"))
    patch = subprocess.check_output(["git", "-C", str(worker), "diff", base])
    (repo / "tracked").write_text((repo / "tracked").read_text().replace("three", "author"))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "author edit")
    subprocess.run(["git", "-C", str(repo), "apply", "--3way"], input=patch, check=True)
    assert (repo / "tracked").read_text().startswith("worker\n")
    assert "\nauthor\n" in (repo / "tracked").read_text()


def test_restart_without_bridge(repo, bridge_root, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(repo))
    job = repo / ".claude-flyte/delegations/delegate-lost"
    job.mkdir(parents=True)
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert (job / "error.txt").read_text() == "supervisor restarted before this job finished"
    assert _bridge.delegate_cli(["status"]) == 0
    assert "delegate-lost failed" in capsys.readouterr().out
    (bridge_root / "answers/delegate-lost.json").unlink()
    assert _bridge.delegate_cli(["wait", "delegate-lost"]) == 1
    assert "supervisor restarted" in capsys.readouterr().err


def test_restart_cut_off_between_summary_and_patch(repo, bridge_root):
    cut = repo / ".claude-flyte/delegations/delegate-cut"
    cut.mkdir(parents=True)
    (cut / "summary.md").write_text("done")
    finished = repo / ".claude-flyte/delegations/delegate-done"
    finished.mkdir(parents=True)
    (finished / "summary.md").write_text("done")
    (finished / "factory.patch").write_text("")
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert (cut / "error.txt").read_text().startswith("supervisor restarted before this job finished")
    assert not (finished / "error.txt").exists()


@pytest.mark.parametrize("content", ["", "{oops", "[]"])
def test_restart_corrupt_answer(repo, bridge_root, content):
    (bridge_root / "requests/delegate-bad.json").write_text("{}")
    (bridge_root / "answers/delegate-bad.json").write_text(content)
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert json.loads((bridge_root / "answers/delegate-bad.json").read_text())["state"] == "failed"


@pytest.mark.asyncio
async def test_bad_requests_keep_serving(repo, bridge_root):
    messages = []
    supervisor = Delegations(bridge_root, repo, UNUSED, lambda: "claude", lambda *args: messages.append(args))
    (bridge_root / "requests/delegate-broken.json").write_text("{oops")
    (bridge_root / "requests/delegate-missing.json").write_text('{"id": "delegate-missing"}')
    supervisor.scan()
    for job_id in ("delegate-broken", "delegate-missing"):
        assert json.loads((bridge_root / f"answers/{job_id}.json").read_text())["state"] == "failed"
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_worker_summary_without_patch(repo, bridge_root):
    async def worker(*args, **kwargs):
        return "Could not implement: missing dependency"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *args: None)
    _bridge._write_atomic(
        bridge_root / "requests/delegate-next.json", {"id": "delegate-next", "brief": "fix"}
    )
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert "missing dependency" in (supervisor.jobs / "delegate-next/summary.md").read_text()
    assert "missing dependency" in (supervisor.jobs / "delegate-next/error.txt").read_text()


def test_usable_repository_and_worktree(repo, tmp_path):
    from flyte_agent_factory._delegation import usable_repo

    assert not usable_repo(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init")
    assert not usable_repo(empty)
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", str(worktree), "-b", "worker")
    assert (worktree / ".git").is_file()
    assert usable_repo(worktree)
    target = tmp_path / "snapshot"
    target.mkdir()
    assert snapshot(worktree, target) == git(worktree, "rev-parse", "HEAD")


def test_snapshot_index_lock(repo, tmp_path):
    (repo / ".git/index.lock").write_text("busy")
    target = tmp_path / "snapshot"
    target.mkdir()
    assert snapshot(repo, target) == git(repo, "rev-parse", "HEAD")
    assert (repo / ".git/index.lock").read_text() == "busy"


def test_snapshot_git_error_includes_stderr(repo, tmp_path):
    (repo / ".git/index").write_bytes(b"broken")
    target = tmp_path / "snapshot"
    target.mkdir()
    with pytest.raises(RuntimeError, match="index"):
        snapshot(repo, target)


@pytest.mark.parametrize("value", ["garbage", "0"])
def test_invalid_delegate_limit(monkeypatch, value):
    from flyte_agent_factory._delegation import delegate_limit

    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_MAX", value)
    messages = []
    assert delegate_limit(lambda *args: messages.append(args)) == 3
    assert "using 3" in messages[0][1]


@pytest.mark.parametrize("brief", ["status", "wait"])
def test_submit_reserved_word(bridge_root, capsys, brief):
    assert _bridge.delegate_cli(["submit", brief]) == 0
    job_id = capsys.readouterr().out.strip()
    assert json.loads((bridge_root / f"requests/{job_id}.json").read_text())["brief"] == brief


def test_submit_timeout_rejected(bridge_root):
    with pytest.raises(SystemExit):
        _bridge.delegate_cli(["ordinary brief", "--timeout", "1"])


def test_wrapper_removed_when_delegation_unavailable(tmp_path):
    _session_setup._install_bridge_commands(tmp_path, delegation_available=True)
    _session_setup._install_bridge_commands(tmp_path, delegation_available=False)
    assert not (tmp_path / "delegate").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "value"), [("MODEL", "not a model"), ("MODEL", "-x"), ("EFFORT", "invalid")])
async def test_invalid_author_policy(monkeypatch, key, value):
    monkeypatch.setenv(f"AGENT_FACTORY_AUTHOR_{key}", value)
    messages = []
    headers = {}

    class Stopped(Exception):
        pass

    class Timeline:
        def row(self, *args):
            messages.append(args)

        def header(self, key, value):
            headers[key] = value

        async def flush(self, **kwargs):
            raise Stopped

    monkeypatch.setattr(_timeline, "_Timeline", Timeline)
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    with pytest.raises(Stopped):
        await agent.project.func("brief")
    assert headers["model"] == "sonnet (default effort)"
    assert "author misconfigured" in messages[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["opusplan", "sonnet[1m]", "claude-sonnet-5-5"])
async def test_author_model_aliases_accepted(monkeypatch, alias):
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_MODEL", alias)
    messages = []
    headers = {}

    class Stopped(Exception):
        pass

    class Timeline:
        def row(self, *args):
            messages.append(args)

        def header(self, key, value):
            headers[key] = value

        async def flush(self, **kwargs):
            raise Stopped

    monkeypatch.setattr(_timeline, "_Timeline", Timeline)
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    with pytest.raises(Stopped):
        await agent.project.func("brief")
    assert headers["model"].startswith(f"{alias} ")
    assert not any("misconfigured" in str(m) for m in messages)


@pytest.mark.asyncio
async def test_answer_job_without_patch_survives_restart(repo, bridge_root, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(repo))

    async def worker(target, brief, engine, answer_only=False, on_engine=None):
        # The worker is told it is research, so its final message is the answer.
        assert answer_only is True
        assert "read-only research" in brief
        assert "git diff" not in brief
        return "The entry point is agent.py."

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    assert _bridge.delegate_cli(["--answer", "Where is the entry point?"]) == 0
    job_id = capsys.readouterr().out.strip()
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert not (supervisor.jobs / job_id / "factory.patch").exists()
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "The entry point is agent.py."
    (bridge_root / f"answers/{job_id}.json").unlink()
    Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "The entry point is agent.py."


def test_answer_instructions_require_delegation():
    assert "delegate --answer" in _instructions._project_memory(False, delegation_available=True)
    assert "delegate --answer" not in _instructions._project_memory(False)


def test_answer_restart_after_summary_before_bridge_result(repo, bridge_root, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(repo))
    job_id = "delegate-answer"
    job = repo / ".claude-flyte/delegations" / job_id
    job.mkdir(parents=True)
    (job / "answer-only").touch()
    (job / "summary.md").write_text("complete answer")
    _bridge._write_atomic(
        bridge_root / f"requests/{job_id}.json", {"id": job_id, "brief": "question", "answer_only": True}
    )
    _bridge._write_atomic(bridge_root / f"answers/{job_id}.json", {"state": "running"})
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "complete answer"


@pytest.mark.asyncio
async def test_completion_notices_include_all_job_outcomes(repo, bridge_root):
    async def worker(target, brief, engine, **kwargs):
        if "broken" in brief:
            raise RuntimeError("worker unavailable")
        if not kwargs.get("answer_only"):
            (target / "result").mkdir()
            (target / "result/factory.patch").write_text("diff --git a/new b/new\n")
        return "finished"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    await supervisor.service({"id": "delegate-ok", "brief": "implement"})
    await supervisor.service({"id": "delegate-failed", "brief": "broken"})
    await supervisor.service({"id": "delegate-answer", "brief": "research", "answer_only": True})
    notices = supervisor.drain_notices()
    assert len(notices) == 3
    assert "delegate-ok (codex, ok: 1 files)" in notices[0]
    assert "delegate-failed (codex, failed: RuntimeError: worker unavailable)" in notices[1]
    assert "delegate-answer (codex, answer ready)" in notices[2]
    assert all(
        f"delegate wait {job}" in notice
        for job, notice in zip(["delegate-ok", "delegate-failed", "delegate-answer"], notices, strict=True)
    )
    assert supervisor.drain_notices() == []


async def _finish_one(repo, bridge_root):
    async def worker(target, brief, engine, **kwargs):
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("diff --git a/new b/new\n")
        return "finished"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    await supervisor.service({"id": "delegate-ok", "brief": "implement"})
    return supervisor


@pytest.mark.asyncio
async def test_pending_notices_survive_a_supervisor_restart(repo, bridge_root):
    await _finish_one(repo, bridge_root)
    restarted = Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    notices = restarted.drain_notices()
    assert len(notices) == 1 and "delegate-ok (codex, ok: 1 files)" in notices[0]
    # Delivered, so a second restart has nothing left to say.
    assert Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED).drain_notices() == []


@pytest.mark.asyncio
async def test_a_job_cut_off_by_a_restart_is_announced_after_the_next_one(repo, bridge_root):
    (repo / ".claude-flyte/delegations/delegate-cut").mkdir(parents=True)
    first = Delegations(bridge_root, repo, UNUSED, lambda: "codex", UNUSED)
    # Changed on purpose (round 2, item 4): a failure with no engine is labelled "unknown"; it used to
    # call the routing callable, which logged a decision for work that is not being run.
    assert "delegate-cut (unknown, failed: supervisor restarted" in first.drain_notices()[0]
    first = Delegations(bridge_root, repo, UNUSED, lambda: "codex", UNUSED)
    assert first.drain_notices() == []


@pytest.mark.asyncio
async def test_notices_whose_delivery_failed_can_be_requeued(repo, bridge_root):
    supervisor = await _finish_one(repo, bridge_root)
    notices = supervisor.drain_notices()
    supervisor.requeue_notices()
    # Re-queued notices are persisted again, not only held in memory.
    assert Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED).drain_notices() == notices
    assert supervisor.drain_notices() == notices
    assert supervisor.drain_notices() == []


@pytest.mark.asyncio
async def test_delegation_tracks_the_engine_after_fallback(repo, bridge_root):
    async def worker(target, brief, engine, *, answer_only=False, on_engine=None):
        on_engine("claude", "claude sonnet/default")
        assert supervisor.active["delegate-fallback"]["engine"] == "claude"
        assert supervisor.active["delegate-fallback"]["setting"] == "claude sonnet/default"
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("")
        return "done"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *a: None)
    await supervisor.service({"id": "delegate-fallback", "brief": "work"})
    assert supervisor.recent[-1]["engine"] == "claude"
    assert "delegate-fallback (claude, ok:" in supervisor.drain_notices()[0]
    assert supervisor.total == 1


@pytest.mark.asyncio
async def test_startup_total_counts_the_delegations_already_made(repo, bridge_root):
    await _finish_one(repo, bridge_root)

    async def failing_worker(*args, **kwargs):
        # Counting is the point here, not the work: a worker that fails as a real one can.
        raise RuntimeError("no worker in this test")

    restarted = Delegations(bridge_root, repo, failing_worker, lambda: "codex", lambda *a: None)
    assert restarted.total == 1
    await restarted.service({"id": "delegate-second", "brief": "more"})
    assert restarted.total == 2
    assert Delegations(bridge_root, repo, UNUSED, lambda: "codex", lambda *a: None).total == 2


@pytest.mark.asyncio
async def test_an_invalid_request_counts_the_same_live_and_after_a_restart(repo, bridge_root):
    supervisor = await _finish_one(repo, bridge_root)
    (bridge_root / _bridge.REQUESTS / "delegate-bad.json").write_text(json.dumps({"id": "delegate-bad"}))
    supervisor.scan()
    assert (supervisor.jobs / "delegate-bad" / "error.txt").exists()
    restarted = Delegations(bridge_root, repo, UNUSED, lambda: "codex", lambda *a: None)
    assert supervisor.total == restarted.total == 1


@pytest.mark.asyncio
async def test_the_production_worker_passes_on_engine_through(tmp_path, monkeypatch):
    calls: list[Any] = []

    async def read_meters(timeline):
        calls.append("meters")

    class Output:
        async def download(self, path):
            calls.append(("download", path))

    async def call_reviewer(**kwargs):
        calls.append(kwargs)
        return "summary", Output()

    async def from_local(path):
        return f"dir:{path}"

    monkeypatch.setattr(_usage, "_read_meters", read_meters)
    monkeypatch.setattr(_reviewer, "_call_reviewer", call_reviewer)
    monkeypatch.setattr(Dir, "from_local", from_local)
    (tmp_path / "repo.tar.gz").write_bytes(b"")

    def on_engine(engine, setting):
        return None

    worker = _session_setup._delegate_worker(_timeline._Timeline())
    assert await worker(tmp_path, "brief", "codex", on_engine=on_engine) == "summary"
    assert calls[0] == "meters", "the meters are read before the worker starts"
    assert calls[1]["on_engine"] is on_engine
    assert (calls[1]["purpose"], calls[1]["engine"], calls[1]["role"]) == ("delegate", "codex", "dev")
    assert calls[2] == ("download", str(tmp_path / "result"))


def test_a_failure_without_an_engine_is_labelled_unknown_and_does_not_route(tmp_path, monkeypatch):
    root, work = tmp_path / "bridge", tmp_path / "work"
    for sub in ("requests", "answers"):
        (root / sub).mkdir(parents=True)
    work.mkdir()

    def engine():
        raise AssertionError("a failure report must not make a routing decision")

    supervisor = Delegations(root, work, UNUSED, engine, lambda *a: None)
    notices: list[str] = []
    monkeypatch.setattr(supervisor, "queue_notice", lambda job_id, text: notices.append(text))
    supervisor.fail("delegate-9", "boom")
    assert "(unknown, failed: boom)" in notices[0]
    supervisor.fail("delegate-10", "boom", engine="codex")
    assert "(codex, failed: boom)" in notices[1]


@pytest.mark.parametrize("kind", ["reviewer", "judge", "delegate"])
async def test_subagent_snapshot_preserves_executable_modes(repo, tmp_path, monkeypatch, kind):
    for name, mode in (("committed.sh", 0o755), ("changed.sh", 0o644)):
        (repo / name).write_text("#!/bin/sh\nexit 0\n")
        (repo / name).chmod(mode)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "scripts")
    head = git(repo, "rev-parse", "HEAD")
    (repo / "changed.sh").chmod(0o755)
    (repo / "new.sh").write_text("#!/bin/sh\nexit 0\n")
    (repo / "new.sh").chmod(0o755)
    before = git(repo, "status", "--porcelain")

    # Object storage transports bytes, not POSIX file permissions.
    class StoredDir:
        def __init__(self, root):
            self.root = root

        async def download(self, path):
            shutil.copytree(self.root, path, copy_function=shutil.copyfile, dirs_exist_ok=True)
            for file in Path(path).rglob("*"):
                if file.is_file():
                    file.chmod(0o644)

    stored: list[Path] = []

    async def from_local(path):
        dest = tmp_path / f"object-{len(stored)}"
        stored.append(dest)
        shutil.copytree(path, dest, copy_function=shutil.copyfile)
        return StoredDir(dest)

    monkeypatch.setattr(Dir, "from_local", from_local)
    if kind == "delegate":
        target = tmp_path / "snapshot"
        target.mkdir()
        base = snapshot(repo, target)
        workdir = await Dir.from_local(str(target))
    else:
        base = head
        workdir = await _checkpoint._upload_workspace(repo)

    async def run_codex(work, *args, **kwargs):
        tree = work
        if kind == "delegate":
            subprocess.run(["tar", "xzf", "repo.tar.gz"], cwd=work, check=True, timeout=10)
            tree = work / "repo"
        for name in ("committed.sh", "changed.sh", "new.sh"):
            assert (tree / name).stat().st_mode & 0o111 == 0o111
            subprocess.run([str(tree / name)], check=True, timeout=10)
        assert git(tree, "ls-files", "-s", "committed.sh").startswith("100755 ")
        git(tree, "add", "changed.sh", "new.sh")
        for name in ("changed.sh", "new.sh"):
            assert git(tree, "ls-files", "-s", name).startswith("100755 ")
        (tree / "committed.sh").chmod(0o644)
        patch = subprocess.check_output(["git", "-C", str(tree), "diff", "--binary", base])
        assert b"old mode 100755\nnew mode 100644" in patch
        if kind != "delegate":
            assert b"old mode 100644\nnew mode 100755" in patch
        (work / "factory.patch").write_bytes(patch)
        return "finished"

    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    summary, output = await agent.subagent.func(
        "check scripts",
        workdir=workdir,
        engine="codex",
        routed=True,
        role="dev" if kind == "delegate" else "qa",
    )
    assert summary == "finished"
    result = tmp_path / "result"
    await output.download(str(result))
    patch = (result / "factory.patch").read_bytes()
    assert b"old mode 100755\nnew mode 100644" in patch
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain") == before
    if kind == "delegate":
        subprocess.run(["git", "-C", str(repo), "apply", "--3way"], input=patch, check=True, timeout=10)
        assert (repo / "committed.sh").stat().st_mode & 0o111 == 0
        assert git(repo, "ls-files", "-s", "committed.sh").startswith("100644 ")


async def test_a_subagent_pod_writes_its_run_context_before_its_session_starts(tmp_path, monkeypatch):
    """`python -m flyte_agent_factory.child abort` reads the context where it runs: the sub-agent's own."""
    from types import SimpleNamespace

    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(name="sub1")))
    written: list[Path] = []

    def write_context(base: Path) -> Path:
        written.append(Path(base))
        return Path(base)

    monkeypatch.setattr(agent, "write_context", write_context)

    async def from_local(path):
        return path

    async def run_codex(work, *args, **kwargs):
        assert written == [work], "the context must be there before the session can run the helper"
        return "finished"

    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    summary, _ = await agent.subagent.func("b", engine="codex", routed=True)
    assert summary == "finished"


async def test_subagent_snapshot_keeps_absolute_symlinks(tmp_path, monkeypatch):
    # A workspace virtualenv links to the image's interpreter by absolute path.
    workspace = tmp_path / "ws"
    (workspace / ".venv" / "bin").mkdir(parents=True)
    (workspace / ".venv" / "bin" / "python").symlink_to("/usr/bin/env")
    (workspace / "run.sh").write_text("#!/bin/sh\n")
    (workspace / "run.sh").chmod(0o755)

    class StoredDir:
        def __init__(self, root):
            self.root = root

        async def download(self, path):
            shutil.copytree(self.root, path, copy_function=shutil.copyfile, dirs_exist_ok=True)

    async def from_local(path):
        dest = tmp_path / "object"
        shutil.copytree(path, dest, copy_function=shutil.copyfile)
        return StoredDir(dest)

    monkeypatch.setattr(Dir, "from_local", from_local)
    workdir = await _checkpoint._upload_workspace(workspace)
    target = tmp_path / "work"
    target.mkdir()
    await _checkpoint._download_workspace(workdir, target)
    link = target / ".venv" / "bin" / "python"
    assert link.is_symlink() and str(link.readlink()) == "/usr/bin/env"
    assert (target / "run.sh").stat().st_mode & 0o111 == 0o111
    assert not (target / _checkpoint._WORKSPACE_ARCHIVE).exists()


@pytest.mark.asyncio
async def test_each_job_records_the_child_actions_it_started(repo, bridge_root, capsys):
    """Concurrent delegates share a0; the job folder is what says which action belongs to which."""
    from flyte_agent_factory import child

    gate = asyncio.Event()

    async def worker(target, brief, engine, answer_only=False, on_engine=None):
        name = "kid-" + ("one" if "first" in brief else "two")
        child.notify_submitted(name)
        if "first" in brief:
            await gate.wait()
        else:
            gate.set()
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("diff --git a/x b/x\n")
        return "ok"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *args: None, 2)
    ids = []
    for brief in ("first task", "second task"):
        _bridge.delegate_cli([brief])
        ids.append(capsys.readouterr().out.strip())
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert (supervisor.jobs / ids[0] / "actions.txt").read_text().split() == ["kid-one"]
    assert (supervisor.jobs / ids[1] / "actions.txt").read_text().split() == ["kid-two"]


@pytest.mark.asyncio
async def test_delegate_summaries_are_redacted_before_the_coordinator_reads_them(
    repo, bridge_root, monkeypatch, capsys
):
    """Delegate summaries are redacted before the coordinator reads them."""
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(repo))

    async def worker(target, brief, engine, answer_only=False, on_engine=None):
        return "The token is " + "gho_" + "a1B2" * 9 + "."

    supervisor = Delegations(bridge_root, repo, worker, lambda: "codex", lambda *args: None)
    assert _bridge.delegate_cli(["--answer", "What is the token?"]) == 0
    job_id = capsys.readouterr().out.strip()
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    assert capsys.readouterr().out.strip() == "The token is <redacted>."
    assert "gho_" not in (supervisor.jobs / job_id / "summary.md").read_text()


@pytest.mark.parametrize(
    "archive_name", [_checkpoint._WORKSPACE_ARCHIVE, _checkpoint._LEGACY_WORKSPACE_ARCHIVE]
)
async def test_the_workspace_archive_is_unpacked_with_its_modes_and_removed(tmp_path, archive_name):
    assert _checkpoint._WORKSPACE_ARCHIVE == "agent-factory-workspace.tar"

    class Staged:
        def __init__(self, root):
            self.root = root

        async def download(self, path):
            shutil.copytree(self.root, path, dirs_exist_ok=True)

    source = tmp_path / "src"
    source.mkdir()
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    staged = tmp_path / "staged"
    staged.mkdir()
    with tarfile.open(staged / archive_name, "w") as archive:
        archive.add(source, arcname=".")
    target = tmp_path / "target"
    target.mkdir()
    await _checkpoint._download_workspace(Staged(staged), target)  # type: ignore[arg-type]
    assert (target / "run.sh").stat().st_mode & 0o111 == 0o111
    assert not (target / archive_name).exists()


async def test_workspace_upload_writes_only_the_current_archive_name(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    uploaded = tmp_path / "uploaded"

    async def from_local(path):
        shutil.copytree(path, uploaded)
        return Dir(path=str(uploaded))

    monkeypatch.setattr(Dir, "from_local", from_local)
    await _checkpoint._upload_workspace(source)
    assert _checkpoint._WORKSPACE_ARCHIVE == "agent-factory-workspace.tar"
    assert {path.name for path in uploaded.iterdir()} == {_checkpoint._WORKSPACE_ARCHIVE}
    with tarfile.open(uploaded / _checkpoint._WORKSPACE_ARCHIVE) as archive:
        script = archive.getmember("./run.sh")
        assert script.mode & 0o111 == 0o111
        content = archive.extractfile(script)
        assert content is not None
        with content:
            assert content.read() == b"#!/bin/sh\n"
