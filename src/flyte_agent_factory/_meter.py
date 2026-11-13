"""Usage meters: how much of each account's 5-hour and weekly allowance is used.

Claude (OAuth subscription login): `GET /api/oauth/usage` with the login's access token.
Codex (ChatGPT login): `codex app-server`'s `account/rateLimits/read`, a cheap backend read that
sends no model request. When the access token is near expiry or the CLI lacks this read, use
the `rate_limits` of the last `codex exec --json` event of a real call (`from_events`).

No token is ever logged, persisted or put in an exception message: `MeterError` texts are fixed
strings or status codes, and a `Meter` holds only percentages and times.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast, overload

import httpx
from typing_extensions import Required

if TYPE_CHECKING:
    from . import _codex_auth, _secret_env
    from ._bridge import _write_atomic
else:
    from flyte_agent_factory import _codex_auth, _secret_env
    from flyte_agent_factory._bridge import _write_atomic

# Pending the coordinator's token_meter probe. Enable only when both usage windows are returned.
SETUP_TOKEN_USAGE_SUPPORTED = False
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
#: Separates a setup-token's secret name from its fingerprint in the account key.
FINGERPRINT_SEP = "#"
#: Never read an account more often than this, however many callers ask.
MIN_INTERVAL_S = 300.0
#: A current window at or above this share is full. No reserve: the owner wants all of it. The router takes a
#: full window as a hint only; a benchmark login, which cannot fail over, waits it out (`full_windows`).
ROUTE_AT_PCT = 100.0
#: One warning per window when an account crosses these.
WARN_FIVE_HOUR_PCT = 80.0
WARN_WEEKLY_PCT = 90.0
#: History is trimmed to the newest HISTORY_KEEP lines once it passes HISTORY_MAX.
HISTORY_MAX = 5000
HISTORY_KEEP = 4000
#: Claude's own refusal wording, current ("You've hit your limit") and older CLIs' ("Claude AI usage
#: limit reached"). Nothing broader: a result's free text is often the model's summary of a failure,
#: and "GitHub API rate limit exceeded" there is not a Claude usage limit.
_CLAUDE_LIMIT = re.compile(r"hit your.*limit|claude ai usage limit reached", re.I)
#: A Codex refusal, as Codex words it in its `error` / `turn.failed` events (not in free text).
_CODEX_LIMIT = re.compile(r"usage limit|rate limit|quota|too many requests|429", re.IGNORECASE)
_FIVE_HOURS_MIN = 360  # a Codex window up to this long is the "5-hour" one, a longer one the weekly


class MeterError(Exception):
    """A meter could not be read. The message never contains a credential."""


class _ScopedData(TypedDict, total=False):
    scope: Required[str]
    pct: float | None
    resets_at: float | None


class _MeterFields(TypedDict, total=False):
    account: Required[str]
    source: Required[str]
    observed_at: Required[float]
    five_hour_pct: float | None
    five_hour_resets_at: float | None
    weekly_pct: float | None
    weekly_resets_at: float | None
    limited_until: float | None
    error: str


class MeterData(_MeterFields, total=False):
    scoped: list[_ScopedData]


class _MeterInit(_MeterFields, total=False):
    scoped: list[Scoped]


@dataclass
class Scoped:
    """A weekly limit that applies to one model, e.g. `Fable`."""

    scope: str
    pct: float | None = None
    resets_at: float | None = None


def token_account(secret: str, token: str) -> str:
    """Return the account key of a setup-token: `claude <secret>#<12 hex of sha256(token)>`.

    A token swapped under the same secret name is another account, so it inherits no refusal of the
    old one. The hash identifies the credential without exposing it; an unmounted token has no
    fingerprint and keeps the bare `claude <secret>` key, which is also every stored key from before.
    """
    fingerprint = token_fingerprint(token)
    return f"claude {secret}{FINGERPRINT_SEP}{fingerprint}" if fingerprint else f"claude {secret}"


def token_fingerprint(token: str) -> str:
    """Return the 12 hex of sha256(token) that `token_account` keys a setup-token by; "" for no token."""
    return hashlib.sha256(token.strip().encode()).hexdigest()[:12] if token.strip() else ""


def is_token_account(account: str) -> bool:
    """Return whether `account` carries a setup-token fingerprint."""
    return account.startswith("claude ") and FINGERPRINT_SEP in account


def token_account_secret(account: str) -> str:
    """Return the secret name of a setup-token account, or "" for any other account."""
    return account.removeprefix("claude ").partition(FINGERPRINT_SEP)[0] if is_token_account(account) else ""


@dataclass
class Meter:
    account: str  # "claude <login>" or "codex"
    source: str  # where the reading came from: oauth-usage, app-server, exec-event, limit-text
    observed_at: float
    five_hour_pct: float | None = None
    five_hour_resets_at: float | None = None
    weekly_pct: float | None = None
    weekly_resets_at: float | None = None
    scoped: list[Scoped] = field(default_factory=list)
    #: Set from a refusal ("usage limit ... resets 1:50am"): no capacity until then.
    limited_until: float | None = None
    error: str = ""

    @property
    def engine(self) -> str:
        return self.account.split()[0]

    def to_dict(self) -> MeterData:
        # asdict recursively serializes Scoped using the same fields as _ScopedData.
        return cast(MeterData, asdict(self))

    @classmethod
    def from_dict(cls, data: MeterData) -> Meter:
        scoped = [Scoped(**s) for s in data.get("scoped") or []]
        # Only scoped changes representation between the serialized and constructor shapes.
        return cls(**cast(_MeterInit, {**data, "scoped": scoped}))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _num(value: object) -> float | None:
    """Return a finite number, else None: `1e400` in a payload is `inf`, which no window can mean."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) else None


