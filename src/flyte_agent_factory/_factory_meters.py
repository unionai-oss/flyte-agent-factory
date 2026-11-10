"""Login meters and slot headroom: which slot a launch goes to, and what the loop may spend."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from flyte import remote

from flyte_agent_factory import _factory_base, _meter, _profile, _queue, _router
from flyte_agent_factory._errors import REMOTE_ERRORS

if TYPE_CHECKING:
    from flyte_agent_factory import _loop


def slot_meters(slots: Sequence[str]) -> dict[str, list[_meter.Meter]]:
    """Return the meters in each slot's newest run's report; `[]` where there is no run or no readable meter.

    Never raises: `{}` when the listing itself fails. Read-only, for advice and for the planner.
    """
    try:
        newest: dict[str, remote.Run] = {}
        for slot in slots:
            runs = remote.Run.listall(task_name=slot, limit=1, sort_by=("created_at", "desc"))
            for run in runs:
                if _factory_base._task_name(run) == slot:
                    newest.setdefault(slot, run)
    except REMOTE_ERRORS:  # advice only
        return {}
    found: dict[str, list[_meter.Meter]] = {}
    for slot in slots:
        try:
            found[slot] = _meter.from_report(_factory_base.report_html(newest[slot]))
        except (*REMOTE_ERRORS, _meter.MeterError):  # the report is missing or has no readable meter
            found[slot] = []
    return found


def login_meters(
    slots: Sequence[str] | None = None,
    *,
    now: float | None = None,
    raw: bool = False,
    reports: Mapping[str, list[_meter.Meter]] | None = None,
    bench: bool = True,
) -> dict[str, _meter.Meter]:
    """Newest per-login observations from reports and the existing probe's Flyte outputs.

    These are the central read-only cache; no factory state is written to object storage. `raw`
    returns the readings as observed, for the router, whose own rules keep a full window full until
    its reset; otherwise stale or incomplete windows are blanked for intake (`capacity_meter`).
    """
    now = time.time() if now is None else now
    found: dict[str, _meter.Meter] = {}

    def accept(meter: _meter.Meter, slot: _profile.Slot | None = None) -> None:
        # Old Codex reports lacked the profile binding: bind to the Codex token.
        if meter.account == "codex" and slot and (codex := _factory_base.PROFILE.codex_secret(slot)):
            meter.account = f"codex {codex}"
        previous = found.get(meter.account)
        if previous is None or meter.observed_at > previous.observed_at:
            if previous and (previous.limited_until or 0) > (meter.limited_until or 0):
                meter.limited_until = previous.limited_until
            found[meter.account] = meter
        elif (meter.limited_until or 0) > (previous.limited_until or 0):
            previous.limited_until = meter.limited_until

    # `reports`: the caller's own `slot_meters` result, so the slots are not listed twice.
    by_task = {slot.task: slot for slot in _factory_base.PROFILE.slots}
    for task, meters in (
        slot_meters(_factory_base.SLOTS if slots is None else slots) if reports is None else reports
    ).items():
        for meter in meters:
            accept(meter, by_task.get(task))
    # Slot ranking (`bench=False`) has no use for the bench's probes.
    probes = (
        ("factory.read_login_meter", "factory-bench.read_login_meter")
        if bench
        else ("factory.read_login_meter",)
    )
    for task_name in probes:
        try:
            runs = remote.Run.listall(
                task_name=task_name,
                limit=100,
                in_phase=("SUCCEEDED",),
                sort_by=("created_at", "desc"),
            )
            for run in runs:
                if not hasattr(run, "outputs"):
                    continue
                try:
                    (payload,) = run.outputs()
                    accept(_meter.Meter.from_dict(json.loads(payload)))
                except (*REMOTE_ERRORS, ValueError, TypeError, KeyError):
                    continue
        except REMOTE_ERRORS:
            pass
    if raw:
        return found
    return {account: _meter.capacity_meter(meter, now) for account, meter in found.items()}


#: The factory's own mount of `[secrets] claude_token`, read only to fingerprint the token's meter account.
#: Not `CLAUDE_CODE_OAUTH_TOKEN`: `read_login_meter` reads that name as the credential to meter.
TOKEN_ENV = "FACTORY_CLAUDE_TOKEN"
_token_fingerprint: str | None = None


def mounted_token_fingerprint() -> str:
    """Return the fingerprint of the setup-token mounted into this pod (`_meter.token_fingerprint`), or "".

    Read once and removed from the environment, so nothing this process spawns inherits the token. A
    token swapped under the secret reaches the next factory pod, which fingerprints the new value.
    """
    global _token_fingerprint
    if _token_fingerprint is None:
        _token_fingerprint = _meter.token_fingerprint(os.environ.pop(TOKEN_ENV, ""))
    return _token_fingerprint


def token_meter(secret: str, meters: Iterable[_meter.Meter]) -> _meter.Meter | None:
    """Return the newest of `meters` that speaks for the setup-token stored as `secret`, or None.

    A setup-token's readings are keyed by its fingerprint (`_meter.token_account`), or by the bare name
    when they predate it. With the token mounted here only its current fingerprint counts: a swapped-out
    token's refusal must not hold the slot. Without the mount (a local caller) the newest reading of the
    secret speaks for its current token, and an old one ages out behind it.
    """
    account = f"claude {secret}"
    fingerprint = mounted_token_fingerprint()
    current = f"{account}{_meter.FINGERPRINT_SEP}{fingerprint}" if fingerprint else ""

    def ours(meter: _meter.Meter) -> bool:
        if meter.account == account:
            return True
        return bool(
            meter.account == current if current else _meter.token_account_secret(meter.account) == secret
        )

    return max(filter(ours, meters), key=lambda m: m.observed_at, default=None)


def match_claude_meter(
    profile: _profile.Profile,
    slot: _profile.Slot,
    readings: Mapping[str, _meter.Meter],
    reports: Mapping[str, list[_meter.Meter]],
) -> _meter.Meter | None:
    secret = profile.secrets.claude_token
    # `readings` already merges every slot's report, so the slot's own is only the fallback.
    return token_meter(secret, readings.values()) or token_meter(secret, reports.get(slot.task, []))


#: `slot_headroom` for a slot whose every engine refused and has not reset: it cannot take a project.
#: Not 0.0, which is a full meter: that is only a hint, so such a slot ranks last but still launches.
LIMITED = -1.0


def _engine_meters(
    binding: _profile.Slot, cached: Mapping[str, _meter.Meter], reports: Mapping[str, list[_meter.Meter]]
) -> dict[str, _meter.Meter | None]:
    """Return the meter of each engine login a slot can run on, None where there is no reading."""
    meters: dict[str, _meter.Meter | None] = {}
    # Both tokens are shared by every slot, so a refusal seen on any slot must prevent admitting work
    # on it too.
    meters["claude"] = match_claude_meter(_factory_base.PROFILE, binding, cached, reports)
    if codex_secret := _factory_base.PROFILE.codex_secret(binding):
        codex = f"codex {codex_secret}"
        meters["codex"] = cached.get(codex) or next(
            (m for m in reports.get(binding.task, []) if m.account == codex), None
        )
    return meters


def reserve_holds(*, now: float | None = None) -> list[str]:
    """Return why each account is held back by its usage reserve, e.g. `claude reserve: weekly 22% left ...`.

    Empty when no reserve is set or every account clears it. For the queue listing and the overview.
    """
    state = _factory_base.PROFILE.state.uri
    reserves = _queue.read_reserves(state) if state else {}
    if not reserves or not _factory_base.PROFILE.slots:
        return []
    now = time.time() if now is None else now
    reports = slot_meters(_factory_base.SLOTS)
    cached = login_meters(_factory_base.SLOTS, now=now, raw=True, reports=reports, bench=False)
    holds = []
    for engine, reading in _engine_meters(_factory_base.PROFILE.slots[0], cached, reports).items():
        if (
            reading is not None
            and not (check := _meter.reserve_ok(reading, reserves.get(engine, 0.0), now))[0]
        ):
            holds.append(f"{engine} {check[1]}")
    return holds


def slot_headroom(slots: Sequence[str], *, now: float | None = None) -> dict[str, float | None]:
    """Return the headroom of the engine each slot would run on, using both windows and idle-token probes.

    A slot can use the work token and the Codex token, and is routed among them as
    `_router.route` routes any call: it is `LIMITED` only when every one of them refused. None where
    nothing is known: no run on the slot, no meter in its report, or a reading from a window that has
    since reset. Never raises; the caller falls back to the slot order.
    """
    now = time.time() if now is None else now
    found: dict[str, float | None] = {}
    # Busy slots can observe a refusal on the same credential a free slot would use.
    reports = slot_meters(_factory_base.SLOTS)
    reserves = (
        _queue.read_reserves(_factory_base.PROFILE.state.uri) if _factory_base.PROFILE.state.uri else {}
    )
    cached = login_meters(_factory_base.SLOTS, now=now, raw=True, reports=reports, bench=False)
    bindings = {slot.task: slot for slot in _factory_base.PROFILE.slots}
    for slot in slots:
        if slot not in reports:
            continue
        binding = bindings.get(slot)
        if binding is None:
            found[slot] = None  # not a slot of this profile: no token to read
            continue
        meters = _engine_meters(binding, cached, reports)
        engines = list(meters)
        if not engines:
            found[slot] = None
            continue
        # A login under its reserve takes no new project, as if it had refused until the reserve falls.
        for engine, reading in meters.items():
            if reading is not None:
                ok, _, reopens = _meter.reserve_ok(reading, reserves.get(engine, 0.0), now)
                if not ok:
                    meters[engine] = replace(reading, limited_until=reopens)
        # Profile order is the author's preference, as `agent._choose_author_engine` routes it.
        decision = _router.route("author", meters, available=engines, now=now, preferred=engines[0])
        if decision.deferred:
            found[slot] = LIMITED
        elif decision.engine is None or meters[decision.engine] is None:  # not deferred: an engine was chosen
            found[slot] = None
        else:
            found[slot] = next(c for c in decision.candidates if c.engine == decision.engine).headroom
    return found


def free_slot(
    live: Sequence[Any],  # runs, or slot names when `task_name` is the identity
    preferred: str | None = None,
    *,
    task_name: Callable[[Any], str] = _factory_base._task_name,
    headroom: Mapping[str, float | None] | None = None,
) -> str | None:
    """Return the slot to launch on: a free one, with the most headroom across both windows first.

    One project per login is absolute, so only free slots are ever candidates. A slot whose
    headroom is unknown counts as a fresh window (a reading older than its window says nothing
    about the new one); ties keep the order of `preferred` first, then the profile.
    """
    busy = {task_name(r) for r in live}
    free = [s for s in sorted(_factory_base.SLOTS, key=lambda s: s != preferred) if s not in busy]
    # A slot whose every engine refused cannot take the project: the request waits for one that can.
    free = [s for s in free if not (headroom and headroom.get(s) == LIMITED)]
    if headroom and any(headroom.get(s) is not None for s in free):
        free.sort(key=lambda s: -(100.0 if (room := headroom.get(s)) is None else room))
    return free[0] if free else None


def _loop_slot_budget(slot: str, *, demand: bool) -> _loop.Budget:
    from . import _loop, _loop_flow

    now = time.time()
    limits = _factory_base.PROFILE.loop
    if limits.daily_usd <= 0 or limits.weekly_usd <= 0:
        return _loop.Budget("wait", "caps-unset")
    binding = next(s for s in _factory_base.PROFILE.slots if s.task == slot)
    readings = login_meters(raw=True)
    engine = "claude"
    # A setup-token's readings carry its fingerprint; `match_claude_meter` finds the current one.
    reading = match_claude_meter(_factory_base.PROFILE, binding, readings, {})
    try:
        spend = _loop_flow._spend(now)
    except REMOTE_ERRORS:
        return _loop.Budget("wait", "spend-pending", now + _router.DEFAULT_RETRY_S)
    return _loop.budget(
        now,
        spend,
        {engine: reading},
        demand,
        False,
        daily_usd=limits.daily_usd,
        weekly_usd=limits.weekly_usd,
        owner_floor_pct=limits.owner_floor_pct,
    )
