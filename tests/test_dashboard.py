"""The run report as a dashboard: sections, the JSON twin, and the readers that predate it."""

import asyncio
import dataclasses
import json
import re
import time
from types import SimpleNamespace
from typing import Any, cast

import flyte.report
import pytest

from flyte_agent_factory import (
    _contract,
    _dashboard,
    _inbox,
    _meter,
    _profile,
    _reviewer,
    _session_config,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
)
from flyte_agent_factory import _delegation as delegation
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._delegation import Delegations, Worker
from flyte_agent_factory._process import ClaudeProcess
from tests._wait import wait_for

ACCEPTANCE = """# Acceptance

## 1. Core
1. The CLI prints a greeting.
2. `make test` passes.
3. **C3.** Errors exit non-zero.
"""


@pytest.fixture(autouse=True)
def _no_ledger_or_acceptance(monkeypatch):
    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_session_config, "_usage_loaded", True)
    monkeypatch.setattr(_session_config, "_round_scores", [])
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: ("", False))


def data_of(page: str) -> dict[str, Any]:
    data = _dashboard.from_report(page)
    assert data is not None, "the dashboard fell back to the plain rows"
    return data


def timeline_for(**attrs):
    timeline = _timeline._Timeline()
    timeline.header("project", "wordstats")
    timeline.header("run", "r123")
    timeline.header("model", "sonnet (default effort)")
    timeline.run_url = "https://console.example/v2/domain/d/project/p/runs/r123"
    for key, value in attrs.items():
        setattr(timeline, key, value)
    return timeline


def section_titles(page: str) -> list[str]:
    return re.findall(r"<h4>(.*?)</h4>", page)


@pytest.mark.parametrize(
    ("setup", "label"),
    [
        (lambda t: None, "starting"),
        (lambda t: t.phase("building"), "building"),
        (lambda t: t.phase("checks"), "checks"),
        (lambda t: t.phase("review", round_=2), "review round 2 of"),
        (lambda t: t.phase("limited", limited_until=1_700_000_000), "limited until 22:13 UTC"),
        (lambda t: t.phase("limited"), "limited (reset time not shown)"),
        (lambda t: t.phase("done"), "done"),
        (lambda t: t.phase("released"), "released (idle after passing review)"),
        (lambda t: t.phase("failed"), "failed"),
    ],
)
def test_each_phase_renders_in_the_status_line(setup, label):
    timeline = timeline_for()
    setup(timeline)
    page = timeline.render()

    assert label in page
    assert "Needs you" not in page
    data = data_of(page)
    assert data["project"] == "wordstats"
    assert data["phase"]["kind"] in {
        "starting",
        "building",
        "checks",
        "review",
        "limited",
        "done",
        "released",
        "failed",
    }


def test_sections_come_in_the_agreed_order():
    timeline = timeline_for()
    page = timeline.render()
    assert section_titles(page) == [
        "Progress",
        "Review",
        "Working on now",
        "Cost &amp; limits",
        "Deliverables",
        "Risks",
    ]
    assert page.index("<summary>Timeline") > page.index("<h4>Risks</h4>")
    assert page.index("<th>phase</th>") < page.index("<h4>Progress</h4>")


def test_time_in_phase_and_last_activity():
    timeline = timeline_for()
    timeline.phase("building")
    timeline._phase["since"] = time.time() - 125
    timeline._last_note_at = time.time() - 600
    page = timeline.render()
    assert "for 2m" in page
    assert "10m ago" in page


def test_last_activity_ignores_checkpoints_and_counts_session_output(monkeypatch):
    timeline = timeline_for()
    timeline.row("💾", "checkpointed")
    assert data_of(timeline.render())["last_activity_at"] in (None, 0.0)

    # The session stamps output on the monotonic clock and the page shows wall time, so pin both clocks.
    monkeypatch.setattr(
        _timeline,
        "time",
        SimpleNamespace(monotonic=lambda: 500.0, strftime=time.strftime, localtime=time.localtime),
    )
    timeline.proc = SimpleNamespace(last_output=470.0)
    assert timeline.snapshot(now=10_000.0)["last_activity_at"] == 9_970.0


class _Condition:
    name = "ask-a0-001"


def _bridge_with(*questions):
    bridge = Bridge.__new__(Bridge)
    bridge.questions = {q["id"]: q for q in questions}
    return bridge


def _question(**kw):
    now = time.time()
    return {
        "id": "q1",
        "kind": "ask",
        "text": "Which port should the demo use? Default: 8080",
        "opened_at": now - 3 * 60,
        "applies_at": now + 57 * 60,
        "default": "Default: 8080",
        "condition": "ask-a0-001",
        "timed_out": False,
        **kw,
    }


def test_needs_you_appears_only_with_open_questions():
    assert "Needs you" not in timeline_for().render()

    timeline = timeline_for(bridge=_bridge_with(_question()))
    page = timeline.render()
    assert "Needs you (1)" in page
    assert "Which port should the demo use?" in page
    assert "open 3m" in page
    assert "Default: 8080" in page
    assert 'href="https://console.example/v2/domain/d/project/p/runs/r123/actions/ask-a0-001"' in page
    data = data_of(page)
    assert data["phase"]["kind"] == "waiting"
    assert data["needs_you"][0]["text"].startswith("Which port")

    timeline.bridge = _bridge_with()
    assert "Needs you" not in timeline.render()


def test_a_question_that_timed_out_is_flagged_and_does_not_hold_the_phase():
    timeline = timeline_for(bridge=_bridge_with(_question(timed_out=True)))
    timeline.phase("building")
    page = timeline.render()
    assert "timed out on the session side" in page
    assert data_of(page)["phase"]["kind"] == "building"


def test_question_text_is_escaped():
    timeline = timeline_for(bridge=_bridge_with(_question(text="<img src=x onerror=alert(1)>")))
    assert "<img" not in timeline.render()


def test_progress_counts_criteria_with_notes_and_checks(monkeypatch):
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: (ACCEPTANCE, True))
    timeline = timeline_for()
    timeline.row("📝", "C1 PASS, C2: FAIL -- make test red; C3 unchecked")
    timeline.row("📝", "C2 PASS now")
    timeline.checks = {"ok": True, "duration_s": 42.5, "at": time.time()}
    page = timeline.render()

    progress = data_of(page)["progress"]
    assert [(c["id"], c["status"]) for c in progress["criteria"]] == [
        ("C1", "PASS"),
        ("C2", "PASS"),
        ("C3", ""),
    ]
    assert "2 of 3 criteria passing" in page
    assert "unverified" in page
    assert "pinned contract" in page
    assert "latest checks:" in page and "42s" in page


def test_review_section_has_chart_numbers_and_open_findings(monkeypatch):
    monkeypatch.setattr(_session_config, "_round_scores", [7, 4, 2])
    monkeypatch.setattr(_session_config, "_rulings", ["KEEP -- fine (conflict: x)"])
    timeline = timeline_for(
        converging=True, open_findings=["Race in the writer", "Missing test for empty input"]
    )
    timeline.phase("review", round_=3)
    page = timeline.render()

    assert "<svg" in page and page.count('class="bar"') == 3
    assert "R1 7 → R2 4 → R3 2" in page
    assert "planner: converging" in page
    assert "arbiter rulings: 1" in page
    assert "Race in the writer" in page
    review = data_of(page)["review"]
    assert review["scores"] == [7, 4, 2] and review["converging"] is True and review["rulings"] == 1


def test_finding_titles_are_taken_from_blocking_section_only():
    report = (
        "### correctness reviewer\n\n## Findings\n\n1. **Race in the writer** -- two threads\n"
        "2. Missing test for empty input\n\nBLOCKING: 2\n\nNON-BLOCKING:\n- 1. style nit\n\nVERDICT: FAIL"
    )
    assert _dashboard.finding_titles(report) == [
        "Race in the writer -- two threads",
        "Missing test for empty input",
    ]


