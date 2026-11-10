r"""Probe whether a browser in a task pod can reach the public internet.

The demo run turned up a split: its in-pod Chromium loaded `example.com` but
timed out on the Union console host, while `curl` in the same pod got a
401 from that host. So "the pod has egress" and "the browser has egress to the
host you want" are different questions, and the second one decides whether a
project that reads a real website can work in-pod at all.

Checks each host three ways, because the three fail independently:

- DNS, from Python
- `curl`, which uses the system resolver and any proxy env vars
- Chromium, which does its own DNS inside its network service and ignores
  some of what curl honours

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/egress_probe.py probe
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import cast

import flyte

from flyte_agent_factory import _local_browser, _profile
from flyte_agent_factory._bridge import redact
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import agent_image

env = flyte.TaskEnvironment(
    name="egress-probe",
    image=agent_image(browser=True),
    env_vars=_profile.pod_env(),
    resources=flyte.Resources(cpu=(1, 2), memory=("2Gi", "4Gi"), shm="2Gi"),
)

PROFILE = _profile.require()
CONSOLE = f"{PROFILE.console_base}/runs"

# Names the Flyte runtime uses for the in-pod API key, in precedence order.
_KEY_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY", "FLYTE_API_KEY", "UNION_API_KEY")

HOSTS = [
    # The control: known-good, no auth, nothing special about it.
    "https://example.com",
    # What this is really for.
    "https://www.linkedin.com/login",
    # Reproduces the demo run's finding, so a pass here means it was transient.
    f"https://{PROFILE.backend.console_host}/",
    # A third-party API, to separate "no egress" from "egress but filtered".
    "https://api.github.com/",
]

# Proxy settings are the usual explanation for curl and Chromium disagreeing.
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")


async def _curl(url: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} in %{time_total}s",
        "--max-time",
        "25",
        url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace").strip()[:200]


def _resolve(host: str) -> str:
    try:
        # AF_INET and AF_INET6 addresses returned for a host have a string address.
        return ", ".join(sorted({cast(str, info[4][0]) for info in socket.getaddrinfo(host, 443)}))
    except OSError as e:
        return f"{type(e).__name__}: {e}"


@env.task(report=True)
async def probe() -> str:
    """Compare what curl and an in-pod Chromium can reach, and return the findings as JSON."""
    findings: dict[str, object] = {
        # Names and values both: a proxy variable's *value* is the explanation
        # when curl and Chromium disagree, and none of these is a credential.
        "proxy_env": {var: os.environ[var] for var in _PROXY_VARS if var in os.environ},
    }

    home = Path(os.environ.get("HOME", "/tmp"))
    browser = _local_browser.LocalBrowser(
        profile_dir=home / "probe-profile",
        output_dir=home / "probe-output",
    )
    endpoint = await browser.start()
    findings["browser_endpoint"] = endpoint
    findings["browser_tools"] = len(_local_browser.tool_names(endpoint))

    import urllib.request

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **browser.client_headers,
    }
    state = {"id": 0}

    def rpc(method: str, params: dict[str, object] | None = None, notify: bool = False) -> dict[str, object]:
        state["id"] += 1
        body: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = state["id"]
        if params is not None:
            body["params"] = params
        request = urllib.request.Request(
            f"{endpoint}/mcp", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            if session := response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = session
            text = response.read().decode(errors="replace")
            if "text/event-stream" in (response.headers.get("content-type") or ""):
                data = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
                text = data[-1] if data else "{}"
        # The MCP endpoint returns a JSON-RPC object; payload validation is unchanged.
        return cast(dict[str, object], json.loads(text or "{}"))

    rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "egress-probe", "version": "0"},
        },
    )
    rpc("notifications/initialized", notify=True)

    try:
        for url in HOSTS:
            host = url.split("//", 1)[1].split("/", 1)[0]
            entry: dict[str, str] = {"dns": _resolve(host), "curl": await _curl(url)}
            try:
                result = rpc("tools/call", {"name": "browser_navigate", "arguments": {"url": url}})
                text = json.dumps(result)[:400]
                entry["browser"] = "error" if result.get("isError") else "ok"
                entry["browser_detail"] = text
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                entry["browser"] = f"{type(e).__name__}: {str(e)[:150]}"
            findings[host] = entry

        # The question chapter 4 of a demo actually needs: not "is the host
        # reachable" -- it is, and anonymously it redirects to a sign-in page
        # as it should -- but "does the console *render* in-pod once the pod's
        # own key is attached". That worked from the shared app's pod; whether
        # a task pod differs was the open question, and a demo run reported a
        # timeout here that this probe has already contradicted.
        api_key = next((os.environ[v] for v in _KEY_VARS if os.environ.get(v)), "")
        console: dict[str, object] = {"api_key_present": bool(api_key)}
        findings["console"] = console
        if api_key:
            try:
                rpc(
                    "tools/call",
                    {
                        "name": "browser_run_code_unsafe",
                        "arguments": {
                            "code": (
                                "async (page) => { await page.context()."
                                "setExtraHTTPHeaders({ Authorization: 'Bearer "
                                + api_key.replace("'", "")
                                + "' }); return 'ok'; }"
                            )
                        },
                    },
                )
                rpc("tools/call", {"name": "browser_navigate", "arguments": {"url": CONSOLE}})
                rpc("tools/call", {"name": "browser_wait_for", "arguments": {"time": 5}})
                shot = json.dumps(rpc("tools/call", {"name": "browser_snapshot", "arguments": {}}))
                console["rendered"] = "Runs | Union.ai" in shot
                console["is_login"] = "Sign In" in shot or "signin." in shot
                # Scrubbed: Playwright MCP echoes the code it ran, which holds
                # the key.
                console["snapshot_head"] = redact(shot, api_key)[:400]
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                console["error"] = redact(f"{type(e).__name__}: {e}", api_key)[:300]
    finally:
        await browser.stop()

    print(json.dumps(findings, indent=2), flush=True)
    return json.dumps(findings, indent=2)[:8000]
