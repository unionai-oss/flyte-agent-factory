"""The operator's watch loop: never exits, never misses an open question.

A past run sat on an open `ask-human` for 40+ minutes because the
operator only looked when something else prompted it -- a one-shot, bounded
`wait_event` that was not re-armed after it timed out. A watcher that can be
forgotten is a watcher that will be. So:

  * `operate.py launch` starts this loop detached whenever it is not already
    running (see `ensure_running`), so a run cannot be launched unwatched.
  * It scans every owned live run each `--every` seconds, forever.
  * An open `ask-*` condition is reported on first sight AND repeated every
    scan with its age, so a missed line costs one interval, not forty minutes.
    Each line is `[operator] BLOCKED ...` and goes to stdout; attach a Monitor
    to the log to be woken by it.
  * It exits only when no owned run is live and has been for `--idle-exit` scans.
  * A launch replaces a watcher running older code (`ensure_running`), but only
    after the new one has started and initialised; until then the old one stays.

Pure decision logic lives in `scan()` so it can be tested without a cluster.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from flyte import remote

from flyte_agent_factory import _profile
from flyte_agent_factory._errors import REMOTE_ERRORS

HERE = Path(__file__).resolve().parent
LOG = HERE / ".watch.log"
PIDFILE = HERE / ".watch.pid"
STATE = HERE / ".watch.state.json"
#: Touched by every `ensure_running`. An idle watcher about to exit stays if it was
#: kicked recently: a past launch found the old watcher alive, so started
#: none, and that watcher then exited on its idle count, leaving the run unwatched.
KICK = HERE / ".watch.kick"
#: Written by a watcher once it has initialised: "<pid> <code fingerprint>".
READY = HERE / ".watch.ready"
#: Held for the whole of `ensure_running`: two launches at once each spawned a
#: successor, and one killed the other's (qa on watcher-reload).
LOCK = HERE / ".watch.lock"
#: The files whose change means a running watcher is out of date.
CODE = (HERE / "watch.py", HERE / "operate.py")
#: How long a launch waits for a replacement watcher to come up before keeping the old one.
REPLACE_WAIT_S = 60.0
TERMINAL = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT")
#: Per run: ((phase, report, open questions), last activity, last silence alert).
Activity = dict[str, tuple[tuple[str, str | None, frozenset[str]], float, float | None]]


def report_activity(report: str) -> str:
    """Return a hash of a report's rows that changes only when the session made progress."""
    # Header/status counters change on every supervisor pass without session progress, and
    # so do the supervisor's own timer rows: a checkpoint every ~15 minutes kept an
    # idle run "active" forever.
    rows = [
        row
        for row in re.findall(r"<tr><td><code>.*?</tr>", report, flags=re.DOTALL)
        if not re.search(r"</code></td><td>(?:💾|🔑|·)</td>", row)
    ]
    return hashlib.sha256(re.sub(r"\d", "", "".join(rows)).encode()).hexdigest()


def load_activity() -> Activity:
    """Return the activity saved by the previous scan, or an empty dict if it is missing or unreadable."""
    try:
        saved = json.loads(STATE.read_text())
        return {
            run: ((phase, report, frozenset(questions)), active, alert)
            for run, ((phase, report, questions), active, alert) in saved.items()
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_activity(state: Activity) -> None:
    """Save `state` for the next scan, replacing the file atomically."""
    saved = {
        run: ((phase, report, sorted(questions)), active, alert)
        for run, ((phase, report, questions), active, alert) in state.items()
    }
    temporary = STATE.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(saved))
    temporary.replace(STATE)


