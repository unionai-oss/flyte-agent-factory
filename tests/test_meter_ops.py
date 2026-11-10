"""The operator side: `operate.py meters`, `meter_logins.py` and `meter_report.py`."""

from __future__ import annotations

import importlib.util
import json
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _factory, _factory_meters, _meter, _secret_env

OPS = Path(__file__).resolve().parents[1] / "ops"


def _ops(name):
    spec = importlib.util.spec_from_file_location(f"ops_{name}", OPS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


operate = _ops("operate")
meter_logins = _ops("meter_logins")
meter_report = _ops("meter_report")

NOW = time.time()


def _run(name, task, report):
    return SimpleNamespace(
        name=name,
        phase="ActionPhase.RUNNING",
        url="u",
        get_report=lambda: report,
        to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}},
    )


def _report(*meters):
    return "<table>" + "".join(_meter.report_comment(m) for m in meters) + "</table>"


def test_operate_meters_prints_each_live_projects_readings(monkeypatch, capsys):
    NOW = time.time()  # the module's is from collection time, minutes before this test runs
    claude = _meter.Meter("claude work", "oauth-usage", NOW - 180, 34.0, NOW + 3600, 16.0, NOW + 86400 * 3)
    codex = _meter.Meter("codex", "app-server", NOW - 60, 5.0, NOW + 60, 6.0, NOW + 90)
    runs = [_run("p1", "agent-one.project", _report(claude, codex)), _run("p2", "agent-two.project", "<p/>")]
    monkeypatch.setattr(operate, "operated_live", lambda: runs)
    operate.meters(None)
    out = capsys.readouterr().out
    assert "p1 (agent-one.project)" in out
    assert "claude work: 5h 34% (resets" in out and "· week 16% (resets" in out and "read 3m ago" in out
    assert "codex: 5h 5%" in out
    assert "p2 (agent-two.project)\n  no meter in the report yet" in out


def test_operate_meters_with_nothing_live_and_a_missing_report(monkeypatch, capsys):
    monkeypatch.setattr(operate, "operated_live", list)
    operate.meters(None)
    assert "no live projects" in capsys.readouterr().out

    broken = _run("p1", "agent-one.project", "")
    broken.get_report = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
    monkeypatch.setattr(operate, "operated_live", lambda: [broken])
    operate.meters(None)
    assert "report unavailable: RuntimeError: gone" in capsys.readouterr().out


def test_meters_is_a_subcommand_that_accepts_the_profile_flag(monkeypatch, capsys):
    monkeypatch.setattr(operate, "_init", lambda: None)
    monkeypatch.setattr(operate, "operated_live", list)
    operate.main(["meters"])
    assert "no live projects" in capsys.readouterr().out


def test_launch_uses_headroom(flyte_world, monkeypatch, tmp_path):
    # Replaces test_launch_uses_headroom_unless_a_task_is_forced: a queue serves every launcher, so a
    # per-shell OPERATE_TASK preference no longer exists; the slot is always the one with headroom.
    monkeypatch.setitem(sys.modules, "watch", SimpleNamespace(ensure_running=lambda: 1, LOG="log"))
    monkeypatch.setattr(
        _factory_meters, "slot_headroom", lambda slots: {operate.SLOTS[0]: 5.0, operate.SLOTS[1]: 80.0}
    )
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    monkeypatch.setenv("OPERATE_TASK", operate.SLOTS[0])
    operate.launch(SimpleNamespace(brief=str(brief), name="proj", demo=""))
    assert [c["task"] for c in flyte_world.created] == [operate.SLOTS[1]]


# --------------------------------------------------------------------------- #
# meter_logins
# --------------------------------------------------------------------------- #


class FakeRun:
    def __init__(self, outcome):
        self.outcome = outcome

    def wait(self, quiet=False):
        pass

    def raise_for_status(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome

    def outputs(self):
        return (self.outcome,)


def _payload(account, five):
    return json.dumps(_meter.Meter(account, "oauth-usage", NOW, five, NOW + 600, 10.0, NOW + 9000).to_dict())


def _work_token():
    return meter_logins._factory_base.PROFILE.secrets.claude_token


def test_meter_logins_prints_the_work_tokens_line(monkeypatch, capsys):
    monkeypatch.setattr(meter_logins, "_init", lambda: None)
    asked = []
    monkeypatch.setattr(meter_logins._factory_meters, "login_meters", lambda: {})

    def submit(token, *, interruptible):
        asked.append((token, interruptible))
        return FakeRun(_payload(f"claude {token}", 42.0))

    meter_logins.main([], submit=submit)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"claude {_work_token()}: 5h 42%")
    assert asked == [(_work_token(), True)], "one probe for the one token every slot shares"


