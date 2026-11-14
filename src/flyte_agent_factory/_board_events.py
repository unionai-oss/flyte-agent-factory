"""Project board state derived from GitHub evidence, including work without a Flyte run.

`process` moves cards as GitHub events arrive. `drift` is the periodic audit and the board's one
reconciler: it reads the repository's facts once (issues, commits on main, open pull requests, branch
heads) and, for each card, either corrects its Status to the one the facts imply (`derive`) or flags what it
cannot decide. A card whose Status a correction has just moved is not flagged for the same thing.

`derive` is pure. Precedence, the first rule that applies wins:

1. A pull-request card: open in Backlog or Ready -> In review; otherwise left alone.
2. The issue is closed: Done (never moved out of Done).
3. An upstream PR merged and none open: Done (a closed PR resubmitted and merged counts).
4. The issue's whole reference is on main: left alone; `drift` flags it.
5. An open local PR references the issue, or a review round is live: In review.
6. A live run: In flight, only for a card that is behind it.
7. An upstream PR is open: Upstream.
8. Upstream PRs closed, none merged: Ready (a card in Backlog or Ready stays), and a note on the issue.
9. Done on an open issue whose newest run stopped without a pass: Ready.
10. In flight or In review with no evidence of life for `STALE`: Ready.
11. Queued in Backlog: Ready.

Otherwise None. Backlog and Ready stay the owner's: only rules 1, 2, 3, 7, 11 and the stale/stopped/
closed-upstream rules move a card to or from them. Evidence of life is the newest of a branch commit, the
newest run's end and the Status's own last change, so a card the supervisor has just set is never stale.

A card whose Status a person set within `HAND_SET` is held, and the correction is logged instead. GitHub
reports who set a value, but the board PAT may be a person's own, so the factory's writes look like
hand moves too: held for a day after any move, never wrongly overwritten. That is deliberate. The one
exception is a closed issue: moving its card to Done is never wrong, so the guard does not apply.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from . import _board, _github, _issues
from ._errors import REMOTE_ERRORS
from ._github import GithubApp, Json

if TYPE_CHECKING:
    from ._profile import Board, Profile

log = logging.getLogger(__name__)
#: The drift summary's commit-status context.
STATUS_CONTEXT = "factory/board-drift"
#: In flight / In review with no sign of life for this long is Ready.
STALE = timedelta(days=2)
#: A Status a person set this recently is left where they put it.
HAND_SET = timedelta(hours=24)
#: Statuses owned by the owner: the factory only leaves them for a PR card or a queued launch.
_OWNERS = ("Backlog", "Ready")
# Owner, repository and number follow `_board._ISSUE`, which parses what this finds: text that is not
# GitHub syntax (`my_org/x#1`, `example.com/x#1`) is not a link, rather than a link that cannot be read.
_UPSTREAM = re.compile(
    r"https://github\.com/(?P<o1>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<r1>[A-Za-z0-9._-]+)/pull/(?P<n1>[1-9][0-9]*)"
    r"|(?<![\w/.-])(?P<o2>[A-Za-z0-9][A-Za-z0-9-]*)/(?P<r2>[A-Za-z0-9._-]+)#(?P<n2>[1-9][0-9]*)"
)
_UPSTREAM_LINE = re.compile(r"^\s*(?:[-*]\s+)?upstream:(.*)$", re.IGNORECASE)
# GitHub's closing syntax does not include our board/chunk convention. One expression keeps qualified
# references intact, so a foreign repository's #N can never become a local reference on a second pass.
_REFERENCE = re.compile(
    r"(?:(?<![\w])(?P<keyword>close[sd]?|fix(?:e[sd])?|resolve[sd]?|board)\s+)?"
    r"(?P<ref>https://github\.com/[\w.-]+/[\w.-]+/issues/[1-9]\d*|"
    r"(?<![\w#])(?:[\w.-]+/[\w.-]+)?#[1-9]\d*)"
    r"(?P<chunk>\s+chunk\b)?",
    re.IGNORECASE,
)


def references(text: str, repo: str, *, plain: bool = False) -> dict[int, bool]:
    """Local issue numbers mapped to whole-item (True) or partial (False) references."""
    found: dict[int, bool] = {}
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    for match in _REFERENCE.finditer(text):
        keyword, chunk = match.group("keyword", "chunk")
        if not (keyword or chunk or plain):
            continue
        ref = match["ref"]
        if ref.lower().startswith("https://github.com/"):
            target, number = ref[len("https://github.com/") :].split("/issues/")
        else:
            target, number = ref.split("#")
        if target and target.lower() != repo.lower():
            continue
        found[int(number)] = found.get(int(number), False) or bool(keyword and not chunk)
    return found


def repository(profile: Profile) -> str:
    """Return the board's repository: the workspace (`[repo] project`), whatever repos projects target."""
    try:
        return _github.parse_repo(profile.repo).lower()
    except _github.GithubError:
        return ""


