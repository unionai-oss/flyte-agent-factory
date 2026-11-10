"""Find the cluster-internal route to the browser app.

`AppEnvironment.endpoint` returns the internal address only when the platform
has set `INTERNAL_APP_ENDPOINT_PATTERN` in the pod; otherwise it falls back to
the public URL, which sits behind the auth proxy and answers an MCP POST with
an HTML login page. This prints what the pod actually has and tries the
plausible in-cluster names, so the agent can reach `/mcp` without the app
having to be made public.
"""

import json
import os

import flyte

from flyte_agent_factory import _profile
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import agent_image
from flyte_agent_factory.browser import APP_PORT, browser_env

PROFILE = _profile.require()
env = flyte.TaskEnvironment(
    name="endpoint-probe",
    image=agent_image().with_pip_packages("httpx>=0.27"),
    env_vars=_profile.pod_env(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)


@env.task(report=True)
async def probe() -> str:
    """Try each candidate address of the browser app from this pod and return the results as JSON."""
    import httpx

    namespace = f"{PROFILE.backend.project}-{PROFILE.backend.domain}"
    app = browser_env.name
    candidates = [
        f"http://{app}.{namespace}.svc.cluster.local:{APP_PORT}",
        f"http://{app}.{namespace}.svc.cluster.local",
        f"http://{app}.{namespace}:{APP_PORT}",
        f"http://{app}:{APP_PORT}",
        f"http://{app}.{namespace}.svc:{APP_PORT}",
    ]

    candidate_findings: dict[str, object] = {}
    findings: dict[str, object] = {
        # The variable that would make endpoint() return the internal address.
        "INTERNAL_APP_ENDPOINT_PATTERN": os.environ.get("INTERNAL_APP_ENDPOINT_PATTERN"),
        "endpoint_property": browser_env.endpoint,
        "namespace_guess": namespace,
        # Names only, never values. An earlier version printed values for every
        # variable matching "UNION" and dumped `_UNION_EAGER_API_KEY` -- a live
        # cluster credential -- into the run logs. A probe has no business
        # reading a secret to confirm a variable exists.
        "credential_env_vars_present": sorted(
            k for k in os.environ if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        ),
        "endpoint_env_vars": {
            k: v for k, v in os.environ.items() if "ENDPOINT" in k.upper() or "APP_" in k.upper()
        },
        "candidates": candidate_findings,
    }

    async with httpx.AsyncClient(follow_redirects=False) as client:
        for url in candidates:
            try:
                resp = await client.get(f"{url}/healthz", timeout=8)
                candidate_findings[url] = {
                    "status": resp.status_code,
                    "body": resp.text[:60],
                }
            except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
                candidate_findings[url] = f"{type(e).__name__}: {str(e)[:90]}"

        # And what the public URL actually answers, for the record.
        try:
            resp = await client.get(f"{browser_env.endpoint.rstrip('/')}/healthz", timeout=15)
            findings["public_healthz"] = {
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "body": resp.text[:120],
            }
        except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
            findings["public_healthz"] = f"{type(e).__name__}: {str(e)[:120]}"

    print(json.dumps(findings, indent=2), flush=True)
    return json.dumps(findings, indent=2)[:8000]
