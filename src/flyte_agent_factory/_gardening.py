"""Gardening: a weekly, low-priority project that tidies the repos the owner lists in `gardening.toml`.

`gardening.toml` sits at the workspace root and is the owner's, like `waivers.toml`: `_gates` always
blocks a session's change to it, so the factory never adds or removes a target. Without the file the
engine (`[repo] factory`) is the only target; a file with no `[[target]]` opts out. This module reads it,
and decides how urgently a queued gardening launch should take a slot (`priority`, a pure function).
"""

from __future__ import annotations

import configparser
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _profile, _queue

FILE = "gardening.toml"
#: The project the weekly task launches; the queue and the overview know it by this name.
NAME = "factory-gardening"
CHECKS = ("standards", "vulture", "duplicates", "orphans", "deps", "branches", "configs")
DAY = 24 * 3600
LABEL = "gardening"
_FIELDS = {"repo", "path", "cadence_days", "checks"}


@dataclass(frozen=True)
class Target:
    repo: str = ""  # a repository URL, after `[repo]` key indirection; or
    path: str = ""  # a project directory in the workspace
    cadence_days: int | None = None
    checks: tuple[str, ...] = CHECKS

    @property
    def name(self) -> str:
        return self.repo or self.path


def _target(index: int, raw: object, repos: Mapping[str, str]) -> Target:
    def bad(message: str) -> ValueError:
        return ValueError(f"{FILE} target {index}: {message}")

    if not isinstance(raw, dict):
        raise bad("must be a table")
    if unknown := sorted(set(raw) - _FIELDS):
        raise bad(f"unknown key(s): {', '.join(unknown)}")
    if ("repo" in raw) == ("path" in raw):
        raise bad("needs exactly one of repo or path")
    for field in ("repo", "path"):
        if field in raw and not (isinstance(raw[field], str) and raw[field]):
            raise bad(f"{field} must be a non-empty string")
    cadence = raw.get("cadence_days")
    if "cadence_days" in raw and (isinstance(cadence, bool) or not isinstance(cadence, int) or cadence < 1):
        raise bad("cadence_days must be a positive integer")
    checks = raw.get("checks", list(CHECKS))
    if not (isinstance(checks, list) and checks and all(isinstance(c, str) for c in checks)):
        raise bad("checks must be a non-empty list of strings")
    if unknown := [c for c in checks if c not in CHECKS]:
        raise bad(f"unknown check(s) {', '.join(unknown)}; known: {', '.join(CHECKS)}")
    repo = raw.get("repo", "")
    # `repo = "factory"` names the profile's `[repo] factory` entry, so the file carries no URL.
    return Target(repos.get(repo, repo), raw.get("path", ""), cadence, tuple(checks))


def load(workspace: Path, repos: Mapping[str, str]) -> list[Target]:
    """Load the workspace's targets; a missing file has none, an unreadable one raises ValueError."""
    path = workspace / FILE
    if not path.is_file():
        return []
    try:
        return parse(path.read_text(), repos)
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{FILE}: {exc}") from exc


def default_targets(repos: Mapping[str, str]) -> list[Target]:
    """Return the targets of a workspace without `gardening.toml`: the engine, when the profile names it."""
    return [Target(repos["factory"])] if "factory" in repos else []


def load_or_default(workspace: Path, repos: Mapping[str, str]) -> list[Target]:
    """`load`, except that a workspace without the file gets `default_targets`."""
    if not (workspace / FILE).is_file():
        return default_targets(repos)
    return load(workspace, repos)


