"""`factory-deployer`: merged factory changes deploy themselves.

On a signed push, or a periodic reconcile, `deploy_if_changed` compares the deploy repo's
(`[deploy] repo`, default `[repo] factory`) default-branch head with the SHA recorded at the last
successful deploy. If they differ and its CI passed, it deploys that SHA: the work slots, the factory
environments and itself, then the apps. It is on by `[deploy] auto` in the profile and is otherwise a no-op.

It holds no deploy key. Status and source come through the profile's GitHub App
(`_github`) when it is installed on the deploy repo, else anonymously (a public repository); the source is
a shallow fetch of exactly the SHA over HTTPS. Projects that are running keep the version they started with,
so a deploy never interrupts one.

Release history and attempt counts live in labelled Flyte runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, TypeVar, TypeVarTuple, Unpack, cast

import flyte
import flyte.report
from flyte import remote

# Deploy tools also load this file by path, without a package context.
if TYPE_CHECKING:
    from . import _deploy, _github, _profile, _pull_requests, _standards
    from ._errors import REMOTE_ERRORS
    from ._images import agent_image
else:
    from flyte_agent_factory import _deploy, _github, _profile, _pull_requests, _standards
    from flyte_agent_factory._errors import REMOTE_ERRORS
    from flyte_agent_factory._images import agent_image

PROFILE = _profile.require()
PROJECT = os.environ.get(
    "FLYTE_MCP_PROJECT", os.environ.get("FLYTE_INTERNAL_PROJECT", PROFILE.backend.project)
)
DOMAIN = os.environ.get("FLYTE_MCP_DOMAIN", os.environ.get("FLYTE_INTERNAL_DOMAIN", PROFILE.backend.domain))
MAX_RUNTIME = timedelta(minutes=60)
MAX_ATTEMPTS = 3
#: Deploys one `deploy_pair` run makes when the heads move under it; a fourth head waits for a tick.
MAX_DEPLOYS_PER_RUN = 3
SETTLE = 10
# No live result yet. Keep the CAS fallback until ops/probes/run_listing_order.py passes on the tenant.
RUN_LISTING_CONSISTENT = False
_LIVE = _deploy.LIVE_PHASES
_TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED")
_FAILED = ("FAILED", "TIMED_OUT", "ABORTED")

env = flyte.TaskEnvironment(
    name="factory-deployer",
    # It imports the factory-mcp app to deploy it, so it needs flyte's `mcp` extra (pins as pyproject).
    # `kubernetes` is what Flyte imports to serialize the personal browser's pod template: the deployer
    # deploys that app (`_deploy.deploy_apps`), and the agent image it is built from does not carry it.
    image=agent_image().with_pip_packages(
        "mcp>=1.30.0,<2",
        "starlette",
        "fastapi>=0.115,<1",
        "uvicorn",
        "regex>=2024.5.15",
        "kubernetes>=31.0.0",
    ),
    # A tick is a few HTTP calls; a deploy is subprocesses that import the agent and submit an image
    # build to the remote builder. Nothing here is heavy.
    resources=flyte.Resources(cpu=1, memory="2Gi"),
    # Spot: a preempted tick records nothing, and the next one retries.
    interruptible=True,
    secrets=(
        [flyte.Secret(key=PROFILE.github.private_key_secret, as_env_var=_deploy.GITHUB_KEY_ENV)]
        if PROFILE.github.private_key_secret
        else []
    ),
    env_vars={"FLYTE_MCP_PROJECT": PROJECT, "FLYTE_MCP_DOMAIN": DOMAIN, **_profile.pod_env()},
)

_reconcile_minutes = _deploy.reconcile_minutes


TRIGGER = flyte.Trigger(
    name="deploy-on-merge",
    automation=flyte.FixedRate(_reconcile_minutes(PROFILE)),
    description="Deploy the deploy repo's default branch when it has changed and its CI has passed",
    auto_activate=True,
)


def _say(message: str) -> str:
    print(f"[deploy] auto {message}", flush=True)
    return message


def _label_hash(text: str) -> str:
    # Run labels may become pod labels, whose values are at most 63 characters of [A-Za-z0-9_.-]:
    # a full sha256 is 64, and a repo or profile name may hold '/' or '+'.
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def release_labels(profile: _profile.Profile) -> dict[str, str]:
    # A rename must not inherit the old repository's success or give-up history.
    repo = _deploy_repo(profile).lower()
    return {"deploy-repo": _label_hash(repo), "deploy-profile": _label_hash(profile.name)}


def _deploy_repo(profile: _profile.Profile) -> str:
    if not profile.deploy.repo:
        raise _deploy.DeployError("[deploy] auto needs a [deploy] repo or a [repo] factory in the profile")
    return _github.parse_repo(profile.deploy.repo)


def _deploy_reader(app: _github.GithubApp, repo: str) -> _github.GithubApp | _github.PublicGithub:
    """Return the App where it is installed on `repo`, else anonymous reads (the deploy repo is public)."""
    try:
        app.installation_token(repo)
    except _github.GithubError as exc:
        if exc.status != 404:
            raise
        return _github.PublicGithub()
    return app


def _clone_url(app: _github.GithubApp, repo: str) -> str:
    if _deploy_reader(app, repo) is app:
        return _github.clone_url(repo, app.installation_token(repo))
    return f"https://github.com/{repo}.git"


def _pair_labels(profile: _profile.Profile, sha: str, direction_sha: str) -> dict[str, str]:
    return {**release_labels(profile), "deploy-sha": sha, "deploy-direction": direction_sha or "none"}


class _RunFilters(TypedDict):
    with_labels: NotRequired[dict[str, str]]
    in_phase: NotRequired[tuple[str, ...]]
    sort_by: NotRequired[tuple[str, Literal["asc", "desc"]]]
    limit: NotRequired[int]


class DeployOutput(TypedDict):
    deployed: bool
    envs: NotRequired[list[str]]
    slots: NotRequired[list[str]]
    sha: NotRequired[str]
    direction_sha: NotRequired[str]
    standards: NotRequired[dict[str, bool | str | None]]
    #: Secret name -> environments that need it: nothing was deployed because they do not exist yet.
    waiting_for_secrets: NotRequired[dict[str, list[str]]]


async def _runs(profile: _profile.Profile, **kwargs: Unpack[_RunFilters]) -> list[remote.Run]:
    return [
        run
        async for run in remote.Run.listall.aio(
            task_name=_deploy.DEPLOY_PAIR,
            project=PROJECT,
            domain=DOMAIN,
            with_labels=kwargs.pop("with_labels", release_labels(profile)),
            **kwargs,
        )
    ]


async def deployed_pair(profile: _profile.Profile) -> tuple[remote.Run, tuple[str, str]] | None:
    """Newest successful deployment and the (sha, direction_sha) it left deployed, or None.

    The pair is the run's output, not its labels: a run that followed a moved head deployed a later
    pair than the one it was submitted for. A successful stand-down has no deployed pair, and any
    number of them may follow a release, so this pages until it finds one (labels are fixed at
    submission, so a deploy cannot be told from a stand-down by label).
    """
    async for run in remote.Run.listall.aio(
        task_name=_deploy.DEPLOY_PAIR,
        project=PROJECT,
        domain=DOMAIN,
        with_labels=release_labels(profile),
        in_phase=("SUCCEEDED",),
        sort_by=("created_at", "desc"),
        limit=1000000,
    ):
        try:
            (output,) = await run.outputs.aio()
            if (
                isinstance(output, dict)
                and output.get("deployed") is True
                and isinstance(output.get("envs"), list)
                and output["envs"]
                and isinstance(output.get("sha"), str)
                and isinstance(output.get("direction_sha"), str)
            ):
                return run, (output["sha"], output["direction_sha"])
        except (*REMOTE_ERRORS, ValueError, TypeError, KeyError, AttributeError):
            # An unreadable output must not hide the preceding release.
            _say(f"ignoring unreadable outputs of {run.name}")
    return None


def _started(run: remote.Run) -> bool:
    # A pair that waited out `max_queued_time` for a spot node ends TIMED_OUT without ever starting;
    # it ran nothing, so it is not an attempt. An unstarted run's start time reads as the epoch.
    status = run.pb2.action.status
    # Generated protobuf bindings do not type HasField.
    return cast(bool, status.HasField("start_time") and status.start_time.seconds > 0)


async def _failed_attempts(profile: _profile.Profile, labels: dict[str, str], last: remote.Run | None) -> int:
    """Return started runs of this pair that failed since the newest release, counted up to MAX_ATTEMPTS."""
    # The SDK's Run protobuf omits creation time. Traverse server creation order to the
    # success boundary instead of substituting start_time (queued runs can start out of order).
    failures = 0
    async for candidate in remote.Run.listall.aio(
        task_name=_deploy.DEPLOY_PAIR,
        project=PROJECT,
        domain=DOMAIN,
        with_labels=release_labels(profile),
        sort_by=("created_at", "desc"),
        limit=1000000,
    ):
        if last and candidate.name == last.name:
            break
        if (
            str(candidate.phase).split(".")[-1] in _FAILED
            and all(candidate.pb2.labels.get(k) == v for k, v in labels.items())
            and _started(candidate)
        ):
            failures += 1
            if failures == MAX_ATTEMPTS:
                break
    return failures


def _heads(profile: _profile.Profile, app: _github.GithubApp) -> tuple[str, str, str, str]:
    """(repo, sha, direction_repo, direction_sha) of the default branches now; direction ones "" without."""
    repo = _deploy_repo(profile)
    # `git ls-remote` is not REST: a tick whose head is already deployed spends none of an anonymous
    # reader's 60 calls an hour. A repository it cannot read (private) falls back to the App.
    sha = _deploy.public_head(repo)
    if not sha:
        _, sha = _deploy_reader(app, repo).default_branch_sha(repo)
    direction_repo = direction_sha = ""
    if profile.direction:
        direction_repo = _github.parse_repo(profile.direction.repo)
        try:
            _, direction_sha = app.default_branch_sha(direction_repo, profile.direction.ref)
        except _github.GithubError as exc:
            raise _deploy.DeployError(
                f"direction repo {direction_repo} cannot be read, nothing deployed: {exc}"
            ) from exc
    return repo, sha, direction_repo, direction_sha


def _shown(sha: str, direction_sha: str) -> str:
    return f"{sha} with direction {direction_sha}" if direction_sha else sha


async def _not_deployable(
    profile: _profile.Profile,
    app: _github.GithubApp,
    heads: tuple[str, str, str, str],
    last: remote.Run | None,
) -> str | None:
    """Why the tick's checks refuse these heads (given up on, or CI not green), or None to deploy."""
    repo, sha, direction_repo, direction_sha = heads
    shown = _shown(sha, direction_sha)
    if await _failed_attempts(profile, _pair_labels(profile, sha, direction_sha), last) >= MAX_ATTEMPTS:
        return f"giving up on {shown} after {MAX_ATTEMPTS} attempts; waiting for a new commit"
    targets = [(repo, sha, "")]
    if direction_repo:
        targets.append((direction_repo, direction_sha, "direction "))
    for target, head, prefix in targets:
        reader = _deploy_reader(app, target) if target == repo else app
        health, detail = reader.commit_health(target, head)
        if health in ("failing", "pending"):
            retry = "; retrying next tick" if health == "pending" else ""
            return f"skipped {prefix}{head}: {detail}{retry}"
        if health == "none" and target == repo:
            # Deploying code nothing tested is the bug a CI-less repository would silently reintroduce.
            return f"no CI reported on {head}: not deployed; the deploy repo must run CI"
        _say(f"deploying {prefix}{head} (CI: {health}, {detail})")
    return None


