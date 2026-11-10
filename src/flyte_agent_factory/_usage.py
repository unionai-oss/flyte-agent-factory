"""Usage accounting and engine routing for reviewers and delegates, and the limit notices that move work."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from flyte_agent_factory import _meter, _names, _policy, _profile, _router, _session_config, _timeline

#: The supervisor's meter record (None outside `project`), and the guard that keeps two
#: callers from reading at once.
_meters: _meter.MeterStore | None = None
_meter_lock = asyncio.Lock()
#: Read both meters this often; `_meter.MIN_INTERVAL_S` still caps it whoever asks.
_METER_EVERY_S = _meter.MIN_INTERVAL_S  # the router counts two of these as stale
#: What reading, recording and showing a meter raises: the reader's own MeterError, the store's file
#: writes (OSError), a reading that will not format or encode (ValueError, TypeError; OverflowError
#: for a reset time too large to display) and a token gone from the environment between the check
#: and the read (KeyError). Meters must not fail the work; anything else is a bug.
_METER_ERRORS = (_meter.MeterError, OSError, ValueError, TypeError, OverflowError, KeyError)


def _show_meter(store: _meter.MeterStore, meter: _meter.Meter, timeline: _timeline._Timeline | None) -> None:
    try:
        if timeline:
            timeline.meter(meter)
            for warning in store.warnings(meter):
                timeline.row("⚠️", warning)
    except _METER_ERRORS as exc:  # malformed display data must not fail a call
        _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)


async def _read_meters(timeline: _timeline._Timeline) -> None:
    """Refresh the Claude and Codex meters, at most once per `_meter.MIN_INTERVAL_S`.

    Called every `_METER_EVERY_S` and on demand before a review round and a delegation; the
    floor makes the on-demand calls cheap. Never raises: a meter is advice, not the work.
    """
    try:
        store = _meters
        if store is None:
            return
        async with _meter_lock:
            if not store.due():
                return
            store.mark_attempt()
            readers: dict[str, Callable[[], Awaitable[_meter.Meter]]] = {}
            token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            if token and _meter.SETUP_TOKEN_USAGE_SUPPORTED:
                readers["claude"] = lambda: _meter.read_claude(
                    token, _session_config._claude_account(), setup_token=True
                )
            # Codex's meter comes from exec events (`CodexProcess.meter`): the app-server read needs a
            # login file, which a Codex access token is not.
            for engine, read in readers.items():
                account = _session_config._claude_account()
                if not store.due(account):
                    continue
                store.mark_attempt(account)
                try:
                    meter = await read()
                    line = store.update(meter)
                except _METER_ERRORS as exc:
                    store.note_failure(account, type(exc).__name__)
                    _show_meter(store, store.readings[account], timeline)
                    _timeline._log_line(f"[meter] {engine}: {type(exc).__name__}", err=True)
                    continue
                # The log is the history's other copy: the owner can rebuild it from `flyte get logs`.
                _timeline._log_line(f"[meter] {line}")
                _show_meter(store, meter, timeline)
    except _METER_ERRORS as exc:  # meters must not fail the work
        _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)


async def _meter_loop(timeline: _timeline._Timeline, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await _read_meters(timeline)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_METER_EVERY_S)
            return
        except asyncio.TimeoutError:
            pass


#: Reviewer results carry usage in a trailer; direct author children keep it in their Flyte report
#: so the task interface stays unchanged and the author receives clean text.
_USAGE_MARK = "\n<!--factory-usage "
_USAGE_TRAILER = re.compile(r"\n<!--factory-usage (\{.*?\})-->\s*$", re.DOTALL)
#: The review round in progress, for calls that do not carry one (arbiter, contract).
_metering_round = 0


def _usage_trailer(stdout: str) -> str:
    """Return the CLI's own usage and notional cost from `claude -p --output-format json`, as a trailer.

    Never raises: it runs inside the sub-agent task after the work is done (qa).
    """
    try:
        payload = json.loads(stdout)
        usage = payload.get("usage") or {}
        rec: dict[str, Any] = {k: int(usage.get(k) or 0) for k in _session_config._USAGE_FIELDS}
        rec["cost_usd"] = float(payload.get("total_cost_usd") or 0.0)
        rec["models"] = sorted((payload.get("modelUsage") or {}).keys())
        rec["modelUsage"] = payload.get("modelUsage") or {}
        rec["turns"] = int(payload.get("num_turns") or 0)
        observed = _meter.from_claude_events([payload], _session_config._claude_account())
        if observed:
            rec["rate_limits"] = observed.to_dict()
        return f"{_USAGE_MARK}{json.dumps(rec)}-->"
    except (ValueError, TypeError, AttributeError, ArithmeticError):  # unparseable usage is no usage
        return ""


def _split_usage(text: str) -> tuple[str, dict[str, Any]]:
    m = _USAGE_TRAILER.search(text or "")
    if not m:
        return text, {}
    try:
        return text[: m.start()], json.loads(m.group(1))
    except ValueError:
        return text[: m.start()], {}


def _with_route(text: str, route: dict[str, Any] | None) -> str:
    """Add a sub-agent's own routing decision to its usage trailer, for the parent to record.

    A sub-agent that routed itself runs in another container, so its `_routes` never reach the
    project's timeline or snapshot unless the result carries them.
    """
    if not route:
        return text
    clean, rec = _split_usage(text)
    return f"{clean}{_USAGE_MARK}{json.dumps({**rec, 'route': route})}-->"


def _ingest_route(route: object, timeline: _timeline._Timeline | None) -> None:
    """Record a decision a sub-agent made and returned (`_with_route`) as if it were ours."""
    if not isinstance(route, dict):
        return
    route = dict(route)
    row = str(route.pop("row", "") or "")
    _session_config._routes.append(route)
    del _session_config._routes[: max(0, len(_session_config._routes) - _ROUTES_KEEP)]
    if row:
        if timeline:
            timeline.row("🔀", row)
        else:
            _timeline._log_line(f"[router] {row}")


def _record_usage(role: str, round_: int, rec: dict[str, Any]) -> None:
    """Add one call's usage to the ledger under `role@round` (round 0 = outside review)."""
    if not rec:
        return
    _session_config._load_usage()
    key = f"{role}@{round_}"
    row = _session_config._usage.setdefault(
        key, {f: 0.0 for f in (*_session_config._USAGE_FIELDS, "cost_usd", "calls")}
    )
    call_id = rec.get("call_id")
    if call_id:
        calls = row.setdefault("call_ids", [])
        if call_id in calls:
            return
        calls.append(call_id)
    for f in (*_session_config._USAGE_FIELDS, "cost_usd"):
        row[f] += float(rec.get(f) or 0)
    for model, counts in rec.get("modelUsage", {}).items():
        usage = row.setdefault("modelUsage", {}).setdefault(model, {})
        for field, count in counts.items():
            if field in (
                "inputTokens",
                "outputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
                "cacheWriteInputTokens",
            ):
                usage[field] = usage.get(field, 0) + count
    row["model_usage_calls"] = row.get("model_usage_calls", 0) + bool(rec.get("modelUsage"))
    row["calls"] += 1
    for field, values in (
        ("models", rec.get("models", [])),
        ("efforts", [rec["effort"]] if rec.get("effort") else []),
        ("engines", [rec["engine"]] if rec.get("engine") else []),
    ):
        row[field] = sorted(set(row.get(field, [])) | set(values))
    with contextlib.suppress(OSError):
        _session_config._usage_path().parent.mkdir(parents=True, exist_ok=True)
        _session_config._usage_path().write_text(json.dumps(_session_config._usage, indent=1))


