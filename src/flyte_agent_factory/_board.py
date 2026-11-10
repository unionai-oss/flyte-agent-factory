"""Projects v2 only. The supervisor keeps this credential out of child environments.

An item's Status, Flyte, Branch and Engine are a projection of its issue's newest project run
(`desired`): the supervisor writes them as the run moves, and `factory-reconcile` (`Reconciler`)
recomputes them from Flyte every five minutes and writes what differs. Nothing is queued in storage,
so a write that fails is either retried from memory or recomputed on the next tick.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, cast

import httpx

from . import _github, _pull_requests
from ._errors import REMOTE_ERRORS

if TYPE_CHECKING:
    from ._profile import Board, Profile

#: A decoded GraphQL document. GitHub's shape is not ours to pin down, so its values stay `Any`.
GraphQL = dict[str, Any]


class BoardRow(TypedDict, total=False):
    """One card as `BoardClient.items` lists it.

    Not total: a card `_ensure_item` adds on the spot has only the first five keys until the next listing.
    """

    item_id: str
    repo: str
    number: int
    status: str
    texts: dict[str, str]
    node_id: str
    type: str
    open: bool
    body: str
    position: int
    linked_prs: list[GraphQL]
    linked_run: str
    title: str
    labels: list[str]


TOKEN_ENV = "GITHUB_BOARD_PAT"
SELECTS = {
    "Status": ("Backlog", "Ready", "In flight", "In review", "Done"),
    "Size": ("S", "M", "L"),
    # Not "Type": GitHub reserves that name for issue types and refuses to create it.
    "Kind": ("feature", "bug", "refactor", "docs", "research"),
}
TEXT_FIELDS = ("Flyte", "Branch", "Engine")
#: The lifecycle the factory drives, in order: the one list of board statuses. Everything that sets,
#: ranks or audits a status reads it here, so a column the factory never sets cannot creep back.
STATUSES = SELECTS["Status"]
_FIELD = (
    "... on ProjectV2Field {id name dataType} "
    "... on ProjectV2SingleSelectField {id name options {id name color description}}"
)
#: Each text field's value on an item, aliased by its name: the reconciler writes only what differs.
_TEXT_VALUES = "".join(
    f'{field}: fieldValueByName(name:"{field}") {{... on ProjectV2ItemFieldTextValue {{text}}}} '
    for field in TEXT_FIELDS
)
#: GraphQL error types that a retry can outlive: access the owner can grant, and rate limits.
_RETRYABLE_TYPES = {"FORBIDDEN": 403, "INSUFFICIENT_SCOPES": 403, "RATE_LIMITED": 429}
#: The request itself is wrong. Typeless errors with no validation code (GitHub's "Something went
#: wrong", often a timeout) and any other type are treated as a server error and retried.
_INVALID_TYPES = {"NOT_FOUND", "UNPROCESSABLE"}
#: Statuses that need the owner rather than time: an unreadable board, a refused or unauthorised token.
_OWNER_STATUSES = (400, 401, 403, 404)
_ISSUE = re.compile(r"(?:([A-Za-z0-9][A-Za-z0-9-]*)/)?([A-Za-z0-9._-]+)#([1-9][0-9]*)")


def credential_env_vars(secret: str, env: Mapping[str, str]) -> tuple[str, ...]:
    return (TOKEN_ENV, "_UNION_GITHUB_BOARD_PAT", _pull_requests.prefixed_key_env_var(secret, env))


def take_token(secret: str = "") -> str:
    # A secret pasted with its newline would otherwise reach the Authorization header, and httpx's
    # error for an invalid header value quotes the value.
    values = [os.environ.pop(key, "").strip() for key in credential_env_vars(secret, os.environ)]
    return next((value for value in values if value), "")


def normalise_issue(issue: str, repos: Iterable[str]) -> str:
    """`owner/repo#N` in lower case, which is how GitHub compares names.

    `repo#N` is the short form board launches use, and names one of `repos`, the profile's repository
    URLs.
    """
    match = _ISSUE.fullmatch(issue.strip())
    if not match:
        raise ValueError(f"board issue {issue!r} must be owner/repo#N or repo#N")
    owner, name, number = match.groups()
    if owner:
        return f"{owner}/{name}#{number}".lower()
    known = set()
    for url in repos:
        with contextlib.suppress(_github.GithubError):  # a non-GitHub remote cannot hold board issues
            known.add(_github.parse_repo(url).lower())
    matches = [slug for slug in known if slug.split("/", 1)[1] == name.lower()]
    if len(matches) != 1:
        raise ValueError(
            f"board issue {issue!r}: {name} is not exactly one of this profile's repositories; "
            "use owner/repo#N"
        )
    return f"{matches[0]}#{number}"


def _graphql_status(errors: list[GraphQL]) -> int:
    """Return the HTTP status a GraphQL error list stands for.

    Any retryable error makes the request retryable.
    """
    statuses = set()
    for error in errors:
        kind = error.get("type")
        if kind in _RETRYABLE_TYPES:
            statuses.add(_RETRYABLE_TYPES[kind])
        elif kind in _INVALID_TYPES or (not kind and (error.get("extensions") or {}).get("code")):
            statuses.add(400)
        else:
            statuses.add(502)
    return next((status for status in (403, 429, 502) if status in statuses), 400)


def _rate_limited(response: httpx.Response) -> bool:
    return bool(
        response.headers.get("retry-after")
        or response.headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in response.text.lower()
    )


def _emit(icon: str, text: str) -> None:
    logging.getLogger(__name__).warning("%s %s", icon, text)


class BoardError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _graphql_error(errors: list[GraphQL]) -> BoardError:
    # Error messages can echo supplied content. Never log the server's payload.
    return BoardError("board GraphQL request failed", _graphql_status(errors))


class BoardClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
            transport=transport,
        )
        self.project_id = ""
        self.fields: dict[str, GraphQL] = {}
        self.expires: date | None = None

    def close(self) -> None:
        self._http.close()

    def _query(self, query: str, **variables: Any) -> GraphQL:
        body = self._post(query, variables)
        if body.get("errors"):
            raise _graphql_error(body["errors"])
        data: GraphQL = body["data"]
        return data

    def _post(self, query: str, variables: GraphQL) -> GraphQL:
        """Return the decoded response body, errors included: only a failed HTTP exchange raises here."""
        response = self._http.post("/graphql", json={"query": query, "variables": variables})
        expiry = response.headers.get("github-authentication-token-expiration", "")
        if expiry:
            try:
                self.expires = date.fromisoformat(expiry[:10])
            except ValueError:
                pass
        if response.status_code != 200:
            status = response.status_code
            if status == 403 and _rate_limited(response):  # GitHub's secondary rate limit is a 403
                status = 429
            raise BoardError(f"board HTTP {status}", status)
        try:
            body: GraphQL = response.json()
        except ValueError as exc:
            raise BoardError("board response is not JSON", 502) from exc
        return body

    def bootstrap(self, owner: str, owner_type: str, number: int, *, create: bool = True) -> None:
        if owner_type not in ("user", "org"):
            raise ValueError("owner_type must be user or org")
        root = "user" if owner_type == "user" else "organization"
        query = (
            "query ResolveProject($owner:String!,$number:Int!,$after:String) {"
            + root
            + "(login:$owner) {projectV2(number:$number) {id fields(first:100,after:$after) {"
            "nodes {" + _FIELD + "} pageInfo {hasNextPage endCursor}}}}}"
        )
        after = None
        while True:
            owner_data = self._query(query, owner=owner, number=number, after=after)[root]
            project = owner_data and owner_data.get("projectV2")
            if not project:
                raise BoardError("board project is not visible", 404)
            self.project_id = project["id"]
            for field in project["fields"]["nodes"]:
                if field and "name" in field:
                    self.fields[field["name"]] = field
            page = project["fields"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
        if not create:
            return
        for name in (*SELECTS, *TEXT_FIELDS):
            options = SELECTS.get(name, ())
            existing = self.fields.get(name)
            if existing:
                if options and "options" not in existing:
                    raise BoardError(f"board field {name} must be single-select", 400)
                if not options and ("options" in existing or existing.get("dataType", "TEXT") != "TEXT"):
                    raise BoardError(f"board field {name} must be text", 400)
                missing = [
                    opt for opt in options if opt not in {o["name"] for o in existing.get("options", [])}
                ]
                if missing:
                    # The update API replaces the whole option list; preserve IDs and custom options.
                    updated = [dict(o) for o in existing["options"]]
                    updated += [{"name": opt, "color": "GRAY", "description": ""} for opt in missing]
                    result = self._query(
                        "mutation UpdateField($input:UpdateProjectV2FieldInput!) {"
                        "updateProjectV2Field(input:$input)"
                        " {projectV2Field {" + _FIELD + "}}}",
                        input={"fieldId": existing["id"], "singleSelectOptions": updated},
                    )
                    self.fields[name] = result["updateProjectV2Field"]["projectV2Field"]
                continue
            value: GraphQL = {
                "projectId": self.project_id,
                "name": name,
                "dataType": "SINGLE_SELECT" if options else "TEXT",
            }
            if options:
                value["singleSelectOptions"] = [
                    {"name": opt, "color": "GRAY", "description": ""} for opt in options
                ]
            result = self._query(
                "mutation CreateField($input:CreateProjectV2FieldInput!) {createProjectV2Field(input:$input)"
                " {projectV2Field {" + _FIELD + "}}}",
                input=value,
            )
            self.fields[name] = result["createProjectV2Field"]["projectV2Field"]

    def issue_node_id(self, issue: str) -> str:
        """Return the GraphQL node id of `owner/repo#N`, which `add_item` needs.

        The board PAT reads the repo.
        """
        # A malformed `issue` raises AttributeError here (callers pass `normalise_issue` output).
        owner, name, number = _ISSUE.fullmatch(issue).groups()  # type: ignore[union-attr]  # see above
        body = self._post(
            "query IssueNode($owner:String!,$name:String!,$number:Int!) {"
            "repository(owner:$owner,name:$name) {issue(number:$number) {id}}}",
            {"owner": owner, "name": name, "number": int(number)},
        )
        # A repository the token cannot read is a NOT_FOUND error with `repository: null`, not an empty
        # result. Any other error is not about visibility, so it keeps its usual classification.
        errors = body.get("errors") or []
        if any(error.get("type") != "NOT_FOUND" for error in errors):
            raise _graphql_error(errors)
        found = (((body.get("data") or {}).get("repository") or {}).get("issue") or {}).get("id")
        if not found:
            raise BoardError(f"issue {issue} is not visible to the board token", 404)
        node_id: str = found
        return node_id

    def add_item(self, content_node_id: str) -> str:
        data = self._query(
            "mutation AddItem($input:AddProjectV2ItemByIdInput!) {"
            "addProjectV2ItemById(input:$input) {item {id}}}",
            input={"projectId": self.project_id, "contentId": content_node_id},
        )
        item_id: str = data["addProjectV2ItemById"]["item"]["id"]
        return item_id

    def missing_fields(self) -> list[str]:
        """Fields (or Status/Size/Kind options) the factory needs that the board no longer has."""
        missing = []
        for name in (*SELECTS, *TEXT_FIELDS):
            field = self.fields.get(name)
            options = {o["name"] for o in (field or {}).get("options", [])}
            if field is None or not set(SELECTS.get(name, ())) <= options:
                missing.append(name)
        return missing

    def _set(self, item: str, field: str, value: GraphQL) -> None:
        # 409, not 400: retried, and the next tick's bootstrap puts the field back.
        if field not in self.fields:
            raise BoardError(f"board field {field} is missing", 409)
        self._query(
            "mutation SetField($input:UpdateProjectV2ItemFieldValueInput!) {"
            "updateProjectV2ItemFieldValue(input:$input) {projectV2Item {id}}}",
            input={
                "projectId": self.project_id,
                "itemId": item,
                "fieldId": self.fields[field]["id"],
                "value": value,
            },
        )

    def status_of(self, item: str) -> tuple[str, bool] | None:
        """Return the card's Status and whether its issue is open, read now (not from an earlier listing)."""
        data = self._query(
            "query BoardItem($id:ID!) {node(id:$id) {... on ProjectV2Item {id "
            'fieldValueByName(name:"Status") {... on ProjectV2ItemFieldSingleSelectValue {name}} '
            "content {... on Issue {state}}}}}",
            id=item,
        )["node"]
        if not data:
            return None
        content = data.get("content") or {}
        return (data.get("fieldValueByName") or {}).get("name", ""), content.get("state", "OPEN") == "OPEN"

    def set_status(self, item: str, name: str) -> None:
        try:
            option = next(o["id"] for o in self.fields["Status"]["options"] if o["name"] == name)
        except (KeyError, StopIteration) as exc:
            raise BoardError(f"board Status option {name} is not available", 409) from exc
        self._set(item, "Status", {"singleSelectOptionId": option})

    def set_text(self, item: str, field: str, value: str) -> None:
        if field not in TEXT_FIELDS:
            raise ValueError(f"unknown board text field {field}")
        self._set(item, field, {"text": value})

    def default_branch_sha(self, repo: str) -> tuple[str, str]:
        """Read the current default branch with the board token when no App is configured."""
        owner, name = repo.split("/")
        data = self._query(
            "query DefaultBranch($owner:String!,$name:String!) {repository(owner:$owner,name:$name) {"
            "defaultBranchRef {name target {oid}}}}",
            owner=owner,
            name=name,
        )
        branch = (data.get("repository") or {}).get("defaultBranchRef")
        if not branch:
            raise BoardError(f"default branch of {repo} is not visible", 404)
        return branch["name"], branch["target"]["oid"]

    def items(self) -> list[BoardRow]:
        result: list[BoardRow] = []
        after = None
        position = 0
        while True:
            data = self._query(
                "query BoardItems($id:ID!,$after:String) {node(id:$id) {... on ProjectV2 {"
                'items(first:100,after:$after) {nodes {id fieldValueByName(name:"Status") {'
                "... on ProjectV2ItemFieldSingleSelectValue {name}} " + _TEXT_VALUES + "content {__typename "
                "... on Issue {id number title body state repository {nameWithOwner} "
                "labels(first:100) {nodes {name}} closedByPullRequestsReferences(first:100) {nodes {"
                "id number url state repository {nameWithOwner}}}}"
                "... on PullRequest {id number title body state repository {nameWithOwner} "
                "labels(first:100) {nodes {name}}}"
                "... on DraftIssue {id title body}"
                "}} pageInfo {hasNextPage endCursor}}}}}",
                id=self.project_id,
                after=after,
            )["node"]
            if not data:
                raise BoardError("board project is not visible", 404)
            data = data["items"]
            for item in data["nodes"]:
                content = item.get("content") or {}
                board_position = position
                position += 1
                if not content:
                    continue
                typename = content.get("__typename", "Issue")
                # Content the token cannot read arrives redacted. One such card must not lose the
                # whole listing; a draft card has no repository by nature.
                if "id" not in content:
                    logging.getLogger(__name__).warning(
                        "Board item %s skipped: content has no id", item["id"]
                    )
                    continue
                if typename != "DraftIssue" and not content.get("repository"):
                    logging.getLogger(__name__).warning(
                        "Board item %s skipped: content has no repository", item["id"]
                    )
                    continue
                item_type = {"Issue": "issue", "PullRequest": "pr", "DraftIssue": "draft"}.get(
                    typename, "draft"
                )
                result.append(
                    {
                        "item_id": item["id"],
                        "node_id": content["id"],
                        "repo": (content.get("repository") or {}).get("nameWithOwner", ""),
                        "number": content.get("number", 0),
                        "type": item_type,
                        "open": content.get("state", "OPEN") == "OPEN",
                        "body": content.get("body") or "",
                        "position": board_position,
                        # Merged and closed pull requests are history, not occupancy.
                        "linked_prs": [
                            pr
                            for pr in (content.get("closedByPullRequestsReferences") or {}).get("nodes", [])
                            if pr.get("state") == "OPEN"
                        ],
                        "linked_run": (item.get("Flyte") or {}).get("text", ""),
                        "title": content.get("title", ""),
                        "status": (item.get("fieldValueByName") or {}).get("name", ""),
                        "texts": {field: (item.get(field) or {}).get("text", "") for field in TEXT_FIELDS},
                        "labels": [v["name"] for v in (content.get("labels") or {}).get("nodes", [])],
                    }
                )
            if not data["pageInfo"]["hasNextPage"]:
                return result
            after = data["pageInfo"]["endCursor"]