def test_working_on_now_lists_delegations_and_the_last_five(tmp_path):
    delegations = Delegations(
        tmp_path / "b",
        tmp_path,
        cast(Worker, None),
        lambda: "codex",
        lambda *a: None,
        setting=lambda e: f"{e} gpt/high",
    )
    now = time.time()
    delegations.active["delegate-1"] = {
        "id": "delegate-1",
        "engine": "codex",
        "setting": "codex gpt/high",
        "started_at": now - 300,
    }
    delegations.recent = [
        {
            "id": f"delegate-d{i}",
            "engine": "claude",
            "setting": "claude sonnet/default",
            "ok": i % 2 == 0,
            "started_at": now - 900,
            "ended_at": now - 60 * i,
            "elapsed_s": 30,
        }
        for i in range(5)
    ]
    timeline = timeline_for(delegations=delegations, plan_heading="Fix the writer race")
    timeline.row("📝", "starting step 3: parser")
    page = timeline.render()

    assert "delegations in flight: 1" in page
    assert "delegate-1" in page and "codex gpt/high" in page and "5m" in page
    assert page.partition("<script")[0].count("delegate-d") == 5
    assert "plan: Fix the writer race" in page
    assert "last note: starting step 3: parser" in page


def test_cost_and_limits_show_spend_meters_and_fallbacks(monkeypatch):
    monkeypatch.setattr(
        _session_config,
        "_usage",
        {
            "contract@0": {
                "input_tokens": 1e6,
                "output_tokens": 2e6,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cost_usd": 1.5,
                "engines": ["claude"],
            },
            "correctness@1": {
                "input_tokens": 1e6,
                "output_tokens": 0,
                "cache_read_input_tokens": 4e6,
                "cache_creation_input_tokens": 0,
                "cost_usd": 2.0,
                "engines": ["codex"],
            },
        },
    )
    timeline = timeline_for(
        author_usage={
            "input_tokens": 1e6,
            "output_tokens": 1e6,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    )
    timeline.meter(_meter.Meter("claude a", "oauth-usage", time.time(), five_hour_pct=42.0, weekly_pct=10.0))
    timeline.row("🔀", "judge: claude is at its limit; using codex")
    page = timeline.render()

    cost = data_of(page)["cost"]
    assert cost["subagents"]["usd"] == 3.5
    assert cost["by_role"] == {"contract": 1.5, "judge-correctness": 2.0}
    assert cost["by_engine"] == {"claude": 1.5, "codex": 2.0}
    assert cost["coordinator"]["usd_estimate"] == pytest.approx(18.0)  # sonnet: $3 in + $15 out per M
    assert "$3.50" in page
    assert "claude a: 5h 42% (resets ?) · week 10%" in page
    assert cost["fallbacks"] == ["judge: claude is at its limit; using codex"]
    assert data_of(page)["cost"]["meters"][0]["account"] == "claude a"


def test_deliverables_pick_up_commit_and_pr_from_notes():
    timeline = timeline_for(
        repo="https://github.com/acme/widgets", branch="feat", commit="abc1234", unpushed=0
    )
    timeline.row("📝", "opened https://github.com/acme/widgets/pull/12 for review")
    timeline.row("📝", "demo video: https://example.org/demo.mp4.")
    page = timeline.render()
    d = data_of(page)["deliverables"]
    assert d["pr"] == "https://github.com/acme/widgets/pull/12"
    assert d["links"][-1]["url"] == "https://example.org/demo.mp4"
    assert all(link["url"] != d["pr"] for link in d["links"])
    assert "abc1234" in page and "(pushed)" in page
    assert 'href="https://github.com/acme/widgets"' in page


def test_a_javascript_url_in_a_note_is_not_linked():
    timeline = timeline_for()
    timeline.row("📝", "see javascript:alert(1) and https://ok.example/x")
    assert "javascript:" not in timeline.render().replace("see javascript:alert(1)", "")


def test_risks_collect_what_went_wrong():
    timeline = timeline_for(attempt=2)
    timeline.row("🚧", "DONE bounced before review (1): tests red")
    timeline.row("🔀", "judge: claude is at its limit; using codex")
    timeline.row("🚨", "something broke")
    timeline.row("⚠️", "careful")
    timeline.activity("idle for 31m -- send direction, or `/stop` to finish")
    risks = data_of(timeline.render())["risks"]
    text = " | ".join(risks)
    for needle in ("restarted 2", "1 error row", "1 DONE bounced", "1 fallback", "1 warning", "idle for 31m"):
        assert needle in text


def test_routine_rows_move_to_a_second_collapsed_block():
    timeline = timeline_for()
    timeline.row("💾", "checkpointed")
    timeline.row("🔑", "login refreshed")
    timeline.row("✅", "child actions visible again")
    timeline.row("📝", "a real note")
    page = timeline.render()
    main, _, routine = page.partition("Routine rows")
    assert "a real note" in main and "checkpointed" not in main
    assert "checkpointed" in routine and "login refreshed" in routine
    assert page.count("<tr><td><code>") == 4


def test_json_blob_parses_and_matches_the_page():
    timeline = timeline_for(bridge=_bridge_with(_question(text="a </script><b> question")))
    timeline.phase("review", round_=2)
    page = timeline.render()
    found = re.search(r'<script type="application/json" id="factory-status">(.*?)</script>', page, re.DOTALL)
    assert found is not None
    raw = found.group(1)
    assert "<" not in raw
    data = json.loads(raw)
    assert data == _dashboard.from_report(page)
    assert data["schema"] == 1
    assert data["phase"]["kind"] == "waiting"
    assert data["needs_you"][0]["text"] == "a </script><b> question"
    assert page.count("<script") == 1


def test_report_is_deterministic_between_renders():
    timeline = timeline_for()
    timeline.phase("building")
    assert timeline.render() == timeline.render()


def test_size_is_bounded_with_many_rounds_rows_and_findings(monkeypatch):
    monkeypatch.setattr(_session_config, "_round_scores", list(range(1000, 0, -1)))
    timeline = _timeline._Timeline(keep=5000)
    timeline.header("project", "big")
    timeline.open_findings = ["x" * 100] * 50
    for n in range(5000):
        timeline.row("📝" if n % 2 else "💾", f"row {n} " + "y" * 300)
    page = timeline.render()
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    data = data_of(page)
    assert len(data["review"]["scores"]) == 1000
    assert page.count('class="bar"') == _dashboard.CHART_ROUNDS
    # The newest rows survive the trim.
    assert "row 4999" in page


def test_a_broken_dashboard_still_renders_the_rows(monkeypatch, capsys):
    timeline = timeline_for()
    timeline.row("📝", "keep me")
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: 1 / 0)
    page = timeline.render()
    assert "keep me" in page
    assert "[report] dashboard failed" in capsys.readouterr().err


def test_dark_mode_and_no_external_assets():
    page = timeline_for().render()
    assert "prefers-color-scheme:dark" in page
    assert not re.search(r'(?:src|href)="https?://', page.replace("https://console.example", ""))
    assert "@import" not in page and "<link" not in page


# --- readers that predate the dashboard -------------------------------------


def test_meter_comments_still_parse():
    timeline = timeline_for()
    timeline.meter(_meter.Meter("claude a", "oauth-usage", 1000.0, five_hour_pct=42.0, weekly_pct=10.0))
    timeline.meter(_meter.Meter("codex", "app-server", 1000.0, five_hour_pct=7.0))
    found = {m.engine: m for m in _meter.from_report(timeline.render())}
    assert found["claude"].five_hour_pct == 42.0 and found["codex"].five_hour_pct == 7.0