def eligible(profile: Profile, kind: str, event: Json) -> bool:
    if not profile.board or kind not in ("push", "create", "pull_request", "issues"):
        return False
    repo = (event.get("repository") or {}).get("full_name", "")
    target = _issues.repo_targets(profile).get(repo.lower(), ("", ""))[1]
    return bool(repository(profile) and (target or repo).lower() == repository(profile))


def _commit_references(message: str, repo: str, *, plain: bool = False) -> dict[int, bool]:
    subject = message.split("\n", 1)[0]
    return {} if subject.startswith("Revert") else references(subject, repo, plain=plain)


def _move(
    client: _board.BoardClient, current: _board.BoardRow, status: str, *, unmerged: bool = False
) -> bool:
    """Move the card forward, never out of Done. `current` was read inside `_board.WRITE_LOCK`."""
    before = current["status"]
    ranks = {"": 0, **{name: rank for rank, name in enumerate(_board.STATUSES)}}
    rollback = unmerged and before == "In review" and status == "In flight"
    if before != "Done" and (ranks.get(before, -1) < ranks[status] or rollback):
        client.set_status(current["item_id"], status)
        return True
    return False


def _is_open(app: GithubApp | None, current: _board.BoardRow, repo: str, number: int) -> bool:
    return app.issue(repo, number)["state"] == "open" if app else current.get("open", True)


def _complete(
    client: _board.BoardClient,
    current: _board.BoardRow,
    app: GithubApp | None,
    repo: str,
    number: int,
    sha: str,
) -> None:
    if app is None:
        _move(client, current, "Done")
        log.warning(
            "Board %s#%s: completed on default branch; issue could not be closed without GitHub App",
            repo,
            number,
        )
        return
    issue = f"{repo}#{number}"
    # A commit completes an issue once. Its marker says this one did, so a delayed or repeated delivery
    # after a reopen is final. The marker goes last: a failed close or move is retried by the same push.
    # `post_milestone` checks the marker too, but it posts as it checks, which is too early here.
    tag = _issues.marker(issue, sha, "board-completed")
    if any(tag in (c.get("body") or "") for c in app.issue_comments_since(repo, number)):
        return
    # GitHub may already have closed a keyword-linked issue before delivering the push.
    if app.issue(repo, number)["state"] == "open":
        # A first delivery can arrive after a human reopen, before we have ever posted a marker.
        reopens = [e["created_at"] for e in app.issue_events(repo, number) if e["event"] == "reopened"]
        if reopens:
            committed_at = app.commit(repo, sha)["commit"]["committer"]["date"]
            committed = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            if any(datetime.fromisoformat(at.replace("Z", "+00:00")) > committed for at in reopens):
                return
        app.close_issue(repo, number)
    _move(client, current, "Done")
    _issues.post_milestone(
        app, issue, sha, "board-completed", f"Completed in https://github.com/{repo}/commit/{sha}"
    )


