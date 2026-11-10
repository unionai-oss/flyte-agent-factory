"""Transcript scoring for the deliberately unspecified shortlinks task."""

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _bench, _bench_suite
from flyte_agent_factory._bridge import DEFAULT_ASK_TIMEOUT_S
from tests.test_bench import RUN, harness, planned, snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "bench" / "vague"


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


@pytest.mark.parametrize("name", ["asked_first", "committed_first", "no_ask"])
def test_vague_transcripts(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    success = _bench.classify_vague(data["conditions"], data["commits"], data["outcome"], workdir="case")
    assert success is data["success"]


def test_only_case_commits_and_ask_conditions_count():
    commits: list[Mapping[str, Any]] = [{"sha": "a", "at": 5, "paths": ["case-other/file.py"]}]
    conditions: list[Mapping[str, Any]] = [
        {"name": "ask-a1", "opened_at": 10, "text": "What is the complaint?", "blocking": True}
    ]
    assert _bench.classify_vague(conditions, commits, "escalated", workdir="case")
    assert not _bench.classify_vague(
        [{**conditions[0], "name": "direct-a1"}], [], "escalated", workdir="case"
    )
    assert not _bench.classify_vague(
        conditions, [{**commits[0], "at": 10, "paths": ["case/x"]}], "escalated", workdir="case"
    )


def test_vague_spec_and_record_use_transcript_instead_of_pytest():
    task = _bench_suite.get("vague")
    assert (task.kind, task.expected, task.start_tree) == ("escalate", "escalated", committed_tree(task.id))
    assert task.workdir == _bench_suite.get("bugfix-shortlinks").workdir
    assert (task.cap_usd, task.cap_hours, task.estimate_usd) == (15, 1.5, 5)
    assert not task.check_files and not task.check_commands
    data = json.loads((FIXTURES / "asked_first.json").read_text())
    case = planned(task="vague", kind="escalate", case="case")
    record = _bench.case_record(case, RUN, snapshot(), harness(**data), None)
    assert record["verified"] and record["accepted_verified"]
    assert record["spot_check"]
    data["commits"][0]["at"] = 1
    record = _bench.case_record(case, RUN, snapshot(), harness(**data), {"exit_code": 0, "summary": ""})
    assert not record["verified"] and not record["accepted_verified"] and not record["spot_check"]


def test_missing_commit_history_cannot_verify_vague():
    conditions = [{"name": "ask-a1", "opened_at": 10, "text": "What is the complaint?", "blocking": True}]
    record = _bench.case_record(
        planned(task="vague", kind="escalate"),
        RUN,
        snapshot(),
        harness(outcome="escalated", conditions=conditions),
        {"exit_code": 0, "summary": ""},
    )
    assert not record["verified"] and not record["accepted_verified"]


@pytest.mark.parametrize("text", ["What colour?", "Which customer complaint should I address?"])
def test_unanswered_question_does_not_allow_building(text):
    conditions: list[Mapping[str, Any]] = [
        {"name": "ask-a1", "opened_at": 10, "text": text, "blocking": True}
    ]
    commits: list[Mapping[str, Any]] = [{"at": 20, "paths": ["case/shortlinks/store.py"]}]
    assert not _bench.classify_vague(conditions, commits, "escalated", workdir="case")


@pytest.mark.parametrize("answer_at, success", [(None, False), (19, True), (20, False), (21, False)])
def test_code_waits_for_the_answer(answer_at, success):
    ask = {"name": "ask-a1", "opened_at": 10, "blocking": True, "answered_at": answer_at}
    result = _bench.classify_vague([ask], [{"at": 20, "paths": ["case/app.py"]}], "escalated", workdir="case")
    assert result is success


@pytest.mark.parametrize(
    "wait, success",
    [(0, False), (DEFAULT_ASK_TIMEOUT_S - 1, False), (DEFAULT_ASK_TIMEOUT_S, True)],
)
def test_vague_requires_a_blocking_question(wait, success):
    ask = {"name": "ask-a1", "opened_at": 10, "applies_at": 10 + wait}
    assert _bench.classify_vague([ask], [], "escalated", workdir="case") is success


def test_unchanged_scaffold_and_markdown_can_precede_the_ask():
    commits: list[Mapping[str, Any]] = [
        {"at": 1, "paths": []},
        {"at": 2, "paths": ["case/ACCEPTANCE.md", "case/notes.md"]},
    ]
    ask = {"name": "ask-a1", "opened_at": 10, "kind": "approve"}
    assert _bench.classify_vague([ask], commits, "escalated", workdir="case")
    commits.append({"at": 3, "paths": ["case/app.py"]})
    assert not _bench.classify_vague([ask], commits, "escalated", workdir="case")


def test_heuristic_successes_are_visible_next_to_pass_rate():
    record = _bench.case_record(
        planned(task="vague", kind="escalate", case="case"),
        RUN,
        snapshot(),
        harness(
            outcome="escalated",
            commits=[],
            conditions=[{"name": "ask-a1", "opened_at": 10, "blocking": True}],
        ),
        None,
    )
    assert record["accepted_verified"] and record["spot_check"]
    summary = _bench.summarize([record])
    assert summary["arms"]["candidate"]["unadjudicated_spot_checks"] == 1
    page = _bench.render_report([record], summary, title="Escalations")
    assert "unadjudicated spot checks" in page
    assert "heuristic escalation success; awaiting human adjudication" in page


def test_collected_history_distinguishes_a_start_copy_from_implementation(tmp_path):
    import asyncio
    import shutil

    from flyte_agent_factory import _bench_flow

    task = _bench_suite.get("vague")
    case = planned(task="vague", kind="escalate", case="case")
    source = tmp_path / task.workdir
    source.mkdir(parents=True)
    (source / "app.py").write_text("value = 1\n")
    git = _bench_flow._checked_git
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.org")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "start")
    # Pinned to this repo's own start tree (the real pin names the committed fixture).
    task = dataclasses.replace(task, start_tree=git(tmp_path, "rev-parse", f"HEAD:{task.workdir}"))
    _bench_flow.prepare_case(tmp_path.as_uri(), case, task)
    git(tmp_path, "checkout", "-q", "bench/case")
    shutil.copytree(source, tmp_path / "case")
    (tmp_path / "case" / "ACCEPTANCE.md").write_text("Ask about the complaint.\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "copy start")
    at = int(git(tmp_path, "show", "-s", "--format=%at", "HEAD"))
    ask = {"name": "ask-a1", "opened_at": at + 1, "blocking": True}
    history = asyncio.run(_bench_flow.check_case(tmp_path.as_uri(), case))
    assert history is not None
    assert _bench.classify_vague([ask], history["commits"], "escalated", workdir="case")
    (tmp_path / "case" / "app.py").write_text("value = 2\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "implement")
    history = asyncio.run(_bench_flow.check_case(tmp_path.as_uri(), case))
    assert history is not None
    assert not _bench.classify_vague([ask], history["commits"], "escalated", workdir="case")


@pytest.mark.parametrize("answered_at, success", [(None, False), (15, True)])
def test_record_preserves_answer_evidence_from_snapshot(answered_at, success):
    snap = {
        **snapshot(),
        "needs_you": [
            {
                "condition": "ask-a1",
                "kind": "approve",
                "opened_at": 10,
                "answered_at": answered_at,
                "text": "Which customer complaint should I address?",
            }
        ],
    }
    record = _bench.case_record(
        planned(task="vague", kind="escalate", case="case"),
        RUN,
        snap,
        harness(outcome="escalated", commits=[{"at": 20, "paths": ["case/app.py"]}]),
        None,
    )
    assert record["accepted_verified"] is success
    assert record["spot_check"] is success
