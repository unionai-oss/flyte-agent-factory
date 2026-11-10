"""Offline experiment fixtures: remote calls must all be supplied by the test."""

import asyncio
import json
import time
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _meter, _session_config, _supervise_support, _timeline
from flyte_agent_factory._process import ClaudeProcess
from ops.model_eval.data import cost_breakdown, read_jsonl, scrub
from ops.model_eval.experiment import grade_prompt, ladder, replay, settings
from ops.model_eval.remote import collect_run


def test_costs_use_latest_cumulative_row_not_sum():
    older = (
        "💰 run so far, sub-agents: qa 1.00M tok $2.00 | author session: 0.10M out, 0.20M in, 1.00M "
        "cache-write, 10.0M cache-read"
    )
    latest = (
        "💰 run so far, sub-agents: qa 2.00M tok $4.00; qa+codex 1.00M tok $1.00 | author session: 0.20M "
        "out, 0.30M in, 2.00M cache-write, 20.0M cache-read"
    )
    report = f"<table><tr><td>{latest}</td></tr><tr><td>{older}</td></tr></table>"
    result = cost_breakdown(report, "run")
    assert result["coordinator"]["cache_read_input_tokens"] == 20e6
    assert result["coordinator_cost_usd"] == pytest.approx(17.4)
    assert result["roles"]["qa"]["cli_cost_usd"] == 4
    assert result["roles"]["qa"]["cost_usd"] is None
    assert result["roles"]["qa+codex"]["cli_cost_usd"] == 1
    assert result["fallback_cost_usd"] is None
    assert result["roles"]["qa"]["engine"] is None
    assert cost_breakdown("unavailable", "run")["coordinator"] is None


async def test_collect_case_from_fake_run(tmp_path):
    inputs = {
        "brief": "You are the **planner** between review rounds. sk-secret123456789012345",
        "workdir": SimpleNamespace(path="s3://bucket/work"),
        "role": "qa",
        "engine": "claude",
        "model": "fable",
        "effort": "medium",
        "max_turns": 80,
        "answer_only": True,
    }

    class Details:
        async def inputs(self):
            return inputs

        async def outputs(self):
            return ('plan\n<!--factory-usage {"output_tokens": 10}-->', SimpleNamespace(path="s3://out"))

    async def parent_inputs():
        return {"name": "myproj"}

    async def get(**kwargs):
        if kwargs == {"run_name": "run", "name": "a0"}:
            return SimpleNamespace(inputs=lambda: parent_inputs())
        assert kwargs == {"run_name": "run", "name": "action"}
        return Details()

    remote = SimpleNamespace(  # a fake of the `flyte.remote` module
        Run=SimpleNamespace(
            get=lambda name: SimpleNamespace(get_report=lambda: "🔧 planner: claude fable/medium")
        ),
        Action=SimpleNamespace(
            listall=lambda **kwargs: [
                SimpleNamespace(name="action", task_name="x.subagent"),
                SimpleNamespace(name="other", task_name="x.project"),
            ]
        ),
        ActionDetails=SimpleNamespace(get=SimpleNamespace(aio=get)),
    )
    paths = await collect_run("run", tmp_path, cast(ModuleType, remote))
    assert len(paths) == 1
    case = json.loads(paths[0].read_text())
    assert case["inputs"]["workdir"] == "s3://bucket/work"
    assert case["purpose"] == "planner"
    assert case["project_name"] == "myproj"
    assert case["output"] == "plan"
    assert case["usage"]["output_tokens"] == 10
    assert "sk-secret" not in paths[0].read_text()
    assert case["ground_truth"]["later_disposition"] is None


class Backend:
    def __init__(self, percentages):
        self.percentages = iter(percentages)
        self.calls = []

    def live(self):
        return []

    async def meters(self, engine):
        return [_meter.Meter(engine, "fake", time.time(), next(self.percentages), weekly_pct=1)]

    async def launch(self, inputs, setting):
        self.calls.append((inputs, setting))
        return {"output": "VERDICT: PASS", "wall_s": 1, "usage": {"output_tokens": 10, "cost_usd": 0.01}}


def case():
    return {
        "id": "run/action",
        "purpose": "reviewer",
        "inputs": {"brief": "review", "engine": "codex", "model": "", "effort": "high"},
    }


