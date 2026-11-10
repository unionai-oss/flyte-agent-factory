"""Round-3 re-QA regressions for model evaluation: real trailers, container import, family judging."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _meter, _session_config, _subagent_run, _usage
from ops.model_eval import mechanical
from ops.model_eval.__main__ import main
from ops.model_eval.data import cost_breakdown, price, read_jsonl
from ops.model_eval.experiment import (
    BudgetStop,
    call_cost,
    grade,
    parse_reply,
    reference,
    replay,
    summarize,
)

PATCH = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
ADJUDICATION = json.dumps({"confirmed_blocking": ["a defect"], "disputed": [], "rejected": []})
ASSESSMENT = {
    "outcome": "pass",
    "correct": True,
    "matched": [],
    "unmatched": [],
    "extras": [],
    "reasons": [],
    "tests_passed": None,
    "verdict_agreement": None,
}
CODEX = {"engine": "codex", "model": "", "effort": "low"}
CLAUDE = {"engine": "claude", "model": "sonnet", "effort": "high"}


async def real_codex_usage(tmp_path, monkeypatch):
    """The usage dict of a real `_run_codex` trailer (its `models` is `["codex:default"]`)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(
        "#!/bin/sh\n"
        'out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        'echo ok > "$out"\n'
        """echo '{"type":"turn.completed","usage":"""
        """{"input_tokens":1000000,"cached_input_tokens":0,"output_tokens":100000}}'\n"""
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "x")
    work = tmp_path / "w"
    (work / ".claude-flyte").mkdir(parents=True)
    (work / ".claude-flyte" / "BRIEF.md").write_text("q")
    out = await _subagent_run._run_codex(
        work, tmp_path, "q", answer_only=True, role="", model="", effort="high"
    )
    return _usage._split_usage(out)[1]


def claude_usage(model, tokens=1_000_000):
    payload = {
        "usage": {"input_tokens": tokens, "output_tokens": tokens},
        "total_cost_usd": 1,
        "modelUsage": {model: {"inputTokens": tokens, "outputTokens": tokens}},
    }
    return _usage._split_usage("x" + _usage._usage_trailer(json.dumps(payload)))[1]


# 1. Codex cost


async def test_real_codex_trailer_is_priced(tmp_path, monkeypatch):
    usage = await real_codex_usage(tmp_path, monkeypatch)
    assert usage["models"] == ["codex:default"]
    cost = call_cost({"usage": usage}, {"engine": "codex", "model": "", "effort": "high"}, {})
    assert cost["cost_usd"] == pytest.approx(1.75 + 1.4)
    assert cost["resolved_model"] == "default"
    assert cost["price_assumption"] is True
    basis = {
        "tokens": {k: usage[k] for k in _session_config._USAGE_FIELDS},
        "engines": ["codex"],
        "models": usage["models"],
    }
    report = "💰 run so far, sub-agents: qa+codex 1.10M tok $0.00 (0%) [usage " + json.dumps(basis) + "]"
    assert cost_breakdown(report, "run")["roles"]["qa+codex"]["cost_usd"] == pytest.approx(3.15)


@pytest.mark.parametrize("key", ["fable", "claude:fable"])
def test_config_price_keys_with_or_without_engine_prefix(key):
    usage = claude_usage("fable")
    cost = call_cost(
        {"usage": usage}, {"engine": "claude", "model": "fable"}, {"prices": {key: [1, 2, 0, 0]}}
    )
    assert cost["cost_usd"] == pytest.approx(3)
    assert price({"input_tokens": 1_000_000}, "codex:codex:default") == pytest.approx(1.75)
    assert price({"input_tokens": 1_000_000}, "gpt-5.4") == pytest.approx(2.5)


# 2. container import


async def test_mechanical_task_imports_from_a_real_code_bundle_without_flyte_agent_factory(tmp_path):
    from flyte._code_bundle.bundle import build_code_bundle

    root = Path(__file__).resolve().parents[1]
    bundle = await build_code_bundle(root, dryrun=True, copy_bundle_to=tmp_path)
    with tarfile.open(bundle.tgz if hasattr(bundle, "tgz") else next(tmp_path.glob("*.tar.gz"))) as tar:
        tar.extractall(tmp_path / "bundle", filter="data")
    # The container has the bundle root as cwd and nothing of ours on PYTHONPATH.
    clean = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = (
        "import sys, ops.model_eval.mechanical as m; "
        "assert 'flyte_agent_factory' not in sys.modules; print(m.checks_rel('p'))"
    )
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", code],
        check=False,
        cwd=tmp_path / "bundle",
        env=clean,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "p/checks.sh"


