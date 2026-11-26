"""The launch queue's storage: request files and marker files under `queue/` on the state branch.

One directory per project name; inside it, per generation (`gen`, one per launch of that name):

    NNNN.json         the request
    NNNN.started      closed: dispatched ({at, run, url, slot, attempt})
    NNNN.cancelled    closed: cancelled   ({at})
    NNNN.failed       closed: could not be launched ({at, error})
    NNNN.attempt-<n>  a dispatch of attempt n-1 lost its slot; the next run name uses n ({at}), or the
                      run was aborted from inside and is recreated ({at, aborted_run, recreated_from})

A request is pending until one of the first three markers exists. Writes claim nothing by themselves;
the one claim is `take_lease`, a compare-and-swap on the branch tip (`_state_store`). Flyte has none:
creating a run under a name that exists REPLACES that run (measured, not documented), so run names are
random and never repeat. A request that still gets two runs is deduplicated on the server by labels
(`_factory`). The markers are caches of what Flyte already knows.

Everything is synchronous and goes through `_state_store`'s filesystem view: callers are plain
functions (`ops/operate.py`, a thread in a project pod) as well as tasks that already run an event loop.
Paths are `_state_store.ROOT` plus a key; that is the only storage there is.
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
from typing import Any, TypedDict, cast

from flyte_agent_factory import _errors, _state_store
from flyte_agent_factory._seal_spec import SealSpec

#: Suffixes that close a request, strongest first. `started` and `cancelled` can both exist (a cancel
#: and a dispatch crossing); `_outcome` picks between them. A request can only be failed before a run exists.
CLOSING = ("started", "cancelled", "failed")
_FILE = re.compile(r"(\d+)\.(json|started|cancelled|failed|attempt-(\d+)|yield-([a-z0-9-]+))")
_MTIME_KEY = "mtime"  # the only key `StateFS.find` emits
#: Admission order of a request's `priority`, first admitted first. `low` never takes a slot while a
#: request of another priority waits that could start (`_factory._dispatch_pending`).
PRIORITIES = ("expedite", "normal", "low")
#: A dispatcher that dies holding the lease blocks the others for this long. A dispatch is a few
#: Flyte calls, so a few minutes is generous.
LEASE_TTL = 5 * 60
_LEASE = ".dispatch-lease.json"
_BASE36 = string.digits + string.ascii_lowercase
_RUN_NAME_MAX = 30  # the control plane's limit


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
    """What to launch. Never holds a secret: the state branch is readable by everyone with the repository."""

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
    #: A sealed run's spec (`SealSpec.to_json()`), "" for an ordinary project. It holds no secret -- task
    #: repo URL, base sha, cutoff, hosts; the gold patch is never in it, the run gets the snapshot anyway --
    #: so carrying it on a request anyone with the repository can read is acceptable.
    seal: str = ""

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
            request.seal,
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
        if request.seal:
            try:
                SealSpec.from_json(request.seal)
            except ValueError as exc:  # a request is dispatched later; a bad spec must not wait until then
                raise ValueError(f"not a launch request: {exc}") from exc
        return request


@dataclass
class Entry:
    """One generation of one project name, as the state branch shows it."""

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
    """Return the queue prefix inside the state root."""
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


def _fs(path: str) -> _state_store.StateFS:
    """Return the state store's filesystem view; ValueError for a path that is not a state path."""
    _state_store.key_of(path)
    return _state_store.current().fs()


def _path(queue: str, name: str, gen: int, suffix: str) -> str:
    return f"{queue}/{name}/{gen:04d}.{suffix}"


def _write(path: str, body: bytes) -> None:
    _fs(path).pipe_file(path, body)


def listing(prefix: str, suffix: str = ".json") -> list[str]:
    """Every file under `prefix` whose name ends in `suffix`, sorted, as paths `_fs` reads back."""
    return sorted(path for path in _fs(prefix).find(prefix) if path.endswith(suffix))


def _modified(info: dict[str, object]) -> float:
    value = info.get(_MTIME_KEY)
    return float(value) if isinstance(value, (int, float)) else 0.0


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


def find_position(queue: str, name: str, gen: int) -> int:
    """Return the 1-based place of request `name` generation `gen` among the readable waiting launches."""
    readable = [e for e in pending(queue) if e.request is not None]
    return next(i for i, e in enumerate(readable, 1) if (e.name, e.gen) == (name, gen))


