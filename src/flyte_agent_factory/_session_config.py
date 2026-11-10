"""Paths, timings and per-run state that more than one part of the session needs.

They live below `agent`, `_timeline` and `_instructions` so that none of them has to import another to read
them. Callers look the names up on this module at call time (`_session_config.NAME`), which is what lets a
test patch one with `monkeypatch.setattr(_session_config, "NAME", ...)`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _git,
    _meter,
    _names,
    _profile,
    _secret_env,
    _standards,
    _state,
)
from flyte_agent_factory._bridge import INBOX_SUBDIR

# Tasks here run as an unprivileged user (`flyte`), not root, so nothing may
# assume it can create a directory at the filesystem root. Every writable path
# hangs off HOME, which is also where Flyte extracts the code bundle. These are
# resolved at import time, which happens inside the pod -- so they must never
# be baked into the environment's `env_vars` at definition time, where HOME is
# whatever laptop ran `flyte deploy`.
BASE_DIR = Path(_names.getenv("AGENT_FACTORY_BASE") or os.environ.get("HOME") or "/tmp")

# The project's Volume mounts here. Fixed rather than discovered, so these
# paths are known before the mount happens and every helper below can be
# written against them.
STATE_ROOT = BASE_DIR / "state"

WORKSPACE = STATE_ROOT / _state.WORKSPACE_SUBDIR

# Claude Code keeps transcripts here. On the volume alongside the workspace, so
# one commit captures a consistent pair -- the work and the conversation that
# produced it -- and `--continue` resumes after a restart.
CONFIG_DIR = STATE_ROOT / _state.CLAUDE_SUBDIR

# A writable directory on PATH for the `ask-human` / `flyte-note` wrappers;
# /usr/local/bin belongs to root.
BIN_DIR = BASE_DIR / ".local" / "bin"

# Outside STATE_ROOT on purpose: that is checkpointed whole, and a 10 MB debugging capture
# would be uploaded on every timer tick.
RAW_TERMINAL_LOG = BASE_DIR / "logs" / "terminal.log"

_INBOX_DIR = INBOX_SUBDIR  # defined in _bridge, which also drops late answers here

#: Inside the harness's own directory, not the repo root: in a shared repo an earlier
#: run had committed a root `BRIEF.md`, so every later run's brief showed up as a
#: modification, and a project committed it by accident.
_BRIEF_FILE = ".claude-flyte/BRIEF.md"

#: The deployment profile, if one is selected (see `_profile`); a pod gets its text from the env.
_PROFILE = _profile.current()

_PROJECT_API_KEY_VAR = _secret_env.PROJECT_API_KEY_VAR

DESIGN_FILE = "DESIGN.md"


def _passed_path() -> Path:
    return STATE_ROOT / "review" / "passed.json"


def _load_pass_record() -> dict[str, Any] | None:
    """Return what `_save_pass` wrote, or None for a missing or corrupt file.

    A file from before `verdict_posted` existed is treated as posted: its verdict went out in the
    old order's time, and re-posting a comment on every old pass would be noise.
    """
    try:
        saved = json.loads(_passed_path().read_text())
        return {
            "passed": bool(saved.get("passed", True)),
            "in_review": bool(saved.get("in_review", False)),
            "round": int(saved["round"]),
            "sha": str(saved.get("sha") or ""),
            "at": saved.get("at"),
            "pr_url": str(saved.get("pr_url") or ""),
            "verdict_posted": bool(saved.get("verdict_posted", True)),
            "verdicts": list(saved.get("verdicts") or []),
            "pr_at": saved.get("pr_at"),
            "review_round": int(saved.get("review_round", saved["round"])),
            "review_started_at": saved.get("review_started_at"),
        }
    except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return None


def _load_pass() -> int | None:
    """Return the round that passed before a restart; a missing or corrupt file is no pass."""
    record = _load_pass_record()
    return record["round"] if record and record["passed"] else None


# A session that has gone quiet for this long has almost certainly finished the
# turn it was on, rather than being mid-thought.
IDLE_AFTER_S = 180.0

# How often the supervisor looks at the session between notes.
POLL_EVERY_S = 15.0

# How long a failing child listing may go on before the last good count stops counting for the
# nudge, park and kickoff gates. Below `NUDGE_AFTER_S`, so a failing listing cannot hold off
# more than one nudge interval; release stays blocked through `_in_flight_error` regardless.
CHILDREN_STALE_S = 600.0

# A child-action listing runs in the background and is abandoned after this long; the pump
# waits at most `CHILDREN_LIST_WAIT_S` for it on the pass that starts it, and otherwise picks
# the result up on a later pass, using the last good count meanwhile.
CHILDREN_LIST_TIMEOUT_S = 30.0

CHILDREN_LIST_WAIT_S = 2.0

# How often the pump says it is alive in the task log. Events are sparse on a
# healthy long run, and a log with no events looks identical to a dead pump.
HEARTBEAT_EVERY_S = 300.0

#: How long terminal output alone may hold off nudges after the session last said
#: anything: a hung tool call or an endless retry loop keeps the spinner going too.
BUSY_CEILING_S = 2700.0

#: After a usage-limit notice, how often to try resuming the session. Cheap: while
#: still limited the attempt just shows the notice again.
LIMIT_RETRY_S = 1800.0

#: Output after a post-reset submission, with no new notice, that counts as running again.
LIMIT_RESUMED_CHARS = 4096

# Fractions of disk and memory in use that are worth saying out loud. Set below
# the point where the kubelet starts evicting, so the warning lands in the
# report *before* the pod dies rather than never.
_DISK_WARN_AT = 0.85

_MEMORY_WARN_AT = 0.90

# How long a session must be quiet before it is prodded. Much
# longer than `IDLE_AFTER_S`, because the two answer different questions. Three
# minutes of silence is worth *showing* in the report, but it is a normal amount
# to spend waiting on an image build or a queued child action -- a session
# observed doing exactly that would have been nudged mid-wait. Fifteen minutes
# of nothing is a session that has stopped, not one that is blocked.
NUDGE_AFTER_S = 900.0

#: A project that has passed review and been left alone this long gives its slot back (see
#: `_pump`). The review-passed message's `ask-human` expires after the same time, so the usual
#: release comes 30-60 minutes after the pass.
RELEASE_IDLE_S = 1800

# A confirmation menu that is still (or again) open after we declined it: Esc may not
# have closed it, or the session retried the same command. Decline it again after this long.
PROMPT_REDECLINE_S = 300.0

# A streak is the same prompt -- or a near-identical retry of it, see `_same_prompt` -- declined
# again within this window of the previous decline. The session retrying a declined command is
# the usual case: one sat for nine hours retrying `rm -rf s3v_cwd/*`, and every decline went to
# the owner as "keeps coming back" while what it needed was to be told to stop retrying.
PROMPT_DECLINE_WINDOW_S = 1800.0

# From this decline of a streak on, the follow-up tells the session to change approach.
PROMPT_REPEAT_AFTER = 3

# Change-approach nudges before a human is told. Even then the declines and nudges go on: a
# menu left open blocks the session for good, which is worse than one more decline.
PROMPT_REPEAT_NUDGES = 3

PROMPT_DECLINE_CAP = PROMPT_REPEAT_AFTER + PROMPT_REPEAT_NUDGES

# Declines of any prompts within the window before the session is told it keeps hitting them,
# and a human once: alternating between two prompts never forms a streak.
PROMPT_ANY_CAP = 6

# A follow-up waits for the menu to close -- typing into an open one could approve it, since
# Enter picks the highlighted "1. Yes" -- for at most this long, then is dropped.
PROMPT_FOLLOWUP_HOLD_S = 120.0

# Decline a menu open this long even while the terminal keeps writing: a background agent's
# output must not hold a blocked main session open forever (main's 45-minute busy ceiling
# would at least have nudged).
PROMPT_BUSY_DECLINE_S = 300.0

# How many *consecutive* unanswered nudges a run will send before
# giving up on prodding. Bounded because a session that ignores three in a row
# is stuck on something a nudge cannot fix, and an unbounded prodder would bury
# the real problem under its own output.
#
# Consecutive, not lifetime: a project that runs for days would otherwise spend
# its whole allowance in the first hour and have no protection for the rest. A
# note arriving is the agent reporting of its own accord, which is exactly what
# a nudge asks for, so it resets the count.
MAX_NUDGES = 3

_CHECKS_SCRIPT = ".claude-flyte/checks.sh"

#: This project's name, set once by `project`. Briefs name the project's directory
#: after it, so `<name>/ACCEPTANCE.md` is unambiguously this project's criteria.
_project_name = ""

#: The repository `project` works on, set with the name. `_export_runtime_env` runs before the
#: clone and installs the one deploy key the pod keeps, so it must already know which repo.
_project_repo = ""

_project_brief = ""


def _safe_project_dir(name: str) -> bool:
    return bool(name) and not Path(name).is_absolute() and ".." not in Path(name).parts and name != "."


def _layout() -> str:
    """`workspace`, `sandbox` or `root` for the repo this project runs on; see `_git.project_layout`.

    A name that cannot be a directory (`_safe_project_dir`) is always `sandbox`: its harness files
    fall back to the repo-root script and criteria, and a root layout would let `_checks_rel` and
    `_gate_base.checks_rel` disagree about where `checks.sh` is.
    """
    if not _safe_project_dir(_project_name):
        return "sandbox"
    layout: str = _git.project_layout(
        _project_repo, _PROFILE.repo if _PROFILE else "", _gate_base.is_workspace(WORKSPACE)
    )
    return layout


def _checks_rel(name: str, layout: str = "sandbox") -> str:
    # Keep the script with the project's tracked files so reviewers see the same checks.
    return _gate_base.checks_rel(name, layout) if _safe_project_dir(name) else _CHECKS_SCRIPT


def _acceptance_rel(name: str) -> str:
    """Keep session criteria in the checkpointed, ignored harness directory."""
    return f".claude-flyte/{name}/ACCEPTANCE.md" if _safe_project_dir(name) else ".claude-flyte/ACCEPTANCE.md"


def _acceptance_path(workspace: Path) -> Path:
    """Migrate this project's legacy criteria once, without borrowing another project's."""
    target = workspace / _acceptance_rel(_project_name)
    legacy = (
        workspace / _project_name / _ACCEPTANCE_FILE
        if _safe_project_dir(_project_name)
        else workspace / _ACCEPTANCE_FILE
    )
    # A root-layout project never had a `<name>/` directory, and the root ACCEPTANCE.md is the repo's own.
    # (An unsafe name is always the sandbox layout, so it keeps migrating the root file as before.)
    if _layout() == "sandbox" and not target.exists() and legacy.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(legacy.read_text())
    return target


#: The hard ceiling on review rounds. It was 3 ("converge in two or three rounds or
#: not at all"), but hard briefs (taskgraph x5, logstats) were still shrinking their
#: findings at round 3 and needed a human only to say "keep going". Now each failed
#: round gets a planner, effort rises with the round, and the planner's convergence
#: call -- not a count -- decides when a human is needed; this is the backstop.
_QA_MAX_ROUNDS = 8

#: Before this round, a failed review always goes back to the author. From it on, the
#: planner's convergence call decides: converging continues, circling escalates.
_CONVERGENCE_FROM_ROUND = 3

#: Extra rounds granted by a human or the operator after an escalation, via the
#: `/review-again` direction, capped at `_MAX_GRANTED_ROUNDS`. Three escalations
#: in a row (in past runs) ended "round-3 findings fixed, not
#: re-reviewed": small known fixes, shipped with nobody checking them.
_granted_rounds = 0

_MAX_GRANTED_ROUNDS = 2


def _max_rounds() -> int:
    return _QA_MAX_ROUNDS + _granted_rounds


#: Where the session writes what "done" means, before it starts building.
_ACCEPTANCE_FILE = "ACCEPTANCE.md"

#: (role, round) -> summed usage. Persisted to STATE_ROOT/review/usage.json.
_usage: dict[str, dict[str, Any]] = {}

_usage_loaded = False

_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _usage_path() -> Path:
    return STATE_ROOT / "review" / "usage.json"


def _load_usage() -> None:
    """Read the persisted ledger once per process, so a restart adds to it instead of replacing it."""
    global _usage_loaded
    if _usage_loaded:
        return
    _usage_loaded = True
    try:
        saved = json.loads(_usage_path().read_text())
    except (OSError, ValueError):
        return
    for key, row in saved.items():
        if key not in _usage and isinstance(row, dict):
            _usage[key] = {k: float(v) for k, v in row.items() if isinstance(v, (int, float))}
            if isinstance(row.get("modelUsage"), dict):
                _usage[key]["modelUsage"] = row["modelUsage"]
            for field in ("models", "efforts", "engines", "call_ids"):
                if isinstance(row.get(field), list):
                    _usage[key][field] = [v for v in row[field] if isinstance(v, str)]


#: List prices in $ per million tokens: (input, output, cache write, cache read). The interactive
#: session reports no dollars, so the dashboard shows the author's spend as an estimate at these
#: rates; a model not listed (or a price change) shows tokens only rather than a wrong figure.
_LIST_PRICE = {
    "opus": (5.0, 25.0, 6.25, 0.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
}


def _list_price_usd(tokens: dict[str, float], model: str) -> float | None:
    rates = next((r for family, r in _LIST_PRICE.items() if family in model.lower()), None)
    if rates is None:
        return None
    fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(tokens.get(f, 0.0) * rate for f, rate in zip(fields, rates, strict=False)) / 1e6


def _acceptance_text() -> tuple[str, bool]:
    """Return the criteria the dashboard counts, and whether they are the pinned copy.

    The pinned contract once review has started (the session cannot move it); before that the
    file the session is writing.
    """
    pinned = BASE_DIR / ".acceptance-pinned"
    try:
        if pinned.is_file():
            return pinned.read_text(errors="replace"), True
        live = _acceptance_path(WORKSPACE)
        if live.is_file():
            return live.read_text(errors="replace")[:20_000], False
    except OSError:
        pass
    return "", False


#: Router decisions of this process, newest last. Machine-readable twin of the `🔀 router:` rows: the
#: dashboard snapshot (`routing`) carries it to the run report and the benchmark.
_routes: list[dict[str, Any]] = []

#: Binding rulings on design conflicts, shown to every judge from then on.
_rulings: list[str] = []

#: Total blocking findings per failed round, in order: the convergence measure.
_round_scores: list[int] = []

#: The Codex author's home. Inside STATE_ROOT, which the checkpoint saves whole, so the threads Codex
#: stores there come back with the thread id kept beside them.
CODEX_HOME_DIR = STATE_ROOT / "codex"


def _author_handoff_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((STATE_ROOT / "author-handoffs").glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


#: Where a demo run must leave its shareable package, relative to the workspace.
DEMO_SUBDIR = "demo"

#: Brand rules, in the repo. Derived once and then followed, so successive
#: demos look like each other rather than each inventing a look.
_BRAND_FILE = "BRAND.md"

#: Inside the package: the pieces the final video was composed from. Kept so a
#: later instruction -- change the music, reword slide 3, redo one line of
#: narration -- re-renders from these rather than re-recording the demo.
DEMO_ASSETS_SUBDIR = "assets"

#: The composition itself, so a human (or another agent) can see what is where
#: without reverse-engineering it from an ffmpeg command.
DEMO_MANIFEST = "manifest.json"

#: What the final, shareable video must be. MP4/H.264 rather than the WebM the
#: recorder produces: WebM was never a decision, just Chromium's output format,
#: and it does not import into Keynote or PowerPoint, QuickTime will not play
#: it, and mail clients often fail to preview it. The whole point of the package
#: is that it can be sent to someone.
DELIVERABLE_SUFFIX = ".mp4"

#: The console's host, for scoping its auth header; "" when the deployment has no console.
_CONSOLE_HOST = _PROFILE.backend.console_host if _PROFILE else ""

_SIGNIN_HOST = _PROFILE.backend.signin_host if _PROFILE else ""


#: The Codex access token (`[secrets] codex_token`), mounted into project and sub-agent environments when
#: the deploy sets AGENT_FACTORY_CODEX_TOKEN_SECRET to its name. A second engine with its own usage meter.
#: It never refreshes, so nothing rotates and nothing is written back; see `_login_with_access_token`.
CODEX_TOKEN_SECRET = _names.getenv("AGENT_FACTORY_CODEX_TOKEN_SECRET", "").strip()


def _codex_deployed() -> bool:
    return bool(CODEX_TOKEN_SECRET)


#: Meter account of the setup-token, taken while the token is in the environment.
_token_meter_account = ""
#: Name for a mounted setup-token whose deployment did not say which secret it came from.
_TOKEN_SECRET_UNNAMED = "setup-token"


def _claude_account() -> str:
    """Return the meter account of the setup-token the Claude sessions run on.

    The name comes from the deployment's secret. The fingerprint of the token's value is part of it, so a
    token swapped under the same secret name is a new account that inherits no refusal.
    """
    global _token_meter_account
    secret = _names.getenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "")
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if token.strip():
        _token_meter_account = _meter.token_account(secret or _TOKEN_SECRET_UNNAMED, token)
    return _token_meter_account or f"claude {secret or _TOKEN_SECRET_UNNAMED}"


def _captured_standards() -> dict[str, str]:
    """Return the workspace's `standards/` when it has one, else the profile's, else none."""
    config = _PROFILE.standards if _PROFILE else _standards.Standards()
    workspace_standards = WORKSPACE / _gate_scope.STANDARDS_DIR
    if workspace_standards.is_dir():
        files = _standards.capture(dataclasses.replace(config, dir=str(workspace_standards)), WORKSPACE)
    elif _PROFILE:
        files = _PROFILE.captured_standards()
    else:
        files = _standards.capture(config, Path("."))
    return files


def _standards_diagnostics() -> dict[str, bool | str | None]:
    # Snapshots precede the first gate and must not materialize files just to describe their selection.
    config = _PROFILE.standards if _PROFILE else _standards.Standards()
    try:
        files = _standards.select(_captured_standards(), config, _project_dir(), demo=_demo_kind)
    except OSError as exc:
        return {
            "configured": None,
            "warning": f"standards unavailable: {type(exc).__name__}; check standards paths and permissions",
        }
    except ValueError as exc:
        # Standards validation messages describe the fault; decoder errors can contain payload bytes.
        reason = str(exc) if str(exc).startswith("[standards] ") else "invalid standards bundle or text"
        return {"configured": None, "warning": f"standards unavailable: {reason}"[:512]}
    return {"configured": True} if files else {"configured": False, "warning": _standards.NO_STANDARDS}


def _applicable_standards() -> dict[str, str]:
    config = _PROFILE.standards if _PROFILE else _standards.Standards()
    files = _standards.materialize(WORKSPACE, _project_dir(), config, _captured_standards(), demo=_demo_kind)
    if not files:
        print(
            f"{_standards.NO_STANDARDS}: the effective standards selection is empty, "
            "so this project has no rules (docs/standards.md)",
            file=sys.stderr,
        )
    return files


_demo_kind = ""


def _project_dir() -> Path:
    """Return what the project owns in its workspace.

    `<name>/` in the sandbox repo, `projects/<name>/` in a workspace, the repo root otherwise.
    """
    project: Path = _gate_base.project_dir(WORKSPACE, _project_name, _layout())
    return project


CODEX_TOKEN_ENV_VAR = _secret_env.CODEX_TOKEN_ENV_VAR


def _codex_account() -> str:
    """Return the meter account of the Codex credential pods use: the access token's secret."""
    return f"codex {CODEX_TOKEN_SECRET}"
