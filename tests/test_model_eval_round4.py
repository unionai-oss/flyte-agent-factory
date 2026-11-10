"""Round-4 QA regressions: check image, meter outages, grader spend across retries, loud empty ladders."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _meter, _session_config, _session_setup, _subagent_run, _usage
from flyte_agent_factory._images import agent_image
from ops.model_eval import mechanical
from ops.model_eval.__main__ import main, secs
from ops.model_eval.data import read_jsonl
from ops.model_eval.experiment import BudgetStop, grade, reference, summarize
from ops.model_eval.remote import Backend, check_task

PATCH = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
GOOD = json.dumps(
    {
        "outcome": "pass",
        "correct": True,
        "matched": [],
        "unmatched": [],
        "extras": [],
        "reasons": [],
        "tests_passed": None,
        "verdict_agreement": None,
    }
)
USAGE = {"input_tokens": 1_000_000, "output_tokens": 0}


class Scripted:
    """Backend whose meters and launch replies are scripted; replies may be exceptions."""

    def __init__(self, replies=(), meters_error=None):
        self.replies = iter(replies)
        self.meters_error = meters_error
        self.launches = 0

    def live(self):
        return False

    async def meters(self, engine):
        if self.meters_error:
            raise self.meters_error
        return [_meter.Meter(engine, "fake", time.time(), 1, weekly_pct=1)]

    async def mechanical(self, case, result):
        return {"applied": True, "tests_passed": True}

    async def launch(self, inputs, setting):
        self.launches += 1
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return {"output": reply, "usage": USAGE, "run": f"r{self.launches}"}


def delegate_case():
    return {"id": "c", "purpose": "delegate", "inputs": {"brief": "implement"}, "ground_truth": {}}


def candidate(case="c"):
    return {
        "case": case,
        "setting": "cheap",
        "rep": 0,
        "status": "completed",
        "parameters": {"engine": "codex", "model": "", "effort": "low"},
        "output": "summary",
        "patch": PATCH,
    }


# B1


def test_check_task_runs_in_the_agent_image_not_the_base_image(monkeypatch):
    monkeypatch.setenv("_F_IMG_ID", "ghcr.io/flyteorg/flyte:py3.12-v2.10.7")
    monkeypatch.delenv("FLYTE_INTERNAL_IMAGE", raising=False)
    task = check_task()
    assert task.image == agent_image()
    assert cast(object, task.image) != "ghcr.io/flyteorg/flyte:py3.12-v2.10.7"  # the base is a plain str
    # The submitted (overridden) task keeps it too.
    assert task.override(interruptible=False).image == agent_image()


async def test_mechanical_submits_the_agent_image(monkeypatch):
    submitted = []
    run = SimpleNamespace(wait=lambda **kw: None, raise_for_status=lambda: None, outputs=lambda: ({},))

    def submit(task, **kwargs):
        submitted.append(task)
        return run

    import flyte

    backend = object.__new__(Backend)
    backend.config = {}
    backend.factory = cast(Any, SimpleNamespace(PROJECT="p", DOMAIN="d"))
    backend.flyte = cast(
        Any,  # a stub of the `flyte` module: only these three names are used
        SimpleNamespace(
            with_runcontext=lambda **kw: SimpleNamespace(run=submit),
            Timeout=flyte.Timeout,
            errors=flyte.errors,
        ),
    )
    await backend.mechanical({"inputs": {"workdir": "s3://w"}, "project_name": ""}, {"patch": PATCH})
    assert submitted[0].image == agent_image()


def test_mechanical_module_does_not_pick_the_pod_base_image(monkeypatch):
    monkeypatch.setenv("_F_IMG_ID", "ghcr.io/flyteorg/flyte:py3.12-v2.10.7")
    monkeypatch.delenv("FLYTE_INTERNAL_IMAGE", raising=False)
    import importlib

    try:
        reloaded = importlib.reload(mechanical)
        assert reloaded.env.image != "ghcr.io/flyteorg/flyte:py3.12-v2.10.7"
    finally:
        monkeypatch.undo()
        importlib.reload(mechanical)


def test_ladder_with_every_check_errored_is_loud(tmp_path, capsys):
    results = [
        {
            "case": c,
            "setting": s,
            "rep": 0,
            "purpose": "delegate",
            "status": "completed",
            "cost_usd": 1.0,
            "output": "x",
        }
        for c in ("a", "b")
        for s in ("cheap", "original")
    ]
    grades = [
        {
            **{k: r[k] for k in ("case", "setting", "rep")},
            "status": "completed",
            "grade": {"correct": True},
            "mechanical": {"applied": None, "tests_passed": None, "error": "RuntimeError"},
        }
        for r in results
    ]
    data = summarize(results, grades, ["cheap"])
    assert data["ladders"][0]["cases"] == []
    assert any("delegate ladder has no usable cases" in e and "4 mechanical" in e for e in data["errors"])

    for name, rows in (("results", results), ("grades", grades)):
        (tmp_path / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with pytest.raises(SystemExit) as stop:
        main(
            [
                "report",
                "--cases",
                str(tmp_path / "cases"),
                "--results",
                str(tmp_path / "results.jsonl"),
                "--grades",
                str(tmp_path / "grades.jsonl"),
                "--output",
                str(tmp_path / "out"),
                "--ladder",
                "cheap",
            ]
        )
    assert stop.value.code == 2
    assert "ERROR: delegate ladder has no usable cases" in (tmp_path / "out.md").read_text()
    assert "no usable cases" in capsys.readouterr().err


# B2


async def test_meter_outage_is_a_stop_not_a_case_failure(tmp_path):
    case = delegate_case()
    backend = Scripted(meters_error=OSError("meter down"))
    path = tmp_path / "grades.jsonl"
    for _ in range(3):
        with pytest.raises(BudgetStop):
            await grade([case], [candidate()], backend, path, {"grader_bias_sample": 0})
    assert backend.launches == 0
    assert read_jsonl(path) == []  # nothing failed, skipped or recorded against the case


async def test_meter_outage_during_reference_is_a_stop(tmp_path):
    case = {
        "id": "r",
        "purpose": "reviewer",
        "inputs": {"brief": "review", "engine": "claude", "model": "opus", "effort": "high"},
        "output": "VERDICT: PASS",
        "ground_truth": {},
    }
    backend = Scripted(meters_error=OSError("meter down"))
    with pytest.raises(BudgetStop):
        await reference([case], backend, {})
    assert "failures" not in case["reference"] and backend.launches == 0


# B3


async def test_invalid_then_valid_grade_keeps_both_calls_in_spend(tmp_path):
    case, config = delegate_case(), {"grader_bias_sample": 0, "prices": {"claude:opus": [1, 0, 0, 0]}}
    path = tmp_path / "grades.jsonl"
    backend = Scripted(["not json", GOOD])
    await grade([case], [candidate()], backend, path, config)  # invalid
    await grade([case], [candidate()], backend, path, config)  # valid, replaces the invalid row
    rows = read_jsonl(path)
    assert [r["status"] for r in rows] == ["completed"]
    assert len(rows[0]["prior_attempts"]) == 1
    spend = summarize([], rows, [])["evaluation_spend"]["grader"]
    assert spend["calls"] == 2
    assert spend["cost_usd"] == pytest.approx(2.0)
    assert spend["tokens"]["input_tokens"] == 2_000_000
    # Rewriting the row again (another retry cycle) must not lose or duplicate the history.
    assert summarize([], read_jsonl(path), [])["evaluation_spend"]["grader"]["calls"] == 2


async def test_two_invalid_then_skipped_keeps_all_three_calls(tmp_path):
    case, config = delegate_case(), {"grader_bias_sample": 0, "max_failures": 3}
    path = tmp_path / "grades.jsonl"
    backend = Scripted(["bad", "bad", "bad"])
    for _ in range(3):
        await grade([case], [candidate()], backend, path, config)
    rows = read_jsonl(path)
    assert [r["status"] for r in rows] == ["skipped"]
    assert summarize([], rows, [])["evaluation_spend"]["grader"]["calls"] == 3


async def test_adjudicator_retry_keeps_both_calls_in_spend():
    case = {
        "id": "r",
        "purpose": "reviewer",
        "inputs": {"brief": "review", "engine": "claude", "model": "opus", "effort": "high"},
        "output": "VERDICT: PASS",
        "ground_truth": {},
    }
    good = json.dumps({"confirmed_blocking": [], "disputed": [], "rejected": []})
    backend = Scripted(["review one", "review two", "garbage", good])
    await reference([case], backend, {})  # invalid adjudication: recorded as a case failure
    await reference([case], backend, {})
    spend = summarize([], [], [], [case])["evaluation_spend"]
    assert spend["panel"]["calls"] == 2
    assert spend["adjudicator"]["calls"] == 2


async def test_cross_check_does_not_swallow_a_budget_stop(tmp_path):
    case = delegate_case()

    class StopsAfterFirst(Scripted):
        async def meters(self, engine):
            if self.launches:
                raise OSError("meter down")
            return await super().meters(engine)

    path = tmp_path / "grades.jsonl"
    with pytest.raises(BudgetStop):
        await grade([case], [candidate()], StopsAfterFirst([GOOD]), path, {"grader_bias_sample": 1.0})
    assert read_jsonl(path) == []


# Advisory


def test_unpriced_calls_are_warned_about():
    results = [
        {
            "case": "a",
            "setting": "s",
            "rep": 0,
            "purpose": "delegate",
            "status": "completed",
            "cost_usd": None,
        }
    ]
    warnings = summarize(results, [], [])["warnings"]
    assert any("1 calls have unknown cost" in w and "codex:" in w for w in warnings)


def test_secs_has_two_decimals():
    assert secs(1.234) == "1.23"
    assert secs(None) == "n/a"


def test_check_task_submits_the_agent_image_through_flyte_image_resolution(monkeypatch):
    # Flyte submits the image built for the task's environment, not `task.image`.
    import flyte
    import flyte._deploy as deploy
    from flyte._internal.runtime.task_serde import lookup_image_in_cache
    from flyte.models import SerializationContext

    monkeypatch.setenv("_F_IMG_ID", "ghcr.io/flyteorg/flyte:py3.12-v2.10.7")
    monkeypatch.delenv("FLYTE_INTERNAL_IMAGE", raising=False)

    async def echo_build(env_name, image):
        return env_name, image.uri, None

    monkeypatch.setattr(deploy, "_build_image_bg", echo_build)
    flyte.init()
    task = check_task().override(interruptible=True)

    async def build() -> Any:
        # `build_images.aio` is typed as an Awaitable, which `asyncio.run` does not accept.
        parent = task.parent_env
        assert parent is not None
        return await deploy.build_images.aio(parent())

    cache = asyncio.run(build())
    ctx = SerializationContext(version="v1", image_cache=cache, root_dir=Path("."))
    assert task.parent_env_name is not None and isinstance(task.image, flyte.Image)
    submitted = lookup_image_in_cache(ctx, task.parent_env_name, task.image)
    assert submitted == agent_image().uri


def test_purpose_without_results_is_not_an_error():
    results = [
        {
            "case": c,
            "setting": s,
            "rep": 0,
            "purpose": "delegate",
            "status": "completed",
            "cost_usd": 1.0,
            "output": "x",
        }
        for c in ("a", "b")
        for s in ("cheap", "original")
    ]
    grades = [
        {
            **{k: r[k] for k in ("case", "setting", "rep")},
            "status": "completed",
            "grade": {"correct": True},
            "mechanical": {"applied": True, "tests_passed": True, "error": None},
        }
        for r in results
    ]
    assert summarize(results, grades, ["cheap"])["errors"] == []


async def test_replay_routes_historical_reviewer_and_defers_exhausted_hard_pin(monkeypatch):
    import flyte
    import flyte.report

    from flyte_agent_factory import _router, agent

    reports = []
    now = time.time()

    async def meters():
        # Owner rule 2: exhausted is a refusal; this used a 100% reading alone.
        return {"codex": _meter.Meter("codex", "app-server", now, 100, now + 3600, limited_until=now + 3600)}

    async def report(text, **kwargs):
        reports.append(text)

    async def run_codex(*args, **kwargs):
        pytest.fail("an exhausted hard-pinned replay must defer before running Codex")

    def submit(task, **kwargs):
        return asyncio.run(agent.subagent.func(**kwargs))

    monkeypatch.setattr(_subagent_run, "_read_direct_meters", meters)
    monkeypatch.setattr(_subagent_run, "_run_codex", run_codex)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(flyte.report.log, "aio", report)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "{}")
    backend = object.__new__(Backend)
    backend.config = {"task": "eval.subagent"}
    backend.factory = cast(Any, SimpleNamespace(PROJECT="p", DOMAIN="d"))
    backend.base = cast(
        Any, SimpleNamespace(PROFILE=SimpleNamespace(slots=[SimpleNamespace(subagent_env="eval")]))
    )
    backend.remote = cast(Any, SimpleNamespace(Task=SimpleNamespace(get=lambda *a, **k: agent.subagent)))
    backend.flyte = cast(
        Any,  # a stub of the `flyte` module: only these three names are used
        SimpleNamespace(
            with_runcontext=lambda **kw: SimpleNamespace(run=submit),
            Timeout=flyte.Timeout,
            errors=flyte.errors,
        ),
    )
    with pytest.raises(_router.RouterDeferred):
        await backend.launch(
            {"brief": "review", "role": "qa", "answer_only": True, "routed": True},
            {"engine": "codex", "model": "", "effort": "high"},
        )
    record = _session_config._routes[-1]
    assert record["deferred"] and record["pinned"] and record["role"] == "qa"
    _, usage = _usage._split_usage(reports[-1])
    assert usage["route"]["deferred"]
