"""Replace semantics of an app deploy (`_deploy_apps.deploy_app`), against a fake control plane. Offline.

The personal browser holds a lease its old pod keeps renewing, so a changed app must stop before it starts.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyte.remote import _app as sdk_app
from flyteidl2.app import app_definition_pb2 as pb  # type: ignore[import-untyped]  # no py.typed marker

from flyte_agent_factory import _deploy, _deploy_apps

Status = pb.Status.DeploymentStatus
NAME = "claude-browser-personal"


def make_spec(image: str) -> Any:
    spec = pb.Spec(desired_state=pb.Spec.DESIRED_STATE_ACTIVE)
    spec.ingress.private = False
    spec.creator.SetInParent()  # `App.replace` copies the deployed one's `creator` over the new spec's
    spec.container.image = image
    return spec


class ControlPlane:
    """One app on a fake server: `deploy`, and the `remote.App` class (`get`, `replace`, ...)."""

    def __init__(self, spec: Any | None, *, status: Any = Status.DEPLOYMENT_STATUS_ACTIVE):
        self.spec, self.status = spec, status
        self.conditions: list[Any] = []
        self.calls: list[str] = []
        self.after_deploy: tuple[Any, list[str]] = (Status.DEPLOYMENT_STATUS_ACTIVE, [])
        self.build_error: Exception | None = None
        self.replace_error: Exception | None = None
        self.now = 0.0

    # flyte.deploy
    def deploy(self, env: Any, dry_run: bool = False) -> list[Any]:
        self.calls.append("build" if dry_run else "deploy")
        if dry_run and self.build_error:
            raise self.build_error
        planned = SimpleNamespace(
            get_name=lambda: env.name, deployed_app=SimpleNamespace(pb2=SimpleNamespace(spec=env.spec))
        )
        if not dry_run:
            self.spec = env.spec
            self.status, messages = self.after_deploy
            self.conditions = [SimpleNamespace(message=m, deployment_status=self.status) for m in messages]
        return [SimpleNamespace(envs={env.name: planned})]

    # remote.App
    def get(self, name: str) -> Any:
        self.calls.append("get")
        if self.spec is None:
            raise ConnectError(Code.NOT_FOUND, "no such app")
        return Handle(self)

    def replace(self, *, name: str, updated_app_spec: Any, reason: str) -> None:
        self.calls.append("replace")
        if self.replace_error:
            raise self.replace_error
        self.spec = updated_app_spec

    async def _app_specs_are_equal(self, old: Any, new: Any) -> bool:
        return bool(old == new)

    # time
    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def clock(self) -> float:
        return self.now


class Handle:
    """What `App.get` returns: a snapshot of the server's app."""

    def __init__(self, server: ControlPlane):
        self.server = server
        self.status = server.status
        self.pb2 = SimpleNamespace(
            spec=server.spec, status=SimpleNamespace(conditions=list(server.conditions))
        )

    @property
    def deployment_status(self) -> Any:
        return self.status

    # The SDK's own predicates, so the fake cannot drift from them.
    def is_active(self) -> bool:
        return bool(sdk_app._is_active(self.status))

    def is_deactivated(self) -> bool:
        return bool(sdk_app._is_deactivated(self.status))

    def deactivate(self, wait: bool = False) -> None:
        if self.is_deactivated():  # as the SDK's: stopping a stopped app is a no-op
            return
        self.server.calls.append("deactivate")
        if wait and self.status == Status.DEPLOYMENT_STATUS_FAILED:
            # The SDK's watch("deactivated") gives up on the first FAILED condition.
            raise RuntimeError(f"App deployment for app {NAME} has failed!")
        self.server.status = Status.DEPLOYMENT_STATUS_STOPPED

    def activate(self, wait: bool = False) -> None:
        self.server.calls.append("activate")
        self.server.status = Status.DEPLOYMENT_STATUS_ACTIVE