def _watch():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("ops_watch", Path(__file__).parents[1] / "ops" / "watch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watcher_silence_detection_ignores_the_dashboard_chrome():
    watch = _watch()
    timeline = timeline_for()
    timeline.row("📝", "first real row")
    before = watch.report_activity(timeline.render())

    # Time passing, a checkpoint, and a phase age change nothing the watcher hashes...
    timeline._phase["since"] -= 3600
    timeline.row("💾", "checkpointed")
    timeline.row("🔑", "login refreshed")
    timeline.activity("idle for 31m")
    assert watch.report_activity(timeline.render()) == before

    # ...while a real row does.
    timeline.row("📝", "second real row")
    assert watch.report_activity(timeline.render()) != before


def test_legacy_report_without_a_blob_is_not_a_dashboard():
    assert _dashboard.from_report("<h3>old</h3><table></table>") is None
    assert (
        _dashboard.from_report('<script type="application/json" id="factory-status">{nope</script>') is None
    )


@pytest.mark.asyncio
async def test_bridge_tracks_open_questions_until_answered(bridge_root, monkeypatch):
    from flyte_agent_factory import _bridge

    gate = asyncio.Event()

    class Cond:
        name = "ask-a0-001"

        class wait:
            @staticmethod
            async def aio():
                await gate.wait()
                return "yes"

    async def fake_open(base, seq, **kw):
        return Cond(), seq

    monkeypatch.setattr(_bridge, "open_condition", fake_open)
    bridge = Bridge(bridge_root)
    task = asyncio.create_task(
        bridge._service(
            {"id": "r1", "question": "Port? Default: 80", "timeout_s": 600, "created_at": time.time()}, 1
        )
    )
    await wait_for(lambda: "r1" in bridge.questions)
    open_ = bridge.questions["r1"]
    assert open_["condition"] == "ask-a0-001" and open_["default"] == "Default: 80" and not open_["timed_out"]
    gate.set()
    await task
    assert bridge.questions == {}


# The name predates inbox 009's close-at-deadline rule: the question is now closed, not kept.
@pytest.mark.asyncio
async def test_bridge_keeps_a_timed_out_question_until_the_late_answer(bridge_root, monkeypatch):
    # Closed rather than kept: a question past its deadline no longer shows as open, and nothing
    # keeps listening for a late answer (the close names why on the condition).
    from flyte_agent_factory import _bridge

    closed = []
    gate = asyncio.Event()

    class Cond:
        name = "ask-a0-001"

        class wait:
            @staticmethod
            async def aio():
                await gate.wait()

    async def fake_open(base, seq, **kw):
        return Cond(), seq

    async def fake_abandon(self, name, reason, *, approve):
        closed.append((name, reason))

    monkeypatch.setattr(_bridge, "open_condition", fake_open)
    monkeypatch.setattr(_bridge.Bridge, "_abandon", fake_abandon)
    monkeypatch.setattr(_bridge, "DEFAULT_ASK_TIMEOUT_S", 0.0)
    bridge = Bridge(bridge_root)
    real_wait_for = asyncio.wait_for

    async def quick(coro, timeout):
        return await real_wait_for(coro, 0.05)

    monkeypatch.setattr(asyncio, "wait_for", quick)
    await bridge._service({"id": "r2", "question": "Anything?", "created_at": time.time()}, 1)
    assert bridge.questions == {}
    assert closed == [("ask-a0-001", "closed: timed out, no default stated")]


@pytest.mark.asyncio
async def test_delegations_report_active_and_recent_jobs(tmp_path, monkeypatch):
    root = tmp_path / "bridge"
    from flyte_agent_factory._bridge import ensure_dirs

    ensure_dirs(root)
    release = asyncio.Event()

    async def worker(target, brief, engine, answer_only=False, on_engine=None):
        await release.wait()
        return "summary"

    monkeypatch.setattr(delegation, "snapshot", lambda workspace, target: "base")
    delegations = Delegations(
        root, tmp_path, worker, lambda: "claude", lambda *a: None, setting=lambda e: f"{e} sonnet/default"
    )
    task = asyncio.create_task(
        delegations.service({"id": "delegate-9", "brief": "research", "answer_only": True})
    )
    await wait_for(lambda: "delegate-9" in delegations.active)
    assert delegations.active["delegate-9"]["setting"] == "claude sonnet/default"
    release.set()
    await task
    assert delegations.active == {}
    assert delegations.recent[-1]["id"] == "delegate-9" and delegations.recent[-1]["ok"] is True


# --- ops/operate.py overview ------------------------------------------------


def _operate():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "operate_overview", Path(__file__).parents[1] / "ops" / "operate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_run(operate, name, slot, phase, page):
    return SimpleNamespace(
        name=name,
        phase=f"ActionPhase.{phase}",
        url=f"https://console.example/runs/{name}",
        get_report=lambda: page,
        to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": slot}}}}},
    )


def test_overview_reads_the_json_blob_and_falls_back_for_older_reports(monkeypatch):
    operate = _operate()
    slot_a, slot_b = operate.SLOTS[0], operate.SLOTS[1]
    monkeypatch.setattr(_session_config, "_round_scores", [5, 3])

    waiting = timeline_for(bridge=_bridge_with(_question()))
    waiting.phase("review", round_=2)
    waiting.proc = SimpleNamespace(last_output=time.monotonic() - 120)
    waiting.meter(
        _meter.Meter("claude work", "oauth-usage", time.time(), five_hour_pct=61.0, weekly_pct=20.0)
    )
    waiting.author_usage = {"input_tokens": 1e6, "output_tokens": 1e6}

    old = (
        _timeline._Timeline()
        ._render_rows()
        .replace(
            "<h3>Flyte Agent Factory</h3>",
            "<h3>Flyte Agent Factory</h3><table><tr><td><b>project</b></td><td>oldie</td></tr></table>"
            "<p><b>Status:</b> idle for 40m</p>"
            + _meter.report_comment(
                _meter.Meter("claude other", "oauth-usage", time.time(), five_hour_pct=9.0)
            ),
        )
    )
    ended = timeline_for()
    ended.phase("done")
    runs = [
        _fake_run(operate, "run-new", slot_a, "RUNNING", waiting.render()),
        _fake_run(operate, "run-old", slot_b, "RUNNING", old),
        _fake_run(operate, "run-end", slot_b, "SUCCEEDED", ended.render()),
    ]
    monkeypatch.setattr(operate, "_open_questions", lambda run: 2)

    view = operate.build_overview(runs)
    new, legacy, done = view["rows"]

    assert (new["project"], new["slot"], new["phase"], new["round"]) == (
        "wordstats",
        slot_a,
        "waiting on human",
        2,
    )
    assert new["needs_you"] == 1 and new["last_activity"] == "2m ago" and new["spend_usd"] == 18.0
    assert (legacy["project"], legacy["phase"], legacy["needs_you"]) == ("oldie", "idle for 40m", 2)
    assert "older report" in legacy["note"]
    assert done["phase"] == "succeeded"
    assert "claude work: 5h 61%" in operate.overview_text(view)
    assert view["slots"][slot_a] and view["slots"][slot_b][0]["account"] == "claude other"

    out = operate.overview_text(view)
    assert "NEEDS YOU x1" in out and "run-new" in out


def test_overview_command_writes_text_and_html(monkeypatch, tmp_path, capsys):
    operate = _operate()
    run = _fake_run(operate, "run-x", operate.SLOTS[0], "RUNNING", timeline_for().render())
    monkeypatch.setattr(operate._factory, "recent_runs", lambda: [run])
    target = tmp_path / "o.html"
    operate.overview(SimpleNamespace(html=str(target)))

    assert "run-x" in capsys.readouterr().out
    page = target.read_text()
    assert "<table>" in page and 'href="https://console.example/runs/run-x"' in page
    assert operate.SLOTS[1] in page


def test_overview_survives_an_unreadable_report(monkeypatch):
    operate = _operate()
    run = _fake_run(operate, "run-x", operate.SLOTS[0], "RUNNING", "")
    run.get_report = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    row = operate.build_overview([run])["rows"][0]
    assert row["note"] == "report unavailable: RuntimeError" and row["name"] == "run-x"


def test_overview_is_a_subcommand(monkeypatch, capsys):
    operate = _operate()
    monkeypatch.setattr(operate, "_init", lambda: None)
    monkeypatch.setattr(operate._factory, "recent_runs", list)
    operate.main(["overview"])
    assert "no live or recent projects" in capsys.readouterr().out


# --- the supervisor feeds the dashboard ----------------------------------------


@pytest.mark.asyncio
async def test_declared_checks_record_their_result_and_phase(tmp_path):
    checks = tmp_path / _session_config._checks_rel(_session_config._project_name)
    checks.parent.mkdir(parents=True, exist_ok=True)
    checks.write_text("exit 0")
    timeline = _timeline._Timeline()
    await _supervise_support._run_declared_checks(tmp_path, timeline)
    assert timeline.checks is not None
    assert timeline.checks["ok"] is True and timeline.checks["duration_s"] >= 0
    assert timeline._phase["kind"] == "building"

    checks.write_text("exit 3")
    await _supervise_support._run_declared_checks(tmp_path, timeline)
    assert timeline.checks is not None
    assert timeline.checks["ok"] is False


@pytest.mark.asyncio
async def test_the_pump_marks_the_project_as_building(monkeypatch):
    async def no_report(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(flyte.report, "replace", type("R", (), {"aio": staticmethod(no_report)}))
    monkeypatch.setattr(_supervise_support, "_in_flight_children", lambda: (0, ""))

    class Proc:
        tail = ()
        menu = None
        login_expired = False
        last_output = None
        limit_hit_at = None
        limit_text = ""
        last_submission = None

        async def submit(self, text, **kwargs):
            pass

    class Quiet:
        def drain_notes(self):
            return []

        def stop_requested(self):
            return None

    timeline = _timeline._Timeline()
    stop = asyncio.Event()
    pump = asyncio.create_task(
        _supervise._pump(cast(Bridge, Quiet()), cast(ClaudeProcess, Proc()), timeline, stop)
    )
    await wait_for(lambda: timeline._phase["kind"] == "building")
    stop.set()
    await asyncio.wait_for(pump, timeout=30)
    assert timeline._phase["kind"] == "building"


@pytest.mark.real_planner
def test_the_planner_hook_records_the_verdict_and_plan_heading(monkeypatch, tmp_path):
    async def planner(**kwargs):
        text = "# Fix the writer race\n\n1. do it\n\nCONVERGING: YES -- findings are narrowing"
        return text, None

    monkeypatch.setattr(_reviewer, "_call_reviewer", planner)
    timeline = _timeline._Timeline()
    plan, converging, _ = asyncio.run(
        _contract._plan_next_round(tmp_path, "brief", "", "findings", 2, timeline)
    )
    assert converging is True
    assert timeline.converging is True
    assert timeline.plan_heading == "Fix the writer race"
    assert plan.startswith("# Fix the writer race")


def test_huge_dashboard_is_bounded():
    import signal

    def expired(*_):
        raise TimeoutError("dashboard render stalled")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(5)
    try:
        for count in (1, 100):
            data = timeline_for().snapshot()
            huge = "<div>&amp;</div>\n" * 100_000
            data["needs_you"] = [{"text": huge}] * count
            data["review"]["open"] = [huge] * count
            data["cost"]["fallbacks"] = [huge] * count
            data["working"]["note"] = huge
            page = _dashboard.render(
                data,
                header_html="",
                meter_rows="",
                activity=huge,
                rows=[(1, "i", huge)],
                routine=[],
                now=time.time(),
            )
            assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
            assert "…" in page
            assert _dashboard.from_report(page) is not None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_criteria_strip_emphasis():
    assert _dashboard.parse_criteria("1. **Works** with _care_.")[0]["text"] == "Works with care."


# The layout of a real factory-improvement ACCEPTANCE.md: `## ` sections, list items whose ids
# are letter-prefixed or dotted, and continuation lines indented by two spaces.
LETTERED_ACCEPTANCE = """# Acceptance: factory-improvement (items 1-4 gate DONE; 5-9 are follow-on direction)

Each item: own branch `claude/<slug>` from origin/main, failing tests first, full suite + ruff
check + ruff format --check green, >=2 independent `qa` agents on different aspects passing,
`--no-ff` merge to main, pushed (`git rev-list --count origin/main..main` == 0), deployed.

## Global
- G1 `PYTHONPATH=src uv run python -m pytest tests -q` passes on main after every merge, run in
  this pod (suite is hermetic against pod env such as `AGENT_FACTORY_CODEX_SECRET`).
- G2 `uv run --with ruff ruff check src tests ops` and `ruff format --check src tests ops` pass.

## 1. Auto-deploy on merge
- 1.1 A `factory-deployer` TaskEnvironment with task `deploy_if_changed` exists in the library
  (not ops/), with a Flyte `FixedRate` 5-minute trigger, `interruptible=True`.
- 1.10 Each deploy writes a report/timeline line with the SHA and what was deployed, prefixed
  `[deploy] auto`.

## 3. PR-A: runs end in a pull request
- 3.10a The PR body links the run report.

## 5. Follow-on
- P0.2 The poll survives a restart.
- R1 Review rounds are counted.
- T4 Tests pin the engine label.
- M1 The meter shows cost per hour.
- D1 The demo folder has a README.
"""


def test_criteria_with_lettered_and_dotted_ids():
    criteria = _dashboard.parse_criteria(LETTERED_ACCEPTANCE)
    assert [c["id"] for c in criteria] == ["G1", "G2", "1.1", "1.10", "3.10a", "P0.2", "R1", "T4", "M1", "D1"]
    assert criteria[2]["text"].startswith("A factory-deployer TaskEnvironment")


def test_a_long_criteria_file_is_counted_whole():
    text = "\n".join(
        f"- {section}.{n} Criterion {section}.{n}" for section in range(1, 8) for n in range(1, 11)
    )
    assert len(_dashboard.parse_criteria(text)) == 70


def test_criteria_keep_the_numbered_formats():
    text = "1. One\n2) Two\n- C3. Three\n**C4.** Four\n5: Five\n## 6. A heading\n7 plain prose\n"
    assert [c["id"] for c in _dashboard.parse_criteria(text)] == ["C1", "C2", "C3", "C4", "C5"]


def test_criterion_notes_for_lettered_and_dotted_ids():
    results = _dashboard.criterion_results(
        ["G1 PASS, 1.1: FAIL, 3.10a PASS; P0.2 - pass, c5 pass", "1.1 PASS now; round 3 PASS; v1.2 FAIL"]
    )
    assert results == {"G1": "PASS", "1.1": "PASS", "3.10a": "PASS", "P0.2": "PASS", "C5": "PASS"}


def with_state_uri(monkeypatch, uri: str) -> None:
    assert _session_config._PROFILE is not None
    profile = dataclasses.replace(_session_config._PROFILE, state=_profile.State(uri=uri))
    monkeypatch.setattr(_session_config, "_PROFILE", profile)


def test_the_queue_shows_who_is_waiting_and_since_when():
    now = time.time()
    timeline = timeline_for(
        queue=[
            {"position": 1, "name": "alpha", "since": now - 3600},
            {"position": 2, "name": "beta<script>", "since": now - 60},
        ]
    )
    page = timeline.render()

    assert "2 waiting for a slot" in page
    assert "1. alpha" in page and "waiting 1h00m" in page
    assert "beta&lt;script&gt;" in page
    data = data_of(page)
    assert data["queue"]["waiting"] == 2
    assert [e["name"] for e in data["queue"]["entries"]] == ["alpha", "beta<script>"]


def test_an_empty_queue_adds_nothing_to_the_page():
    page = timeline_for().render()

    assert "waiting for a slot" not in page
    assert data_of(page)["queue"] == {"waiting": 0, "entries": []}


@pytest.mark.asyncio
async def test_the_dashboard_loop_reads_the_queue_when_the_profile_has_a_state_uri(monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    now = time.time()
    with_state_uri(monkeypatch, "file:///tmp/state")
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "file:///tmp/state/queue")
    monkeypatch.setattr(
        _factory,
        "waiting_rows",
        lambda queue, at: [
            {"position": 1, "name": "alpha", "waiting_s": 120},
            {"name": "broken", "error": "unreadable request"},
        ],
    )
    timeline = timeline_for()

    await _supervise_support._refresh_queue(timeline)

    assert [(e["position"], e["name"]) for e in timeline.queue] == [(1, "alpha")]
    assert now - timeline.queue[0]["since"] == pytest.approx(120, abs=5)


@pytest.mark.asyncio
async def test_the_queue_is_skipped_without_a_state_uri_and_failures_are_soft(monkeypatch):
    from flyte_agent_factory import _factory, _factory_base

    def boom(*a, **k):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(_factory, "waiting_rows", boom)
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "file:///tmp/state/queue")
    timeline = timeline_for(queue=[{"position": 1, "name": "stale", "since": 0}])

    with_state_uri(monkeypatch, "")
    await _supervise_support._refresh_queue(timeline)
    assert timeline.queue == [{"position": 1, "name": "stale", "since": 0}]

    with_state_uri(monkeypatch, "file:///tmp/state")
    await _supervise_support._refresh_queue(timeline)
    assert timeline.queue == [], "an unreadable queue shows nothing rather than a stale list"


@pytest.mark.asyncio
async def test_root_report_reads_harness_criteria_and_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "BASE_DIR", tmp_path / "state")
    monkeypatch.setattr(_session_config, "_project_name", "proj")
    monkeypatch.setattr(_session_config, "_project_repo", "https://example.org/other/repo.git")
    monkeypatch.setattr(_session_config, "_acceptance_text", _LIVE_ACCEPTANCE_TEXT)
    harness = tmp_path / ".claude-flyte/proj"
    harness.mkdir(parents=True)
    (harness / "ACCEPTANCE.md").write_text("1. Harness criterion\n")
    (harness / "checks.sh").write_text("exit 0\n")
    (tmp_path / "ACCEPTANCE.md").write_text("1. Unrelated repository criterion\n")
    timeline = timeline_for()
    page = timeline.render()
    assert "Harness criterion" in page and "Unrelated repository criterion" not in page
    assert ".claude-flyte/proj/checks.sh" in page
    await _supervise_support._run_declared_checks(tmp_path, timeline)
    assert data_of(timeline.render())["progress"]["checks"]["ok"] is True