def warn_expiry(
    configured: str,
    discovered: date | None,
    emit: Callable[[str, str], None] = _emit,
    *,
    today: date | None = None,
) -> None:
    expiry = date.fromisoformat(configured) if configured else discovered
    if expiry and expiry <= (today or date.today()) + timedelta(days=14):
        emit("🚨", f"Board PAT expires {expiry.isoformat()}; renew the board secret")


def run_url(profile: Profile | None, name: str) -> str:
    """Return the console link shared by supervisor reports and reconciliation."""
    return f"{profile.console_base}/runs/{name}" if profile and profile.console_base else ""


def engines_used(engines: Iterable[str]) -> str:
    """Return the board's engines, including the coordinator and excluding unknown usage."""
    return "+".join(sorted({"claude"} | {e for key in engines for e in key.split("+") if e != "unknown"}))


def _review_state(data: Mapping[str, Any]) -> tuple[GraphQL, bool]:
    """Return a snapshot's last QA verdict, and whether a review round started after it."""
    review = data.get("review") or {}
    phase = data.get("phase") or {}
    verdicts = review.get("verdicts") or []
    last = verdicts[-1] if verdicts else {}
    started = review.get("started_at") or (phase.get("since") if phase.get("kind") == "review" else None)
    later_round = max(review.get("round", 0), phase.get("round", 0)) > last.get("round", 0) or (
        started is not None and last.get("at") is not None and started > last["at"]
    )
    return last, later_round