def process(profile: Profile, token: str, app: GithubApp | None, kind: str, event: Json) -> int:
    if not eligible(profile, kind, event):
        return 0
    repo = repository(profile)
    client = _board.BoardClient(token)
    try:
        return _process(profile, client, app, kind, event, repo)
    finally:
        client.close()


def _process(
    profile: Profile,
    client: _board.BoardClient,
    app: GithubApp | None,
    kind: str,
    event: Json,
    repo: str,
) -> int:
    moves: dict[int, tuple[str, str]] = {}
    branch = ""
    unmerged = False
    if kind == "push" and not event.get("deleted") and event.get("ref", "").startswith("refs/heads/"):
        default = app.default_branch(repo) if app else client.default_branch_sha(repo)[0]
        main = event.get("ref") == f"refs/heads/{default}"
        head = event.get("after", "")
        if main:
            if event.get("forced") or not head:
                return 0
            reachable = (
                app.commit_is_on_default_branch(repo, head)
                if app
                else client.default_branch_sha(repo)[1] == head
            )
            if not reachable:
                return 0
        commits = event.get("commits", [])
        for commit in commits:
            for number, whole in _commit_references(commit.get("message", ""), repo).items():
                status = "Done" if main and whole else "In flight"
                if moves.get(number, ("", ""))[0] != "Done":
                    moves[number] = status, commit.get("id", "")
        if not main:
            branch = event.get("ref", "").removeprefix("refs/heads/")
            for number in references(event.get("ref", ""), repo, plain=True):
                moves.setdefault(number, ("In flight", ""))
    elif kind == "create" and event.get("ref_type") == "branch":
        branch = event.get("ref", "")
        moves = {n: ("In flight", "") for n in references(branch, repo, plain=True)}
    elif kind == "pull_request":
        pull = event.get("pull_request") or {}
        text = "\n".join([pull.get("title") or "", pull.get("body") or ""])
        branch = (pull.get("head") or {}).get("ref", "")
        refs = references(text, repo, plain=True)
        action = event.get("action")
        if action in ("opened", "ready_for_review", "reopened"):
            moves = {n: ("In review", "") for n in refs}
        elif action == "closed" and pull.get("merged"):
            default = app.default_branch(repo) if app else client.default_branch_sha(repo)[0]
            status = "Done" if (pull.get("base") or {}).get("ref") == default else "In review"
            moves = {n: (status, "") for n, whole in refs.items() if whole}
        elif action == "closed":
            unmerged = True
            moves = {n: ("In flight", "") for n in refs}
    elif kind == "issues" and event.get("action") == "closed":
        moves = {event["issue"]["number"]: ("Done", "")}
    if not moves:
        return 0
    # `eligible` returned True, which requires a [board].
    config = cast("Board", profile.board)
    client.bootstrap(config.owner, config.owner_type, config.number, create=False)
    items = client.items()
    for number, (status, sha) in moves.items():
        # Deliveries overlap and the listing above may predate another one's write: the card is read
        # again under the process lock. Writers in other pods can still race; drift heals closed issues.
        with _board.WRITE_LOCK:
            item = _board._ensure_item(client, items, f"{repo}#{number}")
            current = _board.current_item(client, item)
            if current is None:
                continue
            if kind == "issues" and _is_open(app, current, repo, number):
                # The delivery may be old: only an issue that is closed now completes its card.
                continue
            if branch and current["status"] != "Done" and current.get("texts", {}).get("Branch") != branch:
                client.set_text(current["item_id"], "Branch", branch)
            if status == "Done" and kind == "push":
                _complete(client, current, app, repo, number, sha)
            else:
                _move(client, current, status, unmerged=unmerged)
    return len(moves)