async def test_budget_stops_before_launch_and_resume_skips_done_keys(tmp_path):
    path = tmp_path / "results.jsonl"
    backend = Backend([1, 70])
    variants = [{"name": "cheap", "engine": "codex", "model": "", "effort": "low"}]
    with pytest.raises(RuntimeError, match="budget guard"):
        await replay([case()], variants, backend, path, {"concurrency": 1})
    assert len(backend.calls) == 1
    assert len(read_jsonl(path)) == 1
    resumed = Backend([1] * 3)
    await replay([case()], variants, resumed, path, {"concurrency": 1})
    assert len(resumed.calls) == 3
    assert len({(r["case"], r["setting"], r["rep"]) for r in read_jsonl(path)}) == 4
    await replay([case()], variants, Backend([]), path, {"concurrency": 1})


async def test_live_and_unknown_meter_refuse(tmp_path, monkeypatch):
    backend = Backend([1])
    monkeypatch.setattr(backend, "live", lambda: ["live"])
    with pytest.raises(RuntimeError, match="operated project"):
        await replay([case()], [], backend, tmp_path / "r", {})
    assert not backend.calls


def test_ladder_uses_visible_signal_not_correctness():
    rows = [
        {"case": "c", "setting": s, "rep": 0, "purpose": "delegate", "status": "completed", "cost_usd": cost}
        for s, cost in (("cheap", 1), ("strong", 4), ("original", 5))
    ]
    grades = [
        {
            "case": "c",
            "setting": s,
            "rep": 0,
            "status": "completed",
            "grade": {"correct": correct, "outcome": outcome, "tests_passed": tests},
            "mechanical": {"applied": True, "tests_passed": tests},
        }
        for s, correct, outcome, tests in (
            ("cheap", True, "fail", False),
            ("strong", True, "pass", True),
            ("original", True, "pass", True),
        )
    ]
    result = ladder(rows, grades, ["cheap", "strong"], "delegate")
    assert result["cost_usd"] == 5
    assert result["cases"][0]["route"] == ["cheap", "strong"]
    assert result["fixed_cost_usd"] == 5


def test_grader_is_blind_and_settings_include_original_separately():
    prompt = grade_prompt(
        {"purpose": "reviewer", "inputs": {"brief": "task"}, "ground_truth": {}},
        {"output": "answer", "setting": "SECRET-SETTING", "parameters": {"model": "opus"}},
    )
    assert "SECRET-SETTING" not in prompt
    variants = settings({}, ["gpt-small"])
    assert any(s["model"] == "gpt-small" for s in variants)
    assert not any(s["name"] == "original" for s in variants)


async def test_compact_waits_for_output_then_idle_before_delivery(monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS", "true")
    monkeypatch.setattr(_session_config, "IDLE_AFTER_S", 0.01)
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.01)
    events = []
    stop = asyncio.Event()

    class Proc:
        output_count = 0
        last_output = None

        async def submit(self, text, **kwargs):
            events.append(text)

    proc = Proc()
    task = asyncio.create_task(
        _supervise_support._compact_between_rounds(cast(ClaudeProcess, proc), _timeline._Timeline(), stop)
    )
    await asyncio.sleep(0.02)
    assert not task.done()
    proc.output_count = 1
    proc.last_output = time.monotonic()
    assert await asyncio.wait_for(task, 30)
    await proc.submit("review findings")
    assert events[0].startswith("/compact ")
    assert events[1] == "review findings"


async def test_compact_off_and_stop(monkeypatch):
    monkeypatch.delenv("AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS", raising=False)
    assert await _supervise_support._compact_between_rounds(cast(Any, None), cast(Any, None), asyncio.Event())
    monkeypatch.setenv("AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS", "1")
    proc = SimpleNamespace(output_count=0, last_output=None)

    async def submit(*args, **kwargs):
        pass

    proc.submit = submit
    stop = asyncio.Event()
    stop.set()
    assert not await _supervise_support._compact_between_rounds(
        cast(ClaudeProcess, proc), _timeline._Timeline(), stop
    )


def test_scrub_nested_secrets():
    assert scrub({"x": ["Bearer abcdefghijklmnop", "ghp_abcdefghijklmnopq"]}) == {
        "x": ["[REDACTED]", "[REDACTED]"]
    }


