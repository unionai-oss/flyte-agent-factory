"""Pure, read-only admission proposals. Callers supply snapshots, including free capacity.

An empty predicted-files tuple means the whole repository, never an empty edit scope.
No comments, remote reads, submissions or clock reads participate in a decision.
"""

from __future__ import annotations

import fnmatch
import glob
import logging
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import TYPE_CHECKING, Any, Protocol

from . import _gardening, _github, _issues, _router
from ._meter import Meter, reserve_ok

if TYPE_CHECKING:
    from ._board import BoardRow

#: Run labels `_factory`/`_reconcile` put on every issue run: the issue's hash and the generation.
LABEL_ISSUE = "factory-issue"
LABEL_GEN = "factory-gen"
LABEL_PROJECT = "factory-project"

log = logging.getLogger(__name__)
#: Engines the router can place work on; a slot for any other engine cannot take a launch.
ENGINES = ("claude", "codex")


@dataclass(frozen=True)
class BoardItem:
    node_id: str
    number: int
    repo: str
    type: str = "issue"
    open: bool = True
    status: str = ""
    position: int = 0
    labels: tuple[str, ...] = ()
    body: str = ""
    linked_run: str = ""
    #: Identities of currently open linked PRs, supplied from the PR snapshot.
    linked_prs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActiveRun:
    name: str
    repo: str
    issue: str = ""
    generation: str = ""
    phase: str = "RUNNING"
    predicted_files: tuple[str, ...] = ()
    observed_files: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OpenPR:
    repo: str
    changed_files: tuple[str, ...] = ()
    issue: str = ""
    identity: str = ""


@dataclass(frozen=True)
class Capacity:
    """One free execution slot, with its engine login and optional current meter.

    A slot that can run on either engine is listed once per engine with the same `slot`; launching on one
    entry uses the slot, so its other entries go too. An entry with no `slot` is a slot of its own.
    """

    engine: str
    login: str
    meter: Meter | None = None
    slot: str = ""
    #: The account's reserve constant (`_queue.read_reserves`): % of a window held back per hour to reset.
    reserve: float = 0.0


@dataclass(frozen=True)
class ManualRequest:
    name: str
    repo: str
    issue: str = ""
    files: tuple[str, ...] | None = None
    body: str = ""
    #: The effective priority (`_gardening.ORDER`) the request is admitted by, as slots mode ranks it.
    priority: str = "normal"
    policy: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Facts:
    items: Sequence[BoardItem] = ()
    active_runs: Sequence[ActiveRun] = ()
    open_prs: Sequence[OpenPR] = ()
    capacity: Sequence[Capacity] = ()
    manual_requests: Sequence[ManualRequest] = ()
    tracked_paths: Mapping[str, set[str]] = field(default_factory=dict)
    repo_aliases: Mapping[str, str] = field(default_factory=dict)
    #: UTC epoch seconds, supplied so the router never needs to read a clock.
    now: float = 0
    #: Context only; never used for admission or approval.
    comments: Sequence[str] = ()
    loop_enabled: bool = False
    loop_killed: bool = False
    #: The author's preferred engine (the profile's `COORDINATOR_ENGINE` policy): it takes the launch
    #: whenever it is not limited, as the author session's own routing would choose.
    author_engine: str = "claude"
    #: Explicit author-engine pins of queued requests, by identity (a manual request's name or a board
    #: item's node id). Admission keeps a pin unless it is exhausted, and says so in the launch reason; an
    #: engine with no credential waits with a reason rather than launch on the other.
    pins: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Reason:
    code: str
    with_identity: str = ""
    #: Extra context, e.g. `scope=heuristic` when a guessed scope is what conflicted.
    detail: str = ""
    #: For `low_priority_waits`: how many runnable requests it waits behind (not in its identity).
    behind: int = field(default=0, compare=False)


@dataclass(frozen=True)
class Launch:
    item: BoardItem | ManualRequest
    engine: str
    login: str
    predicted_files: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Waiting:
    item: BoardItem | ManualRequest
    reason: Reason


@dataclass(frozen=True)
class Plan:
    launches: tuple[Launch, ...]
    waiting: tuple[Waiting, ...]
    #: Capacity slots that were skipped because their engine cannot take work.
    unusable_slots: tuple[Reason, ...] = ()