async def _read_secrets_last_waited_for(
    profile: _profile.Profile, labels: dict[str, str]
) -> dict[str, list[str]]:
    """Return the secrets the newest finished attempt at these heads waited for; none if it did not wait."""
    for run in await _runs(
        profile, with_labels=labels, in_phase=("SUCCEEDED",), sort_by=("created_at", "desc"), limit=1
    ):
        try:
            (output,) = await run.outputs.aio()
            waiting = output.get("waiting_for_secrets")
            if isinstance(waiting, dict):
                return waiting
        except (*REMOTE_ERRORS, TypeError, AttributeError):
            _say(f"ignoring unreadable outputs of {run.name}")
    return {}


async def _submit(task_name: str, *, labels: dict[str, str] | None = None, **inputs: str) -> remote.Run:
    task = remote.Task.get(name=task_name, auto_version="latest", project=PROJECT, domain=DOMAIN)
    return await flyte.with_runcontext(
        mode="remote", project=PROJECT, domain=DOMAIN, labels=labels or {}
    ).run.aio(task, **inputs)


def _lock_repo(profile: _profile.Profile, app: _github.GithubApp) -> str:
    """Return the deploy lock's repository: the deploy repo if the App is installed, else [direction]."""
    repo = _deploy_repo(profile)
    if _deploy_reader(app, repo) is app:
        return repo
    if profile.direction:
        return _github.parse_repo(profile.direction.repo)
    raise _deploy.DeployError(
        "nothing is deployed: the deploy lock needs a repository the GitHub App can write, and the App is "
        f"not installed on {repo}. Install the App on {repo}, or set [direction] repo to a repository it is "
        "installed on"
    )