def next_gen(found: list[Entry]) -> int:
    """Return the generation a new launch of these entries' name takes."""
    return max((e.gen for e in found), default=0) + 1


#: How far above the bucket's highest generation of a name the new code numbers its launches after a
#: cutover: room for every launch the code still on the old image (hand-overs, re-queues) can make.
CUTOVER_GAP = 100
_MIGRATION = "migration.json"  # written by `ops/migrate_state.py`


def cutover_floor(name: str) -> int:
    """Return the generation `name`'s next launch must exceed: 0, or past what the bucket-era code numbered.

    Runs are matched to requests by their (name, generation) labels, and pods still on the old image
    number their requests from the bucket, so after a migration (`docs/state-store.md`, "Cutover") the
    new code keeps `CUTOVER_GAP` clear above the highest generation the bucket had: the two never label
    a run alike. A store that was never migrated into has no floor.
    """
    body = _state_store.current().read(_MIGRATION)
    if body is None:
        return 0
    gens = json_object(body).get("gens")
    gen = gens.get(name, 0) if isinstance(gens, dict) else 0
    return (gen if isinstance(gen, int) else 0) + CUTOVER_GAP


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


def _parse_lease(body: bytes | None) -> _Lease:
    """Return the lease in `body`, or {} where it is absent, unreadable or not a lease."""
    try:
        lease = json.loads(body) if body is not None else None
    except ValueError:
        return {}
    if not isinstance(lease, dict):
        return {}
    ok = isinstance(lease.get("holder"), str) and isinstance(lease.get("expires"), (int, float))
    # Both lease fields are checked above before exposing their types.
    return cast(_Lease, lease) if ok else {}


def take_lease(queue: str, holder: str, now: float | None = None) -> str | None:
    """None once the dispatch lease is `holder`'s; otherwise the holder that has it.

    This IS mutual exclusion. "Take it if absent, expired or mine" is decided on the branch tip and
    committed in one `StateStore.transact`; a non-forced push is a compare-and-swap on the tip, so when
    two dispatchers race, the second push is rejected, and its re-read shows the first holder.
    What it does not cover is a holder that outlives `LEASE_TTL` mid-dispatch, which the cancel re-reads and
    the label dedupe still catch.
    """
    key = _state_store.key_of(f"{queue}/{_LEASE}")
    expires = 0.0

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], str | None]:
        nonlocal expires
        effective_now = time.time() if now is None else now
        lease = _parse_lease(snap.read(key))
        if lease and lease["holder"] != holder and lease["expires"] > effective_now:
            return {}, lease["holder"]
        expires = effective_now + LEASE_TTL
        body = json.dumps({"holder": holder, "expires": expires}).encode()
        return {key: body}, None

    for _ in range(2):
        other = _state_store.current().transact(decide, f"lease {holder}")
        if other is not None or now is not None or time.time() < expires:
            return other
    raise _state_store.StateError("dispatch lease expired before acquisition completed")


def release_lease(queue: str, holder: str) -> None:
    key = _state_store.key_of(f"{queue}/{_LEASE}")
    store = _state_store.current()

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], None]:
        # Not one that expired and was taken over meanwhile.
        if _parse_lease(snap.read(key)).get("holder") != holder:
            return {}, None
        if store.conditional_delete:
            return {key: None}, None
        # An object store cannot delete only if unchanged, and a delete could remove the lease another
        # dispatcher took after this read. A conditional write of an already expired lease cannot, and
        # `take_lease` treats it as free.
        return {key: json.dumps({"holder": "", "expires": 0}).encode()}, None

    store.transact(decide, f"release lease {holder}")


def exists(path: str) -> bool:
    """Whether a file exists at `path`, read from the branch as it is now, not as it was a second ago."""
    return _state_store.current().exists(_state_store.key_of(path), fresh=True)


def json_object(body: bytes | None) -> dict[str, Any]:
    """Return the JSON object in `body`, or {} where it is absent, unreadable or not an object."""
    try:
        value = json.loads(body) if body is not None else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def read_json(path: str) -> dict[str, Any]:
    """Return the JSON object at `path`, or {} where it is absent, unreadable or not an object."""
    try:
        return read_json_or_raise(path)
    except (OSError, ValueError):
        return {}


def read_json_or_raise(path: str) -> dict[str, Any]:
    """Return the JSON object at `path`, {} where it is absent or not an object; raise if it cannot be read.

    For a caller that reports a store it cannot reach (`OSError`) or a broken file (`ValueError`) rather than
    taking either for "nothing there".
    """
    try:
        raw = _fs(path).cat_file(path)
    except FileNotFoundError:
        return {}
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def write_json(path: str, value: dict[str, Any]) -> None:
    _write(path, json.dumps(value, sort_keys=True).encode())