def _canonical_repo(repo: str, aliases: Mapping[str, str]) -> str:
    repo = aliases.get(repo, repo).strip()
    if "://" in repo or repo.startswith("git@"):
        repo = _github.parse_repo(repo)
    return repo.removesuffix(".git").lower()


def _issue_label(ref: str) -> str:
    # `issue_label` rejects anything but owner/repo#N; a bad reference simply has no label.
    try:
        return _issues.issue_label(ref)
    except ValueError:
        return ""


def _normalize_path(value: str) -> str:
    value = value.strip()
    if (
        not value
        or value.startswith("/")
        or ".." in value.split("/")
        or any(c in value for c in "\\:{}\x00\n\r")
    ):
        raise ValueError("Files must contain repository-relative paths or globs")
    if any("**" in part and part != "**" for part in value.split("/")):
        raise ValueError("invalid Files globstar")
    # fnmatch accepts unmatched brackets as literals; contracts must reject those typos.
    index = 0
    while index < len(value):
        if value[index] == "[":
            end = value.find("]", index + 1)
            if end < 0 or value[index + 1 : end] in ("", "!"):
                raise ValueError("invalid Files glob")
            chars = value[index + 1 : end].removeprefix("!")
            if any(chars[i] == "-" and chars[i - 1] > chars[i + 1] for i in range(1, len(chars) - 1)):
                raise ValueError("invalid Files glob range")
            index = end
        elif value[index] == "]":
            raise ValueError("invalid Files glob")
        index += 1
    result = posixpath.normpath(value)
    return "**" if result == "." else result


def _read_files(body: str) -> tuple[str, ...] | None:
    # This contract accepts a heading and plain list, not arbitrary rendered Markdown.
    # A renderer would still need the same path validation and list-boundary policy.
    lines = body.splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        if in_fence:
            continue
        title = re.sub(r"^#{1,6}\s+", "", line.strip()).rstrip(":").strip("* ").rstrip(":")
        if title.lower() != "files":
            continue
        paths = []
        for entry in lines[index + 1 :]:
            if not entry.strip():
                continue
            match = re.fullmatch(r"\s*(?:[-+*]|\d+[.)])\s+(.*?)\s*", entry)
            if not match:
                break
            paths.append(_normalize_path(match[1].strip("`")))
        if not paths:
            raise ValueError("Files list is empty")
        return tuple(dict.fromkeys(paths))
    return None


#: Separators that end a path-like token in issue prose. Tracked paths with these characters
#: (spaces, commas, quotes) cannot be found by the heuristic; it is a cheap guess, not a parser.
_TOKEN_SEPARATORS = re.compile(r"[\s`'\"(),;<>=:|]+")


def _path_candidates(body: str) -> set[str]:
    """Every token of `body` that could be a path, with markdown/sentence punctuation trimmed."""
    found = set()
    for token in _TOKEN_SEPARATORS.split(body):
        found.add(token)
        found.add(token.strip("*").rstrip(".!?"))
    return found


def _predict_files(
    item: BoardItem | ManualRequest, tracked: set[str], issue_body: str = ""
) -> tuple[tuple[str, ...], str, bool]:
    """`(files, source, literal)`. Only `Files` entries are globs; tracked paths are literal."""
    declared = item.files if isinstance(item, ManualRequest) else None
    if declared is None:
        declared = _read_files(item.body)
    if declared is None and issue_body:
        declared = _read_files(issue_body)
    if declared is not None:
        if not declared:
            raise ValueError("Files list is empty")
        return tuple(dict.fromkeys(_normalize_path(path) for path in declared)), "declared Files", False
    # The git index is supplied by the caller; searching the filesystem here would make the
    # pass impure. One tokenization and a set intersection, not a regex per tracked path.
    found = tuple(sorted(_path_candidates(f"{item.body}\n{issue_body}") & tracked))
    if found:
        return found, "tracked-path heuristic", True
    return (), "unknown scope: whole repository", False


def _has_glob(path: str) -> bool:
    return any(c in path for c in "*?[")


def _directory_prefix(path: str) -> tuple[str, ...]:
    parts = path.split("/")
    for index, part in enumerate(parts):
        if _has_glob(part):
            return tuple(parts[:index])
    return tuple(parts)