def _time(value: object) -> float | None:
    """Epoch seconds from an ISO-8601 string (any offset) or a number; None if neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _pair(window: object) -> tuple[float | None, float | None]:
    window = window if isinstance(window, dict) else {}
    return _num(window.get("utilization", window.get("percent"))), _time(window.get("resets_at"))


def parse_claude(payload: object, account: str, now: float) -> Meter:
    """Return the `/api/oauth/usage` body. Unknown keys are ignored and missing ones become None."""
    payload = payload if isinstance(payload, dict) else {}
    limits = [x for x in payload.get("limits") or [] if isinstance(x, dict)]
    by_kind = {x.get("kind"): x for x in limits if x.get("kind") != "weekly_scoped"}
    five_pct, five_at = _pair(payload.get("five_hour") or by_kind.get("session"))
    week_pct, week_at = _pair(payload.get("seven_day") or by_kind.get("weekly_all"))
    scoped = []
    for item in limits:
        if item.get("kind") != "weekly_scoped":
            continue
        model = ((item.get("scope") or {}).get("model") or {}) if isinstance(item.get("scope"), dict) else {}
        pct, at = _pair(item)
        scoped.append(Scoped(str(model.get("display_name") or "?"), pct, at))
    return Meter(account, "oauth-usage", now, five_pct, five_at, week_pct, week_at, scoped)


def from_claude_events(
    events: Sequence[object], account: str = "claude", now: float | None = None
) -> Meter | None:
    """Read quota headers when the CLI exposes them; absent fields stay unknown.

    httpx parses HTTP headers, but neither it nor the CLI normalizes subscription
    windows. Keep that provider-specific mapping here beside the usage parser.
    """
    now = time.time() if now is None else now
    meter = None
    for event in events:
        if not isinstance(event, dict):
            continue
        headers = _find(event, ("response_headers", "headers")) or {}
        headers = {str(k).lower(): v for k, v in headers.items()} if isinstance(headers, dict) else {}
        reading = Meter(account, "cli-headers", now)
        blocking_resets = []
        exhausted_headers = False
        for window, pct_field, reset_field in (
            ("5h", "five_hour_pct", "five_hour_resets_at"),
            ("7d", "weekly_pct", "weekly_resets_at"),
        ):
            prefix = f"anthropic-ratelimit-unified-{window}"
            try:
                pct = float(headers[f"{prefix}-utilization"]) * 100  # type: ignore[arg-type]  # Caught below.
            except (KeyError, TypeError, ValueError):
                pct = None
            if pct is not None and math.isfinite(pct) and 0 <= pct <= 100:
                setattr(reading, pct_field, pct)
            reset = _header_reset(headers.get(f"{prefix}-reset"))
            setattr(reading, reset_field, reset)
            exhausted = (pct is not None and math.isfinite(pct) and pct >= 100) or headers.get(
                f"{prefix}-status"
            ) == "rejected"
            exhausted_headers = exhausted_headers or exhausted
            if exhausted and reset is not None and reset > now:
                blocking_resets.append(reset)
        for name in ("requests", "tokens", "unified"):
            remaining = headers.get(f"anthropic-ratelimit-{name}-remaining")
            try:
                empty = float(remaining) <= 0  # type: ignore[arg-type]  # Caught below.
            except (TypeError, ValueError):
                empty = False
            exhausted = empty or headers.get(f"anthropic-ratelimit-{name}-status") == "rejected"
            exhausted_headers = exhausted_headers or exhausted
            reset = _header_reset(headers.get(f"anthropic-ratelimit-{name}-reset"))
            if exhausted and reset is not None and reset > now:
                blocking_resets.append(reset)
        text = str(event.get("result") or event.get("error") or "")
        limited = event.get("api_error_status", event.get("status_code")) == 429 or (
            event.get("is_error") and _CLAUDE_LIMIT.search(text)
        )
        limited = (
            limited or exhausted_headers or headers.get("anthropic-ratelimit-unified-status") == "rejected"
        )
        if limited:
            # A refusal without a reset still needs a bounded backoff, not invented utilization. The 300 s is
            # only a floor: every caller lets the notice's own later reset replace it (`_keep_later_reset`).
            try:
                retry = float(headers.get("retry-after", 0))  # type: ignore[arg-type]  # Caught below.
            except (TypeError, ValueError):
                retry = 0
            reading.limited_until = max(
                blocking_resets,
                default=now + (retry if math.isfinite(retry) and retry > 0 else 300),
            )
        if reading.five_hour_pct is not None or reading.weekly_pct is not None or limited:
            if meter is not None:
                for field_name in (
                    "five_hour_pct",
                    "five_hour_resets_at",
                    "weekly_pct",
                    "weekly_resets_at",
                    "limited_until",
                ):
                    if getattr(reading, field_name) is None:
                        setattr(reading, field_name, getattr(meter, field_name))
            meter = reading
    return meter


def _header_reset(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]  # Caught below.
    except (ValueError, TypeError):
        return _time(value)
    return number if math.isfinite(number) else None


def _window(window: object, now: float) -> tuple[float | None, float | None, float | None]:
    """Return (used %, reset time, window minutes) of a Codex window in either spelling.

    The app-server writes `usedPercent`/`resetsAt`/`windowDurationMins`; the CLI's own event
    stream writes `used_percent`/`resets_in_seconds`/`window_minutes`.
    """
    if not isinstance(window, dict):
        return None, None, None
    pct = _num(window.get("usedPercent", window.get("used_percent")))
    minutes = _num(window.get("windowDurationMins", window.get("window_minutes")))
    at = _time(window.get("resetsAt", window.get("resets_at")))
    if at is None and _num(window.get("resets_in_seconds")) is not None:
        at = now + float(window["resets_in_seconds"])
    return pct, at, minutes


def parse_codex(snapshot: object, account: str, now: float, source: str) -> Meter | None:
    """Return a Codex rate-limit snapshot (`primary` and `secondary` windows), or None if it has none."""
    if not isinstance(snapshot, dict):
        return None
    windows = [_window(snapshot.get(k), now) for k in ("primary", "secondary")]
    if all(w[0] is None for w in windows):
        return None
    five = week = None
    for index, window in enumerate(windows):
        if window[0] is None:
            continue
        # By length when it is given; else the usual order, primary 5-hour and secondary weekly.
        is_five = window[2] <= _FIVE_HOURS_MIN if window[2] is not None else index == 0
        if is_five and five is None:
            five = window
        elif week is None:
            week = window
    five = five or (None, None, None)
    week = week or (None, None, None)
    return Meter(account, source, now, five[0], five[1], week[0], week[1])


def _find(value: object, keys: Sequence[str], depth: int = 4) -> dict[str, object] | None:
    if depth < 0 or not isinstance(value, dict):
        return None
    for key in keys:
        if isinstance(value.get(key), dict):
            # Only object-valued JSON fields qualify; their keys are JSON strings.
            return cast(dict[str, object], value[key])
    for child in value.values():
        found = _find(child, keys, depth - 1)
        if found is not None:
            return found
    return None


def from_events(events: Sequence[object], account: str = "codex", now: float | None = None) -> Meter | None:
    """Return the Codex meter from the last `rate_limits` seen in a `codex exec --json` event stream."""
    now = time.time() if now is None else now
    for event in reversed(events):
        meter = parse_codex(_find(event, ("rate_limits", "rateLimits")), account, now, "exec-event")
        if meter:
            return meter
    return None


def from_app_server(result: object, account: str, now: float) -> Meter | None:
    """`account/rateLimits/read`: the `codex` bucket of `rateLimitsByLimitId` if listed, else `rateLimits`."""
    if not isinstance(result, dict):
        return None
    buckets = result.get("rateLimitsByLimitId")
    snapshot = buckets.get("codex") if isinstance(buckets, dict) else None
    return parse_codex(snapshot or result.get("rateLimits"), account, now, "app-server")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


async def read_claude(
    credentials_json: str,
    account: str = "claude",
    *,
    client: httpx.AsyncClient | None = None,
    setup_token: bool = False,
) -> Meter:
    """Read a login file or, when setup_token=True, the raw setup-token credential."""
    if setup_token and not SETUP_TOKEN_USAGE_SUPPORTED:
        raise MeterError("setup-token usage endpoint unconfirmed; use CLI limit observations")
    try:
        token = (
            credentials_json if setup_token else json.loads(credentials_json)["claudeAiOauth"]["accessToken"]
        )
        if not isinstance(token, str) or not token:
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise MeterError("the login holds no claudeAiOauth.accessToken") from None
    headers = {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
    try:
        async with contextlib.AsyncExitStack() as stack:
            http = client or await stack.enter_async_context(httpx.AsyncClient(timeout=20))
            # An absent injected client is replaced by the context-owned AsyncClient above.
            response = await cast(httpx.AsyncClient, http).get(USAGE_URL, headers=headers)
        if response.status_code == 401:
            # Refreshing here would consume the login's rotating refresh token.
            raise MeterError("usage request refused (401): the access token has expired")
        if response.status_code != 200:
            raise MeterError(f"usage request failed: HTTP {response.status_code}")
        body = response.json()
    except MeterError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        # Only the class name: an httpx error can carry the request, headers included.
        raise MeterError(f"usage request failed: {type(exc).__name__}") from None
    return parse_claude(body, account, time.time())


@overload
async def read_codex(
    auth_json: str,
    account: str = "codex",
    *,
    command: tuple[str, ...] = ("codex", "app-server", "-c", 'cli_auth_credentials_store="file"'),
    timeout: float = 30.0,
    _method: Literal["account/rateLimits/read"] = "account/rateLimits/read",
) -> Meter: ...


@overload
async def read_codex(
    auth_json: str,
    account: str = "codex",
    *,
    command: tuple[str, ...] = ("codex", "app-server", "-c", 'cli_auth_credentials_store="file"'),
    timeout: float = 30.0,
    _method: Literal["model/list"],
) -> dict[str, object]: ...


async def read_codex(
    auth_json: str,
    account: str = "codex",
    *,
    command: tuple[str, ...] = ("codex", "app-server", "-c", 'cli_auth_credentials_store="file"'),
    timeout: float = 30.0,
    _method: str = "account/rateLimits/read",
) -> Meter | dict[str, object]:
    """Read the Codex meter through `codex app-server` (stdio JSON-RPC); sends no model request."""
    # A probe must never rotate the shared login's refresh token in a disposable home.
    expires = _codex_auth.access_expiry(auth_json)
    if expires is None or not math.isfinite(expires):
        raise MeterError("Codex token expiry unknown; use exec-event or limit-text meters")
    if expires <= time.time() + 300:
        raise MeterError("Codex token near expiry; use exec-event or limit-text meters")
    # Under $HOME, not /tmp: Codex refuses to set up its helper links under a temp dir.
    parent = Path(os.environ.get("HOME") or Path.home()) / ".codex-meter"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as home:
        auth = Path(home) / "auth.json"
        auth.touch(mode=0o600)
        # The access token is all a read needs. With the refresh token, an app-server that decides to
        # refresh would rotate the chain from whichever pod probed, spending the author's copy.
        auth.write_text(_codex_auth.strip_refresh_token(auth_json))
        # Scrubbed like a session's: the app-server is a Codex process, and its tools read their env.
        env = _secret_env.child_env(CODEX_HOME=home)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                env=env,
                limit=4 * 1024 * 1024,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MeterError(f"codex app-server did not start: {type(exc).__name__}") from None
        try:
            result = await asyncio.wait_for(_rate_limits_request(proc, _method), timeout)
        except asyncio.TimeoutError:
            raise MeterError("codex app-server did not answer in time") from None
        except OSError as exc:  # it exited under our writes
            raise MeterError(f"codex app-server connection failed: {type(exc).__name__}") from None
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
    if _method == "model/list":
        return result
    meter = from_app_server(result, account, time.time())
    if meter is None:
        raise MeterError("codex reported no rate-limit windows")
    return meter


async def read_codex_models(auth_json: str) -> dict[str, object]:
    """Discover models under the same no-refresh expiry guard as the meter read."""
    return await read_codex(auth_json, _method="model/list")


async def _rate_limits_request(
    proc: asyncio.subprocess.Process, method: str = "account/rateLimits/read"
) -> dict[str, object]:
    # The process is created with both streams set to PIPE in read_codex.
    async def send(message: dict[str, object]) -> None:
        cast(asyncio.StreamWriter, proc.stdin).write((json.dumps(message) + "\n").encode())
        await cast(asyncio.StreamWriter, proc.stdin).drain()

    async def reply(request_id: int) -> dict[str, object]:
        # Notifications and warnings arrive in between; only the answer to our request counts.
        while True:
            line = await cast(asyncio.StreamReader, proc.stdout).readline()
            if not line:
                raise MeterError("codex app-server exited before answering")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                if "error" in message:
                    raise MeterError(
                        "codex refused the rate-limit read: authentication required or request refused"
                    )
                # Both supported JSON-RPC methods return an object result.
                return cast(dict[str, object], message.get("result") or {})

    await send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "meter", "version": "1"}}})
    await reply(1)
    await send({"method": "initialized"})
    await send({"id": 2, "method": method})
    return await reply(2)


# --------------------------------------------------------------------------- #
# Reading a meter
# --------------------------------------------------------------------------- #


def _current(resets_at: float | None, now: float) -> bool:
    """Return a reading belongs to the running window; once its reset has passed it is history."""
    return resets_at is None or resets_at > now


def headroom(meter: Meter | None, now: float | None = None) -> float | None:
    """Percent of the 5-hour window left, 0 inside a known limit window, None if not known."""
    now = time.time() if now is None else now
    if meter is None:
        return None
    if meter.limited_until is not None and meter.limited_until > now:
        return 0.0
    if meter.five_hour_pct is None or not _current(meter.five_hour_resets_at, now):
        return None
    return max(0.0, 100.0 - meter.five_hour_pct)


def full_windows(meter: Meter, now: float) -> list[float | None]:
    """Return the reset of each window `meter` reads as full, None for one with no known reset.

    A routed call treats a full window as a hint and fails over on an actual refusal (`_router`). A
    benchmark case is pinned to one login, so it has nowhere to fail over to, and a limit mid-case is a
    harness error rather than a measurement: its guard and its admission wait these out instead. A full
    window with no reset counts only while the reading is recent, or an old reading would block for good.
    """
    fresh = now - meter.observed_at <= 2 * MIN_INTERVAL_S
    return [
        reset
        for pct, reset in (
            (meter.five_hour_pct, meter.five_hour_resets_at),
            (meter.weekly_pct, meter.weekly_resets_at),
        )
        if pct is not None and pct >= ROUTE_AT_PCT and (reset > now if reset is not None else fresh)
    ]


def reserve_ok(meter: Meter, k: float, now: float) -> tuple[bool, str, float | None]:
    """Return whether new work may start on `meter`'s account while it holds back `k` % per hour to reset.

    Every window the meter reports must keep `remaining >= k * hours_until_reset`; one it does not report
    (a weekly-only plan has no 5-hour window) is not checked, and neither is one that has reset or has no
    reset time. The reserve falls as the reset nears, so a held-back account reopens by itself: the third
    value is when the last failing window does (None when nothing is held back). k = 0 holds nothing back.
    """
    if k <= 0:
        return True, "", None
    held: list[str] = []
    reopens = now
    for name, pct, reset in (
        ("5h", meter.five_hour_pct, meter.five_hour_resets_at),
        ("weekly", meter.weekly_pct, meter.weekly_resets_at),
    ):
        if pct is None or reset is None or reset <= now:
            continue
        remaining = 100.0 - pct
        hours = (reset - now) / 3600
        if remaining >= k * hours:
            continue
        reopens = max(reopens, reset - remaining / k * 3600)
        resets = datetime.fromtimestamp(reset, timezone.utc)
        held.append(
            f"{name} {remaining:.0f}% left < {k:g}%/h x {hours:.0f}h = {round(k * hours, 1):g}% held back "
            f"(resets {resets:%a %H:%M}Z)"
        )
    if not held:
        return True, "", None
    return False, f"reserve: {'; '.join(held)}", reopens


def capacity_meter(meter: Meter, now: float) -> Meter:
    """Intake cannot infer a current allowance from a stale or incomplete window."""

    def window(pct: float | None, reset: float | None) -> float | None:
        valid = (
            not meter.error
            and math.isfinite(meter.observed_at)
            and 0 <= now - meter.observed_at <= 2 * MIN_INTERVAL_S
            and isinstance(pct, (int, float))
            and not isinstance(pct, bool)
            and math.isfinite(pct)
            and 0 <= pct <= 100
            and isinstance(reset, (int, float))
            and not isinstance(reset, bool)
            and math.isfinite(reset)
            and reset > now
        )
        return pct if valid else None

    return replace(
        meter,
        five_hour_pct=window(meter.five_hour_pct, meter.five_hour_resets_at),
        weekly_pct=window(meter.weekly_pct, meter.weekly_resets_at),
        scoped=[replace(s, pct=window(s.pct, s.resets_at)) for s in meter.scoped],
    )


def exhausted(meter: Meter | None, now: float | None = None) -> bool:
    """Use the router's freshness and refusal rules at every routing entry point."""
    if TYPE_CHECKING:
        from . import _router
    else:
        from flyte_agent_factory import _router

    return _router.exhausted(meter, time.time() if now is None else now)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _clock(at: float | None) -> str:
    return (
        "?"
        if at is None
        else datetime.fromtimestamp(math.floor(at / 60 + 0.5) * 60, timezone.utc).strftime("%H:%MZ")
    )


