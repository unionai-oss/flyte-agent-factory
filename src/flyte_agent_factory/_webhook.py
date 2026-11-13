"""Signed GitHub deliveries wake deploys, issue intake and reviewed-PR loop runs.

The deployer's stored SHA pair remains authoritative for deploys. Issue, comment and closed-PR events
from allowlisted maintainers and loop verifier successes go to private `factory-issues` tasks; HTTP never
launches a project or writes to GitHub itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import flyte
import flyte.app
import flyte.errors
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from fastapi import FastAPI, HTTPException, Request, Response
from flyte import remote

# Flyte also loads this module as a script, where a relative import has no parent package, so the runtime
# imports are absolute. mypy resolves `flyte_agent_factory` to the editable install, which has no py.typed
# marker, so it checks against the relative imports instead.
if TYPE_CHECKING:
    from . import (
        _admission,
        _board,
        _board_events,
        _deploy,
        _factory,
        _factory_base,
        _git,
        _github,
        _issues,
        _loop,
        _profile,
        _pull_requests,
    )
    from ._errors import REMOTE_ERRORS
    from ._images import agent_image
else:
    from flyte_agent_factory import (
        _admission,
        _board,
        _board_events,
        _deploy,
        _factory,
        _factory_base,
        _git,
        _github,
        _issues,
        _loop,
        _profile,
        _pull_requests,
    )
    from flyte_agent_factory._errors import REMOTE_ERRORS
    from flyte_agent_factory._images import agent_image

PROFILE = _profile.require()
PROJECT = os.environ.get(
    "FLYTE_MCP_PROJECT", os.environ.get("FLYTE_INTERNAL_PROJECT", PROFILE.backend.project)
)
DOMAIN = os.environ.get("FLYTE_MCP_DOMAIN", os.environ.get("FLYTE_INTERNAL_DOMAIN", PROFILE.backend.domain))
SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
#: GitHub caps webhook payloads at 25 MB; anything larger is not a delivery. The body has to be read
#: before its signature can be checked, so this bounds what an unsigned request can make us buffer.
MAX_BODY = 25 * 1024 * 1024
#: Bodies read at once, and how long (seconds) a request waits for a turn before it gets 503. The cap
#: above is per request: without this, concurrent unsigned requests could each buffer 25 MB and
#: exhaust the 512Mi pod. Real deliveries are rare and small; a refused one is a failed delivery in
#: GitHub, which the reconcile covers.
MAX_CONCURRENT_BODIES = 2
BODY_WAIT = 1.0
#: The body must arrive within this (GitHub's delivery timeout), so a slow sender cannot hold a slot.
BODY_READ_DEADLINE = 10.0
#: Events that can trigger a deploy, and those that feed issue intake (`_issues.accept_event`).
DEPLOY_EVENTS = ("push", "check_suite", "status", "workflow_run")
INTAKE_EVENTS = ("issues", "issue_comment", "pull_request")
RECONCILE_EVENTS = (
    "pull_request",
    "pull_request_review",
    "check_run",
    "check_suite",
    "status",
    "workflow_run",
)
#: Board item actions that can put an item in front of admission. `edited` counts only when it changed
#: Status; `archived` and `deleted` take an item off the board.
BOARD_ITEM_ACTIONS = ("created", "edited", "reordered", "converted", "restored")
#: How long an intake submission may take before the delivery gets 503 (GitHub's delivery timeout).
INTAKE_SUBMIT_DEADLINE = 10.0
#: The deployed intake task, submitted by name (a test asserts it equals `process.name`).
ISSUES_TASK = "factory-issues.process"
_bodies = asyncio.Semaphore(MAX_CONCURRENT_BODIES)
_LOG = logging.getLogger(__name__)
_pending: set[asyncio.Task[None]] = set()
_consumer: _admission.Consumer | None = None
_reconcile_dirty: asyncio.Event | None = None
_completion_tasks: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _consumer, _reconcile_dirty
    if PROFILE.planner.admission == "planner" and os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise RuntimeError("planner admission requires one ASGI worker")
    owned = []
    if PROFILE.planner.admission == "planner":
        from . import _planner_runtime

        _consumer = await asyncio.to_thread(_planner_runtime.consumer, PROFILE)
        owned = [asyncio.create_task(_consumer.run()), asyncio.create_task(_planner_timer())]
    if PROFILE.board or PROFILE.github.configured:
        _reconcile_dirty = asyncio.Event()
        _reconcile_dirty.set()
        owned.append(asyncio.create_task(_reconcile_events()))
    try:
        yield
    finally:
        _consumer = None
        _reconcile_dirty = None
        tasks = [*owned, *_pending, *_completion_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


#: The planner's poll when board moves cannot arrive as events. GitHub delivers `projects_v2_item`
#: webhooks for organization boards only, so on a user-owned board a move to Ready is seen only by this
#: timer: keep it at the pre-#49 three minutes there instead of leaving a free slot idle for an hour.
_USER_BOARD_PLANNER_S = 180


def planner_interval_s(profile: _profile.Profile | None = None) -> float:
    """How often the planner wakes without an event: hourly when board moves arrive as events."""
    board = (profile or PROFILE).board
    if board is not None and board.owner_type == "org":
        return _deploy.RECONCILE_MINUTES * 60
    return _USER_BOARD_PLANNER_S


async def _planner_timer() -> None:
    while True:
        await asyncio.sleep(planner_interval_s())
        if _consumer is not None:
            _consumer.wake()


async def _wait_for_run(name: str) -> None:
    # Run.wait also caches outputs; a wake needs only terminal phase, even for failed projects.
    while True:
        run = await asyncio.to_thread(remote.Run.get, name=name, project=PROJECT, domain=DOMAIN)
        if _factory_base._phase(run) in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            return
        await asyncio.sleep(2)


def _wake_outcomes() -> None:
    if _reconcile_dirty is not None:
        _reconcile_dirty.set()
    if _consumer is not None:
        _consumer.wake()


async def _reconcile_events() -> None:
    # The admission Consumer persists launch intents in GitHub. Outcome repair has no intents:
    # Flyte is its source of truth, so an Event plus the hourly trigger suffices here.
    # `_lifespan` sets it before it creates this task.
    dirty = cast(asyncio.Event, _reconcile_dirty)
    while True:
        await dirty.wait()
        dirty.clear()
        try:
            async for run in remote.Run.listall.aio(
                task_name=_deploy.RECONCILE_TASK,
                in_phase=_deploy.LIVE_PHASES,
                project=PROJECT,
                domain=DOMAIN,
            ):
                await _wait_for_run(run.name)
            async with asyncio.timeout(30):
                task = await asyncio.to_thread(
                    remote.Task.get,
                    name=_deploy.RECONCILE_TASK,
                    auto_version="latest",
                    project=PROJECT,
                    domain=DOMAIN,
                )
                run = await flyte.with_runcontext(mode="remote", project=PROJECT, domain=DOMAIN).run.aio(task)
            await _wait_for_run(run.name)
        except REMOTE_ERRORS as exc:
            _LOG.error("Event reconcile failed (%s); hourly trigger will retry", type(exc).__name__)


async def _completion(run_name: str) -> None:
    try:
        # The sender is still RUNNING during its last act. Reading now would see an occupied slot
        # and omit the ended milestone; wait for Flyte's authoritative terminal phase instead.
        await _wait_for_run(run_name)
        _wake_outcomes()
    except REMOTE_ERRORS as exc:
        _LOG.error("Completion wake failed (%s); hourly trigger will retry", type(exc).__name__)


def _board_item_event(event: dict[str, Any]) -> bool:
    """Whether a `projects_v2_item` delivery can make a board item launchable.

    These are organization events with no repository, so the board's owner is the only scope there is.
    """
    board = PROFILE.board
    organization = event.get("organization")
    if board is None or not isinstance(organization, dict):
        return False
    if str(organization.get("login", "")).lower() != board.owner.lower():
        return False
    item = event.get("projects_v2_item")
    project = item.get("project_node_id") if isinstance(item, dict) else None
    # Without a configured node id every board of the organization wakes the planner: a spare,
    # idempotent pass, never a launch the board does not hold.
    if board.node_id and project != board.node_id:
        return False
    action = event.get("action")
    if action == "edited":
        changes = event.get("changes")
        field = changes.get("field_value") if isinstance(changes, dict) else None
        return isinstance(field, dict) and field.get("field_name") == "Status"
    return action in BOARD_ITEM_ACTIONS


def _outcome_event(kind: str, event: dict[str, Any]) -> bool:
    repository = event.get("repository")
    if not isinstance(repository, dict):
        return False
    repo = repository.get("full_name")
    if not isinstance(repo, str) or repo.lower() not in _issues.repo_targets(PROFILE):
        return False
    if kind == "status":
        # The drift pass posts this status itself; waking reconcile for it would run drift again.
        return event.get("state") in _FINAL_STATES and event.get("context") != _board_events.STATUS_CONTEXT
    if kind in ("check_run", "check_suite", "workflow_run"):
        return event.get("action") == "completed"
    return kind in ("pull_request", "pull_request_review")


# The intake tasks import this module, so they need the app's packages too.
_image = agent_image().with_pip_packages("fastapi>=0.115,<1", "uvicorn")
#: Issue intake and the closed-PR milestone. It holds the App key; the public app never does.
issues_env = flyte.TaskEnvironment(
    name="factory-issues",
    image=_image,
    resources=flyte.Resources(cpu=1, memory="1Gi"),
    secrets=[
        *(
            [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_pull_requests.KEY_ENV_VAR)]
            if PROFILE.github.private_key_secret
            else []
        ),
        *([flyte.Secret(key=PROFILE.board.pat_secret, as_env_var=_board.TOKEN_ENV)] if PROFILE.board else []),
    ],
    env_vars={"FLYTE_MCP_PROJECT": PROJECT, "FLYTE_MCP_DOMAIN": DOMAIN, **_profile.pod_env()},
)
# A public, unauthenticated endpoint: nothing but /github is served, not even the generated docs.
app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
env = flyte.app.AppEnvironment(
    name="factory-webhook",
    image=_image,
    requires_auth=False,
    resources=flyte.Resources(cpu=1, memory="512Mi"),
    scaling=flyte.app.Scaling(replicas=1),
    secrets=[
        *(
            [flyte.Secret(key=PROFILE.github.webhook_secret, as_env_var=SECRET_ENV)]
            if PROFILE.github.webhook_secret
            else []
        ),
        *(
            [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV)]
            if PROFILE.planner.admission == "planner"
            else []
        ),
    ],
    env_vars={"FLYTE_MCP_PROJECT": PROJECT, "FLYTE_MCP_DOMAIN": DOMAIN, **_profile.pod_env()},
    depends_on=[
        *([_factory.env] if PROFILE.planner.admission == "planner" else []),
        *(
            [issues_env]
            if PROFILE.board
            or PROFILE.loop.enabled
            or (PROFILE.planner.admission != "planner" and PROFILE.github.intake)
            else []
        ),
    ],
)


@env.server
async def serve() -> None:
    """Serve the signed public endpoint with the app's own Flyte identity."""
    import uvicorn

    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8080, workers=1)).serve()