def _usage_summary(round_: int | None = None) -> str:
    """One line: tokens and notional $ per role, for one round or the whole run."""
    per: dict[str, list[float]] = {}
    for key, row in _session_config._usage.items():
        role, _, r = key.partition("@")
        if round_ is not None and r != str(round_):
            continue
        acc = per.setdefault(role, [0.0, 0.0])
        acc[0] += sum(row[f] for f in _session_config._USAGE_FIELDS)
        acc[1] += row["cost_usd"]
    if not per:
        return ""
    total = sum(v[1] for v in per.values()) or 1.0
    parts = []
    for role, (tok, cost) in sorted(per.items(), key=lambda kv: -kv[1][1]):
        rows = [
            row
            for key, row in _session_config._usage.items()
            if key.partition("@")[0] == role and (round_ is None or key.partition("@")[2] == str(round_))
        ]
        counts = {field: sum(row.get(field, 0) for row in rows) for field in _session_config._USAGE_FIELDS}
        models = sorted({model for row in rows for model in row.get("models", [])})
        engines = sorted({engine for row in rows for engine in row.get("engines", [])})
        by_model: dict[str, dict[str, Any]] = {}
        for row in rows:
            for model, usage in row.get("modelUsage", {}).items():
                totals = by_model.setdefault(model, {})
                for field, count in usage.items():
                    totals[field] = totals.get(field, 0) + count
        basis = {"tokens": counts, "models": models, "engines": engines}
        # Partial ledgers cannot price the entire role from only the retained model split.
        if all(row.get("model_usage_calls", 0) == row.get("calls") for row in rows):
            basis["modelUsage"] = by_model
        parts.append(
            f"{role} {tok / 1e6:.2f}M tok ${cost:.2f} ({100 * cost / total:.0f}%)"
            + " [usage "
            + json.dumps(basis, separators=(",", ":"))
            + "]"
        )
    return "; ".join(parts)