def _acquire_ref(profile: _profile.Profile, app: _github.GithubApp, run_name: str, sha: str) -> bool:
    """GitHub's create-if-absent / fast-forward update supplies CAS when listings cannot.

    Keep the ref after completion. Deleting a dead holder's ref races with its successor; instead
    contenders create sibling commits, so only one fast-forward can replace the terminal holder.
    A missing/unreadable holder never expires: fail closed until its Flyte phase is authoritative.
    """
    repo = _lock_repo(profile, app)
    key = hashlib.sha256(f"{PROJECT}/{DOMAIN}/{profile.name}".encode()).hexdigest()
    ref = f"heads/factory-deployer-lock/{key}"
    path = f"/repos/{repo}/git"
    parent = None
    try:
        current = app._call(repo, "GET", f"{path}/ref/{ref}")
        parent = current["object"]["sha"]
    except _github.GithubError as exc:
        if exc.status != 404:
            raise
    if parent:
        commit = app._call(repo, "GET", f"{path}/commits/{parent}")
        owner = json.loads(commit["message"])
        if owner.get("profile") != profile.name or owner.get("scope") != f"{PROJECT}/{DOMAIN}":
            raise _deploy.DeployError("unrecognized deployer lock; operator resolution required")
        if owner["run"] == run_name:
            return True
        holder = remote.Run.get(name=owner["run"])
        if str(holder.phase).split(".")[-1] not in _TERMINAL:
            _say(f"lease held by {owner['run']}")
            return False
    # The lock commit must be made of objects its own repository has: the deployed commit is not in a
    # lock repo that is not the deploy repo, so that repo's own head stands in.
    anchor = sha if repo == _deploy_repo(profile) else app.default_branch_sha(repo)[1]
    source = app._call(repo, "GET", f"{path}/commits/{anchor}")
    commit = app._call(
        repo,
        "POST",
        f"{path}/commits",
        json={
            "message": json.dumps({"run": run_name, "profile": profile.name, "scope": f"{PROJECT}/{DOMAIN}"}),
            "tree": source["tree"]["sha"],
            "parents": [parent or anchor],
        },
    )
    try:
        if parent:
            app._call(repo, "PATCH", f"{path}/refs/{ref}", json={"sha": commit["sha"], "force": False})
        else:
            app._call(repo, "POST", f"{path}/refs", json={"ref": f"refs/{ref}", "sha": commit["sha"]})
    except _github.GithubError as exc:
        if exc.status not in (409, 422):
            raise
        # Only contention is a stand-down; permissions/validation failures must fail the run.
        current = app._call(repo, "GET", f"{path}/ref/{ref}")
        if current["object"]["sha"] == (parent or commit["sha"]):
            raise
        _say("lease taken by a concurrent deploy_pair")
        return False
    return True