def desired(live: bool, url: str, snapshot: Mapping[str, Any] | None) -> dict[str, str]:
    """Return the board values an issue's newest run implies: its Status, and the text fields it knows.

    `live` is whether the run is live in Flyte, `snapshot` its report's status (`_dashboard.from_report`).
    A run whose last QA verdict was PASS with no newer review round, a restored legacy pass, or a
    released run is Done;
    a live run is In flight, or In review once it has a pull request or a later review round.
    A run that failed or was aborted implies nothing,
    so the item stays where it is. Backlog and Ready are the owner's, and are never desired.
    """
    data = snapshot or {}
    deliverables = data.get("deliverables") or {}
    phase = data.get("phase") or {}
    last, later_round = _review_state(data)
    review = data.get("review") or {}
    passed = (
        phase.get("kind") in {"done", "released"}
        or (last.get("verdict") == "PASS" and not later_round)
        or (review.get("passed") and not review.get("verdicts"))
    )
    if passed:
        status = "Done"
    elif live:
        status = "In review" if later_round or deliverables.get("pr") else "In flight"
    else:
        return {}
    values = {"Status": status, "Flyte": url}
    if snapshot:
        if deliverables.get("branch"):
            values["Branch"] = deliverables["branch"]
        values["Engine"] = engines_used((data.get("cost") or {}).get("by_engine") or {})
    return {field: value for field, value in values.items() if value}


