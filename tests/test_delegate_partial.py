"""A delegate worker that runs out of turns hands back its partial work instead of losing it."""

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte.report
import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _auth,
    _bridge,
    _instructions,
    _reviewer,
    _session_setup,
    _subagent_run,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._delegation import Delegations


def git(root: Path, *args: str) -> str:
    """Run `git` in `root` and return its stripped output."""
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def worker_tree(tmp_path):
    """`work/repo` as a worker leaves it: a base commit, an edited file, a new file and a binary one."""
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
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    return SimpleNamespace(work=work, repo=repo, base=base)


def test_the_turn_limit_is_read_from_the_result_subtype():
    """The turn limit is the one `_delegate_worker` hands to the CLI."""
    assert _session_setup._DELEGATE_MAX_TURNS == 80
    assert "max_turns=_DELEGATE_MAX_TURNS" in inspect.getsource(_session_setup._delegate_worker)


async def test_partial_handback_writes_the_patch_with_untracked_files_and_the_plan(worker_tree):
    """A partial handback writes the patch, untracked files included, and the plan."""

    async def never():
        raise AssertionError("the CLI gave a plan; no follow-up needed")

    payload = {"subtype": "error_max_turns", "result": "1. finish new.py\n2. run tests"}
    summary = await _subagent_run._partial_handback(worker_tree.work, worker_tree.base, 80, payload, never)
    patch = (worker_tree.work / "factory.partial.patch").read_bytes()
    assert b"diff --git a/new.py b/new.py" in patch and b"+after" in patch and b"blob.bin" in patch
    assert (worker_tree.work / "plan.md").read_text() == "1. finish new.py\n2. run tests\n"
    assert summary.startswith("PARTIAL: the worker hit its 80-turn limit") and "finish new.py" in summary
    # The patch applies to the base it was cut from, binary file included.
    git(worker_tree.repo, "checkout", "-q", "--", "tracked")
    (worker_tree.repo / "new.py").unlink()
    (worker_tree.repo / "blob.bin").unlink()
    git(worker_tree.repo, "reset", "-q")
    subprocess.run(["git", "-C", str(worker_tree.repo), "apply"], input=patch, check=True)
    assert (worker_tree.repo / "blob.bin").read_bytes() == b"\x00\x01\x02"


async def test_partial_handback_diffs_off_the_event_loop_thread(worker_tree, monkeypatch):
    """The diff is a blocking git call; it must not run on the event loop's thread."""
    threads = []

    def fake_work_diff(repo, base):
        threads.append(threading.current_thread())
        return b"patch"

    monkeypatch.setattr(_subagent_run, "work_diff", fake_work_diff)
    await _subagent_run._partial_handback(
        worker_tree.work,
        worker_tree.base,
        80,
        {"subtype": "error_max_turns", "result": "plan"},
        cast(Any, None),
    )
    assert threads and threads[0] is not threading.current_thread()


async def test_partial_handback_asks_for_a_plan_when_the_cli_gave_none(worker_tree):
    """A partial handback asks for a plan when the CLI gave none."""

    async def ask():
        return "  1. the rest  "

    await _subagent_run._partial_handback(
        worker_tree.work, worker_tree.base, 80, {"subtype": "error_max_turns"}, ask
    )
    assert (worker_tree.work / "plan.md").read_text() == "1. the rest\n"


async def test_partial_handback_still_hands_back_the_patch_when_the_plan_fails(worker_tree):
    """A partial handback still hands back the patch when the plan follow-up fails."""

    async def ask():
        raise RuntimeError("follow-up failed")

    summary = await _subagent_run._partial_handback(worker_tree.work, worker_tree.base, 80, {}, ask)
    assert (worker_tree.work / "factory.partial.patch").stat().st_size > 0
    assert "the worker gave no plan" in (worker_tree.work / "plan.md").read_text()
    assert summary.startswith("PARTIAL:")


