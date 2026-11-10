"""The launch queue's storage: request objects and marker objects under `<[state] uri>/queue/`.

One directory per project name; inside it, per generation (`gen`, one per launch of that name):

    NNNN.json         the request
    NNNN.started      closed: dispatched ({at, run, url, slot, attempt})
    NNNN.cancelled    closed: cancelled   ({at})
    NNNN.failed       closed: could not be launched ({at, error})
    NNNN.attempt-<n>  a dispatch of attempt n-1 lost its slot; the next run name uses n ({at}), or the
                      run was aborted from inside and is recreated ({at, aborted_run, recreated_from})

A request is pending until one of the first three markers exists. There is no compare-and-swap
here: `flyte.storage` does not expose conditional puts, so nothing in this module claims anything.
Nor does Flyte: creating a run under a name that exists REPLACES that run (measured, not documented),
so run names are random and never repeat. Dispatchers are serialised by an approximate lease
(`take_lease`), and a request that still gets two runs is deduplicated on the server by labels
(`_factory`). The markers are caches of what Flyte already knows.

Everything is synchronous and goes through the filesystem `flyte.storage` itself reads and writes
with (`get_underlying_filesystem`): callers are plain functions (`ops/operate.py`, a thread in a
project pod) as well as tasks that already run an event loop, and fsspec's sync methods work in both.
"""

from __future__ import annotations

import json
import math
import re
import secrets
import string
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, TypedDict, cast, overload

import flyte.storage

from flyte_agent_factory import _errors

#: Suffixes that close a request, strongest first. `started` and `cancelled` can both exist (a cancel
#: and a dispatch crossing); `_outcome` picks between them. A request can only be failed before a run exists.
CLOSING = ("started", "cancelled", "failed")
_FILE = re.compile(r"(\d+)\.(json|started|cancelled|failed|attempt-(\d+)|yield-([a-z0-9-]+))")
_MTIME_KEYS = ("mtime", "LastModified", "last_modified")
#: Admission order of a request's `priority`, first admitted first. `low` never takes a slot while a
#: request of another priority waits that could start (`_factory._dispatch_pending`).
PRIORITIES = ("expedite", "normal", "low")
#: A dispatcher that dies holding the lease blocks the others for this long. A dispatch is a few
#: Flyte calls, so a few minutes is generous.
LEASE_TTL = 5 * 60
#: Seconds between writing the lease and re-reading it; see `take_lease`.
LEASE_SETTLE = 2
_LEASE = ".dispatch-lease.json"
_BASE36 = string.digits + string.ascii_lowercase
_RUN_NAME_MAX = 30  # the control plane's limit


class _Filesystem(Protocol):
    def makedirs(self, path: str, *, exist_ok: bool) -> None: ...
    def pipe_file(self, path: str, body: bytes) -> None: ...
    @overload
    def find(self, path: str, *, detail: Literal[True]) -> dict[str, dict[str, object]]: ...
    @overload
    def find(self, path: str) -> list[str]: ...
    def cat_file(self, path: str) -> bytes: ...
    def rm(self, path: str) -> None: ...
    def exists(self, path: str) -> bool: ...


class _Marker(TypedDict, total=False):
    at: float
    run: str
    aborted_run: str
    recreated_from: str
    url: str
    slot: str
    attempt: int
    error: str


class _Lease(TypedDict, total=False):
    holder: str
    expires: float