async def _admitted(profile: _profile.Profile, app: _github.GithubApp, run_name: str, sha: str) -> bool:
    if not RUN_LISTING_CONSISTENT:
        return await asyncio.to_thread(_acquire_ref, profile, app, run_name, sha)
    await asyncio.sleep(SETTLE)
    # Include every live pair, not just this SHA: moving heads cannot deploy concurrently.
    runs = await _runs(profile, in_phase=_LIVE, sort_by=("created_at", "asc"), limit=1000000)
    if not any(run.name == run_name for run in runs):
        raise _deploy.DeployError(f"deploy run {run_name} is missing from the live listing")
    if runs[0].name != run_name:
        _say(f"lease held by {runs[0].name}")
        return False
    return True


def _direction_profile(profile: _profile.Profile, app: _github.GithubApp, sha: str) -> _profile.Profile:
    """Return the profile at `sha` of the direction repo.

    Never falls back: an unreadable one fails the deploy.
    """
    # Only called after the caller has checked profile.direction.
    direction = cast(_profile.Direction, profile.direction)
    repo = _github.parse_repo(direction.repo)
    with _deploy.fetched_tree(_github.clone_url(repo, app.installation_token(repo)), sha) as tree:
        try:
            # Unknown top-level keys may be the deployed engine's: `_deploy_sha` has it check them.
            shipped = _profile.load_from_direction(tree, direction.profile, tolerate_unknown=True)
        except _profile.ProfileError as exc:
            raise _deploy.DeployError(f"{direction.profile} at {sha} of {repo} is not usable: {exc}") from exc
    if shipped.name != profile.name:  # the release history and deployed envs are keyed on it
        raise _deploy.DeployError(
            f"{direction.profile} at {sha} of {repo} is profile {shipped.name!r}, not {profile.name!r}"
        )
    same = shipped.direction is not None and (
        _github.parse_repo(shipped.direction.repo) == repo
        and (shipped.direction.profile, shipped.direction.ref) == (direction.profile, direction.ref)
    )  # the repo by owner/name, so an https spelling of the same repo is the same [direction]
    if not same:
        # The deployer redeploys itself with this profile: without the same [direction] the next deployer
        # would stop reading (or read another) direction repo, silently. Changing it is a hand deploy.
        raise _deploy.DeployError(
            f"{direction.profile} at {sha} of {repo} must keep [direction] as deployed ({direction.repo}, "
            f"{direction.profile}, ref {direction.ref or 'default'}); change it with a hand deploy"
        )
    _say(f"profile: {direction.profile} from {repo} at {sha}")
    captured = _standards.decode(shipped.standards_payload)
    if captured:
        _say(f"direction standards captured: {len(captured)} files from {repo}, {direction.profile} at {sha}")
    return shipped


