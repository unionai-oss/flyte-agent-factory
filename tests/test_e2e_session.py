"""A whole session, offline: real pty, real bridge, real wrappers, real pump.

Only two things are faked: the `claude` binary (tests/fake_claude.py, scripted)
and the cloud reviewers (`_evaluate`). Everything between -- the pty, the
`flyte-note` wrapper script, the notes file, the DONE gate, what gets typed
back into the session, what reaches the task log -- is the production path.

Run alone with `PYTHONPATH=src uv run python -m pytest tests/test_e2e_session.py -v -s`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _checkpoint,
    _review,
    _session_config,
    _session_setup,
    _supervise,
    _supervise_support,
    _timeline,
)
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._process import ClaudeProcess

FAKE = Path(__file__).parent / "fake_claude.py"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def workspace(tmp_path):
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "t")
    (work / "app.py").write_text("print('v0')\n")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "-u", "origin", "HEAD:main")
    (work / "BRIEF.md").write_text("Build the thing.\n")
    return work


class Rehearsal:
    """One scripted session under the real supervisor loop."""

    def __init__(self, tmp_path, workspace, bridge_root, steps):
        self.workspace = workspace
        self.bridge = Bridge(bridge_root)
        self.timeline = _timeline._Timeline()
        self.stop = asyncio.Event()
        self.log = tmp_path / "received.log"
        bin_dir = tmp_path / "bin"
        _session_setup._install_bridge_commands(bin_dir)
        script = tmp_path / "script.json"
        script.write_text(json.dumps(steps))
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_CLAUDE_SCRIPT": str(script),
            "FAKE_CLAUDE_LOG": str(self.log),
            "PYTHONUNBUFFERED": "1",
        }
        self.proc = ClaudeProcess([sys.executable, str(FAKE)], cwd=workspace, env=env, prompt_rules=[])

    def received(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    async def run(self, until, *, timeout=120.0, **pump_kwargs) -> None:
        await self.proc.start()
        pump = asyncio.create_task(
            _supervise._pump(self.bridge, self.proc, self.timeline, self.stop, **pump_kwargs)
        )
        try:
            await self.proc.submit("Read BRIEF.md and get started on it now.")
            async with asyncio.timeout(timeout):
                while not until():
                    if pump.done():
                        pump.result()
                    await asyncio.sleep(0.05)
        finally:
            self.stop.set()
            await asyncio.wait_for(pump, 30)
            await self.proc.stop(grace=5)


@pytest.fixture(autouse=True)
def fast_loop(monkeypatch):
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.05)


@pytest.mark.asyncio
async def test_a_session_goes_from_premature_done_to_a_passed_review(
    tmp_path, workspace, bridge_root, capsys, monkeypatch
):
    monkeypatch.setattr(_session_config, "_project_name", "rehearsal")
    # The checks script is required at DONE (board #18), so the session declares one with its criteria.
    push = (
        "git add -A -- rehearsal/ACCEPTANCE.md rehearsal/checks.sh app.py"
        " && git commit -qm work && git push -q"
    )
    steps = [
        {"when": "Read BRIEF.md", "run": ['flyte-note "starting"', 'flyte-note "DONE: first attempt"']},
        {
            "when": "rehearsal/ACCEPTANCE.md. is missing",
            "run": [
                "mkdir -p rehearsal && echo '- [ ] app prints v1' > rehearsal/ACCEPTANCE.md",
                "printf 'python3 app.py\\n' > rehearsal/checks.sh",
                "echo \"print('v1')\" > app.py",
                push,
                'flyte-note "DONE: criteria written"',
            ],
        },
        {
            "when": "rejected it .round 1",
            "run": ["echo \"print('v2')\" > app.py", push, 'flyte-note "DONE: fixed the finding"'],
        },
        {"when": "passed your work", "run": ['flyte-note "told the human"']},
    ]
    rounds: list[int] = []

    async def reviewers(workspace_, brief, acceptance, round_, timeline):
        rounds.append(round_)
        assert "app prints v1" in (workspace_ / "rehearsal/ACCEPTANCE.md").read_text()
        return (
            (True, "")
            if round_ == 2
            else (False, "### correctness reviewer\n\napp.py prints the wrong version")
        )

    monkeypatch.setattr(_review, "_evaluate", reviewers)
    run = Rehearsal(tmp_path, workspace, bridge_root, steps)
    await run.run(
        lambda: (
            "passed your work" in run.received()
            and any("told the human" in t for _, _, t in run.timeline._rows)
        ),
        workspace=workspace,
        brief="Build the thing.",
        pinned_acceptance=tmp_path / "pinned",
    )

    told = run.received()
    # The premature DONE was bounced by the preflight, not by a reviewer.
    assert "no review round was used" in told
    assert rounds == [1, 2], "the bounce must not consume a review round"
    assert "app.py prints the wrong version" in told, "findings reach the session"
    assert "PushNotification" in told and "ask-human" in told

    # And the task log -- the only thing `flyte get logs` shows -- tells the story.
    out = capsys.readouterr().out
    # `PYTHONPATH=src uv run python -m pytest tests/test_e2e_session.py -v -s` shows the log a run would have
    print(out, file=sys.__stdout__)
    for fragment in (
        "🚧 DONE bounced before review",
        "📌 pinned .claude-flyte/rehearsal/ACCEPTANCE.md",
        "status: done -- passed review in 2 round(s)",
    ):
        assert fragment in out, fragment
    assert (await _checkpoint._git_out(workspace, "rev-list", "--count", "@{u}..HEAD"))[1] == "0"


@pytest.mark.asyncio
async def test_a_silent_unattended_session_is_nudged_then_declared_parked(
    tmp_path, workspace, bridge_root, capsys, monkeypatch
):
    monkeypatch.setattr(_session_config, "IDLE_AFTER_S", 0.1)
    monkeypatch.setattr(_session_config, "NUDGE_AFTER_S", 0.4)
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))
    run = Rehearsal(tmp_path, workspace, bridge_root, [])
    await run.run(
        lambda: any(icon == "🚨" for _, icon, _ in run.timeline._rows),
        workspace=workspace,
        brief="b",
    )
    nudges = [line for line in run.received().splitlines() if "gone quiet" in line]
    assert len(nudges) == _session_config.MAX_NUDGES
    assert "Reminder 2 of" in nudges[1] and "ACCEPTANCE.md" in nudges[1], "later nudges name what is open"
    captured = capsys.readouterr()
    print(captured.out, captured.err, file=sys.__stdout__)
    assert "parked" in captured.err and "[ERROR]" in captured.err