@dataclass(frozen=True)
class Request:
    """What to launch. Never holds a secret: the bucket is readable cluster-wide."""

    name: str
    gen: int
    kind: str  # "new" | "resume"
    brief: str  # what the launcher passed
    model: str
    repo: str
    demo_package: str
    policy: dict[str, str]
    enqueued_at: float
    resume_from: str = (
        ""  # the newest SUCCEEDED run when enqueued; informational, dispatch searches the runs again
    )
    base: str = ""  # a resume: the project's brief so far, which `brief` is appended to as direction

    issue: str = ""
    priority: str = "normal"  # one of PRIORITIES
    reason: str = ""  # why the factory queued this itself, e.g. "token rotated"; shown wherever it waits
    #: Browser identities (sorted, unique names, never secrets) this project's run may use; none by default.
    browser: tuple[str, ...] = ()

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> Request:
        """Return the request in `data`; ValueError if it is not one."""
        try:
            raw = json.loads(data)
            if isinstance(raw.get("browser"), list):  # JSON has no tuples
                raw["browser"] = tuple(raw["browser"])
            request = cls(**raw)
        except (TypeError, ValueError) as exc:  # bad JSON, not an object, missing or unknown keys
            raise ValueError(f"not a launch request: {exc}") from exc
        strings = (
            request.name,
            request.kind,
            request.brief,
            request.model,
            request.repo,
            request.demo_package,
            request.resume_from,
            request.base,
            request.issue,
            request.priority,
            request.reason,
        )
        policy = request.policy
        if not (
            all(isinstance(s, str) for s in strings)
            and isinstance(request.gen, int)
            and isinstance(request.enqueued_at, (int, float))
            and isinstance(policy, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in policy.items())
            and request.kind in ("new", "resume")
            and request.priority in PRIORITIES
            and isinstance(request.browser, tuple)
            and all(isinstance(b, str) for b in request.browser)
        ):
            raise ValueError("not a launch request: a field has the wrong type or value")
        return request


@dataclass
class Entry:
    """One generation of one project name, as the bucket shows it."""

    name: str
    gen: int
    closed: str = ""  # one of CLOSING, or "" while pending
    attempt: int = 0  # highest attempt marker seen; the next dispatch uses it
    #: Runs a dispatcher created and aborted itself on losing the slot (`yield-<run>` markers, one per run,
    #: so two dispatchers that yield at once cannot overwrite each other's record).
    yielded: frozenset[str] = frozenset()
    modified: float = 0.0  # best effort; 0 where the filesystem does not say
    request: Request | None = None  # None until `load`, and after it if `error`
    error: str = ""
    corrupt: bool = False  # readable, but the body names another (name, gen) than the path does

    @property
    def pending(self) -> bool:
        return not self.closed


def root(uri: str) -> str:
    """Return the queue prefix inside the `[state] uri`."""
    return f"{uri.rstrip('/')}/queue"


def run_id(name: str, gen: int, attempt: int) -> str:
    """Return a run name no run has had before: `<name>-<gen>-<attempt>-<6 base36>`.

    At most 30 characters and starting with a letter; it is the name that is cut to fit, never the
    random suffix. It is random on purpose: creating a run under a name that exists replaces that
    run, so nothing here may be derived from (name, gen, attempt) alone.
    """
    tail = f"-{gen}-{attempt}-" + "".join(secrets.choice(_BASE36) for _ in range(6))
    head = name if name[:1].isalpha() else f"p{name}"
    # A cut that lands just after a hyphen must not leave `--` against the tail's own.
    return head[: _RUN_NAME_MAX - len(tail)].rstrip("-") + tail


def _fs(path: str) -> _Filesystem:
    # fsspec has no stubs; Flyte returns its synchronous filesystem interface.
    return cast(_Filesystem, flyte.storage.get_underlying_filesystem(path=path))


def _path(queue: str, name: str, gen: int, suffix: str) -> str:
    return f"{queue}/{name}/{gen:04d}.{suffix}"


def _write(path: str, body: bytes) -> None:
    fs = _fs(path)
    if path.startswith("file://"):  # object stores have no directories to make; a local path does
        fs.makedirs(path.removeprefix("file://").rsplit("/", 1)[0], exist_ok=True)
    fs.pipe_file(path, body)


def listing(prefix: str, suffix: str = ".json") -> list[str]:
    """Every object under `prefix` whose name ends in `suffix`, sorted, as paths `_fs` reads back.

    Object-store listings (obstore's S3 and GCS stores) return `bucket/key` without the scheme, and `_fs`
    of that is the local filesystem: reads miss and writes land on local disk. So the scheme of `prefix`
    is put back. Not `fs.unstrip_protocol`: that adds the store's first protocol, which need not be the one
    the caller configured (`gs` against `gcs`), and turns a plain local prefix into a `file://` one.
    """
    scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
    found = _fs(prefix).find(prefix)
    return sorted(path if "://" in path else scheme + path for path in found if path.endswith(suffix))


