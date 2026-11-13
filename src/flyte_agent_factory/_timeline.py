"""The run report: a bounded activity log, rewritten in place, and the metrics block beside it."""

from __future__ import annotations

import asyncio
import html
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import flyte
import flyte.report

from flyte_agent_factory import (
    _dashboard,
    _instructions,
    _issues,
    _meter,
    _names,
    _policy,
    _roles,
    _session_config,
)
from flyte_agent_factory._bridge import DEFAULT_ASK_TIMEOUT_S, Bridge, _write_atomic, redact_tokens
from flyte_agent_factory._delegation import ActiveJob, Delegations, EndedJob
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._grants import GrantStore
from flyte_agent_factory._process import ClaudeProcess

#: Rows that mean something went wrong. ERROR is the subset that ended, or
#: could have ended, the run; WARN is a thing that degraded.
_ERROR_ICONS = frozenset({"🚨", "💥", "🛑"})

_PROBLEM_ICONS = _ERROR_ICONS | {"⚠️", "❌"}

#: Timer rows that say nothing about the work; the dashboard keeps them in a second, collapsed block.
_ROUTINE_ICONS = frozenset({"💾", "🔑"})

#: How long one report update may take before it is abandoned (the next change sends it again).
REPORT_FLUSH_TIMEOUT_S = 30.0

#: Status lines that mean the session is not moving by itself.
_STUCK_STATUS = re.compile(r"idle for|parked|keeps coming back|needs a human|stuck", re.IGNORECASE)


def _is_routine(row: tuple[float, str, str]) -> bool:
    _, icon, text = row
    return icon in _ROUTINE_ICONS or text == "child actions visible again"


#: Phase changes kept in the metrics block: the first (the run's start, which stage times count from) and the
#: newest. A run makes hundreds; the bound only stops a pathological one from pushing the block off the page.
PHASE_HISTORY_MAX = 500


def _bounded_history(history: list[list[Any]]) -> list[list[Any]]:
    if len(history) <= PHASE_HISTORY_MAX:
        return history
    return [history[0], *history[-(PHASE_HISTORY_MAX - 1) :]]


def _log_line(text: str, *, err: bool = False) -> None:
    print(text, file=sys.stderr if err else sys.stdout, flush=True)


#: A meter reading inside a router row, e.g. `claude 5h 97% weekly 40%` or the `retry at 01:50 UTC` tail.
_ROUTER_READING = re.compile(
    r"(?:claude|codex) (?:meter unavailable|not configured|at its limit"
    r"|(?:5h|weekly) \d+%(?: weekly \d+%)?)(?:, )?"
    r"|; retry at \d\d:\d\d UTC"
)


def _fallback_key(text: str) -> str:
    """Return the row without the meter readings that change between attempts.

    They are the same routing problem.
    """
    if text.startswith("router: "):
        return re.sub(r"\(\s*;?\s*", "(", _ROUTER_READING.sub("", text))[:500]
    return re.sub(r" \(.*\); using ", "; using ", text)[:500]


#: The core of the metrics block has to fit the page whatever the run did: newest first to go.
_METRICS_ROUNDS = 30

_METRICS_ASKS = 50

_METRICS_CHANGED_FILES = 100


def _bounded_round(row: dict[str, Any]) -> dict[str, Any]:
    """Return a round with at most `_METRICS_CHANGED_FILES` paths, and how many there were."""
    files = list(row.get("changed_files") or [])
    return {
        **row,
        "changed_files": files[:_METRICS_CHANGED_FILES],
        "changed_files_count": row.get("changed_files_count", len(files)),
    }


def _brief_goal() -> str:
    """Return what the project is for: the brief's first heading, else its first line; "" before it exists."""
    try:
        text = (_session_config.WORKSPACE / _session_config._BRIEF_FILE).read_text(errors="replace")
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line = next((line for line in lines if line.startswith("#")), lines[0] if lines else "")
    return _dashboard._short(re.sub(r"[*_`]", "", line.lstrip("#")).strip(), 160)


