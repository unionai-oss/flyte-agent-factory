"""Launch policy: name and policy validation, and which engine a launch is admitted for."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Any, cast

from flyte import remote

from flyte_agent_factory import _errors, _factory_base, _factory_meters, _gardening, _names, _profile, _queue


def substitute_name(brief: str, name: str) -> str:
    # Each project needs its own directory, rather than evidence from a previous run.
    return brief.replace("{name}", name)


_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


def _slug(value: str, label: str) -> None:
    if not _SLUG.fullmatch(value):
        raise ValueError(f"{label} must be a short lowercase slug (2–41 letters, digits or hyphens)")


def validate(name: str, model: str) -> None:
    _slug(name, "name")
    # The name is a Kubernetes label value on every run (LABEL_PROJECT): it must end alphanumeric.
    # `--` is refused too so a run name cut from it can never join two hyphens (`_queue.run_id`).
    if name.endswith("-") or "--" in name:
        raise ValueError("name must not end with a hyphen or contain '--' (it is a Kubernetes label value)")
    # re.ASCII: \w alone would accept any Unicode letter or digit
    if not re.fullmatch(r"[A-Za-z0-9][\w.:\[\]-]*", model, re.ASCII):
        raise ValueError("model must be a model name, e.g. opus or sonnet")


def validate_demo(demo: str) -> None:
    _slug(demo, "demo_package")


POLICY_SHAPE = "policy must be a JSON object of KEY: value strings"


def parse_policy(text: str) -> dict[str, str]:
    """Return the policy overrides in a JSON object string (the chat task's input).

    ValueError if it is not one.
    """
    if not text.strip():
        return {}
    try:
        policy = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{POLICY_SHAPE}: {exc}") from exc
    return _policy_object(policy)


def _policy_object(policy: object) -> dict[str, str]:
    if not isinstance(policy, dict) or not all(isinstance(v, str) for v in policy.values()):
        raise ValueError(POLICY_SHAPE)
    return policy


def normalize_policy(policy: Mapping[str, str]) -> dict[str, str]:
    """Return policy overrides as run environment variables (`AGENT_FACTORY_<KEY>`).

    The prefix is optional on input. A key starting `CLAUDE_` is refused: the factory's variables used to
    be spelled that way and that spelling is no longer read, so it must fail rather than be set under a name
    nothing reads.
    """
    keys = {}
    for key, value in _policy_object(policy).items():
        key = key.removeprefix(_names.PREFIX)
        if key.startswith("CLAUDE_"):
            raise ValueError(f"policy key {key!r} must not start with CLAUDE_; use the key without a prefix")
        if key in _BROWSER_KEYS:
            raise ValueError("a browser grant comes from the launch's browser input, not from policy")
        if not re.fullmatch(r"[A-Z0-9_]+", key):
            raise ValueError(f"policy key {key!r} is not an uppercase policy key (A-Z, 0-9 and _)")
        keys[key] = value
    # Together, so that AUTHOR_ENGINE and COORDINATOR_ENGINE are folded and checked against each other.
    return {
        f"AGENT_FACTORY_{key}": value for key, value in _profile._policy(keys, "policy", "policy").items()
    }


#: The run env var the slot pod reads its browser grant from; only a request's grant may set it.
BROWSER_ENV = "AGENT_FACTORY_BROWSER_IDENTITIES"
#: What the slot pod reads about browsers; set by the factory and the supervisor, never by a launch's policy.
_BROWSER_KEYS = frozenset({"BROWSER_IDENTITIES", "BROWSER_GATE", "BROWSER_IDENTITY_NAMES"})
_IDENTITY = re.compile(r"[a-z][a-z0-9-]{0,19}")


def browser_identity_names(profile: _profile.Profile) -> frozenset[str]:
    """Return the browser identity names `profile` configures (a legacy `personal` one is "personal")."""
    return frozenset(i.name for i in profile.browser.identities)


def parse_browser(text: str) -> tuple[str, ...] | None:
    """Return the identities a launch grants: None for "" (inherit), () for "none", else the sorted names.

    Names are separated by commas or whitespace. ValueError for a malformed name or one the profile does not
    configure, so a launch is refused before anything is queued.
    """
    names = text.replace(",", " ").split()
    if not names:
        return None
    if [n.lower() for n in names] == ["none"]:
        return ()
    configured = browser_identity_names(_factory_base.PROFILE)
    for name in names:
        if not _IDENTITY.fullmatch(name):
            raise ValueError(f"browser identity {name!r} must match {_IDENTITY.pattern}")
        if name not in configured:
            known = f"the configured identities are {', '.join(sorted(configured))}"
            raise ValueError(
                f"unknown browser identity {name!r}; "
                + (known if configured else "this profile configures no browser identities")
            )
    return tuple(sorted(set(names)))


def read_engine_pin(request: _queue.Request) -> str | None:
    """Return the engine `request` is pinned to (its own pin, else the profile's), or None when unpinned."""
    pin = _names.coordinator_engine(request.policy) or _factory_base.PROFILE.policy.get(
        "COORDINATOR_ENGINE", ""
    )
    return _profile.normalize_engine(pin) or None


def pick_launch_engine(request: _queue.Request) -> str:
    """Return the engine a shared launch authors on: the launch's pin, else the profile's, else Claude."""
    return read_engine_pin(request) or "claude"


def _has_token(engine: str) -> bool:
    """Whether the profile has a credential for `engine` on any slot (a Codex login is one slot's)."""
    return bool(
        _factory_base.PROFILE.secrets.claude_token if engine == "claude" else _factory_base.PROFILE.has_codex
    )


def _pick_run_engine(run: remote.Run, launched: Mapping[str, str] = MappingProxyType({})) -> str:
    """Return the engine a live run counts against: the one this sweep launched it for, else its label.

    The run `CreateRun` returns does not carry its labels, so a run launched in this sweep is known by
    `launched`. A run without the label predates the caps (or was started by hand): it ran on Claude.
    """
    return launched.get(run.name) or _factory_base.run_labels(run).get(_factory_base.LABEL_ENGINE, "claude")


def _has_room(engine: str, using: list[str]) -> bool:
    """Whether the shared environment can add a project on `engine` to those on the engines `using`."""
    if len(using) >= _factory_base.cap():
        return False
    # A profile without `[[slots]]` always has [concurrency], and only that profile is shared.
    limit = cast(_profile.Concurrency, _factory_base.PROFILE.concurrency).per_engine.get(engine)
    return limit is None or using.count(engine) < limit


def _shared_admits(request: _queue.Request, live: list[remote.Run], launched: Mapping[str, str]) -> bool:
    """Whether the shared environment has room for `request`: `max_projects`, then its engine's own cap."""
    return _has_room(pick_launch_engine(request), [_pick_run_engine(r, launched) for r in live])


def retain_live_run(run: remote.Run, live: list[remote.Run], busy: set[str]) -> bool:
    """Count a dedupe keeper's actual capacity; return whether it was newly discovered."""
    # A concurrent keeper may have been absent from the sweep's initial listing. The discarded run's
    # slot and engine do not describe the capacity the keeper holds.
    if any(r.name == run.name for r in live):
        return False
    live.append(run)
    busy.add(_factory_base._task_name(run))
    return True


# --- why a launch waits: the reasons an admission pass leaves for `factory.queue_list`


class Waits(dict[str, dict[str, Any]]):
    """The last pass's reasons by request name: `reason` is the text, `wait` the structured detail."""

    #: The launch a pass stopped at to retry it first: what it did not reach waits behind that launch.
    retrying: str = ""

    def add(self, entry: _queue.Entry, code: str, text: str, **detail: Any) -> None:
        self[entry.name] = _queue.describe_wait(entry.gen, code, text, **detail)

    def hold(
        self,
        entry: _queue.Entry,
        i: int,
        admission: _gardening.Admission,
        code: str,
        text: str,
        **detail: Any,
    ) -> None:
        """Note a hold of this request's own (lane, engine, account, project): `low` work is free of it."""
        self.add(entry, code, text, **detail)
        print(f"[queue] {entry.name} waits: {self[entry.name]['reason']}", flush=True)
        admission.mark_unrunnable(i)

    def hold_without_headroom(
        self,
        entry: _queue.Entry,
        i: int,
        admission: _gardening.Admission,
        busy: Collection[str],
        headroom: dict[str, float | None],
    ) -> None:
        """Note that no slot this request may use has headroom (`no_capacity`).

        Its priority hold is released only when another slot still has headroom for other work.

        `busy` are the slots taken; a shared environment's one slot takes every run, so it is never busy.
        """
        shared = _factory_base.PROFILE.shared
        available = [s for s in _factory_base.SLOTS if shared or s not in busy]
        self.add(entry, "no_capacity", "no free slot has headroom (usage reserve or meter)")
        # Its pin or loop budget may restrict this request to slots without headroom.
        if _factory_meters.free_slot(
            sorted(set(_factory_base.SLOTS) - set(available)), task_name=lambda s: s, headroom=headroom
        ):
            admission.mark_unrunnable(i)

    def finish(
        self,
        queue: str,
        waiting: Sequence[_queue.Entry],
        admission: _gardening.Admission,
    ) -> None:
        """Store the pass's reasons; what it did not reach waits behind runnable work, or for a slot.

        Only what still waits keeps a reason: a request the pass started, cancelled or failed is gone.
        """
        for i, entry in enumerate(waiting):
            if entry.name in self or i in admission.started:
                continue
            if admission.held(i):
                text = f"behind {admission.ahead} higher-priority request(s) still waiting"
                self.add(entry, "priority_behind", text, behind=admission.ahead)
            elif self.retrying:
                text = f"behind {self.retrying}, whose launch is retried first"
                self.add(entry, "behind_retry", text, holders=[self.retrying])
            else:
                self.add(entry, "no_free_slot", "every slot is busy")
        try:
            still = {(e.name, e.gen) for e in _queue.pending(queue)}
        except _errors.REMOTE_ERRORS as exc:  # only the listing depends on it: the pass's starts stand
            print(f"[queue] wait reasons not stored: {type(exc).__name__}: {exc}", flush=True)
            return
        _queue.write_waits(
            _factory_base.PROFILE.state.uri, {n: w for n, w in self.items() if (n, w["gen"]) in still}
        )


def word_engine_busy(holders: Sequence[str], limit: int) -> str:
    """Word an `engine_busy` hold: who holds the engine-repo lane, and its limit when it is not 1."""
    # The same wording as the planner's `engine_busy` reason. Not imported from it: `_planner` is admission,
    # which a slot's bundle does not carry (see test_every_package_module_loads_with_the_agent).
    names = ", ".join(holders)
    return f"{names} (limit {limit})" if limit > 1 else names


def word_engine_limit(request: _queue.Request) -> str:
    """Word an `engine_limit` hold: the engine this request would launch on is at its project limit."""
    return f"{pick_launch_engine(request)} is at its project limit"


def keep_slots(busy: set[str], kept_for: list[remote.Run]) -> set[str]:
    """Return `busy` plus a slot for each run handing over to its resume, which `low` work may not take.

    A handed-over slot that another project has taken by now is replaced by a still free one: the resume
    needs a slot, whichever it gets.
    """
    busy = set(busy)
    for slot in (_factory_base._task_name(run) for run in kept_for):
        busy.add(next((s for s in _factory_base.SLOTS if s not in busy), slot) if slot in busy else slot)
    return busy


def describe_queue_wait(
    request: _queue.Request, waits: dict[str, dict[str, Any]], running: bool
) -> dict[str, Any]:
    """Return the `wait_reason` and `wait` fields of a `queue_list` row.

    A stored reason counts only for the generation it was made for, and only with text and a coded `wait`.
    """
    stored = waits.get(request.name)
    if (
        isinstance(stored, dict)
        and stored.get("gen") == request.gen
        and isinstance(stored.get("reason"), str)
        and stored["reason"]
        and isinstance(stored.get("wait"), dict)
        and isinstance(stored["wait"].get("code"), str)
        and stored["wait"]["code"]
    ):
        return {"wait_reason": stored["reason"], "wait": stored["wait"]}
    # No pass has judged this request yet (it was just queued), or its project's run has just started.
    if running:
        return {
            "wait_reason": f"waiting for {request.name}'s current run to finish",
            "wait": {"code": "project_running"},
        }
    return {
        "wait_reason": "not yet judged: it waits for the next admission pass",
        "wait": {"code": "awaiting_admission"},
    }
