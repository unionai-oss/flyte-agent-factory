"""The factory benchmark's measurement: the bench profile, case planning, scoring and the report.

Pure: no Flyte and no network, so everything that decides a number is tested offline. Executing a
benchmark on Flyte is `_bench_flow`; what a task is, `_bench_suite`. The spec is
`design-benchmark.md` in the factory-improvement harness directory.

A case is one run of one task by one arm (an engine SHA and a launch policy), one repeat. Its record
(`case_record`, one JSONL line) is read from the case's run report: the JSON snapshot the supervisor
embeds (`_dashboard.from_report`) plus what the harness saw while it drove the run.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import statistics
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

# Writes the bench profile, which tomllib cannot. flyte depends on `toml`, so every image has it.
import toml  # type: ignore[import-untyped]  # no stubs for toml in the dev dependencies

# The runtime imports are absolute (see `_webhook` for why); mypy resolves `flyte_agent_factory` to the
# install, which has no py.typed marker, so it checks against the relative imports instead.
if TYPE_CHECKING:
    from . import _bench_suite, _dashboard, _names, _profile
    from ._bridge import DEFAULT_ASK_TIMEOUT_S
else:
    from flyte_agent_factory import _bench_suite, _dashboard, _names, _profile
    from flyte_agent_factory._bridge import DEFAULT_ASK_TIMEOUT_S

SCHEMA = 1
OUTCOMES = (
    "accepted",
    "escalated",
    "not_converged",
    "capped_cost",
    "capped_time",
    "failed",
    "harness_error",
    "skipped_login_busy",
)
#: What a build case's non-blocking question is answered with: a benchmark has no human.
ASK_ANSWER = "Benchmark: no human is available; take your stated default."
#: Sent to the case's `direct-*` condition to end it: the session exits, checkpoints and completes the run.
STOP = "/stop"
#: Where each dollar figure comes from: the CLI's own total for `claude -p` sub-agents, list price for the
#: author, whose interactive session has no `-p` JSON (`_usage._author_usage`).
USD_SOURCE = {"author": "list-price", "subagents": "cli-total_cost_usd"}
CHECK_SUMMARY_MAX = 500
ARMS = ("candidate",)  # the baseline arm comes with the A/B comparison (design step 5)
MAX_REPEATS = 3
POLL_S = 60
# Reserve the poll, stop delivery and shutdown grace at the assumed initial burn rate.
INITIAL_BURN_USD_PER_S = 15 / (3 * 3600)
STOP_RETRY_S = 10 * 60
STOP_GRACE_S = 10 * 60
STOP_MARGIN_USD = (POLL_S + STOP_RETRY_S + STOP_GRACE_S) * INITIAL_BURN_USD_PER_S
_SHA = re.compile(r"[0-9a-f]{40}")

# Profile tables a bench env must not carry, and why:
#   state       a project's last act dispatches the production queue when it is set
#   board       board moves on production's board
#   deploy, direction   only the deployer reads them (and direction requires auto, auto requires state)
#   mcp         names [repo] keys the bench profile does not have
#   browser     the personal browsers are production's: their apps, tokens and profile archives
#   repo_keys, repo_aliases   a bench pod holds the bench repo's key and no other
#   bench       an older engine's strict parser refuses a table it does not know
#   self_agent  retired
# Cases push with the bench deploy key; the App stays in the harness.
_NOT_FOR_BENCH = (
    "state",
    "board",
    "deploy",
    "direction",
    "mcp",
    "browser",
    "repo_keys",
    "repo_aliases",
    "bench",
    "self_agent",
    "github",
)


def check_sha(sha: str) -> str:
    """`sha` if it is a full lowercase commit SHA; ValueError otherwise."""
    if not _SHA.fullmatch(sha):
        raise ValueError(f"{sha!r} is not a 40-character commit SHA")
    return sha


def bench_envs(sha: str, count: int) -> list[str]:
    """Return the bench env names for engine `sha`, one per concurrent case: `bench-<sha7>-<i>`.

    Named apart from every slot, so `_factory.operated_live`, `free_slot` and the queue, which list by
    slot task name, never see a bench case; and deploying one never moves production, which launches
    `<slot>.project` at its latest version.
    """
    check_sha(sha)
    return [f"bench-{sha[:7]}-{i}" for i in range(1, count + 1)]


def bench_profile(profile: _profile.Profile, sha: str) -> _profile.Profile:
    """Return the profile engine `sha`'s bench envs are deployed with: production's, cut down.

    Its slots are `[bench] token_concurrency` envs on the bench token, its project repository is the bench
    repo with only that repo's key, and it has nothing that reaches production state (`_NOT_FOR_BENCH`).

    The bench repo is the profile's `[repo] project` rather than a new `[repo]` key: `[repo]` takes only
    `project` and `factory`, and older engines refuse anything else. So cases use the sandbox layout,
    each in its own `<case>/` directory, which is also what the design's sandbox fallback does.
    """
    bench = profile.bench
    if bench is None:
        raise ValueError("the profile has no [bench] table")
    data = tomllib.loads(profile.text)
    for key in _NOT_FOR_BENCH:
        data.pop(key, None)
    data["repo"] = {"project": bench.repo}
    data["secrets"] = {"deploy_key": bench.repo_key, "claude_token": bench.claude_token}
    data["policy"] = {
        k: v for k, v in data.get("policy", {}).items() if "ENGINE" not in k and "CODEX" not in k
    }
    data["slots"] = [
        {"env": env, "subagent_env": f"{env}-sub"} for env in bench_envs(sha, bench.token_concurrency)
    ]
    data["cap"] = bench.token_concurrency
    parsed = _profile.parse(toml.dumps(data), f"bench profile for {sha[:7]}")
    # The standards are production's, as captured when this pod was deployed.
    return replace(parsed, standards_payload=profile.standards_payload, source_dir=profile.source_dir)


def case_name(bench_run: str, task: str, arm: str, repeat: int) -> str:
    """Return the case's run and project name, the same on every retry: a retried harness finds its run by it.

    `b-<task[:10]>-<6 hex>`: a valid project name (`_factory_policy.validate`), so its branch is
    `factory/<name>` and its directory `<name>/`.
    """
    digest = hashlib.sha256(f"{bench_run}\0{task}\0{arm}\0{repeat}".encode()).hexdigest()[:6]
    return f"b-{task[:10].rstrip('-')}-{digest}"


def plan_cases(
    bench_run: str, sha: str, task_ids: Iterable[str], repeats: int, policy: Mapping[str, str], model: str
) -> list[dict[str, Any]]:
    """Return every case of a candidate-only benchmark, in run order.

    Run order is repeat by repeat, so no task runs twice in a row. ValueError for an unknown task or a
    repeat count outside 1-`MAX_REPEATS`.
    """
    check_sha(sha)
    if not 1 <= repeats <= MAX_REPEATS:
        raise ValueError(f"repeats must be 1 to {MAX_REPEATS}, not {repeats}")
    for key, value in policy.items():
        routing_key = key.removeprefix(_names.PREFIX)
        allowed = "false" if routing_key == "QA_DIFFERENT_ENGINE" else "claude"
        if ("ENGINE" in routing_key and value != allowed) or "CODEX" in routing_key:
            raise ValueError("step 1 benchmark arms are Claude-only")
    tasks = [_bench_suite.get(t) for t in dict.fromkeys(list(task_ids) or list(_bench_suite.TASKS))]
    digest = _bench_suite.digest()
    return [
        {
            "bench_run": bench_run,
            "suite": _bench_suite.SUITE,
            "suite_digest": digest,
            "task": task.id,
            "kind": task.kind,
            "heldout": False,
            "arm": "candidate",
            "engine_sha": sha,
            "policy": dict(policy),
            "model": model,
            "repeat": repeat,
            "case": case_name(bench_run, task.id, "candidate", repeat),
        }
        for repeat in range(1, repeats + 1)
        for task in tasks
    ]


def budget_usd(cases: Iterable[Mapping[str, Any]]) -> float:
    """Return the reserved case budgets, including stop reserves; reported spend can overshoot."""
    return sum(_bench_suite.get(c["task"]).cap_usd for c in cases)


def expected_usd(cases: Iterable[Mapping[str, Any]]) -> float:
    return sum(_bench_suite.get(c["task"]).estimate_usd for c in cases)


def spend_refusal(ceiling: float, max_usd: float, profile_max: float) -> str | None:
    """Return why a benchmark that can spend `ceiling` must not start, or None.

    `max_usd` is the launch's own ceiling, `profile_max` the deployment's (`[bench] max_usd`).
    """
    if not math.isfinite(max_usd) or not math.isfinite(profile_max):
        return "max_usd and profile max_usd must be finite"
    if max_usd < 0:
        return f"max_usd must be 0 (estimate only) or more, not {max_usd}"
    if max_usd > profile_max:
        return f"max_usd {max_usd:g} is over the profile's [bench] max_usd {profile_max:g}"
    if ceiling > max_usd:
        return (
            f"these cases can spend up to ${ceiling:g} (budgeted at ${ceiling:g}, including stop margins), "
            f"over max_usd {max_usd:g}; run fewer tasks or repeats, or raise max_usd"
        )
    return None


# --------------------------------------------------------------------------- #
# Reading a case's report snapshot
# --------------------------------------------------------------------------- #


def usd(snapshot: Mapping[str, Any]) -> tuple[float | None, dict[str, float], float | None]:
    """`(total, by sub-agent role, author)` in API-equivalent dollars; None where the report has no figure."""
    cost = snapshot.get("cost") or {}
    author = (cost.get("coordinator") or {}).get("usd_estimate")
    # The supervisor adds `subagents` with its first recorded sub-agent usage, so with an author
    # figure an absent block is $0 so far, not unknown.
    subagents = (
        (cost["subagents"] or {}).get("usd") if "subagents" in cost else (0.0 if author is not None else None)
    )
    complete = all(isinstance(x, (int, float)) for x in (subagents, author))
    # `complete` established that both are numbers; mypy cannot narrow through all().
    return (
        (cast(float, subagents) + cast(float, author) if complete else None),
        dict(cost.get("by_role") or {}),
        author,
    )


def review_passed(snapshot: Mapping[str, Any], *, live: bool) -> bool:
    """Check that the supervisor recorded a passing review round, which acceptance needs."""
    metrics = snapshot.get("metrics")
    if metrics:
        return bool(metrics.get("passed_round") and metrics.get("passed_at") is not None)
    phase = (snapshot.get("phase") or {}).get("kind")
    passed_round = (snapshot.get("review") or {}).get("passed_round")
    if phase not in ("done", "released") or not passed_round:
        return False
    timeline = snapshot.get("timeline") or []
    round_, verdicts = _dashboard.latest_verdicts(timeline)
    return round_ == passed_round and all(
        verdicts.get(judge) == "PASS" for judge in ("correctness", "completeness")
    )


def open_asks(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the case's questions with a Flyte condition still open, oldest first."""
    return [q for q in snapshot.get("needs_you") or [] if q.get("condition") and not q.get("timed_out")]


