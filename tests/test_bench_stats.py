"""Offline fixtures for the benchmark's paired statistics and decision rule."""

import math

import pytest

from flyte_agent_factory import _bench_stats as stats


def cases(tasks: int = 6, repeats: int = 2, ratio: float = 1.0) -> list[stats.CaseRecord]:
    return [
        stats.CaseRecord(
            task=f"task-{task}",
            kind="build",
            arm=arm,
            repeat=repeat,
            usd_total=10 * (ratio if arm == "candidate" else 1),
            wall_s=100,
            first_pass=True,
            verified=True,
            accepted_verified=True,
            heldout=False,
        )
        for task in range(tasks)
        for arm in ("candidate", "baseline")
        for repeat in range(repeats)
    ]


def test_per_task_counts_failed_cost_and_only_accepted_time():
    records = cases(tasks=1)
    records[0].update({"accepted_verified": False, "first_pass": False, "wall_s": 1000, "usd_total": 30})
    task = stats.per_task(records)["task-0"]["candidate"]
    assert task == dict(c_t=40, wall_s=100, first_pass=0.5, passes=1, repeats=2)
    records[1]["accepted_verified"] = False
    assert stats.matched(records) == {}


def test_escalation_counts_on_pass_only():
    records = cases(tasks=1)
    for record in records:
        record["kind"] = "escalate"
    assert stats.per_task(records)["task-0"]["candidate"]["passes"] == 2
    assert stats.per_task(records)["task-0"]["candidate"]["c_t"] is None
    assert stats.matched(records) == {}


def test_constant_ratio_has_known_bootstrap_interval():
    expected = math.log(0.5)
    assert stats.bootstrap(cases(ratio=0.5), "cost", n=100, seed=12) == pytest.approx(
        (expected, expected, expected)
    )


def test_seeded_bootstrap_resamples_tasks_and_repeats():
    records = cases(tasks=2)
    for record, cost in zip(records, [2, 18, 5, 15, 4, 36, 5, 15], strict=True):
        record["usd_total"] = cost
    result = stats.bootstrap(records, "cost", n=100, seed=7)
    assert result == stats.bootstrap(records, "cost", n=100, seed=7)
    assert result == pytest.approx((math.log(2) / 2, -0.9264273595768591, 1.1383841640481531))


def test_aa_inside_band_and_assumed_band():
    records = cases()
    result = stats.verdict(records, [records])
    assert result["verdict"] == "no_worse"
    assert result["b"] == 0.1
    assert result["sigma_aa"] == 0
    assert result["band_source"] == "measured"
    assumed = stats.verdict(records, [])
    assert assumed["band_source"] == "assumed"
    assert assumed["sigma_aa"] == 0.30
    assert assumed["b"] == pytest.approx(1.64 * 0.30 / math.sqrt(6))


def test_noise_pools_only_last_three_runs():
    band = stats.noise_band([cases(ratio=100), cases(ratio=2), cases(), cases(ratio=0.5)], 6)
    assert band["sigma_aa"] == pytest.approx(math.sqrt(2 * math.log(2) ** 2 / 3))


@pytest.mark.parametrize("heldout", [False, True])
@pytest.mark.parametrize("shortfall, expected", [(1, "inconclusive"), (2, "worse")])
def test_pass_shortfall_never_better(heldout, shortfall, expected):
    records = cases(ratio=0.1)
    if heldout:
        records += [{**record, "task": "heldout-" + record["task"], "heldout": True} for record in cases()]
    failed = 0
    for record in records:
        if record["arm"] == "candidate" and record["heldout"] == heldout and record["repeat"] == 0:
            record["accepted_verified"] = False
            failed += 1
            if failed == shortfall:
                break
    assert stats.verdict(records, [cases()])["verdict"] == expected


def test_unmatched_failures_and_escalations_still_count_on_pass():
    for kind in ("build", "escalate"):
        records = cases()
        extra: list[stats.CaseRecord] = [
            {**record, "task": "extra", "kind": kind} for record in cases(tasks=1)
        ]
        for record in extra:
            if record["arm"] == "candidate":
                record["accepted_verified"] = False
        assert stats.verdict(records + extra, [cases()])["verdict"] == "worse"


@pytest.mark.parametrize("tasks, repeats", [(5, 2), (6, 1)])
def test_underpowered(tasks, repeats):
    result = stats.verdict(cases(tasks, repeats), [])
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == "underpowered"