@issues_env.task(
    retries=flyte.RetryStrategy(
        count=5,
        backoff=flyte.Backoff(base=timedelta(seconds=30), factor=2, cap=timedelta(minutes=5)),
    ),
    timeout=flyte.Timeout(max_runtime=timedelta(minutes=4), max_queued_time=timedelta(minutes=5)),
)
async def process(event: str, payload: str) -> str:
    """Launch, direct or report on the project an accepted issue event is about."""
    key = _pull_requests.take_app_key(PROFILE.github.private_key_secret)
    app = _github.GithubApp(PROFILE.github.app_id, key) if PROFILE.github.configured and key else None
    data = json.loads(payload)
    if _board_events.eligible(PROFILE, event, data):
        # `eligible` is False without a [board].
        token = _board.take_token(cast("_profile.Board", PROFILE.board).pat_secret)
        await asyncio.to_thread(_board_events.process, PROFILE, token, app, event, data)
    if not PROFILE.state.uri and _issues.accept_event(PROFILE, event, data):
        raise flyte.errors.NonRecoverableError("GitHub issue intake requires [state] uri")
    try:
        result = await asyncio.to_thread(_issues.process_event, PROFILE, event, data, app=app)
    except ValueError as exc:
        raise flyte.errors.NonRecoverableError(str(exc)) from exc
    return json.dumps(result)