#: Board Status options in the order a run moves through them.
_STATUS_ORDER = STATUSES
#: Serializes card read-check-writes within this process only. Task pods, reconcile and supervisors
#: have separate locks, and GraphQL has no compare-and-set. A cross-process race may move a card back;
#: the next successful hourly drift pass heals closed issues to Done (a one-tick healing bound).
WRITE_LOCK = threading.Lock()


def _rank(status: str) -> int:
    return _STATUS_ORDER.index(status) if status in _STATUS_ORDER else -1


class _StatusWriter(Protocol):
    def set_status(self, item: str, name: str) -> None: ...


def advance_status(board: _StatusWriter, item: str, status: str) -> bool:
    """Move a card forward to `status` only: re-read it under WRITE_LOCK and never leave Done.

    For writers that start from an earlier listing (planner admission, backlog publication): a merge
    may have completed the card since. A board object without `status_of` (a test double) is written
    as before.
    """
    with WRITE_LOCK:
        reread = getattr(board, "status_of", None)
        now = reread(item) if reread else None
        if now is not None:
            current, _open = now
            if current == "Done" or _rank(status) <= _rank(current):
                return False
        board.set_status(item, status)
        return True


def _forward_only(want: dict[str, str], item: BoardRow, url: str) -> dict[str, str]:
    """`want` without a Status that would move the item backward while it still shows this run.

    The item shows this run when its Flyte field is the run's URL (the supervisor and the reconciler write
    it with the first Status). Then a lower Status is a stale report overwriting the supervisor's newer
    move, or an owner's hand-set Done, so it is dropped.
    An item that shows another run (or none) starts over from the new run's Status.
    """
    status = want.get("Status")
    if not status or item.get("texts", {}).get("Flyte") != url or _rank(status) >= _rank(item["status"]):
        return want
    return {field: value for field, value in want.items() if field != "Status"}