def test_html_status_and_delegate_round_metrics():
    report = """<p><b>Status:</b> QA round 2 reviewing (65m so far)</p><table>
    <tr><td>📉 blocking findings, round 2: 3 (rounds so far: [5, 3])</td></tr>
    <tr><td>✅ delegate delegate-001 (codex) finished in 12s (2 files)</td></tr>
    <tr><td>🔧 delegate delegate-001 started (codex)</td></tr></table>"""
    result = cost_breakdown(report, "run")
    assert result["over_60m"] == 1
    assert result["over_5m"] == 1
    assert result["blocking_by_round"] == {"2": 3}
    assert result["delegate_count"] == 1
    assert result["delegate_duration_s"] == 12


@pytest.mark.parametrize("five,weekly,age", [(None, 1, 0), (1, None, 0), (1, 80, 0), (1, 1, 301)])
async def test_missing_stale_and_weekly_meters_stop(five, weekly, age, monkeypatch):
    from ops.model_eval.experiment import budget_guard

    backend = Backend([])

    async def meters(engine):
        return [_meter.Meter(engine, "fake", time.time() - age, five, weekly_pct=weekly)]

    monkeypatch.setattr(backend, "meters", meters)
    with pytest.raises(RuntimeError, match="budget guard"):
        await budget_guard(backend, "codex", {})
    assert backend.calls == []


async def test_model_discovery_reuses_expiry_guard(monkeypatch):
    async def cannot_start(*args, **kwargs):
        pytest.fail("an unknown expiry must not start app-server")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_start)
    with pytest.raises(_meter.MeterError, match="expiry unknown"):
        await _meter.read_codex_models('{"tokens":{"access_token":"invalid"}}')


@pytest.mark.parametrize("telemetry", ["trailer", "report"])
async def test_remote_adapter_bounds_queue_and_returns_patch(monkeypatch, telemetry):
    from ops.model_eval.remote import Backend as RemoteBackend

    overrides = []
    launches = []

    class Task:
        def override(self, **kwargs):
            overrides.append(kwargs)
            return self

    class OutputDir:
        path = "s3://candidate"

        async def download(self, folder):
            from pathlib import Path

            Path(folder, "factory.patch").write_text("diff --git a/x b/x\n")

    run = SimpleNamespace(
        name="replay",
        wait=lambda **kwargs: None,
        raise_for_status=lambda: None,
        outputs=lambda: (
            'done\n<!--factory-usage {"output_tokens": 7}-->' if telemetry == "trailer" else "done",
            OutputDir(),
        ),
        get_report=lambda: (
            '<div>\n<!--factory-usage {"route": {}}-->\n<!--factory-usage {"output_tokens": 7}--></div>'
        ),
    )

    def run_task(task, **kwargs):
        launches.append(kwargs)
        return run

    import flyte

    backend = object.__new__(RemoteBackend)
    backend.flyte = cast(  # a fake of the `flyte` module
        ModuleType,
        SimpleNamespace(
            with_runcontext=lambda **kwargs: SimpleNamespace(run=run_task),
            Timeout=flyte.Timeout,
            errors=flyte.errors,
        ),
    )
    backend.remote = cast(
        ModuleType, SimpleNamespace(Task=SimpleNamespace(get=lambda *args, **kwargs: Task()))
    )  # a fake of `flyte.remote`
    backend.factory = cast(
        ModuleType, SimpleNamespace(PROJECT="p", DOMAIN="d")
    )  # a fake of the factory module
    backend.base = cast(
        ModuleType,
        SimpleNamespace(PROFILE=SimpleNamespace(slots=[SimpleNamespace(subagent_env="test-env")])),
    )  # and of `_factory_base`, where the profile lives
    backend.config = {}
    backend.allow_live = False
    result = await backend.launch(
        {"brief": "implement", "workdir": "s3://original"}, {"engine": "codex", "model": "", "effort": "low"}
    )
    assert overrides[0]["interruptible"] is True
    assert overrides[0]["timeout"].max_queued_time.total_seconds() == 300
    assert launches[0]["workdir"].path == "s3://original"
    assert launches[0]["hard_pin"] is True
    assert result["patch_file_count"] == 1
    assert result["usage"] == {"output_tokens": 7}
    assert result["output"] == "done"