def parse(text: str, repos: Mapping[str, str]) -> list[Target]:
    """Parse the text of `gardening.toml`; ValueError if it is not valid."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{FILE}: {exc}") from exc
    entries = data.get("target", [])
    if not isinstance(entries, list):
        raise ValueError(f"{FILE}: target must be an array of tables ([[target]])")
    return [_target(i, raw, repos) for i, raw in enumerate(entries)]


def priority(
    now: float,
    runs: Sequence[float],
    regression: Sequence[str],
    open_issues: int,
    settings: _profile.Gardening,
    targets: int | None = None,
) -> tuple[str, list[str]]:
    """How a queued gardening launch ranks, and why: ("low" | "normal" | "reserved", reasons).

    `runs` are the end times of completed gardening runs; `regression` names the targets whose standards
    gate on main has new findings since the last clean run. `normal` is the escalation: the launch is
    then served like any other work. `reserved` is the guaranteed weekly share: it goes ahead of normal
    work (but `_queue.PRIORITIES` keeps `expedite` first, see `rank`). `targets` is how many the owner
    lists, None when unknown; with none there is nothing to tend, so the launch stays `low`.
    """
    if targets == 0:
        return "low", [f"no targets in {FILE}"]
    reasons = []
    last = max(runs, default=None)
    if last is None:
        reasons.append("no completed run yet")
    elif now - last > settings.max_days_between * DAY:
        reasons.append(f"no completed run in {int((now - last) // DAY)} days")
    if regression and settings.escalate_on_regression:
        reasons.append(f"standards gate regressed on {', '.join(regression)}")
    if open_issues > settings.max_open_issues:
        reasons.append(f"{open_issues} open gardening issues (max {settings.max_open_issues})")
    this_week = sum(now - t <= 7 * DAY for t in runs)
    if this_week < settings.min_runs_per_week:
        return "reserved", [
            *reasons,
            f"reserved share ({this_week} of {settings.min_runs_per_week} runs in 7 days)",
        ]
    return ("normal" if reasons else "low"), reasons


#: Admission order of the priorities a request can be ranked by, first admitted first: the stored ones,
#: with `reserved` (only `priority` returns it) between expedite and normal.
ORDER = ("expedite", "reserved", "normal", "low")


def rank(effective: str) -> int:
    return ORDER.index(effective)


class Admission:
    """The admission order of waiting requests and the rule that holds `low`; both admission paths use it.

    `levels` are effective priorities (`ORDER`), one per waiting request. `order` lists their indices first
    admitted first; within a level the given order stays, or follows `ties`, a flag per request where
    False goes first. A `low` request is `held` while any request that is not low still waits and could
    start: `admitted` releases a request that took a slot, `mark_unrunnable` one that cannot start now for a
    reason of its own (its lane, engine, account, project), which must not keep the slots from `low` work
    it does not apply to. `ahead` counts the requests a held `low` one waits behind.
    """

    def __init__(self, levels: Sequence[str], ties: Sequence[bool] | None = None) -> None:
        self._levels = levels
        keys = ties if ties is not None else [False] * len(levels)
        self.order = sorted(range(len(levels)), key=lambda i: (rank(levels[i]), keys[i]))
        self._not_low = sum(level != "low" for level in levels)
        self.started: set[int] = set()

    def held(self, index: int) -> bool:
        return self._levels[index] == "low" and self._not_low > 0

    @property
    def ahead(self) -> int:
        return self._not_low

    def admitted(self, index: int) -> None:
        self.started.add(index)
        self._not_low -= self._levels[index] != "low"

    def mark_unrunnable(self, index: int) -> None:
        self._not_low -= self._levels[index] != "low"


def describe(level: str, reasons: Sequence[str]) -> str:
    """Return a decision's overview line, e.g. `gardening: normal priority (no completed run in 9 days)`."""
    return f"gardening: {level} priority" + (f" ({'; '.join(reasons)})" if reasons else "")


def _counts_path(uri: str) -> str:
    return f"{uri.rstrip('/')}/gardening/counts.json"


Counts = dict[str, dict[str, int]]  # target -> standards rule -> findings


def _clean_counts(raw: object) -> Counts:
    if not isinstance(raw, dict):
        return {}
    return {
        t: {rule: n for rule, n in rules.items() if isinstance(n, int)}
        for t, rules in raw.items()
        if isinstance(rules, dict)
    }