_WAITS = f"{_state_store.ROOT}/waits.json"


def describe_wait(gen: int, code: str, text: str = "", **detail: Any) -> dict[str, Any]:
    """Build one stored wait record: `reason` is `code: text`, `wait` is `code` plus its detail keys.

    The dispatcher and the planner both record through it, so a code's JSON has one shape whoever wrote it.
    """
    return {"gen": gen, "reason": f"{code}: {text}" if text else code, "wait": {"code": code, **detail}}


def write_waits(waits: dict[str, dict[str, Any]]) -> None:
    """Store why each waiting request waited at the last admission pass, by request name.

    `queue_list` reads it back: the reasons are the admission decision's own, so the listing cannot
    drift from what the dispatcher or planner did. Each reason carries the `gen` it was made for. Only a
    listing depends on it, so a store that fails never stops an admission pass.
    """
    try:
        write_json(_WAITS, waits)
    except _errors.REMOTE_ERRORS as exc:  # a failed git push is a StateError, not an OSError
        print(f"[queue] wait reasons not stored: {type(exc).__name__}: {exc}", flush=True)


def read_waits() -> dict[str, dict[str, Any]]:
    """Return the reasons stored by `write_waits`; {} where there are none."""
    try:
        stored = read_json(_WAITS)
    except _errors.REMOTE_ERRORS as exc:  # optional diagnostics must not hide the readable queue
        print(f"[queue] wait reasons not read: {type(exc).__name__}: {exc}", flush=True)
        return {}
    return {k: v for k, v in stored.items() if isinstance(v, dict)}


#: The accounts a usage reserve can be set for: the engines, a profile having one login for each.
RESERVE_ACCOUNTS = ("claude", "codex")


_RESERVE = f"{_state_store.ROOT}/reserve.json"


def read_reserves() -> dict[str, float]:
    """Return each account's reserve constant `k` (`_meter.reserve_ok`); one with none holds nothing back.

    It lives in `reserve.json` on the state branch, so a chat session changes it (`factory.set_reserve`)
    and the next admission reads it without a redeploy. A bad or absent entry is no reserve.
    """
    return _reserves_in(read_json(_RESERVE))


def _reserves_in(stored: dict[str, Any]) -> dict[str, float]:
    return {
        account: float(k)
        for account, k in stored.items()
        if account in RESERVE_ACCOUNTS and isinstance(k, (int, float)) and math.isfinite(k) and k > 0
    }


def put_reserve(account: str, k: float) -> None:
    """Store `k` for `account`; 0 removes the reserve."""
    if account not in RESERVE_ACCOUNTS:
        raise ValueError(f"unknown account {account!r}; the accounts are {', '.join(RESERVE_ACCOUNTS)}")
    if not math.isfinite(k) or k < 0:
        raise ValueError(f"k must be a number of percent per hour, 0 or more, not {k!r}")
    key = _state_store.key_of(_RESERVE)

    def decide(snap: _state_store.SnapshotView) -> tuple[dict[str, bytes | None], None]:
        # The whole file is rewritten, so it is read here, on the tip the push is made against.
        stored = {a: v for a, v in _reserves_in(json_object(snap.read(key))).items() if a != account}
        body = json.dumps({**stored, **({account: k} if k else {})}, sort_keys=True).encode()
        return {key: body}, None

    _state_store.current().transact(decide, f"reserve {account}")


def reopen(queue: str, name: str, gen: int) -> None:
    """Remove the `started` marker, so the request waits again (its run went away)."""
    path = _path(queue, name, gen, "started")
    _fs(path).rm(path)


def cancelled(queue: str, name: str, gen: int) -> bool:
    """Return whether a cancel marker exists.

    Read from the branch tip, not the cache: the dispatcher's last look before it claims.
    """
    return exists(_path(queue, name, gen, "cancelled"))


def effective_brief(request: Request) -> str:
    """Return the brief a run gets.

    A resume appends the new text to the project's brief as dated direction.
    """
    if not request.base:
        return request.brief
    day = datetime.fromtimestamp(request.enqueued_at, timezone.utc).strftime("%Y-%m-%d")
    return f"{request.base}\n\n## Further direction ({day})\n\n{request.brief}"