#: What reading the author's transcript can raise: files, and JSON lines that are not the shape expected.
_USAGE_READ_ERRORS = (OSError, ValueError, TypeError, AttributeError)


def _author_usage_split(config_dir: Path, workspace: Path) -> dict[str, dict[str, Any]]:
    """Tokens the author session has used so far, split into `coordinator` and `helpers`.

    `coordinator` is the main conversation: the top-level `<session>.jsonl` transcripts. `helpers` are
    the in-pod Agent-tool helpers, whose transcripts sit under `<session>/subagents/`.

    Claude Code writes one entry per content block of a reply, each carrying the reply's
    usage, so entries are deduplicated by message id. No dollar figure: the interactive
    session reports none.
    """
    folder = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    split: dict[str, dict[str, Any]] = {
        name: {**{f: 0.0 for f in _session_config._USAGE_FIELDS}, "modelUsage": {}}
        for name in ("coordinator", "helpers")
    }
    seen: set[str] = set()
    if not folder.is_dir():
        return split
    # Main transcripts first, so a reply id that appears in both counts for the main conversation.
    paths = sorted(folder.rglob("*.jsonl"), key=lambda p: "subagents" in p.relative_to(folder).parts)
    for path in paths:
        part = split["helpers" if "subagents" in path.relative_to(folder).parts else "coordinator"]
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            if d.get("type") != "assistant" or not isinstance(msg, dict):
                continue
            mid = str(msg.get("id") or d.get("uuid") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            usage = msg.get("usage") or {}
            model = part["modelUsage"].setdefault(msg.get("model") or "unknown", {})
            for field, target in zip(
                _session_config._USAGE_FIELDS,
                ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"),
                strict=True,
            ):
                count = float(usage.get(field) or 0)
                part[field] += count
                model[target] = model.get(target, 0) + count
    return split


def _sum_usage(*parts: dict[str, Any]) -> dict[str, Any]:
    """Add usage dicts of the `_author_usage` shape: the four token fields and `modelUsage` per model."""
    total: dict[str, Any] = {f: sum(p.get(f, 0.0) for p in parts) for f in _session_config._USAGE_FIELDS}
    models: dict[str, dict[str, Any]] = {}
    for part in parts:
        for model, counts in part.get("modelUsage", {}).items():
            into = models.setdefault(model, {})
            for key, count in counts.items():
                into[key] = into.get(key, 0) + count
    return {**total, "modelUsage": models}


def _author_usage(config_dir: Path, workspace: Path) -> dict[str, Any]:
    """Tokens the author session has used so far, helpers included (`_author_usage_split` apart them).

    The total keeps its meaning so that figures from earlier runs stay comparable.
    """
    split = _author_usage_split(config_dir, workspace)
    return _sum_usage(split["coordinator"], split["helpers"])


def _is_coordinator_measured(split: dict[str, dict[str, Any]]) -> bool:
    # A Codex author writes no Claude transcript, so its zeros are "not read", not "nothing used".
    return _author_engine == "claude" or any(
        split["coordinator"].get(f, 0.0) for f in _session_config._USAGE_FIELDS
    )


def _report_usage(round_: int, timeline: _timeline._Timeline) -> None:
    """Return a round's spend by role, plus the run so far, so tuning starts from numbers."""
    _session_config._load_usage()
    author: dict[str, Any] = {}
    with contextlib.suppress(*_USAGE_READ_ERRORS):
        split = _author_usage_split(_session_config.CONFIG_DIR, _session_config.WORKSPACE)
        author = _sum_usage(split["coordinator"], split["helpers"])
        timeline.set_author_usage(split, author, measured=_is_coordinator_measured(split))
    line = _usage_summary(round_)
    if line:
        timeline.row("💰", f"round {round_} spend (sub-agents): {line}")
    if author:
        # Shown apart: the interactive session reports no dollars, and cache reads dominate a
        # plain token sum, so output and fresh input are given separately.
        timeline.row(
            "💰",
            f"run so far, sub-agents: {_usage_summary() or 'none'} | author session: "
            f"{author['output_tokens'] / 1e6:.2f}M out, {author['input_tokens'] / 1e6:.2f}M in, "
            f"{author['cache_creation_input_tokens'] / 1e6:.2f}M cache-write, "
            f"{author['cache_read_input_tokens'] / 1e6:.1f}M cache-read",
        )


#: Roles whose verdict is a second opinion on the author's work.
_QA_ROLES = frozenset({"correctness", "completeness", "craft"})
_hard_pins = False
_ROUTES_KEEP = 200
#: The engine the author session of this process runs on; `project` sets it once it has chosen.
_author_engine = "claude"


def _static_engine(purpose: str) -> tuple[str, bool]:
    """Return the engine a role prefers, and whether policy pins it.

    `AGENT_FACTORY_<PURPOSE>_ENGINE` (set from the profile's policy) is a pin. Without one, a QA judge
    defaults to Codex when it is deployed (a different engine from the Claude author), and the builder
    side (author, delegates, planner) stays on Claude: load is balanced by role, not drained onto one
    engine (owner, inbox 038).
    """
    key = re.sub(r"[^A-Z0-9]", "_", (purpose or "").upper())
    choice = _profile.normalize_engine(_policy.read(f"{key}_ENGINE"))
    if choice not in ("claude", "codex") and purpose in _QA_ROLES:
        choice = _profile.normalize_engine(_policy.read("JUDGE_ENGINE"))  # every judge at once
    if choice in ("claude", "codex"):
        return (
            (choice, True) if choice == "claude" or _session_config._codex_deployed() else ("claude", False)
        )
    if _session_config._codex_deployed() and purpose in _QA_ROLES and _qa_differs():
        return "codex", False
    return "claude", False


def _qa_differs() -> bool:
    differs: bool = _policy.read("QA_DIFFERENT_ENGINE", "true").lower() == "true"
    return differs


def _engine_for(purpose: str) -> str:
    """Return the static choice for `purpose`: the engine the router keeps unless it is limited."""
    return _static_engine(purpose)[0]


def _engine_exhausted(engine: str) -> bool:
    """Return the meter says `engine` cannot take a call now. False when it is unknown."""
    return bool(_meters) and _router.exhausted(_child_meter(engine), time.time())


def _child_meter(engine: str) -> _meter.Meter | None:
    """Return the latest meter for the credential a child on `engine` will use."""
    if not _meters:
        return None
    if engine == "claude":
        return _meters.readings.get(_session_config._claude_account())
    return _meters.latest.get(engine)


def _record_route(decision: _router.Decision, timeline: _timeline._Timeline | None) -> None:
    """Log a decision as a timeline row and keep its machine-readable record."""
    _session_config._routes.append(decision.record())
    del _session_config._routes[: max(0, len(_session_config._routes) - _ROUTES_KEEP)]
    if timeline:
        timeline.row("🔀", decision.row())
    else:
        _timeline._log_line(f"[router] {decision.row()}")


def _route(
    purpose: str,
    timeline: _timeline._Timeline | None,
    *,
    pin: str = "",
    exclude: tuple[str, ...] = (),
    setting: Callable[[str], tuple[str, str]] | None = None,
) -> _router.Decision:
    """Ask the router for `purpose`'s engine from the supervisor's meters, and log the decision.

    A QA judge prefers any engine but the author's. `exclude` is engines that just refused this call.
    `setting(engine)` applies and returns the (model, effort) the call will use there, so the record
    carries them.
    """
    static, pinned = _static_engine(purpose)
    available = _author_engines()
    decision = _router.route(
        purpose,
        {e: _child_meter(e) for e in available} if _meters else {},
        available=available,
        now=time.time(),
        preferred=pin or static,
        pin=pin or (static if pinned else ""),
        hard_pin=_hard_pins,
        builders=(_author_engine,) if purpose in _QA_ROLES and _qa_differs() else (),
        exclude=exclude,
    )
    if setting and not decision.deferred:
        assert decision.engine is not None  # not deferred
        model, effort = setting(decision.engine)
        decision = replace(decision, model=model, effort=effort)
    _record_route(decision, timeline)
    return decision


def _deferred(decision: _router.Decision) -> _router.RouterDeferred:
    # The limit mark is what `_one_judge` and the review loop already wait on.
    return _router.RouterDeferred(f"{USAGE_LIMIT_MARK} {decision.row()}")


def _delegation_engine(timeline: _timeline._Timeline) -> Callable[[], Awaitable[str]]:
    """Return the `Delegations` engine callable: routes each delegation as it starts."""

    async def engine() -> str:
        await _read_meters(timeline)
        decision = _route(
            "delegate",
            timeline,
            setting=lambda target: (
                _policy.resolve_codex("delegate") if target == "codex" else _policy.DEFAULTS["author"]
            ),
        )
        if decision.deferred:
            raise _deferred(decision)
        assert decision.engine is not None  # not deferred
        engine_name: str = decision.engine
        return engine_name

    return engine


#: How long a Codex token that failed to authenticate stays out of routing. Only a new token mends it
#: (a person replaces the `[secrets] codex_token` secret); long enough that nothing retries the dead one.
_CODEX_BROKEN_S = 6 * 3600.0
_CODEX_LOGIN_UNUSABLE = "codex login unusable"


def _note_codex_broken(reason: str = "") -> None:
    """Record the Codex token as unusable, so routers and delegates stop choosing it."""
    if _meters:
        _meters.note_limit(
            "codex", time.time() + _CODEX_BROKEN_S, _limit_account("codex"), error=_CODEX_LOGIN_UNUSABLE
        )
    _timeline._log_line(f"[meter] {_CODEX_LOGIN_UNUSABLE}: {reason[:200]}", err=True)


def _note_engine_limit(engine: str, refusal: str) -> None:
    """Remember a refusal's reset time so later calls can route around the limit."""
    if engine == "codex" and _CODEX_LOGIN_UNUSABLE in refusal:
        _note_codex_broken(refusal)
        return
    if _meters:
        _meters.note_limit(
            engine, _reset_at(refusal) or time.time() + _session_config.LIMIT_RETRY_S, _limit_account(engine)
        )


def _limit_account(engine: str) -> str:
    """Return the account a child's refusal on `engine` belongs to; empty keeps the engine-wide record."""
    return _session_config._claude_account() if engine == "claude" else ""


def _same_limit_notice(text: str, earlier: str) -> bool:
    """Whether `text` is the notice already seen, allowing for a redraw that cut or garbled it."""
    return text == earlier or bool(
        text
        and earlier
        and (
            text.startswith(earlier)
            or earlier.startswith(text)
            or difflib.SequenceMatcher(None, text, earlier).ratio() >= 0.95
        )
    )


def _note_session_limit(
    reset_at: float | None, timeline: _timeline._Timeline, codex_meter: _meter.Meter | None = None
) -> None:
    """Record a refusal of the headless session's own credential as a meter.

    Without a usage endpoint for setup-tokens nothing else reads this account, so slots and the
    router would never learn that the credential is exhausted. A Codex author's refusal is recorded
    as a child's is, on top of the rate limits its last turn reported (`codex_meter`).
    """
    if _author_engine == "codex":
        if not _meters:
            return
        try:
            if codex_meter is not None:
                _meters.update(codex_meter)
                blocking_resets = [
                    at
                    for pct, at in (
                        (codex_meter.five_hour_pct, codex_meter.five_hour_resets_at),
                        (codex_meter.weekly_pct, codex_meter.weekly_resets_at),
                    )
                    if pct is not None and pct >= 100 and at is not None and at > time.time()
                ]
                reset_at = reset_at or (max(blocking_resets) if blocking_resets else None)
            _meters.note_limit(
                "codex", reset_at or time.time() + _session_config.LIMIT_RETRY_S, _limit_account("codex")
            )
            _show_meter(_meters, _meters.latest["codex"], timeline)
        except _METER_ERRORS as exc:  # meters must not fail the work
            _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)
        return
    if not _meters or not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return
    account = _session_config._claude_account()
    try:
        _meters.note_limit("claude", reset_at or time.time() + _session_config.LIMIT_RETRY_S, account)
        _show_meter(_meters, _meters.readings[account], timeline)
    except _METER_ERRORS as exc:  # meters must not fail the work
        _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)


