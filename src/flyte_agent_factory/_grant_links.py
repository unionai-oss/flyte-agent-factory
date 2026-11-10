"""Where the credential seal page lives: the base URL of the factory-mcp app.

Shared by the project supervisor (`_grants_supervisor`, which puts the link in a question) and the
factory tasks (`_factory_grants`, which hand one to chat), so the two cannot disagree.
"""

from __future__ import annotations

from flyte import remote

from flyte_agent_factory._errors import REMOTE_ERRORS

#: A base that is never served. The link's fragment is all `python -m flyte_agent_factory._grants seal` reads,
#: so a project whose page URL is unknown can still be given a credential.
UNAVAILABLE_BASE = "https://factory-mcp.invalid"


def read_page_base_url(public_url: str, project: str, domain: str) -> str:
    """Return the factory-mcp app's URL: `[mcp] public_url`, else the deployed app's endpoint, else ''.

    Blocking (a remote call); run it in a thread.
    """
    if public_url:
        return public_url.rstrip("/")
    try:
        endpoint = remote.App.get(name="factory-mcp", project=project, domain=domain).endpoint
    except (*REMOTE_ERRORS, AttributeError):  # not deployed, or an SDK without the lookup: no page
        return ""
    return str(endpoint or "").rstrip("/")