async def test_partial_handback_with_no_extracted_repo_says_there_is_no_work(tmp_path):
    """A partial handback with no extracted repo says there is no work to hand back."""

    async def never():
        raise AssertionError("nothing to plan for")

    summary = await _subagent_run._partial_handback(
        tmp_path, "a" * 40, 80, {"subtype": "error_max_turns"}, never
    )
    assert "no work to hand back" in summary and summary.startswith("PARTIAL:")
    assert not (tmp_path / "plan.md").exists() and not (tmp_path / "factory.partial.patch").exists()


async def test_the_plan_follow_up_gets_the_token_through_the_pipe_like_the_worker(tmp_path, monkeypatch):
    """The plan follow-up gets the token through the pipe, as the worker does."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-test-token")
    monkeypatch.delenv(_auth.TOKEN_IN_ENV_VAR, raising=False)
    seen = {}

    class Done:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "1. the rest"}).encode(), b""

    async def fake_exec(*args, **kwargs):
        env, fds = kwargs["env"], kwargs["pass_fds"]
        seen["fds"], seen["env"] = fds, env
        seen["read"] = os.read(int(env[_auth.TOKEN_FD_VAR]), 100) if fds else None
        return Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plan = await _subagent_run._ask_remaining_work(
        "claude", tmp_path, {"HOME": "/h"}, {"session_id": "s"}, "m", "e"
    )
    assert plan == "1. the rest"
    assert seen["fds"] and seen["read"] == b"sk-test-token"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]


@pytest.fixture
def fake_claude(tmp_path, monkeypatch, worker_tree):
    """Provide a `claude` that ends at the turn limit, and answers a `--resume` follow-up with a plan."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    script = bin_dir / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if '--resume' in sys.argv:\n"
        "    print(json.dumps({'result': '1. the remaining step'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True,\n"
        "                      'session_id': 'sess-1', 'num_turns': 81}))\n"
        "    sys.exit(1)\n"
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
    return SimpleNamespace(calls=calls, workdir=Stored())


async def test_a_claude_worker_at_the_turn_limit_returns_its_partial_work(fake_claude, worker_tree):
    """A `claude` worker that stops at the turn limit returns its partial work."""
    summary, output = await agent.subagent.func(
        brief="b",
        workdir=fake_claude.workdir,
        engine="claude",
        routed=True,
        role="dev",
        max_turns=7,
        partial_base=worker_tree.base,
    )
    assert summary.startswith("PARTIAL: the worker hit its 7-turn limit") and "the remaining step" in summary
    work = Path(output.path)
    assert b"new.py" in (work / "factory.partial.patch").read_bytes()
    assert (work / "plan.md").read_text() == "1. the remaining step\n"
    first, followup = [json.loads(line) for line in fake_claude.calls.read_text().splitlines()]
    assert first[first.index("--max-turns") + 1] == "7"
    assert followup[followup.index("--resume") + 1] == "sess-1"
    assert int(followup[followup.index("--max-turns") + 1]) <= 3


async def test_a_worker_with_no_delegation_base_still_fails_at_the_turn_limit(fake_claude):
    """A worker with no delegation base still fails at the turn limit."""
    with pytest.raises(RuntimeError, match="exited 1"):
        await agent.subagent.func(
            brief="b", workdir=fake_claude.workdir, engine="claude", routed=True, role="dev"
        )


async def test_the_delegate_worker_names_its_turn_budget_and_passes_the_base(tmp_path, monkeypatch):
    """The delegate worker passes its turn budget and the delegation base to the reviewer call."""
    calls = []

    async def read_meters(timeline):
        pass

    class Output:
        async def download(self, path):
            pass

    async def call_reviewer(**kwargs):
        calls.append(kwargs)
        return "summary", Output()

    async def from_local(path):
        return path

    monkeypatch.setattr(_usage, "_read_meters", read_meters)
    monkeypatch.setattr(_reviewer, "_call_reviewer", call_reviewer)
    monkeypatch.setattr(Dir, "from_local", from_local)
    (tmp_path / "repo.tar.gz").write_bytes(b"")
    (tmp_path / "base").write_text("abc123\n")
    worker = _session_setup._delegate_worker(_timeline._Timeline())
    await worker(tmp_path, "brief", "claude")
    shutil.rmtree(tmp_path / "upload")
    await worker(tmp_path, "question", "claude", answer_only=True)
    assert [(c["max_turns"], c["partial_base"]) for c in calls] == [
        (_session_setup._DELEGATE_MAX_TURNS, "abc123"),
        (80, ""),
    ]