def blocking(ask: Mapping[str, Any]) -> bool:
    """Return whether an ask is an approval, or a question with no timeout of its own.

    Such an ask waits on a human, whom a benchmark does not have. `ask-human` without `--timeout`
    waits `DEFAULT_ASK_TIMEOUT_S`.
    """
    waits = float(ask.get("applies_at") or 0) - float(ask.get("opened_at") or 0)
    return ask.get("kind") == "approve" or waits >= DEFAULT_ASK_TIMEOUT_S


def _blocking_condition(condition: Mapping[str, Any]) -> bool:
    # Collected transcripts may already carry the result of blocking(). Missing evidence is not a wait.
    if "kind" in condition or "applies_at" in condition:
        return blocking(condition)
    return condition.get("blocking") is True


def _implementation_times(commits: list[Mapping[str, Any]], *, workdir: str = "") -> list[float]:
    prefix = workdir.rstrip("/") + "/"
    return [
        commit["at"]
        for commit in commits
        if any(
            (not workdir or path == workdir or path.startswith(prefix)) and not path.lower().endswith(".md")
            for path in commit["paths"]
        )
    ]


def _waited_for_answer(condition: Mapping[str, Any], times: list[float]) -> bool:
    opened = condition["opened_at"]
    answer = condition.get("answered_at")
    return not any(at >= opened and (answer is None or answer <= opened or at <= answer) for at in times)


