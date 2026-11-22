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

import difflib
import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import NamedTuple

from . import _names, _roles

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
    # Re-runs a judge's findings in a fresh copy and drops the ones that do not reproduce. Mechanical work
    # (run the command, read the output), so a cheap model; it only ever removes findings, never adds any.
    # Override with FINDING_CHECK_MODEL / FINDING_CHECK_EFFORT.
    "finding_check": Setting("sonnet", "low"),
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
        # The judgement call (ask-human triage) is routed and tuned on its own.
        "judgement": "high",
    }.items()
}


def _key(role: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", role.upper())


def resolve_codex(
    role: str, *, claude_effort: str = "", warn: Callable[[str, str], object] | None = None
) -> Setting:
    key = _key(role)
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


#: The three judges' concerns; `_usage._QA_ROLES` is built from it.
JUDGE_CONCERNS = ("correctness", "completeness", "craft")

# Policy keys that are read by a literal name: `read("X")`, or `AGENT_FACTORY_X` through `_names.getenv`
# (`_names.coordinator_engine`; `_context_guard`, whose hook reads a given environment). `AUTHOR_ENGINE`
# is the deprecated alias of `COORDINATOR_ENGINE` (`_names.coordinator_engine`, `_profile._policy`); it is
# removed from a profile at load, but a launch from an older CLI or a pod from an older engine still sends it.
_LITERAL_KEYS = (
    "ADMISSION_NOTE",
    "ADMITTED_ENGINE",
    "AUTHOR_ENGINE",
    "COMPACT_BETWEEN_ROUNDS",
    "CONTEXT_GUARD",
    "CONTEXT_GUARD_BYTES",
    "COORDINATOR_ENGINE",
    "CRAFT_BLOCKING",
    "DELEGATE_MAX",
    "ENGINE_FALLBACK",
    "JUDGE_ENGINE",
    "PARK_RELEASE_MIN",
    "QA_DIFFERENT_ENGINE",
)
# `_usage._static_engine` reads `<PURPOSE>_ENGINE` for any purpose a call is routed for; only these are keys.
# The coordinator's is COORDINATOR_ENGINE, above.
# `_check_findings` re-runs a correctness or completeness judge's findings as the purpose `<concern>-check`.
CHECK_PURPOSES = ("correctness-check", "completeness-check")
_ENGINE_PURPOSES = (
    "delegate",
    "planner",
    "contract",
    "arbiter",
    "judgement",
    *JUDGE_CONCERNS,
    *CHECK_PURPOSES,
)
# `_subagent_run` asks `resolve_codex` by the sub-agent's role, whichever name the session used: the roles
# and their aliases (`_roles`), and `subagent` for a call made without one. They default as a judge does.
SUBAGENT_ROLES = (*_roles.SESSION_ROLES, *_roles.ROLE_ALIASES, "subagent")


def _build_policy_keys() -> frozenset[str]:
    keys = set(_LITERAL_KEYS)
    for role in DEFAULTS:
        keys |= {f"{role.upper()}_MODEL", f"{role.upper()}_EFFORT"}
    # A judge asks `resolve_codex` by its concern, never as `judge`: that entry only supplies the defaults,
    # so a `JUDGE_CODEX_*` key would load and do nothing.
    codex_roles = (
        *(r for r in CODEX_DEFAULTS if r != "judge"),
        *JUDGE_CONCERNS,
        *CHECK_PURPOSES,
        *SUBAGENT_ROLES,
    )
    for role in codex_roles:
        keys |= {f"{_key(role)}_CODEX_MODEL", f"{_key(role)}_CODEX_EFFORT"}
    keys |= {f"{_key(purpose)}_ENGINE" for purpose in _ENGINE_PURPOSES}
    return frozenset(keys)


#: Every policy key a profile or a launch may set (the variable is `AGENT_FACTORY_<KEY>`). Anything else is
#: refused by `_profile._policy`. `read()` stays open: tests and operators name roles of their own, but a
#: key the code reads must be listed here (tests/test_policy_keys.py).
POLICY_KEYS = _build_policy_keys()


#: Policy keys that name an engine: the coordinator's, and its old name.
ENGINE_KEYS = ("COORDINATOR_ENGINE", "AUTHOR_ENGINE")
#: Policy keys read as numbers, with the check their reader applies (the readers fall back to a default
#: on a bad value; a profile fails instead). `PARK_RELEASE_MIN`: 0 keeps a parked session for ever.
NUMERIC_POLICY: Mapping[str, tuple[type[int] | type[float], float, str]] = MappingProxyType(
    {
        "PARK_RELEASE_MIN": (float, 0, "a finite number of minutes, 0 or more"),
        "DELEGATE_MAX": (int, 1, "a whole number, 1 or more"),
    }
)


#: Keys that were documented but never read, with what does the job; the nearest spelling would mislead.
_RETIRED_KEYS = MappingProxyType(
    {
        f"JUDGE_CODEX_{part}": " (it was never read; set "
        + ", ".join(f"{concern.upper()}_CODEX_{part}" for concern in JUDGE_CONCERNS)
        + ")"
        for part in ("MODEL", "EFFORT")
    }
)


def suggest_nearest_key(key: str) -> str:
    """Return ` (did you mean X?)` for the known key closest to `key`, or "" when none is close."""
    if key in _RETIRED_KEYS:
        return _RETIRED_KEYS[key]
    # The judges share one Claude model and effort; the nearest spelling of `CRAFT_EFFORT` is someone else's.
    concern, _, part = key.partition("_")
    if concern.lower() in JUDGE_CONCERNS and part in ("MODEL", "EFFORT"):
        return f" (did you mean JUDGE_{part}?)"
    near = difflib.get_close_matches(key, POLICY_KEYS, n=5)
    # A typo keeps the shape: prefer as many words (`REVEIW_MODEL` is `JUDGE_MODEL`, not `DEV_CODEX_MODEL`).
    near = [k for k in near if k.count("_") == key.count("_")] or near
    return f" (did you mean {near[0]}?)" if near else ""


def is_claude_model(model: str) -> bool:
    """Return a Claude alias or id (`sonnet`, `opusplan`, `sonnet[1m]`, `claude-...`), never sent to Codex."""
    return model.lower().startswith(("claude", "sonnet", "opus", "haiku", "fable"))
