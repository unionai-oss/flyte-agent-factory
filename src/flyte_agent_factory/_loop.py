"""Admission, benchmark decisions and the owner-controlled loop kill switch."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, TypedDict, cast

from typing_extensions import NotRequired

from . import _github, _router

if TYPE_CHECKING:
    from ._meter import Meter
    from ._profile import Profile

#: What the loop never merges on its own (owner, inbox 028: stop only for irreversible actions, spend or
#: credentials): the gates and standards that define "passed", the benchmark and loop that judge a change (a
#: candidate must not change its own judge), credentials, deployment and CI. Everything else, agent.py and
#: the policy knobs included, is ordinary code the benchmark measures; its hidden checks mean a more lenient
#: review cannot buy passes.
OWNER_PATHS = (
    "src/flyte_agent_factory/_gate*.py",
    "src/flyte_agent_factory/_standards*.py",
    "standards/*",
    "src/flyte_agent_factory/_bench*.py",
    "src/flyte_agent_factory/_loop*.py",
    "src/flyte_agent_factory/_backlog.py",
    "src/flyte_agent_factory/_coord.py",
    "ops/bench_*",
    "ops/model_eval/*",
    "profiles/*",
    "src/flyte_agent_factory/_profile.py",
    "src/flyte_agent_factory/_auth.py",
    # Holds the signed-in browsers' tokens and decides what a session may do as the owner.
    "src/flyte_agent_factory/_browser_gate.py",
    "src/flyte_agent_factory/_github.py",
    "src/flyte_agent_factory/_pull_requests.py",
    "docs/security.md",
    "SECURITY.md",
    "src/flyte_agent_factory/_deployer.py",
    "src/flyte_agent_factory/_deploy*.py",
    "src/flyte_agent_factory/_webhook.py",
    ".github/*",
    "ops/deploy_*",
    "DESIGN.md",
    "AGENTS.md",
    "CLAUDE.md",
    # What CI runs and how: the checks script is the commit-health gate, pyproject its lint and test config.
    "factory-improvement/*",
    "pyproject.toml",
    "uv.lock",
    "tests/conftest.py",
    # Steering documents (design-loop 4.2).
    "ops/briefs/*",
    ".briefs/*",
)
#: GitHub lists at most this many files of a pull request; a longer change cannot be classified.
MAX_LISTED_FILES = 3000
#: Merged without a benchmark when review passed. Not tests/: a tests-only change is benchmarked, so a
#: weakened test cannot slip in unmeasured.
_DOCS_PATHS = ("docs/",)
MARKER = "<!-- factory-loop:decision "


def _normal(path: str) -> str:
    """`./a//b/./c` is `a/b/c`: an unnormalised spelling of an owner file must not slip past the patterns."""
    import posixpath

    normal = posixpath.normpath(path.replace("\\", "/"))
    return normal.lstrip("/") if normal != "." else ""


def _owner(path: str) -> bool:
    path = _normal(path)
    if path.startswith("../") or path == "..":
        return True  # outside the repository: not something the loop can classify
    for pattern in OWNER_PATHS:
        if fnmatchcase(path, pattern):
            return True
        # A package `_gates/` shadows the module `_gates.py` at import, so it is the same owner file.
        if pattern.endswith(".py") and fnmatchcase(path, pattern[: -len(".py")] + "/*"):
            return True
    return False


class ChangedFile(TypedDict):
    filename: str
    previous_filename: NotRequired[str]


class _HeadRepo(TypedDict, total=False):
    full_name: str


class _PullHead(TypedDict, total=False):
    ref: str
    repo: _HeadRepo | None


class Pull(TypedDict, total=False):
    head: _PullHead | None


class Spend(TypedDict):
    at: float
    engine: str
    usd_total: float


def classify(pr_files: Sequence[str | ChangedFile], changed_files: int | None = None, path: str = "") -> str:
    """Return `needs_owner`, `docs_only` or `benchmark`.

    `changed_files` is the PR's own count: a listing that does not account for every changed file
    (GitHub stops at 3000) cannot show that no owner file changed.

    `path` is the subtree holding the engine (`[loop] path`). Its files are judged relative to it, as the
    owner's patterns name the engine's own layout; a file outside it is not the engine, so not the loop's.
    """
    if changed_files is not None and (len(pr_files) != changed_files or len(pr_files) >= MAX_LISTED_FILES):
        return "needs_owner"
    paths = []
    for item in pr_files:
        if isinstance(item, str):
            paths.append(item)
        else:
            paths.append(item["filename"])
            if item.get("previous_filename"):
                paths.append(item["previous_filename"])
    if path:
        prefix = _normal(path) + "/"
        # Normalised first: `projects/engine/../../x` must not look like it is inside the subtree.
        paths = [_normal(p) for p in paths]
        if not all(p.startswith(prefix) for p in paths):
            return "needs_owner"
        paths = [p[len(prefix) :] for p in paths]
    if not paths or any(_owner(p) for p in paths):
        return "needs_owner"
    return "docs_only" if all(p.startswith(_DOCS_PATHS) for p in paths) else "benchmark"


def decide(verdict: Mapping[str, object], classify_result: str, review_passed: bool) -> str:
    if classify_result == "needs_owner":
        return "needs_owner"
    if not review_passed:
        return "skip"
    if classify_result == "docs_only":
        return "merge"
    if classify_result != "benchmark":
        return "needs_owner"
    if verdict.get("verdict") == "worse":
        return "close"
    m = verdict.get("m")
    if (
        verdict.get("verdict") not in ("better", "no_worse")
        or verdict.get("band_source") != "measured"
        or not isinstance(m, int)
        or isinstance(m, bool)
        or m < 6
        or verdict.get("reason") != "cost"
    ):
        return "inconclusive"
    # Keep the merge gate closed even if an incomplete or malformed summary claims a passing verdict.
    numbers = [verdict.get(key) for key in ("S", "L", "U", "b", "sigma_aa")]
    if not all(isinstance(n, (float, int)) and not isinstance(n, bool) and math.isfinite(n) for n in numbers):
        return "inconclusive"
    # The finite-number validation above rules out missing values and non-numeric fields.
    score, lower, upper, band, sigma = cast(list[float], numbers)
    if band < 0.1 or sigma < 0 or not lower <= score <= upper:
        return "inconclusive"
    # The band must be the one the A/A implies (design-loop 2.2); any other is not a measured verdict.
    if not math.isclose(band, max(0.1, 1.64 * sigma / math.sqrt(m)), rel_tol=1e-6, abs_tol=1e-9):
        return "inconclusive"
    return "merge" if upper < -band or (upper <= 2 * band and score <= band) else "inconclusive"


def decision_comment(
    decision: str, verdict: Mapping[str, object], head: str, baseline: str, *, mode: str = "on"
) -> str:
    evidence = {key: verdict.get(key) for key in ("verdict", "reason", "b", "sigma_aa", "m", "S", "L", "U")}
    record = {"head": head, "baseline": baseline, "decision": decision, "mode": mode, **evidence}
    detail = ", ".join(f"{key}={value}" for key, value in evidence.items())
    marker = f"{MARKER}{json.dumps(record, sort_keys=True)} -->"
    return f"Factory loop: **{decision}** ({mode}).\n\n{detail}\n\n{marker}"


def _loop_run_labels(project: str) -> list[str]:
    """Return the `factory-loop` labels (each names an issue) of the runs launched for project `project`."""
    from . import _factory_base

    return [
        labels["factory-loop"]
        for labels in map(_factory_base.run_labels, _factory_base._labelled_runs(project))
        if labels.get("factory-loop")
    ]


def loop_pull(app: _github.GithubApp, repo: str, pull: Pull) -> bool:
    """Whether `pull` was opened by a loop project, the only kind of PR the loop may merge or close.

    The authority is the launch record, not anything the PR says about itself: the PR's branch is
    `factory/<project>`, a run of that project carries `factory-loop` naming its issue, and that issue
    is open to the loop's own tests: labelled `loop`, with the backlog generator's marker, written by
    the App. A PR body ("Closes #7") is free text anyone can write, so it is never consulted.
    """
    from . import _issues

    head = pull.get("head") or {}
    ref = head.get("ref") or ""
    if (head.get("repo") or {}).get("full_name", "").lower() != repo.lower() or not ref.startswith(
        "factory/"
    ):
        return False
    launched = set(_loop_run_labels(ref.removeprefix("factory/")))
    if not launched:
        return False
    login = app.app_login().lower()
    for issue in app.labelled_issues(repo, "loop"):
        if (
            "pull_request" not in issue
            and any((label.get("name") or "").lower() == "loop" for label in issue.get("labels", []))
            and re.search(r"^<!-- loop-source: [^\n]+ -->$", issue.get("body") or "", re.M)
            and (issue.get("user") or {}).get("login", "").lower() == login
            and _issues.issue_label(f"{repo}#{issue['number']}") in launched
        ):
            return True
    return False


def killed(profile: Profile, app: _github.GithubApp | None = None) -> bool:
    if app is None:
        import os

        from ._pull_requests import KEY_ENV_VAR

        app = _github.GithubApp(profile.github.app_id, os.environ[KEY_ENV_VAR])
    maintainers = {login.lower() for login in profile.github.maintainers}
    trusted = maintainers | {app.app_login().lower()}
    for issue in app.labelled_issues(profile.loop.repo, "loop-kill"):
        active = False
        trusted_label = False
        for event in app.issue_events(profile.loop.repo, issue["number"]):
            actor = (event.get("actor") or {}).get("login", "").lower()
            if event["event"] == "labeled" and (event.get("label") or {}).get("name") == "loop-kill":
                if actor in trusted:
                    trusted_label = True
                    active = True
            elif event["event"] == "closed" and actor in maintainers:
                active = False
            elif event["event"] == "reopened" and trusted_label:
                active = True
        if active:
            return True
    return False


def lint_contract(body: str) -> list[str]:
    """Contract errors in an issue body; quoted evidence cannot supply contract fields."""
    # HTML comments are invisible on the issue, so they cannot carry a contract field either.
    visible = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    text = "\n".join(line for line in visible.splitlines() if not line.startswith(">"))
    fields = {}
    for match in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        fields[match[1].strip()] = match[2].strip()
    aliases = {"Files": "Files likely touched", "Acceptance": "Acceptance criteria"}
    for short, long in aliases.items():
        fields.setdefault(long, fields.get(short, ""))
    required = (
        "Goal",
        "Why",
        "Files likely touched",
        "Acceptance criteria",
        "Dependencies",
        "Size",
        "Expected effect",
    )
    errors = [f"missing {name}" for name in required if not fields.get(name)]
    if fields.get("Size") not in ("S", "M"):
        errors.append("Size must be S or M")
    if fields.get("Expected effect") not in ("cost", "rounds", "neutral"):
        errors.append("Expected effect must be cost, rounds or neutral")
    paths = re.findall(r"`([^`]+)`", fields.get("Files likely touched", ""))
    if not paths or any(
        PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or "\\" in path
        or re.search(r"\s", path)
        for path in paths
    ):
        errors.append("Files must contain repo-relative paths or globs in backticks")
    acceptance = fields.get("Acceptance criteria", "")
    if not re.search(r"`[^`]+`.*(?:pass|exit|expected|result|no_worse)", acceptance, re.I | re.S):
        if not re.search(r"benchmark task: [a-z0-9][a-z0-9_-]*", acceptance):
            errors.append("Acceptance needs a command and expected result or benchmark task id")
    # The marker is itself a comment, so it is found in the unstripped (non-quoted) body.
    unquoted = "\n".join(line for line in body.splitlines() if not line.startswith(">"))
    if len(re.findall(r"^<!-- loop-source: [A-Za-z0-9_.:-]+ -->$", unquoted, re.M)) != 1:
        errors.append("one loop-source marker is required")
    return errors


@dataclass(frozen=True)
class Budget:
    action: str
    reason: str = ""
    until: float | None = None


def budget(
    now: float,
    spend: Sequence[Spend],
    meters: Mapping[str, Meter | None],
    owner_demand: bool,
    killed: bool,
    *,
    daily_usd: float = 0,
    weekly_usd: float = 0,
    owner_floor_pct: float = 30,
) -> Budget:
    """Pure admission; spend rows contain `at`, `engine` and API-equivalent `usd_total`."""
    if killed:
        return Budget("wait", "killed")
    midnight = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = (midnight + timedelta(days=1)).timestamp()
    if daily_usd <= 0 or weekly_usd <= 0:
        return Budget("wait", "caps-unset")
    daily: dict[str, float] = {}
    for row in spend:
        if midnight.timestamp() <= row["at"] <= now:
            daily[row["engine"]] = daily.get(row["engine"], 0) + row["usd_total"]
    if any(usd >= daily_usd for usd in daily.values()):
        return Budget("wait", "cap-daily", tomorrow)
    resets = []
    for engine in set(meters) | {row["engine"] for row in spend}:
        meter = meters.get(engine)
        reset = meter.weekly_resets_at if meter else None
        # Without a weekly meter, a rolling seven-day window prevents a calendar reset granting extra spend.
        reset = reset if reset is not None and reset > now else None
        start = reset - 7 * 86400 if reset is not None else now - 7 * 86400
        rows = [row for row in spend if row["engine"] == engine and start <= row["at"] <= now]
        if sum(row["usd_total"] for row in rows) >= weekly_usd:
            resets.append(reset if reset is not None else min(row["at"] for row in rows) + 7 * 86400)
    if resets:
        return Budget("wait", "cap-weekly", max(resets))
    decision = _router.route("loop", meters, available=tuple(meters), now=now)
    if decision.deferred:
        return Budget("wait", "router-deferred", decision.retry_at)
    if owner_demand:
        usable = [c for c in decision.candidates if c.engine in meters and c.state != "exhausted"]
        if not any(c.headroom is not None and c.headroom > owner_floor_pct for c in usable):
            return Budget("wait", "owner-demand", now + _router.DEFAULT_RETRY_S)
    return Budget("run")


def breaker(
    now: float, reverts: Sequence[float] = (), aa: Sequence[str] = (), harness: Sequence[str] = ()
) -> str:
    """Decide whether to trip; inputs are revert timestamps, A/A verdicts and case outcomes, oldest first."""
    if sum(now - 7 * 86400 <= at <= now for at in reverts) >= 2:
        return "reverts"
    if any(verdict in ("better", "worse") for verdict in aa):
        return "aa"
    if len(harness) >= 3 and all(outcome == "harness_error" for outcome in harness[-3:]):
        return "harness-errors"
    return ""


def restarted_at(profile: Profile, app: _github.GithubApp) -> float:
    """Only a maintainer's close resets the breaker's evidence window."""
    maintainers = {login.lower() for login in profile.github.maintainers}
    closed = [0.0]
    for issue in app.labelled_issues(profile.loop.repo, "loop-kill"):
        for event in app.issue_events(profile.loop.repo, issue["number"]):
            if (
                event.get("event") == "closed"
                and (event.get("actor") or {}).get("login", "").lower() in maintainers
                and event.get("created_at")
            ):
                closed.append(datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp())
    return max(closed)


def open_breaker(profile: Profile, app: _github.GithubApp, reason: str) -> None:
    """Only the owner can close this switch; retries reuse an already active switch."""
    if not reason or killed(profile, app):
        return
    issue = app.create_issue(
        profile.loop.repo,
        "Factory loop stopped: " + reason,
        "Circuit breaker: " + reason + ". The owner must close this issue to resume.",
    )
    app.add_labels(profile.loop.repo, issue["number"], ["loop-kill"])