def run(server: ControlPlane, new_image: str = "v2", **kw: Any) -> list[Any]:
    env = SimpleNamespace(name=NAME, spec=make_spec(new_image))
    return _deploy_apps.deploy_app(
        env, deploy=server.deploy, app_cls=server, sleep=server.sleep, clock=server.clock, **kw
    )


def steps(server: ControlPlane) -> list[str]:
    return [c for c in server.calls if c != "get"]


def test_an_unchanged_app_is_not_stopped_and_still_counts_as_deployed(capsys):
    server = ControlPlane(make_spec("v1"))
    deployments = run(server, "v1")
    assert steps(server) == ["build"]
    assert f"{NAME} unchanged" in capsys.readouterr().out
    assert [d.get_name() for dep in deployments for d in dep.envs.values()] == [NAME]


def test_a_changed_app_is_built_then_stopped_then_deployed_and_waited_for():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (Status.DEPLOYMENT_STATUS_ACTIVE, [])
    deployments = run(server)
    assert steps(server) == ["build", "deactivate", "deploy"]
    assert server.spec == make_spec("v2") and server.status == Status.DEPLOYMENT_STATUS_ACTIVE
    assert [d.get_name() for dep in deployments for d in dep.envs.values()] == [NAME]


def test_an_unchanged_spec_on_a_stopped_app_is_redeployed():
    server = ControlPlane(make_spec("v1"), status=Status.DEPLOYMENT_STATUS_STOPPED)
    run(server, "v1")
    assert steps(server) == ["build", "deploy"]  # already stopped: deactivating is a no-op in the SDK


def test_the_wait_polls_until_the_app_is_active():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (Status.DEPLOYMENT_STATUS_PENDING, [])
    polls = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        server.now += seconds
        if len(polls) == 3:
            server.status = Status.DEPLOYMENT_STATUS_ACTIVE

    env = SimpleNamespace(name=NAME, spec=make_spec("v2"))
    _deploy_apps.deploy_app(env, deploy=server.deploy, app_cls=server, sleep=sleep, clock=server.clock)
    assert polls == [_deploy_apps.POLL_SECONDS] * 3 and "replace" not in server.calls


def test_a_build_failure_touches_nothing_that_is_running():
    server = ControlPlane(make_spec("v1"))
    server.build_error = RuntimeError("image build failed")
    with pytest.raises(RuntimeError, match="image build failed"):
        run(server)
    assert server.calls == ["build"]
    assert server.spec == make_spec("v1") and server.status == Status.DEPLOYMENT_STATUS_ACTIVE


def test_an_app_that_fails_to_start_is_rolled_back_with_its_condition_messages():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (
        Status.DEPLOYMENT_STATUS_FAILED,
        ["pulling image", "lease held by another pod", "lease held by another pod", "startup failed"],
    )
    with pytest.raises(_deploy.DeployError) as caught:
        run(server)
    message = str(caught.value)
    assert message.startswith(f"{NAME} failed to start: ")
    assert "pulling image; lease held by another pod; startup failed" in message
    assert message.endswith("; restored the previous spec")
    assert steps(server) == ["build", "deactivate", "deploy", "replace", "activate"]
    assert server.spec == make_spec("v1")


def test_an_app_that_never_becomes_active_times_out_and_is_rolled_back():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (Status.DEPLOYMENT_STATUS_PENDING, ["waiting for a pod"])
    with pytest.raises(
        _deploy.DeployError, match=f"{NAME} did not become active in 900 s: waiting for a pod"
    ):
        run(server)
    assert steps(server)[-2:] == ["replace", "activate"] and server.now >= 900


def test_condition_messages_are_redacted():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (
        Status.DEPLOYMENT_STATUS_FAILED,
        ["clone of https://u:hunter2@example.org/x failed"],
    )
    with pytest.raises(_deploy.DeployError) as caught:
        run(server)
    assert "hunter2" not in str(caught.value) and "https://u:***@example.org" in str(caught.value)


