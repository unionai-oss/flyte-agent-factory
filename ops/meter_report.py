"""How many full 5-hour windows does the weekly limit hold? Estimated from the meter history.

    python ops/meter_report.py history.jsonl [more.jsonl ...]

Reads the supervisor's `meters/history.jsonl` (under its state directory) or a task log holding
its `[meter] {...}` lines, e.g. from `flyte get logs`. Per account it fits the weekly-percent
gained against the 5-hour percent gained, within each 5-hour window: using every window's own
first and last reading keeps unobserved gaps out of the sums. A window of 100% five-hour usage
costs `100 * slope` weekly percent, so the weekly limit holds `1 / slope` of them.

The range is the slope's two-standard-error interval; the rounding of the readings (whole
percents) is part of the scatter it sees. Few windows give a wide range, and the report says how
many it used.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: A window that gained less than this many 5-hour percent says nothing through whole-percent rounding.
MIN_FIVE_HOUR_GAIN = 5.0
#: Fewer windows than this give no error range worth printing.
MIN_WINDOWS = 3


def read_history(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Return samples from history files or task logs; lines that are not a meter sample are skipped."""
    samples, seen, skipped = [], set(), 0
    for path in paths:
        for line in Path(path).read_text(errors="replace").splitlines():
            line = line.strip().removeprefix("[meter] ")
            if not line.startswith("{"):
                continue
            try:
                sample = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not (
                isinstance(sample, dict)
                and isinstance(sample.get("account"), str)
                and sample["account"]
                and isinstance(sample.get("t"), (int, float))
                and not isinstance(sample["t"], bool)
                and math.isfinite(sample["t"])
                and all(
                    value is None
                    or (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                    )
                    for value in (
                        sample.get(k)
                        for k in ("five_hour", "weekly", "five_hour_resets_at", "weekly_resets_at")
                    )
                )
            ):
                skipped += 1
                continue
            key = (sample["account"], sample["t"])
            if key not in seen:
                samples.append(sample)
                seen.add(key)
    if skipped:
        print(f"skipped {skipped} malformed history lines", file=sys.stderr)
    return samples


def _windows(samples: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """(5-hour gain, weekly gain) per 5-hour window, from its first to its last reading."""
    groups: defaultdict[tuple[int, int], list[tuple[float, float, float]]] = defaultdict(list)
    for s in samples:
        five, week = s.get("five_hour"), s.get("weekly")
        five_at, week_at = s.get("five_hour_resets_at"), s.get("weekly_resets_at")
        if five is None or week is None or five_at is None or week_at is None:
            continue
        # By reset time, to the minute: it wobbles by fractions of a second between readings.
        groups[(round(five_at / 60), round(week_at / 60))].append((s["t"], five, week))
    gains = []
    for readings in groups.values():
        readings.sort()
        d5, dw = readings[-1][1] - readings[0][1], readings[-1][2] - readings[0][2]
        if len(readings) >= 2 and d5 >= MIN_FIVE_HOUR_GAIN and dw >= 0:
            gains.append((d5, dw))
    return gains


def estimate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Windows per week for one account's samples: {windows, low, high, n_windows, n_samples}.

    `windows` is None when there is no usable window; `low`/`high` are None below MIN_WINDOWS, and
    `high` is None (unbounded) when the slope's interval reaches zero.
    """
    gains = _windows(samples)
    result: dict[str, Any] = {
        "windows": None,
        "low": None,
        "high": None,
        "n_windows": len(gains),
        "n_samples": len(samples),
    }
    sxx = sum(d5 * d5 for d5, _ in gains)
    slope = sum(d5 * dw for d5, dw in gains) / sxx if sxx else 0.0
    if slope <= 0:
        return result
    result["windows"] = 1.0 / slope
    if len(gains) >= MIN_WINDOWS:
        residual = sum((dw - slope * d5) ** 2 for d5, dw in gains)
        stderr = math.sqrt(residual / (len(gains) - 1) / sxx)
        result["low"] = 1.0 / (slope + 2 * stderr)
        result["high"] = 1.0 / (slope - 2 * stderr) if slope > 2 * stderr else None
    return result


def report(samples: list[dict[str, Any]]) -> list[str]:
    """Return one line per account estimating its window sizes from the samples."""
    by_account: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in samples:
        by_account[s["account"]].append(s)
    lines = []
    for account, rows in sorted(by_account.items()):
        found = estimate(rows)
        used = f"from {found['n_windows']} windows, {found['n_samples']} samples"
        if found["windows"] is None:
            lines.append(
                f"{account}: no estimate ({used}; needs windows that gained 5-hour and weekly percent)"
            )
        elif found["low"] is None:
            lines.append(
                f"{account}: about {found['windows']:.1f} five-hour windows per week, no error range ({used})"
            )
        else:
            high = f"{found['high']:.1f}" if found["high"] is not None else "unbounded"
            lines.append(
                f"{account}: about {found['windows']:.1f} five-hour windows per week "
                f"(range {found['low']:.1f} to {high}, two standard errors; {used})"
            )
    return lines


def main(argv: list[str] | None = None) -> None:
    """Print the meter estimate for each account found in the history files."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("history", nargs="+", help="history.jsonl files, or task logs with [meter] lines")
    samples = read_history(parser.parse_args(argv).history)
    for line in report(samples) or ["no meter samples found"]:
        print(line)


if __name__ == "__main__":
    main()
