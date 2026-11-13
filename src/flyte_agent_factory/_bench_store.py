"""Benchmark measurements on the direction repository's orphan results branch.

Git supplies atomic publication and history. JSONL appends to the same file need a replay after a
rebase conflict; a textual merge cannot safely choose between two valid case records.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast, overload

from typing_extensions import NotRequired

if TYPE_CHECKING:
    from . import _bench
else:
    from flyte_agent_factory import _bench

log = logging.getLogger(__name__)

BRANCH = "bench-results"
REF = f"refs/heads/{BRANCH}"
REMOTE_REF = f"refs/remotes/origin/{BRANCH}"
MAX_ATTEMPTS = 12
#: Outcomes that are a harness fault, not a measurement: a rerun retries them and they are never reused.
RETRYABLE = frozenset({"harness_error", "skipped_login_busy"})
DAY_S = 86400


class _StoredCase(TypedDict):
    case_run: str
    bench_run: str
    started_at: float
    ended_at: float
    engine_sha: str
    policy: dict[str, str]
    suite_digest: str
    task: str
    repeat: int
    outcome: str
    model: NotRequired[str]
    engine_source: NotRequired[str]


class _AARun(TypedDict):
    suite_digest: str
    model: str
    logins: list[str]
    ended_at: float
    records: list[dict[str, object]]


class StoreError(RuntimeError):
    """The results could not be read or published. Messages never include clone credentials."""


def _is_fault_of(line: str, case_run: str) -> bool:
    try:
        row = json.loads(line)
    except ValueError:
        return False
    return isinstance(row, dict) and row.get("case_run") == case_run and row.get("outcome") in RETRYABLE


def _run_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("bench_run must be a run name without path separators")
    return name


class Store:
    """A temporary checkout of `bench-results`. Use as a context manager; one writer per instance.

    `url` is an App-authenticated HTTPS clone URL in production, a file URL in offline tests.
    Reads reflect the checkout opened here; writes fetch and retry concurrent publications.
    """

    def __init__(self, url: str):
        self._tmp = tempfile.TemporaryDirectory(prefix="bench-store-")
        self.tree = Path(self._tmp.name) / "tree"
        self.tree.mkdir()
        try:
            self._git("clone", "-q", "--no-checkout", url, ".")
            self._git("config", "user.name", "Factory benchmark")
            self._git("config", "user.email", "factory-bench@users.noreply.github.com")
            # Refuse an unknown or unborn default as well: there is no safe branch to assume.
            head = self._git("ls-remote", "--symref", "origin", "HEAD")
            if not head.startswith("ref: refs/heads/"):
                raise StoreError("cannot determine the direction repository default branch")
            if f"ref: {REF}\tHEAD" in head:
                raise StoreError("bench-results must never be the direction repository's default branch")
            if self._has_remote():
                # A branch that shares history with any other would carry the direction repo's files.
                others = [
                    ref
                    for ref in self._git(
                        "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"
                    ).split()
                    if ref not in (REMOTE_REF, "refs/remotes/origin/HEAD")
                ]
                if any(
                    self._git("merge-base", REMOTE_REF, ref, check=False).returncode == 0 for ref in others
                ):
                    raise StoreError(
                        "bench-results exists but is not an orphan branch; refusing to write to it"
                    )
                self._git("checkout", "-q", "-B", BRANCH, REMOTE_REF)
            else:
                # checkout --orphan would populate the index with the direction repo's private files.
                self._git("symbolic-ref", "HEAD", REF)
                self._git("read-tree", "--empty")
        except BaseException:
            self._tmp.cleanup()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()

    @overload
    def _git(self, *args: str, check: Literal[True] = True) -> str: ...

    @overload
    def _git(self, *args: str, check: Literal[False]) -> subprocess.CompletedProcess[str]: ...

    def _git(self, *args: str, check: bool = True) -> str | subprocess.CompletedProcess[str]:
        failed = False
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=self.tree,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            failed = True  # raised below, outside the handler: its context would carry the token URL
        if failed:
            raise StoreError(f"results store git {args[0]} could not complete")
        if not check:
            return done
        if done.returncode:
            # Git diagnostics and command arguments can contain the installation token.
            raise StoreError(f"results store git {args[0]} failed")
        return done.stdout.strip()

    def _has_remote(self) -> bool:
        return self._git("show-ref", "--verify", "--quiet", REMOTE_REF, check=False).returncode == 0

    def _push(self, ref: str) -> subprocess.CompletedProcess[str]:
        if ref != REF:
            raise StoreError(f"results store refuses pushes outside {REF}")
        return self._git("push", "--porcelain", "origin", f"HEAD:{ref}", check=False)

    def _write(self, change: Callable[[], bool], message: str) -> None:
        if not change():
            return
        self._git("add", "--all")
        self._git("commit", "-q", "-m", message)
        for attempt in range(MAX_ATTEMPTS):
            pushed = self._push(REF)
            if pushed.returncode == 0:
                return
            detail = pushed.stdout + pushed.stderr
            concurrent = (
                "[rejected]" in detail
                and any(reason in detail for reason in ("fetch first", "non-fast-forward"))
            ) or ("[remote rejected]" in detail and "cannot lock ref" in detail)
            if not concurrent:
                raise StoreError(
                    "results store push failed: the GitHub App requires contents: write on the direction "
                    "repository; also check branch protection and repository availability"
                )
            if attempt == MAX_ATTEMPTS - 1:
                raise StoreError("results store push failed after concurrent-write retries")
            # Jittered, capped exponential backoff: writers that collided must not retry in lockstep.
            time.sleep(random.uniform(0, min(5.0, 0.1 * 2**attempt)))
            self._git("fetch", "-q", "origin", f"{REF}:{REMOTE_REF}")
            rebased = self._git("rebase", REMOTE_REF, check=False)
            if rebased.returncode:
                self._git("rebase", "--abort")
            # Even a clean textual rebase can split a run across dated directories when its first
            # writers straddle UTC midnight. Replay the operation against the published layout.
            self._git("reset", "--hard", REMOTE_REF)
            if not change():
                return
            self._git("add", "--all")
            self._git("commit", "-q", "-m", message)

    def _directory(self, bench_run: str, at: float) -> Path:
        name = _run_name(bench_run)
        existing = [p for p in (self.tree / "runs").glob("*") if p.name[11:] == name]
        if len(existing) > 1:
            raise StoreError("results store has multiple directories for the same bench_run")
        if existing:
            return existing[0]
        date = datetime.fromtimestamp(at, timezone.utc).date().isoformat()
        return self.tree / "runs" / f"{date}-{name}"

    def records(self, bench_run: str | None = None) -> list[dict[str, object]]:
        """Case records in this checkout, optionally restricted to one benchmark run."""
        if bench_run is not None:
            _run_name(bench_run)
        records = []
        try:
            for path in sorted((self.tree / "runs").glob("*/cases.jsonl")):
                if bench_run is not None and path.parent.name[11:] != bench_run:
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    # Records are validated when written. Reading tolerates later schema changes and
                    # skips a damaged line, so one bad row never hides the rest of the history.
                    try:
                        row = json.loads(line)
                    except ValueError:
                        log.warning(
                            "results store: skipping unreadable line %d of %s", number, path.parent.name
                        )
                        continue
                    if isinstance(row, dict) and isinstance(row.get("case_run"), str):
                        records.append(row)
        except OSError as exc:
            raise StoreError("results store contains unreadable case records") from exc
        return records

    def completed(self, bench_run: str) -> set[str]:
        """Return the deterministic case run names already recorded for `bench_run`."""
        # records() only returns rows whose case_run was checked as a string.
        return {
            cast(str, row["case_run"])
            for row in self.records(bench_run)
            if row.get("outcome") not in RETRYABLE
        }

    def append_case(self, record: dict[str, object]) -> None:
        """Validate and durably append a case, once per deterministic case run name."""
        _bench.validate(record)
        # Validation establishes the fields the store indexes; other record fields are preserved.
        case = cast(_StoredCase, record)
        _run_name(case["bench_run"])
        line = json.dumps(record, allow_nan=False) + "\n"

        def append() -> bool:
            if case["case_run"] in self.completed(case["bench_run"]):
                return False
            path = self._directory(case["bench_run"], case["started_at"]) / "cases.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # A harness fault recorded earlier for this case is replaced by the retry's record.
            kept = [
                old
                for old in (path.read_text().splitlines() if path.exists() else [])
                if not _is_fault_of(old, case["case_run"])
            ]
            path.write_text("".join(f"{old}\n" for old in kept) + line)
            return True

        self._write(append, f"Record benchmark case {case['case_run']}")

    def production_runs(self) -> set[str]:
        """Production runs already published in this checkout."""
        return {path.stem for path in (self.tree / "labels" / "production").glob("*.jsonl")}

    def write_production_labels(self, run: str, rows: list[dict[str, object]]) -> None:
        """Publish one immutable file per production run, replayed safely on a push race."""
        name = _run_name(run)
        if not rows or len(rows) > 300:
            raise ValueError("production labels require between 1 and 300 rows")
        for row in rows:
            _bench.validate_label(row)
            if row["run"] != name:
                raise ValueError("production labels must belong to the named run")
        text = "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows)

        def write() -> bool:
            path = self.tree / "labels" / "production" / f"{name}.jsonl"
            if path.exists():
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            return True

        self._write(write, f"Record production labels {name}")

    def _write_json(self, locate: Callable[[], Path], value: dict[str, object], message: str) -> None:
        text = json.dumps(value, allow_nan=False, indent=2) + "\n"

        def write() -> bool:
            path = locate()
            if path.exists() and path.read_text() == text:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            return True

        self._write(write, message)

    def write_summary(self, bench_run: str, summary: dict[str, object]) -> None:
        _run_name(bench_run)
        self._write_json(
            lambda: self._directory(bench_run, time.time()) / "summary.json",
            summary,
            f"Summarize benchmark {bench_run}",
        )

    def baseline_records(
        self,
        sha: str,
        policy: Mapping[str, str],
        suite_digest: str,
        task: str,
        repeat: int,
        max_age_days: float,
        model: str = "opus",
        engine_source: str = "",
    ) -> dict[str, object] | None:
        """Newest matching baseline younger than `max_age_days`, measured at ended_at (UTC epoch seconds).

        `engine_source` is the case's `Source.key`, "" for the root of `[repo] factory`: the same SHA in
        another repo and subtree is a different engine.
        """
        now = time.time()
        # Published cases have the schema validated on append, even though records() tolerates new fields.
        matches = [
            row
            for row in cast(list[_StoredCase], self.records())
            if (row["engine_sha"], row["policy"], row["suite_digest"], row["task"], row["repeat"])
            == (sha, policy, suite_digest, task, repeat)
            and row.get("model", "opus") == model
            and row.get("engine_source", "") == engine_source
            and row.get("outcome") not in RETRYABLE
            and 0 <= now - row["ended_at"] < max_age_days * DAY_S
        ]
        # Keep the permissive read interface so callers retain fields beyond this store's indexed subset.
        return cast(dict[str, object] | None, max(matches, key=lambda row: row["ended_at"], default=None))

    def write_aa(
        self,
        bench_run: str,
        records: list[dict[str, object]],
        *,
        sigma_aa: float | None,
        suite_digest: str,
        model: str,
        logins: list[str],
    ) -> None:
        """Publish calibration records and their matching metadata as one A/A run."""
        name = _run_name(bench_run)
        if not records:
            raise ValueError("an A/A run needs case records")
        for row in records:
            _bench.validate(row)
            if row["bench_run"] != name:
                raise ValueError("A/A case records must belong to the named run")
        # Every record passed the full case validator above.
        ended_at = max(row["ended_at"] for row in cast(Sequence[_StoredCase], records))
        date = datetime.fromtimestamp(ended_at, timezone.utc).date().isoformat()
        self._write_json(
            lambda: self.tree / "aa" / f"{date}-{name}.json",
            {
                "records": records,
                "sigma_aa": sigma_aa,
                "suite_digest": suite_digest,
                "model": model,
                "logins": logins,
                "ended_at": ended_at,
            },
            f"Record A/A calibration {name}",
        )

    def aa_history(
        self,
        suite_digest: str,
        model: str,
        logins: list[str],
        max_age_days: float = 28,
        max_merges: int | None = None,
    ) -> list[list[dict[str, object]]]:
        """Return matching calibration case lists, oldest first.

        Optionally keep only the newest `max_merges`.
        """
        now = time.time()
        matches: list[_AARun] = []
        try:
            for path in (self.tree / "aa").glob("*.json"):
                # write_aa writes this metadata shape; malformed fields fail in the guarded reads below.
                run = cast(_AARun, json.loads(path.read_text()))
                if (run["suite_digest"], run["model"], sorted(run["logins"])) != (
                    suite_digest,
                    model,
                    sorted(logins),
                ) or not 0 <= now - run["ended_at"] < max_age_days * DAY_S:
                    continue
                for row in run["records"]:
                    _bench.validate(row)
                matches.append(run)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StoreError("results store contains unreadable or invalid A/A records") from exc
        matches.sort(key=lambda run: run["ended_at"])
        if max_merges is not None:
            if max_merges < 0:
                raise ValueError("max_merges must be non-negative")
            matches = matches[-max_merges:] if max_merges else []
        return [run["records"] for run in matches]
