"""Which model, at what effort, does each job in the factory get.

Review gates everything: a false pass ships bad work and a false fail burns a
round, so judges are the last place to economise. The author is the cheap
default. Every choice is here rather than in call sites, so a drift test can
see them all and an operator can override one without a code change:

    AGENT_FACTORY_<ROLE>_MODEL=opus  AGENT_FACTORY_<ROLE>_EFFORT=high

Effort is passed to `claude --effort` and is one of EFFORTS; empty leaves the
CLI default.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

from . import _names

# Environment reads are centralized here because copying the launch policy would claim
# unused knobs were honored, including knobs an older engine does not implement.
knobs_seen: dict[str, str] = {}


def read(name: str, default: str = "") -> str:
    value = _names.getenv(f"AGENT_FACTORY_{name}", default)
    knobs_seen[name] = value
    return value


EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The remote launcher promises this default; the operator CLI can retain the slot's author setting.
LAUNCH_MODEL = "opus"


class Setting(NamedTuple):
    model: str
    effort: str


# role -> default. Keys are the only roles callers may ask for.
DEFAULTS: dict[str, Setting] = {
    "author": Setting("sonnet", ""),
    "judge": Setting("opus", "high"),
    # Turns a failed round's findings into the author's next plan, and judges whether
    # the loop is converging. The highest-leverage call per round, so the strongest model.
    "planner": Setting("fable", "medium"),
}

# Resources rise with the round: a round that failed is evidence the problem is
# harder than the last one assumed. From FINAL_ROUND judges get xhigh, from
# MAX_EFFORT_ROUND max. The name FINAL_ROUND predates rounds past 3; it is where
# escalated effort starts.
FINAL_ROUND = 3
_FINAL_ROUND_EFFORT = "xhigh"
MAX_EFFORT_ROUND = 5
#: The arbiter settles conflicts between rounds: one call, top effort.
ARBITER_EFFORT = "xhigh"
#: planner effort by round: medium first, then high, then xhigh from round 4.
_PLANNER_EFFORT = {1: "medium", 2: "high", 3: "high"}


def resolve(role: str, round_: int = 1) -> Setting:
    """Return the model and effort for `role`, after env overrides.

    Raises KeyError for an unknown role (a typo must not silently run on a default)
    and ValueError for an effort the CLI would reject.
    """
    base = DEFAULTS[role]
    key = role.upper()
    model = read(f"{key}_MODEL").strip() or base.model
    override = read(f"{key}_EFFORT").strip()
    effort = override or base.effort
    # Judges no longer escalate by round: rising reviewer effort widened the search each
    # round, so findings kept coming, narrower and narrower, and convergence could not be
    # measured (a past run, 8 rounds). They grade a frozen contract at constant effort.
    if role == "planner" and not override:
        effort = _PLANNER_EFFORT.get(round_, "xhigh")
    if effort and effort not in EFFORTS:
        raise ValueError(f"effort {effort!r} for {role!r} is not one of {EFFORTS}")
    return Setting(model, effort)


#: Where a model falls back to when a call on it fails. Fable has its own usage
#: meter, separate from Opus and Sonnet, so a Fable limit must not cost the call.
FALLBACK: dict[str, str] = {"fable": "opus"}


def fallback(setting: Setting) -> Setting | None:
    """Return the setting to retry a failed call with, or None. Same effort, a different meter."""
    model = FALLBACK.get(setting.model)
    return Setting(model, setting.effort) if model else None


def argv(setting: Setting) -> list[str]:
    """Return the `claude` flags for a setting."""
    out = ["--model", setting.model]
    if setting.effort:
        out += ["--effort", setting.effort]
    return out


CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
# Review and planning need careful reasoning; mechanical implementation benefits
# from medium effort. Leave models unpinned so Codex owns its supported default.
CODEX_DEFAULTS = {
    role: Setting("", effort)
    for role, effort in {
        "author": "medium",
        "delegate": "medium",
        "judge": "high",
        "arbiter": "high",
        "contract": "high",
        "planner": "high",
    }.items()
}


def resolve_codex(
    role: str, *, claude_effort: str = "", warn: Callable[[str, str], object] | None = None
) -> Setting:
    key = re.sub(r"[^A-Z0-9]", "_", role.upper())
    base = CODEX_DEFAULTS.get(role, CODEX_DEFAULTS["judge"])
    prefix = f"{key}_CODEX"
    model = read(f"{prefix}_MODEL").strip()
    effort = read(f"{prefix}_EFFORT").strip()
    default_effort = {"max": "xhigh"}.get(claude_effort, claude_effort) or base.effort
    if model and not re.fullmatch(r"[A-Za-z0-9][\w.:/\[\]-]*", model):
        if warn:
            warn("⚠️", f"AGENT_FACTORY_{prefix}_MODEL misconfigured: {model!r}; using Codex default")
        model = base.model
    if effort and effort not in CODEX_EFFORTS:
        if warn:
            warn("⚠️", f"AGENT_FACTORY_{prefix}_EFFORT misconfigured: {effort!r}; using {default_effort}")
        effort = ""
    return Setting(model, effort or default_effort)


def is_claude_model(model: str) -> bool:
    """Return a Claude alias or id (`sonnet`, `opusplan`, `sonnet[1m]`, `claude-...`), never sent to Codex."""
    return model.lower().startswith(("claude", "sonnet", "opus", "haiku", "fable"))
