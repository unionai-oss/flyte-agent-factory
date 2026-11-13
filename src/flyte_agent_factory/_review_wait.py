"""A review that will re-run by itself: deferred by the usage limit, or incomplete (#151, #169, #87).

Not a round either way. The wait is saved with the pass record, so a pod restart resumes it, and the
status shows the real next try while it lasts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from flyte_agent_factory import _inbox, _reviewer, _session_config, _supervise_support, _timeline, _usage

#: What a review waits on when the usage limit deferred it; the only wait the status shows as `limited`.
LIMIT = "the usage limit"
INCOMPLETE = "an incomplete review"


def format_utc_hhmm(at: float) -> str:
    """Return a wall time as `HH:MM`, UTC, the way the router and the limit notices state it."""
    return time.strftime("%H:%M", time.gmtime(at))


@dataclass
class ReviewWait:
    """When the review re-runs (`at`, wall time), what it waits on, and the incomplete re-runs since DONE."""

    at: float | None = None
    why: str = ""
    incomplete_reruns: int = 0

    @classmethod
    def restore_from(cls, record: dict[str, Any] | None, workspace: object) -> ReviewWait:
        """Return the wait a restart found in the pass record: it goes on, not lost with the memory."""
        wait = cls()
        if record and not record["passed"]:
            wait.incomplete_reruns = record["incomplete_reruns"]  # a restart grants no more silent re-runs
            if record["review_retry_at"] and workspace:
                wait.at, wait.why = record["review_retry_at"], record["review_retry_why"] or LIMIT
        return wait

    def save(self, round_: int, timeline: _timeline._Timeline) -> None:
        """Save the wait with the pass record, so a restart resumes it."""
        _inbox._unpass(round_, timeline, self.at, self.why, self.incomplete_reruns)

    def wait_for_limit(self, e: _reviewer.ReviewLimited, round_: int, timeline: _timeline._Timeline) -> None:
        """Wait for the reviewers' usage limit to lift. The session is not told: it is usually on it too."""
        now = time.time()
        # The router's own retry time first (it is what deferred the call, or its text says it), then the
        # reset the account printed, then a guess. At least 10 minutes: a reset already past (a late reset,
        # or a stale notice time) must not re-run three refused reviewers on every poll (qa: 84/s).
        # Past is not unknown: only a notice with no time waits LIMIT_RETRY_S (#194).
        said = _usage._reset_at(e.reset_text)
        lifts = (
            e.retry_at
            or _supervise_support._parse_router_retry_at(e.reset_text, now)
            or (now + _session_config.LIMIT_RETRY_S if said is None else said)
        )
        self.at = max(
            lifts + _supervise_support._REVIEW_RETRY_SLACK_S, now + _supervise_support._REVIEW_RETRY_MIN_S
        )
        self.why = LIMIT
        timeline.row("⏸️", f"review waiting: {e}; it re-runs automatically after the reset")
        timeline.phase("limited", limited_until=self.at)
        self.save(round_, timeline)

    def schedule_incomplete_rerun(
        self, e: _reviewer.ReviewUnavailable, round_: int, timeline: _timeline._Timeline
    ) -> bool:
        """Schedule a re-run of the missing judges; False once the re-runs are spent (a human's problem).

        Some judge gave no verdict and none failed. The other judges' verdicts are kept, so asking again
        is cheap. The session is not told, as for the limit.
        """
        if not isinstance(e, _reviewer.ReviewIncomplete) or (
            self.incomplete_reruns >= _supervise_support._REVIEW_INCOMPLETE_MAX_RERUNS
        ):
            return False
        self.incomplete_reruns += 1
        self.at, self.why = time.time() + _supervise_support._REVIEW_INCOMPLETE_RETRY_S, INCOMPLETE
        timeline.row(
            "🔁",
            f"review incomplete ({e}); re-running the missing judge(s) automatically at "
            f"{format_utc_hhmm(self.at)} UTC",
        )
        # In review, not limited: no limit is involved. The activity line names the time.
        timeline.phase("review", round_=round_ + 1)
        self.save(round_, timeline)
        return True

    def explain_unreviewed(self, e: _reviewer.ReviewUnavailable) -> str:
        """Return why the work went unreviewed: the spent re-runs of an incomplete review, else `e` as is."""
        if isinstance(e, _reviewer.ReviewIncomplete):
            return f"review incomplete after {self.incomplete_reruns} automatic re-runs: {e}"
        return str(e)

    def show(self, timeline: _timeline._Timeline) -> None:
        """Keep the status on the review's real next try while it waits on the limit.

        Whatever else set the phase meanwhile (an author limit clearing sets done/building): never a past
        time, never hidden.
        """
        if self.why == LIMIT and self.at is not None and self.at > time.time():
            if (timeline._phase["kind"], timeline._phase["limited_until"]) != ("limited", self.at):
                timeline.phase("limited", limited_until=self.at)

    def describe_activity(self) -> str:
        """Return the activity line while the review waits: on what, and when it re-runs (UTC)."""
        return f"review waiting on {self.why} -- re-runs automatically at {format_utc_hhmm(self.at or 0)} UTC"
