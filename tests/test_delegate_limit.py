"""A delegate whose engine is at its usage limit waits or re-routes, and keeps the work it did (#125)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import flyte.report
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _meter,
    _pins,
    _reviewer,
    _session_config,
    _session_setup,
    _subagent_run,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations
from flyte_agent_factory._router import RouterDeferred

_KEYS = (
    "AGENT_FACTORY_COORDINATOR_ENGINE",
    "AGENT_FACTORY_AUTHOR_ENGINE",
    "AGENT_FACTORY_DELEGATE_ENGINE",
    "AGENT_FACTORY_ENGINE_FALLBACK",
    "AGENT_FACTORY_ADMITTED_ENGINE",
)


# The fixed "now" of every wait below. Far in the future so that the real meter store, which judges
# a recorded limit against the real clock, still sees one set from here as live.
NOW = 4_000_000_000.0


def _fixed_clock(supervisor: Delegations) -> None:
    supervisor.clock = lambda: NOW


@pytest.fixture(autouse=True)
def _no_pins(monkeypatch, tmp_path):
    """Each test sets the pins it is about; none leak in from the environment or an earlier test."""
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(_pins, "engine_input", "")
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    """The coordinator's workspace, which a delegation snapshots."""
    root = tmp_path / "author"
    root.mkdir()
    git(root, "init", "-q")
    (root / "tracked").write_text("before")
    (root / ".gitignore").write_text(".claude-flyte/\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return root


class Rows:
    """The supervisor's timeline rows, as `emit` receives them."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []

    def __call__(self, icon: str, text: str) -> None:
        self.rows.append((icon, text))

    def text(self) -> str:
        return "\n".join(f"{icon} {text}" for icon, text in self.rows)


def answer(bridge_root: Path, job_id: str) -> dict[str, Any]:
    answered: dict[str, Any] = json.loads((bridge_root / "answers" / f"{job_id}.json").read_text())
    return answered


async def done_worker(
    target: Path,
    brief: str,
    engine: str,
    *,
    answer_only: bool = False,
    on_engine: Callable[[str, str], None] | None = None,
) -> str:
    (target / "result").mkdir()
    (target / "result/factory.patch").write_text("")
    return f"done on {engine}"


async def test_pinned_delegate_waits_then_completes(repo, bridge_root, monkeypatch):
    """Routing defers once: the job records `waiting` with the reset, sleeps outside its slot, then runs."""
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "claude")
    reset = NOW + 600
    routed: list[str] = []

    def engine() -> str:
        routed.append("asked")
        if len(routed) == 1:
            raise RouterDeferred("USAGE_LIMIT router: delegate -> deferred (pin claude exhausted)", reset)
        return "claude"

    rows = Rows()
    supervisor = Delegations(bridge_root, repo, done_worker, engine, rows, max_jobs=1)
    _fixed_clock(supervisor)
    seen_while_waiting: list[dict[str, Any]] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        seen_while_waiting.append(answer(bridge_root, "delegate-a"))
        # The wait is outside the slot: another job could run now.
        assert not supervisor.slots.locked()
        assert "delegate-a" not in supervisor.active

    supervisor.sleep = sleep
    await supervisor.service({"id": "delegate-a", "brief": "implement"})

    assert seen_while_waiting == [{"state": "waiting", "engine": "claude", "until": reset}]
    assert slept == [600]
    final = answer(bridge_root, "delegate-a")
    assert final["state"] == "done" and final["summary"] == "done on claude"
    assert "⏸️ delegate delegate-a waits for claude until" in rows.text()
    assert "failed after" not in rows.text() and "deferred:" not in rows.text()
    assert not (supervisor.jobs / "delegate-a" / "error.txt").exists()
    assert [job["ok"] for job in supervisor.recent] == [True]
    assert supervisor.total == 1


@pytest.fixture
def production(repo, bridge_root, tmp_path, monkeypatch):
    """The production delegate engine and worker over a real `MeterStore`; the sub-agent call is faked."""
    store = _meter.MeterStore(tmp_path / "meters")
    calls: list[str] = []

    class Output:
        async def download(self, path):
            Path(path).mkdir()
            (Path(path) / "factory.patch").write_text("")

    async def raw(**kwargs):
        calls.append(kwargs["engine"])
        return "done", Output()

    async def upload(path):
        return path

    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "{}")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    monkeypatch.setattr(_reviewer, "_call_reviewer_raw", raw)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    monkeypatch.setattr(Dir, "from_local", upload)
    timeline = _timeline._Timeline()
    rows = Rows()
    supervisor = Delegations(
        bridge_root,
        repo,
        _session_setup._delegate_worker(timeline),
        _usage._delegation_engine(timeline),
        rows,
    )
    return SimpleNamespace(store=store, calls=calls, supervisor=supervisor, rows=rows)


async def test_a_pinned_delegate_waits_for_its_engine_through_the_real_router(
    production, bridge_root, monkeypatch
):
    """`DELEGATE_ENGINE=claude` with Claude limited: waits until Claude's reset, then runs on Claude only."""
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "claude")
    reset = NOW + 900
    _fixed_clock(production.supervisor)
    production.store.note_limit("claude", reset, _usage._limit_account("claude"))
    seen: list[dict[str, Any]] = []

    async def sleep(seconds: float) -> None:
        seen.append(answer(bridge_root, "delegate-p"))
        # Claude's limit resets: a fresh store knows of no limit.
        monkeypatch.setattr(_usage, "_meters", _meter.MeterStore(production.store.root / "after"))

    production.supervisor.sleep = sleep
    await production.supervisor.service({"id": "delegate-p", "brief": "implement"})

    assert len(seen) == 1 and seen[0]["state"] == "waiting" and seen[0]["engine"] == "claude"
    assert seen[0]["until"] == reset
    assert answer(bridge_root, "delegate-p")["state"] == "done"
    assert production.calls == ["claude"]
    assert "failed after" not in production.rows.text() and "USAGE_LIMIT" not in production.rows.text()


