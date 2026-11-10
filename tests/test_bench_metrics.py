"""Benchmark step 2, fix round 3: the metrics block through the real pump, a restart and the page budget."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import cast

import pytest

from flyte_agent_factory import (
    _bench,
    _checkpoint,
    _contract,
    _dashboard,
    _demo_checks,
    _gate_base,
    _gates,
    _inbox,
    _policy,
    _review,
    _reviewer,
    _session_config,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
)
from flyte_agent_factory import _bridge as _bridge_module
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._process import ClaudeProcess
from tests._wait import wait_for
from tests.test_nudge import _git, _Proc, _Queue, offline_report, repo  # noqa: F401 -- `repo` is a fixture, used by name

FAIL_TEXT = (
    "FINDING: crash on empty input\nEvidence: command: pytest tests/test_api.py | output: "
    "shortlinks/__main__.py:42 KeyError\nVERDICT: FAIL\n"
)
PASS_TEXT = "all good\nVERDICT: PASS\n"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", True)
    monkeypatch.setattr(_session_config, "_round_scores", [])
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("", False))
    monkeypatch.setattr(_policy, "knobs_seen", {})


class _Driver:
    """The real `_pump` → `_review_round` → `_evaluate` path, with a scripted reviewer and session."""

    def __init__(self, ws, monkeypatch, *, verdicts, post_verdict=None):
        self.ws = ws
        self.proc = _Proc()
        monkeypatch.setattr(self.proc, "returncode", None, raising=False)
        self.bridge = _Queue()
        monkeypatch.setattr(self.bridge, "questions", {}, raising=False)
        monkeypatch.setattr(self.bridge, "ask_history", [], raising=False)
        self.timeline = _timeline._Timeline()
        # The fakes stand in for the real bridge and process; the pump only touches what they define.
        self.timeline.bridge = cast(Bridge, self.bridge)
        self.stop = asyncio.Event()
        self.task: asyncio.Task[None] | None = None
        monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)

        async def nothing(*a, **k):
            return None

        async def empty(*a, **k):
            return ""

        for name in (
            "_move_board",
            "_publish_pull_request",
        ):
            monkeypatch.setattr(_supervise_support, name, nothing)
        monkeypatch.setattr(_demo_checks, "_publish_demo_reports", nothing)
        monkeypatch.setattr(_usage, "_read_meters", nothing)
        monkeypatch.setattr(_contract, "_load_or_freeze_contract", nothing)
        monkeypatch.setattr(_supervise_support, "_post_verdict", post_verdict or nothing)
        for name in ("_review_facts", "_push_fact"):
            monkeypatch.setattr(_review, name, empty)
        monkeypatch.setattr(_demo_checks, "_demo_package_problem", empty)
        monkeypatch.setattr(_session_config, "_applicable_standards", lambda: {})

        async def upload(_w):
            return "snapshot"

        async def plan(*a, **k):
            return "", True, ""

        monkeypatch.setattr(_checkpoint, "_upload_workspace", upload)
        monkeypatch.setattr(_contract, "_plan_next_round", plan)
        # Gate output that names a path no filesystem accepts: the P0 crash, on the real path.
        monkeypatch.setattr(
            _gates,
            "standards_results",
            lambda *a, **k: [
                SimpleNamespace(rule="lint", status=_gate_base.Status.PASS, output="x" * 256 + ".py")
            ],
        )
        self.round_verdicts = verdicts

        async def reviewer(*, purpose, round_, **k):
            passed = self.round_verdicts(round_)
            return (PASS_TEXT if passed or purpose != "correctness" else FAIL_TEXT), None

        monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)

    def start(self):
        self.task = asyncio.create_task(
            _supervise._pump(
                cast(Bridge, self.bridge),
                cast(ClaudeProcess, self.proc),
                self.timeline,
                self.stop,
                workspace=self.ws,
                brief="b",
            )
        )

    async def done_with(self, note, *, sent_before):
        self.bridge.push(note)
        await wait_for(lambda: len(self.proc.submitted) > sent_before, what=str(note))

    async def finish(self):
        self.stop.set()
        assert self.task is not None
        await asyncio.wait_for(self.task, timeout=30)


def _commit(ws, name, text="x = 1\n"):
    # Named paths only: the harness directory holds logs the pump writes while it runs.
    (ws / name).write_text(text)
    _git(ws, "add", name, *[p.name for p in ws.glob("gen_*.py")])
    _git(ws, "commit", "-m", name)
    _git(ws, "push")


def _accept(ws):
    path = ws / _session_config._acceptance_rel(_session_config._project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1. Prints a greeting.\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "acceptance")
    _git(ws, "push")


async def test_each_bounce_is_counted_once_in_its_own_category(repo, monkeypatch):  # noqa: F811 -- `repo` is the imported fixture
    d = _Driver(repo, monkeypatch, verdicts=lambda r: True)
    checks = repo / _session_config._checks_rel(_session_config._project_name)
    d.start()
    try:
        # 1. No ACCEPTANCE: the DONE is not ready, and the gate itself passes.
        await d.done_with("DONE: one", sent_before=0)
        assert "DONE bounced before review" in " ".join(text for _, _, text in d.timeline._rows)
        # 2. Declared checks fail: a preflight bounce.
        _accept(repo)
        checks.write_text("exit 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "failing checks")
        _git(repo, "push")
        await d.done_with("DONE: two", sent_before=len(d.proc.submitted))
        assert d.timeline.snapshot()["metrics"]["bounces"] == {"done": 1, "preflight": 1}
    finally:
        await d.finish()


async def test_the_pass_is_recorded_when_it_is_decided_and_survives_a_restart(repo, monkeypatch):  # noqa: F811 -- `repo` is the imported fixture
    _accept(repo)

    release = asyncio.Event()

    async def slow_post(*a, **k):
        await release.wait()

    d = _Driver(repo, monkeypatch, verdicts=lambda r: True, post_verdict=slow_post)
    d.start()
    try:
        # The post is held, so the pass is decided and recorded while the verdict is still unposted.
        d.bridge.push("DONE: gate")
        await wait_for(lambda: d.timeline.snapshot()["metrics"]["passed_round"])
        metrics = d.timeline.snapshot()["metrics"]
        done_at = [at for kind, at in metrics["phase_history"] if kind == "done"][0]
        assert metrics["passed_at"] == done_at
        release.set()
        await wait_for(lambda: d.proc.submitted)
        # _post_verdict finishes and _save_pass writes the record again
        await wait_for(lambda: (_session_config._load_pass_record() or {}).get("verdict_posted"))
        assert d.timeline.snapshot()["metrics"]["passed_at"] == done_at
        d.timeline.snapshot()  # persisted
    finally:
        await d.finish()
    again = _timeline._Timeline()
    assert again.snapshot()["metrics"]["passed_at"] == done_at


async def test_a_round_lists_only_its_own_changes_capped_at_100(repo, monkeypatch):  # noqa: F811 -- `repo` is the imported fixture
    _accept(repo)
    d = _Driver(repo, monkeypatch, verdicts=lambda r: r >= 2)
    d.start()
    try:
        for i in range(130):
            (repo / f"gen_{i}.py").write_text("x = 1\n")
        _commit(repo, "first.py")
        await d.done_with("DONE: one", sent_before=0)
        await wait_for(lambda: len(d.timeline.verdicts) >= 1)
        _commit(repo, "second.py")
        await d.done_with("DONE: two", sent_before=len(d.proc.submitted))
        await wait_for(lambda: len(d.timeline.verdicts) >= 2)
        rounds = {row["round"]: row for row in d.timeline.snapshot()["metrics"]["rounds"]}
    finally:
        await d.finish()
    # 130 generated files, first.py and the ACCEPTANCE file committed since the project started.
    assert rounds[1]["changed_files_count"] == 132
    assert len(rounds[1]["changed_files"]) == 100
    assert rounds[2]["changed_files"] == ["second.py"]
    assert rounds[2]["changed_files_count"] == 1


# --- a restart ------------------------------------------------------------------------------ #


def _condition(name, waits):
    future = waits[name] = asyncio.get_running_loop().create_future()

    class Wait:
        @staticmethod
        async def aio():
            return await future

    return SimpleNamespace(name=name, wait=Wait)


async def test_a_restart_keeps_the_block_and_adds_one_entry(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(
        _session_config, "_acceptance_text", lambda: ("## 1. Core\n1. Prints a greeting.\n", False)
    )
    counter = [0]

    waits: dict[str, asyncio.Future[str]] = {}

    async def open_condition(*a, **k):
        counter[0] += 1
        return _condition(f"ask-a0-{counter[0]:03d}", waits), counter[0]

    async def abandon(self, name, reason, *, approve):
        waits[name].set_result(reason)  # a text condition's close comes back as its value

    monkeypatch.setattr(_bridge_module, "open_condition", open_condition)
    monkeypatch.setattr(Bridge, "_abandon", abandon)
    registry = tmp_path / "state" / "bridge" / "questions.json"
    bridge = Bridge(tmp_path / "bridge", registry=registry)
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    timeline.phase("building")
    clock[0] = 1010.0
    timeline.snapshot()  # criteria_at = 1010
    clock[0] = 1020.0
    timeline.row("🚧", "DONE bounced before review (1): missing commit")
    timeline.preflight_bounces += 1
    timeline.phase("review", round_=1)
    timeline.verdicts.append({"round": 1, "verdict": "FAIL", "blocking": 2, "at": 1021.0})
    timeline.metric_label(kind="gate", round=1, name="lint", verdict="FAIL", file="a.py")
    _policy.knobs_seen["JUDGE_MODEL"] = "haiku"
    asker = asyncio.create_task(bridge._service({"id": "q1", "question": "Port?", "timeout_s": 600}, 1))
    await wait_for(lambda: "q1" in bridge.questions)
    await bridge.close_answered_in_session("8080", "Claude app", 1e12)
    await asker
    asker = asyncio.create_task(bridge._service({"id": "q2", "question": "OK?", "kind": "approve"}, 2))
    await wait_for(lambda: "q2" in bridge.questions)
    before = json.loads(json.dumps(timeline.snapshot()["metrics"]))
    asker.cancel()

    clock[0] = 5000.0
    monkeypatch.setattr(_policy, "knobs_seen", {})
    bridge2 = Bridge(tmp_path / "bridge", registry=registry)
    bridge2.restore()
    bridge2.restore()
    restarted = _timeline._Timeline()
    restarted.bridge = bridge2
    after = json.loads(json.dumps(restarted.snapshot()["metrics"]))
    for task in list(bridge2._pending):
        task.cancel()

    assert after["phase_history"] == [*before["phase_history"], ["restarting", 5000.0]]
    assert after["criteria_at"] == 1010.0
    assert {**after, "phase_history": before["phase_history"]} == before
    assert after["bounces"] == {"done": 1, "preflight": 1}
    assert len(after["asks"]) == 2


# --- the page ------------------------------------------------------------------------------- #


def test_the_degraded_page_keeps_the_core_with_10_rounds_of_2000_files():
    timeline = _timeline._Timeline()
    timeline.phase("building")
    for r in range(1, 11):
        timeline.verdicts.append(
            {
                "round": r,
                "verdict": "FAIL",
                "blocking": 2,
                "at": 1.0,
                "changed_files": [f"src/pkg/module_{k}/file.py" for k in range(2000)],
            }
        )
    for i in range(300):
        timeline.metric_label(kind="judge", round=1, name="c", verdict="FAIL", file=f"src/{i}.py", title="t")
    full = timeline.snapshot()["metrics"]
    page = timeline.render()
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    report = _dashboard.from_report(page)
    assert report is not None
    metrics = report["metrics"]
    for key in ("schema", "phase_history", "criteria_at", "passed_round", "passed_at", "rounds", "bounces"):
        assert metrics[key] == json.loads(json.dumps(full[key])), key
    assert all(len(row["changed_files"]) == 100 for row in metrics["rounds"])
    assert all(row["changed_files_count"] == 2000 for row in metrics["rounds"])


def test_the_core_holds_at_most_50_asks_and_30_rounds():
    timeline = _timeline._Timeline()
    # A stub with only the two attributes the timeline reads.
    timeline.bridge = cast(
        Bridge,
        SimpleNamespace(
            questions={},
            ask_history=[
                {
                    "id": f"q{i}",
                    "condition": f"ask-a0-{i:03d}",
                    "kind": "ask",
                    "opened_at": float(i),
                    "applies_at": float(i) + 60,
                }
                for i in range(80)
            ],
        ),
    )
    for r in range(1, 41):
        timeline.verdicts.append({"round": r, "verdict": "FAIL", "blocking": 1, "at": float(r)})
    metrics = timeline.snapshot()["metrics"]
    assert [a["name"] for a in metrics["asks"]][0] == "ask-a0-030"
    assert len(metrics["asks"]) == 50
    assert [row["round"] for row in metrics["rounds"]] == list(range(11, 41))


# --- case_record counts asks as the harness does --------------------------------------------- #


def test_case_record_leaves_out_asks_opened_after_the_pass(monkeypatch):
    from tests.test_bench import RUN, harness, planned

    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    timeline = _timeline._Timeline()

    def ask(name, opened, kind="ask"):
        return {
            "id": name,
            "condition": name,
            "kind": kind,
            "opened_at": opened,
            "applies_at": opened + 60,
            "deadline_at": opened + 90,
        }

    history = [ask("ask-a0-001", 1100.0), ask("ask-a0-002", 1300.0, "approve")]
    timeline.bridge = cast(Bridge, SimpleNamespace(questions={}, ask_history=history))
    timeline.verdicts.append({"round": 1, "verdict": "PASS", "blocking": 0, "at": 1200.0})
    clock[0] = 1200.0
    timeline.phase("done")
    _inbox._save_pass(1, "abc", timeline=timeline)  # the release ask (1300) is opened after this
    snap = _dashboard.from_report(timeline.render())
    assert snap is not None
    record = _bench.case_record(planned(), RUN, snap, harness(started_at=1000.0, ended_at=1400.0), None)
    assert (record["asks"], record["blocking_asks"], record["first_ask_s"]) == (1, 0, 100.0)


def test_phase_history_is_bounded_and_keeps_the_start():
    timeline = _timeline._Timeline()
    first = list(timeline.phase_history[0])
    for i in range(2 * _timeline.PHASE_HISTORY_MAX):
        timeline.phase("review" if i % 2 else "build", round_=i)
    assert len(timeline.phase_history) == _timeline.PHASE_HISTORY_MAX
    assert timeline.phase_history[0] == first
    assert timeline.phase_history[-1][0] == "review"
    timeline._save_metrics()
    again = _timeline._Timeline()  # a restart appends its entry within the same bound
    assert len(again.phase_history) == _timeline.PHASE_HISTORY_MAX
    assert again.phase_history[0] == first and again.phase_history[-1][0] == "restarting"


def test_a_read_only_state_directory_is_reported_once(monkeypatch):
    lines = []
    monkeypatch.setattr(_timeline, "_log_line", lambda text, **kw: lines.append(text))
    timeline = _timeline._Timeline()

    def denied(*a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(_timeline, "_write_atomic", denied)
    for i in range(5):
        timeline.phase("build", round_=i)
        timeline._save_metrics()
    assert sum("could not save the metrics state" in line for line in lines) == 1
