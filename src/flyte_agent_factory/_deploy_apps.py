"""Deploying an app that must be replaced, not rolled over (`deploy_app`), and stopping a dropped one.

Flyte's own `flyte.deploy` of an app updates it in place, and Knative starts the new revision while the old
one still runs. The personal browser holds a single-owner lease on its profile archive that the running pod
renews every 30 s and a new pod waits only 180 s for, so a rolling update can never become ready. The old
revision has to stop first, which is a brief gap in service; this module makes it the only way an app is
updated, and puts the previous spec back if the new one does not come up.

`_deploy.APPS_CHILD` calls it from the engine tree. It is a normal module, so it is tested with a fake
`flyte.remote.App`.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Iterable
from typing import Any

import flyte
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte import remote
from flyteidl2.app import app_definition_pb2  # type: ignore[import-untyped]  # no py.typed marker

from . import _deploy
from ._errors import REMOTE_ERRORS

#: Seconds between looks at an app that is starting, and how long it may take: an image pull and a lease wait
#: (180 s at most) fit well inside it.
POLL_SECONDS = 10
ACTIVE_TIMEOUT_SECONDS = 15 * 60
#: How long the old app may take to stop before a replacement: generous against the pod's 90 s grace.
STOP_TIMEOUT_SECONDS = 5 * 60

_FAILED = app_definition_pb2.Status.DeploymentStatus.DEPLOYMENT_STATUS_FAILED


class _NotReached(Exception):
    """The app did not reach the state waited for: why, as a sentence, and its latest condition messages."""

    def __init__(self, reason: str, messages: str):
        super().__init__(reason)
        self.reason, self.messages = reason, messages


def _latest_condition_messages(app: Any) -> str:
    """Return the last three distinct condition messages of `app`, redacted: why it is down."""
    distinct: list[str] = []
    for condition in app.pb2.status.conditions:
        if condition.message:
            distinct = [m for m in distinct if m != condition.message] + [condition.message]
    return "; ".join(_deploy.redact(m) for m in distinct[-3:])


def _describe_failure(exc: BaseException) -> str:
    if isinstance(exc, _NotReached):
        return f"{exc.reason}: {exc.messages}" if exc.messages else exc.reason
    return f"deploy failed ({type(exc).__name__}: {_deploy.redact(str(exc))})"


def _wait_until(
    reached: Callable[[Any], bool],
    get: Callable[[], Any],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    timeout: float,
    *,
    what: str,
    failed_is_fatal: bool,
) -> None:
    """Return when `reached(get())`; `_NotReached` if `timeout` passes, or on FAILED when that is fatal."""
    deadline = clock() + timeout
    app = None
    while True:
        try:
            app = get()
        except REMOTE_ERRORS:
            # A control-plane blip must not roll back a deploy under way; the deadline still ends this.
            pass
        else:
            if reached(app):
                return
            if failed_is_fatal and app.deployment_status == _FAILED:
                raise _NotReached("failed to start", _latest_condition_messages(app))
        if clock() >= deadline:
            messages = _latest_condition_messages(app) if app else ""
            raise _NotReached(f"did not {what} in {timeout:.0f} s", messages)
        sleep(POLL_SECONDS)


def _wait_active(
    get: Callable[[], Any], sleep: Callable[[float], None], clock: Callable[[], float], timeout: float
) -> None:
    """Return when the deployed app is active; `_NotReached` if it fails or `timeout` passes."""
    _wait_until(
        lambda app: app.is_active(), get, sleep, clock, timeout, what="become active", failed_is_fatal=True
    )


def _wait_stopped(
    get: Callable[[], Any], sleep: Callable[[float], None], clock: Callable[[], float], timeout: float
) -> None:
    """Return when the app is stopped; `_NotReached` if `timeout` passes.

    FAILED is not the end of it: a crash-looping app reports FAILED until the controller tears it down.
    """
    _wait_until(
        lambda app: app.is_deactivated(), get, sleep, clock, timeout, what="stop", failed_is_fatal=False
    )


def _roll_back(name: str, old_spec: Any, app_cls: Any) -> str:
    """Put `old_spec` back and start it, best effort; a sentence for the error."""
    try:
        app_cls.replace(name=name, updated_app_spec=old_spec, reason="restoring the previous spec")
        app_cls.get(name=name).activate(wait=False)
    except REMOTE_ERRORS as exc:  # the caller still has the deploy's own failure to report
        return f"restoring the previous spec failed too ({type(exc).__name__}: {_deploy.redact(str(exc))})"
    return "restored the previous spec"


def deploy_app(
    env: Any,
    *,
    dry_run: bool = False,
    deploy: Callable[..., Any] = flyte.deploy,
    app_cls: Any = remote.App,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = ACTIVE_TIMEOUT_SECONDS,
) -> list[Any]:
    """Deploy the app `env` with replace semantics; the deployments to record.

    The image is built (a dry run) before anything running is touched. A spec equal to the deployed one on
    an active app changes nothing. Otherwise the app is stopped, deployed and waited for; if it does not
    become active, the previous spec is restored and the error carries the app's own condition messages.
    Needs an initialised Flyte client.

    Raises DeployError when the new revision does not come up. A build failure (the dry run), a lookup error
    other than not-found, and a first deploy's remote error escape as the SDK raises them, and reach the
    stage's error through the child's stderr tail.
    """
    name = env.name
    planned = deploy(env, dry_run=True)  # a build failure raises here, with the old app still running
    if dry_run:
        return list(planned)
    new_spec = next(e.deployed_app.pb2.spec for d in planned for e in d.envs.values() if e.get_name() == name)

    try:
        old = app_cls.get(name=name)
    except ConnectError as exc:
        if exc.code != Code.NOT_FOUND:
            raise
        old = None

    if old is None:
        deployments = deploy(env)
        try:
            _wait_active(lambda: app_cls.get(name=name), sleep, clock, timeout)
        except _NotReached as exc:
            raise _deploy.DeployError(f"{name} {_describe_failure(exc)}") from None
        return list(deployments)

    # `App.replace` compares specs this way: the creator is the old one's, whoever deploys.
    comparable = copy.deepcopy(new_spec)
    comparable.creator.CopyFrom(old.pb2.spec.creator)
    if old.is_active() and asyncio.run(app_cls._app_specs_are_equal(old.pb2.spec, comparable)):
        print(f"{name} unchanged", flush=True)
        return list(planned)

    old_spec = copy.deepcopy(old.pb2.spec)
    try:
        # Not the SDK's wait: its watch gives up on a FAILED condition, which a crash-looping app shows.
        old.deactivate(wait=False)
        _wait_stopped(lambda: app_cls.get(name=name), sleep, clock, STOP_TIMEOUT_SECONDS)
        deployments = deploy(env)
        _wait_active(lambda: app_cls.get(name=name), sleep, clock, timeout)
    except (_NotReached, *REMOTE_ERRORS) as exc:  # a failed stop, deploy or start: put the old one back
        raise _deploy.DeployError(
            f"{name} {_describe_failure(exc)}; {_roll_back(name, old_spec, app_cls)}"
        ) from None
    return list(deployments)


def stop_app(name: str, *, app_cls: Any = remote.App) -> bool:
    """Ask the app `name` to stop unless it is missing or already stopped; whether it was asked.

    Whatever state it is in otherwise (running, starting, failed), it is asked. Nothing waits for it: no
    deploy follows a retirement. Raises DeployError.
    """
    try:
        app = app_cls.get(name=name)
        if app.is_deactivated():
            return False
        app.deactivate(wait=False)
    except ConnectError as exc:
        if exc.code == Code.NOT_FOUND:
            return False
        raise _deploy.DeployError(
            f"{name} could not be stopped ({type(exc).__name__}: {_deploy.redact(str(exc))})"
        ) from None
    except REMOTE_ERRORS as exc:
        raise _deploy.DeployError(
            f"{name} could not be stopped ({type(exc).__name__}: {_deploy.redact(str(exc))})"
        ) from None
    return True


def deployed_personal_apps(*, app_cls: Any = remote.App) -> frozenset[str]:
    """Return the names of the deployed personal browser apps: `claude-browser-<x>`, never the shared one.

    An identity dropped from the profile leaves no trace in it, so the apps to stop are found on the server.
    """
    prefix = f"{_deploy.SHARED_BROWSER_APP}-"
    return frozenset(app.name for app in app_cls.listall(limit=1000) if app.name.startswith(prefix))


def retire_apps(names: Iterable[str], *, dry_run: bool = False, app_cls: Any = remote.App) -> list[str]:
    """Ask each app in `names`, which the profile does not enable, to stop; the names asked.

    Stopped, not deleted: a personal browser's profile archive stays, and enabling it again redeploys it.
    """
    stopped = []
    for name in sorted(names):
        if dry_run:
            print(f"would stop {name}: the profile does not enable it", flush=True)
        elif stop_app(name, app_cls=app_cls):
            print(f"{name}: stop requested, the profile does not enable it", flush=True)
            stopped.append(name)
    return stopped