async def test_unpinned_delegate_skips_limited_engine(production, bridge_root, monkeypatch):
    """With only the coordinator pinned to Claude and Claude limited, a delegate runs on Codex at once."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    production.store.note_limit("claude", NOW + 900, _usage._limit_account("claude"))

    production.supervisor.sleep = AsyncMock(
        side_effect=AssertionError("an unpinned delegate with a usable engine does not wait")
    )
    await production.supervisor.service({"id": "delegate-u", "brief": "implement"})

    assert answer(bridge_root, "delegate-u")["state"] == "done"
    assert production.calls == ["codex"]


def test_delegate_does_not_inherit_coordinator_pin(monkeypatch):
    """The coordinator's pin, hard as it is, is not the delegates': only `DELEGATE_ENGINE` pins them."""
    monkeypatch.setenv("AGENT_FACTORY_COORDINATOR_ENGINE", "claude")
    assert _pins.author_pin() == _pins.Pin("claude", True, "COORDINATOR_ENGINE")
    assert _pins.delegate_pin().engine == ""
    _pins.set_direction_pin("codex")
    assert _pins.delegate_pin().engine == ""
    monkeypatch.setenv("AGENT_FACTORY_DELEGATE_ENGINE", "codex")
    assert _pins.delegate_pin() == _pins.Pin("codex", True, "DELEGATE_ENGINE")


async def test_worker_deferral_is_not_a_worker_failure(repo, bridge_root):
    """A worker refused mid-task with no engine left defers (a RuntimeError) rather than failing the job."""
    runs: list[str] = []

    async def worker(target: Path, brief: str, engine: str, **kwargs: Any) -> str:
        runs.append(engine)
        if len(runs) == 1:
            raise RouterDeferred("USAGE_LIMIT router: delegate -> deferred (pin claude exhausted)", None)
        return await done_worker(target, brief, engine, **kwargs)

    rows = Rows()
    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", rows)
    seen: list[dict[str, Any]] = []
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        seen.append(answer(bridge_root, "delegate-w"))

    supervisor.sleep = sleep
    _fixed_clock(supervisor)
    await supervisor.service({"id": "delegate-w", "brief": "implement"})

    # No reset known: it waits the standard retry interval.
    assert seen[0]["state"] == "waiting" and seen[0]["engine"] == "any engine"
    assert seen[0]["until"] == NOW + _session_config.LIMIT_RETRY_S
    assert slept == [_session_config.LIMIT_RETRY_S]
    assert runs == ["claude", "claude"]
    assert answer(bridge_root, "delegate-w")["state"] == "done"
    assert "failed" not in rows.text()
    job = supervisor.jobs / "delegate-w"
    assert not (job / "error.txt").exists()
    assert "delegate-w" not in supervisor.notices or "failed" not in supervisor.notices["delegate-w"]
    assert [entry["ok"] for entry in supervisor.recent] == [True]


async def test_a_job_waiting_when_the_supervisor_restarts_is_routed_again(repo, bridge_root):
    """`waiting` is recorded state: a restart serves the request afresh rather than failing it."""
    first = Delegations(bridge_root, repo, done_worker, lambda: "claude", Rows())
    (bridge_root / "requests" / "delegate-r.json").write_text(
        json.dumps({"id": "delegate-r", "brief": "implement"})
    )

    def deferred() -> str:
        raise RouterDeferred("USAGE_LIMIT deferred", NOW + 600)

    first.engine = deferred

    async def cut_off(seconds: float) -> None:
        raise asyncio.CancelledError  # the pod goes away mid-wait

    first.sleep = cut_off
    with pytest.raises(asyncio.CancelledError):
        await first.service({"id": "delegate-r", "brief": "implement"})
    assert answer(bridge_root, "delegate-r")["state"] == "waiting"

    restarted = Delegations(bridge_root, repo, done_worker, lambda: "claude", Rows())
    job = restarted.jobs / "delegate-r"
    assert not (job / "error.txt").exists()
    assert restarted.total == 1
    restarted.scan()
    await asyncio.gather(*restarted.pending)
    assert answer(bridge_root, "delegate-r")["state"] == "done"
    assert restarted.total == 1


async def test_a_usage_limited_partial_says_so_in_its_notice(repo, bridge_root):
    """The notice and row of a usage-limited hand-back name the limit, not the turn limit."""

    async def worker(target: Path, brief: str, engine: str, **kwargs: Any) -> str:
        (target / "result").mkdir()
        (target / "result/factory.partial.patch").write_text("diff --git a/new b/new\n")
        (target / "result/plan.md").write_text("1. finish\n")
        return "PARTIAL: usage limit on claude: the worker stopped before finishing."

    rows = Rows()
    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", rows)
    await supervisor.service({"id": "delegate-l", "brief": "implement"})
    assert answer(bridge_root, "delegate-l")["state"] == "partial"
    assert "partial: hit a usage limit, 1 files so far" in supervisor.notices["delegate-l"]
    assert "delegate delegate-l (claude) hit a usage limit" in rows.text()


# --------------------------------------------------------------------------- #
# Inside the worker: a usage limit hands back the partial work.
# --------------------------------------------------------------------------- #


@pytest.fixture
def worker_tree(tmp_path):
    """`work/repo` as a worker leaves it: a base commit and an edited and a new file."""
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "tracked").write_text("before\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Delegation base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "tracked").write_text("after\n")
    (repo / "new.py").write_text("print(1)\n")
    return SimpleNamespace(work=work, repo=repo, base=base)


@pytest.fixture
def limited_claude(tmp_path, monkeypatch, worker_tree):
    """A `claude` CLI refused with its usage limit, run by the real `subagent` on `worker_tree`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    script = bin_dir / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': True,\n"
        "                  'result': \"You've hit your limit · resets 3pm (UTC)\", 'num_turns': 12}))\n"
        "sys.exit(1)\n"
    )
    script.chmod(0o755)
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(program, *args, **kwargs):
        return await real_exec(str(script) if program == "claude" else program, *args, **kwargs)

    class Stored:
        async def download(self, path):
            shutil.copytree(worker_tree.work, path, dirs_exist_ok=True)

    async def from_local(path):
        return SimpleNamespace(path=path)

    async def report(text, **kwargs):
        return None

    reset = NOW + 3 * 3600
    monkeypatch.setattr(_usage, "_reset_at", lambda text, now=None: reset)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_session_setup, "_seed_team", lambda work: {"dev"})
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path))
    monkeypatch.setattr(Dir, "from_local", from_local)
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "slot").mkdir()
    return SimpleNamespace(calls=calls, workdir=Stored(), reset=reset)