def _day(at: float | None) -> str:
    if at is None:
        return "?"
    when = datetime.fromtimestamp(at, timezone.utc)
    return f"{when:%b} {when.day}"


def format_line(meter: Meter) -> str:
    """`claude <login>: 5h 34% (resets 06:50Z) · week 16% (resets Oct 24)`."""
    text = (
        f"{meter.account}: 5h {_pct(meter.five_hour_pct)} (resets {_clock(meter.five_hour_resets_at)})"
        f" · week {_pct(meter.weekly_pct)} (resets {_day(meter.weekly_resets_at)})"
    )
    if meter.limited_until is not None:
        text += f" · limited until {_clock(meter.limited_until)}"
    if meter.error:
        text += f" · unavailable ({meter.error})"
    return text


_REPORT_MARK = re.compile(r"<!--meter (\{.*?\})-->")


def report_comment(meter: Meter) -> str:
    """Return the machine-readable twin of `format_line`, hidden in the report for `from_report`."""
    payload = json.dumps(meter.to_dict()).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!--meter {payload}-->"


def from_report(html: str) -> list[Meter]:
    """Return the meters a run's report carries (see `report_comment`); [] for a report with none."""
    meters = []
    for match in _REPORT_MARK.finditer(str(html)):
        with contextlib.suppress(ValueError, TypeError):
            meters.append(Meter.from_dict(json.loads(match.group(1))))
    return meters