def _modified(info: dict[str, object]) -> float:
    for key in _MTIME_KEYS:
        value = info.get(key)
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def entries(queue: str, name: str | None = None) -> list[Entry]:
    """Every generation under the queue (or of one `name`), from a single listing. Requests are not read."""
    prefix = f"{queue}/{name}" if name else queue
    found: dict[tuple[str, int], Entry] = {}
    closers: dict[tuple[str, int], dict[str, float]] = {}  # the closing markers each entry has, by mtime
    for path, info in _fs(prefix).find(prefix, detail=True).items():
        parts = path.rstrip("/").rsplit("/", 2)
        match = _FILE.fullmatch(parts[-1]) if len(parts) == 3 else None
        if not match:
            continue  # not ours; a foreign object must not break the listing
        key = (parts[1], int(match[1]))
        entry = found.setdefault(key, Entry(*key))
        kind = match[2]
        if kind in CLOSING:
            closers.setdefault(key, {})[kind] = _modified(info)
        elif match[3]:
            entry.attempt = max(entry.attempt, int(match[3]))
        elif match[4]:
            entry.yielded = entry.yielded | {match[4]}
    for key, kinds in closers.items():
        entry = found[key]
        entry.closed = _outcome(queue, entry, set(kinds))
        entry.modified = kinds[entry.closed]
    return sorted(found.values(), key=lambda e: (e.name, e.gen))


def _outcome(queue: str, entry: Entry, kinds: set[str]) -> str:
    """Which closing marker of `entry` says what happened.

    A dispatch and a cancel that cross leave both `started` and `cancelled`. The later one wins, by the
    time written inside the markers (a filesystem's mtime may be missing or coarse). A cancel that
    recorded the started run among its `aborted_run` wins whatever the order: that run was aborted, and
    the dispatcher's `started` may have been written after the cancel finished.
    """
    if {"started", "cancelled"} <= kinds:
        started = marker(queue, entry.name, entry.gen, "started")
        cancel = marker(queue, entry.name, entry.gen, "cancelled")
        aborted = [r.strip() for r in str(cancel.get("aborted_run", "")).split(",")]
        if started.get("run") in aborted or cancel.get("at", 0) >= started.get("at", 0):
            return "cancelled"
        return "started"
    return min(kinds, key=CLOSING.index)


def load(queue: str, entry: Entry) -> Entry:
    """`entry` with its request read, or with `error` set when the object is missing or unreadable.

    The path is the request's identity: markers, labels and cancel all use it. A body that names another
    (name, gen) is `corrupt` and is not loaded, so nothing can act on the wrong project.
    """
    path = _path(queue, entry.name, entry.gen, "json")
    try:
        request = Request.from_json(_fs(path).cat_file(path))
    except (OSError, ValueError) as exc:  # FileNotFoundError is an OSError
        entry.request, entry.error = None, f"{type(exc).__name__}: {exc}"
        return entry
    if (request.name, request.gen) != (entry.name, entry.gen):
        entry.request, entry.corrupt = None, True
        entry.error = (
            f"corrupt request: stored as {entry.name}/{entry.gen} but names {request.name}/{request.gen}"
        )
    else:
        entry.request = request
    return entry


def pending(queue: str, name: str | None = None) -> list[Entry]:
    """Open requests, loaded, in FIFO order: `enqueued_at`, then name. Unreadable ones come last."""
    waiting = [load(queue, e) for e in entries(queue, name) if e.pending]
    return sorted(
        waiting, key=lambda e: (e.request is None, e.request.enqueued_at if e.request else 0, e.name)
    )


def next_gen(found: list[Entry]) -> int:
    """Return the generation a new launch of these entries' name takes."""
    return max((e.gen for e in found), default=0) + 1


def enqueue(queue: str, request: Request) -> None:
    """Write `request`; writing the same (name, gen) again replaces it, which keeps its place in line."""
    _write(_path(queue, request.name, request.gen, "json"), request.to_json())


