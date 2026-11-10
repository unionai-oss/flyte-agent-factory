"""Re-QA regressions for reference integrity, stored patches and token pricing."""

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _meter, _session_config, _usage
from ops.model_eval.data import append_jsonl, cost_breakdown, read_jsonl, scrub
from ops.model_eval.experiment import anonymous, call_cost, grade, grade_prompt, reference, settings
from ops.model_eval.remote import Backend

PATCH = """diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1 +1,3 @@
-old = True
+effort = "high"
+engine = "claude"
+token = os.environ["X"]
"""


def reviewer(name="case"):
    return {
        "id": name,
        "purpose": "reviewer",
        "inputs": {
            "brief": 'Review effort = "high" and engine = "claude".',
            "engine": "claude",
            "model": "opus",
            "effort": "high",
        },
        "output": "Claude found a blocking issue",
        "ground_truth": {},
    }


class FakeBackend:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def live(self):
        return False

    async def meters(self, engine):
        return [_meter.Meter(engine, "fake", time.time(), 1, weekly_pct=1)]

    async def launch(self, inputs, setting):
        self.calls.append((inputs, setting))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return {"output": reply, "usage": {"output_tokens": 10}}


async def test_default_panel_disjoint_and_reference_verdict():
    candidate = reviewer()
    adjudication = {"confirmed_blocking": ["a real defect"], "disputed": [], "rejected": []}
    backend = FakeBackend(
        ["OpenAI review", "Anthropic review", "```json\n" + json.dumps(adjudication) + "\n```"]
    )
    snapshots = []
    await reference(
        [candidate], backend, {}, persist=lambda case: snapshots.append(json.loads(json.dumps(case)))
    )
    assert [len(c["reference"]["calls"]) for c in snapshots[:3]] == [1, 2, 3]
    assert candidate["ground_truth"]["verdict"] == "FAIL"
    panel = candidate["reference"]["panel"]

    def signature(setting):
        return tuple(setting.get(k) for k in ("engine", "model", "effort"))

    assert not {signature(s) for s in panel} & {signature(s) for s in settings({}) + [candidate["inputs"]]}
    assert sorted(candidate["reference"]["review_order"]) == [0, 1, 2]
    evidence = json.loads(backend.calls[2][0]["brief"].split("\n", 1)[1])
    assert evidence["brief"] == candidate["inputs"]["brief"]
    assert all(
        identity not in json.dumps(evidence["reviews"]) for identity in ("Claude", "Anthropic", "OpenAI")
    )
    with pytest.raises(ValueError, match="disjoint"):
        await reference([reviewer()], FakeBackend([]), {"reference_panel": [candidate["inputs"], panel[1]]})


async def test_panel_results_resume_and_parse_failure_continues():
    first, second = reviewer("first"), reviewer("second")
    backend = FakeBackend(["one", RuntimeError("interrupted")])
    await reference([first], backend, {})
    assert len(first["reference"]["calls"]) == 1
    assert first["reference"]["failures"]["reference"] == {"attempts": 1, "error": "RuntimeError"}
    valid = json.dumps({"confirmed_blocking": [], "disputed": [], "rejected": []})
    resumed = FakeBackend(["two", "malformed", "three", "four", valid])
    await reference([first, second], resumed, {})
    assert len(resumed.calls) == 5
    assert first["reference"]["error"] == "invalid reference adjudication"
    assert second["ground_truth"]["verdict"] == "PASS"


def test_patch_reaches_grader_and_store_unchanged_and_applies(tmp_path):
    candidate = reviewer()
    candidate["purpose"] = "delegate"
    prompt = grade_prompt(candidate, {"output": "Codex summary", "patch": PATCH, "setting": "a"})
    evidence = json.loads(prompt.split("\n\n", 1)[1])
    assert evidence["candidate_patch"] == PATCH
    assert evidence["brief"] == candidate["inputs"]["brief"]
    path = tmp_path / "results.jsonl"
    append_jsonl(path, {"patch": PATCH})
    record = read_jsonl(path)[0]
    assert record["patch"] == PATCH
    assert record["patch_scrubbed"] is False
    crlf_path = tmp_path / "crlf.jsonl"
    append_jsonl(crlf_path, {"patch": PATCH.replace("\n", "\r\n")})
    assert read_jsonl(crlf_path)[0]["patch"] == PATCH.replace("\n", "\r\n")
    assert (tmp_path / record["patch_file"]).read_bytes() == PATCH.encode()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "config.py").write_text("old = True\n")
    subprocess.run(["git", "apply", str(tmp_path / record["patch_file"])], cwd=tmp_path, check=True)