@issues_env.task(
    retries=3,
    timeout=flyte.Timeout(max_runtime=timedelta(minutes=4), max_queued_time=timedelta(minutes=5)),
)
async def wake_loop(sha: str) -> None:
    """Resolve reviewed PR heads privately; retries adopt the same run for each PR and head."""
    if not PROFILE.loop.enabled:
        return
    key = _pull_requests.take_app_key(PROFILE.github.private_key_secret)
    github = _github.GithubApp(PROFILE.github.app_id, key)
    repo = PROFILE.loop.repo
    pulls = await asyncio.to_thread(github._get_pages, repo, f"/repos/{repo}/pulls", state="open")
    for pull in pulls:
        if pull["head"]["sha"].lower() != sha.lower():
            continue
        # Several PRs can share a head, and any reviewed PR gets this status: wake only the loop's own.
        # `pull` is GitHub's JSON; loop_pull reads only the keys it declares and tolerates their absence.
        if not await asyncio.to_thread(_loop.loop_pull, github, repo, cast("_loop.Pull", pull)):
            continue
        number = pull["number"]
        name = "loop-pr-" + hashlib.sha256(f"{repo.lower()}:{number}:{sha.lower()}".encode()).hexdigest()[:24]
        await _named_loop_run(name, "factory-loop.loop_run", pr_number=number)