class _Timeline:
    """The run report: a bounded, rewritten-in-place activity log.

    Rewritten rather than appended because the report is the only place the
    claude.ai session URL surfaces, and it needs to stay pinned at the top
    where a human (or an agent reading the report) will actually find it.

    The report cannot be read from the CLI, so everything that lands in it is
    also written to the task log as a `[timeline]` line. Debugging the login
    took a day partly because the rows that explained it existed only in a
    report nobody could `flyte get logs`. The log is the durable copy; the
    report is the pretty one.
    """

    def __init__(self, *, keep: int = 300) -> None:
        self.issues: _issues.Milestones | None = None
        self.started_at = time.time()
        self.internal_subagents = 0
        self.internal_qa_subagents = 0
        self.internal_by_role: dict[str, int] = {}
        self._child_usage_seen: set[str] = set()
        self._fallback_counts: dict[str, int] = {}
        self._fallback_total = 0
        self._rows: list[tuple[float, str, str]] = []
        self._keep = keep
        #: Every `flyte-note`, kept apart from the bounded rows so an early decision
        #: is still there for the reviewers after hundreds of other rows.
        self.notes: list[str] = []
        self._header: list[str] = []
        #: Account label -> (shown text, hidden machine-readable twin); rewritten on every reading.
        self._meters: dict[str, tuple[str, str]] = {}
        self._activity: str | None = None
        self._dirty = False
        # Shutdown joins the pump before using its review summary or supplying a fallback.
        self.session_end: str | None = None
        self.session_end_written = False
        # --- dashboard state; see `snapshot` -------------------------------------------
        self.run_url = ""
        self.pr_url = ""  # the project's pull request, once the supervisor has opened it
        self.pr_at: float | None = None  # when its "pull request" row was written
        #: Every QA verdict, `{"round", "verdict", "blocking", "at"}`: what the board's Done and the issue's
        #: review milestones are recomputed from (`_board.desired`, `_issues.derive`).
        self.verdicts: list[dict[str, Any]] = []
        self.review_passed = False
        self.review_round = 0
        self.review_started_at: float | None = None
        # The commit `_evaluate` snapshotted for the review in flight. A field rather than part of the
        # return value so that `_evaluate`/`_review_round` keep their shape for their many callers.
        self.reviewed_sha = ""
        self.attempt = 0
        self.repo = ""
        self.branch = ""
        self.commit = ""
        self.unpushed: int | None = None
        #: Set by `project` once the pieces exist: open questions, delegations, the session.
        self.bridge: Bridge | None = None
        self.delegations: Delegations | None = None
        #: The project's credential grants (`_grants.GrantStore`); the report shows metadata only.
        self.credentials: GrantStore | None = None
        self.proc: ClaudeProcess | None = None
        #: Tokens the author session has used (`_author_usage`); refreshed on a timer, not per render.
        self.author_usage: dict[str, Any] = {}
        #: The same tokens apart: the main conversation, and the in-pod helpers (`_author_usage_split`).
        #: Empty until a split is set; `author_usage` stays the total of both.
        self.coordinator_usage: dict[str, Any] = {}
        #: False when the author engine reports no usage (Codex), so zeros here mean "not read", not "free".
        self.coordinator_measured: bool = True
        self.helper_usage: dict[str, Any] = {}
        #: Projects waiting for a slot, from the launch queue (`_refresh_queue`): position, name, since.
        self.queue: list[dict[str, Any]] = []
        self.checks: dict[str, Any] | None = None
        self.converging: bool | None = None
        self.open_findings: list[str] = []
        self.plan_heading = ""
        self._labels: dict[str, str] = {}
        self._phase: dict[str, Any] = {
            "kind": "starting",
            "since": time.time(),
            "round": 0,
            "limited_until": None,
        }
        self._last_note_at = 0.0
        self._meter_data: dict[str, _meter.MeterData] = {}
        self._fallbacks: list[str] = []
        self._problems: list[str] = []
        self._warnings = 0
        self._errors = 0
        self._bounces = 0
        self.preflight_bounces = 0
        self.phase_history: list[list[Any]] = [["starting", self.started_at]]
        self.criteria_at: float | None = None
        self.metric_labels: list[dict[str, Any]] = []
        self.round_scores: dict[int, int] = {}
        #: When the review pass was decided (the instant the "done" phase starts); None until then.
        self.passed_at: float | None = None
        #: The commit the last review round judged: where the next round's changes start.
        self.last_round_head = ""
        #: The latest review round's judges: judge -> {outcome, reason, engine, attempts, carried}.
        self.judges: dict[str, dict[str, Any]] = {}
        #: Every time a judge returned no verdict (retries included), by judge and by engine.
        self.no_verdict: dict[str, dict[str, int]] = {"by_judge": {}, "by_engine": {}}
        #: judge -> its PASS/ADVISORY on a commit whose review round ended undecided (`_review._Carried`
        #: fields), so the re-run asks only the judges still missing, across a restart too.
        self.carried: dict[str, dict[str, Any]] = {}
        self._image_defects: list[str] = []
        self._metrics_saved = ""
        self._metrics_save_failed = False
        self._restore_metrics()

    def phase(
        self,
        kind: str,
        *,
        round_: int = 0,
        limited_until: float | None = None,
        paused: dict[str, Any] | None = None,
    ) -> None:
        """Where the project is, for the dashboard's status line; the clock restarts when it changes.

        `paused` is `_router.Paused.as_dict()` while the author waits on a limit: who, until when, and why
        not the other engines. `factory.status` returns it as is.
        """
        if (kind, round_) != (self._phase["kind"], self._phase["round"]):
            self._phase = {"kind": kind, "since": time.time(), "round": round_, "limited_until": None}
            self.phase_history = _bounded_history([*self.phase_history, [kind, self._phase["since"]]])
        if kind == "review":
            self.review_passed = False
        if kind == "review" and round_ > self.review_round:
            self.review_round = round_
            self.review_started_at = self._phase["since"]
        self._phase["limited_until"] = limited_until
        self._phase["paused"] = paused
        self._dirty = True

    def record_judge_outcome(self, judge: str, **fields: Any) -> None:
        """Record how `judge` ended this round (`outcome`, `reason`, `engine`, `attempts`, `carried`)."""
        self.judges[judge] = {**fields, "reason": str(fields.get("reason", ""))[:200]}
        self._dirty = True

    def count_no_verdict(self, judge: str, engine: str) -> None:
        """Count one judge call that returned no verdict, under the judge and the engine it ran on."""
        for key, name in (("by_judge", judge), ("by_engine", engine or "unknown")):
            self.no_verdict[key][name] = self.no_verdict[key].get(name, 0) + 1
        self._dirty = True

    def _metrics_path(self) -> Path:
        return _session_config.STATE_ROOT / "metrics" / "timeline.json"

    def _metrics_state(self) -> dict[str, Any]:
        """Return what the metrics block needs from this timeline to survive a restart."""
        return {
            "phase_history": self.phase_history,
            "criteria_at": self.criteria_at,
            "bounces": self._bounces,
            "preflight_bounces": self.preflight_bounces,
            "labels": self.metric_labels,
            "knobs": _policy.knobs_seen,
            "verdicts": self.verdicts,
            "round_scores": self.round_scores,
            "passed_at": self.passed_at,
            "last_round_head": self.last_round_head,
            "judges": self.judges,
            "no_verdict": self.no_verdict,
            "carried": self.carried,
        }

    def _save_metrics(self) -> bool:
        """Write the metrics state when it changed, under STATE_ROOT so it rides the checkpoint.

        Return False when it could not be written.
        """
        text = json.dumps(self._metrics_state(), sort_keys=True)
        if text == self._metrics_saved:
            return True
        try:
            self._metrics_path().parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self._metrics_path(), json.loads(text))
        except OSError as e:
            # Measurement only: a read-only state directory must not stop the project. Logged once.
            if not self._metrics_save_failed:
                _log_line(f"[timeline] could not save the metrics state: {type(e).__name__}: {e}", err=True)
            self._metrics_save_failed = True
            return False
        self._metrics_saved = text
        return True

    def _restore_metrics(self) -> None:
        """Take back the metrics state of the run before a restart; the restart is one more phase entry.

        `criteria_at` and `passed_at` keep their first values: they are when it happened, not when we
        last looked.
        """
        try:
            saved = json.loads(self._metrics_path().read_text())
            history = [[str(kind), float(at)] for kind, at in saved["phase_history"]]
            bounces, preflight = int(saved["bounces"]), int(saved["preflight_bounces"])
            criteria_at = None if saved["criteria_at"] is None else float(saved["criteria_at"])
            passed_at = None if saved["passed_at"] is None else float(saved["passed_at"])
            labels = [dict(row) for row in saved["labels"]]
            verdicts = [dict(row) for row in saved["verdicts"]]
            round_scores = {int(n): int(score) for n, score in saved["round_scores"].items()}
            knobs = {str(k): str(v) for k, v in saved["knobs"].items()}
            last_round_head = str(saved["last_round_head"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return  # none, or not ours: start from nothing, as a first run does
        self._bounces, self.preflight_bounces = bounces, preflight
        self.criteria_at, self.passed_at, self.last_round_head = criteria_at, passed_at, last_round_head
        self.metric_labels, self.verdicts, self.round_scores = labels, verdicts, round_scores
        _policy.knobs_seen.update(knobs)
        self.phase_history = _bounded_history([*history, ["restarting", self.started_at]])
        # Newer keys: a state file from before them is still good.
        try:
            self.judges = {str(j): dict(v) for j, v in saved["judges"].items()}
            self.no_verdict = {
                key: {str(k): int(n) for k, n in saved["no_verdict"][key].items()}
                for key in ("by_judge", "by_engine")
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        try:
            self.carried = {str(j): dict(v) for j, v in saved["carried"].items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            pass

    def metric_label(self, **label: Any) -> None:
        for field, limit in (("file", 300), ("title", 200), ("evidence_kind", 200)):
            if field in label:
                label[field] = str(label[field])[:limit]
        self.metric_labels.append(label)
        del self.metric_labels[:-300]
        self._dirty = True

    def header(self, label: str, value: str, *, link: bool = False) -> None:
        shown = (
            f'<a href="{html.escape(value, quote=True)}">{html.escape(value)}</a>'
            if link
            else html.escape(value)
        )
        self._header.append(f"<tr><td><b>{html.escape(label)}</b></td><td>{shown}</td></tr>")
        self._labels[label] = value
        self._dirty = True
        _log_line(f"[timeline] {label}: {value}")

    def meter(self, meter: _meter.Meter) -> None:
        """Show an account's latest usage meter under the header, replacing its previous reading."""
        self._meters[meter.account] = (_meter.format_line(meter), _meter.report_comment(meter))
        self._meter_data[meter.account] = meter.to_dict()
        # The session reads its headroom from its memory file; a reading is the cadence it changes on.
        _instructions.refresh_headroom(_meter.Meter.from_dict(d) for d in self._meter_data.values())
        self._dirty = True

    def activity(self, text: str) -> None:
        """Set the current activity, replacing whatever it said before.

        A row would append, so a project that idles and wakes repeatedly would
        bury its own history in status churn.
        """
        if text != self._activity:
            # Only changes: the pump re-renders the idle duration every pass,
            # and a log line per pass would bury the events.
            _log_line(f"[timeline] status: {text}")
        self._activity = text
        self._dirty = True

    def row(self, icon: str, text: str) -> None:
        text = redact_tokens(text)
        if self.issues:
            self.issues.timeline(icon, text)
        now = time.time()
        if icon == "🔀" and text.startswith("pull request ") and self.pr_at is None:
            self.pr_at = now
        self._rows.append((now, icon, text))
        if score := re.match(r"blocking findings, round (\d+): (\d+)", text):
            # PASS clears the contract scores, and bounded timeline rows can lose earlier rounds.
            self.round_scores[int(score[1])] = int(score[2])
        if icon == "📝":
            self.notes.append(text)
            del self.notes[: max(0, len(self.notes) - 1000)]
            self._last_note_at = now
        self._count_risk(icon, text)
        del self._rows[: max(0, len(self._rows) - self._keep)]
        self._dirty = True
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        line = f"[timeline] {stamp} {icon} {text}"
        if icon in _PROBLEM_ICONS:
            # stderr and a severity tag, so a failure is one grep away rather
            # than somewhere in a log that also holds the whole terminal.
            _log_line(f"{line[:2000]}  [{'ERROR' if icon in _ERROR_ICONS else 'WARN'}]", err=True)
        else:
            _log_line(line[:2000])

    def _count_risk(self, icon: str, text: str) -> None:
        if icon in _ERROR_ICONS:
            self._errors += 1
            self._problems = [*self._problems[-4:], text]
        elif icon in _PROBLEM_ICONS:
            self._warnings += 1
        if icon == "🚧" and text.startswith("DONE bounced"):
            self._bounces += 1
        if self._is_fallback(icon, text):
            self._fallbacks = [*self._fallbacks[-19:], text]
            self._fallback_total += 1
            reason = _fallback_key(text)
            # Re-inserted on every repeat, so the dict is ordered by most recent use: the report shows
            # the newest reasons, and the cap drops the one unused longest rather than one still recurring.
            self._fallback_counts[reason] = self._fallback_counts.pop(reason, 0) + 1
            if len(self._fallback_counts) > 20:
                del self._fallback_counts[next(iter(self._fallback_counts))]

    @staticmethod
    def _is_fallback(icon: str, text: str) -> bool:
        """Return a call that ended up on a different engine or model than it was first given.

        A router row (`router: ...`) is an ordinary decision or a pin unless it records a refusal
        retry (`<engine> refused`); counting every decision made the risk list shout about routine work.
        """
        if text.startswith("router: "):
            return " refused" in text and " -> deferred " not in text
        return "retrying on" in text or (icon == "🔀" and ("; using " in text or ": using " in text))

    def _questions(self) -> list[dict[str, Any]]:
        out = []
        for q in sorted(
            (self.bridge.questions if self.bridge else {}).values(), key=lambda q: q["opened_at"]
        ):
            url = (
                f"{self.run_url}/actions/{q['condition']}"
                if self.run_url and q["condition"]
                else self.run_url
            )
            out.append({**q, "url": url})
        return out

    def _delegations(self) -> tuple[list[ActiveJob], list[EndedJob]]:
        if self.delegations is None:
            return [], []
        return list(self.delegations.active.values()), list(self.delegations.recent)

    def _breakdown(self, model: str) -> tuple[dict[str, Any], float]:
        """Return the cost in four groups, each always present, and the total in dollars.

        `coordinator` is the main conversation, priced at list price like `cost.coordinator`; `helpers` are
        the in-pod Agent-tool helpers, priced the same way; `subagents` are the review roles and the other
        child-action sub-agents; `workers` are the `delegate` runs. A report from
        before the split has only the author total, which then stands for the coordinator.
        The coordinator group says whether it was `measured`; when it was not, or its model has no list
        price, its `usd` is None and the total leaves it out, which the dashboard then says.
        """
        fields = _session_config._USAGE_FIELDS
        coordinator = self.coordinator_usage or self.author_usage
        groups: dict[str, dict[str, Any]] = {
            "coordinator": {
                "tokens": {f: coordinator.get(f, 0.0) for f in fields},
                "usd": 0.0,
                "measured": self.coordinator_measured,
            },
            "helpers": {"tokens": {f: self.helper_usage.get(f, 0.0) for f in fields}, "usd": 0.0},
            "subagents": {"tokens": dict.fromkeys(fields, 0.0), "usd": 0.0},
            "workers": {"tokens": dict.fromkeys(fields, 0.0), "usd": 0.0},
        }
        if not self.coordinator_measured:
            groups["coordinator"]["usd"] = None
        elif coordinator:
            # None when the model has no list price: no figure, not a free coordinator.
            groups["coordinator"]["usd"] = _session_config._list_price_usd(coordinator, model)
        if self.helper_usage:
            groups["helpers"]["usd"] = _session_config._list_price_usd(self.helper_usage, model)
        for key, row in _session_config._usage.items():
            role = _roles.display_role(key.partition("@")[0])
            group = groups["workers" if role == "delegate" else "subagents"]
            group["usd"] += row.get("cost_usd", 0.0)
            for f in fields:
                group["tokens"][f] += row.get(f, 0.0)
        # The sum of what is priced; an unmeasured or unpriced coordinator is left out, not counted as $0.
        return groups, sum(g["usd"] for g in groups.values() if g["usd"] is not None)

    def set_author_usage(
        self, split: dict[str, dict[str, Any]], total: dict[str, Any], *, measured: bool = True
    ) -> None:
        """Record what `_author_usage_split` read, with its total (the figure history is compared on)."""
        self.author_usage = total
        self.coordinator_usage, self.helper_usage = split["coordinator"], split["helpers"]
        self.coordinator_measured = measured

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Everything the dashboard shows, as plain data: the page renders it, tools read it as JSON."""
        now = time.time() if now is None else now
        questions = self._questions()
        phase = dict(self._phase)
        waiting = [q for q in questions if not q["timed_out"]]
        if waiting and phase["kind"] not in ("done", "failed", "limited"):
            phase = {**phase, "kind": "waiting", "since": min(q["opened_at"] for q in waiting)}
        if phase["kind"] == "review":
            phase["max_rounds"] = _session_config._max_rounds()
        active, recent = self._delegations()
        # Session output or a delegate event; checkpoints and login refreshes are not activity.
        moments = [self._last_note_at]
        out_at = getattr(self.proc, "last_output", None)
        if isinstance(out_at, float):
            moments.append(now - (time.monotonic() - out_at))
        moments += [d["started_at"] for d in active] + [d["ended_at"] for d in recent]

        criteria_text, pinned = _session_config._acceptance_text()
        criteria = _dashboard.parse_criteria(criteria_text)
        if criteria and self.criteria_at is None:
            self.criteria_at = now
        results = _dashboard.criterion_results(self.notes)
        for c in criteria:
            c["status"] = results.get(c["id"], "")

        _session_config._load_usage()
        sub_tokens = {
            f: sum(row.get(f, 0.0) for row in _session_config._usage.values())
            for f in _session_config._USAGE_FIELDS
        }
        by_role: dict[str, float] = {}
        by_engine: dict[str, float] = {}
        for key, row in _session_config._usage.items():
            role = _roles.display_role(key.partition("@")[0])
            by_role[role] = by_role.get(role, 0.0) + row.get("cost_usd", 0.0)
            engine = "+".join(row.get("engines") or ["unknown"])
            by_engine[engine] = by_engine.get(engine, 0.0) + row.get("cost_usd", 0.0)
        cost: dict[str, Any] = {
            "meters": sorted(self._meter_data.values(), key=lambda m: m["account"]),
            "fallbacks": list(self._fallbacks),
            "fallback_counts": dict(self._fallback_counts),
        }
        if _session_config._usage:
            cost.update(
                subagents={"tokens": sub_tokens, "usd": sum(by_role.values())},
                by_role=by_role,
                by_engine=by_engine,
            )
        model = self._labels.get("model", "")
        cost["breakdown"], cost["total_usd"] = self._breakdown(model)
        if self.author_usage and self.coordinator_measured:
            estimate = _session_config._list_price_usd(self.author_usage, model)
            elapsed = now - self.started_at
            cost["coordinator"] = {
                "tokens": self.author_usage,
                "usd_estimate": estimate,
                "usd_per_hour": estimate * 3600 / elapsed if estimate is not None and elapsed > 0 else None,
            }

        notes_links = [
            {"label": _dashboard._short(note.split("http", 1)[0].strip(" :-") or "link", 40), "url": url}
            for note in self.notes[-100:]
            for url in _dashboard.links_in(note)
        ]
        pr = self.pr_url or next(
            (link["url"] for link in reversed(notes_links) if "/pull/" in link["url"]), ""
        )
        notes_links = [link for link in notes_links if link["url"] != pr]
        risks = list(self._image_defects)
        if self.attempt:
            risks.append(
                f"restarted {self.attempt} time(s) (spot preemption or a crash); resumed from a checkpoint"
            )
        if self._errors:
            risks.append(f"{self._errors} error row(s); latest: {_dashboard._short(self._problems[-1], 160)}")
        if self._bounces:
            risks.append(f"{self._bounces} DONE bounced before review")
        if self._fallbacks:
            risks.append(f"{self._fallback_total} fallback(s) to another engine or model")
        if self._warnings:
            risks.append(f"{self._warnings} warning row(s) in the timeline")
        if self._activity and _STUCK_STATUS.search(self._activity):
            risks.append(f"status: {_dashboard._short(self._activity, 160)}")

        pass_record = _session_config._load_pass_record()
        passed = pass_record if pass_record and pass_record["passed"] else {}
        rounds = {
            n: {"round": n, "verdict": "FAIL", "blocking": score, "changed_files": []}
            for n, score in enumerate(_session_config._round_scores, 1)
        }
        for n, score in self.round_scores.items():
            rounds[n] = {"round": n, "verdict": "FAIL", "blocking": score, "changed_files": []}
        for row in [*((pass_record or {}).get("verdicts") or []), *self.verdicts]:
            rounds[row["round"]] = {"changed_files": [], **row}
        if passed:
            rounds.setdefault(
                passed["round"],
                {
                    "round": passed["round"],
                    "verdict": "PASS",
                    "blocking": 0,
                    "at": passed["at"],
                    "changed_files": [],
                },
            )
        bridge = self.bridge
        asks = getattr(bridge, "ask_history", list(bridge.questions.values())) if bridge else []
        # A question the factory decided itself, or that was closed while queued, never reached the owner:
        # it is not an ask in the metrics.
        asks = [q for q in asks if not q.get("decided") and not q.get("never_opened")]
        usage = {key: dict(row) for key, row in _session_config._usage.items()}
        author_models = self.author_usage.get("modelUsage")
        if author_models is None and self.author_usage:
            author_models = {
                self._labels.get("model", "unknown"): {
                    target: self.author_usage.get(source, 0)
                    for source, target in zip(
                        _session_config._USAGE_FIELDS,
                        ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"),
                        strict=True,
                    )
                }
            }
        usage["author"] = {**self.author_usage, "modelUsage": author_models or {}}
        if self.coordinator_usage:
            usage["coordinator"] = dict(self.coordinator_usage)
            usage["helpers"] = dict(self.helper_usage)
        last_note = self.notes[-1] if self.notes else ""
        gate_results = (
            _session_config.WORKSPACE
            / ".claude-flyte"
            / _session_config._project_name
            / "standards-results.json"
        )
        try:
            standards_results = json.loads(gate_results.read_text())
        except (OSError, ValueError):
            standards_results = []
        if not isinstance(standards_results, list):
            standards_results = []
        # Gate diagnostics can include whole test logs and the page cap counts JSON-escaped bytes.
        standards_results = _dashboard.compact_standards_results(standards_results)
        self._save_metrics()
        return {
            "engine_sha": _names.getenv("AGENT_FACTORY_ENGINE_SHA", ""),
            "standards_results": standards_results,
            "standards": _session_config._standards_diagnostics(),
            "metrics": {
                "schema": 1,
                "phase_history": [list(row) for row in self.phase_history],
                "criteria_at": self.criteria_at,
                "passed_round": passed.get("round"),
                # When the pass was decided; the saved record's time is its last rewrite, after the post.
                "passed_at": (self.passed_at if self.passed_at is not None else passed.get("at"))
                if passed
                else None,
                "rounds": [_bounded_round(rounds[n]) for n in sorted(rounds)][-_METRICS_ROUNDS:],
                "bounces": {"done": self._bounces, "preflight": self.preflight_bounces},
                "asks": [
                    {
                        "name": q.get("condition") or q["id"],
                        "blocking": q["kind"] == "approve"
                        or q["applies_at"] - q["opened_at"] >= DEFAULT_ASK_TIMEOUT_S,
                        "opened_at": q["opened_at"],
                        "answered_at": q.get("answered_at"),
                        "closed_at": q.get("closed_at"),
                    }
                    for q in asks[-_METRICS_ASKS:]
                ],
                "usage": usage,
                "knobs": dict(_policy.knobs_seen),
                "labels": [dict(row) for row in self.metric_labels],
            },
            "schema": _dashboard.SCHEMA,
            "generated_at": int(now // 60 * 60),
            "project": self._labels.get("project", ""),
            "goal": _brief_goal(),
            "run": self._labels.get("run", ""),
            "run_url": self.run_url,
            "model": self._labels.get("model", ""),
            "phase": phase,
            "last_activity_at": max(moments) or None,
            "needs_you": questions,
            "decisions": _dashboard.compact_decisions(getattr(bridge, "decisions", [])),
            "progress": {
                "criteria": criteria,
                "pinned": pinned,
                "checks": self.checks,
                "checks_script": _session_config._checks_rel(
                    _session_config._project_name, _session_config._layout()
                )
                if (
                    _session_config.WORKSPACE
                    / _session_config._checks_rel(_session_config._project_name, _session_config._layout())
                ).is_file()
                else "",
            },
            "review": {
                "internal_runs": {
                    "subagents": self.internal_subagents,
                    "qa_subagents": self.internal_qa_subagents,
                    "by_role": dict(self.internal_by_role),
                    "delegations": self.delegations.total if self.delegations else 0,
                },
                "scores": list(_session_config._round_scores),
                "verdicts": list(self.verdicts),
                "passed": self.review_passed,
                "round": self.review_round,
                "started_at": self.review_started_at,
                # The checkpointed pass record survives score reset and timeline truncation.
                "passed_round": _session_config._load_pass(),
                "open": list(self.open_findings),
                "judges": {j: dict(v) for j, v in self.judges.items()},
                "no_verdict": {key: dict(counts) for key, counts in self.no_verdict.items()},
                "converging": self.converging,
                "rulings": len(_session_config._rulings),
            },
            "working": {
                "plan": self.plan_heading,
                "note": _dashboard._short(last_note.splitlines()[0], 200) if last_note else "",
                "delegations": active,
                "recent": recent,
            },
            "cost": cost,
            "credentials": self.credentials.report() if self.credentials else {},
            "routing": list(_session_config._routes),
            "handoffs": _session_config._author_handoff_records(),
            "queue": {"waiting": len(self.queue), "entries": list(self.queue[: _dashboard.QUEUE_SHOWN])},
            "deliverables": {
                "repo": self.repo,
                "branch": self.branch,
                "commit": self.commit,
                "unpushed": self.unpushed,
                "pr": pr,
                "pr_at": self.pr_at,
                "links": notes_links[-8:],
            },
            "risks": risks,
        }

    def render(self) -> str:
        now = time.time()
        try:
            return self._render_dashboard(now)
        except (
            ArithmeticError,
            LookupError,
            TypeError,
            ValueError,
            AttributeError,
        ) as e:  # a dashboard bug must not stop the timeline
            print(f"[report] dashboard failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            return self._render_rows()

    def _render_dashboard(self, now: float) -> str:
        meter_rows = "".join(
            f"<tr><td><b>meter</b></td><td>{html.escape(text)}{twin}</td></tr>"
            for _, (text, twin) in sorted(self._meters.items())
        )
        # Project, run and model are in the status block already.
        extra = [h for h in self._header if not re.match(r"<tr><td><b>(?:project|run|model)</b>", h)]
        header = f"<table>{''.join(extra)}</table>" if extra else ""
        routine = [r for r in self._rows if _is_routine(r)]
        return _dashboard.render(
            self.snapshot(now),
            header_html=header,
            meter_rows=meter_rows,
            activity=self._activity or "",
            rows=[r for r in self._rows if not _is_routine(r)],
            routine=routine,
            now=now,
        )

    def _render_rows(self) -> str:
        """Return the report before the dashboard.

        Header, status and rows. The fallback if the dashboard breaks.
        """
        meters = [
            f"<tr><td><b>meter</b></td><td>{html.escape(text)}{twin}</td></tr>"
            for text, twin in self._meters.values()
        ]
        head = f"<table>{''.join(self._header + meters)}</table>" if self._header or meters else ""
        if self._activity:
            head += f"<p><b>Status:</b> {html.escape(self._activity)}</p>"
        body = "".join(
            f"<tr><td><code>{time.strftime('%H:%M:%S', time.localtime(at))}</code></td>"
            f"<td>{icon}</td><td>{html.escape(text)}</td></tr>"
            for at, icon, text in reversed(self._rows)
        )
        return f"<h3>Flyte Agent Factory</h3>{head}<table>{body}</table>"

    async def flush(self, *, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        self._dirty = False
        try:
            # Bounded: every pass of the pump awaits this, so a hung report call stalled it.
            await asyncio.wait_for(
                flyte.report.replace.aio(self.render(), do_flush=True), timeout=REPORT_FLUSH_TIMEOUT_S
            )
        except REMOTE_ERRORS as e:  # a broken report must not end the project
            # Visible in the run logs, but never fatal: losing the report is an
            # inconvenience, losing a month-old project is not. Dirty again, so the next
            # flush resends it: the last change before a release may be the one that failed.
            self._dirty = True
            print(f"[report] flush failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