_LIVE_ACCEPTANCE_TEXT = _session_config._acceptance_text


def test_internal_runs_are_distinct_from_formal_review():
    timeline = timeline_for(internal_subagents=7, internal_qa_subagents=5)
    timeline.delegations = SimpleNamespace(active={}, recent=[], total=12)
    page = timeline.render()
    # Only sub-agents whose meta says `qa` are called QA; delegations are not.
    assert "internal runs: 19 (5 QA sub-agents, 2 other sub-agents, 12 delegations)" in page
    assert "internal QA/sub-agent runs" not in page
    assert "no formal review round yet" in page
    assert data_of(page)["review"]["internal_runs"] == {
        "subagents": 7,
        "qa_subagents": 5,
        "by_role": {},
        "delegations": 12,
    }


def test_repeated_fallbacks_are_grouped():
    timeline = timeline_for()
    for _ in range(25):
        timeline.row("🔀", "delegate: codex is at its limit; using claude")
    page = timeline.render()
    assert "25 fallback(s)" in page
    assert "25× delegate: codex is at its limit; using claude" in page


def test_coordinator_hourly_rate_uses_elapsed_run_time():
    timeline = timeline_for(author_usage={"input_tokens": 1e6})
    timeline.started_at = 1000
    data = timeline.snapshot(now=8200)
    assert data["cost"]["coordinator"]["usd_per_hour"] == 1.5
    page = _dashboard.render(data, header_html="", meter_rows="", activity="", rows=[], routine=[], now=8200)
    assert "≈$1.50/hour" in page and "≈$3.00 at list price" in page
    assert timeline.snapshot(now=1000)["cost"]["coordinator"]["usd_per_hour"] is None