def read_counts(uri: str) -> dict[str, Counts]:
    """Standards-gate findings per target and rule: `clean` is the baseline, `latest` the newest scan."""
    body = _queue.read_json(_counts_path(uri))
    return {key: _clean_counts(body.get(key)) for key in ("clean", "latest")}


def record_counts(uri: str, scans: Mapping[str, Mapping[str, int]]) -> None:
    """Store `scans` as `latest`, and move a target's `clean` baseline to its scan unless a rule got worse.

    The first scan of a target is its baseline. Whether a gardening project finished is the agent's to
    know, which the factory module cannot hook, so a scan with no more findings than the baseline on
    every rule is taken as the new clean one, and a worse one leaves the baseline where it was.
    """
    clean = read_counts(uri)["clean"]
    for target, rules in scans.items():
        if target not in clean or all(n <= clean[target].get(rule, 0) for rule, n in rules.items()):
            clean[target] = dict(rules)
    _queue.write_json(_counts_path(uri), {"clean": clean, "latest": {t: dict(r) for t, r in scans.items()}})


def regressed(counts: Mapping[str, Mapping[str, Mapping[str, int]]]) -> list[str]:
    """`target: rule` for each rule with more findings at the newest scan than at the target's baseline."""
    clean, latest = counts.get("clean", {}), counts.get("latest", {})
    return sorted(
        f"{target}: {rule}"
        for target, rules in latest.items()
        if target in clean
        for rule, n in rules.items()
        if n > clean[target].get(rule, 0)
    )


def _setting(value: object) -> frozenset[str] | None:
    """Return a config value as a set of strings, so list order and TOML/ini spelling do not differ."""
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    # TOML says `true`, ini says `True`.
    return frozenset(str(i).lower() if str(i).lower() in ("true", "false") else str(i) for i in items)


def _ruff_settings(ruff: Mapping[str, Any]) -> dict[str, frozenset[str] | None]:
    """Return the ruff settings that the engine and the template are meant to share, by name."""
    lint = ruff.get("lint", {})
    per_file = lint.get("per-file-ignores", {})
    return {
        "line-length": _setting(ruff.get("line-length")),
        "select": _setting(lint.get("select")),
        "ignore": _setting(lint.get("ignore")),
        "pydocstyle convention": _setting(lint.get("pydocstyle", {}).get("convention")),
        "per-file-ignores": _setting([f"{glob}={code}" for glob, codes in per_file.items() for code in codes])
        if per_file
        else None,
    }


def _differences(
    tool: str, engine: Mapping[str, frozenset[str] | None], template: Mapping[str, frozenset[str] | None]
) -> list[str]:
    return [
        f"{tool} {key}: engine {_show(engine.get(key))}, template {_show(template.get(key))}"
        for key in dict.fromkeys([*engine, *template])
        if engine.get(key) != template.get(key)
    ]


def _show(setting: frozenset[str] | None) -> str:
    return "unset" if setting is None else ", ".join(sorted(setting))


def config_drift(pyproject: Path, configs: Path) -> list[str]:
    """Return one line per setting where the engine's tool config differs from the template's baseline.

    `pyproject` is the engine's; `configs` is the directory holding the baseline `ruff.toml` and `mypy.ini`.
    """
    engine = tomllib.loads(pyproject.read_text()).get("tool", {})
    ruff = tomllib.loads((configs / "ruff.toml").read_text())
    # Interpolation off: mypy's `exclude` is a regex and may carry a `%`.
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(configs / "mypy.ini")
    mypy_template = {k: _setting(v) for k, v in sorted(ini["mypy"].items())}
    # `overrides` is an array of tables in pyproject and `[mypy-*]` sections in the ini: not compared.
    mypy_engine = {k: _setting(v) for k, v in sorted(engine.get("mypy", {}).items()) if k != "overrides"}
    return [
        *_differences("ruff", _ruff_settings(engine.get("ruff", {})), _ruff_settings(ruff)),
        *_differences("mypy", mypy_engine, mypy_template),
    ]