def test_a_failed_rollback_is_reported_too():
    server = ControlPlane(make_spec("v1"))
    server.after_deploy = (Status.DEPLOYMENT_STATUS_FAILED, ["startup failed"])
    server.replace_error = RuntimeError("control plane down")
    with pytest.raises(_deploy.DeployError, match="startup failed; restoring the previous spec failed too"):
        run(server)


def test_a_deploy_that_raises_after_the_stop_is_rolled_back():
    server = ControlPlane(make_spec("v1"))

    def deploy(env: Any, dry_run: bool = False) -> list[Any]:
        if dry_run:
            return ControlPlane.deploy(server, env, dry_run=True)
        raise RuntimeError("upload failed")

    env = SimpleNamespace(name=NAME, spec=make_spec("v2"))
    with pytest.raises(
        _deploy.DeployError, match="deploy failed \\(RuntimeError: upload failed\\); restored"
    ):
        _deploy_apps.deploy_app(env, deploy=deploy, app_cls=server, sleep=server.sleep, clock=server.clock)
    assert steps(server) == ["build", "deactivate", "replace", "activate"]


def test_the_first_deploy_has_nothing_to_stop_or_restore():
    server = ControlPlane(None)

    def deploy(env: Any, dry_run: bool = False) -> list[Any]:
        result = ControlPlane.deploy(server, env, dry_run=dry_run)
        server.status = Status.DEPLOYMENT_STATUS_ACTIVE
        return result

    env = SimpleNamespace(name=NAME, spec=make_spec("v2"))
    _deploy_apps.deploy_app(env, deploy=deploy, app_cls=server, sleep=server.sleep, clock=server.clock)
    assert steps(server) == ["build", "deploy"]


def test_a_first_deploy_that_fails_to_start_says_why_and_restores_nothing():
    server = ControlPlane(None)
    server.after_deploy = (Status.DEPLOYMENT_STATUS_FAILED, ["no such secret"])
    with pytest.raises(_deploy.DeployError, match=f"^{NAME} failed to start: no such secret$"):
        run(server)
    assert "replace" not in server.calls


def test_a_lookup_that_is_not_not_found_is_not_a_first_deploy():
    server = ControlPlane(make_spec("v1"))

    def get(name: str) -> Any:
        raise ConnectError(Code.PERMISSION_DENIED, "no")

    server.get = get  # type: ignore[method-assign]
    with pytest.raises(ConnectError):
        run(server)
    assert steps(server) == ["build"]


def test_a_dry_run_only_builds():
    server = ControlPlane(make_spec("v1"))
    run(server, dry_run=True)
    assert server.calls == ["build"]


# --- an app the profile no longer enables is stopped -------------------------------------------


def test_an_app_the_profile_dropped_is_stopped():
    server = ControlPlane(make_spec("v1"))
    assert _deploy_apps.retire_apps([NAME], app_cls=server) == [NAME]
    assert steps(server) == ["deactivate"]


class Fleet:
    """A `remote.App` with several apps, by name: `listall` and `get`, recording what was stopped."""

    def __init__(self, *names: str):
        self.names = list(names)
        self.stopped: list[str] = []

    def listall(self, limit: int = 100) -> list[Any]:
        return [SimpleNamespace(name=n) for n in self.names]

    def get(self, name: str) -> Any:
        fleet = self
        return SimpleNamespace(
            is_deactivated=lambda: False, deactivate=lambda wait=False: fleet.stopped.append(name)
        )


def test_an_identity_dropped_from_the_profile_is_stopped_and_the_shared_browser_is_not():
    fleet = Fleet("claude-browser", "claude-browser-github", "claude-browser-linkedin", "factory-webhook")
    enabled = frozenset({"claude-browser-github"})
    extra = _deploy_apps.deployed_personal_apps(app_cls=fleet) - _deploy.APPS - enabled
    assert extra == {"claude-browser-linkedin"}
    assert _deploy_apps.retire_apps(extra, app_cls=fleet) == ["claude-browser-linkedin"]
    assert fleet.stopped == ["claude-browser-linkedin"]  # stopped, never deleted


