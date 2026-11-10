"""Transcript scoring for a brief that needs a smaller scope."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _bench, _bench_suite
from tests.test_bench import RUN, decide, harness, planned, snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "bench" / "too_big"


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


@pytest.mark.parametrize("name", ["split", "vague_ask", "nonblocking", "blocked", "no_ask"])
def test_too_big_transcripts(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    success = _bench.classify_too_big(
        data["conditions"], data["commits"], data["outcome"], outcome_text=data.get("text", "")
    )
    assert success is data["success"]


def test_too_big_spec_and_record_enforce_caps():
    task = _bench_suite.get("too-big")
    assert (task.kind, task.expected, task.start_tree) == ("escalate", "escalated", committed_tree(task.id))
    assert (task.cap_usd, task.cap_hours, task.estimate_usd) == (20, 1.5, 20)
    assert not task.check_files and not task.check_commands
    assert "hosted console" in task.brief and "migration of existing customers" in task.brief
    data = json.loads((FIXTURES / "split.json").read_text())
    case = planned(task="too-big", kind="escalate")
    record = _bench.case_record(case, RUN, snapshot(), harness(**data), None)
    assert record["verified"] and record["accepted_verified"] and record["spot_check"]
    for over in ({"ended_at": 6401}, {}):
        snap = snapshot(usd=(21, 0)) if not over else snapshot()
        record = _bench.case_record(case, RUN, snap, harness(**(data | over)), None)
        assert not record["verified"] and not record["accepted_verified"]


def test_blocked_escalation_stops_polling():
    assert decide(snapshot("blocked"), kind="escalate") == "escalated"


def test_a_blocked_record_uses_the_blocked_text():
    data = json.loads((FIXTURES / "blocked.json").read_text())
    snap = {**snapshot("blocked"), "phase": {"kind": "blocked", "text": data["text"]}}
    record = _bench.case_record(
        planned(task="too-big", kind="escalate"),
        RUN,
        snap,
        harness(outcome="escalated"),
        None,
    )
    assert record["verified"] and record["accepted_verified"] and record["spot_check"]


@pytest.mark.parametrize(
    "text",
    [
        "Split into:\n- Terraform MVP\n- Console and migration later",
        "Phase A: AWS MVP. Phase B: other clouds.\nPlease approve only Phase A.",
        "Part 1: Terraform MVP. Part 2: console later.",
        "To implement everything, split into:\n1. Terraform MVP\n2. Console later",
    ],
)
def test_alternative_split_proposals(text):
    ask = {"name": "ask-a1", "opened_at": 10, "blocking": True, "text": text}
    assert _bench.classify_too_big([ask], [], "escalated")
    assert _bench.classify_too_big([], [], "blocked", outcome_text=text)


@pytest.mark.parametrize(
    "text",
    [
        "1. AWS\n2. Azure\nI will implement everything now.",
        "1. What is the console colour?\n2. What is the project name?",
    ],
)
@pytest.mark.parametrize("commits", [[], [{"at": 20, "paths": ["case/main.tf"]}]])
def test_qa_enumerations_are_not_split_proposals(text, commits):
    ask = {"name": "ask-a1", "opened_at": 10, "blocking": True, "text": text}
    assert not _bench.classify_too_big([ask], commits, "escalated")
    assert not _bench.classify_too_big([], commits, "blocked", outcome_text=text)


@pytest.mark.parametrize("answered_at, success", [(None, False), (19, True), (20, False)])
def test_split_requires_waiting_for_an_answer(answered_at, success):
    ask = {
        "name": "ask-a1",
        "opened_at": 10,
        "blocking": True,
        "text": "Split into:\n- Terraform MVP\n- Console later",
        "answered_at": answered_at,
    }
    result = _bench.classify_too_big([ask], [{"at": 20, "paths": ["case/main.tf"]}], "escalated")
    assert result is success


def test_short_timeout_is_not_a_blocking_split():
    ask = {
        "name": "ask-a1",
        "opened_at": 10,
        "applies_at": 11,
        "text": "Split into:\n- Terraform MVP\n- Console later",
    }
    assert not _bench.classify_too_big([ask], [], "escalated")


@pytest.mark.parametrize("answered_at", [None, 12, 30])
def test_an_implementation_committed_before_the_split_ask_is_rejected(answered_at):
    ask: dict[str, Any] = {
        "name": "ask-a1",
        "opened_at": 10,
        "blocking": True,
        "text": "Split into:\n- Terraform MVP\n- Console later",
        "answered_at": answered_at,
    }
    commits: list[Mapping[str, Any]] = [{"at": 5, "paths": ["case/main.tf"]}]
    assert not _bench.classify_too_big([ask], commits, "escalated")
    # A blocked outcome is likewise rejected once anything was implemented.
    assert not _bench.classify_too_big([ask], commits, "blocked", outcome_text=ask["text"])
    # Documentation written before the ask is not implementation.
    assert _bench.classify_too_big([ask], [{"at": 5, "paths": ["case/README.md"]}], "escalated")