async def _named_loop_run(name: str, task_name: str, **inputs: Any) -> remote.Run:
    try:
        return await remote.Run.get.aio(name=name, project=PROJECT, domain=DOMAIN)
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
    task = remote.Task.get(name=task_name, auto_version="latest", project=PROJECT, domain=DOMAIN)
    try:
        return await flyte.with_runcontext(
            mode="remote",
            project=PROJECT,
            domain=DOMAIN,
            name=name,
        ).run.aio(task, **inputs)
    except ConnectError as error:
        # Concurrent signed deliveries can both observe NOT_FOUND before either submission finishes.
        # (Not `exc`: mypy would carry the first handler's narrowing of `exc.code` into this one.)
        if error.code != Code.ALREADY_EXISTS:
            raise
        return await remote.Run.get.aio(name=name, project=PROJECT, domain=DOMAIN)


async def _submit_loop(sha: str) -> None:
    name = (
        "loop-status-"
        + hashlib.sha256(f"{PROFILE.loop.repo.lower()}:{sha.lower()}".encode()).hexdigest()[:24]
    )
    await _named_loop_run(name, "factory-issues.wake_loop", sha=sha)


def _loop_sha(kind: str, event: dict[str, Any]) -> object | None:
    repository = event.get("repository")
    if (
        PROFILE.loop.enabled
        and kind == "status"
        and isinstance(repository, dict)
        and str(repository.get("full_name", "")).lower() == PROFILE.loop.repo.lower()
        and event.get("context") == _pull_requests.STATUS_CONTEXT
        and event.get("state") == "success"
    ):
        return event.get("sha")
    return None


async def _intake(kind: str, event: dict[str, Any]) -> Response:
    """Submit an issue event from an allowlisted maintainer, waiting for the submission.

    Unlike a deploy there is no reconcile behind it, so a failed submission must show as a failed
    delivery (503) that can be redelivered. A redelivery, or a submission that timed out here but
    still started, is harmless: `_issues.process_event` dedupes launches and comments in storage.
    """
    if PROFILE.planner.admission == "planner":
        if _consumer is None:
            raise HTTPException(status_code=503, detail="Planner unavailable")
        if isinstance(event.get("repository"), dict):
            repo = event["repository"].get("full_name", "")
            if not isinstance(repo, str) or repo.lower() not in _issues.repo_targets(PROFILE):
                return Response(status_code=204)
            _consumer.wake()
            return Response(status_code=202)
        raise HTTPException(status_code=400, detail="Invalid event payload")
    # GitHub's JSON, typed by the keys accept_event reads; it rejects a malformed one with the errors below.
    payload = cast("_issues.EventPayload", event)
    try:
        accepted = _issues.accept_event(PROFILE, kind, payload)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid event payload") from None
    # The public endpoint has no App key. The private task authenticates the bot before any launch.
    if accepted is None and not (kind == "issues" and _issues.loop_creation(PROFILE, payload)):
        return Response(status_code=204)
    if not PROFILE.state.uri:
        raise HTTPException(status_code=503, detail="Intake state unavailable")
    await _submit_issue_event(kind, event)
    return Response(status_code=202)


async def _submit_issue_event(kind: str, event: dict[str, Any]) -> None:
    try:
        async with asyncio.timeout(INTAKE_SUBMIT_DEADLINE):
            # By reference, as `_submit` does: submitting the local `process` would make the SDK build
            # and upload a code bundle from this app pod, which has no storage-write permission.
            task = remote.Task.get(name=ISSUES_TASK, auto_version="latest", project=PROJECT, domain=DOMAIN)
            await flyte.with_runcontext(mode="remote", project=PROJECT, domain=DOMAIN).run.aio(
                task, event=kind, payload=json.dumps(event)
            )
    except REMOTE_ERRORS as error:  # TimeoutError, the deadline, is an OSError
        # As for deploys: SDK exception text can contain request details, so log only the type.
        _LOG.error("Webhook intake submission failed (%s)", type(error).__name__)
        raise HTTPException(status_code=503, detail="Submission unavailable") from None