def test_session_subagent_count_is_scoped_to_the_workspace(tmp_path):
    workspace = tmp_path / "work"
    project = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    for rel in ("session.jsonl", "s1/subagents/agent-a.jsonl", "s2/subagents/agent-b.jsonl"):
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    assert _supervise_support._author_subagents(tmp_path, workspace) == (2, 0)
    assert _supervise_support._author_subagents(tmp_path, tmp_path / "other") == (0, 0)


def test_session_subagents_are_qa_only_when_their_meta_says_so(tmp_path):
    workspace = tmp_path / "work"
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace)) / "s1" / "subagents"
    folder.mkdir(parents=True)
    # The meta file Claude Code writes beside each sub-agent transcript.
    metas = {
        "a": {"agentType": "qa", "description": "Second QA: PR-4 standards text", "spawnDepth": 1},
        "b": {"agentType": "qa"},
        "c": {"agentType": "dev"},
        "d": None,  # no meta at all: a sub-agent, role unknown
        "e": "not json",
    }
    for name, meta in metas.items():
        (folder / f"agent-{name}.jsonl").write_text("{}\n")
        if meta is not None:
            (folder / f"agent-{name}.meta.json").write_text(
                meta if isinstance(meta, str) else json.dumps(meta)
            )
    assert _supervise_support._author_subagents(tmp_path, workspace) == (5, 2)


def test_risks_collapse_repeated_messages():
    data = timeline_for().snapshot()
    data["risks"] = ["Codex fallback", "Codex fallback", "other risk"]
    section = _dashboard._risks(data)
    assert section.count("Codex fallback") == 1
    assert "2× Codex fallback" in section and "other risk" in section


def test_author_start_survives_a_checkpoint_restore(tmp_path):
    workspace = tmp_path / "work"
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    (folder / "session.jsonl").write_text(
        "not json\n"
        + json.dumps({"timestamp": "2026-01-01T00:00:00Z"})
        + "\n"
        + json.dumps({"timestamp": "2026-01-01T02:00:00Z"})
        + "\n"
    )
    assert _supervise_support._author_started_at(tmp_path, workspace) == 1767225600
    assert _supervise_support._author_started_at(tmp_path, tmp_path / "other") is None


def test_author_start_is_the_earliest_across_transcripts(tmp_path):
    workspace = tmp_path / "work"
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    # Sorted by name, the first transcript is not the earliest one.
    for name, stamp in (
        ("a.jsonl", "2026-01-03T00:00:00Z"),
        ("b.jsonl", "2026-01-01T00:00:00Z"),
        ("c.jsonl", "2026-01-02T00:00:00Z"),
    ):
        (folder / name).write_text(json.dumps({"timestamp": stamp}) + "\n")
    assert _supervise_support._author_started_at(tmp_path, workspace) == 1767225600