def mark(queue: str, name: str, gen: int, suffix: str, **body: object) -> None:
    """Write the marker `suffix` ("started", "cancelled", "failed" or "attempt-<n>") with `body`, stamped."""
    _write(_path(queue, name, gen, suffix), json.dumps({"at": time.time(), **body}).encode())


def marker(queue: str, name: str, gen: int, suffix: str) -> _Marker:
    """Return the body of a marker, or {} where it is absent or unreadable."""
    path = _path(queue, name, gen, suffix)
    try:
        value = json.loads(_fs(path).cat_file(path))
    except (OSError, ValueError):
        return {}
    # Marker writers supply these fields; reads only check that the body is an object.
    return cast(_Marker, value) if isinstance(value, dict) else {}


def _read_lease(path: str) -> _Lease:
    """Return the lease at `path`, or {} where it is absent, unreadable or not a lease."""
    try:
        lease = json.loads(_fs(path).cat_file(path))
    except (OSError, ValueError):
        return {}
    ok = isinstance(lease, dict) and isinstance(lease.get("holder"), str)
    # Both lease fields are checked above before exposing their types.
    return cast(_Lease, lease) if ok and isinstance(lease.get("expires"), (int, float)) else {}


def take_lease(queue: str, holder: str, now: float | None = None) -> str | None:
    """None once the dispatch lease is `holder`'s; otherwise the holder that has it.

    Same honesty as `_deployer._take_lease`: this is not mutual exclusion. Object stores give no
    compare-and-swap, so two dispatchers can both read "no lease" and both write. After writing we
    wait `LEASE_SETTLE` seconds and read again, so a racing write has landed and the last writer
    carries on. A slower race, or a store that serves stale reads, still lets two through; the
    label dedupe after run creation is what catches those.
    """
    now = time.time() if now is None else now
    path = f"{queue}/{_LEASE}"
    lease = _read_lease(path)
    if lease and lease["holder"] != holder and lease["expires"] > now:
        return lease["holder"]
    _write(path, json.dumps({"holder": holder, "expires": now + LEASE_TTL}).encode())
    time.sleep(LEASE_SETTLE)
    lease = _read_lease(path)
    return None if lease.get("holder") == holder else lease.get("holder", "unknown")


def release_lease(queue: str, holder: str) -> None:
    path = f"{queue}/{_LEASE}"
    if _read_lease(path).get("holder") == holder:  # not one that expired and was taken over meanwhile
        _fs(path).rm(path)


def read_json(path: str) -> dict[str, Any]:
    """Return the JSON object at `path`, or {} where it is absent, unreadable or not an object."""
    try:
        value = json.loads(_fs(path).cat_file(path))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: str, value: dict[str, Any]) -> None:
    _write(path, json.dumps(value, sort_keys=True).encode())


def _waits_path(uri: str) -> str:
    return f"{uri.rstrip('/')}/waits.json"


def describe_wait(gen: int, code: str, text: str = "", **detail: Any) -> dict[str, Any]:
    """Build one stored wait record: `reason` is `code: text`, `wait` is `code` plus its detail keys.

    The dispatcher and the planner both record through it, so a code's JSON has one shape whoever wrote it.
    """
    return {"gen": gen, "reason": f"{code}: {text}" if text else code, "wait": {"code": code, **detail}}


def write_waits(uri: str, waits: dict[str, dict[str, Any]]) -> None:
    """Store why each waiting request waited at the last admission pass, by request name.

    `queue_list` reads it back: the reasons are the admission decision's own, so the listing cannot
    drift from what the dispatcher or planner did. Each reason carries the `gen` it was made for. Only a
    listing depends on it, so a store that is absent or fails never stops an admission pass.
    """
    if not uri:
        return
    try:
        write_json(_waits_path(uri), waits)
    except _errors.REMOTE_ERRORS as exc:  # an object store raises obstore's errors, not OSError
        print(f"[queue] wait reasons not stored: {type(exc).__name__}: {exc}", flush=True)