async def test_usage_limit_with_work_hands_back_partial(limited_claude, worker_tree):
    """A routed worker refused mid-task returns its patch and a plan, and still reports the limit."""
    summary, output = await agent.subagent.func(
        brief="b",
        workdir=limited_claude.workdir,
        engine="claude",
        routed=True,
        role="dev",
        partial_base=worker_tree.base,
    )
    clean, record = _usage._split_usage(summary)
    assert clean.startswith("PARTIAL: usage limit on claude (resets ")
    assert "before finishing" in clean and "Remaining work:" in clean
    # The caller (`_call_reviewer`) notes a refusal it ingests just as one it catches.
    assert record == {"refusals": {"claude": limited_claude.reset}}
    work = Path(output.path)
    patch = (work / "factory.partial.patch").read_bytes()
    assert b"new.py" in patch and b"+after" in patch
    assert "the worker gave no plan" in (work / "plan.md").read_text()
    # One CLI call: a limited engine is not asked for a plan.
    assert len(limited_claude.calls.read_text().splitlines()) == 1


async def test_usage_limit_without_work_reraises(limited_claude, worker_tree):
    """With nothing changed there is nothing to keep: the limit is raised for the caller to wait on."""
    git(worker_tree.repo, "checkout", "-q", "--", "tracked")
    (worker_tree.repo / "new.py").unlink()
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        await agent.subagent.func(
            brief="b",
            workdir=limited_claude.workdir,
            engine="claude",
            routed=True,
            role="dev",
            partial_base=worker_tree.base,
        )
    assert not (worker_tree.work / "factory.partial.patch").exists()