def scan(
    runs: dict[str, str],
    conditions: dict[str, list[tuple[str, str]]],
    first_seen: dict[tuple[str, str], float],
    now: float,
    *,
    activity: dict[str, str] | None = None,
    activity_seen: Activity | None = None,
    silent_after: float = 3600,
) -> list[str]:
    """Lines to emit for one pass.

    runs: run name -> phase. conditions: run name -> [(condition name, phase)].
    first_seen is mutated: when each open ask was first observed.
    activity contains report hashes; missing reports retain the previous signal.
    activity_seen tracks each signal, last activity, and last silence alert.
    """
    lines: list[str] = []
    for run, phase in runs.items():
        for name, cphase in conditions.get(run, []):
            if name.startswith("direct-"):
                continue  # the standing inbox, not a question
            key = (run, name)
            if "PAUSED" in cphase:
                first = first_seen.setdefault(key, now)
                lines.append(f"[operator] BLOCKED {run} is waiting on YOU: {name} open {int(now - first)}s")
            else:
                first_seen.pop(key, None)
        if phase in TERMINAL:
            lines.append(f"[operator] ENDED {run}: {phase}")
    if activity_seen is not None:
        for run in list(activity_seen):
            if runs.get(run) in TERMINAL:
                del activity_seen[run]
        for run, phase in runs.items():
            if phase in TERMINAL:
                continue
            previous = activity_seen.get(run)
            report = (activity or {}).get(run, previous[0][1] if previous else None)
            questions = frozenset(
                name
                for name, cphase in conditions.get(run, [])
                if not name.startswith("direct-") and "PAUSED" in cphase
            )
            signal = (phase, report, questions)
            if previous is None or signal != previous[0]:
                activity_seen[run] = (signal, now, None)
                continue
            _, last_active, last_alert = previous
            if (
                not questions
                and now - last_active >= silent_after
                and (last_alert is None or now - last_alert >= 3600)
            ):
                lines.append(f"[operator] SILENT {run} no activity for {int((now - last_active) / 60)}m")
                activity_seen[run] = (signal, last_active, now)
    return lines