@pytest.mark.asyncio
async def test_the_dashboard_loop_counts_subagents_and_folds_in_the_earliest_start(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    project = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    (project / "s1" / "subagents").mkdir(parents=True)
    (project / "s1.jsonl").write_text(json.dumps({"timestamp": "2026-01-01T00:00:00Z"}) + "\n")
    for name, role in (("a", "qa"), ("b", "dev"), ("c", "qa")):
        (project / "s1" / "subagents" / f"agent-{name}.jsonl").write_text("{}\n")
        (project / "s1" / "subagents" / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": role}))
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_usage, "_author_usage", lambda *a: {})

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(_supervise_support, "_refresh_deliverables", nothing)
    monkeypatch.setattr(_supervise_support, "_refresh_queue", nothing)
    stop = asyncio.Event()
    timeline = timeline_for()

    async def flush(force=False):
        stop.set()

    timeline.flush = flush
    await asyncio.wait_for(_supervise_support._dashboard_loop(timeline, stop, every=0), timeout=30)
    assert (timeline.internal_subagents, timeline.internal_qa_subagents) == (3, 2)
    assert timeline.started_at == 1767225600

    # A later transcript does not move the start forward.
    (project / "s1.jsonl").write_text(json.dumps({"timestamp": "2026-06-01T00:00:00Z"}) + "\n")
    stop.clear()
    await asyncio.wait_for(_supervise_support._dashboard_loop(timeline, stop, every=0), timeout=30)
    assert timeline.started_at == 1767225600


def test_declared_checks_line_is_on_the_page(tmp_path, monkeypatch):
    data = timeline_for().snapshot()
    data["progress"]["checks"] = None
    data["progress"]["checks_script"] = ".claude-flyte/proj/checks.sh"
    section = _dashboard._progress(data)
    assert (
        '<p class="mute">declared checks: .claude-flyte/proj/checks.sh; no harness result yet</p>' in section
    )
    data["progress"]["checks_script"] = ""
    assert "checks have not run yet" in _dashboard._progress(data)


def test_fallback_grouping_ignores_changing_meter_readings():
    timeline = timeline_for()
    for percent in (96, 97):
        timeline.row("🔀", f"delegate: codex is at its limit (codex: 5h {percent}%); using claude")
    data = timeline.snapshot()
    assert data["cost"]["fallback_counts"] == {"delegate: codex is at its limit; using claude": 2}
    assert len(data["cost"]["fallbacks"]) == 2


def test_fallback_groups_stay_bounded_without_losing_total_count():
    timeline = timeline_for()
    for n in range(50):
        timeline.row("🔀", f"delegate {n}: using claude")
    data = timeline.snapshot()
    assert len(data["cost"]["fallback_counts"]) == 20
    assert any("50 fallback(s)" in risk for risk in data["risks"])


def test_snapshot_includes_report_activity_and_timeline():
    timeline = timeline_for()
    timeline.activity("reviewing the patch")
    timeline.row("🔧", "worker finished")
    data = data_of(timeline.render())
    assert data["activity"] == "reviewing the patch"
    assert data["timeline"][-1]["icon"] == "🔧"
    assert data["timeline"][-1]["text"] == "worker finished"
    assert isinstance(data["timeline"][-1]["time"], (int, float))


def test_snapshot_timeline_trims_with_the_visible_report_rows(monkeypatch):
    monkeypatch.setattr(_dashboard, "MAX_REPORT_BYTES", 30_000)
    timeline = timeline_for()
    for i in range(80):
        timeline.row("📝", f"event {i} " + "x" * 500)
    page = timeline.render()
    data = data_of(page)
    assert len(page.encode()) <= 30_000
    assert 0 < len(data["timeline"]) < 80
    assert data["timeline"][-1]["text"].startswith("event 79 ")
    assert "event 0 " not in page


def test_a_recurring_fallback_stays_visible_and_counted():
    # qa: groups were ordered by first sighting, so the most frequent, most recent reason fell off the shown
    # three and out of the 20-group cap.
    timeline = timeline_for()
    timeline.row("🔀", "delegate: codex is at its limit (codex: 5h 96%); using claude")
    for name in ("B", "C", "D"):
        timeline.row("🔀", f"planner {name}: using claude")
    for pct in range(10):
        timeline.row("🔀", f"delegate: codex is at its limit (codex: 5h {90 + pct}%); using claude")
    counts = timeline.snapshot()["cost"]["fallback_counts"]
    assert list(counts)[-1] == "delegate: codex is at its limit; using claude"
    assert counts["delegate: codex is at its limit; using claude"] == 11
    for i in range(30):
        timeline.row("🔀", f"one-off reason {i}: using claude")
        timeline.row("🔀", "delegate: codex is at its limit (codex: 5h 99%); using claude")
    assert (
        timeline.snapshot()["cost"]["fallback_counts"]["delegate: codex is at its limit; using claude"] == 41
    )


def test_open_questions_do_not_hide_a_usage_limit():
    timeline = timeline_for(bridge=_bridge_with(_question()))
    timeline.phase("limited", limited_until=time.time() + 3600)
    data = data_of(timeline.render())
    assert data["phase"]["kind"] == "limited"
    assert data["needs_you"]


def test_metrics_follow_the_real_timeline_and_pass_record(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    timeline = timeline_for()
    timeline.phase("building")
    clock[0] = 1010.0
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: (ACCEPTANCE, False))
    assert timeline.snapshot()["metrics"]["criteria_at"] == 1010.0
    clock[0] = 1020.0
    timeline.phase("checks")
    timeline.phase("checks")
    timeline.row("🚧", "DONE bounced before review (1): missing commit")
    timeline.phase("review", round_=1)
    _inbox._save_pass(1, "abc", timeline=timeline)
    metrics = data_of(timeline.render())["metrics"]
    assert metrics["schema"] == 1
    assert metrics["phase_history"] == [
        ["starting", 1000.0],
        ["building", 1000.0],
        ["checks", 1020.0],
        ["review", 1020.0],
    ]
    assert metrics["criteria_at"] == 1010.0
    assert (metrics["passed_round"], metrics["passed_at"]) == (1, 1020.0)
    assert metrics["bounces"] == {"done": 1, "preflight": 0}


def test_truncated_report_keeps_300_newest_metric_labels(monkeypatch):
    timeline = timeline_for()
    for i in range(310):
        timeline.metric_label(
            kind="judge", round=1, name="correctness", verdict="FAIL", file=f"src/{i}.py", title="<" * 500
        )
    timeline.open_findings = ["<" * 2000] * 300
    page = timeline.render()
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    metrics = data_of(page)["metrics"]
    assert len(metrics["labels"]) == 300
    assert metrics["labels"][0]["file"] == "src/10.py"
    assert all(len(row["title"]) <= 200 for row in metrics["labels"])


async def test_metrics_keep_an_answered_question(tmp_path, monkeypatch):
    from flyte_agent_factory import _bridge

    class Condition:
        name = "ask-a0-001"

        class wait:
            @staticmethod
            async def aio():
                return "8080"

    async def open_condition(*args, **kwargs):
        return Condition, 1

    monkeypatch.setattr(_bridge, "open_condition", open_condition)
    bridge = Bridge(tmp_path / "bridge")
    timeline = timeline_for(bridge=bridge)
    await bridge._service({"id": "q1", "question": "Port?", "timeout_s": 60}, 1)
    assert not bridge.questions
    asks = timeline.snapshot()["metrics"]["asks"]
    assert len(asks) == 1
    assert asks[0]["name"] == "ask-a0-001"
    assert asks[0]["blocking"] is False
    assert asks[0]["answered_at"] >= asks[0]["opened_at"]


def test_metrics_only_record_policy_knobs_when_read(monkeypatch):
    from flyte_agent_factory import _policy

    monkeypatch.setattr(_policy, "knobs_seen", {})
    monkeypatch.setenv("AGENT_FACTORY_JUDGE_MODEL", "haiku")
    monkeypatch.setenv("AGENT_FACTORY_UNUSED", "1")
    timeline = timeline_for()
    assert _policy.resolve("judge").model == "haiku"
    assert timeline.snapshot()["metrics"]["knobs"] == {"JUDGE_MODEL": "haiku", "JUDGE_EFFORT": ""}


@pytest.mark.parametrize("oversize_core", [False, True])
def test_oversized_metrics_keep_the_report_within_budget(oversize_core):
    data = timeline_for().snapshot()
    metrics = data["metrics"]
    metrics.update(
        passed_round=3,
        passed_at=1234.0,
        criteria_at=1000.0,
        rounds=[{"round": 3, "blocking": 0}],
        labels=[{"title": "x" * 2048} for _ in range(300)],
        usage={"author": {"detail": "x" * _dashboard.MAX_REPORT_BYTES}},
    )
    if oversize_core:
        metrics["phase_history"] = [["x" * _dashboard.MAX_REPORT_BYTES, 1000.0]]
    page = _dashboard.render(
        data, header_html="", meter_rows="", activity="", rows=[], routine=[], now=1234.0
    )
    assert len(page.encode()) <= _dashboard.MAX_REPORT_BYTES
    snapshot = data_of(page)
    if not oversize_core:
        core = ("schema", "passed_round", "passed_at", "phase_history", "criteria_at", "rounds", "bounces")
        for key in core:
            assert snapshot["metrics"][key] == metrics[key], key
        assert "labels" not in snapshot["metrics"]
        assert "usage" not in snapshot["metrics"]
    else:
        assert snapshot["truncated"]


# --- the report's lead: goal, now, evidence, verdict ---------------------------------------------


def _lead_of(timeline, activity="running the checks"):
    page = _dashboard.render(
        timeline.snapshot(now=8200),
        header_html="",
        meter_rows="",
        activity=activity,
        rows=[],
        routine=[],
        now=8200,
    )
    return page, data_of(page)


def _with_criteria(monkeypatch, notes):
    monkeypatch.setattr(_session_config, "_acceptance_text", lambda: (ACCEPTANCE, False))
    return timeline_for(notes=notes)


def test_the_lead_comes_before_the_status_section(monkeypatch, tmp_path):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    (tmp_path / ".claude-flyte").mkdir()
    (tmp_path / ".claude-flyte/BRIEF.md").write_text("\n# Does caching help the replay?\n\nLong text.\n")
    timeline = _with_criteria(monkeypatch, ["C1 PASS", "C2 FAIL"])
    page, data = _lead_of(timeline)
    assert data["goal"] == "Does caching help the replay?"
    lead = page[: page.index("<h4>")]
    assert "<b>Goal:</b> Does caching help the replay?" in lead
    assert "<b>Now:</b> running the checks" in lead
    assert (
        "<b>Evidence so far:</b> 1 of 3 criteria pass, 1 failing, 1 not reported yet; no review yet" in lead
    )
    assert "<b>Verdict:</b> unclear" in lead
    assert page.index("<b>Goal:</b>") < page.index("<b>Now:</b>") < page.index("<b>Evidence")
    assert page.index("<b>Verdict:</b>") < page.index("<h4>")


def test_goal_falls_back_to_the_first_line_and_is_empty_without_a_brief(monkeypatch, tmp_path):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    assert timeline_for().snapshot(now=8200)["goal"] == ""
    (tmp_path / ".claude-flyte").mkdir()
    (tmp_path / ".claude-flyte/BRIEF.md").write_text("Build a word counter.\n\nMore.\n")
    assert timeline_for().snapshot(now=8200)["goal"] == "Build a word counter."


@pytest.mark.parametrize(
    ("notes", "setup", "word", "reason"),
    [
        (["C1 PASS", "C2 PASS", "C3 PASS"], lambda t: None, "good", "all 3 criteria pass"),
        (["C1 PASS"], lambda t: setattr(t, "review_passed", True), "good", "the review passed"),
        (["C1 PASS", "C2 FAIL"], lambda t: None, "unclear", "no review round has judged them yet"),
        (
            ["C1 PASS", "C2 FAIL"],
            lambda t: t.phase("review", round_=1),
            "bad",
            "criterion C2 still fails after review round 1",
        ),
        (["C1 PASS"], lambda t: t.phase("failed"), "bad", "the run failed"),
        ([], lambda t: None, "unclear", "no result reported yet"),
    ],
)
def test_each_verdict_case(monkeypatch, notes, setup, word, reason):
    timeline = _with_criteria(monkeypatch, notes)
    setup(timeline)
    page, data = _lead_of(timeline)
    assert f"<b>Verdict:</b> {word}: {reason}" in page
    assert _dashboard.verdict(data) == {"word": word, "reason": reason}


def test_evidence_names_the_latest_review_verdict(monkeypatch):
    timeline = _with_criteria(monkeypatch, ["C1 PASS", "C2 FAIL"])
    timeline.phase("review", round_=2)
    timeline.verdicts = [{"round": 2, "verdict": "FAIL", "blocking": 3, "at": 1.0}]
    page, _ = _lead_of(timeline)
    assert "review round 2 FAIL (3 blocking)" in page


def test_session_subagents_are_counted_per_role_with_aliases_mapped(tmp_path):
    workspace = tmp_path / "work"
    folder = tmp_path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace)) / "s1" / "subagents"
    folder.mkdir(parents=True)
    types = {
        "a": "qa",
        "b": "verify",
        "c": "dev",
        "d": "build",
        "e": "product",
        "f": "discover",
        "g": "Explore",
    }
    for name, agent_type in types.items():
        (folder / f"agent-{name}.jsonl").write_text("{}\n")
        (folder / f"agent-{name}.meta.json").write_text(json.dumps({"agentType": agent_type}))
    (folder / "agent-h.jsonl").write_text("{}\n")  # no meta file
    (folder / "agent-i.jsonl").write_text("{}\n")
    (folder / "agent-i.meta.json").write_text(json.dumps({"description": "no agentType"}))
    assert _supervise_support._author_subagent_roles(tmp_path, workspace) == {
        "verify": 2,
        "build": 2,
        "scope": 1,
        "discover": 1,
        "Explore": 1,
        "unknown": 2,
    }
    # The two-number fields older readers use: qa_subagents is verify + qa.
    assert _supervise_support._author_subagents(tmp_path, workspace) == (9, 2)