@dataclasses.dataclass(frozen=True)
class Facts:
    """What is true of one card right now. The defaults are a card with no evidence at all."""

    issue_open: bool = True
    pr_card: bool = False
    queued: bool = False
    live: bool = False  # the newest run is live
    review_live: bool = False  # ... and in a review round (or has its pull request)
    stopped: bool = False  # the newest run ended without a pass
    open_pr: bool = False  # a local open PR references the issue
    merged: bool = False  # the whole issue is referenced by a commit on main
    active_at: datetime | None = None  # newest sign of life
    #: Upstream pull requests the issue names: `(owner/repo#N, OPEN | MERGED | CLOSED)`.
    upstream: tuple[tuple[str, str], ...] = ()

    @property
    def upstream_states(self) -> set[str]:
        return {state for _, state in self.upstream}


def upstream_links(text: str, own: Collection[str]) -> list[str]:
    """`owner/repo#N` (lower case) of each `Upstream:` line's pull requests, minus ones in repos `own`.

    A line `Upstream: https://github.com/o/r/pull/N` or `Upstream: o/r#N`, key in any case. Quoted
    (`>`) lines are replies, not the author's links. A link into a repository the factory controls is not
    upstream: it is the factory's own work.
    """
    found: dict[str, None] = {}
    for line in text.splitlines():
        match = _UPSTREAM_LINE.match(line)
        if not match:
            continue
        for link in _UPSTREAM.finditer(match[1]):
            owner, repo, number = (
                (link["o1"], link["r1"], link["n1"]) if link["o1"] else link.group("o2", "r2", "n2")
            )
            if f"{owner}/{repo}".lower() not in own:
                found[f"{owner}/{repo}#{number}".lower()] = None
    return list(found)


def derive(facts: Facts, current: str, now: datetime) -> tuple[str | None, str]:
    """Return the Status `facts` imply for a card now at `current` and why; (None, "") to leave it."""
    rank = _board._rank
    states = facts.upstream_states
    if facts.pr_card:
        if facts.issue_open and current in _OWNERS:
            return "In review", "open pull request card"
        return None, ""
    if not facts.issue_open:
        return (None, "") if current == "Done" else ("Done", "issue is closed")
    if "MERGED" in states and "OPEN" not in states:
        return (None, "") if current == "Done" else ("Done", "upstream pull request merged")
    if facts.merged:
        return None, ""
    if facts.open_pr or facts.review_live:
        reason = "open pull request" if facts.open_pr else "review round live"
        return (None, "") if current == "In review" else ("In review", reason)
    if facts.live:
        behind = rank(current) < rank("In flight")
        return ("In flight", "run is live") if behind else (None, "")
    if "OPEN" in states:
        return (None, "") if current == "Upstream" else ("Upstream", "upstream pull request open")
    if states == {"CLOSED"}:
        if current in _OWNERS:
            return None, ""
        return "Ready", "upstream pull request closed unmerged"
    if current == "Done" and facts.stopped:
        return "Ready", "run stopped without delivering"
    if current in ("In flight", "In review") and not facts.queued:
        if facts.active_at is None or now - facts.active_at >= STALE:
            return "Ready", f"no live run, pull request or activity for {STALE.days} days"
    if current == "Backlog" and facts.queued:
        return "Ready", "launch is queued"
    return None, ""


def _own_repos(profile: Profile) -> set[str]:
    """Repositories the factory controls: every `[repo]` entry, the workspace and the deploy repository."""
    own = set()
    for url in (*profile.repos.values(), profile.repo, profile.deploy.repo):
        try:
            own.add(_github.parse_repo(url).lower())
        except _github.GithubError:
            pass  # not a GitHub remote: no GitHub link can name it
    return own


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _held(item: _board.BoardRow, now: datetime) -> timedelta | None:
    """How long ago a person set this card's Status, if within `HAND_SET`."""
    at, by = item.get("status_at", ""), item.get("status_by", "")
    if not at or not by or by == "Bot":
        return None
    age = now - _time(at)
    return age if age < HAND_SET else None


def _age(age: timedelta) -> str:
    return f"{int(age.total_seconds() // 3600)}h"


def _upstream_note(upstream: str) -> str:
    return f"Upstream pull request {upstream} was closed without merging, so this moved back to Ready."