def _is_watcher(pid: int) -> bool:
    """Whether `pid` is a live watcher, not merely a live pid.

    A past launch trusted a pidfile left by a watcher I had just killed: the pid was
    still alive for a moment (or reused), no watcher was started, and the run had none.
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    state = stat.rsplit(")", 1)[-1].split()[0] if ")" in stat else ""
    return b"watch.py" in cmdline and state not in ("Z", "X")


def code_fingerprint(paths: tuple[Path, ...] = CODE) -> str:
    """Content hash of the watcher's own code.

    A deploy that adds a slot must restart the watcher: one started before it
    keeps the old `operate` imported and watches only the old slots, so a run on
    the new slot goes unwatched.
    """
    import hashlib

    h = hashlib.sha256()
    for path in paths:
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"missing")
    return h.hexdigest()


def _ready() -> tuple[int, str]:
    try:
        pid, fp = READY.read_text().split()
        return int(pid), fp
    except (OSError, ValueError):
        return 0, ""


def _spawn() -> int:
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve())],
        stdout=LOG.open("a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    return proc.pid


def ensure_running(wait_s: float | None = None) -> int:
    """Serialise `_ensure_running`; see there."""
    import fcntl

    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ensure_running(wait_s)


def _ensure_running(wait_s: float | None) -> int:
    """Make sure a watcher on the current code is alive. Returns its pid.

    Only a launch calls this, so the watcher is upgraded at a deliberate moment to
    the same code that is launching -- never by a branch switch in the workspace.
    A watcher on older code is replaced only once its successor is alive and has
    initialised; if the successor dies or never gets there, the old one is kept.
    """
    KICK.touch()
    want = code_fingerprint()
    try:
        old = int(PIDFILE.read_text())
    except (OSError, ValueError):
        old = 0
    if old and not _is_watcher(old):
        old = 0
    if old and _ready() == (old, want):
        return old
    new = _spawn()
    deadline = time.time() + (REPLACE_WAIT_S if wait_s is None else wait_s)
    try:
        while time.time() < deadline and _ready() != (new, want) and _is_watcher(new):
            time.sleep(0.5)
    except BaseException:
        # A launch interrupted mid-wait must not leave its successor as a second watcher.
        with contextlib.suppress(ProcessLookupError):
            os.kill(new, signal.SIGTERM)
        raise
    if _ready() != (new, want):
        # Still not initialised: kill it so it cannot become a second watcher.
        if _is_watcher(new):
            with contextlib.suppress(ProcessLookupError):
                os.kill(new, signal.SIGTERM)
        new = 0
    if not new:
        print(f"[operator] WARN a new watcher did not start; see {LOG}", file=sys.stderr, flush=True)
        if old:
            PIDFILE.write_text(str(old))
            return old
        raise RuntimeError(f"no watcher could be started; see {LOG}")
    if old and old != new:
        with contextlib.suppress(ProcessLookupError):
            os.kill(old, signal.SIGTERM)
    PIDFILE.write_text(str(new))
    return new


def vanished(announced: set[str], live: dict[str, str]) -> list[str]:
    """Return runs we were watching that are no longer live, i.e. have ended.

    `operated_live()` lists live runs only, so a run that finishes simply drops
    out and `scan` never sees its terminal phase (a run once ended silently).
    """
    return sorted(announced - set(live))


def should_exit(quiet: int, idle_exit: int, kicked_at: float, now: float, window: float) -> bool:
    """Idle long enough, and nobody launched anything within the last `window` seconds."""
    return quiet >= idle_exit and now - kicked_at > window


def main() -> None:
    """Scan the live runs every `--every` seconds and alert on questions, ends and silence."""
    p = argparse.ArgumentParser()
    p.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    p.add_argument("--every", type=int, default=30)
    p.add_argument(
        "--idle-exit", type=int, default=20, help="exit after this many scans with no owned live run"
    )
    p.add_argument("--silent-after", type=int, default=3600, help="seconds without activity before alerting")
    args = p.parse_args()
    _profile.select_from_argv()

    import operate  # type: ignore[import-not-found]  # sibling script, on sys.path only when run as a script

    operate._init()
    # The pidfile is written only by `ensure_running`, under its lock: a watcher that
    # wrote its own could overwrite the live one's and then delete it on idle exit.
    first_seen: dict[tuple[str, str], float] = {}
    # Timeline rows carry only time-of-day, so persist the clock across replacements.
    activity_seen = load_activity()
    quiet = 0
    print(f"[operator] watching owned runs of {', '.join(operate.SLOTS)} every {args.every}s", flush=True)
    READY.write_text(f"{os.getpid()} {code_fingerprint()}")
    announced: set[str] = set(activity_seen)
    window = 2.0 * args.every

    def kicked_at() -> float:
        try:
            return KICK.stat().st_mtime
        except OSError:
            return 0.0

    while not should_exit(quiet, args.idle_exit, kicked_at(), time.time(), window):
        try:
            owned = operate.operated_live()
            # Never idle out while a run we announced has not been seen to end.
            if not owned and not announced:
                quiet += 1
            else:
                quiet = 0
            runs = {r.name: operate._phase(r) for r in owned}
            conds = {n: [(c.name, c.phase) for c in remote.Condition.listall(run_name=n)] for n in runs}
            for n in runs:
                if n not in announced:
                    announced.add(n)
                    print(f"[operator] now watching {n}", flush=True)
            activity = {}
            for run in owned:
                # A missing report must not hide open questions or count as renewed activity.
                with contextlib.suppress(*REMOTE_ERRORS):
                    # The report includes the session timeline; phase alone misses a stalled session.
                    activity[run.name] = report_activity(run.get_report())
            for line in scan(
                runs,
                conds,
                first_seen,
                time.time(),
                activity=activity,
                activity_seen=activity_seen,
                silent_after=args.silent_after,
            ):
                print(line, flush=True)
            for n in vanished(announced, runs):
                try:
                    phase = operate._phase(remote.Run.get(n))
                except REMOTE_ERRORS as e:  # still say it ended
                    phase = f"unknown ({type(e).__name__})"
                if phase in operate.LIVE:
                    continue  # a flaky listing, not an ending; keep watching it
                print(f"[operator] ENDED {n}: {phase}", flush=True)
                announced.discard(n)
                activity_seen.pop(n, None)
            save_activity(activity_seen)
        except Exception as e:  # noqa: BLE001 -- a watcher that dies on one bad poll is the bug being fixed
            traceback.print_exc()
            print(
                f"[operator] WARN poll failed: {type(e).__name__}: {str(e)[:200]}",
                file=sys.stderr,
                flush=True,
            )
        time.sleep(args.every)
    with contextlib.suppress(OSError, ValueError):
        if int(PIDFILE.read_text()) == os.getpid():
            PIDFILE.unlink()
    print("[operator] no owned runs live; exiting", flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