def classify_vague(
    conditions: list[Mapping[str, Any]], commits: list[Mapping[str, Any]], outcome: str, *, workdir: str
) -> bool:
    """Return a blocking ask must precede implementation, which must wait for its answer."""
    if outcome != "escalated":
        return False
    times = _implementation_times(commits, workdir=workdir)
    first_commit = min(times, default=math.inf)
    return any(
        condition["name"].startswith("ask-")
        and _blocking_condition(condition)
        and condition["opened_at"] < first_commit
        and _waited_for_answer(condition, times)
        for condition in conditions
    )


def _proposes_split(text: str) -> bool:
    # This recognizes list shapes, not whether the proposed scope is sensible; successes need adjudication.
    if re.search(r"\b(?:implement|build|do|deliver)\s+(?:everything|it all)\s+now\b", text, re.IGNORECASE):
        return False
    numbered = {
        number
        for number, item in re.findall(r"^\s*(\d+)[.)]\s+(.+)$", text, re.MULTILINE)
        if not item.rstrip().endswith("?")
    }
    bullets = [
        item
        for item in re.findall(r"^\s*[-*+]\s+(.+)$", text, re.MULTILINE)
        if not item.rstrip().endswith("?")
    ]
    phases = {label.lower() for label in re.findall(r"\b(?:phase|part)\s+([a-z]|\d+)\b", text, re.IGNORECASE)}
    return len(numbered) >= 2 or len(bullets) >= 2 or len(phases) >= 2


def classify_too_big(
    conditions: list[Mapping[str, Any]],
    commits: list[Mapping[str, Any]],
    outcome: str,
    *,
    outcome_text: str = "",
) -> bool:
    """Return a blocking ask or blocked outcome must propose a split, before any implementation, and wait.

    Enumeration is only a heuristic for a sensible split, so successes require a human spot-check.
    """
    if outcome not in ("escalated", "blocked"):
        return False
    times = _implementation_times(commits)
    if outcome == "blocked" and _proposes_split(outcome_text) and not times:
        return True
    first_commit = min(times, default=math.inf)
    return any(
        condition["name"].startswith("ask-")
        and _blocking_condition(condition)
        and _proposes_split(condition["text"])
        and condition["opened_at"] < first_commit
        and _waited_for_answer(condition, times)
        for condition in conditions
    )


def decide(
    snapshot: Mapping[str, Any],
    *,
    kind: str,
    live: bool,
    run_phase: str = "",
    elapsed_s: float,
    cap_usd: float,
    cap_s: float,
    stop_margin_usd: float = STOP_MARGIN_USD,
    asks: int,
    blocking_asks: int,
) -> str | None:
    """Return the case's outcome once it has one, else None (keep polling)."""
    passed = review_passed(snapshot, live=live)
    # A claimed pass (a recorded passing round), or a completed run, with no verdict rows to back it is not
    # evidence of a pass. A live run at `done` without them is still being reviewed: keep polling.
    claimed = (snapshot.get("phase") or {}).get("kind") in ("done", "released") and (
        (snapshot.get("review") or {}).get("passed_round") or not live
    )
    if (
        claimed
        and not snapshot.get("metrics")
        and not _dashboard.latest_verdicts(snapshot.get("timeline") or [])[1]
    ):
        return "harness_error"
    if kind == "escalate":
        if asks or (snapshot.get("phase") or {}).get("kind") == "blocked":
            return "escalated"
        if passed:
            return "failed"  # it built what it should have pushed back on
    else:
        if passed:
            return "accepted"
        if blocking_asks:
            return "escalated"
    total, _, _ = usd(snapshot)
    if total is not None and total >= max(0.0, cap_usd - stop_margin_usd):
        return "capped_cost"
    if elapsed_s >= cap_s:
        return "capped_time"
    if not live:
        return (
            "failed"
            if run_phase in ("FAILED", "ABORTED") or (snapshot.get("phase") or {}).get("kind") == "failed"
            else "not_converged"
        )
    return None


_BOUNCES = re.compile(r"(\d+) DONE bounced before review")
_RESTARTS = re.compile(r"restarted (\d+) time")


def _risk_count(snapshot: Mapping[str, Any], pattern: re.Pattern[str]) -> int:
    for risk in snapshot.get("risks") or []:
        found = pattern.search(str(risk))
        if found:
            return int(found.group(1))
    return 0