def _find(items: list[BoardRow], issue: str) -> BoardRow | None:
    return next((i for i in items if f"{i['repo']}#{i['number']}".lower() == issue.lower()), None)


def _ensure_item(client: BoardClient, items: list[BoardRow], issue: str) -> BoardRow:
    """Return the board item for `issue`, added if it is not among `items`.

    GitHub's addProjectV2ItemById returns the existing item for content already on the board, so a
    race with another adder (the supervisor and the reconciler) is harmless.
    """
    item = _find(items, issue)
    if item is not None:
        return item
    repo, number = issue.rsplit("#", 1)
    item_id = client.add_item(client.issue_node_id(issue))
    item = {"item_id": item_id, "repo": repo, "number": int(number), "status": "", "texts": {}}
    items.append(item)
    return item


def current_item(client: BoardClient, item: BoardRow) -> BoardRow | None:
    """`item` as the board holds it now: the listing a caller started from may predate another write."""
    data = client._query(
        "query BoardItem($id:ID!) {node(id:$id) {... on ProjectV2Item {id "
        'fieldValueByName(name:"Status") {... on ProjectV2ItemFieldSingleSelectValue {name}} '
        + _TEXT_VALUES
        + "content {... on Issue {state}}}}}",
        id=item["item_id"],
    )["node"]
    if not data or not data.get("content"):
        return None
    return {
        **item,
        "status": (data.get("fieldValueByName") or {}).get("name", ""),
        "open": data["content"]["state"] == "OPEN",
        "texts": {field: (data.get(field) or {}).get("text", "") for field in TEXT_FIELDS},
    }