def test_the_shared_browser_is_never_a_personal_app():
    fleet = Fleet("claude-browser", "claude-browser-personal", "claude-browserx")
    assert _deploy_apps.deployed_personal_apps(app_cls=fleet) == {"claude-browser-personal"}


@pytest.mark.parametrize(
    "server",
    [
        ControlPlane(None),
        ControlPlane(make_spec("v1"), status=Status.DEPLOYMENT_STATUS_STOPPED),
        ControlPlane(make_spec("v1"), status=Status.DEPLOYMENT_STATUS_UNASSIGNED),
    ],
    ids=["missing", "stopped", "unassigned"],
)
def test_stopping_an_app_that_does_not_exist_or_is_already_stopped_does_nothing(server):
    assert _deploy_apps.retire_apps([NAME], app_cls=server) == []
    assert steps(server) == []


def test_a_dry_run_stops_nothing(capsys):
    server = ControlPlane(make_spec("v1"))
    assert _deploy_apps.retire_apps([NAME], dry_run=True, app_cls=server) == []
    assert steps(server) == []
    assert f"would stop {NAME}" in capsys.readouterr().out


def test_a_stop_that_fails_names_the_app_and_is_redacted():
    class Broken(ControlPlane):
        def get(self, name: str) -> Any:
            raise ConnectError(Code.UNAVAILABLE, "fetch https://u:hunter2@x.org failed")

    with pytest.raises(_deploy.DeployError) as caught:
        _deploy_apps.retire_apps([NAME], app_cls=Broken(make_spec("v1")))
    assert NAME in str(caught.value) and "***" in str(caught.value) and "hunter2" not in str(caught.value)


@pytest.mark.parametrize(
    "status",
    [
        Status.DEPLOYMENT_STATUS_FAILED,
        Status.DEPLOYMENT_STATUS_PENDING,
        Status.DEPLOYMENT_STATUS_DEPLOYING,
        Status.DEPLOYMENT_STATUS_ASSIGNED,
        Status.DEPLOYMENT_STATUS_SCALING_UP,
        Status.DEPLOYMENT_STATUS_STARTED,
    ],
)
def test_an_app_the_profile_dropped_is_stopped_whatever_state_it_is_in(status):
    server = ControlPlane(make_spec("v1"), status=status)
    assert _deploy_apps.retire_apps([NAME], app_cls=server) == [NAME]
    assert steps(server) == ["deactivate"]


def test_a_stop_lookup_error_that_is_not_not_found_is_a_deploy_error():
    class Unreachable(ControlPlane):
        def get(self, name: str) -> Any:
            raise httpx.ConnectError("connection refused")

    with pytest.raises(_deploy.DeployError, match=NAME):
        _deploy_apps.retire_apps([NAME], app_cls=Unreachable(make_spec("v1")))


def test_a_failed_app_is_replaced_not_rolled_back():
    server = ControlPlane(make_spec("v1"), status=Status.DEPLOYMENT_STATUS_FAILED)
    run(server)
    assert steps(server) == ["build", "deactivate", "deploy"]


def test_an_app_that_does_not_stop_in_time_is_rolled_back():
    class Stuck(ControlPlane):
        def get(self, name: str) -> Any:
            handle = super().get(name)
            server = self

            def deactivate(wait: bool = False) -> None:
                server.calls.append("deactivate")  # asked, but the controller never tears it down

            handle.deactivate = deactivate
            return handle

    server = Stuck(make_spec("v1"), status=Status.DEPLOYMENT_STATUS_DEPLOYING)
    with pytest.raises(_deploy.DeployError, match=r"did not stop in .*restored the previous spec"):
        run(server)
    assert steps(server) == ["build", "deactivate", "replace", "activate"]