def _stages(metrics: Mapping[str, Any], started: float, ended: float) -> dict[str, float]:
    stages = dict.fromkeys(("planning", "build", "checks", "review", "waiting", "limited"), 0.0)
    history = sorted(metrics.get("phase_history") or [], key=lambda row: row[1])
    criteria = metrics.get("criteria_at")
    asks = metrics.get("asks") or []
    boundaries = {started, ended}
    boundaries.update(at for _, at in history if started < at < ended)
    if criteria is not None and started < criteria < ended:
        boundaries.add(criteria)
    for ask in asks:
        for at in (ask["opened_at"], ask.get("closed_at") or ask.get("answered_at") or ended):
            if started < at < ended:
                boundaries.add(at)
    points = sorted(boundaries)
    for left, right in itertools.pairwise(points):
        phase = next((phase for phase, at in reversed(history) if at <= left), "starting")
        waiting = any(
            q["opened_at"] <= left < (q.get("closed_at") or q.get("answered_at") or ended) for q in asks
        )
        if phase == "limited":
            stage = "limited"
        elif waiting and phase not in ("done", "released", "failed"):
            stage = "waiting"
        elif phase in ("done", "released", "failed"):
            continue
        elif criteria is None or left < criteria:
            stage = "planning"
        else:
            stage = {"starting": "build", "building": "build"}.get(phase, phase)
        if stage in stages:
            stages[stage] += right - left
    return stages


def _tokens(usage: Mapping[str, Any]) -> dict[str, Any]:
    tokens: dict[str, dict[str, Any]] = {}
    for key, row in usage.items():
        role = key.partition("@")[0]
        for model, counts in (row.get("modelUsage") or {}).items():
            total = tokens.setdefault(role, {}).setdefault(
                model, dict.fromkeys(("input", "output", "cache_read", "cache_write"), 0)
            )
            for field, source in (
                ("input", "inputTokens"),
                ("output", "outputTokens"),
                ("cache_read", "cacheReadInputTokens"),
            ):
                total[field] += counts.get(source, 0)
            total["cache_write"] += counts.get(
                "cacheCreationInputTokens", counts.get("cacheWriteInputTokens", 0)
            )
    return tokens


def _with_coordinator(tokens: dict[str, Any]) -> dict[str, Any]:
    """Make `coordinator` the main conversation's tokens; runs before the split only have `author`.

    `author` is the main conversation plus in-pod helpers, so on those runs it overstates the coordinator.
    """
    if "coordinator" not in tokens and "author" in tokens:
        return {**tokens, "coordinator": tokens["author"]}
    return tokens


#: Every field of a case record and the types it may hold. `None` where nothing measures it yet: the
#: supervisor metrics block supplies stage times, tokens per role, preflight bounces and knobs.
FIELDS: dict[str, tuple[type, ...]] = {
    "schema": (int,),
    "bench_run": (str,),
    "suite": (str,),
    "suite_digest": (str,),
    "task": (str,),
    "kind": (str,),
    "heldout": (bool,),
    "arm": (str,),
    "engine_sha": (str,),
    "env": (str,),
    "policy": (dict,),
    "model": (str,),
    "repeat": (int,),
    "case_run": (str,),
    "case_url": (str,),
    "start_sha": (str, type(None)),
    "start_tree": (str, type(None)),
    "start_pinned": (bool,),
    "started_at": (float, int),
    "ended_at": (float, int),
    "wall_s": (float, int),
    "outcome": (str,),
    "error": (str, type(None)),
    "review_passed": (bool,),
    "passed_round": (int, type(None)),
    "passed_at": (float, int, type(None)),
    "first_pass": (bool,),
    "verified": (bool, type(None)),
    "check_summary": (str,),
    "accepted_verified": (bool,),
    "spot_check": (bool,),
    "qa_rounds": (int,),
    "blocking_by_round": (list,),
    "gate_bounces": (int,),
    "preflight_bounces": (int, type(None)),
    "asks": (int,),
    "blocking_asks": (int,),
    "first_ask_s": (float, int, type(None)),
    "escalated": (bool,),
    "stages_s": (dict, type(None)),
    "tokens": (dict, type(None)),
    "usd_total": (float, int, type(None)),
    "usd_complete": (bool,),
    "contamination_risk": (bool,),
    "cleanup_error": (str, type(None)),
    "waited_on_limit_s": (float, int),
    "usd_by_role": (dict,),
    "usd_author": (float, int, type(None)),
    "usd_source": (dict,),
    "fallbacks": (dict,),
    "restarts": (int,),
    "forced_abort": (bool,),
    "forced_abort_without_stop": (bool,),
    "overshoot_usd": (float, int),
    "unpriced_calls": (type(None),),
    "meter_at_start": (dict, type(None)),
    "knobs_seen": (dict, type(None)),
    "metrics_schema": (int, type(None)),
}


#: Fields a record may carry but need not: only a reused baseline has `reused_from` (its source case).
OPTIONAL_FIELDS: dict[str, tuple[type, ...]] = {"reused_from": (dict,)}