def _write(
    client: BoardClient, item: BoardRow, values: Mapping[str, str], *, keep_done: bool = False
) -> None:
    """Set `values` on `item`, preserving the new-run evidence until its Status lands.

    Nothing is written while one of the fields is missing (409, retried once the reconciler has
    bootstrapped it), so a retried move does not write its other fields twice.

    A closed issue's Done is always kept. With `keep_done` an open issue's Done is kept too, unless the
    card shows an earlier run: then Done was that run's, and a new run starts the card over. Without an
    earlier run Done came from GitHub evidence (a merge) or a hand move, and no run may take it back.
    """
    shown = item.get("texts", {}).get("Flyte", "")
    earlier_run = bool(shown) and shown != values.get("Flyte", shown)
    if item["status"] == "Done" and (not item.get("open", True) or (keep_done and not earlier_run)):
        values = {field: value for field, value in values.items() if field != "Status"}
    absent = [field for field in values if field not in client.fields]
    if absent:
        raise BoardError(f"board field(s) {', '.join(absent)} missing", 409)
    new_run = "Flyte" in values and values["Flyte"] != item.get("texts", {}).get("Flyte")
    reset = new_run and "Status" in values and _rank(values["Status"]) < _rank(item["status"])
    # Advancing Flyte first would make a failed reset look like a hand-set Done on this run.
    if reset:
        client.set_status(item["item_id"], values["Status"])
    for field, value in values.items():
        if field != "Status":
            client.set_text(item["item_id"], field, value)
    if "Status" in values and not reset:
        client.set_status(item["item_id"], values["Status"])


