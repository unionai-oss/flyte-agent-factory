"""Chromium and the Playwright MCP server inside the agent's own pod.

The alternative is the shared browser app (`browser.py`), and this exists
because that app is one browser for every project at once. Playwright allows a
single client per persistent profile, so projects contend for it; a client that
disconnects badly leaves the lock held, and then every call returns `Browser is
already in use` with nothing actually using it. `--isolated` restores
concurrency but throws the profile away.

In-pod there is no contention to manage, and two things get better:

- **localhost works.** The browser shares a network namespace with the agent,
  so the agent can serve a page -- a report, a plot, a dev server -- and browse
  it. Against the app that is impossible, which is why showing it
  agent-authored content means encoding the page into a `data:` URL.
- **The profile persists** under the project's state directory, so it rides the
  checkpoint. The app's profile is wiped every time the app is redeployed.

What is given up is the live noVNC window and a human signing in by hand: a
task has no Service and no public URL. The app stays deployed for that.

Nothing here is exposed outside the pod. `--host 127.0.0.1` is deliberate: the
MCP endpoint has no authentication of its own and does not need any, because
nothing can route to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from . import _browser_exit_node, _secret_env
else:
    from flyte_agent_factory import _browser_exit_node, _secret_env

#: Loopback port for the MCP server. Arbitrary, but matches the app so the
#: two paths differ only in host.
MCP_PORT = 8931

#: How the server is *addressed*, as opposed to what it binds to. It binds
#: 127.0.0.1 so nothing outside the pod can reach it, but it must be addressed
#: as `localhost`: playwright-mcp validates Host and Origin as DNS-rebinding
#: protection and answers 403 to anything else -- including `127.0.0.1`, which
#: cost a run to discover. The shared browser app only works because nginx
#: rewrites both headers on the way through; in-pod there is no nginx, so the
#: caller has to get them right.
MCP_HOST = "localhost"

#: X display Xvfb serves and Chromium renders on.
DISPLAY = ":99"

VIEWPORT = "1280,720"

_READY_TIMEOUT_S = 90.0


def available() -> bool:
    """Whether this image actually carries the browser stack.

    The agent runs in one of two images and only one has Chromium, so the
    caller has to be able to ask rather than assume and fail at the first tool
    call.
    """
    return bool(shutil.which("playwright-mcp")) and bool(shutil.which("Xvfb"))


class LocalBrowser:
    """Xvfb plus `playwright-mcp` on loopback, for the life of the session."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        output_dir: Path,
        isolated: bool = True,
        exit_node: str = "",
        tailscale_secret: str = "",
    ) -> None:
        self._exit_node = exit_node
        self._tailscale_secret = tailscale_secret
        self._proxy: _browser_exit_node.BrowserExitNode | None = None
        self._proxy_config: tempfile.TemporaryDirectory[str] | None = None
        self._profile_dir = profile_dir
        self._output_dir = output_dir
        self._isolated = isolated
        self._procs: list[asyncio.subprocess.Process] = []

    @property
    def endpoint(self) -> str:
        return f"http://{MCP_HOST}:{MCP_PORT}"

    @property
    def client_headers(self) -> dict[str, str]:
        """Headers any MCP client must send, Claude Code included."""
        return {"Origin": f"http://{MCP_HOST}:{MCP_PORT}"}

    async def start(self) -> str:
        """Bring up the display and the MCP server. Returns the endpoint.

        Raises if the server does not accept connections, rather than returning
        an endpoint that will fail on the first tool call -- an image without
        Chromium looked healthy that way for a long time.
        """
        self._profile_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        proxy_args = []
        if self._exit_node or self._tailscale_secret:
            if not self._exit_node or not self._tailscale_secret:
                raise RuntimeError("browser exit node requires a Tailscale secret")
            await self._start_tailscale()
            self._proxy_config = tempfile.TemporaryDirectory(prefix="browser-proxy-")
            config = Path(self._proxy_config.name) / "mcp.json"
            config.write_text(
                json.dumps(
                    {
                        "browser": {
                            "launchOptions": {
                                "args": [
                                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                                    # Resolve public hosts through SOCKS, preserving local pages.
                                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                                ]
                            }
                        }
                    }
                )
            )
            proxy_args = [
                "--proxy-server",
                f"socks5://127.0.0.1:{_browser_exit_node.SOCKS_PORT}",
                "--config",
                str(config),
            ]

        # The session drives playwright-mcp, and `browser_run_code_unsafe` can read its process.env.
        env = _secret_env.child_env(DISPLAY=DISPLAY)

        await self._spawn(["Xvfb", DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"], env)
        # Chromium exits immediately against a display that is not up yet.
        if not await _wait(lambda: Path(f"/tmp/.X11-unix/X{DISPLAY.lstrip(':')}").exists(), 15.0):
            raise RuntimeError(f"Xvfb did not create a socket for {DISPLAY}")

        await self._spawn(
            [
                "playwright-mcp",
                "--port",
                str(MCP_PORT),
                "--host",
                "127.0.0.1",
                # Explicit: the default is the *branded Chrome* channel, which
                # `playwright install chromium` does not provide, and every
                # navigation then fails while the server looks healthy.
                "--browser",
                "chromium",
                "--no-sandbox",
                # devtools unlocks browser_start_video / browser_stop_video plus
                # chapter cards and action overlays; pdf unlocks
                # browser_pdf_save. Without them the tools are simply absent.
                "--caps=devtools,pdf",
                "--viewport-size",
                VIEWPORT,
                # Isolated by default, and the reason is not concurrency
                # between *projects* -- it is that Playwright allows one client
                # per persistent profile, and this process is itself a client.
                # The supervisor's own pre-flight tool listing took the seat,
                # so the session it had just started got "Browser is already in
                # use" on every call and could not record anything.
                *(["--isolated"] if self._isolated else ["--user-data-dir", str(self._profile_dir)]),
                *proxy_args,
                "--output-dir",
                str(self._output_dir),
            ],
            env,
        )

        if not await _wait(lambda: _listening(MCP_PORT), _READY_TIMEOUT_S):
            await self.stop()
            raise RuntimeError(f"playwright-mcp did not listen on {MCP_PORT} within {_READY_TIMEOUT_S:.0f}s")
        return self.endpoint

    async def _start_tailscale(self) -> None:
        self._proxy = _browser_exit_node.BrowserExitNode(self._exit_node, self._tailscale_secret)
        await self._proxy.start()

    async def stop(self) -> None:
        for proc in reversed(self._procs):
            if proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        for proc in reversed(self._procs):
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=10)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        self._procs.clear()
        if self._proxy:
            await self._proxy.stop()
        if self._proxy_config:
            self._proxy_config.cleanup()

    async def _spawn(self, argv: list[str], env: dict[str, str]) -> None:
        self._procs.append(
            await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )


def _listening(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _wait(predicate: Callable[[], bool], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.5)
    return False


#: Names the Flyte runtime uses for the in-pod API key, in precedence order.
API_KEY_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY", "FLYTE_API_KEY", "UNION_API_KEY")


def api_key() -> str:
    """Return the pod's own API key, if it has one."""
    for var in API_KEY_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return ""


def authorize_console(endpoint: str, key: str, *, token_path: Path, host: str) -> str:
    """Attach the pod's credential to console requests, from the *task*.

    Done here rather than by the session for one reason: the MCP server echoes
    back the code it was asked to run, so a session that sets this header
    itself puts the key straight into the run log. One did, in full, and
    reported it. The task holds the key anyway, so it installs the header once
    and the session never needs the credential -- `_session_env` then strips it
    from the session's environment.

    **The key is passed by file path, not inside the code.** The browser runs
    in this pod now, so the snippet can read a 0600 file; only the path is
    echoed. Passing it in the code string is what leaked it, and an `env`
    argument on the tool is not something to assume exists.

    Scoped to the console's own host with a route handler rather than set
    globally: a blanket `Authorization` header goes to *every* request the page
    makes, fonts and CDNs included, and the console then renders blank. A
    session found that, and the fix, the hard way.

    Returns a short status, never anything containing the key.
    """
    import json

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(key)
    token_path.chmod(0o600)

    code = (
        "async (page) => {"
        "  const fs = require('fs');"
        f"  const token = fs.readFileSync({json.dumps(str(token_path))}, 'utf8').trim();"
        f"  await page.context().route('**://{host}/**', async (route) => {{"
        "    const headers = Object.assign({}, route.request().headers(),"
        "      { Authorization: 'Bearer ' + token });"
        "    await route.continue({ headers });"
        "  });"
        "  return 'console authorized';"
        "}"
    )
    result = _rpc(
        endpoint,
        [("tools/call", {"name": "browser_run_code_unsafe", "arguments": {"code": code}})],
    )
    text = json.dumps(result)
    # Belt and braces: if the key ever does come back, say nothing more.
    if key and key in text:
        return "console authorized (response withheld: it contained the key)"
    return "console authorized"


class _Tool(TypedDict, total=False):
    name: str


class _RpcResult(TypedDict, total=False):
    tools: list[_Tool]


class _RpcReply(TypedDict, total=False):
    result: _RpcResult


def _rpc(
    endpoint: str, calls: list[tuple[str, dict[str, object] | None]], timeout: float = 120.0
) -> _RpcReply:
    """Open an MCP session, make `calls` in order, return the last result.

    `urllib` rather than httpx: this runs in the agent image, which carries no
    HTTP client beyond the standard library. The protocol details that are easy
    to get wrong -- the `Origin` the server insists on, the session id handed
    back by `initialize`, and replies arriving as SSE even for a single
    request -- live here once rather than at each call site.
    """
    import json
    import urllib.request

    url = f"{endpoint.rstrip('/')}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # Required: the server 403s a request whose Origin it does not
        # recognise, and urllib sends none at all by default.
        "Origin": f"http://{MCP_HOST}:{MCP_PORT}",
    }
    counter = {"id": 0}

    def send(method: str, params: dict[str, object] | None, notify: bool = False) -> _RpcReply:
        counter["id"] += 1
        body: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = counter["id"]
        if params is not None:
            body["params"] = params
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if session := response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = session
            text = response.read().decode(errors="replace")
            if "text/event-stream" in (response.headers.get("content-type") or ""):
                data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
                text = data[-1] if data else "{}"
        # MCP replies may carry other fields; only the tools/list result is inspected here.
        return cast(_RpcReply, json.loads(text or "{}"))

    send(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "flyte-agent-factory", "version": "0"},
        },
    )
    send("notifications/initialized", None, notify=True)

    result: _RpcReply = {}
    for method, params in calls:
        result = send(method, params)
    return result


def tool_names(endpoint: str, timeout: float = 30.0) -> list[str]:
    """Ask the MCP server what tools it offers.

    Worth a pre-flight call because a session with no browser tools does not
    fail -- it works around their absence. A demo run produced a complete,
    well-curated package with no video in it for exactly this reason, and
    nothing in the logs said the tools were missing.
    """
    listed = _rpc(endpoint, [("tools/list", {})], timeout=timeout)
    return [t["name"] for t in listed.get("result", {}).get("tools", []) if "name" in t]