def _ingest_subagent_usage(
    text: str,
    purpose: str,
    round_: int,
    timeline: _timeline._Timeline | None,
    *,
    engine: str = "",
    effort: str = "",
    call_id: str = "",
) -> str:
    clean, rec = _split_usage(text)
    route = rec.pop("route", None)
    for decision in rec.pop("routes", None) or [route]:
        _ingest_route(decision, timeline)
    for refused, until in rec.pop("refusals", {}).items():
        if _meters:
            _meters.note_limit(refused, until, _limit_account(refused))
    # After the refusals: a dead login's record replaces the plain limit noted for the same engine.
    if "codex" in rec.pop("unusable", []):
        _note_codex_broken(f"a sub-agent ({purpose}) could not authenticate")
    limits = rec.pop("rate_limits", None)
    if _meters and isinstance(limits, dict):
        # Codex's own rate-limit report from the call: the cheapest reading there is.
        try:
            meter = _meter.Meter.from_dict(cast("_meter.MeterData", limits))
            _timeline._log_line(f"[meter] {_meters.update(meter)}")
            _show_meter(_meters, meter, timeline)
        except (_meter.MeterError, OSError, ValueError, TypeError) as exc:  # meters must not fail a call
            _timeline._log_line(f"[meter] {type(exc).__name__}", err=True)
    if rec:
        rec.setdefault("engine", engine)
        rec.setdefault("effort", effort)
    if call_id:
        rec["call_id"] = call_id
    _record_usage(purpose or "subagent", round_, rec)
    return clean