class Reconciler:
    """One `factory-reconcile` tick of the board.

    It recomputes every item's values from Flyte and writes the differences. For a run the item
    already shows, Status only moves forward (`_forward_only`).

    `latest(ref)` is `(live, url, snapshot)` for the issue's newest project run when that run is live or
    ended recently enough to still be the factory's to project (`_reconcile.latest_run`), else None.
    Only this class bootstraps the board, and its ticks never overlap (the task's `max_runtime` is less
    than its rate), so a field is never created twice by the factory.
    """

    def __init__(
        self,
        profile: Profile,
        token: str,
        latest: Callable[[str], Any],
        emit: Callable[[str, str], None] = _emit,
        *,
        include_shown: Callable[[str, str], object] | None = None,
        live_issues: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        # Callers build a Reconciler only for a profile with a [board] (`_reconcile` checks first).
        self.config = cast("Board", profile.board)
        self.token, self.latest, self.emit = token, latest, emit
        self.include_shown = include_shown
        self.live_issues = live_issues
        self._failing = False
        self._unaddable: set[str] = set()  # live issues the token cannot add, alerted once per streak

    def tick(self) -> None:
        """Raise the first remote error after trying every item, so a failed sync is visible in Flyte.

        An issue the board token cannot read is the exception: it is alerted once and does not fail the tick,
        since waiting cannot fix it and a FAILED run every five minutes would bury real failures.
        """
        client = BoardClient(self.token)
        config = self.config
        try:
            client.bootstrap(config.owner, config.owner_type, config.number, create=False)
            missing = client.missing_fields()
            if missing:
                self.emit("⚠️", f"Board fields missing ({', '.join(missing)}); bootstrapping")
                client.bootstrap(config.owner, config.owner_type, config.number)
            failed: list[Exception] = []
            items = client.items()
            # A live run whose issue never reached the board (a failed add at launch): add it here, so
            # the loop below gives it its values.
            unaddable: set[str] = set()
            for ref in self.live_issues() if self.live_issues else ():
                try:
                    _ensure_item(client, items, ref)
                except BoardError as exc:
                    if exc.status != 404:
                        failed.append(exc)
                        continue
                    unaddable.add(ref)
                    if ref not in self._unaddable:
                        self.emit(
                            "🚨",
                            f"Board item {ref} could not be added. Can the board PAT read its repository?",
                        )
                except REMOTE_ERRORS as exc:
                    failed.append(exc)
            self._unaddable = unaddable
            for item in items:
                if item.get("type", "issue") != "issue":
                    continue
                ref = f"{item['repo']}#{item['number']}"
                try:
                    if self.include_shown:
                        self.include_shown(ref, item.get("texts", {}).get("Flyte", ""))
                    found = self.latest(ref)
                    if not found:
                        continue
                    want = _forward_only(desired(*found), item, found[1])
                    current = {"Status": item["status"], **item.get("texts", {})}
                    if not {f: v for f, v in want.items() if current.get(f) != v}:
                        continue  # in sync per the listing: no reread, no write
                    with WRITE_LOCK:
                        # The listing may predate a completion written since (a merge closing the issue):
                        # decide again against the card as it is now, so a closed issue's Done is kept.
                        item = current_item(client, item) or item
                        want = _forward_only(desired(*found), item, found[1])
                        current = {"Status": item["status"], **item.get("texts", {})}
                        _write(client, item, {f: v for f, v in want.items() if current.get(f) != v})
                except REMOTE_ERRORS as exc:
                    failed.append(exc)
            if failed:
                raise failed[0]
            self._failing = False
        except REMOTE_ERRORS as exc:
            status = getattr(exc, "status", 0)
            if not self._failing:
                self.emit(
                    "🚨" if status in _OWNER_STATUSES else "⚠️",
                    f"Board sync failed (HTTP {status or 'remote'})",
                )
            self._failing = True
            raise
        finally:
            try:
                warn_expiry(config.pat_expires, client.expires, self.emit)
            finally:
                client.close()


class Supervisor:
    """A project's own moves of its item, written as they happen.

    A failed write keeps the latest move in memory (with every text field set so far) and
    `_supervise_support._board_loop` calls `tick` every minute until it lands. Nothing survives the pod: the
    reconciler recomputes the same values from Flyte within five minutes. It never takes a card back
    out of Done, except to start a new run over an earlier run's Done and for a review round after the
    run's own pass (`move`'s `reopen_done`).
    """

    def __init__(self, profile: Profile, token: str, issue: str, emit: Callable[[str, str], None]) -> None:
        # `supervisor()` returns None for a profile without a [board], so one is always set here.
        self.config = cast("Board", profile.board)
        self.token, self.issue, self.emit = token, issue, emit
        self._status = ""
        self._texts: dict[str, str] = {}
        self._reopen_done = False
        self.pending = False
        self._failing = False
        self._warned_expiry = False
        # `move` runs in a worker thread while `_board_loop` may be in `tick` in another.
        self._lock = threading.Lock()

    def move(self, status: str, *, reopen_done: bool = False, **texts: str) -> None:
        """Record the move and try to write it. `reopen_done` lets it take the card out of Done.

        Only a run reopens its own Done, when it starts another review round after a pass. Done that the
        run did not give itself (a merge, a person) stays.
        """
        if not self.issue:
            return
        with self._lock:
            self._status = status
            self._reopen_done = reopen_done
            self._texts.update(texts)
            self.pending = True
        self.tick()

    def tick(self) -> None:
        with self._lock:
            if not self.pending:
                return
            try:
                self._apply()
            except (BoardError, httpx.TransportError) as exc:
                if not self._failing:
                    status = getattr(exc, "status", 0)
                    if status == 404:
                        self.emit(
                            "🚨",
                            f"Board item {self.issue} could not be added; retrying every minute. "
                            "Can the board PAT read its repository?",
                        )
                    else:
                        self.emit(
                            "🚨" if status in _OWNER_STATUSES else "⚠️",
                            f"Board write failed (HTTP {status or 'transport'}); retrying every minute",
                        )
                self._failing = True
                return
            self.pending = self._failing = False

    def _apply(self) -> None:
        client = BoardClient(self.token)
        config = self.config
        try:
            client.bootstrap(config.owner, config.owner_type, config.number, create=False)
            with WRITE_LOCK:
                item = _ensure_item(client, client.items(), self.issue)
                _write(client, item, {**self._texts, "Status": self._status}, keep_done=not self._reopen_done)
        finally:
            if not self._warned_expiry and (config.pat_expires or client.expires):
                self._warned_expiry = True
                warn_expiry(config.pat_expires, client.expires, self.emit)
            client.close()


def supervisor(
    profile: Profile | None, token: str, issue: str, emit: Callable[[str, str], None] = _emit
) -> Supervisor | None:
    if profile is None or profile.board is None:
        return None
    return Supervisor(profile, token, issue, emit)