def _overlap(left: str, right: str) -> bool:
    if _has_glob(left) and _has_glob(right):
        a, b = _directory_prefix(left), _directory_prefix(right)
        return a[: len(b)] == b or b[: len(a)] == a
    if not _has_glob(left):
        left, right = right, left
    patterns, paths = left.split("/"), right.split("/")

    # pathlib/glob need filesystem reads or lack directory-prefix matching. fnmatch supplies
    # segment matching; this small walk adds directory occupancy and zero-or-more '**'.
    @cache
    def matches(i: int, j: int) -> bool:
        if i == len(patterns) or j == len(paths):
            return True
        if patterns[i] == "**":
            return matches(i + 1, j) or matches(i, j + 1)
        return fnmatch.fnmatchcase(paths[j], patterns[i]) and matches(i + 1, j + 1)

    return matches(0, 0)


def _conflicts(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return not left or not right or any(_overlap(a, b) for a in left for b in right)


def _literal(path: str) -> str:
    """Return a changed path as a pattern that matches only itself (`glob.escape`, which fnmatch honours)."""
    path = posixpath.normpath(path.strip())
    if not path or path.startswith("/") or ".." in path.split("/") or "\x00" in path:
        raise ValueError("changed paths must be repository-relative")
    return glob.escape(path)


def _occupied_files(predicted: tuple[str, ...], observed: tuple[str, ...] = ()) -> tuple[str, ...]:
    # Observing a few edits does not narrow an unknown prediction. Invalid external facts
    # also reserve the repository, rather than silently letting conflicting work through.
    # Predictions come from `Files` lists, so they may be globs; observed paths are real files.
    if not predicted:
        return ()
    try:
        return tuple(
            dict.fromkeys((*(_normalize_path(path) for path in predicted), *(_literal(p) for p in observed)))
        )
    except ValueError:
        return ()


def _changed_files(changed: tuple[str, ...]) -> tuple[str, ...]:
    # A PR with no listed files, or an unreadable one, reserves the repository.
    try:
        return tuple(dict.fromkeys(_literal(path) for path in changed))
    except ValueError:
        return ()


def _scope_detail(source: str) -> str:
    if source == "tracked-path heuristic":
        return "scope=heuristic"
    return "scope=unknown" if source.startswith("unknown") else ""


def _choose_capacity(
    capacity: list[Capacity], now: float, preferred: str = "claude", pin: str = ""
) -> tuple[Capacity | None, str]:
    # The router compares engines; first find the best free login for each engine so an
    # exhausted login cannot hide another login with headroom. A login with no reading is
    # assumed available, as `_factory_meters.free_slot` counts it, rather than ranked below every
    # measured one.
    # A login under its reserve is out until the reserve falls below what it has left, as if it had refused.
    held: dict[str, str] = {}
    adjusted = []
    for slot in capacity:
        if slot.meter is not None:
            ok, why, reopens = reserve_ok(slot.meter, slot.reserve, now)
            if not ok:
                held.setdefault(slot.engine, f"{slot.engine} {why}")
                slot = replace(slot, meter=replace(slot.meter, limited_until=reopens))
        adjusted.append(slot)
    capacity = adjusted
    best: dict[str, tuple[Capacity, float]] = {}
    for slot in capacity:
        decision = _router.route("author", {slot.engine: slot.meter}, available=[slot.engine], now=now)
        candidate = next(c for c in decision.candidates if c.engine == slot.engine)
        if decision.deferred:
            continue
        current = best.get(slot.engine)
        rank = 100.0 if candidate.headroom is None else candidate.headroom
        if current is None or rank > current[1]:
            best[slot.engine] = slot, rank
    # Include exhausted engines so the ordinary router reason explains the fallback.
    meters = {slot.engine: slot.meter for slot in capacity}
    meters.update({engine: slot.meter for engine, (slot, _) in best.items()})
    decision = _router.route(
        "author",
        meters,
        available=list(dict.fromkeys(s.engine for s in capacity)),
        now=now,
        preferred=preferred,
        pin=pin,
    )
    chosen = best[decision.engine][0] if decision.engine in best else None
    pinned = [slot for slot in capacity if slot.engine == pin]
    if chosen and pinned and chosen.engine != pin:
        # The pod is handed the pin and runs it unless its own router moves off it too (and says so),
        # so the launch, its record and the board name the pin's login, not the engine admission
        # would have preferred. The reason still says the pin looked exhausted.
        chosen = best[pin][0] if pin in best else pinned[0]
    return chosen, decision.reason if chosen else "; ".join(held.values())


def is_loop(item: object) -> bool:
    """Return whether a board item carries the `loop` label, in any case.

    A spelling must not escape the loop's pause, single flight and spend attribution (`priority` is
    matched the same way).
    """
    return any(str(label).lower() == "loop" for label in (getattr(item, "labels", None) or ()) if label)


def _priority_of(entry: BoardItem | ManualRequest) -> str:
    """Return the priority `entry` is admitted by: a request's own, `expedite` for a labelled board item."""
    if isinstance(entry, ManualRequest):
        return entry.priority
    labels = {str(label).lower() for label in (entry.labels or ()) if label}
    return "expedite" if "expedite" in labels else "normal"


def plan(facts: Facts) -> Plan:
    """Propose manual requests and owner Ready issues, then loop Ready issues, by priority.

    Within a priority, manual requests come first. A `low` request waits while a request that can start waits.

    Capacity is a precomputed list of free slots. Every proposal immediately reserves its
    slot, issue identity and files in this local pass; the input snapshots stay untouched.
    """
    launches, waiting = [], []
    free = [slot for slot in facts.capacity if slot.engine in ENGINES]
    unusable = tuple(
        Reason("unusable_slot", slot.login) for slot in facts.capacity if slot.engine not in ENGINES
    )
    occupied: list[tuple[str, tuple[str, ...], str]] = []
    # The project can end before PR review and benchmarking finish the loop's iteration.
    loop_running = any(
        item.type == "issue"
        and item.open
        and is_loop(item)
        and (item.status in {"In flight", "In review"} or item.linked_prs)
        for item in facts.items
    )
    owner_waiting = False
    running: dict[tuple[str, str], str] = {}
    prs: dict[tuple[str, str], str] = {}
    tracked: dict[str, set[str]] = {}
    issue_ids: dict[tuple[str, str], str] = {}

    def safe_repo(repo: str) -> str:
        # Facts about other work are read leniently: an unreadable repository name still groups
        # that work together, and only the item being decided is rejected for it.
        try:
            return _canonical_repo(repo, facts.repo_aliases)
        except (_github.GithubError, AttributeError, TypeError):
            return str(repo or "").strip().lower()

    issue_bodies: dict[tuple[str, str], str] = {}
    for item in facts.items:
        if item.type != "issue":
            continue
        repo = safe_repo(item.repo)
        ref = f"{repo}#{item.number}"
        for key in filter(None, (item.node_id, ref, _issue_label(ref))):
            issue_ids[repo, key] = item.node_id
        issue_bodies[repo, item.node_id] = item.body

    def resolve_issue(repo: str, issue: str) -> str:
        if "#" in issue:
            issue_repo, number = issue.rsplit("#", 1)
            # A bare `#N` is relative to the repository of whatever carries the reference.
            issue = f"{safe_repo(issue_repo) if issue_repo else repo}#{number}"
        return issue_ids.get((repo, issue), issue)

    project_runs: dict[str, str] = {}  # project name -> its live run, by the queue's label
    for repo, paths in facts.tracked_paths.items():
        tracked.setdefault(safe_repo(repo), set()).update(paths)
    for run in facts.active_runs:
        if run.phase.upper() in {"SUCCEEDED", "FAILED", "ABORTED", "CANCELLED", "CANCELED", "TIMED_OUT"}:
            continue
        loop_running = loop_running or "factory-loop" in run.labels
        if project := run.labels.get(LABEL_PROJECT):
            project_runs[project] = run.name
        repo = safe_repo(run.repo)
        occupied.append((repo, _occupied_files(run.predicted_files, run.observed_files), run.name))
        issue = run.issue or run.labels.get("factory-issue", "")
        if issue:
            running[repo, resolve_issue(repo, issue)] = run.name
    for pr in facts.open_prs:
        repo = safe_repo(pr.repo)
        identity = pr.identity or f"{repo}#PR"
        occupied.append((repo, _changed_files(pr.changed_files), identity))
        if pr.issue:
            prs[repo, resolve_issue(repo, pr.issue)] = identity
    items = sorted(
        (item for item in facts.items if item.status == "Ready"),
        key=lambda item: (
            is_loop(item),
            "priority" not in {str(label).lower() for label in (item.labels or ()) if label},
            item.position if isinstance(item.position, (int, float)) else float("inf"),
        ),
    )

    def identity_of(item: BoardItem | ManualRequest) -> str:
        return item.node_id if isinstance(item, BoardItem) else item.name

    def decide(
        item: BoardItem | ManualRequest,
    ) -> Reason | tuple[Launch, Capacity, str, tuple[str, ...], str]:
        """Return a `Reason` it must wait, or the `Launch` with its slot, repository, scope and issue."""
        if isinstance(item, BoardItem) and is_loop(item):
            if not facts.loop_enabled or facts.loop_killed:
                return Reason("loop_paused")
            if loop_running:
                return Reason("loop_running")
            slots = {slot.slot or f"entry-{i}" for i, slot in enumerate(free)}
            if owner_waiting and len(slots) <= 1:
                return Reason("owner_waiting")
        # A request of a project must not start while the project's old run is live or handing over: two
        # pods would work on one project, the new one from a workspace the old one has not published.
        if isinstance(item, ManualRequest) and item.name in project_runs:
            return Reason("project_running", project_runs[item.name])
        repo = _canonical_repo(item.repo, facts.repo_aliases)
        if not repo:
            return Reason("invalid_item")
        issue = resolve_issue(repo, item.node_id if isinstance(item, BoardItem) else item.issue)
        if isinstance(item, BoardItem) and item.type != "issue":
            return Reason("not_issue")
        if isinstance(item, BoardItem) and not item.open:
            return Reason("closed")
        if issue and (repo, issue) in running:
            return Reason("already_running", running[repo, issue])
        if issue and (repo, issue) in prs:
            return Reason("pr_open", prs[repo, issue])
        if isinstance(item, BoardItem) and item.linked_prs:
            return Reason("pr_open", item.linked_prs[0])
        try:
            files, source, literal = _predict_files(
                item, tracked.get(repo, set()), issue_bodies.get((repo, issue), "")
            )
        except ValueError:
            return Reason("invalid_files")
        # Tracked paths are files, not patterns: escape them so `docs/[x].md` is not a character class.
        scope = tuple(glob.escape(path) for path in files) if literal else files
        conflict = next(
            (who for other, paths, who in occupied if repo == other and _conflicts(scope, paths)), ""
        )
        if conflict:
            return Reason("file_conflict", conflict, _scope_detail(source))
        pin = facts.pins.get(identity_of(item), "")
        if pin and free and not any(slot.engine == pin for slot in free):
            return Reason("pin_unconfigured", pin)
        slot, route_reason = _choose_capacity(free, facts.now, pin or facts.author_engine, pin)
        if slot is None:
            return Reason("no_capacity", detail=route_reason)
        launch = Launch(item, slot.engine, slot.login, files, f"{source}; {route_reason}")
        return launch, slot, repo, scope, issue

    queued: list[BoardItem | ManualRequest] = [*facts.manual_requests, *items]
    admission = _gardening.Admission([_priority_of(entry) for entry in queued])
    for i in admission.order:
        entry = queued[i]
        if admission.held(i):
            waiting.append(Waiting(entry, Reason("low_priority_waits", behind=admission.ahead)))
            owner_waiting = True
            continue
        # One bad entry must wait with a reason; it must not stop the rest of the pass.
        try:
            decision = decide(entry)
        except (ValueError, TypeError, AttributeError, _github.GithubError) as exc:
            log.warning("planner: %s is invalid: %s: %s", identity_of(entry), type(exc).__name__, exc)
            decision = Reason("invalid_item")
        if isinstance(decision, Reason):
            if isinstance(entry, ManualRequest) or (
                entry.type == "issue" and entry.open and not is_loop(entry)
            ):
                owner_waiting = True
            waiting.append(Waiting(entry, decision))
            # Its own hold (lane, files, account...) keeps nothing from a low request it does not apply to.
            # With no slot left (`no_capacity` on an empty `free`) every request waits alike, and low
            # still waits behind it.
            if decision.code != "no_capacity" or free:
                admission.mark_unrunnable(i)
            continue
        launch, slot, repo, scope, issue = decision
        admission.admitted(i)
        launches.append(launch)
        if isinstance(entry, BoardItem) and is_loop(entry):
            loop_running = True
        # Every entry of the slot goes: one slot takes one launch, whichever engine it ran on.
        free[:] = [
            other for other in free if other is not slot and not (slot.slot and other.slot == slot.slot)
        ]
        occupied.append((repo, scope, identity_of(entry)))
        if issue:
            running[repo, issue] = identity_of(entry)
    return Plan(tuple(launches), tuple(waiting), unusable)


def _pull_identity(pr: Mapping[str, Any]) -> str:
    """Return a pull request's URL, else `owner/repo#N`.

    The same identity from a board link or a PR listing.
    """
    repo = pr.get("repo") or (pr.get("repository") or {}).get("nameWithOwner", "")
    identity: str = pr.get("url") or f"{repo}#{pr['number']}"
    return identity


def _manual_request(request: ManualRequest | Mapping[str, Any]) -> ManualRequest:
    if isinstance(request, ManualRequest):
        return request
    files = request.get("files")
    return ManualRequest(**{**request, "files": None if files is None else tuple(files)})


class _RunPb2(Protocol):
    @property
    def labels(self) -> Mapping[str, str]: ...


class RunListing(Protocol):
    """The part of a `flyte.remote.Run` that `facts_from` reads."""

    @property
    def name(self) -> str: ...
    @property
    def phase(self) -> object: ...
    @property
    def pb2(self) -> _RunPb2: ...


def facts_from(
    board_items: Iterable[BoardRow],
    runs: Iterable[RunListing],
    prs: Iterable[Mapping[str, Any]],
    capacity: Iterable[Capacity],
    manual: Iterable[ManualRequest | Mapping[str, Any]],
    *,
    tracked_paths: Mapping[str, set[str]] | None = None,
    repo_aliases: Mapping[str, str] | None = None,
    default_repo: str = "",
    now: float = 0,
) -> Facts:
    """Planner facts from what the live readers return; reads nothing and submits nothing.

    `board_items` is `BoardClient.items()`; `runs` are `flyte.remote.Run` listings (name, phase and
    protobuf labels); `prs` are open-PR dicts with `repo`, `number`, `changed_files` and optionally `url`.
    A run carries only the issue's hash (`factory-issue`), so its repository is the one of the board item
    that hash belongs to; a run no board item explains (a manual launch) is taken to be in `default_repo`.
    Its files are not known, so it occupies that whole repository.
    """
    items = tuple(
        BoardItem(
            node_id=row["node_id"],
            number=row["number"],
            repo=row["repo"],
            type=row["type"],
            open=row["open"],
            status=row["status"],
            position=row["position"],
            labels=tuple(row["labels"]),
            body=row["body"],
            linked_run=row["linked_run"],
            linked_prs=tuple(_pull_identity(pr) for pr in row["linked_prs"]),
        )
        for row in board_items
    )
    repo_of_label, issue_of_pull = {}, {}
    for item in items:
        if item.type != "issue":
            continue
        label = _issue_label(f"{item.repo}#{item.number}")
        if label:
            repo_of_label[label] = item.repo
        for pull in item.linked_prs:
            issue_of_pull[pull] = item.node_id
    active = []
    for run in runs:
        labels = dict(run.pb2.labels)
        issue = labels.get(LABEL_ISSUE, "")
        active.append(
            ActiveRun(
                name=run.name,
                repo=repo_of_label.get(issue, default_repo),
                issue=issue,
                generation=labels.get(LABEL_GEN, ""),
                phase=str(run.phase).split(".")[-1],
                labels=labels,
            )
        )
    open_prs = []
    for pr in prs:
        identity = _pull_identity(pr)
        issue = issue_of_pull.get(identity, "")
        open_prs.append(OpenPR(pr["repo"], tuple(pr["changed_files"]), issue=issue, identity=identity))
    unique_capacity: dict[tuple[str, str, str], Capacity] = {}
    for slot in capacity:
        unique_capacity.setdefault((slot.engine, slot.login, slot.slot), slot)
    return Facts(
        items=items,
        active_runs=tuple(active),
        open_prs=tuple(open_prs),
        # One Codex login serves every slot, so its capacity is one entry however many slots list it.
        capacity=tuple(unique_capacity.values()),
        manual_requests=tuple(_manual_request(m) for m in manual),
        tracked_paths=dict(tracked_paths or {}),
        repo_aliases=dict(repo_aliases or {}),
        now=now,
    )