#: A sub-agent turned away by the usage limit says so in its result (HTTP 429). Marked so the
#: supervisor waits for the reset instead of asking a human (seen in a past run).
USAGE_LIMIT_MARK = "USAGE_LIMIT:"
_RESET_DAY = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d"
    r"|\d{1,2}/\d{1,2}",
    re.IGNORECASE,
)
_RESET_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(?\s*UTC", re.IGNORECASE)


def _reset_at(text: str, now: float | None = None) -> float | None:
    """Unix time of the next "resets 1:50am (UTC)" after `now`, or None if not given in UTC."""
    # Failed Flyte actions have no outputs. Reuse the meter report format to carry
    # a header reset through their exception text to the supervisor's refusal cache.
    resets: list[float] = [
        meter.limited_until for meter in _meter.from_report(text) if meter.limited_until is not None
    ]
    if resets:
        return max(resets)
    m = _RESET_CLOCK.search(text or "")
    if not m or _RESET_DAY.search(text or ""):
        # A weekday or date ("resets Mon 9am", weekly limits) cannot be placed from the clock
        # alone; reading it as today swallowed a real weekly limit (qa).
        return None
    import datetime as _dt

    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    minute = int(m.group(2) or 0)
    base = _dt.datetime.fromtimestamp(time.time() if now is None else now, tz=_dt.timezone.utc)
    try:
        at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    # The nearest of yesterday/today/tomorrow that is not more than an hour past: a notice
    # handled a minute after the minute it names has reset already (rolling it a day ahead
    # turned a 3 h idle into 24 h), including across midnight (11:30pm seen at 00:10).
    day = _dt.timedelta(days=1)
    floor = base.timestamp() - 3600
    return min(c.timestamp() for c in (at - day, at, at + day) if c.timestamp() >= floor)


def _author_engines() -> tuple[str, ...]:
    """Return the engines this slot holds a credential for, Claude first.

    Read from what the pod was given, so a slot without `AGENT_FACTORY_CODEX_TOKEN_SECRET` is never
    offered Codex.
    """
    claude = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        or _names.getenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "").strip()
    )
    return (*(("claude",) if claude else ()), *(("codex",) if _session_config._codex_deployed() else ()))
