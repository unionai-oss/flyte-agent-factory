"""Judge outcomes in a review round: a missing verdict is never a FAIL and never a pass (#169, #87, #151)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _checkpoint,
    _contract,
    _dashboard,
    _factory_status,
    _policy,
    _review,
    _reviewer,
    _router,
    _session_config,
    _timeline,
    _usage,
)

PASS = "fine\nVERDICT: PASS"
FAIL_WITH_EVIDENCE = "FINDING: broken\nEvidence: missing: app/out.txt\nVERDICT: FAIL"


# --- verdict parsing ---


@pytest.mark.parametrize(
    ("line", "word"),
    [
        ("VERDICT: PASS", "PASS"),
        ("verdict: fail", "FAIL"),
        ("**VERDICT:** PASS", "PASS"),
        ("**VERDICT: PASS**", "PASS"),
        ("`VERDICT: PASS`", "PASS"),
        ("> VERDICT: PASS", "PASS"),
        ("   VERDICT: PASS", "PASS"),
        ("## VERDICT: FAIL", "FAIL"),
        ("VERDICT - PASS", "PASS"),
        ("VERDICT — FAIL", "FAIL"),
        ("VERDICT: PASS.", "PASS"),
        ("VERDICT: PASS (with notes)", "PASS"),
        ("**VERDICT:** `FAIL`", "FAIL"),
    ],
)
def test_verdict_drift_forms_parse(line, word):
    found = _review._VERDICT.findall(f"report\n{line}\n")
    assert [w.upper() for w in found] == [word]


def test_a_verdict_line_ending_in_crlf_parses():
    assert _review._VERDICT.findall("ok\r\nVERDICT: PASS\r\n") == ["PASS"]


@pytest.mark.parametrize(
    "line",
    [
        "VERDICT: PASS or FAIL, I could not tell",
        "The VERDICT: PASS line is what the author claims",
        "VERDICT: maybe PASS",
        "VERDICT PASS",
        "VERDICT:",
    ],
)
def test_a_line_that_only_mentions_a_verdict_is_not_one(line):
    assert not _review._VERDICT.search(line)


def test_the_last_verdict_line_wins():
    text = "the author wrote:\n> VERDICT: PASS\nbut\n**VERDICT: FAIL**"
    assert _review._VERDICT.findall(text)[-1] == "FAIL"


# --- classifying a missing verdict ---


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "no output"),
        ("   \n", "no output"),
        (_reviewer._NO_FINAL_MESSAGE, "no output"),
        ('{"type":"result","subtype":"error_max_turns","is_error":false}', "turn limit"),
        (f"{_usage.USAGE_LIMIT_MARK} resets 5pm", "tool or limit error"),
        ("API Error: 529 overloaded", "tool or limit error"),
        ("You've hit a rate limit", "tool or limit error"),
        ("I looked around and it seems fine.", "unparseable"),
        ("API Error: 429 rate limit exceeded", "tool or limit error"),
        ("I reviewed the rate-limit handling and the retry.\n" + "analysis. " * 200, "unparseable"),
        ("The rate limit handling reads well, but I ran out of time.", "unparseable"),
        ("Overall tidy and well named.\nRate limits are parsed in one place, which is good.", "unparseable"),
        ("Rate-limit handling is clean; names are verb phrases.\n- Minor: one long function.", "unparseable"),
        (
            "Craft notes:\n- Naming is clear.\nRate limit handling in _router reads well; nothing to change.",
            "unparseable",
        ),
        ("Looks fine overall.\nError: handling lives in one place.", "unparseable"),
        ("[API Error: 529 overloaded]", "tool or limit error"),
        (f"Reviewing the retry.\n{_usage.USAGE_LIMIT_MARK} quoted in docs/operating.md", "unparseable"),
    ],
)
def test_no_verdict_is_classified(text, reason):
    assert _review._classify_no_verdict(text) == reason


@pytest.mark.parametrize("blocking", [False, True])
def test_a_report_with_a_verdict_that_mentions_a_rate_limit_is_usable(blocking):
    text = "Clean, well-named code. The rate limit handling reads well.\nVERDICT: PASS"
    assert _review._find_usable_result_gap(text, advisory_craft=not blocking) == ""


# --- driving _evaluate ---


class Reviewers:
    """A scripted `_call_reviewer`: judge -> replies (an Exception is raised); the last one repeats."""

    def __init__(self, replies: dict[str, list[Any]], engine: str = "claude") -> None:
        self.replies = replies
        self.engine = engine
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, *, brief, on_engine=None, engine=None, **_):
        judge = next(j for j, _ in _review._JUDGES if f"**{j}** reviewer" in brief)
        self.calls.append((judge, engine))
        if on_engine:
            on_engine(engine or self.engine, "label")
        script = self.replies.get(judge, [PASS])
        n = sum(1 for j, _ in self.calls if j == judge)
        reply = script[min(n, len(script)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply, None

    def count(self, judge: str) -> int:
        return sum(1 for j, _ in self.calls if j == judge)


@pytest.fixture
def run(monkeypatch):
    sha = {"value": "a" * 40}

    async def git_out(*_a, **_k):
        return 0, sha["value"]

    async def upload(_path):
        return object()

    async def facts():
        return ""

    async def push(*_a, **_k):
        return ""

    async def contract(*_a, **_k):
        return None

    monkeypatch.setattr(_checkpoint, "_git_out", git_out)
    monkeypatch.setattr(_checkpoint, "_upload_workspace", upload)
    monkeypatch.setattr(_review, "_review_facts", facts)
    monkeypatch.setattr(_review, "_push_fact", push)
    monkeypatch.setattr(_contract, "_load_or_freeze_contract", contract)
    monkeypatch.setattr(Dir, "from_local", upload)
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))

    def go(reviewers, round_=1, timeline=None):
        monkeypatch.setattr(_reviewer, "_call_reviewer", reviewers)
        timeline = timeline or _timeline._Timeline()
        return _review._evaluate(Path("."), "brief", "", round_, timeline)

    go.sha = sha  # type: ignore[attr-defined]
    return go


def report_rows(timeline) -> list[dict[str, str]]:
    return [{"icon": icon, "text": text} for _, icon, text in timeline._rows]


def rows(timeline) -> list[str]:
    return [text for _, _, text in timeline._rows]


@pytest.mark.asyncio
async def test_a_judge_with_no_verdict_once_is_retried_and_the_round_passes(run):
    reviewers = Reviewers({"completeness": ["I checked everything, looks good.", PASS]})
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed
    assert reviewers.count("completeness") == 2
    assert reviewers.count("correctness") == reviewers.count("craft") == 1
    text = "\n".join(rows(timeline))
    assert "NO VERDICT (unparseable; attempt 1/3" in text
    assert "treating as FAIL" not in text
    assert timeline.no_verdict["by_judge"] == {"completeness": 1}
    assert timeline.judges["completeness"]["attempts"] == 2
    # The retry's PASS replaces the NO VERDICT row for the dashboard.
    assert _dashboard.latest_verdicts(report_rows(timeline))[1]["completeness"] == "PASS"


@pytest.mark.asyncio
async def test_a_judge_with_no_verdict_on_every_attempt_is_incomplete_and_re_run_alone(run):
    reviewers = Reviewers({"completeness": [_reviewer._NO_FINAL_MESSAGE]})
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewIncomplete) as raised:
        await run(reviewers, timeline=timeline)
    assert list(raised.value.missing) == ["completeness"]
    assert "no output" in raised.value.missing["completeness"]
    assert reviewers.count("completeness") == _review._NO_VERDICT_ATTEMPTS
    assert not timeline.verdicts
    assert timeline.carried.keys() == {"correctness", "craft"}

    again = Reviewers({"completeness": [PASS]})
    passed, _ = await run(again, timeline=timeline)
    assert passed
    assert [j for j, _ in again.calls] == ["completeness"]
    assert "judge-correctness: PASS (carried from round 1, same commit)" in rows(timeline)
    assert len(timeline.verdicts) == 1
    assert not timeline.carried


@pytest.mark.asyncio
async def test_carried_verdicts_survive_a_restart_during_the_wait(run):
    soon = time.time() + 3600
    deferred = Reviewers(
        {"craft": [_router.RouterDeferred(f"{_usage.USAGE_LIMIT_MARK} full", retry_at=soon)]}
    )
    with pytest.raises(_reviewer.ReviewLimited):
        await run(deferred, timeline=_timeline._Timeline())
    # A pod restart: nothing in memory, the timeline rebuilt from the checkpointed metrics file.
    restarted = _timeline._Timeline()
    assert restarted.carried.keys() == {"correctness", "completeness"}
    again = Reviewers({})
    passed, _ = await run(again, timeline=restarted)
    assert passed and [j for j, _ in again.calls] == ["craft"]
    assert "judge-completeness: PASS (carried from round 1, same commit)" in rows(restarted)
    assert not _timeline._Timeline().carried


@pytest.mark.asyncio
async def test_carried_verdicts_that_could_not_be_saved_are_said_to_be_lost_on_restart(run, monkeypatch):
    def unwritable(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(_timeline, "_write_atomic", unwritable)
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewIncomplete):
        await run(Reviewers({"completeness": [""]}), timeline=timeline)
    assert any("could not be saved" in r and "restart" in r for r in rows(timeline))


def test_a_carried_report_is_bounded():
    timeline = _timeline._Timeline()
    outcomes = {"craft": _review._JudgeOutcome("ADVISORY")}
    own = {"craft": (["u"] * 50, ["n"] * 50, ["c"] * 50)}
    _review._keep_carried("a" * 40, 1, outcomes, {"craft": "x" * 20000}, own, {}, timeline)
    kept = timeline.carried["craft"]
    assert len(kept["report"]) <= 8000
    assert all(len(kept[k]) <= 30 for k in ("unverified", "nonblocking", "conflicts"))


@pytest.mark.asyncio
async def test_a_changed_commit_re_runs_every_judge(run):
    with pytest.raises(_reviewer.ReviewIncomplete):
        await run(Reviewers({"completeness": [""]}))
    run.sha["value"] = "b" * 40
    again = Reviewers({})
    timeline = _timeline._Timeline()
    passed, _ = await run(again, timeline=timeline)
    assert passed
    assert sorted(j for j, _ in again.calls) == ["completeness", "correctness", "craft"]
    assert sum("commit changed" in r for r in rows(timeline)) == 1


@pytest.mark.asyncio
async def test_craft_deferred_and_correctness_limited_is_never_a_pass(run):
    soon = time.time() + 600
    long_text = f"{_usage.USAGE_LIMIT_MARK} " + "all engines are full; " * 20 + "retry at 14:05 UTC"
    assert len(long_text) > 200
    reviewers = Reviewers(
        {
            "craft": [_router.RouterDeferred(long_text, retry_at=soon)],
            "correctness": [RuntimeError(f"{_usage.USAGE_LIMIT_MARK} resets 5pm (UTC)")],
        }
    )
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        await run(reviewers, timeline=timeline)
    assert raised.value.retry_at == soon
    assert set(raised.value.missing) == {"craft", "correctness"}
    assert raised.value.reset_text.endswith("retry at 14:05 UTC")
    assert not timeline.verdicts and not _session_config._round_scores
    assert timeline.carried.keys() == {"completeness"}
    assert any(r.startswith("judge-craft: DEFERRED (") and len(r) < 220 for r in rows(timeline))
    assert _dashboard.latest_verdicts(report_rows(timeline))[1]["craft"] == "DEFERRED"


@pytest.mark.asyncio
async def test_a_deferral_with_a_late_retry_at_is_not_the_one_chosen(run):
    reviewers = Reviewers(
        {
            "craft": [_router.RouterDeferred(f"{_usage.USAGE_LIMIT_MARK} late", retry_at=2000.0)],
            "correctness": [_router.RouterDeferred(f"{_usage.USAGE_LIMIT_MARK} early", retry_at=1000.0)],
        }
    )
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        await run(reviewers)
    assert raised.value.retry_at == 1000.0 and raised.value.reset_text == "early"


@pytest.mark.asyncio
async def test_a_deferral_mixed_with_no_verdict_waits_for_the_reset(run):
    reviewers = Reviewers(
        {
            "craft": [_router.RouterDeferred(f"{_usage.USAGE_LIMIT_MARK} full", retry_at=5.0)],
            "completeness": [""],
        }
    )
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        await run(reviewers)
    assert set(raised.value.missing) == {"craft", "completeness"}


@pytest.mark.asyncio
async def test_every_judge_not_run_is_unavailable(run):
    boom = RuntimeError("pod never scheduled")
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewUnavailable, match="none of the reviewers could run"):
        await run(Reviewers({j: [boom] for j, _ in _review._JUDGES}), timeline=timeline)
    # Nothing re-runs on this path (a human is told), so the row promises no re-run.
    assert "QA round 1 undecided: none of the reviewers could run" in rows(timeline)
    assert not any("re-runs for those judges" in r for r in rows(timeline))


@pytest.mark.asyncio
async def test_a_timeout_is_not_run_with_that_reason(run):
    reviewers = Reviewers({"craft": [TimeoutError("took too long")]})
    timeline = _timeline._Timeline()
    with pytest.raises(_reviewer.ReviewIncomplete) as raised:
        await run(reviewers, timeline=timeline)
    assert raised.value.missing == {"craft": "NOT_RUN: timeout"}
    assert "judge-craft: NOT RUN (timeout)" in rows(timeline)


@pytest.mark.asyncio
async def test_a_fail_with_evidence_stands_and_names_the_judge_without_a_verdict(run):
    reviewers = Reviewers({"correctness": [FAIL_WITH_EVIDENCE], "completeness": [""]})
    timeline = _timeline._Timeline()
    passed, findings = await run(reviewers, timeline=timeline)
    assert passed is False
    assert "broken" in findings
    assert "### completeness reviewer: NO VERDICT (no output)" in findings
    assert timeline.verdicts[-1]["verdict"] == "FAIL"
    assert not timeline.carried
    assert "completeness reviewer" not in "\n".join(timeline.open_findings)


@pytest.mark.asyncio
async def test_the_retry_goes_to_the_other_engine_when_there_is_one(run, monkeypatch):
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda engine: False)
    reviewers = Reviewers({"craft": ["", PASS]})
    await run(reviewers)
    assert [e for j, e in reviewers.calls if j == "craft"] == [None, "codex"]


@pytest.mark.asyncio
async def test_an_exhausted_other_engine_and_a_hard_pin_keep_the_routing(run, monkeypatch):
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude", "codex"))
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda engine: engine == "codex")
    reviewers = Reviewers({"craft": ["", PASS]})
    await run(reviewers)
    assert [e for j, e in reviewers.calls if j == "craft"] == [None, None]
    monkeypatch.setattr(_usage, "_engine_exhausted", lambda engine: False)
    monkeypatch.setattr(_usage, "_hard_pins", True)
    pinned = Reviewers({"craft": ["", PASS]})
    await run(pinned)
    assert [e for j, e in pinned.calls if j == "craft"] == [None, None]


@pytest.mark.asyncio
async def test_with_only_claude_the_retry_runs_on_claude(run):
    reviewers = Reviewers({"completeness": ["", PASS]}, engine="claude")
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("completeness") == 2
    assert timeline.judges["completeness"]["engine"] == "claude"
    assert timeline.no_verdict["by_engine"] == {"claude": 1}


@pytest.mark.asyncio
async def test_an_advisory_craft_with_no_output_is_no_verdict_and_a_report_is_enough(run):
    with pytest.raises(_reviewer.ReviewIncomplete) as raised:
        await run(Reviewers({"craft": [""]}))
    assert list(raised.value.missing) == ["craft"]
    assert "craft" not in _timeline._Timeline().carried  # what a restart would find
    timeline = _timeline._Timeline()
    passed, _ = await run(Reviewers({"craft": ["Looks tidy, no verdict line."]}), timeline=timeline)
    assert passed and timeline.judges["craft"]["outcome"] == "ADVISORY"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["API Error: 529 overloaded", f"{_usage.USAGE_LIMIT_MARK} resets 5pm UTC"])
async def test_an_advisory_craft_that_only_reports_an_error_is_no_verdict(run, text):
    with pytest.raises(_reviewer.ReviewIncomplete) as raised:
        await run(Reviewers({"craft": [text]}))
    assert "tool or limit error" in raised.value.missing["craft"]
    # A real craft review may mention a rate limit in passing and is still a report.
    review = "Naming is consistent.\n" * 40 + "The retry handles an API Error from the rate limiter well."
    timeline = _timeline._Timeline()
    passed, _ = await run(Reviewers({"craft": [review]}), timeline=timeline)
    assert passed and timeline.judges["craft"]["outcome"] == "ADVISORY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        "Overall tidy and well named.\nRate limits are parsed in one place, which is good.",
        "Rate-limit handling is clean; names are verb phrases.\n- Minor: one long function.",
        "Craft notes:\n- Naming is clear.\nRate limit handling in _router reads well; nothing to change.",
    ],
)
async def test_an_advisory_craft_report_that_opens_a_line_with_rate_limit_is_a_report(run, report):
    reviewers = Reviewers({"craft": [report]})
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("craft") == 1
    assert timeline.judges["craft"]["outcome"] == "ADVISORY"
    assert not any("NO VERDICT" in row for row in rows(timeline))


@pytest.mark.asyncio
async def test_an_advisory_craft_report_that_mentions_a_rate_limit_is_a_report(run):
    reviewers = Reviewers(
        {"craft": ["Clean, well-named code. The rate limit handling reads well.\nVERDICT: PASS"]}
    )
    timeline = _timeline._Timeline()
    passed, _ = await run(reviewers, timeline=timeline)
    assert passed and reviewers.count("craft") == 1
    assert timeline.judges["craft"]["outcome"] == "ADVISORY"


@pytest.mark.asyncio
async def test_a_carried_advisory_craft_is_re_run_once_craft_is_blocking(run, monkeypatch):
    with pytest.raises(_reviewer.ReviewIncomplete):
        await run(Reviewers({"completeness": [""]}))
    assert _timeline._Timeline().carried["craft"]["outcome"] == "ADVISORY"
    read = _policy.read
    monkeypatch.setattr(_policy, "read", lambda k, d="": "true" if k == "CRAFT_BLOCKING" else read(k, d))
    again = Reviewers(
        {"craft": ["FINDING: x\nEvidence: missing: a.txt\nVERDICT: FAIL"], "completeness": [PASS]}
    )
    passed, _ = await run(again, timeline=_timeline._Timeline())
    assert sorted(j for j, _ in again.calls) == ["completeness", "craft"]
    assert not passed


@pytest.mark.asyncio
async def test_an_undecided_round_leaves_the_next_round_its_changed_files(run):
    timeline = _timeline._Timeline()
    timeline.last_round_head = "0" * 40
    with pytest.raises(_reviewer.ReviewIncomplete):
        await run(Reviewers({"completeness": [""]}), timeline=timeline)
    # The decided re-run diffs from where the last decided round was judged, not from the undecided one.
    assert timeline.last_round_head == "0" * 40


# --- state, status and the dashboard ---


def test_judges_and_counts_are_in_the_snapshot_and_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    timeline.record_judge_outcome(
        "craft", outcome="NO_VERDICT", reason="no output", engine="claude", attempts=3
    )
    timeline.count_no_verdict("craft", "claude")
    timeline.count_no_verdict("craft", "codex")
    review = timeline.snapshot()["review"]
    assert review["judges"]["craft"]["outcome"] == "NO_VERDICT"
    assert review["no_verdict"] == {"by_judge": {"craft": 2}, "by_engine": {"claude": 1, "codex": 1}}
    timeline._save_metrics()
    restored = _timeline._Timeline()
    assert restored.no_verdict == timeline.no_verdict and restored.judges == timeline.judges


def test_an_old_metrics_file_without_the_new_keys_still_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    timeline = _timeline._Timeline()
    timeline.last_round_head = "abc"
    state = timeline._metrics_state()
    del state["judges"], state["no_verdict"], state["carried"]
    path = timeline._metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps(state))
    restored = _timeline._Timeline()
    assert restored.last_round_head == "abc"
    assert restored.judges == {} and restored.no_verdict == {"by_judge": {}, "by_engine": {}}
    assert restored.carried == {}
    # A file that has the judges but not the carried verdicts keeps its judges.
    timeline.judges = {"craft": {"outcome": "PASS"}}
    state = timeline._metrics_state()
    del state["carried"]
    path.write_text(json.dumps(state))
    restored = _timeline._Timeline()
    assert restored.judges == {"craft": {"outcome": "PASS"}} and restored.carried == {}


def test_factory_status_passes_judges_through():
    data = {
        "review": {
            "judges": {"craft": {"outcome": "DEFERRED"}},
            "no_verdict": {"by_judge": {"craft": 1}, "by_engine": {}},
        }
    }
    review = _factory_status._review(data)
    assert review["judges"]["craft"]["outcome"] == "DEFERRED"
    assert review["no_verdict"]["by_judge"] == {"craft": 1}


def test_latest_verdicts_maps_the_new_rows():
    rows_ = [
        {"icon": "🔍", "text": "QA round 2: 3 reviewers"},
        {"icon": "⚠️", "text": "judge-correctness: NO VERDICT (no output; attempt 3/3)"},
        {"icon": "⏸️", "text": "judge-craft: DEFERRED (all engines are full)"},
        {"icon": "⚠️", "text": "judge-completeness: NOT RUN (timeout)"},
    ]
    assert _dashboard.latest_verdicts(rows_) == (
        2,
        {"correctness": "NO VERDICT", "craft": "DEFERRED", "completeness": "NOT RUN"},
    )
    carried = [
        {"icon": "🔍", "text": "QA round 2: 3 reviewers"},
        {"icon": "✅", "text": "judge-correctness: PASS (carried from round 2, same commit)"},
        {"icon": "⚠️", "text": "completeness: no verdict, treating as FAIL"},
    ]
    assert _dashboard.latest_verdicts(carried)[1] == {"correctness": "PASS", "completeness": "FAIL"}


@pytest.mark.asyncio
async def test_a_contract_freeze_deferral_keeps_the_routers_retry_time(monkeypatch):
    retry_at = time.time() + 4 * 3600
    text = f"{_usage.USAGE_LIMIT_MARK} " + "x" * 230 + "; retry at 06:00 UTC"

    async def deferred(*_a, **_k):
        raise _router.RouterDeferred(text, retry_at=retry_at)

    monkeypatch.setattr(_reviewer, "_call_reviewer", deferred)
    monkeypatch.setattr(_policy, "fallback", lambda _setting: None)
    with pytest.raises(_reviewer.ReviewLimited) as raised:
        await _contract._freeze_contract(cast(Dir, object()), "brief", "", _timeline._Timeline())
    assert raised.value.retry_at == retry_at
    assert "retry at 06:00 UTC" in raised.value.reset_text


@pytest.mark.asyncio
async def test_a_contract_freeze_deferral_does_not_move_the_decided_head(run, monkeypatch):
    async def limited(*_a, **_k):
        raise _reviewer.ReviewLimited("x")

    monkeypatch.setattr(_contract, "_load_or_freeze_contract", limited)
    timeline = _timeline._Timeline()
    timeline.last_round_head = "b" * 40
    with pytest.raises(_reviewer.ReviewLimited):
        await run(Reviewers({}), timeline=timeline)
    assert timeline.last_round_head == "b" * 40
