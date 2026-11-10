r"""Record a short video end to end: the whole chain your demo goal needs.

Tests, in one run: the browser launches (chromium, not the branded Chrome
channel), it renders a page, `browser_start_video` records, chapter markers
land, and the resulting file can be fetched out of the browser container
through `/output/`.

The page is a `data:text/html;base64,...` URL rather than something served by
the agent. That matters: the browser runs in its own pod, so it cannot reach
the agent's localhost, and the agent's repo is private. A data URL carries the
content with no network and no credentials -- which makes it the practical way
to show a report, a plot, or code the agent just wrote.

The last section records the *Union console* in the same take, because "a demo
of the code and the UI" needs both and they are reached differently: the
console is a real site behind the platform's auth, entered with the pod's own
API key as a bearer header. Proving the page renders is not the same as
proving it records -- a authenticated SPA can navigate fine and still show
nothing by the time a frame is captured.

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/demo_chain_probe.py probe
"""

import base64
import json
import os

import flyte

from browser_probe import MCP
from flyte_agent_factory import _profile
from flyte_agent_factory._bridge import redact
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import agent_image
from flyte_agent_factory.browser import browser_endpoint

env = flyte.TaskEnvironment(
    name="demo-chain-probe",
    image=agent_image().with_pip_packages("httpx>=0.27"),
    env_vars=_profile.pod_env(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)

PROFILE = _profile.require()
CONSOLE = f"{PROFILE.console_base}/runs"

# Names the Flyte runtime uses for the in-pod API key, in precedence order.
_KEY_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY", "FLYTE_API_KEY", "UNION_API_KEY")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.6 system-ui;margin:0;padding:48px;background:#0f1115;color:#e6e6e6}}
h1{{font-size:34px;margin:0 0 8px}} .k{{color:#7fd1ff}} pre{{background:#171a21;padding:16px;border-radius:8px}}
</style></head><body><h1>{title}</h1><p class="k">{sub}</p><pre>{body}</pre></body></html>"""  # noqa: E501 -- HTML template kept verbatim, as served


def _data_url(title: str, sub: str, body: str) -> str:
    html = PAGE.format(title=title, sub=sub, body=body)
    return "data:text/html;base64," + base64.b64encode(html.encode()).decode()


@env.task(report=True)
async def probe() -> str:
    """Run the record-a-video chain against the browser app and return the findings."""
    import httpx

    base = browser_endpoint()
    findings: dict[str, object] = {"browser": base}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        mcp = MCP(client, f"{base}/mcp")
        try:
            await mcp.initialize()
        except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
            findings["mcp_error"] = f"{type(e).__name__}: {e}"
            return json.dumps(findings, indent=2)

        # 1. Does the browser launch and render at all?
        findings["navigate"] = (
            await mcp.call("browser_navigate", url=_data_url("Chromium check", "step 1", "ok"))
        )[:200]
        findings["snapshot"] = (await mcp.call("browser_snapshot"))[:250]

        # 2. Record a two-chapter walkthrough.
        findings["start_video"] = (await mcp.call("browser_start_video"))[:200]
        for n, (title, sub, body) in enumerate(
            [
                ("Polynomial regression", "raw x only, no basis features", "y = f(x)"),
                ("In-range vs extrapolation", "the interesting result", "fits inside, diverges outside"),
            ],
            start=1,
        ):
            await mcp.call("browser_navigate", url=_data_url(title, sub, body))
            findings[f"chapter_{n}"] = (await mcp.call("browser_video_chapter", title=title))[:150]
            await mcp.call("browser_wait_for", time=1)
        # 2b. The other half of "code and UI": the console, which is a real
        # site behind the platform's auth rather than a data URL. Recorded in
        # the same take, because that is how a demo would do it.
        api_key = next((os.environ[v] for v in _KEY_VARS if os.environ.get(v)), "")
        findings["api_key_present"] = bool(api_key)
        if api_key:
            try:
                await mcp.call(
                    "browser_run_code_unsafe",
                    code=(
                        "async (page) => { await page.context().setExtraHTTPHeaders("
                        "{ Authorization: 'Bearer " + api_key.replace("'", "") + "' }); "
                        "return 'ok'; }"
                    ),
                )
                await mcp.call("browser_navigate", url=CONSOLE)
                await mcp.call("browser_wait_for", time=3)
                findings["console_chapter"] = (
                    await mcp.call("browser_video_chapter", title="Running on Flyte")
                )[:150]
                # Scrubbed: Playwright MCP echoes the code it ran, so anything
                # derived from that call can carry the key into run logs.
                shot = await mcp.call("browser_snapshot")
                findings["console_rendered"] = "Runs | Union.ai" in shot or "Runs" in shot
                findings["console_is_login"] = "Sign In" in shot or "signin." in shot
                findings["console_snapshot_head"] = redact(shot, api_key)[:300]
                await mcp.call("browser_wait_for", time=2)
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                findings["console_error"] = redact(f"{type(e).__name__}: {e}", api_key)[:300]

        findings["stop_video"] = (await mcp.call("browser_stop_video"))[:300]

        # 3. Can the file be fetched out of the browser container?
        listing = await client.get(f"{base}/output/", timeout=30)
        findings["output_status"] = listing.status_code
        findings["output_listing"] = listing.text[:400]
        try:
            entries = json.loads(listing.text)
            videos = [e["name"] for e in entries if str(e.get("name", "")).endswith((".webm", ".mp4"))]
            findings["videos"] = videos
            if videos:
                got = await client.get(f"{base}/output/{videos[0]}", timeout=120)
                findings["video_bytes"] = len(got.content)
                findings["video_content_type"] = got.headers.get("content-type", "")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            findings["listing_parse_error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(findings, indent=2), flush=True)
    return json.dumps(findings, indent=2)[:8000]