def test_prose_identity_redaction_preserves_fences_and_diff_hunks():
    source = '```python\nengine = "Claude"\n```\n'
    diff = "@@ -1 +1 @@\n-Claude\n+OpenAI\n"
    assert anonymous("Claude and GPT-5.4 from OpenAI\n" + source).endswith(source)
    assert "Claude" not in anonymous("Claude and GPT-5.4 from OpenAI")
    assert anonymous(diff) == diff
    assert anonymous("high low medium original claude token") == "high low medium original claude token"


@pytest.mark.parametrize(
    "value",
    [
        'token = os.environ["X"]',
        "max_token=4096",
        "def f(secret: str):",
        'effort = "high"',
        'engine = "claude"',
    ],
)
def test_source_false_positives_preserved(value):
    assert scrub(value) == value
    assert scrub({"patch": value})["patch"] == value


@pytest.mark.parametrize(
    "value",
    [
        'password: "correct horse battery"',
        'export CLAUDE_CODE_OAUTH_TOKEN="oat01 abc"',
        "tskey-auth-" + "a" * 30,
        "Bearer " + "a" * 30,
        *[
            f"-----BEGIN {kind}PRIVATE KEY-----\nsecret data"
            for kind in ("", "RSA ", "DSA ", "EC ", "OPENSSH ", "ENCRYPTED ")
        ],
    ],
)
def test_complete_secret_values_removed(value):
    assert scrub(value) == ("export [REDACTED]" if value.startswith("export ") else "[REDACTED]")


def test_secret_dict_keys_and_scrubbed_patch_exclusion(tmp_path):
    assert scrub(
        {"Client_Secret": "space separated value", "MY_API_KEY": "whole value", "refresh_token": "x"}
    ) == {"Client_Secret": "[REDACTED]", "MY_API_KEY": "[REDACTED]", "refresh_token": "[REDACTED]"}
    path = tmp_path / "results.jsonl"
    append_jsonl(path, {"patch": PATCH + "+key = 'sk-ant-" + "a" * 30 + "'\n"})
    row = read_jsonl(path)[0]
    assert row["patch_scrubbed"] is True
    backend = object.__new__(Backend)
    result = asyncio.run(backend.mechanical({"inputs": {}}, row))
    assert result["applied"] is None and result["tests_passed"] is None


async def test_project_checks_and_clean_local_environment(tmp_path, monkeypatch):
    from flyte.io import Dir

    async def download(self, folder):
        root = Path(folder)
        await asyncio.to_thread(subprocess.run, ["git", "init", "-q", str(root)], check=True)
        (root / "config.py").write_text("old = True\n")
        (root / "myproj").mkdir()
        (root / "myproj/checks.sh").write_text('test -z "$OPERATOR_SECRET" && grep -q high config.py\n')
        (root / "checks.sh").write_text("exit 99\n")

    monkeypatch.setenv("OPERATOR_SECRET", "must not reach child")
    monkeypatch.setattr(Dir, "download", download)
    backend = object.__new__(Backend)
    backend.config = {"local_mechanical": True}
    result = await backend.mechanical(
        {"inputs": {"workdir": "s3://original"}, "project_name": "myproj"}, {"patch": PATCH}
    )
    assert result["applied"] is True and result["tests_passed"] is True
    assert result["exit_code"] == 0


def test_real_multimodel_claude_trailer_pricing(monkeypatch):
    payload = {
        "usage": {"input_tokens": 100, "output_tokens": 200},
        "total_cost_usd": 99,
        "modelUsage": {
            "claude-opus-4-7": {"inputTokens": 1_000_000, "outputTokens": 1_000_000},
            "claude-sonnet-4-5-20250929": {"inputTokens": 1_000_000, "outputTokens": 1_000_000},
            "sonnet[1m]": {"cacheReadInputTokens": 1_000_000},
        },
    }
    _, usage = _usage._split_usage("answer" + _usage._usage_trailer(json.dumps(payload)))
    cost = call_cost({"usage": usage}, {"engine": "claude", "model": "opus"}, {})
    assert cost["cost_usd"] == pytest.approx(48.3)
    assert cost["cli_cost_usd"] == 99
    ledger = {
        "qa@1": {
            **{field: usage[field] for field in _session_config._USAGE_FIELDS},
            "cost_usd": 99,
            "calls": 1,
            "model_usage_calls": 1,
            "models": usage["models"],
            "engines": ["claude"],
            "modelUsage": usage["modelUsage"],
        }
    }
    monkeypatch.setattr(_session_config, "_usage", ledger)
    report = "💰 run so far, sub-agents: " + _usage._usage_summary()
    assert cost_breakdown(report, "run")["roles"]["qa"]["cost_usd"] == pytest.approx(48.3)

    usage["modelUsage"]["unknown-model"] = {"outputTokens": 100}
    cost = call_cost({"usage": usage}, {"engine": "claude", "model": "opus"}, {})
    assert cost["cost_usd"] is None
    assert cost["known_subtotal_usd"] == pytest.approx(48.3)