@dataclasses.dataclass
class _World:
    """What one drift pass reads once for every card, and the handles it reads the rest with."""

    app: GithubApp
    client: _board.BoardClient
    own: set[str]
    newest: Callable[[str], tuple[bool, datetime | None, Any] | None]
    queued: Collection[str]
    now: datetime
    issues: dict[int, str] = dataclasses.field(default_factory=dict)  # REST state of each listed issue
    merged: dict[int, str] = dataclasses.field(default_factory=dict)  # issues wholly on main -> commit
    pulled: set[int] = dataclasses.field(default_factory=set)  # issues an open local PR references
    activity: dict[int, datetime] = dataclasses.field(default_factory=dict)  # newest branch commit naming it
    branches: dict[str, datetime] = dataclasses.field(default_factory=dict)  # newest commit per branch
    upstream: dict[str, str] = dataclasses.field(default_factory=dict)  # link -> state, read once per pass


def _facts(world: _World, item: _board.BoardRow, ref: str) -> Facts:
    number = item["number"]
    if item.get("type", "issue") == "pr":
        return Facts(pr_card=True, issue_open=item["open"])
    # Beyond the REST cap the listing still carries issue state.
    opened = world.issues[number] == "open" if number in world.issues else item["open"]
    seen = [
        at
        for at in (world.branches.get(item.get("texts", {}).get("Branch", "")), world.activity.get(number))
        if at
    ]
    if item.get("status_at"):
        seen.append(_time(item["status_at"]))
    live = review_live = stopped = False
    found = world.newest(ref) if opened else None
    if found:
        live, ended, snapshot = found
        status = _board.desired(live, "", snapshot).get("Status", "")
        review_live = live and status == "In review"
        # No report (None) says nothing about whether the run delivered.
        # A pass whose pull request merged without closing the issue still delivered: ask the report.
        stopped = not live and snapshot is not None and not _board.run_passed(snapshot)
        if ended:
            seen.append(ended)
    upstream: tuple[tuple[str, str], ...] = ()
    if opened:
        repo, _ = ref.rsplit("#", 1)
        texts = [item.get("body", "")]
        texts += [c.get("body") or "" for c in world.app.issue_comments_since(repo, number)]
        for link in dict.fromkeys(found for text in texts for found in upstream_links(text, world.own)):
            if link not in world.upstream:
                world.upstream[link] = world.client.pull_request_state(link)
            if world.upstream[link]:
                upstream += ((link, world.upstream[link]),)
    return Facts(
        issue_open=opened,
        queued=ref.lower() in world.queued,
        live=live,
        review_live=review_live,
        stopped=stopped,
        open_pr=number in world.pulled or bool(item.get("linked_prs")),
        merged=number in world.merged,
        active_at=max(seen, default=None),
        upstream=upstream,
    )


def _correct(world: _World, item: _board.BoardRow, ref: str, facts: Facts) -> str:
    """Write the card's derived Status if it differs; the log row, "" when nothing moved."""
    if derive(facts, item["status"], world.now)[0] is None:
        return ""
    with _board.WRITE_LOCK:
        # The listing may predate another writer (a merge closing the issue): decide again on the card now.
        current = _board.current_item(world.client, item)
        if current is None:
            return ""
        facts = dataclasses.replace(facts, issue_open=current["open"])
        want, reason = derive(facts, current["status"], world.now)
        if want is None:
            return ""
        before = current["status"]
        # A closed issue's Done is never wrong, and the PAT makes the factory's own writes look hand-set.
        held = _held(current, world.now) if current["open"] else None
        if held is not None:
            log.warning(
                "Board truth %s: held %s (set by hand %s ago); would move to %s: %s",
                ref,
                before,
                _age(held),
                want,
                reason,
            )
            return ""
        if facts.upstream_states == {"CLOSED"} and want == "Ready":
            # The note first: its marker makes a retry after a failed write post nothing twice.
            for link, _ in facts.upstream:
                _issues.post_milestone(world.app, ref, link, "upstream-closed", _upstream_note(link))
        world.client.set_status(current["item_id"], want)
    row = f"{ref}: {before} -> {want}: {reason}"
    log.warning("Board truth %s", row)
    return row