def _watched_branch(event: dict[str, Any]) -> str | None:
    """Return the branch the deployer follows in the event's repository, or None if it follows none there."""
    repository = event.get("repository")
    if not isinstance(repository, dict):
        return None
    full_name, branch = repository.get("full_name"), repository.get("default_branch")
    if not isinstance(full_name, str) or not isinstance(branch, str) or not branch:
        return None
    identity = _git._repo_identity(f"https://github.com/{full_name}")
    deploy_repo = PROFILE.deploy.repo
    repos = [(deploy_repo, branch)]
    factory = PROFILE.repos.get("factory", "")
    if factory and deploy_repo and _git._repo_identity(factory) == _git._repo_identity(deploy_repo):
        repos.extend((url, branch) for url in PROFILE.repo_aliases.get("factory", ()))
    if PROFILE.direction:
        direction = PROFILE.direction
        repos.append((direction.repo, direction.ref or branch))
        for key, url in PROFILE.repos.items():
            if _git._repo_identity(url) == _git._repo_identity(direction.repo):
                repos.extend((alias, direction.ref or branch) for alias in PROFILE.repo_aliases.get(key, ()))
    return next((ref for url, ref in repos if url and identity == _git._repo_identity(url)), None)


#: Commit-status states after which the status no longer changes; "pending" is the only other one.
_FINAL_STATES = frozenset({"success", "failure", "error"})


def _deploy_sha(kind: str, event: dict[str, Any]) -> object | None:
    """Return the head SHA an eligible event is about, or None when the event cannot change what deploys.

    A push moves a followed branch. The CI events matter because the push usually lands before its
    CI finishes, and the deployer skips a pending head: without them the deploy would wait for the
    reconcile. Which head is deployed is still the deployer's call, from the branch heads it resolves.
    """
    branch = _watched_branch(event)
    if branch is None:
        return None
    if kind == "push":
        if event.get("deleted") or event.get("ref") != f"refs/heads/{branch}":
            return None
        return event.get("after")
    if kind in ("check_suite", "workflow_run"):
        run = event.get(kind)
        if (
            event.get("action") != "completed"
            or not isinstance(run, dict)
            or run.get("head_branch") != branch
        ):
            return None
        return run.get("head_sha")
    if kind == "status":
        branches = event.get("branches")
        if event.get("state") not in _FINAL_STATES or not isinstance(branches, list):
            return None
        if not any(isinstance(b, dict) and b.get("name") == branch for b in branches):
            return None
        return event.get("sha")
    return None


async def _submit(sha: str) -> None:
    # The HTTP deadline also covers a slow Flyte API: even the live-run lookup stays off that path.
    async with asyncio.timeout(30):
        async for _ in remote.Run.listall.aio(
            task_name=_deploy.DEPLOYER_TASK,
            in_phase=_deploy.LIVE_PHASES,
            limit=1,
            project=PROJECT,
            domain=DOMAIN,
        ):
            return
        task = remote.Task.get(
            name=_deploy.DEPLOYER_TASK, auto_version="latest", project=PROJECT, domain=DOMAIN
        )
        await flyte.with_runcontext(
            mode="remote",
            project=PROJECT,
            domain=DOMAIN,
            annotations={"github-sha": sha},
        ).run.aio(task)


def _submitted(task: asyncio.Task[None]) -> None:
    _pending.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        # SDK exception text can contain request details; the reconcile recovers failed submissions.
        _LOG.error("Webhook deploy submission failed (%s); reconcile will retry", type(error).__name__)