def test_the_report_shows_each_role_count_and_the_snapshot_carries_them():
    timeline = timeline_for(internal_subagents=7, internal_qa_subagents=3)
    timeline.internal_by_role = {"verify": 3, "build": 3, "scope": 1}
    timeline.delegations = SimpleNamespace(active={}, recent=[], total=2)
    page = timeline.render()
    assert "internal runs: 9 (3 build, 3 verify, 1 scope, 2 delegations)" in page
    runs = data_of(page)["review"]["internal_runs"]
    assert runs == {
        "subagents": 7,
        "qa_subagents": 3,
        "by_role": {"verify": 3, "build": 3, "scope": 1},
        "delegations": 2,
    }


def test_old_cost_by_role_keys_are_shown_under_the_new_names():
    data = timeline_for().snapshot()
    data["cost"].update(
        subagents={"tokens": {}, "usd": 6.0},
        by_role={
            "correctness": 1.0,
            "judge-correctness": 1.0,
            "planner": 2.0,
            "arbiter": 1.0,
            "contract": 1.0,
        },
    )
    page = _dashboard.render(data, header_html="", meter_rows="", activity="", rows=[], routine=[], now=0)
    assert "judge-correctness $2.00; plan $2.00; arbitrate $1.00; contract $1.00" in page
    assert "planner $" not in page and "arbiter $" not in page


def test_verdict_rows_parse_under_both_the_new_and_the_old_judge_names():
    rows = [
        {"icon": "🔍", "text": "QA round 2: x"},
        {"icon": "✅", "text": "judge-correctness: PASS"},
        {"icon": "❌", "text": "craft: FAIL"},
        {"icon": "⚠️", "text": "judge-completeness: no verdict, treating as FAIL"},
    ]
    assert _dashboard.latest_verdicts(rows) == (
        2,
        {"correctness": "PASS", "craft": "FAIL", "completeness": "FAIL"},
    )


# --- decisions the factory made in the owner's place ---------------------------------------------------


def _decided_bridge(*decisions):
    bridge = _bridge_with()
    bridge.decisions = list(decisions)
    return bridge


def _decision(n=1, **kw):
    return {
        "id": f"decision-{n:03d}",
        "at": 1_700_000_000.0 + n,
        "question": "Split _tools.py?",
        "decision": "Put new tools in sibling modules.",
        "reasoning": "Leaves upstream's code alone.",
        "approve": None,
        "request": "r1",
        **kw,
    }


def test_decisions_appear_in_the_snapshot_and_the_page_only_when_there_are_some():
    assert "Decided in the factory" not in timeline_for(bridge=_decided_bridge()).render()
    page = timeline_for(bridge=_decided_bridge(_decision(1), _decision(2, decision="Second."))).render()
    assert "Decided in the factory (2)" in page
    data = data_of(page)
    assert [d["id"] for d in data["decisions"]] == ["decision-001", "decision-002"]
    assert "Put new tools in sibling modules." in page and "Leaves upstream&#x27;s code alone." in page
    assert page.index("decision-002") < page.index("decision-001"), "newest first"


def test_an_overruled_decision_says_so_and_all_decision_text_is_escaped():
    decision = _decision(
        decision="<script>alert(1)</script>",
        question="a </script><b> question",
        overruled_at=1_700_000_100.0,
        overruled_by="Union: <i>no</i>",
    )
    page = timeline_for(bridge=_decided_bridge(decision)).render()
    assert "overruled: Union: &lt;i&gt;no&lt;/i&gt;" in page
    assert "<script>alert(1)</script>" not in page and "<i>no</i>" not in page
    assert data_of(page)["decisions"][0]["decision"] == "<script>alert(1)</script>"


def test_a_decision_with_a_bad_time_still_renders_with_the_others():
    page = timeline_for(bridge=_decided_bridge(_decision(1, at="soon"), _decision(2))).render()
    assert "Decided in the factory (2)" in page and "decision-001" in page and "decision-002" in page