def validate(record: Mapping[str, Any]) -> None:
    """ValueError naming every field that is missing, unknown or of the wrong type."""
    problems = [f"missing {k}" for k in FIELDS if k not in record]
    problems += [f"unknown {k}" for k in record if k not in FIELDS and k not in OPTIONAL_FIELDS]
    for key, kinds in {**FIELDS, **OPTIONAL_FIELDS}.items():
        value = record.get(key)
        # bool is an int: a count or a time must not be True.
        wrong_bool = isinstance(value, bool) and bool not in kinds
        if key in record and (not isinstance(value, kinds) or wrong_bool):
            problems.append(f"{key} is {type(value).__name__}, not {'/'.join(k.__name__ for k in kinds)}")

    def finite(value: object) -> bool:
        if isinstance(value, float):
            return math.isfinite(value)
        if isinstance(value, Mapping):
            return all(finite(v) for v in value.values())
        if isinstance(value, list):
            return all(finite(v) for v in value)
        return True

    if not finite(record):
        problems.append("measurements must be finite")
    if record.get("outcome") not in OUTCOMES:
        problems.append(f"outcome {record.get('outcome')!r} is not one of {', '.join(OUTCOMES)}")
    if record.get("metrics_schema") not in (None, 1):
        problems.append("metrics_schema must be 1 or null")
    stages = record.get("stages_s")
    if isinstance(stages, dict):
        if set(stages) != {"planning", "build", "checks", "review", "waiting", "limited"}:
            problems.append("stages_s must contain the six benchmark stages")
        if any(type(v) not in (float, int) or v < 0 for v in stages.values()):
            problems.append("stage times must be non-negative numbers")
    tokens = record.get("tokens")
    if isinstance(tokens, dict):
        for models in tokens.values():
            if not isinstance(models, dict):
                problems.append("tokens must map roles to models")
                continue
            for counts in models.values():
                if (
                    not isinstance(counts, dict)
                    or set(counts) != {"input", "output", "cache_read", "cache_write"}
                    or any(type(v) not in (float, int) or v < 0 for v in counts.values())
                ):
                    problems.append("tokens must contain four non-negative counts per model")
    if problems:
        raise ValueError("case record: " + "; ".join(problems))