async def _body(request: Request) -> bytes:
    """Return the request body, refused with 413 once it exceeds MAX_BODY, declared or not."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY:
        raise HTTPException(status_code=413, detail="Payload too large")
    # Streamed rather than `request.body()`: a chunked or under-declared body must be cut off too.
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(status_code=413, detail="Payload too large")
    return bytes(body)


async def _read_body(request: Request) -> bytes:
    try:
        async with asyncio.timeout(BODY_WAIT):
            await _bodies.acquire()
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Busy") from None
    try:
        async with asyncio.timeout(BODY_READ_DEADLINE):
            body = await _body(request)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="Request body too slow") from None
    finally:
        _bodies.release()
    return body


def _board_item_delivery(body: bytes) -> Response:
    # Admission reads the board itself; the event only says when to look. Not a deploy or an intake.
    if PROFILE.board is None or (_consumer is None and _reconcile_dirty is None):
        return Response(status_code=204)
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event")
    if not _board_item_event(event):
        return Response(status_code=204)
    _wake_outcomes()
    return Response(status_code=202)


@app.post("/github")
async def github(request: Request) -> Response:
    """Authenticate a delivery; acknowledge eligible pushes without waiting for Flyte, submit intake."""
    body = await _read_body(request)
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook secret unavailable")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid signature")
    # "" for a missing header: no event name below matches either it or None.
    kind = request.headers.get("X-GitHub-Event", "")
    if kind == "ping":
        return Response(status_code=200)
    if kind == "projects_v2_item":
        return _board_item_delivery(body)
    board_event = PROFILE.board is not None and kind in ("push", "create", "pull_request", "issues")
    intake = kind in INTAKE_EVENTS and (
        PROFILE.planner.admission == "planner" or bool(PROFILE.github.maintainers)
    )
    outcomes = kind in RECONCILE_EVENTS and (
        PROFILE.board or PROFILE.github.configured or PROFILE.planner.admission == "planner"
    )
    if (
        not board_event
        and not intake
        and not outcomes
        and (kind not in DEPLOY_EVENTS or not (PROFILE.deploy.auto or PROFILE.loop.enabled))
    ):
        return Response(status_code=204)
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event")
    board_submitted = board_event and _board_events.eligible(PROFILE, kind, event)
    if board_submitted:
        await _submit_issue_event(kind, event)
    outcome = (
        outcomes and (_consumer is not None or _reconcile_dirty is not None) and _outcome_event(kind, event)
    )
    if outcome:
        if intake and PROFILE.planner.admission == "planner":
            if _reconcile_dirty is not None:
                _reconcile_dirty.set()
        else:
            _wake_outcomes()
    if intake and (not board_submitted or PROFILE.planner.admission == "planner"):
        response = await _intake(kind, event)
        return Response(status_code=202) if outcome and response.status_code == 204 else response
    loop_sha = _loop_sha(kind, event)
    if loop_sha is not None:
        if (
            not isinstance(loop_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", loop_sha)
            or loop_sha == "0" * 40
        ):
            raise HTTPException(status_code=400, detail="Invalid commit SHA")
        try:
            async with asyncio.timeout(INTAKE_SUBMIT_DEADLINE):
                await _submit_loop(loop_sha)
        except (TimeoutError, *REMOTE_ERRORS) as exc:
            _LOG.error("Webhook loop submission failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=503, detail="Submission unavailable") from None
    sha = _deploy_sha(kind, event) if PROFILE.deploy.auto else None
    if sha is None:
        return Response(status_code=202 if loop_sha is not None or outcome or board_submitted else 204)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha) or sha == "0" * 40:
        raise HTTPException(status_code=400, detail="Invalid commit SHA")
    # A single replica coalesces deliveries while submission is pending. Once submitted, the
    # server's live-run list suppresses duplicates; the deployer's lease covers listing races.
    if not _pending:
        task = asyncio.create_task(_submit(sha))
        _pending.add(task)
        task.add_done_callback(_submitted)
    return Response(status_code=202)


@app.post("/internal/planner")
async def planner_wakeup(request: Request) -> Response:
    """Authenticate planner completion wakes using the existing App secret."""
    if PROFILE.planner.admission != "planner" or _consumer is None:
        raise HTTPException(status_code=503, detail="Planner unavailable")
    body = await _read_body(request)
    key = os.environ.get(_deploy.GITHUB_KEY_ENV, "")
    signature = request.headers.get("X-Factory-Signature", "")
    expected = hmac.new(key.encode(), b"factory-planner-wakeup" + body, hashlib.sha256).hexdigest()
    if not key or not hmac.compare_digest(signature.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid signature")
    if not body:
        _wake_outcomes()
        return Response(status_code=202)
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None
    run_name = event.get("run") if isinstance(event, dict) else None
    if not isinstance(run_name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", run_name):
        raise HTTPException(status_code=400, detail="Invalid run name")
    task = asyncio.create_task(_completion(run_name))
    _completion_tasks.add(task)
    task.add_done_callback(_completion_tasks.discard)
    return Response(status_code=202)