def _deploy_sha(
    profile: _profile.Profile, app: _github.GithubApp, repo: str, sha: str, direction_sha: str = ""
) -> tuple[list[str], _profile.Profile, dict[str, bool | str | None]]:
    """Deploy `sha` of `repo`: slots, factory, then apps.

    Return its environments, profile and standards diagnostics.

    With `[direction]` the profile is the one at `direction_sha` of the direction repo. Otherwise it is
    the merged tree's `profiles/<name>.toml` when it has one, else `profile`, the one this
    pod started with: the children take their profile text from here, so a pod profile alone would
    ship every later change to it (and to the standards) only by hand, and the deployer would
    redeploy itself with stale text. Raises on the first stage that fails.

    This process is the previous release. It reads the profile only for what it acts on itself (the
    name and `[direction]`; every table it knows is still validated) and lets through top-level keys
    it does not know. The tree's own code (`_deploy_child`) then reads the profile strictly, and
    decides and deploys every environment: a key released with the code that reads it deploys in one
    step, and a key that code does not know either (a typo) fails the check, naming it, before
    anything is deployed.
    """
    # The children import and run the deployed code: they get neither the App key nor a token.
    credentials = _pull_requests.credential_env_vars(profile.github.private_key_secret, os.environ)
    base = {k: v for k, v in os.environ.items() if k not in credentials}
    with _deploy.fetched_tree(_clone_url(app, repo), sha) as tree:
        if profile.direction:
            # The direction's profile carries its own standards, captured from the direction repository: the
            # children must not capture them again from the engine tree.
            text = _direction_profile(profile, app, direction_sha).deployment_text()
            where, standards = f"{profile.direction.profile} at {direction_sha}", None
        else:
            try:
                shipped = _profile.load_from_tree(tree, profile.name, tolerate_unknown=True)
            except _profile.ProfileError as exc:
                raise _deploy.DeployError(
                    f"profiles/{profile.name}.toml at {sha} is not usable: {exc}"
                ) from exc
            if shipped:
                _say(f"profile: profiles/{profile.name}.toml from {sha}")
                text, where, standards = shipped.text, f"profiles/{profile.name}.toml at {sha}", tree
            else:
                _say(f"no profiles/{profile.name}.toml in {sha}; using the deployed profile")
                # It already carries its standards: they are not the tree's.
                text, where, standards = profile.deployment_text(), "the deployed profile", None
        src = tree / "src"
        diagnostics: dict[str, bool | str | None] = {
            "configured": None,
            "warning": "standards metadata unavailable (older engine)",
        }
        planned = _deploy.check_in_tree(
            src, text, where, tree=standards, base_env=base, diagnostics=diagnostics
        )
        _say(f"the engine at {sha} accepts {where}; deploying {', '.join(planned)}")
        deployed = _deploy.deploy_in_tree(
            src, text, where, tree=standards, project=PROJECT, domain=DOMAIN, ref=sha, base_env=base
        )
    missing = sorted(set(planned) - set(deployed))
    if missing:  # a success must not claim an environment that was not deployed
        raise _deploy.DeployError(f"deploy of {sha} did not deploy {', '.join(missing)}")
    return deployed, _profile.parse(text, where, tolerate_unknown=True), diagnostics