def is_correction(row: str) -> bool:
    """Whether a `drift` row is a Status correction (`ref: from -> to: reason`), not a flag or a heal."""
    return " -> " in row


#: Branch heads read per drift pass, so a board of many named branches costs a bounded number of calls.
MAX_BRANCH_READS = 100


def _flagged_digest(flagged: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(flagged)).encode()).hexdigest()[:16] if flagged else ""


#: How many recent default-branch commits are searched for the last drift status.
DIGEST_LOOKBACK = 20


def _published_digest(app: GithubApp, repo: str, commits: list[Json]) -> str:
    """Return the flagged-rows digest of the newest drift status on recent main commits, "" if none.

    Searched across commits, newest first, so main advancing past an unchanged flagged set does not
    publish it again; bounded at DIGEST_LOOKBACK status reads.
    """
    for commit in commits[:DIGEST_LOOKBACK]:
        for status in app.commit_statuses(repo, commit["sha"]):
            if status["context"] == STATUS_CONTEXT:
                digest: str = (status.get("target_url") or "").partition("#flagged-")[2]
                return digest
    return ""


def _branches_to_read(items: Iterable[_board.BoardRow]) -> list[str]:
    """Distinct branches named by cards' Branch field, in-flight cards first, at most MAX_BRANCH_READS."""
    ordered = sorted(items, key=lambda item: item["status"] != "In flight")
    names = dict.fromkeys(item.get("texts", {}).get("Branch", "") for item in ordered)
    return [name for name in names if name][:MAX_BRANCH_READS]


