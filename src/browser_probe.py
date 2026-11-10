r"""Probe whether the browser can reach the Union console with credentials the pod already has.

The browser app keeps a persistent profile, so the plan has been: a human signs
in once through noVNC and every later tool call inherits the session. This
probe asks whether that step is avoidable -- whether the API key already in a
task's environment can get a rendered, authenticated console page instead.

Three things are tested, cheapest first:

1. **Plain navigation.** What does the console do for a browser with an empty
   profile? If it renders, something is already authenticating it.
2. **Bearer header.** Set `Authorization: Bearer <api key>` on every request
   and navigate again. Web consoles usually authenticate with a session cookie
   rather than a header, so this is the long shot -- but it is the one that
   would remove the human step entirely.
3. **The API.** Hit the gRPC-gateway/HTTP API with the same key, to confirm the
   key is valid and the failure above is about the UI, not the credential.

It also exercises the browser app end to end for the first time: MCP over
HTTP, the devtools caps, and the `/output/` path.

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/browser_probe.py probe
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, TypedDict, cast

import flyte

from flyte_agent_factory import _profile
from flyte_agent_factory._bridge import redact
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import agent_image
from flyte_agent_factory.browser import browser_endpoint, browser_env

if TYPE_CHECKING:
    import httpx

env = flyte.TaskEnvironment(
    name="browser-probe",
    image=agent_image().with_pip_packages("httpx>=0.27"),
    env_vars=_profile.pod_env(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
    depends_on=[browser_env],
)

PROFILE = _profile.require()
CONSOLE = f"{PROFILE.console_base}/runs"

# Names the Flyte runtime uses for the in-pod API key, in precedence order.
_KEY_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY", "FLYTE_API_KEY", "UNION_API_KEY")


class _MCPTool(TypedDict):
    name: str


class _MCPContent(TypedDict, total=False):
    text: str


class _MCPResult(TypedDict, total=False):
    tools: list[_MCPTool]
    content: list[_MCPContent]
    serverInfo: dict[str, object]


class _MCPError(TypedDict, total=False):
    message: str


class _MCPReply(TypedDict, total=False):
    result: _MCPResult
    error: _MCPError


class MCP:
    """A minimal streamable-HTTP MCP client: initialize, then tools/call."""

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        """Talk to the MCP server at `url` through `client`, an httpx client."""
        self._client = client
        self._url = url
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._id = 0

    async def _rpc(self, method: str, params: dict[str, object] | None = None) -> _MCPReply:
        self._id += 1
        body: dict[str, object] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        resp = await self._client.post(self._url, headers=self._headers, json=body, timeout=120)
        if resp.status_code >= 400:
            # The body is the only thing that says *why*; raise_for_status
            # throws it away, which cost several rounds of guessing at a 403.
            raise RuntimeError(
                f"HTTP {resp.status_code} from {self._url} "
                f"ct={resp.headers.get('content-type', '')!r} body={resp.text[:300]!r}"
            )
        if session := resp.headers.get("Mcp-Session-Id"):
            self._headers["Mcp-Session-Id"] = session

        text = resp.text
        # Streamable HTTP may answer as SSE; take the last data: line.
        if "text/event-stream" in resp.headers.get("content-type", ""):
            payloads = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            text = payloads[-1] if payloads else "{}"
        # The probe assumes the MCP JSON-RPC reply shape, as it did before typing.
        return cast(_MCPReply, json.loads(text or "{}"))

    async def initialize(self) -> _MCPReply:
        """Send the MCP initialize handshake and return the server's reply."""
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "browser-probe", "version": "0"},
            },
        )
        await self._rpc("notifications/initialized")
        return result

    async def tools(self) -> list[str]:
        """Return the sorted names of the tools the server offers."""
        result = await self._rpc("tools/list")
        return sorted(t["name"] for t in result.get("result", {}).get("tools", []))

    async def call(self, name: str, **arguments: object) -> str:
        """Call tool `name` and return its text output, or `ERROR <message>`."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in result:
            return f"ERROR {result['error'].get('message', result['error'])}"
        content = result.get("result", {}).get("content", [])
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))


# Not @flyte.trace: tracing serializes the arguments as task inputs, and an
# MCP client holds an httpx session with a thread lock, which cannot pickle.
async def _probe_navigation(mcp: MCP, label: str) -> dict[str, str | bool]:
    """Navigate and report where we ended up and what is on the page."""
    nav = await mcp.call("browser_navigate", url=CONSOLE)
    snapshot = await mcp.call("browser_snapshot")
    lowered = snapshot.lower()
    return {
        "label": label,
        "navigate": nav[:300],
        "looks_like_login": any(
            k in lowered for k in ("sign in", "log in", "login", "continue with", "password")
        ),
        "mentions_runs": "runs" in lowered or "project" in lowered,
        "snapshot_head": snapshot[:600],
    }


@env.task(report=True)
async def probe() -> str:
    """Try the console with no credentials, then with the pod's API key."""
    import httpx

    key_var = next((v for v in _KEY_VARS if os.environ.get(v)), None)
    api_key = os.environ.get(key_var or "", "")

    findings: dict[str, object] = {
        "api_key_env_var": key_var,
        "api_key_present": bool(api_key),
    }

    # Deliberately the same resolver the agent uses, rather than a hardcoded
    # address. An earlier version of this probe built the internal URL itself
    # and so kept passing while the agent -- which went through
    # `browser_env.endpoint` -- was being handed the public URL and a 302 to a
    # login page. A probe that does not exercise the real path proves nothing.
    internal = browser_endpoint()
    findings["internal_endpoint"] = internal

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Which Host the MCP server will accept is the open question: it 403s
        # a direct localhost POST too, so this is not about nginx or the token.
        host_attempts: dict[str, str] = {}
        findings["host_attempts"] = host_attempts
        host_that_worked = ""
        for host in (None, "localhost:8931", "127.0.0.1:8931", "localhost"):
            probe = MCP(client, f"{internal}/mcp")
            if host:
                probe._headers["Host"] = host
            try:
                init = await probe._rpc(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "host-probe", "version": "0"},
                    },
                )
                host_attempts[host or "(none)"] = "ok"
                host_that_worked = host or "(none)"
                findings["host_that_worked"] = host_that_worked
                break
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                host_attempts[host or "(none)"] = f"{type(e).__name__}: {str(e)[:70]}"

        mcp = MCP(client, f"{internal}/mcp")
        if host_that_worked and host_that_worked != "(none)":
            mcp._headers["Host"] = host_that_worked
        try:
            init = await mcp.initialize()
            findings["mcp_server"] = init.get("result", {}).get("serverInfo", {})
            names = await mcp.tools()
            findings["tool_count"] = len(names)
            # Confirms --caps=devtools actually took effect.
            findings["has_video_tools"] = [n for n in names if "video" in n]
        except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
            findings["mcp_error"] = f"{type(e).__name__}: {e}"
            return json.dumps(findings, indent=2)

        findings["anonymous"] = await _probe_navigation(mcp, "no credentials")

        if api_key:
            try:
                # Every subsequent request carries the key.
                await mcp.call(
                    "browser_evaluate",
                    function="() => 'noop'",
                )
                set_headers = await mcp.call(
                    "browser_run_code_unsafe",
                    code=(
                        "async (page) => { await page.context().setExtraHTTPHeaders("
                        "{ Authorization: 'Bearer " + api_key.replace("'", "") + "' }); "
                        "return 'headers set'; }"
                    ),
                )
                # Scrubbed, not truncated. Playwright MCP echoes back the code
                # it ran, so this result contains the API key -- and an earlier
                # version of this line put a prefix of it into the probe's
                # output and the run's logs.
                findings["bearer_setup"] = redact(set_headers, api_key)[:200]
                found = await _probe_navigation(mcp, "bearer header")
                findings["with_bearer"] = {
                    k: redact(v, api_key) if isinstance(v, str) else v for k, v in found.items()
                }
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                findings["bearer_error"] = f"{type(e).__name__}: {e}"

        # Does the key work against the API at all? Separates "bad credential"
        # from "the UI does not accept credentials this way".
        if api_key:
            try:
                resp = await client.get(
                    f"https://{PROFILE.backend.console_host}/api/v1/projects",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30,
                )
                findings["api_status"] = resp.status_code
                findings["api_body_head"] = resp.text[:200]
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                findings["api_error"] = f"{type(e).__name__}: {e}"

    return json.dumps(findings, indent=2)