# --------------------------------------------------------------------------- #
# The supervisor's record
# --------------------------------------------------------------------------- #


class MeterStore:
    """Latest reading per login plus a bounded history, under one directory (no tokens).

    `latest.json` keeps the readings, the limit windows learned from refusals and the warnings
    already given, so a supervisor restart neither forgets a limit nor repeats a warning.
    """

    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.root, self.clock = root, clock
        self.readings: dict[str, Meter] = {}
        # Supervisor callers have one mounted login per engine; retain their engine view.
        self.latest: dict[str, Meter] = {}
        self.attempts: dict[str, float] = {}
        self.failures: dict[str, str] = {}
        self.warned: list[str] = []
        self.last_attempt = 0.0
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            saved = json.loads((root / "latest.json").read_text())
            for value in saved["latest"].values():
                meter = Meter.from_dict(value)
                self.readings[meter.account] = meter
                self.latest[meter.engine] = meter
            self.warned = list(saved.get("warned", []))
            self.attempts = saved.get("attempts", {})
            self.failures = saved.get("failures", {})
            self.last_attempt = saved.get("last_attempt", 0.0)

    @property
    def history(self) -> Path:
        return self.root / "history.jsonl"

    def due(self, account: str = "") -> bool:
        """Return a failed refresh also waits a full interval, including across restarts."""
        attempted = self.attempts.get(account) if account else self.last_attempt
        return attempted is None or self.clock() - attempted >= MIN_INTERVAL_S

    def mark_attempt(self, account: str = "") -> None:
        if account:
            self.attempts[account] = self.clock()
        else:
            self.last_attempt = self.clock()
        self._save()

    def note_failure(self, account: str, reason: str) -> None:
        """Record a safe error category, never an exception carrying credentials."""
        self.update(Meter(account, "refresh-failed", self.clock(), error=reason))

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_atomic(
            self.root / "latest.json",
            {
                "latest": {k: m.to_dict() for k, m in self.readings.items()},
                "warned": self.warned[-50:],
                "attempts": self.attempts,
                "failures": self.failures,
                "last_attempt": self.last_attempt,
            },
        )

    def update(self, meter: Meter) -> str:
        """Record a reading; returns its history line."""
        previous = self.readings.get(meter.account)
        if previous is None and meter.account != meter.engine:
            # Engine-only records predate login identities; bind them on the first mounted reading.
            previous = self.readings.pop(meter.engine, None)
        # A refusal's reset time outlives a reading that does not know about it.
        if previous and meter.limited_until is None and (previous.limited_until or 0) > self.clock():
            meter.limited_until = previous.limited_until
        self.readings[meter.account] = meter
        self.latest[meter.engine] = meter
        self.attempts[meter.account] = max(self.attempts.get(meter.account, 0), meter.observed_at)
        if meter.error:
            self.failures[meter.account] = meter.error
        else:
            self.failures.pop(meter.account, None)
        line = json.dumps(
            {
                "t": meter.observed_at,
                "account": meter.account,
                "source": meter.source,
                **({"error": meter.error} if meter.error else {}),
                "five_hour": meter.five_hour_pct,
                "five_hour_resets_at": meter.five_hour_resets_at,
                "weekly": meter.weekly_pct,
                "weekly_resets_at": meter.weekly_resets_at,
                **({"limited_until": meter.limited_until} if meter.limited_until is not None else {}),
                **({"scoped": {s.scope: s.pct for s in meter.scoped}} if meter.scoped else {}),
            },
            separators=(",", ":"),
        )
        self._append(line)
        self._save()
        return line

    def _append(self, line: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.history.open("a") as handle:
            handle.write(line + "\n")
        lines = self.history.read_text().splitlines()
        if len(lines) > HISTORY_MAX:
            self.history.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n")

    def note_limit(self, engine: str, until: float, account: str = "", *, error: str = "") -> None:
        """Return a call was refused with a usage limit that resets at `until`.

        With `account`, the refusal belongs to that credential alone: another login's reading of the
        same engine is never marked for it. `error` says the credential is unusable rather than full
        (a token that must be replaced); the router treats both as out until `until`. A known future
        `limited_until` is never shortened: a repeat refusal that carries a guess (or a stale time) must not
        make a weekly limit look over (#125).
        """
        if account:
            meter = self.readings.get(account) or Meter(account, "limit-text", self.clock())
        else:
            meter = (
                self.readings.get(engine)
                or self.latest.get(engine)
                or Meter(engine, "limit-text", self.clock())
            )
        known = meter.limited_until or 0.0
        meter.limited_until = max(until, known) if known > self.clock() else until
        if error:
            meter.error = error
        self.update(meter)

    def warnings(self, meter: Meter) -> list[str]:
        """Warnings this reading earns, each once per window."""
        found = []
        for name, pct, resets_at, limit in (
            ("5h", meter.five_hour_pct, meter.five_hour_resets_at, WARN_FIVE_HOUR_PCT),
            ("weekly", meter.weekly_pct, meter.weekly_resets_at, WARN_WEEKLY_PCT),
        ):
            if pct is None or pct < limit or not _current(resets_at, meter.observed_at):
                continue
            # Rounded to the minute: the reset time wobbles by fractions of a second.
            window = round(resets_at / 60) if resets_at else int(meter.observed_at // 18000)
            key = f"{meter.account}|{name}|{window}"
            if key in self.warned:
                continue
            self.warned.append(key)
            found.append(f"{meter.account}: {name} window at {pct:.0f}% (resets {_clock(resets_at)})")
        if found:
            self._save()
        return found