def drift(
    profile: Profile,
    token: str,
    app: GithubApp,
    latest: Callable[[str], Any],
    *,
    newest: Callable[[str], tuple[bool, datetime | None, Any] | None] | None = None,
    queued: Collection[str] = (),
    now: datetime | None = None,
) -> list[str]:
    """Correct each card's Status from the facts, heal proven completion and log every mismatch.

    `newest(ref)` is `(live, ended_at, snapshot)` of the issue's newest project run however long ago it
    ended, or None; `queued` the lower-case `owner/repo#N` of issues with a pending launch request. Without
    `newest` there is no corrections pass and drift only heals and flags. `latest` is the run the card
    shows now, for the in-flight audit.

    A card a correction moved is not flagged for the same thing; what derive leaves alone, and a correction
    held by the hand-set guard, is still flagged. A closed issue's card is healed to Done whoever set it.
    One card's failed read does not stop the others: the first
    error is raised once all are tried.

    A summary status goes on main's head only when the set of flagged rows differs from the one its last
    status carries. The status event reaches the webhook and wakes reconcile, so a status on every pass
    would be a loop (the webhook also ignores this context).
    """
    repo = repository(profile)
    if not repo:
        return []
    now = now or datetime.now(timezone.utc)
    client = _board.BoardClient(token)
    rows: list[str] = []
    flagged: list[str] = []
    failed: list[Exception] = []
    try:
        # `_reconcile` calls drift only for a profile with a [board].
        config = cast("Board", profile.board)
        client.bootstrap(config.owner, config.owner_type, config.number, create=False)
        cards = [
            item for item in client.items() if item["repo"].lower() == repo and item.get("type") != "draft"
        ]
        items = [item for item in cards if item.get("type", "issue") == "issue"]
        extra = [
            option["name"]
            for option in (client.fields.get("Status") or {}).get("options", [])
            if option["name"] not in _board.STATUSES
        ]
        if extra:
            # Flag only: an option the factory never sets may still hold cards a human placed there.
            rows.append(f"board: Status options outside the factory lifecycle: {', '.join(extra)}")
            flagged.append(rows[-1])
        if not cards:
            return rows
        world = _World(app, client, _own_repos(profile), newest or (lambda ref: None), queued, now)
        world.issues = {issue["number"]: issue["state"] for issue in app.repo_issues(repo, max_pages=10)}
        commits = app.main_commits(repo)
        for commit in commits:
            for number, whole in _commit_references(commit["commit"]["message"], repo).items():
                if whole:
                    world.merged.setdefault(number, commit["sha"])
        for pull in app.repo_pulls(repo):
            world.pulled.update(
                references(f"{pull.get('title') or ''}\n{pull.get('body') or ''}", repo, plain=True)
            )
        names = _branches_to_read(items)
        if len({item.get("texts", {}).get("Branch") for item in items} - {None, ""}) > MAX_BRANCH_READS:
            log.warning("Board drift branch reads truncated at %s branches", MAX_BRANCH_READS)
        for branch in app.branch_heads(repo, names) if names else ():
            commit = branch["commit"]
            at = _time(commit["committer"]["date"])
            world.branches[branch["name"]] = at
            for number in references(branch["name"], repo, plain=True) | _commit_references(
                commit["message"], repo, plain=True
            ):
                world.activity[number] = max(world.activity.get(number, at), at)
        for item in cards:
            number, status = item["number"], item["status"]
            ref = f"{repo}#{number}"
            if newest is not None:
                try:
                    row = _correct(world, item, ref, _facts(world, item, ref))
                except REMOTE_ERRORS as exc:
                    log.warning("Board truth %s: could not be checked: %s", ref, type(exc).__name__)
                    failed.append(exc)
                else:
                    if row:
                        # Corrected: the flags below would only restate what the correction fixed.
                        rows.append(row)
                        continue
            if item.get("type", "issue") != "issue":
                continue
            # Beyond the REST cap the board listing still carries issue state. A heal always checks
            # it again by node ID, so the cap cannot strand a closed issue outside the first pages.
            opened = world.issues[number] == "open" if number in world.issues else item["open"]
            if not opened and status == "Done":
                continue
            message = ""
            if not opened and status != "Done":
                with _board.WRITE_LOCK:
                    current = _board.current_item(client, item)
                    # The node read includes the issue state, so a reopen since the bulk REST listing
                    # wins without an extra REST call per card. Both checks share the process lock.
                    if current is not None and not current["open"] and _move(client, current, "Done"):
                        message = "closed but card not Done; healed"
            elif opened and number in world.merged:
                message = f"open issue merged on main ({world.merged[number]}); needs review"
            elif opened and status == "Done":
                message = "Done but issue open; needs review"
            elif opened and status == "In review" and number not in world.pulled:
                message = "In review without open PR; needs review"
            elif opened and status == "In flight" and number not in world.pulled:
                run = latest(ref)
                active = max(
                    (
                        at
                        for at in (
                            world.activity.get(number),
                            world.branches.get(item.get("texts", {}).get("Branch", "")),
                        )
                        if at is not None
                    ),
                    default=None,
                )
                if not (run and run[0]) and (active is None or now - active >= timedelta(days=7)):
                    message = (
                        "In flight with no activity for 7 days "
                        "(no live run, open PR or branch activity); needs review"
                    )
            if message:
                rows.append(f"{ref}: {message}")
                if "needs review" in message:
                    flagged.append(rows[-1])
                log.warning("Board drift %s", rows[-1])
        summary = f"{len(items)} cards checked; {len(rows) - len(flagged)} healed; {len(flagged)} need review"
        log.info("Board drift: %s", summary)
        digest = _flagged_digest(flagged)
        if commits and digest != _published_digest(app, repo, commits):
            # Commit health gates deploys on any failure; drift needs attention without blocking fixes.
            # The digest rides in the link, since the description is for people.
            app.set_status(
                repo,
                commits[0]["sha"],
                STATUS_CONTEXT,
                "success",
                summary,
                f"https://github.com/{repo}/commit/{commits[0]['sha']}#flagged-{digest}",
            )
        if failed:
            raise failed[0]
        return rows
    finally:
        client.close()
