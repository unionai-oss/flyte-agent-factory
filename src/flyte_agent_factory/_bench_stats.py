"""Pure paired benchmark statistics (design-loop §2).

A/A history is a sequence of runs, each containing case records, oldest first.
Callers select compatible, current calibration runs before passing that history.
Metrics are ``cost``, ``time`` and ``first_pass``, and ``coord_cache_read`` (a guard that never decides
the verdict); positive log ratios always
mean candidate regression. ABBA plans contain (task, arm, repeat) tuples.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, TypedDict, TypeVar, cast

Arm = Literal["candidate", "baseline"]
Decision = Literal["worse", "better", "no_worse", "inconclusive"]
Task = TypeVar("Task")


class _OptionalCaseFields(TypedDict, total=False):
    heldout: bool
    tokens: dict[str, Any] | None


class CaseRecord(_OptionalCaseFields):
    task: str
    arm: str
    kind: str
    repeat: int
    usd_total: float
    wall_s: float
    accepted_verified: bool
    first_pass: bool
    verified: bool


class TaskSummary(TypedDict):
    c_t: float | None
    wall_s: float | None
    first_pass: float | None
    passes: int
    repeats: int


class NoiseBand(TypedDict):
    sigma_aa: float
    b: float
    band_source: str


class Guard(NoiseBand):
    S: float | None
    L: float | None
    U: float | None
    verdict: Decision


class Verdict(Guard):
    reason: str
    m: int
    guards: dict[str, Guard]


_ARMS: tuple[Arm, Arm] = ("candidate", "baseline")
_METRICS: dict[str, Literal["c_t", "wall_s", "first_pass"]] = {
    "cost": "c_t",
    "time": "wall_s",
    "first_pass": "first_pass",
}
#: Coordinator cache-read tokens per accepted case. Not in `_METRICS`: it is read from each case's `tokens`,
#: not from `per_task`, whose summary is the cost, time and first-pass table.
COORD_CACHE_READ = "coord_cache_read"
#: A decision, and an A/A run that calibrates one, needs this many matched build tasks x repeats per arm.
MIN_TASKS, MIN_REPEATS = 6, 2


def _groups(records: Iterable[CaseRecord]) -> Mapping[str, Mapping[str, list[CaseRecord]]]:
    groups: defaultdict[str, defaultdict[str, list[CaseRecord]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        groups[record["task"]][record["arm"]].append(record)
    return groups


def per_task(records: Sequence[CaseRecord]) -> dict[str, dict[str, TaskSummary]]:
    """Summarize each task and arm, retaining pass counts for escalation tasks."""
    tasks: dict[str, dict[str, TaskSummary]] = {}
    for task, arms in _groups(records).items():
        tasks[task] = {}
        for arm, repeats in arms.items():
            accepted = [r for r in repeats if r["accepted_verified"]]
            build = repeats[0]["kind"] == "build"
            tasks[task][arm] = {
                "c_t": sum(r["usd_total"] for r in repeats) / len(accepted) if build and accepted else None,
                "wall_s": statistics.median(r["wall_s"] for r in accepted) if build and accepted else None,
                "first_pass": (
                    sum(r["first_pass"] and r["verified"] for r in repeats) / len(repeats) if build else None
                ),
                "passes": len(accepted),
                "repeats": len(repeats),
            }
    return tasks


def matched(records: Sequence[CaseRecord]) -> dict[str, dict[str, TaskSummary]]:
    """Return build task summaries with at least one acceptance in each arm."""
    return {
        task: arms
        for task, arms in per_task(records).items()
        if all(arm in arms and arms[arm]["c_t"] is not None for arm in _ARMS)
    }


def _ratio(candidate: float, baseline: float) -> float:
    # Zero first-pass shares occur naturally; no pseudocount should hide a
    # complete loss of first-pass acceptance, or invent a gain when both fail.
    if candidate == baseline:
        return 0.0
    if baseline == 0:
        return math.inf
    if candidate == 0:
        return -math.inf
    return math.log(candidate / baseline)


def _ratios(tasks: dict[str, dict[str, TaskSummary]], metric: str) -> list[float]:
    """Per-task effects, positive meaning the candidate is worse.

    Cost and time are log ratios. First-pass acceptance is a share in [0, 1] that is often exactly 0 with
    two repeats, where a log ratio is infinite: one such A/A would make the band infinite and switch the
    guard off. So it is the difference of shares (baseline minus candidate), always finite; its band is in
    share points (the 0.10 floor is ten points).
    """
    # matched() retains build tasks with acceptance in both arms, so all three metrics are populated.
    measured = cast(dict[str, dict[str, dict[str, float]]], tasks)
    field = _METRICS[metric]
    if metric == "first_pass":
        return [arms["baseline"][field] - arms["candidate"][field] for arms in measured.values()]
    return [_ratio(arms["candidate"][field], arms["baseline"][field]) for arms in measured.values()]


def _coord_cache_read(record: CaseRecord) -> float | None:
    """Return the coordinator's cache-read tokens in one case, or None where the record has no token split."""
    tokens = record.get("tokens") or {}
    models = tokens.get("coordinator", tokens.get("author"))
    if models is None:
        return None
    return float(sum(counts["cache_read"] for counts in models.values()))


