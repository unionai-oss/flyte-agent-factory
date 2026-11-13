"""GitHub-backed launch intents and Flyte planner backend.

Only the short planner task submits work. The public app persists its own
submission intent in the same direction repository before calling Flyte.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
from dataclasses import asdict, replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypedDict, cast

import flyte
from flyte import remote

from . import (
    _admission,
    _board,
    _deploy,
    _factory,
    _factory_base,
    _factory_meters,
    _factory_policy,
    _factory_recreate,
    _github,
    _issues,
    _loop,
    _names,
    _planner,
    _queue,
)

if TYPE_CHECKING:
    from ._profile import Direction, Profile

log = logging.getLogger(__name__)

TASK = "factory.plan_pass"
CORRELATION = "factory-admission"
PLANNER_LABEL = "factory-planner"
TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED")


class Intent(TypedDict, total=False):
    """A launch intent as `execute` writes it to its Git record. `run` is added once submitted.

    Not total: `reconcile` also reads records written by older versions, so it uses `.get` for the later keys.
    """

    labels: dict[str, str]
    issue_node: str
    generation: int | str  # a queued request's `gen` (int), else the hash of the issue body (str)
    base_sha: str
    engine: str
    login: str
    predicted_files: list[str]
    correlation: str
    state: str
    name: str
    repo: str
    slot: str
    manual: bool
    queue_request: bool
    run: str


def connection(profile: Profile) -> tuple[_github.GithubApp, str, str]:
    app = _github.GithubApp(profile.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])
    # Planner admission is only configured with a [direction] repository (a profile without one fails here).
    direction = cast("Direction", profile.direction)
    repo = _github.parse_repo(direction.repo)
    _, base = app.default_branch_sha(repo, direction.ref)
    return app, repo, base


def planner_labels(profile: Profile) -> dict[str, str]:
    scope = f"{_factory.PROJECT}/{_factory.DOMAIN}/{profile.name}"
    return {PLANNER_LABEL: hashlib.sha256(scope.encode()).hexdigest()[:32]}


def consumer(profile: Profile) -> _admission.Consumer:
    app, repo, base = connection(profile)
    record = _admission.GitRecord(app, repo, "factory/planner-submission", base)
    return _admission.Consumer(
        SimpleNamespace(
            read=partial(asyncio.to_thread, record.read),
            # The value argument is supplied by the caller; mypy wants it bound when the partial is made.
            write=partial(asyncio.to_thread, record.write),  # type: ignore[call-arg]  # see above: value comes later
        ),
        # Backend's SimpleNamespace runs have the name and correlation a PlannerRun needs.
        cast("_admission.Backend", Backend(profile)),
    )


class Backend:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    async def list_runs(self, correlation: str | None = None) -> list[SimpleNamespace]:
        labels = planner_labels(self.profile)
        if correlation:
            labels[CORRELATION] = correlation
        runs = [
            run
            async for run in remote.Run.listall.aio(
                task_name=TASK,
                project=_factory.PROJECT,
                domain=_factory.DOMAIN,
                with_labels=labels,
                limit=1000000,
                **({} if correlation else {"in_phase": _deploy.LIVE_PHASES}),
            )
        ]
        return [SimpleNamespace(name=r.name, correlation=r.pb2.labels[CORRELATION], run=r) for r in runs]

    async def submit(self, correlation: str) -> SimpleNamespace:
        task = remote.Task.get(
            name=TASK, auto_version="latest", project=_factory.PROJECT, domain=_factory.DOMAIN
        )
        run = await flyte.with_runcontext(
            mode="remote",
            project=_factory.PROJECT,
            domain=_factory.DOMAIN,
            labels={**planner_labels(self.profile), CORRELATION: correlation},
        ).run.aio(task)
        return SimpleNamespace(name=run.name, correlation=correlation, run=run)

    async def wait(self, run: SimpleNamespace) -> None:
        # Read the authoritative phase, including queued runs; submission acknowledgement
        # says nothing about whether a planner has finished holding the lock.
        while True:
            current = await asyncio.to_thread(remote.Run.get, name=run.name)
            if _factory_base._phase(current) in TERMINAL:
                return
            await asyncio.sleep(2)


def generation(item: _planner.BoardItem | _planner.ManualRequest) -> str:
    return hashlib.sha256(item.body.encode()).hexdigest()[:32]


def _intents(app: _github.GithubApp, repo: str, base: str) -> list[tuple[_admission.GitRecord, Intent]]:
    rows = app._call(repo, "GET", f"/repos/{repo}/git/matching-refs/factory/intents/")
    result: list[tuple[_admission.GitRecord, Intent]] = []
    for row in rows:
        record = _admission.GitRecord(app, repo, row["ref"].removeprefix("refs/"), base)
        # The record holds the JSON `execute` wrote, which is an Intent.
        result.append((record, cast(Intent, record.read())))
    return result


def _matches(correlation: str) -> list[remote.Run]:
    return list(
        remote.Run.listall(
            project=_factory.PROJECT,
            domain=_factory.DOMAIN,
            with_labels={CORRELATION: correlation},
            limit=1000000,
        )
    )


def reconcile(
    record: _admission.GitRecord, intent: Intent, board: _board.BoardClient, rows: list[_board.BoardRow]
) -> _planner.ActiveRun:
    runs = _matches(intent["correlation"])
    if len(runs) > 1:
        raise _admission.AdmissionInvariant("multiple project runs for one launch intent")
    if not runs:
        raise _admission.UnresolvedSubmission("project submission unresolved; intake blocked")
    run = runs[0]
    record.write({**intent, "state": "submitted", "run": run.name})
    if intent.get("queue_request"):
        queue = _factory_base.queue_root()
        _queue.mark(
            queue,
            intent["name"],
            # A queue request's generation is its int `gen`; only intents with `queue_request` get here.
            cast("int", intent["generation"]),
            "started",
            run=run.name,
            url=run.url,
            slot=intent["slot"],
        )
    row = next((r for r in rows if r["node_id"] == intent["issue_node"]), None)
    terminal = _factory_base._phase(run) in TERMINAL
    if row:
        # The outcome reconciler owns Done/QA. Here a terminal run still reserves
        # its generation, and a failed board write retains the intent for retry.
        if row["status"] == "Ready":
            _board.advance_status(board, row["item_id"], "In flight")
        board.set_text(row["item_id"], "Flyte", run.url)
    reflected = row and row["status"] != "Ready" and row.get("linked_run") == run.url
    if terminal and (reflected or (intent.get("manual") and row is None)):
        record.delete()
    return _planner.ActiveRun(
        run.name,
        intent["repo"],
        intent["issue_node"],
        str(intent["generation"]),
        phase=_factory_base._phase(run),
        predicted_files=tuple(intent["predicted_files"]),
        labels=intent.get("labels", {}),
    )


def import_requests(
    app: _github.GithubApp,
    repo: str,
    base: str,
    board: _board.BoardClient,
    rows: list[_board.BoardRow],
) -> tuple[list[_planner.ManualRequest], dict[str, _queue.Request]]:
    """Keep legacy generation/brief in Git before importing a linked request into Ready."""
    requests: dict[str, _queue.Request] = {}
    queue = _factory_base.queue_root()
    migration = _admission.GitRecord(app, repo, "factory/legacy-import", base)
    migrating = not migration.read().get("done")
    for entry in _queue.pending(queue):
        request = entry.request
        if not migrating or request is None or not request.issue:
            continue
        if _factory._reconcile(queue, entry):
            continue
        node = board.issue_node_id(request.issue)
        row = next((r for r in rows if r["node_id"] == node), None)
        if row is None:
            board.add_item(node)
            rows = board.items()
            row = next(r for r in rows if r["node_id"] == node)
        record = _admission.GitRecord(app, repo, f"factory/imports/{node}-{entry.gen}", base)
        if not record.read():
            record.write({"node": node, "issue_body": row["body"], "request": asdict(request)})
        _board.advance_status(board, row["item_id"], "Ready")
        _queue.mark(queue, entry.name, entry.gen, "cancelled", error="imported into Ready admission")
    if migrating:
        migration.write({"done": True})
    imported = app._call(repo, "GET", f"/repos/{repo}/git/matching-refs/factory/imports/")
    for ref in imported:
        record = _admission.GitRecord(app, repo, ref["ref"].removeprefix("refs/"), base)
        # Written above as {node, issue_body, request}.
        value = cast("dict[str, Any]", record.read())
        row = next((r for r in rows if r["node_id"] == value["node"]), None)
        if row and row["body"] == value["issue_body"]:
            requests[value["node"]] = _queue.Request.from_json(json.dumps(value["request"]).encode())
    manual: list[_planner.ManualRequest] = []
    for entry in _queue.pending(queue):
        request = entry.request
        if request is None:
            continue
        manual.append(
            _planner.ManualRequest(
                request.name,
                request.repo,
                issue=request.issue,
                body=_queue.effective_brief(request),
                priority=_factory.effective_priority(request),
                policy=request.policy,
            )
        )
        requests[request.name] = request
    return manual, requests


def record_waits(plan: _planner.Plan, requests: dict[str, _queue.Request]) -> None:
    """Store why each queued request waits after this pass, for `factory.queue_list`.

    The reasons are the planner's own (`Plan.waiting`). A board item has no queue request and no row there.
    The write replaces the last pass's, so a request that started or was cancelled leaves no stale reason.
    """
    waits: dict[str, dict[str, Any]] = {}
    for waiting in plan.waiting:
        item, reason = waiting.item, waiting.reason
        if not isinstance(item, _planner.ManualRequest) or (request := requests.get(item.name)) is None:
            continue
        detail: dict[str, Any]
        if reason.code == "low_priority_waits":
            text, detail = f"behind {reason.behind} request(s) that can start", {"behind": reason.behind}
        elif reason.code == "project_running":  # the code the dispatcher writes too keeps its shape
            text, detail = reason.with_identity, {"holders": [reason.with_identity]}
        else:  # the planner's own codes keep their identity text as `detail`
            text = " ".join(filter(None, (reason.with_identity, reason.detail)))
            detail = {"detail": reason.with_identity} if reason.with_identity else {}
        waits[item.name] = _queue.describe_wait(request.gen, reason.code, text, **detail)
    _queue.write_waits(_factory_base.PROFILE.state.uri, waits)


PIN_KEY = _names.COORDINATOR_ENGINE
#: The engine admission chose for an unpinned request. The pod prefers it for a new conversation but never
#: treats it as a pin: a resumed project stays on the engine its conversation is on.
ADMITTED_KEY = "AGENT_FACTORY_ADMITTED_ENGINE"
#: Why admission chose an engine other than the preferred one; the pod writes it to the timeline.
NOTE_KEY = "AGENT_FACTORY_ADMISSION_NOTE"

#: Engines supported by the project author runners.
AUTHOR_ENGINES = ("claude", "codex")


def _loop_may_launch(profile: Profile, app: _github.GithubApp, slot: str, *, log_name: str) -> bool:
    """Return the kill switch and spend guard `_factory` applies to a queued loop launch, asked again.

    The planner's snapshot can be minutes old by the time it submits. A kill-switch read error
    propagates: with no answer the loop does not launch.
    """
    if _loop.killed(profile, app):
        log.warning("planner: loop launch of %s refused: killed", log_name)
        return False
    budget = _factory_meters._loop_slot_budget(slot, demand=_factory.owner_demand())
    if budget.action != "run":
        log.warning("planner: loop launch of %s waits: %s", log_name, budget.reason)
        return False
    return True


def execute(
    profile: Profile,
    app: _github.GithubApp,
    repo: str,
    base: str,
    board: _board.BoardClient,
    rows: list[_board.BoardRow],
    facts: _planner.Facts,
    requests: dict[str, _queue.Request] | None = None,
) -> list[str]:
    """Reconcile every surviving intent before proposing or submitting any new work."""
    requests = dict(requests or {})
    occupied: list[_planner.ActiveRun] = []
    for record, intent in _intents(app, repo, base):
        occupied.append(reconcile(record, intent, board, rows))
    # A pin to an engine the profile has no login for can never launch; fail it as `prepare_launch`
    # refuses it, rather than leave it waiting for good. (The planner's own `pin_unconfigured` is a
    # wait: the pinned engine exists, just not on a free slot.)
    failed = set()
    for manual in facts.manual_requests:
        if (request := requests.get(manual.name)) is None:
            continue
        try:
            _factory._check_author_pin(_factory_policy.normalize_policy(request.policy or {}))
        except ValueError as exc:
            log.warning("planner: %s fails: %s", manual.name, exc)
            _queue.mark(_factory_base.queue_root(), request.name, request.gen, "failed", error=str(exc))
            failed.add(manual.name)
            del requests[manual.name]
    facts = replace(
        facts,
        manual_requests=tuple(m for m in facts.manual_requests if m.name not in failed),
        capacity=tuple(slot for slot in facts.capacity if slot.engine in AUTHOR_ENGINES),
        pins={
            node: pin
            for node, queued in requests.items()
            if (pin := _factory_policy.normalize_policy(queued.policy or {}).get(PIN_KEY, "").strip().lower())
        },
        active_runs=(*facts.active_runs, *occupied),
        items=tuple(
            replace(item, body=_queue.effective_brief(requests[item.node_id]))
            if item.node_id in requests
            else item
            for item in facts.items
        ),
    )
    launched: list[str] = []
    used_slots: set[str] = set()
    plan = _planner.plan(facts)
    record_waits(plan, requests)
    for launch in plan.launches:
        item = launch.item
        is_manual = isinstance(item, _planner.ManualRequest)
        node = item.name if isinstance(item, _planner.ManualRequest) else item.node_id
        request = requests.get(node)
        gen = request.gen if request else generation(item)
        ref = f"factory/intents/{node}-{gen}"
        record = _admission.GitRecord(app, repo, ref, base)
        if record.read():
            continue
        # Terminal runs remain the durable generation history after intent cleanup.
        identity = hashlib.sha256(f"{node}:{gen}".encode()).hexdigest()[:32]
        if list(remote.Run.listall(with_labels={"factory-work": identity}, limit=1)):
            continue
        slot = next(
            c.slot
            for c in facts.capacity
            if c.engine == launch.engine and c.login == launch.login and c.slot not in used_slots
        )
        used_slots.add(slot)
        # A manual request always has a queue `request`; without one `item` is a board item with a number.
        issue = request.issue if request else f"{item.repo}#{item.number}"  # type: ignore[union-attr]  # see above
        issue_node = next((row["node_id"] for row in rows if f"{row['repo']}#{row['number']}" == issue), node)
        name = request.name if request else f"issue-{item.number}-{identity[:12]}"  # type: ignore[union-attr]  # see above
        canonical = _planner._canonical_repo(item.repo, facts.repo_aliases)
        _, base_sha = app.default_branch_sha(canonical)
        labels = (
            {"factory-loop": _issues.issue_label(issue)}
            if isinstance(item, _planner.BoardItem) and _planner.is_loop(item)
            else {}
        )
        intent = {
            "labels": labels,
            "issue_node": issue_node,
            "generation": gen,
            "base_sha": base_sha,
            "engine": launch.engine,
            "login": launch.login,
            "predicted_files": list(launch.predicted_files),
            "correlation": secrets.token_hex(16),
            "state": "pending",
            "name": name,
            "repo": canonical,
            "slot": slot,
            "manual": is_manual,
            "queue_request": request is not None,
        }
        brief = _queue.effective_brief(request) if request else _issues.frame_brief(issue, "board", item.body)
        policy = _factory_policy.normalize_policy(request.policy or {}) if request else {}
        # An explicit pin is the request's own and goes through unchanged; the pod honours it or says
        # why it did not. Without one, the pod prefers admission's choice under its own key, which is
        # no pin, and says why when that is not the preferred engine.
        if PIN_KEY not in policy:
            policy[ADMITTED_KEY] = launch.engine
            if launch.engine != facts.author_engine:
                policy[NOTE_KEY] = launch.reason
        workspace, note = _factory_recreate._workspace_for(request) if request else (None, "")
        target_repo = item.repo if is_manual else _issues.repo_targets(profile)[canonical][0]
        target_repo = _factory._resolve_repo(target_repo, issue=issue)
        if labels and not _loop_may_launch(profile, app, slot, log_name=name):
            continue
        record.write(intent)
        run = _factory.launch_on_slot(
            slot,
            brief + note,
            name,
            request.model if request else "",
            repo=target_repo,
            demo=request.demo_package if request else "",
            policy=policy,
            identities=request.browser if request else (),
            workspace_in=workspace,
            labels={
                **labels,
                CORRELATION: intent["correlation"],
                "factory-work": identity,
                _factory_base.LABEL_PROJECT: name,
                _factory_base.LABEL_GEN: str(gen),
                **({"factory-issue": _issues.issue_label(issue)} if issue else {}),
            },
        )
        record.write({**intent, "state": "submitted", "run": run.name})
        reconcile(record, intent, board, rows)
        launched.append(run.name)
    return launched


def notify(profile: Profile, key: str, run_name: str = "") -> None:
    """Wake outcome repair or the planner; the hourly timer heals missed submissions."""
    import hmac
    import json

    import httpx

    if profile.planner.admission != "planner":
        task = remote.Task.get(
            name=_deploy.RECONCILE_TASK,
            auto_version="latest",
            project=_factory.PROJECT,
            domain=_factory.DOMAIN,
        )
        flyte.with_runcontext(mode="remote", project=_factory.PROJECT, domain=_factory.DOMAIN).run(
            task, completion_run=run_name
        )
        return
    app = remote.App.get(name="factory-webhook", project=_factory.PROJECT, domain=_factory.DOMAIN)
    body = json.dumps({"run": run_name}).encode() if run_name else b""
    signature = hmac.new(key.encode(), b"factory-planner-wakeup" + body, hashlib.sha256).hexdigest()
    response = httpx.post(
        app.endpoint.rstrip("/") + "/internal/planner",
        content=body,
        headers={"X-Factory-Signature": signature},
        timeout=10,
    )
    response.raise_for_status()
