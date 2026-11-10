"""A userspace tailnet proxy owned by the in-pod browser, never the session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from . import _names, _profile
else:
    from flyte_agent_factory import _names

SOCKS_PORT = 1055


def credential_env_vars(secret: str, env: Mapping[str, str]) -> tuple[str, ...]:
    if not secret:
        return ()
    name = secret.replace("-", "_").upper()
    if TYPE_CHECKING:
        from ._pull_requests import prefixed_key_env_var
    else:
        from flyte_agent_factory._pull_requests import prefixed_key_env_var

    return (name, prefixed_key_env_var(secret, env))


def settings() -> dict[str, str]:
    if TYPE_CHECKING:
        from . import _profile
    else:
        from flyte_agent_factory import _profile

    profile = _profile.current()
    defaults = deploy_env(profile)
    node = _names.getenv(
        "AGENT_FACTORY_BROWSER_EXIT_NODE", defaults.get("AGENT_FACTORY_BROWSER_EXIT_NODE", "")
    )
    secret = os.environ.get(
        "AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", defaults.get("AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", "")
    )
    return {"exit_node": node, "tailscale_secret": secret}


def deploy_env(profile: _profile.Profile | None) -> dict[str, str]:
    if not profile or not profile.browser.tailscale_secret:
        return {}
    return {
        "AGENT_FACTORY_BROWSER_EXIT_NODE": profile.browser.exit_node,
        "AGENT_FACTORY_BROWSER_TAILSCALE_SECRET": profile.browser.tailscale_secret,
    }


def _secret(name: str) -> str:
    # Flyte's file mount avoids putting the auth key in the pod/session environment.
    value = (Path("/etc/flyte/secrets") / name).read_text().strip()
    if not value:
        raise RuntimeError("browser Tailscale auth key is empty")
    return value


def _child_env() -> dict[str, str]:
    # Scrubbed: the daemon and CLI need no secret from the environment (the auth key goes by file, see
    # `start`), and the session's browser shares their pod. Imported here because `_secret_env` imports
    # this module for `credential_env_vars`.
    if TYPE_CHECKING:
        from ._secret_env import child_env
    else:
        from flyte_agent_factory._secret_env import child_env

    return child_env()


class _ExitStatus(TypedDict, total=False):
    Online: bool


class _Status(TypedDict, total=False):
    BackendState: str
    ExitNodeStatus: _ExitStatus


class BrowserExitNode:
    def __init__(self, exit_node: str, secret: str) -> None:
        self.exit_node = exit_node
        self.secret = secret
        self._temp = tempfile.TemporaryDirectory(prefix="browser-tailnet-")
        self.directory = Path(self._temp.name)
        self._socket = self.directory / "tailscaled.sock"
        self._daemon: asyncio.subprocess.Process | None = None

    async def _spawn(self, argv: list[str]) -> None:
        self._daemon = await asyncio.create_subprocess_exec(
            *argv, env=_child_env(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )

    async def _command(self, *args: str) -> _Status:
        proc = await asyncio.create_subprocess_exec(
            "tailscale",
            f"--socket={self._socket}",
            *args,
            env=_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=35)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            # Authentication errors can echo credentials; keep subprocess output private.
            raise RuntimeError("browser Tailscale command failed")
        try:
            # tailscale status --json supplies these fields; other commands have no JSON output.
            return cast(_Status, json.loads(output)) if "--json" in args else {}
        except ValueError:
            raise RuntimeError("browser Tailscale returned invalid status") from None

    async def start(self) -> None:
        if TYPE_CHECKING:
            from ._local_browser import _wait
        else:
            from flyte_agent_factory._local_browser import _wait

        await self._spawn(
            [
                "tailscaled",
                "--tun=userspace-networking",
                "--state=mem:",
                f"--statedir={self.directory}",
                f"--socket={self._socket}",
                f"--socks5-server=127.0.0.1:{SOCKS_PORT}",
                "--no-logs-no-support",
            ]
        )
        if not await _wait(lambda: self._socket.exists(), 15):
            raise RuntimeError("browser tailscaled did not create its socket")
        auth = self.directory / "auth-key"
        try:
            auth.touch(mode=0o600)
            auth.write_text(_secret(self.secret))
            await self._command(
                "up",
                f"--auth-key=file:{auth}",
                "--accept-dns=false",
                "--accept-routes=false",
                f"--hostname=factory-browser-{os.urandom(6).hex()}",
                "--timeout=30s",
            )
        finally:
            auth.unlink(missing_ok=True)
        # A fresh client cannot resolve a tailnet hostname until it has joined.
        # Let the CLI resolve names rather than duplicating its peer matching rules.
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            try:
                await self._command(
                    "set", f"--exit-node={self.exit_node}", "--exit-node-allow-lan-access=false"
                )
                break
            except RuntimeError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError("browser exit node could not be selected") from None
                await asyncio.sleep(0.5)
        status = await self._command("status", "--json")
        if status.get("BackendState") != "Running" or not (status.get("ExitNodeStatus") or {}).get("Online"):
            raise RuntimeError("browser exit node is unavailable; continuing without browser")

    async def stop(self) -> None:
        if self._daemon is not None and self._daemon.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._daemon.terminate()
            try:
                await asyncio.wait_for(self._daemon.wait(), 10)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self._daemon.kill()
                await self._daemon.wait()
        self._temp.cleanup()