def _coord_effects(records: Sequence[CaseRecord]) -> list[float]:
    """Per-task log ratios of coordinator cache-read tokens per accepted case, positive meaning worse.

    Counted like cost: every repeat's tokens, divided by the accepted repeats. A task where any repeat has
    no token data has no figure and is left out.
    """
    groups = _groups(records)
    effects = []
    for task in matched(records):
        per_arm = {}
        for arm in _ARMS:
            repeats = groups[task][arm]
            reads = [_coord_cache_read(r) for r in repeats]
            if None in reads:
                break
            accepted = sum(bool(r["accepted_verified"]) for r in repeats)
            per_arm[arm] = sum(cast(list[float], reads)) / accepted
        else:
            effects.append(_ratio(per_arm["candidate"], per_arm["baseline"]))
    return effects


def _effects(records: Sequence[CaseRecord], metric: str) -> list[float]:
    if metric == COORD_CACHE_READ:
        return _coord_effects(records)
    return _ratios(matched(records), metric)


def _mean(values: Sequence[float]) -> float:
    # Opposite infinite effects have no defined mean, hence no mergeable estimate.
    if math.inf in values and -math.inf in values:
        return math.nan
    return statistics.mean(values)


def _noise_band(aa_runs: Sequence[Sequence[CaseRecord]], m: int, metric: str) -> NoiseBand:
    if m < 1:
        raise ValueError("noise band needs at least one matched task")
    eligible = [run for run in aa_runs if _powered(run, MIN_TASKS, MIN_REPEATS)]
    ratios = [ratio for run in eligible[-3:] for ratio in _effects(run, metric)]
    sigma = math.sqrt(statistics.mean(r * r for r in ratios)) if ratios else 0.30
    return {
        "sigma_aa": sigma,
        "b": max(0.10, 1.64 * sigma / math.sqrt(m)),
        "band_source": "measured" if ratios else "assumed",
    }


def noise_band(aa_runs: Sequence[Sequence[CaseRecord]], m: int) -> NoiseBand:
    """Pool the last three A/A runs' cost log ratios; use 0.30 without data."""
    return _noise_band(aa_runs, m, "cost")


def _percentile(values: Sequence[float], fraction: float) -> float:
    # statistics.quantiles uses the same linear interpolation, but inf - inf
    # produces NaN for a legitimate interval at a zero first-pass boundary.
    position = (len(values) - 1) * fraction
    lower = values[math.floor(position)]
    upper = values[math.ceil(position)]
    if lower == upper:
        return lower
    if not math.isfinite(lower) or not math.isfinite(upper):
        if lower == -math.inf and upper == math.inf:
            return lower if fraction < 0.5 else upper
        return lower if lower == -math.inf else upper
    return lower + (upper - lower) * (position % 1)


def bootstrap(
    records: Sequence[CaseRecord], metric: str, n: int = 2000, seed: int = 0
) -> tuple[float, float, float] | tuple[None, None, None]:
    """Resample matched tasks, then each arm's repeats, using a local RNG.

    A replicate can lose every accepted repeat. Unmatched tasks are omitted as
    in the original estimator; a replicate with no matches widens the interval
    conservatively instead of silently conditioning on successful draws.
    """
    if metric not in _METRICS and metric != COORD_CACHE_READ:
        raise ValueError(f"unknown metric: {metric}")
    if n < 1:
        raise ValueError("n must be positive")
    tasks = matched(records)
    if not tasks:
        return (None, None, None)
    effects = _effects(records, metric)
    if not effects:
        return (None, None, None)
    score = _mean(effects)
    groups = _groups(records)
    names = list(tasks)
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        ratios = []
        for task in rng.choices(names, k=len(names)):
            sampled = [
                record for arm in _ARMS for record in rng.choices(groups[task][arm], k=len(groups[task][arm]))
            ]
            ratios.extend(_effects(sampled, metric))
        samples.append(_mean(ratios) if ratios else math.nan)
    # Undefined draws must not become spurious evidence for either direction.
    lower = sorted(-math.inf if math.isnan(s) else s for s in samples)
    upper = sorted(math.inf if math.isnan(s) else s for s in samples)
    return score, _percentile(lower, 0.05), _percentile(upper, 0.95)


def _classify(score: float, lower: float, upper: float, band: float) -> Decision:
    if lower > band:
        return "worse"
    if upper < -band:
        return "better"
    if upper <= 2 * band and score <= band:
        return "no_worse"
    return "inconclusive"