@pytest.mark.parametrize("metric", ["time", "first_pass"])
def test_guards_can_block_cheap_candidate(metric):
    records = cases(repeats=4, ratio=0.1)
    for record in records:
        if record["arm"] == "candidate":
            if metric == "time":
                record["wall_s"] *= 10
            else:
                record["first_pass"] = False
    result = stats.verdict(records, [cases()])
    assert result["verdict"] == "worse"
    assert result["guards"][metric]["verdict"] == "worse"


def test_zero_first_pass_in_both_arms_is_equal():
    records = cases()
    for record in records:
        record["first_pass"] = False
    assert stats.verdict(records, [records])["verdict"] == "no_worse"


def test_abba_order():
    assert stats.abba(["one", "two"], 3) == [
        (task, arm, repeat)
        for repeat, arms in [
            (0, ("candidate", "baseline")),
            (1, ("baseline", "candidate")),
            (2, ("candidate", "baseline")),
        ]
        for task in ("one", "two")
        for arm in arms
    ]


@pytest.mark.parametrize("ratio, expected", [(0.5, "better"), (1, "no_worse"), (2, "worse")])
def test_cost_verdicts(ratio, expected):
    assert stats.verdict(cases(ratio=ratio), [cases()])["verdict"] == expected


def test_each_guard_uses_its_own_calibration():
    aa = cases()
    for record in aa:
        if record["arm"] == "candidate":
            record["wall_s"] *= 4
    result = stats.verdict(cases(), [aa])
    assert result["b"] == 0.1
    assert result["guards"]["time"]["b"] == pytest.approx(1.64 * math.log(4) / math.sqrt(6))
    assert result["guards"]["first_pass"]["b"] == 0.1


def test_empty_sample_cannot_produce_a_decision():
    assert stats.bootstrap([], "cost") == (None, None, None)
    result = stats.verdict([], [])
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == "underpowered"
    assert result["m"] == 0


def test_opposite_pass_shortfalls_do_not_cancel_between_partitions():
    records = cases()
    heldout: list[stats.CaseRecord] = [
        {**record, "task": "hidden-" + record["task"], "heldout": True} for record in cases()
    ]
    for record in records[:2]:
        record["accepted_verified"] = False
    for record in heldout[2:4]:
        record["accepted_verified"] = False
    assert stats.verdict(records + heldout, [cases()])["verdict"] == "worse"


def test_inconclusive_time_guard_does_not_block_cost_improvement():
    records = cases(ratio=0.1)
    for record in records:
        if record["arm"] == "candidate":
            record["wall_s"] = 1 if record["repeat"] == 0 else 1000
    result = stats.verdict(records, [cases()])
    assert result["guards"]["time"]["verdict"] == "inconclusive"
    assert result["verdict"] == "better"


def test_wide_cost_interval_is_inconclusive():
    records = cases()
    for record in records:
        if record["arm"] == "candidate":
            record["usd_total"] *= 0.1 if int(record["task"].split("-")[1]) < 3 else 10
    result = stats.verdict(records, [cases()])
    assert result["verdict"] == "inconclusive"
    assert result["L"] is not None and result["U"] is not None
    assert result["L"] < 0 < result["U"]


def _fail(records: list[stats.CaseRecord], task: str, arm: str, repeat: int) -> list[stats.CaseRecord]:
    for record in records:
        if (record["task"], record["arm"], record["repeat"]) == (task, arm, repeat):
            record["accepted_verified"] = False
    return records


def test_too_small_a_plan_is_underpowered_even_with_a_pass_shortfall():
    # QA round 1, finding 1: five tasks cannot decide, whichever way the pass counts go.
    records = _fail(_fail(cases(tasks=5), "task-0", "candidate", 0), "task-1", "candidate", 0)
    result = stats.verdict(records, [cases()])
    assert (result["verdict"], result["reason"]) == ("inconclusive", "underpowered")
    short = [
        r for r in cases() if not (r["task"] == "task-0" and r["arm"] == "candidate" and r["repeat"] == 1)
    ]
    short = _fail(_fail(short, "task-1", "candidate", 0), "task-2", "candidate", 0)
    result = stats.verdict(short, [cases()])
    assert (result["verdict"], result["reason"]) == ("inconclusive", "underpowered")


def test_a_candidate_that_fails_every_task_is_worse_not_underpowered():
    records = cases()
    for record in records:
        if record["arm"] == "candidate":
            record["accepted_verified"] = False
    assert stats.verdict(records, [cases()])["verdict"] == "worse"