def test_cost_rows_table_price_and_cli_zero_separate():
    basis = {"tokens": {"output_tokens": 1_000_000}, "engines": ["codex"], "models": []}
    report = "💰 run so far, sub-agents: qa+codex 1.00M tok $0.00 (0%) [usage " + json.dumps(basis) + "]"
    result = cost_breakdown(report, "run")
    assert result["roles"]["qa+codex"]["cli_cost_usd"] == 0
    assert result["roles"]["qa+codex"]["cost_usd"] == 14
    override = cost_breakdown(report, "run", prices={"codex:default": [1, 2, 0, 0]})
    assert override["roles"]["qa+codex"]["cost_usd"] == 2


@pytest.mark.parametrize("unit", ["m", "min", "mins", "minute", "minutes"])
def test_gap_units(unit):
    assert cost_breakdown(f"idle for 2 {unit}", "run")["idle_gaps"][0]["minutes"] == 2


async def test_mechanical_default_submits_credential_free_task(monkeypatch):
    import flyte

    from ops.model_eval import mechanical

    calls = []
    measured = {"applied": True, "tests_passed": True, "exit_code": 0, "tail": "ok"}
    run = SimpleNamespace(
        wait=lambda **kwargs: None, raise_for_status=lambda: None, outputs=lambda: (measured,)
    )

    def submit(task, **kwargs):
        calls.append((task, kwargs))
        return run

    backend = object.__new__(Backend)
    backend.config = {}
    # the fake stands in for the lazily imported modules, which are module-typed attributes
    monkeypatch.setattr(backend, "factory", SimpleNamespace(PROJECT="p", DOMAIN="d"), raising=False)
    monkeypatch.setattr(
        backend,
        "flyte",
        SimpleNamespace(
            with_runcontext=lambda **kwargs: SimpleNamespace(run=submit),
            Timeout=flyte.Timeout,
            errors=flyte.errors,
        ),
        raising=False,
    )
    result = await backend.mechanical(
        {"inputs": {"workdir": "s3://original"}, "project_name": "myproj"}, {"patch": PATCH}
    )
    assert result == measured
    assert calls[0][1]["patch"] == PATCH
    assert calls[0][1]["project_name"] == "myproj"
    assert calls[0][0].timeout.max_queued_time.total_seconds() == 300
    assert not mechanical.env.secrets


async def test_mechanical_failure_does_not_abort_grading(tmp_path, monkeypatch):
    cases = [
        {"id": name, "purpose": "delegate", "inputs": {"brief": "implement"}, "ground_truth": {}}
        for name in ("first", "second")
    ]
    assessment = json.dumps(
        {
            "outcome": "partial",
            "correct": False,
            "matched": [],
            "unmatched": [],
            "extras": [],
            "reasons": [],
            "tests_passed": None,
            "verdict_agreement": None,
        }
    )
    backend = FakeBackend([assessment, assessment])

    async def mechanical(case, result):
        raise RuntimeError("container failed")

    monkeypatch.setattr(backend, "mechanical", mechanical, raising=False)  # FakeBackend has none
    results = [
        {
            "case": case["id"],
            "setting": "cheap",
            "rep": 0,
            "status": "completed",
            "parameters": {"engine": "codex", "model": "", "effort": "low"},
            "output": "summary",
            "patch": PATCH,
        }
        for case in cases
    ]
    path = tmp_path / "grades.jsonl"
    await grade(cases, results, backend, path, {"grader_bias_sample": 0})
    rows = read_jsonl(path)
    assert len(rows) == 2
    assert all(row["mechanical"]["tests_passed"] is None for row in rows)
    assert all(row["status"] == "completed" for row in rows)


async def test_default_original_and_candidates_remain_gradable(tmp_path):
    candidate = reviewer()
    adjudication = json.dumps({"confirmed_blocking": ["blocking defect"], "disputed": [], "rejected": []})
    await reference([candidate], FakeBackend(["review one", "review two", adjudication]), {})
    assessment = json.dumps(
        {
            "outcome": "pass",
            "correct": True,
            "matched": [],
            "unmatched": [],
            "extras": [],
            "reasons": [],
            "tests_passed": None,
            "verdict_agreement": True,
        }
    )
    variants = settings({}) + [
        {"name": "original", **{k: candidate["inputs"][k] for k in ("engine", "model", "effort")}}
    ]
    results = [
        {
            "case": candidate["id"],
            "setting": setting["name"],
            "rep": 0,
            "status": "completed",
            "parameters": setting,
            "output": "VERDICT: PASS",
        }
        for setting in variants
    ]
    path = tmp_path / "grades.jsonl"
    backend = FakeBackend([assessment] * len(results))
    await grade([candidate], results, backend, path, {"grader_bias_sample": 0})
    rows = read_jsonl(path)
    assert len(rows) == len(variants)
    assert all(row["status"] == "completed" for row in rows)
    assert all(row["grade"]["verdict_agreement"] is False for row in rows)