def case_record(
    case: Mapping[str, Any],
    run: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    harness: Mapping[str, Any],
    check: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return one case's record, validated before it is returned.

    It holds `case` as planned (with its `env`), `run` {name, url}, the final report `snapshot`, what
    the `harness` saw while driving it, and the hidden `check` {exit_code, summary} (None when it could
    not run).
    """
    review = snapshot.get("review") or {}
    scores = [s for s in review.get("scores") or [] if isinstance(s, int)]
    passed = (
        review_passed(snapshot, live=False) if snapshot.get("metrics") else bool(harness["review_passed"])
    )
    round_, _ = _dashboard.latest_verdicts(snapshot.get("timeline") or [])
    # Scores are cleared by _reset_contract at PASS; the verdict round is independent of them.
    passed_round = None
    if passed:
        passed_round = harness.get("passed_round") or review.get("passed_round") or round_
    historical = [
        int(found.group(1))
        for row in snapshot.get("timeline") or []
        if (found := re.match(r"blocking findings, round \d+: (\d+)", row.get("text", "")))
    ]
    verdicts = review.get("verdicts") or []
    failed_verdicts = [row for row in verdicts if row.get("verdict") == "FAIL"]
    scores = historical or scores or [row["blocking"] for row in failed_verdicts or verdicts]
    if passed and not scores:
        scores = [0]
    verified = None if check is None or check["exit_code"] is None else check["exit_code"] == 0
    outcome = harness["outcome"]
    total, by_role, author = usd(snapshot)
    if case["task"] in ("vague", "too-big"):
        conditions = harness.get("conditions")
        if conditions is None:
            conditions = [
                {
                    **q,
                    "name": q["condition"],
                    "opened_at": q["opened_at"],
                    "text": q.get("text", ""),
                    "blocking": blocking(q),
                }
                for q in snapshot.get("needs_you") or []
                if q.get("condition") and q.get("opened_at") is not None
            ]
        commits = harness.get("commits", snapshot.get("commits") or [])
        if case["task"] == "vague":
            # Missing commit history cannot establish that the ask preceded the first change.
            verified = ("commits" in harness or "commits" in snapshot) and classify_vague(
                conditions, commits, outcome, workdir=case["case"]
            )
        else:
            phase = snapshot.get("phase") or {}
            verified = classify_too_big(
                conditions,
                commits,
                "blocked" if phase.get("kind") == "blocked" else outcome,
                outcome_text=harness.get("outcome_text", phase.get("text", "")),
            )
        task = _bench_suite.get(case["task"])
        verified = bool(
            verified
            and outcome == "escalated"
            and total is not None
            and total <= task.cap_usd
            and harness["ended_at"] - harness["started_at"] <= task.cap_hours * 3600
        )
    accepted = passed if case["kind"] == "build" else outcome == "escalated"
    metrics = snapshot.get("metrics") or {}
    if metrics:
        passed_round = metrics.get("passed_round") if passed else None
        rounds = metrics.get("rounds", [])
        # Legacy scores contain failing rounds only; a first-round PASS still has its zero verdict.
        failed_rounds = [row for row in rounds if row.get("verdict") == "FAIL"]
        scores = [row["blocking"] for row in failed_rounds or rounds]
    metric_asks = metrics.get("asks") or []
    if passed and metrics.get("passed_at") is not None:
        # The harness stops counting at the pass: the release confirmation is not a question about the task.
        metric_asks = [q for q in metric_asks if q["opened_at"] <= metrics["passed_at"]]
    unpriced = any(engine != "claude" for engine in (snapshot.get("cost") or {}).get("by_engine", {}))
    record = {
        "schema": SCHEMA,
        **{
            k: case[k]
            for k in (
                "bench_run",
                "suite",
                "suite_digest",
                "task",
                "kind",
                "heldout",
                "arm",
                "engine_sha",
                "env",
                "policy",
                "model",
                "repeat",
            )
        },
        "case_run": run["name"],
        "case_url": run["url"],
        "start_sha": harness.get("start_sha"),
        "start_tree": harness.get("start_tree"),
        "start_pinned": bool(_bench_suite.get(case["task"]).start_tree),
        "started_at": harness["started_at"],
        "ended_at": harness["ended_at"],
        "wall_s": max(0.0, harness["ended_at"] - harness["started_at"]),
        "outcome": outcome,
        "error": harness.get("error"),
        "review_passed": passed,
        "passed_round": passed_round,
        "passed_at": (metrics.get("passed_at") if metrics else harness.get("passed_at")) if passed else None,
        "first_pass": passed_round == 1,
        "verified": verified,
        "check_summary": (check or {}).get("summary", "")[-CHECK_SUMMARY_MAX:],
        "accepted_verified": bool(accepted and verified),
        "spot_check": bool(case["task"] in ("vague", "too-big") and accepted and verified),
        "qa_rounds": (
            max(
                passed_round or round_ or 0,
                max((row["round"] for row in metrics.get("rounds", [])), default=0),
            )
            if metrics
            else passed_round or round_ or len(scores)
        ),
        "blocking_by_round": scores,
        "gate_bounces": metrics["bounces"]["done"] if metrics else _risk_count(snapshot, _BOUNCES),
        "preflight_bounces": metrics["bounces"]["preflight"] if metrics else None,
        "asks": len(metric_asks) if metrics else harness["asks"],
        "blocking_asks": (
            sum(bool(q["blocking"]) for q in metric_asks) if metrics else harness["blocking_asks"]
        ),
        "first_ask_s": (
            (
                max(0.0, min(q["opened_at"] for q in metric_asks) - harness["started_at"])
                if metric_asks
                else None
            )
            if metrics
            else harness.get("first_ask_s")
        ),
        "escalated": outcome == "escalated",
        "stages_s": _stages(metrics, harness["started_at"], harness["ended_at"]) if metrics else None,
        "tokens": _with_coordinator(_tokens(metrics.get("usage") or {})) if metrics else None,
        "usd_total": total,
        "usd_complete": (
            author is not None
            and (snapshot.get("cost") or {}).get("subagents", {}).get("usd") is not None
            and not unpriced
        ),
        "contamination_risk": bool(harness.get("contamination_risk", case.get("contamination_risk", True))),
        "cleanup_error": harness.get("cleanup_error"),
        "waited_on_limit_s": harness.get("waited_on_limit_s", 0.0),
        "usd_by_role": by_role,
        "usd_author": author,
        "usd_source": {**USD_SOURCE, **({"subagents": "unpriced"} if unpriced else {})},
        "unpriced_calls": None,
        "overshoot_usd": max(0.0, (total or 0.0) - _bench_suite.get(case["task"]).cap_usd),
        "fallbacks": dict((snapshot.get("cost") or {}).get("fallback_counts") or {}),
        "restarts": _risk_count(snapshot, _RESTARTS),
        "forced_abort": bool(harness["forced_abort"]),
        "forced_abort_without_stop": bool(harness.get("forced_abort_without_stop")),
        "meter_at_start": case.get("meter_at_start"),
        "knobs_seen": dict(metrics.get("knobs") or {}) if metrics else None,
        "metrics_schema": metrics.get("schema") if isinstance(metrics.get("schema"), int) else None,
    }
    validate(record)
    return record


def error_record(case: Mapping[str, Any], error: str, *, now: float) -> dict[str, Any]:
    """Keep a failed harness task in the report even when it returned no measurements."""
    return case_record(
        case,
        {"name": case["case"], "url": ""},
        {},
        {
            "outcome": "harness_error",
            "error": error,
            "review_passed": False,
            "started_at": now,
            "ended_at": now,
            "asks": 0,
            "blocking_asks": 0,
            "forced_abort": False,
        },
        None,
    )


def to_jsonl(records: Iterable[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)


# --------------------------------------------------------------------------- #
# Summary and report
# --------------------------------------------------------------------------- #


def summarize(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the headline numbers per arm.

    Cost per accepted PR counts every case's cost, failures included.
    """
    arms: dict[str, dict[str, Any]] = {}
    for arm in sorted({r["arm"] for r in records}):
        all_cases = [r for r in records if r["arm"] == arm]
        cases = [r for r in all_cases if r["outcome"] not in ("harness_error", "skipped_login_busy")]
        accepted = [r for r in cases if r["accepted_verified"]]
        build = [r for r in cases if r["kind"] == "build"]
        spent = [r["usd_total"] for r in all_cases if r["usd_total"] is not None]
        spread = {}
        if len({r["repeat"] for r in cases}) > 1:
            for field in ("wall_s", "usd_total"):
                values = [r[field] for r in cases if r[field] is not None]
                if len(values) > 1:
                    quartiles = statistics.quantiles(values, n=4, method="inclusive")
                    spread[field] = {
                        "min": min(values),
                        "max": max(values),
                        "iqr": quartiles[2] - quartiles[0],
                    }
        arms[arm] = {
            "cases": len(all_cases),
            "harness_errors": len(all_cases) - len(cases),
            "accepted_verified": len(accepted),
            "pass_rate": len(accepted) / len(cases) if cases else None,
            "unadjudicated_spot_checks": sum(bool(r.get("spot_check")) for r in accepted),
            "usd_total": sum(spent),
            "usd_unknown": len(all_cases) - len(spent),
            "cost_per_accepted": sum(spent) / len(accepted) if accepted else None,
            "time_to_accepted_s": statistics.median(r["wall_s"] for r in accepted) if accepted else None,
            "first_pass_rate": (
                sum(1 for r in build if r["first_pass"] and r["verified"]) / len(build) if build else None
            ),
            "outcomes": dict(Counter(r["outcome"] for r in all_cases)),
            "spread": spread,
        }
    return {"schema": SCHEMA, "arms": arms}


def caveats(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return what a reader must know before believing the numbers."""
    out = []
    for r in records:
        where = f"{r['task']} #{r['repeat']} ({r['case_run']})"
        if r["contamination_risk"]:
            out.append(
                f"{where}: contamination_risk: the repo key can fetch other branches; "
                "use a dedicated bench repo containing only the seed and serialize cases"
            )
        if r["cleanup_error"]:
            out.append(f"{where}: branch cleanup failed: {r['cleanup_error']}")
        if r["waited_on_limit_s"]:
            out.append(
                f"{where}: waited_on_limit_s={r['waited_on_limit_s']:g}; "
                "excluded from the time cap, included in wall_s"
            )
        if not r["usd_complete"] and r["usd_author"] is None:
            out.append(f"{where}: author cost missing; usd_total is unknown (usd_complete=false)")
        if not r["usd_complete"] and r["usd_author"] is not None:
            out.append(f"{where}: cost is incomplete (usd_complete=false)")
        if not r["start_pinned"]:
            out.append(f"{where}: start not pinned; it started from tree {r['start_tree'] or 'unknown'}")
        if r.get("spot_check"):
            out.append(f"{where}: heuristic escalation success; awaiting human adjudication")
        if r["review_passed"] and not r["verified"]:
            out.append(f"{where}: passed review but the hidden check did not pass; spot-check it")
        if r["forced_abort_without_stop"]:
            out.append(f"{where}: aborted without a /stop after the delivery retry window")
        if "unpriced" in r["usd_source"].values():
            out.append(
                f"{where}: non-Claude usage is unpriced; unpriced_calls is null until per-call metrics"
            )
        if r["forced_abort"]:
            out.append(f"{where}: did not stop on /stop and was aborted")
        if r["outcome"] == "harness_error":
            out.append(f"{where}: harness error: {r['error']}")
        if r["outcome"] == "skipped_login_busy":
            out.append(f"{where}: skipped_login_busy: {r['error']}")
        if any(error in (r["error"] or "") for error in ("stop_undeliverable", "abort_unconfirmed")):
            out.append(f"{where}: shutdown unconfirmed; human intervention required; login quarantined")
        if r["usd_total"] is None:
            out.append(f"{where}: its report had no cost figures")
    if records:
        out.append(
            "held-out suite, policy search and promotion are not available; quota guard, baseline/A-B "
            "comparison and durable result persistence are supplied by the benchmark workflow; "
            "poll-based usage-limit waits are approximate "
            "and earlier waits may be lost on harness restart"
        )
    if any(r["metrics_schema"] is None for r in records):
        out.append(
            "no supervisor metrics block: stage times, tokens per role, preflight bounces, per-call pricing, "
            "and knobs are not recorded (null)"
        )
    return out


def _cell(value: object) -> str:
    if value is None:
        return '<span class="mute">null</span>'
    if isinstance(value, float):
        return f"{value:.2f}"
    return _dashboard._e(value)


def _rate(value: float | None) -> str:
    return _cell(None) if value is None else f"{value:.0%}"


def render_report(records: Sequence[Mapping[str, Any]], summary: Mapping[str, Any], *, title: str) -> str:
    """Return the benchmark's run report: headline per arm, every case, then the caveats."""
    head = (
        "<tr><th>arm</th><th>cases</th><th>harness errors</th><th>accepted (verified)</th><th>pass rate</th>"
        "<th>unadjudicated spot checks</th><th>cost per accepted</th><th>time to accepted</th>"
        "<th>first-pass rate</th><th>total $</th></tr>"
    )
    arms = "".join(
        "<tr>"
        + "".join(
            f"<td>{cell}</td>"
            for cell in (
                _dashboard._e(arm),
                a["cases"],
                a["harness_errors"],
                a["accepted_verified"],
                _rate(a["pass_rate"]),
                a["unadjudicated_spot_checks"],
                _cell(a["cost_per_accepted"]),
                _cell(None)
                if a["time_to_accepted_s"] is None
                else _dashboard.duration(a["time_to_accepted_s"]),
                _rate(a["first_pass_rate"]),
                _cell(a["usd_total"]),
            )
        )
        + "</tr>"
        for arm, a in summary["arms"].items()
    )
    spreads = "".join(
        f"<p>{_dashboard._e(arm)} {_dashboard._e(field)}: min {_cell(values['min'])}, "
        f"max {_cell(values['max'])}, IQR {_cell(values['iqr'])}</p>"
        for arm, data in summary["arms"].items()
        for field, values in data["spread"].items()
    )
    columns = (
        "task",
        "repeat",
        "arm",
        "outcome",
        "verified",
        "review_passed",
        "passed_round",
        "qa_rounds",
        "gate_bounces",
        "asks",
        "usd_total",
        "wall_s",
        "check_summary",
    )
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{_cell(r[c])}</td>" for c in columns)
        + f"<td>{_dashboard._link(r['case_url'], r['case_run'])}</td></tr>"
        for r in records
    )
    notes = "".join(f"<li>{_dashboard._e(c)}</li>" for c in caveats(records)) or "<li>none</li>"
    decision = summary.get("verdict")
    comparison = ""
    if decision:
        comparison = (
            f"<h4>Verdict: {_cell(decision['verdict'])}: {_cell(decision['reason'])}</h4>"
            f"<p>A/A band b={_cell(decision['b'])}; σ={_cell(decision['sigma_aa'])}; "
            f"m={decision['m']}; source={_cell(decision['band_source'])}</p>"
        )
    calibration = summary.get("calibration")
    if calibration:
        comparison += (
            f"<p>A/A qualifies as calibration: {_cell(calibration['qualifies'])}; "
            f"σ={_cell(calibration['sigma_aa'])}</p>"
        )
    for reused in summary.get("reused_baselines", []):
        comparison += (
            f"<p>Reused baseline {_cell(reused['source_case'])}: age {_cell(reused['age_days'])} days</p>"
        )
    return (
        f'<div class="cx"><style>{_dashboard._CSS}</style><h3>{_dashboard._e(title)}</h3>'
        f"{comparison}<h4>Headline</h4><table>{head}{arms}</table>{spreads}"
        f"<h4>Cases</h4><table><tr>{''.join(f'<th>{c}</th>' for c in columns)}<th>run</th></tr>{rows}</table>"
        f"<h4>Caveats</h4><ul>{notes}</ul></div>"
    )


_LABEL_FIELDS = {
    "source",
    "run",
    "project",
    "engine_sha",
    "kind",
    "round",
    "name",
    "verdict",
    "file",
    "line",
    "title",
    "evidence_kind",
}
_GATE_STATUSES = {"PASS", "FAIL", "not applicable", "unknown (legacy history)", "tool unavailable"}


def validate_label(row: Mapping[str, Any]) -> None:
    """Reject malformed production labels before publishing them to the results branch."""
    if not isinstance(row, Mapping) or set(row) != _LABEL_FIELDS:
        raise ValueError("invalid production label fields")
    strings = _LABEL_FIELDS - {"round", "line"}
    if any(not isinstance(row[key], str) for key in strings):
        raise ValueError("production label text fields must be strings")
    if row["source"] != "production" or row["kind"] not in {"judge", "gate", "outcome", "merge"}:
        raise ValueError("invalid production label source or kind")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", row["run"]) or not row["project"]:
        raise ValueError("production labels require a run name and project")
    if row["engine_sha"] and not _SHA.fullmatch(row["engine_sha"]):
        raise ValueError("invalid production label engine SHA")
    if type(row["round"]) is not int or row["round"] < 0:
        raise ValueError("invalid production label round")
    if row["line"] is not None and (type(row["line"]) is not int or row["line"] < 1):
        raise ValueError("invalid production label line")
    if any(
        len(row[key]) > limit
        for key, limit in (("title", 200), ("file", 300), ("evidence_kind", 200), ("name", 200))
    ):
        raise ValueError("production label text exceeds its bound")
    allowed = {"judge": {"PASS", "FAIL"}, "gate": _GATE_STATUSES, "merge": {"merged", "closed", "open"}}
    if not row["verdict"] or (row["kind"] in allowed and row["verdict"] not in allowed[row["kind"]]):
        raise ValueError("invalid production label verdict")


def labels_from_report(
    report: Mapping[str, Any], pr_state: str | None, terminal_phase: str, report_state: str = ""
) -> list[dict[str, Any]]:
    """Return recorded judge evidence, tool gates and final outcomes, bounded to 300 production labels.

    `terminal_phase` is the remote run's own end state; the supervisor's last phase is recorded
    separately because a FAILED run's snapshot can still say "building". `report_state` is
    "missing" or "truncated" when the run has no usable snapshot: only the outcome and merge rows
    are written, and evidence_kind says why. Older reports have no engine provenance or standards
    results. Preserve those gaps instead of attributing the collector's current revision to a
    historical run.
    """
    if not isinstance(report, Mapping):
        raise ValueError("production report must be a mapping")
    phase, review = report.get("phase", {}), report.get("review", {})
    metrics = report.get("metrics", {})
    if not all(isinstance(part, Mapping) for part in (phase, review, metrics)):
        raise ValueError("production phase, review and metrics must be mappings")
    round_ = review.get("round", phase.get("round", 0))
    base = {
        "source": "production",
        "run": report.get("run", ""),
        "project": report.get("project", ""),
        "engine_sha": report.get("engine_sha", ""),
        "round": round_,
        "name": "",
        "file": "",
        "line": None,
        "title": "",
        "evidence_kind": "",
    }

    def label(kind: str, **fields: Any) -> dict[str, Any]:
        row = {**base, "kind": kind, **fields}
        if type(row["line"]) is int and row["line"] < 1:
            # Tool output cites line 0 (or `host:0`); it names no line.
            row["line"] = None
        for key, limit in (("title", 200), ("file", 300), ("evidence_kind", 200), ("name", 200)):
            if isinstance(row[key], str):
                row[key] = row[key][:limit]
        validate_label(row)
        return row

    final = [label("outcome", name="phase", verdict=terminal_phase, evidence_kind=report_state)]
    if not report_state:
        final.append(
            label(
                "outcome",
                name="supervisor_phase",
                verdict=phase.get("kind") or ("PASS" if review.get("passed") else "unknown"),
            )
        )
    pr_rows = [label("merge", name="pr", verdict=pr_state, evidence_kind=report_state)] if pr_state else []
    final += pr_rows
    if report_state:
        return final
    recorded = metrics.get("labels", [])
    if not isinstance(recorded, list):
        raise ValueError("judge labels must be a list")
    if any(not isinstance(entry, Mapping) for entry in recorded):
        raise ValueError("invalid recorded label")
    recorded = [entry for entry in recorded if entry.get("kind") == "judge"]
    # Budget order: outcome and merge rows, then judges (the primary signal), then gates.
    judge_budget = 300 - len(final)
    # Keep the latest recorded evidence.
    judges = [
        label("judge", **{key: value for key, value in entry.items() if key != "kind"})
        for entry in (recorded[-judge_budget:] if judge_budget else [])
    ]
    gates = []
    entries = report.get("standards_results", [])
    if not isinstance(entries, list):
        raise ValueError("standards results must be a list")
    for entry in entries[: 300 - len(final) - len(judges)]:
        if not isinstance(entry, Mapping):
            raise ValueError("standards result must be a mapping")
        output = entry.get("output", "")
        if not isinstance(output, str):
            raise ValueError("standards output must be text")
        path = _dashboard.LABEL_PATH.search(output)
        gates.append(
            label(
                "gate",
                name=entry.get("rule", ""),
                verdict=entry.get("status", ""),
                file=path[1] if path else "",
                line=int(path[2]) if path and path[2] else None,
                title=output.splitlines()[0] if output else "",
                evidence_kind="tool",
            )
        )
    return judges + gates + final