@pytest.mark.parametrize("name", ["", "proj", "a/b", "/abs", "../up", "."])
def test_copied_checks_path_matches_the_factory(name):
    assert mechanical.checks_rel(name) == _session_config._checks_rel(name)


# 3. panel scope


def reviewer(name="r"):
    return {
        "id": name,
        "purpose": "reviewer",
        "inputs": {"brief": "review", "engine": "claude", "model": "opus", "effort": "high"},
        "output": "VERDICT: FAIL",
        "ground_truth": {},
    }


class FakeBackend:
    """Replies by engine and prompt kind, recording every launch."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = list(fail)

    def live(self):
        return False

    async def meters(self, engine):
        return [_meter.Meter(engine, "fake", time.time(), 1, weekly_pct=1)]

    async def launch(self, inputs, setting):
        self.calls.append((inputs, setting))
        if self.fail and self.fail[0](inputs, setting):
            raise RuntimeError("boom token=secretvalue123")
        brief = inputs["brief"]
        if brief.startswith("Adjudicate"):
            return {"output": ADJUDICATION, "usage": {"output_tokens": 1}}
        if brief.startswith("Independently grade"):
            return {"output": json.dumps(ASSESSMENT), "usage": {"output_tokens": 1}}
        return {"output": "VERDICT: FAIL", "usage": {"output_tokens": 1}}


async def test_overlap_check_covers_only_reviewer_cases():
    planner = {
        "id": "p",
        "purpose": "planner",
        "inputs": {"brief": "plan", "engine": "claude", "model": "fable", "effort": "high"},
        "output": "plan",
    }
    backend = FakeBackend()
    await reference([planner, reviewer()], backend, {})
    assert len(backend.calls) == 3
    colliding = reviewer("c")
    colliding["inputs"].update(model="fable", effort="high")
    with pytest.raises(ValueError, match="disjoint"):
        await reference([planner, colliding], FakeBackend(), {})


# 4. cross-family judging


async def test_adjudicator_alternates_family_and_is_recorded():
    cases = [reviewer("a"), reviewer("b"), reviewer("c")]
    await reference(cases, FakeBackend(), {})
    assert [c["reference"]["adjudicator"]["engine"] for c in cases] == ["claude", "codex", "claude"]
    for case in cases:
        roles = [call["role"] for call in case["reference"]["calls"]]
        assert roles == ["panel", "panel", "adjudicator"]
        assert case["reference"]["calls"][2]["parameters"] == case["reference"]["adjudicator"]
    panel = cases[0]["reference"]["panel"]
    assert sorted(s["engine"] for s in panel) == ["claude", "codex"]


def graded_case():
    case = reviewer()
    case["ground_truth"] = {"verdict": "FAIL"}
    case["reference"] = {
        "panel": [],
        "adjudicator": {"engine": "codex", "model": "", "effort": "high"},
        "adjudication": {"confirmed_blocking": ["a"], "disputed": [], "rejected": []},
    }
    return case


def candidate(setting, name):
    return {
        "case": "r",
        "setting": name,
        "rep": 0,
        "status": "completed",
        "parameters": setting,
        "output": "VERDICT: FAIL",
        "cost_usd": 1.0,
        "purpose": "reviewer",
    }


async def test_candidates_are_graded_by_the_other_family_and_bias_is_measured(tmp_path):
    backend = FakeBackend()
    path = tmp_path / "grades.jsonl"
    results = [candidate(CLAUDE, "sonnet"), candidate(CODEX, "codex-low")]
    await grade([graded_case()], results, backend, path, {"grader_bias_sample": 1})
    rows = {r["setting"]: r for r in read_jsonl(path)}
    assert (
        rows["sonnet"]["grader_family"] == "codex"
        and rows["sonnet"]["cross_check"]["grader_family"] == "claude"
    )
    assert rows["codex-low"]["grader_family"] == "claude"
    assert rows["codex-low"]["cross_check"]["grader_family"] == "codex"
    assert rows["sonnet"]["adjudicator_family"] == "codex"
    engines = [setting["engine"] for _, setting in backend.calls]
    assert engines == ["codex", "claude", "claude", "codex"]
    summary = summarize(results, read_jsonl(path), [], [graded_case()])
    assert {b["candidate_family"] for b in summary["grader_bias"]} == {"claude", "codex"}
    assert all(b["n"] == 1 and b["bias"] == 0 for b in summary["grader_bias"])
    assert {(f["candidate_family"], f["adjudicator_family"]) for f in summary["family_agreement"]} == {
        ("claude", "codex"),
        ("codex", "codex"),
    }
    assert summary["evaluation_spend"]["grader"]["calls"] == 4


async def test_bias_sample_defaults_to_a_fraction_and_is_stable(tmp_path):
    results = [{**candidate(CLAUDE, "sonnet"), "rep": i} for i in range(200)]
    path = tmp_path / "grades.jsonl"
    await grade([graded_case()], results, FakeBackend(), path, {})
    sampled = sum("cross_check" in r for r in read_jsonl(path))
    assert 10 < sampled < 80  # ~20% of 200, not all and not none
    again = tmp_path / "again.jsonl"
    await grade([graded_case()], results, FakeBackend(), again, {})
    assert [r["rep"] for r in read_jsonl(again) if "cross_check" in r] == [
        r["rep"] for r in read_jsonl(path) if "cross_check" in r
    ]


# 5. failure isolation


async def test_reference_failures_are_isolated_counted_and_skipped():
    first, second = reviewer("first"), reviewer("second")
    # Fail the very first launch (it belongs to `first`); everything after succeeds.
    calls = {"n": 0}

    def fail_first_case(inputs, setting):
        calls["n"] += 1
        return calls["n"] == 1

    await reference([first, second], FakeBackend(fail=[fail_first_case]), {})
    assert first["reference"]["failures"]["reference"] == {"attempts": 1, "error": "RuntimeError"}
    assert "secretvalue" not in json.dumps(first["reference"])
    assert second["ground_truth"]["verdict"] == "FAIL"
    # Retried next run and cleared on success.
    await reference([first, second], FakeBackend(), {})
    assert "failures" not in first["reference"] and first["ground_truth"]["verdict"] == "FAIL"

    stubborn = reviewer("stubborn")

    def always(inputs, setting):
        return True

    for _ in range(3):
        await reference([stubborn], FakeBackend(fail=[always]), {})
    assert stubborn["reference"]["skipped"] == "failed 3 times in reference: RuntimeError"
    untouched = FakeBackend()
    await reference([stubborn], untouched, {})
    assert untouched.calls == []


async def test_budget_stop_is_not_recorded_against_the_case(monkeypatch):
    case = reviewer()
    backend = FakeBackend()
    monkeypatch.setattr(backend, "live", lambda: True)
    with pytest.raises(BudgetStop):
        await reference([case], backend, {})
    assert "failures" not in case.get("reference", {})


async def test_grade_failures_are_isolated_superseded_and_skipped(tmp_path):
    path = tmp_path / "grades.jsonl"
    results = [{**candidate(CLAUDE, "sonnet"), "rep": i} for i in range(2)]

    grading = {"armed": True, "launches": 0}

    def first_grade_fails(inputs, setting):
        grading["launches"] += 1
        return grading["armed"] and grading["launches"] == 1

    backend = FakeBackend(fail=[first_grade_fails])
    config = {"grader_bias_sample": 0}
    await grade([graded_case()], results, backend, path, config)
    rows = {r["rep"]: r for r in read_jsonl(path)}
    assert rows[0]["status"] == "error" and rows[0]["attempts"] == 1
    assert rows[0]["error"] == "RuntimeError" and "secretvalue" not in json.dumps(rows[0])
    assert rows[1]["status"] == "completed"
    # The retry succeeds and replaces the error row: one row per key.
    grading["armed"] = False
    await grade([graded_case()], results, FakeBackend(), path, config)
    final = read_jsonl(path)
    assert sorted((r["rep"], r["status"]) for r in final) == [(0, "completed"), (1, "completed")]

    path = tmp_path / "skip.jsonl"

    def always(inputs, setting):
        return True

    for _ in range(3):
        await grade([graded_case()], results[:1], FakeBackend(fail=[always]), path, config)
    (row,) = read_jsonl(path)
    assert row["status"] == "skipped" and row["reason"] == "failed 3 times: RuntimeError"
    untouched = FakeBackend()
    await grade([graded_case()], results[:1], untouched, path, config)
    assert untouched.calls == []
    summary = summarize(results, read_jsonl(path), [], [])
    assert summary["failures"]["grade_skipped"][0]["reason"] == "failed 3 times: RuntimeError"


# 6. mechanical environment


class FakeDir:
    def __init__(self, files):
        self.files = files

    async def download(self, folder):
        root = Path(folder)
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(root)], check=True)
        for name, text in self.files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)


PYTEST_REPO = {
    "x": "old\n",
    "test_x.py": "def test_x():\n    assert open('x').read() == 'new\\n'\n",
    "checks.sh": "python -m pytest -q test_x.py\n",
}


def production_like_path(monkeypatch):
    # The image's python lives outside os.defpath, as /opt/venv/bin does in the factory image.
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}:/usr/bin:/bin")


async def test_container_env_runs_checks_with_the_images_path(monkeypatch):
    production_like_path(monkeypatch)
    monkeypatch.setenv("CODEX_AUTH_JSON", "login")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "t")
    monkeypatch.setenv("ORDINARY_SETTING", "kept")
    env = mechanical.checks_env(os.environ, "/scratch")
    assert env["PATH"] == os.environ["PATH"] and env["HOME"] == "/scratch"
    assert env["ORDINARY_SETTING"] == "kept"
    assert "CODEX_AUTH_JSON" not in env and "MY_SERVICE_TOKEN" not in env
    result = await mechanical.run_check(cast(Any, FakeDir(PYTEST_REPO)), PATCH, "")
    assert result["tests_passed"] is True, result["tail"]


async def test_local_mechanical_uses_env_minus_i_and_scratch_home(monkeypatch):
    production_like_path(monkeypatch)
    monkeypatch.setenv("OPERATOR_SECRET", "must not reach child")
    monkeypatch.setenv("ORDINARY_SETTING", "also cleared by env -i")
    script = (
        'test -z "$OPERATOR_SECRET" && test -z "$ORDINARY_SETTING" || exit 3\n'
        'case "$HOME" in */model-eval-check-*/home) ;; *) exit 4 ;; esac\n'
        "python -m pytest -q test_x.py\n"
    )
    files = {**PYTEST_REPO, "checks.sh": script}
    result = await mechanical.run_check(cast(Any, FakeDir(files)), PATCH, "", isolate=True)
    assert result["tests_passed"] is True, result["tail"]


# 7. report


def test_report_has_spend_section_and_formatted_tables(tmp_path):
    results = [
        {
            "case": "r",
            "purpose": "reviewer",
            "setting": "sonnet",
            "rep": 0,
            "status": "completed",
            "cost_usd": 1.23456,
            "wall_s": 3.0,
            "usage": {"input_tokens": 1234, "output_tokens": 5},
            "output": "VERDICT: FAIL",
        }
    ]
    case = graded_case()
    case["reference"]["calls"] = [
        {"role": "panel", "cost_usd": 0.5, "usage": {"input_tokens": 10}},
        {"role": "panel", "cost_usd": None, "usage": {"input_tokens": 20}},
        {"role": "adjudicator", "cost_usd": 0.25, "usage": {"output_tokens": 7}},
    ]
    summary = summarize(results, [], [], [case])
    spend = summary["evaluation_spend"]
    assert spend["panel"] == {
        "calls": 2,
        "cost_usd": 0.5,
        "unknown_cost": 1,
        "tokens": {
            "input_tokens": 30,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    assert spend["adjudicator"]["cost_usd"] == 0.25


# 8. adjudicator JSON after prose


@pytest.mark.parametrize(
    "text",
    [
        'Here is my ruling.\n{"confirmed_blocking": [], "disputed": ["x {y}"], "rejected": []}',
        'Note {not json}.\n```json\n{"confirmed_blocking": [], "disputed": ["x {y}"], "rejected": []}\n```\n'
        "Done.",
        '{"confirmed_blocking": [], "disputed": ["x {y}"], "rejected": []}',
    ],
)
def test_parse_reply_finds_first_balanced_object(text):
    assert parse_reply(text) == {"confirmed_blocking": [], "disputed": ["x {y}"], "rejected": []}


def test_parse_reply_without_json_raises():
    with pytest.raises(ValueError):
        parse_reply("no braces here")
    with pytest.raises(ValueError):
        parse_reply("only {broken")


async def test_reference_accepts_adjudication_after_prose(monkeypatch):
    case = reviewer()
    backend = FakeBackend()
    original = backend.launch

    async def launch(inputs, setting):
        result = await original(inputs, setting)
        if inputs["brief"].startswith("Adjudicate"):
            result["output"] = "My ruling follows.\n" + result["output"]
        return result

    monkeypatch.setattr(backend, "launch", launch)
    await reference([case], backend, {})
    assert case["ground_truth"]["verdict"] == "FAIL"


# 8b. one budget check per logical call


async def test_on_demand_fallback_is_not_budget_checked_again(monkeypatch):
    import flyte

    from ops.model_eval import experiment
    from ops.model_eval.remote import Backend

    async def refuse(*args, **kwargs):
        raise AssertionError("fallback must not re-run the budget guard")

    monkeypatch.setattr(experiment, "budget_guard", refuse)
    attempts = []

    class Task:
        def override(self, **kwargs):
            attempts.append(kwargs["interruptible"])
            return self

    def run_task(task, **kwargs):
        interruptible = attempts[-1]

        def raise_for_status():
            if interruptible:
                raise flyte.errors.MaxQueuedTimeExceededError("queued")

        return SimpleNamespace(
            name="r",
            wait=lambda **kw: None,
            raise_for_status=raise_for_status,
            outputs=lambda: ("done", None),
            get_report=lambda: "",
        )

    backend: Any = object.__new__(Backend)  # the fakes below replace its real collaborators
    backend.flyte = SimpleNamespace(
        with_runcontext=lambda **kw: SimpleNamespace(run=run_task), Timeout=flyte.Timeout, errors=flyte.errors
    )
    backend.remote = SimpleNamespace(Task=SimpleNamespace(get=lambda *a, **k: Task()))
    backend.factory = SimpleNamespace(PROJECT="p", DOMAIN="d")
    backend.base = SimpleNamespace(PROFILE=SimpleNamespace(slots=[SimpleNamespace(subagent_env="env")]))
    backend.config, backend.allow_live = {}, False
    result = await backend.launch({"brief": "b", "answer_only": True}, CODEX)
    assert attempts == [True, False] and result["output"] == "done"


async def test_replay_checks_budget_once_per_logical_call(tmp_path):
    guards = []

    class Counting(FakeBackend):
        async def meters(self, engine):
            guards.append(engine)
            return await super().meters(engine)

    case = reviewer()
    await replay([case], [{"name": "cheap", **CODEX}], Counting(), tmp_path / "r.jsonl", {"concurrency": 1})
    assert len(guards) == 4  # one candidate + three original repetitions


# End-to-end CLI pipeline with fake backends


def action(name, brief, output, workdir=True, **inputs):
    values = {
        "brief": brief,
        "role": "qa",
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "answer_only": True,
        **inputs,
    }
    if workdir:
        values["workdir"] = SimpleNamespace(path=f"s3://work/{name}")

    class Details:
        async def inputs(self):
            return values

        async def outputs(self):
            return (output, SimpleNamespace(path=f"s3://out/{name}"))

    return SimpleNamespace(name=name, task_name="x.subagent"), Details()


def test_cli_pipeline_collect_replay_reference_grade_report(tmp_path, monkeypatch):
    codex_usage = asyncio.run(real_codex_usage(tmp_path, monkeypatch))
    claude = claude_usage("claude-opus-4-6")

    def trailer(usage):
        return "\n<!--factory-usage " + json.dumps(usage) + "-->"

    review = "You are the **correctness** reviewer. ## Your concern\nCheck it."
    delegate = "First run `tar xzf repo.tar.gz && cd repo`. Implement x."
    actions = {
        a.name: (a, d)
        for a, d in [
            action("a1", review, "Found issues.\nBLOCKING: 1\nVERDICT: FAIL" + trailer(claude)),
            action("a2", review + " again", "Fine.\nVERDICT: PASS" + trailer(claude)),
            action("a3", delegate, "implemented" + trailer(claude), answer_only=False, role="dev"),
        ]
    }

    async def get(**kwargs):
        if kwargs["name"] == "a0":
            return SimpleNamespace(inputs=lambda: _async({"name": "proj"}))
        return actions[kwargs["name"]][1]

    remote = SimpleNamespace(
        Run=SimpleNamespace(get=lambda name: SimpleNamespace(get_report=lambda: "report")),
        Action=SimpleNamespace(listall=lambda **kw: [a for a, _ in actions.values()]),
        ActionDetails=SimpleNamespace(get=SimpleNamespace(aio=get)),
    )
    launches = []

    class Backend:
        def __init__(self, config, allow_live=False):
            self.remote = remote

        def runs(self, names, days=None):
            return [SimpleNamespace(name=n) for n in names]

        def live(self):
            return False

        async def meters(self, engine):
            return [_meter.Meter(engine, "fake", time.time(), 1, weekly_pct=1)]

        async def mechanical(self, case, result):
            return {"applied": True, "tests_passed": result["setting"] != "codex-low"}

        async def launch(self, inputs, setting):
            launches.append((inputs["brief"][:20], setting["engine"]))
            brief = inputs["brief"]
            usage = codex_usage if setting["engine"] == "codex" else claude
            if brief.startswith("Adjudicate"):
                return {"output": ADJUDICATION, "usage": usage, "run": "adj", "wall_s": 1.0}
            if brief.startswith("Independently grade"):
                return {"output": json.dumps(ASSESSMENT), "usage": usage, "run": "g", "wall_s": 1.0}
            if inputs.get("answer_only"):
                return {"output": "Reviewed.\nVERDICT: FAIL", "usage": usage, "run": "r", "wall_s": 2.0}
            return {
                "output": "done",
                "usage": usage,
                "run": "d",
                "wall_s": 3.0,
                "patch": PATCH,
                "patch_scrubbed": False,
                "patch_file_count": 1,
            }

    import ops.model_eval.remote as remote_module

    async def no_models(config):
        return []

    monkeypatch.setattr(remote_module, "Backend", Backend)
    monkeypatch.setattr(remote_module, "discover_models", no_models)
    config = tmp_path / "eval.toml"
    config.write_text(
        "concurrency = 1\ngrader_bias_sample = 1.0\n[prices]\nfable = [1, 5, 1.25, 0.1]\n"
        '[[settings]]\nname = "codex-low"\nengine = "codex"\nmodel = ""\neffort = "low"\n'
        '[[settings]]\nname = "sonnet"\nengine = "claude"\nmodel = "sonnet"\neffort = "high"\n'
    )
    cases, results, grades = (tmp_path / "cases", tmp_path / "results.jsonl", tmp_path / "grades.jsonl")
    base = ["--config", str(config)]
    main([*base, "collect", "run1", "--cases", str(cases)])
    main([*base, "replay", "--cases", str(cases), "--results", str(results), "--allow-live"])
    main([*base, "replay", "--reference", "--cases", str(cases), "--allow-live"])
    main(
        [
            *base,
            "grade",
            "--cases",
            str(cases),
            "--results",
            str(results),
            "--grades",
            str(grades),
            "--allow-live",
        ]
    )
    out = tmp_path / "report"
    main(
        [
            *base,
            "report",
            "--cases",
            str(cases),
            "--results",
            str(results),
            "--grades",
            str(grades),
            "--ladder",
            "codex-low",
            "sonnet",
            "--output",
            str(out),
        ]
    )
    data = json.loads(out.with_suffix(".json").read_text())
    assert {ladder["purpose"] for ladder in data["ladders"]} == {"delegate", "reviewer"}
    for ladder in data["ladders"]:
        assert ladder["cases"], f"empty {ladder['purpose']} ladder: excluded {ladder['excluded']}"
        assert ladder["excluded"] == []
    assert all(g["cost_usd"]["unknown"] == 0 for g in data["groups"]), "every candidate cost is priced"
    spend = data["evaluation_spend"]
    assert spend["panel"]["calls"] == 4 and spend["adjudicator"]["calls"] == 2
    assert spend["grader"]["calls"] > 0 and spend["grader"]["unknown_cost"] == 0
    assert data["grader_bias"] and data["family_agreement"]
    assert data["failures"]["grade_errors"] == 0
    # Adjudication alternated, and graders never shared a family with their candidate.
    reviewers = [json.loads(p.read_text()) for p in sorted(cases.glob("*/*.json"))]
    families = sorted(
        c["reference"]["adjudicator"]["engine"] for c in reviewers if c["purpose"] == "reviewer"
    )
    assert families == ["claude", "codex"]
    for row in read_jsonl(grades):
        assert row["grader_family"] != row["candidate_family"]
        assert row["cross_check"]["grader_family"] == row["candidate_family"]
    text = out.with_suffix(".md").read_text()
    for heading in ("## Candidates", "## Escalation ladders", "## Evaluation spend", "## Grader bias"):
        assert heading in text
    assert "| reviewer | codex-low | 2 | 2 | 100% | $" in text
    assert "$ std dev" in text and "Unknown $" in text and "Input tok" in text


async def _async(value):
    return value
