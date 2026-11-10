"""The #92 context-guard A/B analysis, from the case records of a factory-bench A/B run and an A/A run.

    python factory-improvement/context_guard_bench.py fetch AB_RUN AA_RUN  # writes bench/context-guard/
    python factory-improvement/context_guard_bench.py report  # prints the markdown tables

`fetch` reads each `run_case` output of the two runs (it needs an interpreter that can attach to a Flyte run);
`report` needs only the committed JSONL files. The verdict is `_bench_stats.verdict` (the bench's own), with
the A/A run as the noise band; the sensitivity rows drop cases that waited on the login's usage limit.
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flyte_agent_factory import _bench_stats

#: One case record as the JSONL holds it.
Record = dict[str, Any]

DATA = Path(__file__).resolve().parent / "bench" / "context-guard"
KEEP = (
    "task kind arm policy repeat outcome accepted_verified review_passed first_pass "
    "verified qa_rounds wall_s waited_on_limit_s usd_total usd_complete usd_author usd_by_role tokens "
    "heldout engine_sha"
).split()


async def _fetch(run_names: list[str]) -> None:
    import flyte.remote as remote

    from flyte_agent_factory.child import attach

    attach()
    DATA.mkdir(parents=True, exist_ok=True)
    for label, name in zip(("ab", "aa"), run_names, strict=True):
        rows = []
        for action in remote.Action.listall(for_run_name=name):
            if not action.pb2.metadata.task.id.name.endswith("run_case") or not str(action.phase).endswith(
                "SUCCEEDED"
            ):
                continue
            details = await remote.ActionDetails.get.aio(run_name=name, name=action.name)
            outputs = await details.outputs()
            record = json.loads(outputs[0])
            rows.append({key: record.get(key) for key in KEEP})
        rows.sort(key=lambda r: (r["task"], r["arm"], r["repeat"]))
        (DATA / f"{label}.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        print(f"{label}: {name}, {len(rows)} cases")


def _load(label: str) -> list[Record]:
    return [json.loads(line) for line in (DATA / f"{label}.jsonl").read_text().splitlines() if line]


def _coordinator_cache_read_of(record: Record) -> float:
    return _bench_stats._coord_cache_read(cast(_bench_stats.CaseRecord, record)) or 0.0


def _per_accepted(records: list[Record], arm: str) -> tuple[int, int, float | None]:
    cases = [r for r in records if r["arm"] == arm]
    accepted = sum(bool(r["accepted_verified"]) for r in cases)
    total = sum(_coordinator_cache_read_of(r) for r in cases)
    return len(cases), accepted, (total / accepted if accepted else None)


def _fmt_m(tokens: float | None) -> str:
    return "n/a" if tokens is None else f"{tokens / 1e6:.2f}M"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None or (isinstance(value, float) and math.isnan(value)) else f"{value:+.3f}"


def _summary(name: str, records: list[Record], aa: list[Record]) -> list[str]:
    lines = [
        f"#### {name}",
        "",
        "| arm | cases | accepted | mean review rounds (accepted) | coordinator cache-read "
        "per accepted case | usd |",
        "|---|---|---|---|---|---|",
    ]
    for arm in ("baseline", "candidate"):
        n, accepted, per = _per_accepted(records, arm)
        rounds = [r["qa_rounds"] for r in records if r["arm"] == arm and r["accepted_verified"]]
        usd = sum(r["usd_total"] for r in records if r["arm"] == arm)
        mean_rounds = f"{statistics.mean(rounds):.2f}" if rounds else "n/a"
        lines.append(f"| {arm} | {n} | {accepted} | {mean_rounds} | {_fmt_m(per)} | {usd:.2f} |")
    result = _bench_stats.verdict(
        cast("list[_bench_stats.CaseRecord]", records),
        [cast("list[_bench_stats.CaseRecord]", aa)] if aa else [],
        min_tasks=1,
        min_repeats=1,
    )
    lines += [
        "",
        f"Bench verdict (min sample relaxed to what ran): **{result['verdict']}** ({result['reason']}), "
        f"m={result['m']} matched tasks, cost band b={result['b']:.3f} ({result['band_source']}).",
        "",
    ]
    lines += ["| guard | S (mean log ratio B/A) | 90% interval | band b | verdict |", "|---|---|---|---|---|"]
    lines.append(
        f"| cost | {_fmt(result['S'])} | {_fmt(result['L'])} .. {_fmt(result['U'])} | {result['b']:.3f} | "
        f"{result['verdict'] if result['reason'] == 'cost' else '-'} |"
    )
    for metric, guard in result["guards"].items():
        lines.append(
            f"| {metric} | {_fmt(guard['S'])} | {_fmt(guard['L'])} .. {_fmt(guard['U'])} | "
            f"{guard['b']:.3f} | {guard['verdict']} |"
        )
    return lines + [""]


def _cases(records: list[Record]) -> list[str]:
    lines = [
        "| task | arm | repeat | outcome | review rounds | waited on limit (min) | coordinator cache-read | "
        "usd |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        waited = (r.get("waited_on_limit_s") or 0) / 60
        lines.append(
            f"| {r['task']} | {r['arm']} | {r['repeat']} | {r['outcome']} | {r['qa_rounds']} | "
            f"{waited:.0f} | {_fmt_m(_coordinator_cache_read_of(r))} | {r['usd_total']:.2f} |"
        )
    return lines + [""]


def _matched_no_wait(name: str, records: list[Record]) -> list[str]:
    """Per task, the mean coordinator cache-read of accepted cases that never waited on the usage limit."""
    lines = [
        f"#### {name}",
        "",
        "| task | baseline cases | baseline mean | candidate cases | candidate mean | ratio B/A |",
        "|---|---|---|---|---|---|",
    ]
    logs = []
    for task in sorted({r["task"] for r in records}):
        means = {}
        for arm in ("baseline", "candidate"):
            reads = [
                _coordinator_cache_read_of(r)
                for r in records
                if r["task"] == task
                and r["arm"] == arm
                and r["accepted_verified"]
                and not r.get("waited_on_limit_s")
            ]
            means[arm] = (len(reads), statistics.mean(reads) if reads else None)
        (nb, mb), (nc, mc) = means["baseline"], means["candidate"]
        ratio = mc / mb if mb and mc else None
        if ratio:
            logs.append(math.log(ratio))
        lines.append(
            f"| {task} | {nb} | {_fmt_m(mb)} | {nc} | {_fmt_m(mc)} | "
            f"{'n/a' if ratio is None else f'{ratio:.2f}'} |"
        )
    if logs:
        lines += [
            "",
            f"{len(logs)} tasks with a no-wait accepted case in both arms; geometric mean ratio "
            f"{math.exp(statistics.mean(logs)):.2f} (mean log ratio {statistics.mean(logs):+.3f}).",
        ]
    return lines + [""]


def report() -> str:
    """Return the markdown tables for docs/context-guard.md from the committed case records."""
    ab, aa = _load("ab"), _load("aa")
    clean = [r for r in ab if not r.get("waited_on_limit_s")]
    clean_aa = [r for r in aa if not r.get("waited_on_limit_s")]
    out = _summary(
        "A/B: guard off (baseline) vs on (candidate); band assumed, "
        "A/A too small to calibrate (4 matched tasks, 6 needed)",
        ab,
        aa,
    )
    out += _summary("A/A: both arms guard off (the noise it measures)", aa, [])
    out += _summary("Sensitivity: cases that waited on the usage limit left out", clean, clean_aa)
    out += _matched_no_wait("Matched no-wait accepted cases, A/B", ab)
    out += _matched_no_wait("Matched no-wait accepted cases, A/A (same comparison where nothing differs)", aa)
    out += ["#### Cases, A/B", ""] + _cases(ab) + ["#### Cases, A/A", ""] + _cases(aa)
    return "\n".join(out)


if __name__ == "__main__":
    if sys.argv[1:2] == ["fetch"]:
        asyncio.run(_fetch(sys.argv[2:4]))
    else:
        print(report())
