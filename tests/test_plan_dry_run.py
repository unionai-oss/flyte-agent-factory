"""`ops/plan_dry_run.py`: the proposal as JSON from supplied facts, and which slots count as capacity."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from flyte_agent_factory import _meter, _planner

OPS = Path(__file__).resolve().parents[1] / "ops"
spec = importlib.util.spec_from_file_location("ops_plan_dry_run", OPS / "plan_dry_run.py")
assert spec is not None and spec.loader is not None
plan_dry_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_dry_run)
_factory = plan_dry_run._factory
_factory_base = plan_dry_run._factory_base

NOW = 1_800_000_000.0


def live_run(task):
    return SimpleNamespace(
        name=f"run-{task}", to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}}
    )


def test_free_slots_give_the_one_claude_token_each_and_the_codex_token():
    base = _factory_base.PROFILE
    profile = dataclasses.replace(
        base, secrets=dataclasses.replace(base.secrets, codex_token="codex_token_x")
    )
    token = profile.secrets.claude_token
    claude = _meter.Meter(f"claude {token}", "test", NOW, five_hour_pct=10)
    codex = _meter.Meter("codex", "test", NOW, five_hour_pct=20)
    meters = {profile.slots[0].task: [claude], profile.slots[1].task: [codex]}
    found = plan_dry_run.free_capacity(profile, [], meters)
    # Every slot runs on the same token, so each one is offered it and the one reading is the token's.
    assert [(c.slot, c.login) for c in found if c.engine == "claude"] == [
        (slot.task, token) for slot in profile.slots
    ]
    by_slot = {(c.slot, c.engine): c for c in found}
    assert all(by_slot[slot.task, "claude"].meter is claude for slot in profile.slots)
    assert all(by_slot[slot.task, "codex"].meter is codex for slot in profile.slots)
    assert {c.login for c in found if c.engine == "codex"} == {"codex_token_x"}
    busy = plan_dry_run.free_capacity(profile, [live_run(profile.slots[0].task)], meters)
    assert [(c.slot, c.login) for c in busy if c.engine == "claude"] == [(profile.slots[1].task, token)]
    assert plan_dry_run.free_capacity(profile, [live_run(s.task) for s in profile.slots], meters) == []


def test_a_profile_without_a_codex_token_offers_only_claude():
    found = plan_dry_run.free_capacity(_factory_base.PROFILE, [], {})
    assert {c.engine for c in found} == {"claude"}


def test_the_dry_run_prints_the_proposal_as_json_and_submits_nothing(monkeypatch, capsys):
    item = _planner.BoardItem("I1", 1, "example/repo", status="Ready", body="## Files\n- a")
    blocked = _planner.BoardItem("I2", 2, "example/repo", status="Ready", body="## Files\n- a")
    facts = _planner.Facts(
        items=(item, blocked),
        capacity=(_planner.Capacity("claude", "login"), _planner.Capacity("gemini", "odd")),
        now=NOW,
    )
    monkeypatch.setattr(_factory.flyte, "with_runcontext", lambda **kw: 1 / 0)  # any submission fails
    plan_dry_run.main([], read=lambda profile: facts)
    out = json.loads(capsys.readouterr().out)
    assert [(x["item"], x["engine"], x["predicted_files"]) for x in out["launches"]] == [
        ("example/repo#1", "claude", ["a"])
    ]
    assert out["waiting"] == [
        {"item": "example/repo#2", "reason": "file_conflict", "with": "I1", "detail": ""}
    ]
    assert out["unusable_slots"] == [{"login": "odd"}]