@pytest.fixture
def repo(tmp_path):
    """Provide the author's workspace, which a delegation snapshots."""
    root = tmp_path / "author"
    root.mkdir()
    git(root, "init", "-q")
    (root / "tracked").write_text("before")
    (root / ".gitignore").write_text(".claude-flyte/\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return root


async def partial_job(
    repo: Path, bridge_root: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Delegations, str, list[tuple[str, ...]]]:
    """Run one delegation whose worker ends partially; return the supervisor, job id and notices."""

    async def worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        # The worker is told the base the same way the job folder records it.
        assert (target / "base").read_text() == (
            repo / ".claude-flyte/delegations" / job_id / "base"
        ).read_text()
        (target / "result").mkdir()
        (target / "result/factory.partial.patch").write_text("diff --git a/new b/new\n")
        (target / "result/plan.md").write_text("1. finish\n")
        return "PARTIAL: the worker hit its 80-turn limit"

    notices: list[tuple[str, ...]] = []
    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *args: notices.append(args))
    assert _bridge.delegate_cli(["implement it"]) == 0
    job_id = capsys.readouterr().out.strip()
    supervisor.scan()
    await asyncio.gather(*supervisor.pending)
    return supervisor, job_id, notices


async def test_delegation_records_a_partial_result(repo, bridge_root, capsys):
    """A partial result is recorded in the answer file and the job folder."""
    supervisor, job_id, _ = await partial_job(repo, bridge_root, capsys)
    job = supervisor.jobs / job_id
    answer = json.loads((bridge_root / "answers" / f"{job_id}.json").read_text())
    assert answer == {
        "state": "partial",
        "summary": "PARTIAL: the worker hit its 80-turn limit",
        "partial_patch": str(job / "factory.partial.patch"),
        "plan": str(job / "plan.md"),
    }
    assert not (job / "factory.patch").exists() and not (job / "error.txt").exists()
    notice = supervisor.notices[job_id]
    assert "partial" in notice and "plan.md" in notice
    assert supervisor.recent[-1]["ok"] is False


async def test_delegate_wait_and_status_show_a_partial_result(repo, bridge_root, capsys, monkeypatch):
    """`delegate wait` and `delegate status` show a partial result."""
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(repo))
    supervisor, job_id, _ = await partial_job(repo, bridge_root, capsys)
    job = supervisor.jobs / job_id
    assert _bridge.delegate_cli(["status"]) == 0
    assert f"{job_id} partial" in capsys.readouterr().out
    assert _bridge.delegate_cli(["wait", job_id, "--timeout", "0"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("PARTIAL:")
    assert lines[-2:] == [str(job / "factory.partial.patch"), str(job / "plan.md")]
    # With the answer file gone, the job folder alone still says partial.
    (bridge_root / "answers" / f"{job_id}.json").unlink()
    assert _bridge.delegate_cli(["status"]) == 0
    assert f"{job_id} partial" in capsys.readouterr().out


async def test_a_restart_keeps_a_partial_result(repo, bridge_root, capsys):
    """A restart keeps a partial result."""
    supervisor, job_id, _ = await partial_job(repo, bridge_root, capsys)
    # A restart only reads the job folder; it never reaches the worker.
    restarted = Delegations(bridge_root, repo, cast(Any, None), lambda: "claude", lambda *a: None)
    job = restarted.jobs / job_id
    assert not (job / "error.txt").exists()
    assert json.loads((bridge_root / "answers" / f"{job_id}.json").read_text())["state"] == "partial"


def test_the_coordinator_is_told_how_to_continue_a_partial_result():
    """The coordinator note says how to continue a partial result."""
    note = _instructions._coordinator_note()
    assert "partial" in note and "plan.md" in note and "git apply --3way" in note