async def test_blind_grades_resume_and_reject_malformed_rubric(tmp_path, monkeypatch):
    from ops.model_eval.experiment import grade

    candidate = case()
    candidate["ground_truth"] = {"verdict": "PASS"}
    candidate["reference"] = {
        "panel": [],
        "adjudication": {"confirmed_blocking": [], "disputed": [], "rejected": []},
    }
    rows = [
        {
            "case": candidate["id"],
            "setting": "secret-setting",
            "rep": 0,
            "status": "completed",
            "parameters": {"engine": "codex", "model": "", "effort": "low"},
            "output": "VERDICT: PASS",
        }
    ]
    backend = Backend([1, 1])
    assessment = {
        "outcome": "pass",
        "correct": True,
        "matched": [],
        "unmatched": [],
        "extras": [],
        "reasons": [],
        "tests_passed": None,
        "verdict_agreement": True,
    }
    seen = []

    async def launch(inputs, setting):
        seen.append(inputs["brief"])
        assert setting == {"engine": "claude", "model": "opus", "effort": "high"}
        return {"output": json.dumps(assessment), "usage": {"output_tokens": 3}}

    monkeypatch.setattr(backend, "launch", launch)
    path = tmp_path / "grades.jsonl"
    config = {"grader_bias_sample": 0}
    await grade([candidate], rows, backend, path, config)
    await grade([candidate], rows, backend, path, config)
    assert len(seen) == 1
    assert "secret-setting" not in seen[0]
    assert read_jsonl(path)[0]["status"] == "completed"
    assessment["matched"] = None
    rows[0]["rep"] = 1
    await grade([candidate], rows, backend, path, config)
    assert read_jsonl(path)[1]["status"] == "invalid"


@pytest.mark.parametrize(
    "secret",
    [
        '"refresh_token": "sensitive-value"',
        "token: sensitive-value",
        "CLAUDE_CODE_OAUTH_TOKEN=sensitive-value",
        "secret=sensitive-value",
        "-----BEGIN PRIVATE KEY-----\nsensitive-value\n-----END PRIVATE KEY-----",
        "xoxb-sensitive-value",
        "AIza" + "sensitivevalue" * 3,
    ],
)
def test_scrubber_covers_credential_formats(secret):
    assert scrub(secret) == "[REDACTED]"


def test_price_uses_engine_and_resolved_model_without_cli_fallback():
    from ops.model_eval.experiment import call_cost

    setting = {"engine": "codex", "model": ""}
    result: dict[str, Any] = {"usage": {"output_tokens": 1_000_000, "cost_usd": 0}}
    assert call_cost(result, setting, {})["cost_usd"] == 14
    assert call_cost(result, setting, {})["price_assumption"] is True
    result["usage"]["model"] = "gpt-5.4"
    assert call_cost(result, setting, {})["cost_usd"] == 15
    setting = {"engine": "claude", "model": "fable"}
    assert call_cost(result, setting, {})["cost_usd"] is None
    result["usage"].pop("model")
    assert call_cost(result, setting, {"prices": {"claude:fable": [1, 2, 0, 0]}})["cost_usd"] == 2


def test_reviewer_reference_charged_once_and_verdicts_are_anchored():
    rows = [
        {
            "case": "c",
            "setting": s,
            "rep": 0,
            "purpose": "reviewer",
            "status": "completed",
            "cost_usd": 2 if s == "original" else 1,
            "output": "VERDICT: PASS" if s == "original" else "quoted VERDICT: PASS\nVERDICT: FAIL",
        }
        for s in ("a", "b", "c", "original")
    ]
    grades = [{**r, "grade": {"correct": True}} for r in rows]
    result = ladder(rows, grades, ["a", "b", "c"], "reviewer")
    assert result["cost_usd"] == 5
    assert result["cases"][0]["route"] == ["a", "b", "c"]


def test_milliseconds_are_not_idle_minutes():
    assert cost_breakdown("waiting for poll 300ms", "run")["idle_gaps"] == []


def test_grade_prompt_removes_identity_from_all_evidence():
    config = {
        "settings": [
            {"name": "private-setting", "engine": "codex", "model": "special-model", "effort": "high"}
        ]
    }
    candidate = {
        "purpose": "reviewer",
        "inputs": {
            "brief": "review the repository",
            "engine": "codex",
            "model": "special-model",
            "effort": "high",
        },
        "ground_truth": {"parent_evidence": ["codex special-model high private-setting"]},
    }
    prompt = grade_prompt(
        candidate,
        {"output": "Codex reviewed this", "setting": "private-setting", "parameters": config["settings"][0]},
        config,
    )
    for token in ("codex", "special-model", "high", "private-setting"):
        assert token not in prompt