def read_waits(uri: str) -> dict[str, dict[str, Any]]:
    """Return the reasons stored by `write_waits`; {} where there are none."""
    if not uri:
        return {}
    try:
        stored = read_json(_waits_path(uri))
    except _errors.REMOTE_ERRORS as exc:  # optional diagnostics must not hide the readable queue
        print(f"[queue] wait reasons not read: {type(exc).__name__}: {exc}", flush=True)
        return {}
    return {k: v for k, v in stored.items() if isinstance(v, dict)}


#: The accounts a usage reserve can be set for: the engines, a profile having one login for each.
RESERVE_ACCOUNTS = ("claude", "codex")


def _reserve_path(uri: str) -> str:
    return f"{uri.rstrip('/')}/reserve.json"


def read_reserves(uri: str) -> dict[str, float]:
    """Return each account's reserve constant `k` (`_meter.reserve_ok`); one with none holds nothing back.

    It lives in `reserve.json` under `[state] uri`, so a chat session changes it (`factory.set_reserve`)
    and the next admission reads it without a redeploy. A bad or absent entry is no reserve.
    """
    stored = read_json(_reserve_path(uri))
    return {
        account: float(k)
        for account, k in stored.items()
        if account in RESERVE_ACCOUNTS and isinstance(k, (int, float)) and math.isfinite(k) and k > 0
    }


def put_reserve(uri: str, account: str, k: float) -> None:
    """Store `k` for `account`; 0 removes the reserve."""
    if account not in RESERVE_ACCOUNTS:
        raise ValueError(f"unknown account {account!r}; the accounts are {', '.join(RESERVE_ACCOUNTS)}")
    if not math.isfinite(k) or k < 0:
        raise ValueError(f"k must be a number of percent per hour, 0 or more, not {k!r}")
    stored = read_reserves(uri)
    stored.pop(account, None)
    write_json(_reserve_path(uri), {**stored, **({account: k} if k else {})})


def _checkpoint_path(uri: str, run_name: str) -> str:
    return f"{uri.rstrip('/')}/checkpoints/{run_name}.json"


def note_checkpoint(uri: str, run_name: str, location: str, app: str = "") -> None:
    """Record where run `run_name` saves its checkpoint, and the API key it runs with.

    The location is only known to the run itself; Flyte does not list it. `app` is the client id of the
    key the run's pods authenticate with (`_auth.own_client_id`), never its secret: the factory compares
    it with who the server says aborted the run. It sits beside the queue (under the `[state] uri`), not
    in it, because the queue's listing reads every file it finds.
    """
    note = {"at": time.time(), "location": location, "app": app}
    _write(_checkpoint_path(uri, run_name), json.dumps(note).encode())


def _read_run_note(uri: str, run_name: str) -> dict[str, Any]:
    path = _checkpoint_path(uri, run_name)
    try:
        value = json.loads(_fs(path).cat_file(path))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_checkpoint_uri(uri: str, run_name: str) -> str:
    """Return where `note_checkpoint` said the run saves its checkpoint, or "" if it never did."""
    return str(_read_run_note(uri, run_name).get("location", ""))


def read_app_client_id(uri: str, run_name: str) -> str:
    """Return the client id `note_checkpoint` recorded for the run's API key, or "" if it never did."""
    return str(_read_run_note(uri, run_name).get("app", ""))


def reopen(queue: str, name: str, gen: int) -> None:
    """Remove the `started` marker, so the request waits again (its run went away)."""
    path = _path(queue, name, gen, "started")
    _fs(path).rm(path)


def cancelled(queue: str, name: str, gen: int) -> bool:
    """Return whether a cancel marker exists.

    Read straight from storage: the dispatcher's last look before it claims.
    """
    path = _path(queue, name, gen, "cancelled")
    return _fs(path).exists(path)


def effective_brief(request: Request) -> str:
    """Return the brief a run gets.

    A resume appends the new text to the project's brief as dated direction.
    """
    if not request.base:
        return request.brief
    day = datetime.fromtimestamp(request.enqueued_at, timezone.utc).strftime("%Y-%m-%d")
    return f"{request.base}\n\n## Further direction ({day})\n\n{request.brief}"
