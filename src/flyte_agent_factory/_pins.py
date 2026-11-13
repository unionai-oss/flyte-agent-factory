"""Which engine each builder role is pinned to, and whether the pin is hard.

A hard pin waits for its engine's limit to reset; a soft one moves to the other engine. A pin is hard
when it is set and `ENGINE_FALLBACK` is not "allow". A direction pin (the operator told a paused
project which engine to resume on) and the benchmark's `hard_pin` are always hard.

`ADMITTED_ENGINE` and the built-in role defaults are not pins: they stay the router's `preferred`.
Judge and reviewer pins are read elsewhere and are unchanged.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path

from flyte_agent_factory import _names, _policy, _profile, _session_config

_ENGINES = ("claude", "codex")

#: The `engine` task input of `agent.project`; it sets this once at start.
engine_input = ""


@dataclass(frozen=True)
class Pin:
    engine: str  # "" when unpinned
    hard: bool
    #: "direction", "engine input", "COORDINATOR_ENGINE", "AUTHOR_ENGINE (deprecated)", "DELEGATE_ENGINE",
    #: "benchmark hard_pin"; "" when unpinned
    source: str


def fallback_allowed() -> bool:
    return _policy.read("ENGINE_FALLBACK").strip().lower() == "allow"


def _direction_file() -> Path:
    # Under STATE_ROOT, which the checkpoint saves whole, so a direction survives a handoff restart.
    return _session_config.STATE_ROOT / "engine-direction.json"


def set_direction_pin(engine: str) -> None:
    path = _direction_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"engine": engine}))


def direction_pin() -> str:
    """Return the engine the operator directed the project to, or ""."""
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
        engine = _profile.normalize_engine(json.loads(_direction_file().read_text()).get("engine", ""))
        return engine if engine in _ENGINES else ""
    return ""


def _benchmark_hard() -> bool:
    # Late import: `_usage` imports this module to route, and owns the run-wide flag `agent.project` sets.
    from flyte_agent_factory import _usage

    return _usage._hard_pins


def _pin(engine: str, source: str) -> Pin:
    engine = _profile.normalize_engine(engine)
    if engine not in _ENGINES:
        return Pin("", _benchmark_hard(), "")
    if _benchmark_hard():
        return Pin(engine, True, "benchmark hard_pin")
    return Pin(engine, not fallback_allowed(), source)


def author_pin() -> Pin:
    """Return the coordinator's pin: a direction, else the `engine` input, else `COORDINATOR_ENGINE`."""
    if direction := direction_pin():
        return Pin(direction, True, "direction")
    if _profile.normalize_engine(engine_input) in _ENGINES:
        return _pin(engine_input, "engine input")
    configured = _names.coordinator_engine()
    canonical = _names.getenv(_names.COORDINATOR_ENGINE).strip()
    return _pin(configured, "COORDINATOR_ENGINE" if canonical else "AUTHOR_ENGINE (deprecated)")


def delegate_pin() -> Pin:
    """Return the delegates' pin: `DELEGATE_ENGINE` only. They do not inherit the coordinator's."""
    return _pin(_policy.read("DELEGATE_ENGINE"), "DELEGATE_ENGINE")
