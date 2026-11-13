"""Project board state derived from GitHub evidence, including work without a Flyte run."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from . import _board, _github, _issues
from ._github import GithubApp, Json

if TYPE_CHECKING:
    from ._profile import Board, Profile

log = logging.getLogger(__name__)
#: The drift summary's commit-status context.
STATUS_CONTEXT = "factory/board-drift"
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
    now: datetime | None = None,
) -> list[str]:
    """Heal proven completion and log every mismatch.

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
    try:
        # `_reconcile` calls drift only for a profile with a [board].
        config = cast("Board", profile.board)
        client.bootstrap(config.owner, config.owner_type, config.number, create=False)
        items = [
            item
            for item in client.items()
            if item.get("type", "issue") == "issue" and item["repo"].lower() == repo
        ]
        extra = [
            option["name"]
            for option in (client.fields.get("Status") or {}).get("options", [])
            if option["name"] not in _board.STATUSES
        ]
        if extra:
            # Flag only: an option the factory never sets may still hold cards a human placed there.
            rows.append(f"board: Status options outside the factory lifecycle: {', '.join(extra)}")
            flagged.append(rows[-1])
        if not items:
            return rows
        issues = {issue["number"]: issue["state"] for issue in app.repo_issues(repo, max_pages=10)}
        commits = app.main_commits(repo)
        merged: dict[int, str] = {}
        for commit in commits:
            for number, whole in _commit_references(commit["commit"]["message"], repo).items():
                if whole:
                    merged.setdefault(number, commit["sha"])
        prs: set[int] = set()
        for pull in app.repo_pulls(repo):
            prs.update(references(f"{pull.get('title') or ''}\n{pull.get('body') or ''}", repo, plain=True))
        activity: dict[int, datetime] = {}
        branch_activity: dict[str, datetime] = {}
        names = _branches_to_read(items)
        if len({item.get("texts", {}).get("Branch") for item in items} - {None, ""}) > MAX_BRANCH_READS:
            log.warning("Board drift branch reads truncated at %s branches", MAX_BRANCH_READS)
        for branch in app.branch_heads(repo, names) if names else ():
            commit = branch["commit"]
            text = branch["name"]
            at = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
            branch_activity[branch["name"]] = at
            for number in references(text, repo, plain=True) | _commit_references(
                commit["message"], repo, plain=True
            ):
                activity[number] = max(activity.get(number, at), at)
        for item in items:
            number, status = item["number"], item["status"]
            ref = f"{repo}#{number}"
            # Beyond the REST cap the board listing still carries issue state. A heal always checks
            # it again by node ID, so the cap cannot strand a closed issue outside the first pages.
            opened = issues[number] == "open" if number in issues else item["open"]
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
            elif opened and number in merged:
                message = f"open issue merged on main ({merged[number]}); needs review"
            elif opened and status == "Done":
                message = "Done but issue open; needs review"
            elif opened and status == "In review" and number not in prs:
                message = "In review without open PR; needs review"
            elif opened and status == "In flight" and number not in prs:
                run = latest(ref)
                active = max(
                    (
                        at
                        for at in (
                            activity.get(number),
                            branch_activity.get(item.get("texts", {}).get("Branch", "")),
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
        return rows
    finally:
        client.close()