def test_delegate_ladder_ignores_grader_test_claim():
    rows = [
        {"case": "c", "setting": s, "rep": 0, "purpose": "delegate", "status": "completed", "cost_usd": 1}
        for s in ("a", "b", "original")
    ]
    grades = [
        {
            **r,
            "grade": {"correct": True, "tests_passed": True},
            "mechanical": {"tests_passed": r["setting"] != "a"},
        }
        for r in rows
    ]
    assert ladder(rows, grades, ["a", "b"], "delegate")["cases"][0]["route"] == ["a", "b"]


async def test_reference_panel_adjudicates_and_records_cost(tmp_path, monkeypatch):
    from ops.model_eval.experiment import reference

    candidate = {**case(), "output": "original finding", "ground_truth": {}}
    backend = Backend([1, 1, 1])

    async def launch(inputs, setting):
        backend.calls.append((inputs, setting))
        return {
            "output": json.dumps(
                {"confirmed_blocking": ["confirmed"], "disputed": ["uncertain"], "rejected": []}
            )
            if len(backend.calls) == 3
            else "finding",
            "usage": {"output_tokens": 10},
        }

    monkeypatch.setattr(backend, "launch", launch)
    await reference([candidate], backend, {})
    assert candidate["ground_truth"]["findings"] == ["confirmed"]
    assert len(candidate["reference"]["calls"]) == 3
    assert {s["engine"] for _, s in backend.calls[:2]} == {"claude", "codex"}
    await reference([candidate], backend, {})
    assert len(backend.calls) == 3


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_mechanical_harness_applies_patch_and_runs_repository_checks(tmp_path, monkeypatch, exit_code):
    import subprocess
    from pathlib import Path

    from flyte.io import Dir

    from ops.model_eval.remote import Backend as RemoteBackend

    async def download(self, folder):
        root = Path(folder)
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(root)], check=True)
        (root / "x").write_text("old\n")
        (root / "checks.sh").write_text(f"grep -qx new x || exit 2\nexit {exit_code}\n")

    monkeypatch.setattr(Dir, "download", download)
    backend = object.__new__(RemoteBackend)
    backend.config = {"local_mechanical": True}
    candidate = {"inputs": {"workdir": "s3://original"}}
    result = {"patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"}
    measured = await backend.mechanical(candidate, result)
    assert measured["applied"] is True
    assert measured["tests_passed"] is (exit_code == 0)


def test_cli_error_reports_scrubbed_message(monkeypatch, capsys):
    from ops.model_eval import __main__ as cli

    async def fail(args):
        raise ValueError("bad configuration secret=sensitive-value")

    monkeypatch.setattr(cli, "execute", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["report"])
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "ValueError: bad configuration" in message
    assert "sensitive-value" not in message


def test_unknown_cost_is_counted_and_excluded():
    from ops.model_eval.experiment import summarize

    rows = [
        {
            "case": "c",
            "setting": "s",
            "rep": 0,
            "purpose": "reviewer",
            "status": "completed",
            "cost_usd": None,
        }
    ]
    group = summarize(rows, [], [])["groups"][0]
    assert group["cost_usd"] == {"n": 0, "unknown": 1, "mean": None, "stdev": None}
    assert group["cost_per_correct"] is None


async def test_panel_settings_are_marked_and_not_graded(tmp_path):
    from ops.model_eval.experiment import grade

    candidate = case()
    parameters = {"engine": "codex", "model": "", "effort": "high"}
    candidate["reference"] = {"panel": [parameters]}
    candidate["ground_truth"] = {"findings": []}
    rows = [
        {
            "case": candidate["id"],
            "setting": "panel-setting",
            "rep": 0,
            "status": "completed",
            "parameters": parameters,
            "output": "VERDICT: PASS",
        }
    ]
    backend = Backend([])
    path = tmp_path / "grades"
    await grade([candidate], rows, backend, path, {})
    await grade([candidate], rows, backend, path, {})
    assert not backend.calls
    assert len(read_jsonl(path)) == 1
    assert read_jsonl(path)[0]["status"] == "panel_excluded"