def test_the_snapshot_bounds_the_number_and_length_of_decisions():
    many = [_decision(n, reasoning="r" * 50_000, question="q" * 50_000) for n in range(1, 41)]
    data = timeline_for(bridge=_decided_bridge(*many)).snapshot()
    assert [d["id"] for d in data["decisions"]] == [f"decision-{n:03d}" for n in range(21, 41)]
    assert all(len(d["reasoning"]) <= 800 and len(d["question"]) <= 300 for d in data["decisions"])
    assert "request" not in data["decisions"][0]


def test_the_cost_breakdown_has_all_four_groups_even_with_no_helper_subagent_or_worker_usage():
    timeline = timeline_for(
        coordinator_usage={"input_tokens": 1e6, "output_tokens": 1e6},
        author_usage={"input_tokens": 2e6, "output_tokens": 1e6},
    )
    cost = data_of(timeline.render())["cost"]
    groups = cost["breakdown"]
    assert set(groups) == {"coordinator", "helpers", "subagents", "workers"}
    assert groups["coordinator"]["usd"] == pytest.approx(18.0)  # main conversation only: $3 in + $15 out
    for name in ("helpers", "subagents", "workers"):
        assert groups[name]["usd"] == 0.0 and set(groups[name]["tokens"]) == set(
            _session_config._USAGE_FIELDS
        )
        assert not any(groups[name]["tokens"].values())
    assert cost["total_usd"] == pytest.approx(18.0)


def test_the_cost_breakdown_and_total_render_with_the_coordinator_included(monkeypatch):
    monkeypatch.setattr(
        _session_config,
        "_usage",
        {
            "correctness@1": {"output_tokens": 1e6, "cost_usd": 2.0, "engines": ["claude"]},
            "delegate@1": {"cache_read_input_tokens": 5e6, "cost_usd": 1.0, "engines": ["claude"]},
        },
    )
    timeline = timeline_for(
        coordinator_usage={"input_tokens": 1e6, "output_tokens": 1e6, "cache_read_input_tokens": 9e6},
        helper_usage={"input_tokens": 1e6},
    )
    timeline.author_usage = {"input_tokens": 2e6, "output_tokens": 1e6, "cache_read_input_tokens": 9e6}
    page = timeline.render()
    cost = data_of(page)["cost"]
    assert cost["breakdown"]["workers"]["usd"] == 1.0
    assert cost["breakdown"]["workers"]["tokens"]["cache_read_input_tokens"] == 5e6
    # the review role's $2.00; the in-pod helpers have their own line: 1M in at $3
    assert cost["breakdown"]["subagents"]["usd"] == pytest.approx(2.0)
    assert cost["breakdown"]["helpers"]["usd"] == pytest.approx(3.0)
    assert cost["breakdown"]["helpers"]["tokens"]["input_tokens"] == 1e6
    # $3 + $15 + 9M cache reads at $0.30
    assert cost["breakdown"]["coordinator"]["usd"] == pytest.approx(20.7)
    assert cost["total_usd"] == pytest.approx(20.7 + 5.0 + 1.0)
    text = re.sub(r"<[^>]+>", " ", page)
    labels = (
        "breakdown: coordinator",
        "breakdown: in-pod helpers",
        "breakdown: sub-agents",
        "breakdown: workers",
    )
    for label in (*labels, "total"):
        assert label in text
    assert "9.00M cache-read" in text and "5.00M cache-read" in text
    assert f"≈${cost['total_usd']:.2f} with the coordinator" in text


def _one_review_role(monkeypatch):
    monkeypatch.setattr(
        _session_config,
        "_usage",
        {"correctness@1": {"output_tokens": 1e6, "cost_usd": 2.0, "engines": ["claude"]}},
    )


def test_the_total_does_not_claim_the_coordinator_when_its_usage_is_unmeasured(monkeypatch):
    _one_review_role(monkeypatch)
    timeline = timeline_for()
    timeline.header("model", "codex default (default effort)")
    fields = _session_config._USAGE_FIELDS
    zeros = {"coordinator": dict.fromkeys(fields, 0.0), "helpers": dict.fromkeys(fields, 0.0)}
    timeline.set_author_usage(zeros, dict.fromkeys(fields, 0.0), measured=False)
    page = timeline.render()
    cost = data_of(page)["cost"]
    assert cost["breakdown"]["coordinator"]["measured"] is False
    assert cost["breakdown"]["coordinator"]["usd"] is None
    assert cost["total_usd"] == 2.0
    text = re.sub(r"<[^>]+>", " ", page)
    assert re.search(r"breakdown: coordinator\s+not measured", text)
    assert "without the coordinator (not measured)" in text
    assert "with the coordinator at list price" not in text
    assert "coordinator" not in cost  # the legacy row would print a misleading 0.00M out


def test_the_total_says_so_when_the_coordinator_has_no_list_price(monkeypatch):
    _one_review_role(monkeypatch)
    timeline = timeline_for(coordinator_usage={"input_tokens": 1e6})
    timeline.header("model", "fable (default effort)")
    page = timeline.render()
    cost = data_of(page)["cost"]
    assert cost["breakdown"]["coordinator"]["measured"] is True
    assert cost["breakdown"]["coordinator"]["usd"] is None
    assert cost["total_usd"] == 2.0
    text = re.sub(r"<[^>]+>", " ", page)
    assert "no list price" in text
    assert "without the coordinator (no list price)" in text
    assert "with the coordinator at list price" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("engine", "measured"), [("codex", False), ("claude", True)])
async def test_the_dashboard_loop_marks_a_codex_author_unmeasured(tmp_path, monkeypatch, engine, measured):
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path)  # no projects/ folder: no transcript
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "work")
    monkeypatch.setattr(_usage, "_author_engine", engine)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(_supervise_support, "_refresh_deliverables", nothing)
    monkeypatch.setattr(_supervise_support, "_refresh_queue", nothing)
    stop = asyncio.Event()
    timeline = timeline_for()

    async def flush(force=False):
        stop.set()

    timeline.flush = flush
    await asyncio.wait_for(_supervise_support._dashboard_loop(timeline, stop, every=0), timeout=30)
    assert timeline.coordinator_measured is measured


@pytest.mark.asyncio
@pytest.mark.parametrize(("engine", "measured"), [("codex", False), ("claude", True)])
async def test_report_usage_keeps_the_dashboard_loops_measured_flag(tmp_path, monkeypatch, engine, measured):
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path)  # no transcript: zero usage
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "work")
    monkeypatch.setattr(_usage, "_author_engine", engine)
    monkeypatch.setattr(_session_config, "_load_usage", lambda: None)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(_supervise_support, "_refresh_deliverables", nothing)
    monkeypatch.setattr(_supervise_support, "_refresh_queue", nothing)
    stop = asyncio.Event()
    timeline = timeline_for()

    async def flush(force=False):
        stop.set()

    timeline.flush = flush
    await asyncio.wait_for(_supervise_support._dashboard_loop(timeline, stop, every=0), timeout=30)
    for round_ in range(1, 4):
        _usage._report_usage(round_, timeline)
        assert timeline.coordinator_measured is measured
    if not measured:
        text = re.sub(r"<[^>]+>", " ", timeline.render())
        assert data_of(timeline.render())["cost"]["breakdown"]["coordinator"]["usd"] is None
        assert "without the coordinator (not measured)" in text


def test_report_usage_keeps_a_nonzero_codex_coordinator_measured(monkeypatch):
    monkeypatch.setattr(_usage, "_author_engine", "codex")
    monkeypatch.setattr(_session_config, "_load_usage", lambda: None)
    split = {
        "coordinator": {
            **dict.fromkeys(_session_config._USAGE_FIELDS, 0.0),
            "output_tokens": 5.0,
            "modelUsage": {},
        },
        "helpers": {**dict.fromkeys(_session_config._USAGE_FIELDS, 0.0), "modelUsage": {}},
    }
    monkeypatch.setattr(_usage, "_author_usage_split", lambda *a: split)
    timeline = timeline_for()
    _usage._report_usage(1, timeline)
    assert timeline.coordinator_measured is True
