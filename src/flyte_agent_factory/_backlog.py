"""Improvement contracts from measurements and notes; GitHub issues are the durable ledger."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import TypedDict, cast

from . import _board, _github, _loop


@dataclass(frozen=True)
class Draft:
    key: str
    title: str
    body: str
    files: tuple[str, ...]


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _draft(key: str, title: str, evidence: str, files: tuple[str, ...]) -> Draft:
    # Source paths are evidence too: do not let a forged path escape the Files field or avoid owner review.
    if not files or any(not re.fullmatch(r"[A-Za-z0-9_./*?-]+", path) or ".." in path for path in files):
        files = ("src/flyte_agent_factory/*",)
    quote = "\n".join(f"> {line}" for line in evidence.splitlines())
    body = (
        f"<!-- loop-source: {key} -->\n\n"
        "## Goal\nApply the recorded finding to the factory with a measured improvement.\n\n"
        "## Why\nFactory measurements or owner direction identify an improvement hypothesis.\n\n"
        "## Files likely touched\n" + "\n".join(f"- `{path}`" for path in files) + "\n\n"
        "## Acceptance criteria\n"
        "- Run `bash factory-improvement/checks.sh`; expected result: all checks pass.\n"
        "- Compare the changed factory against main on the same benchmark tasks; expected result: "
        "equal pass rate and no_worse cost. Apply the measured lesson to factory code.\n\n"
        "## Dependencies\nNone.\n\n"
        "## Size\nS\n\n"
        "## Expected effect\ncost\n\n"
        "## Source evidence\n" + quote
    )
    return Draft(key, title, body, files)


class _Record(TypedDict, total=False):
    task: str
    engine_sha: str
    accepted_verified: bool
    outcome: str
    usd_total: float | None


class _CostRecord(TypedDict, total=False):
    task: str
    engine_sha: str
    usd_total: float


class _Summary(TypedDict, total=False):
    verdict: Mapping[str, object] | None


class _Label(TypedDict, total=False):
    verdict: str
    file: str
    title: str


class _Metrics(TypedDict, total=False):
    labels: list[_Label]


class _Review(TypedDict, total=False):
    open: list[str] | None


class _Snapshot(TypedDict, total=False):
    metrics: _Metrics | None
    phase: Mapping[str, str] | None
    review: _Review | None


class _Issue(TypedDict, total=False):
    number: int
    node_id: str
    body: str | None
    state: str
    user: dict[str, str] | None
    labels: list[dict[str, str]]
    created_at: str
    closed_at: str | None


def drafts(
    *,
    records: Sequence[_Record] = (),
    summaries: Sequence[_Summary] = (),
    snapshots: Sequence[_Snapshot] = (),
    notes: str = "",
    inbox: Mapping[str, str] | None = None,
    hypotheses: Sequence[str] = (),
) -> list[Draft]:
    """Pure source-to-contract conversion. External titles and text occur only in quoted evidence."""
    result = []
    row: _Record | _CostRecord
    number: str | int
    for row in records:
        task, sha = str(row.get("task", "unknown")), str(row.get("engine_sha", "unknown"))
        identity = f"{_hash(sha)}:{_hash(task)}"
        evidence = repr(row)
        files = ("src/flyte_agent_factory/agent.py",)
        result.append(_draft(f"lesson:{identity}", "Apply benchmark lesson to the factory", evidence, files))
        if not row.get("accepted_verified", False) or row.get("outcome") in ("failed", "inconclusive"):
            result.append(
                _draft(f"regress:{identity}", "Improve an unsuccessful benchmark task", evidence, files)
            )
    # Per-task cost outliers compare repetitions of the same task, rather than unrelated task difficulty.
    by_task: dict[str | None, list[_CostRecord]] = {}
    for row in records:
        # The isinstance check establishes the numeric type for the direct key lookup.
        if isinstance(row.get("usd_total"), (int, float)) and cast(float, row["usd_total"]) > 0:
            # The guard establishes the numeric cost required for the outlier comparison.
            by_task.setdefault(row.get("task"), []).append(cast(_CostRecord, row))
    for rows in by_task.values():
        cheapest = min(row["usd_total"] for row in rows)
        for row in rows:
            if row["usd_total"] >= 2 * cheapest:
                key = f"cost:{_hash(str(row.get('engine_sha')))}:{_hash(str(row.get('task')))}"
                result.append(
                    _draft(
                        key,
                        "Reduce a benchmark task cost outlier",
                        repr(row),
                        ("src/flyte_agent_factory/agent.py",),
                    )
                )
    for summary in summaries:
        verdict = summary.get("verdict") or {}
        if verdict.get("verdict") in ("worse", "inconclusive"):
            key = "regress:" + _hash(json.dumps(summary, sort_keys=True))
            result.append(
                _draft(
                    key,
                    "Investigate a benchmark decision",
                    repr(summary),
                    ("src/flyte_agent_factory/agent.py",),
                )
            )
    recurring: Counter[str] = Counter()
    for snapshot in snapshots:
        for label in (snapshot.get("metrics") or {}).get("labels", []):
            if label.get("verdict") in ("FAIL", "BLOCK", "fail", "blocking"):
                evidence = repr(label)
                identity = _hash(f"{label.get('file', '')}:{label.get('title', '')}")
                result.append(
                    _draft(
                        f"production:{identity}",
                        "Address a production review finding",
                        evidence,
                        (label.get("file") or "src/flyte_agent_factory/agent.py",),
                    )
                )
        phase = snapshot.get("phase") or {}
        if phase.get("kind") != "done":
            # Scores are counts, not findings; use the accompanying open titles and require recurrence.
            recurring.update(set((snapshot.get("review") or {}).get("open") or []))
    for title, count in recurring.items():
        if count >= 2:
            result.append(
                _draft(
                    f"qa:{_hash(title)}",
                    "Resolve a recurring review finding",
                    title,
                    ("src/flyte_agent_factory/agent.py",),
                )
            )
    followups = False
    for line in notes.splitlines():
        if line.startswith("## "):
            followups = line.strip() == "## Follow-ups"
        elif line.strip() and ((followups and line.startswith("- ")) or line.startswith("- TODO(factory):")):
            result.append(
                _draft(
                    f"notes:{_hash(line)}", "Apply a factory follow-up", line, ("src/flyte_agent_factory/*",)
                )
            )
    for number, text in (inbox or {}).items():
        if re.fullmatch(r"[0-9]+", number):
            result.append(
                _draft(
                    f"inbox:{number}",
                    "Apply owner direction to the factory",
                    text,
                    ("src/flyte_agent_factory/*",),
                )
            )
    for number, text in enumerate(hypotheses, 1):
        result.append(
            _draft(
                f"hyp:{number}",
                "Test a factory improvement hypothesis",
                text,
                ("src/flyte_agent_factory/agent.py",),
            )
        )
    return list({row.key: row for row in result}.values())


def _labels(issue: _Issue) -> set[str]:
    return {label["name"].lower() for label in issue.get("labels", [])}


def _owner(files: tuple[str, ...]) -> bool:
    # Glob-to-glob overlap has no fnmatch primitive. A shared literal prefix is conservatively owner-only.
    return any(
        _loop._owner(path) or fnmatchcase(pattern, path) or pattern.startswith(re.split(r"[\*?\[]", path)[0])
        for path in files
        for pattern in _loop.OWNER_PATHS
    )


def publish(
    app: _github.GithubApp,
    repo: str,
    proposals: Sequence[Draft],
    *,
    now: datetime,
    board: _board.BoardClient | None = None,
    intake_label: str = "factory",
) -> list[_Issue]:
    """Create at most five marked issues per UTC day. Caller serializes publication with the queue lease."""
    today = now.astimezone(timezone.utc).date().isoformat()
    login = app.app_login().lower()
    # GithubApp exposes unparameterized dictionaries; this is the issue shape used by its API.
    existing = cast(list[_Issue], app.repo_issues(repo))
    remaining = 5 - sum(
        (issue.get("user") or {}).get("login", "").lower() == login
        and "<!-- loop-source: " in (issue.get("body") or "")
        and issue.get("created_at", "").startswith(today)
        for issue in existing
    )
    items = board.items() if board else []
    placed = {item.get("node_id") for item in items if item.get("status")}
    ready = (
        sum(
            item.get("status") == "Ready"
            and "loop" in {str(label).lower() for label in item.get("labels", [])}
            for item in items
        )
        if board
        else sum(
            issue.get("state") == "open" and {"loop", intake_label.lower()} <= _labels(issue)
            for issue in existing
        )
    )
    created = []
    labels: set[str] | list[str]
    for proposal in proposals:
        errors = _loop.lint_contract(proposal.body)
        if errors:
            raise ValueError("invalid loop contract: " + "; ".join(errors))
        marker = f"<!-- loop-source: {proposal.key} -->"
        # Search and listing return the same GitHub issue shape.
        matches = cast(list[_Issue], app.search_issues(repo, marker)) if remaining > 0 else []
        # The listing closes search's indexing delay on retries. Keep closed and rejected issues too.
        matches += [issue for issue in existing if marker in (issue.get("body") or "")]
        blocked = False
        for issue in sorted(matches, key=lambda issue: issue["number"]):
            labels = _labels(issue)
            closed = issue.get("closed_at")
            recent = bool(
                closed and datetime.fromisoformat(closed.replace("Z", "+00:00")) >= now - timedelta(days=30)
            )
            if (
                issue.get("state") == "open"
                or recent
                # A rejected or reverted change stays rejected; the generator's own needs-owner label is no
                # owner decision to reopen it ("skipped without an owner label", design-loop L4).
                or labels & {"loop:rejected", "loop:reverted"}
            ):
                if (
                    board
                    and issue.get("state") == "open"
                    and issue.get("node_id") not in placed
                    and (issue.get("user") or {}).get("login", "").lower() == login
                ):
                    # A create may have succeeded just before a board outage. Retries finish that write.
                    item = board.add_item(issue["node_id"])
                    promote = not _owner(proposal.files) and intake_label.lower() in labels and ready < 3
                    _board.advance_status(board, item, "Ready" if promote else "Backlog")
                    ready += promote
                    placed.add(issue["node_id"])
                blocked = True
                break
        if blocked or remaining <= 0:
            continue
        owner = _owner(proposal.files)
        promote = not owner and ready < 3
        labels = ["loop"] + (["needs-owner"] if owner else []) + ([intake_label] if promote else [])
        # Creation returns the same issue representation as search and listing.
        issue = cast(_Issue, app.create_issue(repo, proposal.title, proposal.body, labels))
        remaining -= 1
        created.append(issue)
        existing.append(issue)
        if board:
            item = board.add_item(issue["node_id"])
            _board.advance_status(board, item, "Ready" if promote else "Backlog")
            placed.add(issue["node_id"])
        ready += promote
    return created