def test_meter_logins_says_when_the_token_cannot_be_read(monkeypatch, capsys):
    monkeypatch.setattr(meter_logins, "_init", lambda: None)
    monkeypatch.setattr(meter_logins._factory_meters, "login_meters", lambda: {})

    def submit(token, *, interruptible):
        return FakeRun(_meter.MeterError("usage request refused (401): the access token has expired"))

    meter_logins.main([], submit=submit)
    (line,) = capsys.readouterr().out.splitlines()
    assert "unavailable" in line and "401" in line


def test_a_probe_that_cannot_be_placed_on_spot_runs_on_demand(monkeypatch):
    token = _work_token()
    tried = []

    def submit(token, *, interruptible):
        tried.append(interruptible)
        if interruptible:
            return FakeRun(meter_logins.flyte.errors.MaxQueuedTimeExceededError("never placed"))
        return FakeRun(_payload("claude x", 1.0))

    assert meter_logins.measure(token, submit).five_hour_pct == 1.0
    assert tried == [True, False]

    def never(token, *, interruptible):
        return FakeRun(meter_logins.flyte.errors.MaxQueuedTimeExceededError("never placed"))

    with pytest.raises(RuntimeError, match="could not be placed"):
        meter_logins.measure(token, never)


def test_the_probe_mounts_only_the_work_tokens_secret_and_bounds_the_queue(monkeypatch):
    token = _work_token()
    got = {}

    class Task:
        def override(self, **kwargs):
            got.update(kwargs)
            return "task"

    class Context:
        def run(self, task, **inputs):
            got.update(task=task, inputs=inputs)
            return "run"

    monkeypatch.setattr(meter_logins._factory, "read_login_meter", Task())
    monkeypatch.setattr(meter_logins.flyte, "with_runcontext", lambda **kw: Context())
    assert meter_logins._submit(token, interruptible=True) == "run"
    (secret,) = got["secrets"]
    assert (secret.key, secret.as_env_var) == (token, "CLAUDE_CODE_OAUTH_TOKEN")
    assert got["timeout"].max_queued_time == meter_logins.QUEUE_BOUND
    assert got["inputs"] == {"account": f"claude {token}"}


async def test_the_probe_task_returns_only_the_parsed_meter(monkeypatch):
    token = "sk-ant-oat01-PROBE-TOKEN"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)

    async def read(credentials, account, *, setup_token=False):
        assert credentials == token and setup_token
        return _meter.Meter(account, "oauth-usage", NOW, 12.0, NOW + 5)

    monkeypatch.setattr(_meter, "read_claude", read)
    out = await _factory.read_login_meter.func("claude work")
    assert token not in out and json.loads(out)["five_hour_pct"] == 12.0


# --------------------------------------------------------------------------- #
# meter_report
# --------------------------------------------------------------------------- #

WEEK = 7 * 86400
FIVE_HOURS = 5 * 3600


def synthetic(windows_per_week, account="claude login-a", weeks=3, seed=1, sample_every=900, whole=True):
    """History of a login whose weekly limit holds exactly `windows_per_week` full 5-hour windows."""
    rng = random.Random(seed)
    per_window = 100.0 / windows_per_week  # weekly percent one full window costs
    rows, t0 = [], 1_790_000_000.0
    for week in range(weeks):
        week_end = t0 + (week + 1) * WEEK
        used = 0.0
        start = t0 + week * WEEK
        while start + FIVE_HOURS < week_end - 3600:
            peak = rng.choice([20, 35, 50, 70, 90, 100])
            resets = start + FIVE_HOURS
            for step in range(int(FIVE_HOURS / sample_every) + 1):
                progress = peak * step / int(FIVE_HOURS / sample_every)
                weekly = used + per_window * progress / 100
                rows.append(
                    {
                        "t": start + step * sample_every,
                        "account": account,
                        "source": "oauth-usage",
                        "five_hour": round(progress) if whole else progress,
                        "five_hour_resets_at": resets + rng.uniform(0, 0.5),
                        "weekly": round(weekly) if whole else weekly,
                        "weekly_resets_at": week_end,
                    }
                )
            used += per_window * peak / 100
            start += FIVE_HOURS + rng.choice([2, 6, 10]) * 3600
    return rows