#: The whole dispatch wake, app discovery included: a stalled control-plane read must not hold the deploy.
WAKE_TIMEOUT_S = 15


#: What a deploy step raises: control-plane and GitHub errors, git failing, and a refusal (a SystemExit).
_DEPLOY_ERRORS = (*REMOTE_ERRORS, subprocess.SubprocessError, _deploy.Refused)

_Ts = TypeVarTuple("_Ts")
_T = TypeVar("_T")


def _on_daemon_thread(fn: Callable[[Unpack[_Ts]], _T], *args: Unpack[_Ts]) -> asyncio.Future[_T]:
    """Run `fn(*args)` on a daemon thread, awaitable.

    Unlike `asyncio.to_thread`, a call that outlives its timeout cannot hold the deployer pod open:
    interpreter exit waits for executor threads, not daemons.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def settle(callback: Callable[[], object]) -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:  # the loop closed after the wake gave up on this call
            pass

    def run() -> None:
        try:
            result = fn(*args)
        # Anything else is a bug: the thread's excepthook shows it and the awaiting wake times out.
        except REMOTE_ERRORS as exc:
            error = exc  # `exc` is unbound when the except block ends; the callback runs later
            settle(lambda: future.done() or future.set_exception(error))  # type: ignore[func-returns-value]  # Short-circuit skips completed futures.
        else:
            settle(lambda: future.done() or future.set_result(result))  # type: ignore[func-returns-value]  # Short-circuit skips completed futures.

    threading.Thread(target=run, name="dispatch-wake", daemon=True).start()
    return future


async def _wake_dispatch(profile: _profile.Profile) -> None:
    try:
        if profile.planner.admission == "planner":
            from . import _planner_runtime

            key = os.environ[_deploy.GITHUB_KEY_ENV]
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                await _on_daemon_thread(_planner_runtime.notify, profile, key)
        else:
            async with asyncio.timeout(WAKE_TIMEOUT_S):
                await _submit("factory.sweep")
    except (*REMOTE_ERRORS, TimeoutError) as exc:
        _say(f"dispatch wake failed ({type(exc).__name__}); the safety net will retry")


async def run_once(
    profile: _profile.Profile, run_name: str, app: _github.GithubApp | None, now: float | None = None
) -> str:
    """Resolve current heads, check CI and submit one labelled attempt."""
    if not profile.deploy.auto:
        return _say("off: [deploy] auto is false in the profile")
    if app is None:
        raise _deploy.DeployError("[deploy] auto needs the GitHub App")
    try:
        heads = _heads(profile, app)
        _, sha, _, direction_sha = heads
        shown = _shown(sha, direction_sha)
        release = await deployed_pair(profile)
        if release and release[1] == (sha, direction_sha):
            return _say(f"up to date at {shown}")
        _lock_repo(profile, app)  # refuse here, where the owner reads it, rather than in the deploy run
        refused = await _not_deployable(profile, app, heads, release and release[0])
        if refused:
            return _say(refused)
        if await _runs(profile, in_phase=_LIVE, limit=1):
            return _say("deploy in flight")
        labels = _pair_labels(profile, sha, direction_sha)
        waiting = await _read_secrets_last_waited_for(profile, labels)
        run = await _submit(_deploy.DEPLOY_PAIR, labels=labels, sha=sha, direction_sha=direction_sha)
        # Still an attempt: it deploys as soon as the owner has created what the last one named.
        note = f"; waiting for secrets: {', '.join(sorted(waiting))}" if waiting else ""
        return _say(f"submitted {shown}: {run.name}{note}")
    except _DEPLOY_ERRORS as exc:  # task boundary redacts remote errors
        raise _deploy.DeployError(_deploy.redact(str(exc))) from None


async def run_pair(
    profile: _profile.Profile, app: _github.GithubApp, run_name: str, sha: str, direction_sha: str
) -> DeployOutput:
    """Deploy the pair this run was submitted for, then follow heads that moved meanwhile.

    While this run is live every tick says "deploy in flight", so a push that lands during the
    deploy is this run's to pick up, holding the lock: up to MAX_DEPLOYS_PER_RUN deploys, each
    after the tick's own checks. A failure in a follow-up fails the run, so its earlier deploy is
    not recorded and the next tick redeploys the current heads; that errs towards deploying.
    """
    shown = _shown(sha, direction_sha)
    try:
        # Re-resolve before using attempt inputs. A queued attempt from before a rename or newer
        # push must never query CI or fetch source at a stale commit under the current repository.
        heads = _heads(profile, app)
        repo = heads[0]
        if (sha, direction_sha) != (heads[1], heads[3]):
            _say(f"{shown} is no longer the head; standing down")
            # Best effort: the tick may still see this run live; the periodic reconcile backs it.
            try:
                await _submit(_deploy.DEPLOYER_TASK)
            except REMOTE_ERRORS as exc:  # the periodic tick recovers
                _say(f"could not submit follow-up tick: {_deploy.redact(str(exc))}")
            return {"deployed": False}
        if not await _admitted(profile, app, run_name, sha):
            return {"deployed": False}
        # An older pair for the same heads may have deployed and finished before this one was
        # placed; admission only says no other deploy is live.
        release = await deployed_pair(profile)
        if release and release[1] == (sha, direction_sha):
            _say(f"up to date at {shown}")
            return {"deployed": False}
        previous_slots = {s.env for s in profile.slots}
        if release:
            (previous,) = await release[0].outputs.aio()
            # Older releases did not record slots; the pod profile is their deployed configuration.
            previous_slots = set(previous.get("slots", previous_slots))
        output: DeployOutput = {"deployed": False}
        for deploy in range(MAX_DEPLOYS_PER_RUN):
            # Every deploy, the first included, is judged on CI as it is now: the tick that queued this
            # run may have seen green minutes ago (lock wait, queue time).
            heads = _heads(profile, app)
            if deploy:
                if (heads[1], heads[3]) == (sha, direction_sha):
                    break
                sha, direction_sha = heads[1], heads[3]
                shown = _shown(sha, direction_sha)
            elif (heads[1], heads[3]) != (sha, direction_sha):
                # Moved while waiting for the lock; the next tick judges the new heads.
                _say(f"{shown} is no longer the head; leaving it for the next tick")
                break
            refused = await _not_deployable(profile, app, heads, release and release[0])
            if refused:
                _say(f"{refused}; leaving it for the next tick")
                break
            try:
                envs, deployed_profile, standards = await asyncio.to_thread(
                    _deploy_sha, profile, app, repo, sha, direction_sha
                )
            except _deploy.MissingSecrets as exc:
                # Nothing was deployed and the release is fine: not a failed attempt (which counts against
                # MAX_ATTEMPTS) and not a release. The next tick resubmits, and deploys once they exist.
                _say(str(exc))
                return {**output, "waiting_for_secrets": exc.secrets}
            slots = {s.env for s in deployed_profile.slots}
            if slots != previous_slots:
                await _wake_dispatch(deployed_profile)
            previous_slots = slots
            _say(f"deployed {shown}: {', '.join(envs)}")
            output = {
                "deployed": True,
                "envs": envs,
                "slots": sorted(slots),
                "sha": sha,
                "direction_sha": direction_sha,
                "standards": standards,
            }
        return output
    except _DEPLOY_ERRORS as exc:  # task boundary redacts remote errors
        raise _deploy.DeployError(f"deploy {shown} failed: {_deploy.redact(str(exc))}") from None


def _app() -> _github.GithubApp:
    if _deploy.GITHUB_KEY_ENV not in os.environ:
        raise _deploy.DeployError(f"[deploy] auto needs the {_deploy.GITHUB_KEY_ENV} secret mounted")
    return _github.GithubApp(PROFILE.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])


def _report_html(message: str) -> str:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"<h3>factory-deployer</h3><p>{html.escape(message)}</p><p><small>{stamp}</small></p>"


def _describe_outcome(output: DeployOutput) -> str:
    """Describe an attempt for its report: what it deployed, or which secrets it is waiting for."""
    deployed = f"deployed {', '.join(output['envs'])}" if output.get("deployed") else "nothing deployed"
    warning = output.get("standards", {}).get("warning")
    if warning:
        deployed += f"; {warning}"
    waiting = output.get("waiting_for_secrets")
    if not waiting:
        return deployed
    needs = "; ".join(f"{name} (needed by {', '.join(envs)})" for name, envs in sorted(waiting.items()))
    return f"{deployed}; waiting for secrets, create them with `flyte create secret <name>`: {needs}"


@env.task(
    report=True,
    timeout=flyte.Timeout(max_runtime=MAX_RUNTIME, max_queued_time=timedelta(minutes=5)),
)
async def deploy_pair(sha: str, direction_sha: str) -> dict[str, Any]:
    # Flyte serializes TypedDict through Pydantic, adding defaults for absent keys. Keep its
    # existing untyped STRUCT wire format here; run_pair carries the precise shape internally.
    # Remote tasks always have a run name.
    try:
        output = await run_pair(PROFILE, _app(), cast(str, flyte.ctx().action.run_name), sha, direction_sha)
    except Exception as exc:
        await flyte.report.replace.aio(_report_html(f"failed: {_deploy.redact(str(exc))}"), do_flush=True)
        raise
    await flyte.report.replace.aio(_report_html(_describe_outcome(output)), do_flush=True)
    return cast(dict[str, Any], output)


@env.task(
    report=True,
    # The SDK accepts lists at runtime, but its annotation only admits tuples.
    triggers=cast(tuple[flyte.Trigger, ...], [TRIGGER]),
    timeout=flyte.Timeout(max_runtime=MAX_RUNTIME, max_queued_time=timedelta(minutes=5)),
)
async def deploy_if_changed() -> str:
    """Deploy the factory repo's default branch if it moved since the last deploy."""
    # This task only runs within a named remote action.
    run_name = cast(str, flyte.ctx().action.run_name)
    try:
        app = None
        if PROFILE.deploy.auto:
            app = _app()
        message = await run_once(PROFILE, run_name, app)
    except Exception as exc:
        await flyte.report.replace.aio(_report_html(f"failed: {_deploy.redact(str(exc))}"), do_flush=True)
        raise
    await flyte.report.replace.aio(_report_html(message), do_flush=True)
    return message