def _powered(records: Sequence[CaseRecord], min_tasks: int, min_repeats: int) -> bool:
    """At least `min_tasks` build tasks, each with `min_repeats` repeats in both arms, all matched."""
    groups = _groups(r for r in records if r["kind"] == "build")
    planned = [
        task
        for task, arms in groups.items()
        if all(len({r["repeat"] for r in arms.get(arm, [])}) >= min_repeats for arm in _ARMS)
    ]
    return len(planned) >= min_tasks and len(planned) == len(groups) and len(matched(records)) >= min_tasks


def verdict(
    records: Sequence[CaseRecord],
    aa_runs: Sequence[Sequence[CaseRecord]],
    *,
    min_tasks: int = MIN_TASKS,
    min_repeats: int = MIN_REPEATS,
) -> Verdict:
    """Apply the minimum sample, pass gate, cost table and independent guards (design-loop 2.2-2.3).

    The plan's size is checked first: too few build tasks, or one short of repeats, is `underpowered`
    whatever the pass counts say. A plan of the right size whose candidate loses passes is then judged by
    the pass gate, so a candidate that fails every task is `worse`, not underpowered. Only A/A runs that
    are themselves powered count toward the band; with none, no verdict is mergeable (`no A/A band`),
    though `worse` still stands.
    Freshness and compatibility of the A/A history (age, suite digest, model, login) are the caller's.
    """
    tasks = matched(records)
    m = len(tasks)
    # The band fields are filled by the immediately following update before result is used.
    result = cast(
        Verdict, dict(verdict="inconclusive", reason="underpowered", S=None, L=None, U=None, m=m, guards={})
    )
    # Updating a TypedDict through dict retains the existing keyword-update API.
    cast(dict[str, object], result).update(noise_band(aa_runs, max(m, 1)))
    build = _groups(r for r in records if r["kind"] == "build")
    if len(build) < min_tasks or any(
        len({r["repeat"] for r in arms.get(arm, [])}) < min_repeats
        for arms in build.values()
        for arm in _ARMS
    ):
        return result
    shortfalls = []
    for heldout in (False, True):
        counts = {
            arm: sum(
                bool(r["accepted_verified"])
                for r in records
                if r["arm"] == arm and bool(r.get("heldout", False)) == heldout
            )
            for arm in _ARMS
        }
        shortfalls.append(counts["baseline"] - counts["candidate"])
    shortfall = max(shortfalls)
    if shortfall > 0:
        # TypedDict does not model dict.update keyword arguments.
        cast(dict[str, object], result).update(
            verdict="worse" if shortfall >= 2 else "inconclusive",
            reason="heldout pass shortfall" if shortfalls[1] == shortfall else "visible pass shortfall",
        )
        return result
    if m < min_tasks:
        return result
    score, lower, upper = bootstrap(records, "cost")
    # min_tasks=0 permits empty matches; preserve the existing failure on None estimates.
    decision = _classify(score, lower, upper, result["b"])  # type: ignore[arg-type]  # Empty matches yield None.
    # TypedDict does not model dict.update keyword arguments.
    cast(dict[str, object], result).update(verdict=decision, reason="cost", S=score, L=lower, U=upper)
    for metric in ("time", "first_pass", COORD_CACHE_READ):
        score, lower, upper = bootstrap(records, metric)
        band = _noise_band(aa_runs, m, metric)
        # The verdict is added on the next line before the guard is published.
        guard = cast(Guard, dict(S=score, L=lower, U=upper, **band))
        # Matching does not depend on metric; this has the same empty-match case as cost above.
        if score is None and metric == COORD_CACHE_READ:  # no case carries a token split (older runs)
            guard["verdict"] = "inconclusive"
        else:
            guard["verdict"] = _classify(score, lower, upper, band["b"])  # type: ignore[arg-type]  # Empty matches yield None.
        result["guards"][metric] = guard
        # The coordinator's cache reads are reported beside the verdict, not part of it: a change that
        # trades cache reads for cost or time is judged by cost and time.
        if guard["verdict"] == "worse" and metric != COORD_CACHE_READ:
            # TypedDict does not model dict.update keyword arguments.
            cast(dict[str, object], result).update(verdict="worse", reason=f"{metric} guard")
    if result["band_source"] == "assumed" and result["verdict"] != "worse":
        # TypedDict does not model dict.update keyword arguments.
        cast(dict[str, object], result).update(verdict="inconclusive", reason="no A/A band")
    return result


def abba(tasks: Sequence[Task], repeats: int) -> list[tuple[Task, Arm, int]]:
    """Alternate candidate/baseline then baseline/candidate on each task."""
    return [
        (task, arm, repeat)
        for repeat in range(repeats)
        for task in tasks
        for arm in (_ARMS if repeat % 2 == 0 else _ARMS[::-1])
    ]