@pytest.mark.parametrize("truth", [10, 20, 35])
def test_the_estimate_recovers_the_synthetic_truth_with_an_honest_range(truth):
    found = meter_report.estimate(synthetic(truth, weeks=4))
    assert found["n_windows"] >= 10 and found["n_samples"] > found["n_windows"]
    assert found["low"] <= found["windows"] <= (found["high"] or float("inf"))
    assert found["low"] <= truth <= (found["high"] or float("inf")), found
    assert abs(found["windows"] - truth) / truth < 0.3, found


def test_exact_readings_give_a_tight_estimate():
    found = meter_report.estimate(synthetic(20, whole=False))
    assert found["windows"] == pytest.approx(20, rel=1e-6)
    assert found["high"] - found["low"] < 0.01


def test_too_little_data_says_so_instead_of_inventing_a_range():
    rows = synthetic(20, weeks=1)
    two = {r["five_hour_resets_at"] // 60 for r in rows}
    few = [r for r in rows if r["five_hour_resets_at"] // 60 in sorted(two)[:2]]
    found = meter_report.estimate(few)
    assert found["n_windows"] <= 2 and found["low"] is None and found["high"] is None
    assert meter_report.estimate([])["windows"] is None
    idle = [dict(r, five_hour=0, weekly=3) for r in rows]
    assert meter_report.estimate(idle)["windows"] is None


def test_the_report_has_one_line_per_account_with_its_sample_counts(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    rows = (
        synthetic(20, "claude login-a")
        + synthetic(40, "claude login-b", seed=2)
        + synthetic(10, "codex", seed=3)
    )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    meter_report.main([str(path)])
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in lines] == ["claude login-a", "claude login-b", "codex"]
    assert (
        "five-hour windows per week (range" in lines[0] and "windows," in lines[0] and "samples" in lines[0]
    )
    assert abs(float(lines[1].split("about ")[1].split()[0]) - 40) < 12


def test_a_task_log_with_meter_lines_is_read_too(tmp_path):
    rows = synthetic(20, weeks=2)
    log = tmp_path / "run.log"
    log.write_text(
        "starting\n"
        + "".join(f"[meter] {json.dumps(r)}\n" for r in rows)
        + "[timeline] 00:00:01 ⚠️ something\n[meter] claude: usage request failed: HTTP 500\n{broken\n"
    )
    assert len(meter_report.read_history([log])) == len(rows)
    assert meter_report.report(meter_report.read_history([log]))[0].startswith("claude login-a: about ")


def test_no_samples(tmp_path, capsys):
    empty = tmp_path / "h.jsonl"
    empty.write_text("")
    meter_report.main([str(empty)])
    assert capsys.readouterr().out.strip() == "no meter samples found"


@pytest.mark.parametrize(
    "bad",
    [
        {"account": [], "t": 1},
        {"account": "a", "t": "1"},
        {"account": "a", "t": 1, "weekly": "bad"},
        {"account": "a", "t": True},
        {"account": "a", "t": 1, "five_hour_resets_at": []},
    ],
)
def test_history_skips_wrong_types_and_counts_them(tmp_path, capsys, bad):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps(bad) + "\n")
    assert meter_report.read_history([path]) == []
    assert "skipped 1 malformed history lines" in capsys.readouterr().err


def test_history_deduplicates_account_timestamp_across_files(tmp_path):
    rows = synthetic(20)
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (one, two):
        path.write_text("\n".join(json.dumps(row) for row in rows))
    assert meter_report.read_history([one, two]) == rows


def test_the_meter_accounts_name_the_claude_token_and_the_codex_token_once_each():
    import dataclasses

    profile = meter_logins._factory_base.PROFILE
    both = dataclasses.replace(profile, secrets=dataclasses.replace(profile.secrets, codex_token="codex_tok"))
    assert list(both.meter_logins) == [f"claude {_work_token()}", "codex codex_tok"]


def test_recent_idle_login_output_avoids_another_probe():
    account = "codex shared"
    meter = _meter.Meter(account, "app-server", time.time(), 10, time.time() + 600)

    def submit(*args, **kwargs):
        raise AssertionError("a fresh login must not be probed twice")

    assert meter_logins.measure(account, submit, cached=meter) is meter


@pytest.mark.parametrize("account,engine", [("codex shared", "codex"), ("codex shared", "claude")])
async def test_the_probe_reads_only_the_claude_token(monkeypatch, account, engine):
    monkeypatch.setenv(_secret_env.CODEX_TOKEN_ENV_VAR, "private-codex-token")

    async def forbidden(*args, **kwargs):
        raise AssertionError("a Codex access token has no usage endpoint to probe")

    monkeypatch.setattr(_meter, "read_codex", forbidden)
    with pytest.raises(ValueError, match="Claude setup-token"):
        await _factory.read_login_meter.func(account, engine=engine)