@pytest.mark.parametrize("ratio", [1.0, 0.5])
def test_no_qualifying_aa_band_never_merges(ratio):
    # QA round 1, finding 2: an assumed band, or an A/A below 6 tasks x 2 repeats, gives no mergeable verdict.
    for history in ([], [cases(tasks=1, repeats=1)], [cases(tasks=6, repeats=1)]):
        result = stats.verdict(cases(ratio=ratio), history)
        assert result["band_source"] == "assumed"
        assert (result["verdict"], result["reason"]) == ("inconclusive", "no A/A band")


def test_without_an_aa_band_a_regression_is_still_worse():
    assert stats.verdict(cases(ratio=3.0), [])["verdict"] == "worse"


def test_underpowered_aa_does_not_calibrate_a_larger_comparison():
    band = stats.noise_band([cases(tasks=5), cases(repeats=1)], 6)
    assert band["band_source"] == "assumed"
    assert stats.verdict(cases(), [cases(tasks=5)])["reason"] == "no A/A band"


def test_a_zero_first_pass_share_in_an_aa_keeps_the_guard_working():
    # QA step 5b: a 0 share made the log ratio infinite, the A/A band infinite, and the guard always no_worse.
    aa = cases()
    for record in aa:
        if record["task"] == "task-0" and record["arm"] == "candidate":
            record["first_pass"] = False
    band = stats._noise_band([aa], 6, "first_pass")
    assert math.isfinite(band["sigma_aa"]) and math.isfinite(band["b"])
    regressed = cases()
    for record in regressed:
        if record["arm"] == "candidate":
            record["first_pass"] = False
    result = stats.verdict(regressed, [aa])
    assert result["guards"]["first_pass"]["verdict"] == "worse"
    assert result["verdict"] == "worse"


@pytest.mark.parametrize("candidate, baseline", [(0.0, 1.0), (1.0, 0.0), (0.0, 0.0), (0.5, 1.0)])
def test_the_first_pass_guard_is_always_finite(candidate, baseline):
    records = cases(tasks=1)
    for record in records:
        share = candidate if record["arm"] == "candidate" else baseline
        record["first_pass"] = record["repeat"] < share * 2
    assert all(math.isfinite(r) for r in stats._ratios(stats.matched(records), "first_pass"))


def with_coordinator(records, cache_read_of):
    """Give each record a coordinator token split whose cache-read count `cache_read_of(record)` names."""
    return [
        {
            **r,
            "tokens": {
                "coordinator": {
                    "sonnet": {
                        "input": 1,
                        "output": 1,
                        "cache_read": cache_read_of(r),
                        "cache_write": 1,
                    }
                }
            },
        }
        for r in records
    ]


def test_coord_cache_read_guard_reports_a_regression_without_deciding_the_verdict():
    aa = with_coordinator(cases(), lambda r: 1000 + 100 * (r["repeat"] % 2))
    flat = with_coordinator(cases(), lambda r: 1000)
    heavy = with_coordinator(cases(), lambda r: 5000 if r["arm"] == "candidate" else 1000)
    assert stats.verdict(flat, [aa])["guards"]["coord_cache_read"]["verdict"] == "no_worse"
    result = stats.verdict(heavy, [aa])
    guard = result["guards"]["coord_cache_read"]
    assert guard["verdict"] == "worse" and guard["S"] == pytest.approx(math.log(5))
    assert guard["band_source"] == "measured"
    assert result["verdict"] != "worse"  # cost and time are unchanged


def test_coord_cache_read_counts_failed_cases_but_divides_by_accepted():
    records = with_coordinator(cases(tasks=1), lambda r: 1000)
    for record in records:
        if record["arm"] == "candidate" and record["repeat"] == 0:
            record["accepted_verified"] = False
    assert stats._coord_effects(records) == [
        pytest.approx(math.log(2))
    ]  # 2000 per one accepted vs 1000 per two


def test_coord_cache_read_falls_back_to_author_and_skips_tasks_without_tokens():
    records = cases(tasks=2)
    records = with_coordinator(records, lambda r: 1000)
    for record in records:
        if record["task"] == "task-0":
            record["tokens"] = {"author": record["tokens"]["coordinator"]}
        else:
            record["tokens"] = None
    assert stats._coord_effects(records) == [0.0]
    assert stats.bootstrap(cases(), "coord_cache_read") == (None, None, None)