async def test_a_research_worker_at_its_limit_still_raises(limited_claude, worker_tree):
    """An answer-only worker has no patch to hand back, whatever its tree holds."""
    with pytest.raises(RuntimeError, match=_usage.USAGE_LIMIT_MARK):
        await agent.subagent.func(
            brief="b", workdir=limited_claude.workdir, engine="claude", routed=True, answer_only=True
        )


async def test_limit_handback_without_an_extracted_repo_is_none(tmp_path):
    assert await _subagent_run._limit_handback(tmp_path, "a" * 40, "codex", None, routed=True) is None


async def test_a_tree_that_cannot_be_diffed_at_the_limit_hands_nothing_back(worker_tree, monkeypatch, capsys):
    def broken(repo, base):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(_subagent_run, "work_diff", broken)
    assert await _subagent_run._limit_handback(worker_tree.work, worker_tree.base, "claude", 123.0) is None
    assert "[delegate] no partial patch at the limit: CalledProcessError" in capsys.readouterr().err
    assert not (worker_tree.work / "factory.partial.patch").exists()


async def test_a_routed_limit_handback_carries_the_refusal_in_its_summary(worker_tree):
    until = time.time() + 3600
    routed = await _subagent_run._limit_handback(worker_tree.work, worker_tree.base, "claude", until, True)
    assert routed is not None
    clean, record = _usage._split_usage(routed)
    assert clean.startswith("PARTIAL: usage limit on claude (resets ")
    assert record == {"refusals": {"claude": until}}


async def test_an_unrouted_limit_handback_or_one_without_a_reset_carries_no_refusal(worker_tree):
    until = time.time() + 3600
    for routed, reset in ((False, until), (True, None)):
        summary = await _subagent_run._limit_handback(
            worker_tree.work, worker_tree.base, "claude", reset, routed
        )
        assert summary is not None and _usage._USAGE_MARK not in summary


async def test_turn_limit_handback_text_unchanged(worker_tree, tmp_path):
    """The turn-limit summaries read exactly as before the cause was added."""

    never = AsyncMock(side_effect=AssertionError("the CLI gave a plan"))
    summary = await _subagent_run._partial_handback(
        worker_tree.work, worker_tree.base, 80, {"result": "1. the rest"}, never
    )
    assert (
        summary
        == "PARTIAL: the worker hit its 80-turn limit before finishing.\n\nRemaining work:\n1. the rest"
    )
    nothing = await _subagent_run._partial_handback(tmp_path / "empty", "a" * 40, 7, {}, never)
    assert nothing == (
        "PARTIAL: the worker hit its 7-turn limit before it had extracted the repo, "
        "so there is no work to hand back. Run it again, with a narrower brief if it is large."
    )


async def test_a_healthy_claude_run_reports_its_meter_without_a_limit(
    limited_claude, worker_tree, tmp_path, monkeypatch
):
    """Utilization headers on a successful run are reported, and no reset time is invented."""
    (tmp_path / "bin" / "claude").write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done',\n"
        "                  'headers': {'anthropic-ratelimit-unified-5h-utilization': '0.25'}}))\n"
    )
    comments: list[str] = []

    async def report(text, **kwargs):
        comments.append(text)

    monkeypatch.setattr(flyte.report.log, "aio", report)
    summary, _ = await agent.subagent.func(
        brief="b", workdir=limited_claude.workdir, engine="claude", routed=True, role="dev"
    )
    assert "refusals" not in (_usage._split_usage(summary)[1] or {})
    meter = next(text for text in comments if text.startswith("<!--meter "))
    assert '"five_hour_pct": 25.0' in meter and '"limited_until": null' in meter
