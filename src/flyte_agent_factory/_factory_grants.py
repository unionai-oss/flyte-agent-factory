"""`factory.credential_link` and `factory.credentials`: giving and listing a project's credential grants.

Chat never sees a credential. It reads the public part of a project's grant key from the project's
report (`credentials`, written by `_grants.GrantStore.report`), builds the link the owner opens to seal a
credential, and says where to paste the sealed text. Metadata only, here and in every result.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from flyte import remote

from flyte_agent_factory import _dashboard, _factory_base, _factory_live, _grant_seal
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._grant_links import read_page_base_url
from flyte_agent_factory._grants import MAX_HOURS

#: What `GrantInfo.to_dict` publishes. A report is read from outside the project, so only these cross, even
#: if a report (an older or a different engine's) carries more.
_GRANT_FIELDS = (
    "name",
    "kind",
    "state",
    "reason",
    "region",
    "granted",
    "expires",
    "granted_at",
    "expires_at",
)


def reduce_to_public(section: dict[str, Any]) -> dict[str, Any]:
    """Reduce a report's `credentials` section to the public key and per-grant metadata."""
    if not section:
        return {}
    grants = section.get("grants")
    return {
        **{k: section[k] for k in ("key", "kid") if k in section},
        "grants": [
            {k: g[k] for k in _GRANT_FIELDS if k in g} for g in (grants if isinstance(grants, list) else [])
        ],
    }


def _read_credentials(run: remote.Run) -> dict[str, Any]:
    """Return the `credentials` section of a run's report; empty if it has none (unpublished, or older)."""
    try:
        data = _dashboard.from_report(_factory_base.report_html(run))
    except REMOTE_ERRORS:  # a remote call; its error text may hold signed URLs, so it is omitted
        return {}
    return (data or {}).get("credentials") or {}


def find_live_run(name: str, operated_live: Callable[[], list[remote.Run]]) -> remote.Run:
    """Return the live run of project `name`; `ValueError` if it has none."""
    live = _factory_base._labelled_runs(name, in_phase=_factory_base.SERVER_LIVE, limit=1)
    run = next((r for r in live if _factory_base._phase(r) in _factory_base.LIVE), None)
    if run is None:
        run = next((r for r in operated_live() if _factory_live.project_name(r) == name), None)
    if run is None:
        raise ValueError(f"project {name!r} is not live: a credential can only be given to a running project")
    return run


def credential_link(
    name: str, credential: str, hours: float, operated_live: Callable[[], list[remote.Run]]
) -> dict[str, Any]:
    if not _grant_seal.NAME_RE.fullmatch(credential):
        raise ValueError(f"{credential!r} is not a valid credential name (lower case letters, digits, '-')")
    if not math.isfinite(hours) or not 0 < hours <= MAX_HOURS:
        raise ValueError(f"hours must be more than 0 and at most {MAX_HOURS:g}")
    run = find_live_run(name, operated_live)
    section = _read_credentials(run)
    key, kid = section.get("key"), section.get("kid")
    if not key or not kid:
        raise ValueError(
            f"project {name!r} has not published a credential key yet (its report has none): "
            "try again in a minute, or it may be running an older engine without credential grants"
        )
    inbox = _factory_live.direct_condition(run)
    if not inbox:
        raise ValueError(f"project {name!r} has no open direct- inbox to paste the sealed text into")
    profile = _factory_base.PROFILE
    base = read_page_base_url(
        profile.mcp.public_url if profile.mcp else "", profile.backend.project, profile.backend.domain
    )
    if not base:
        raise ValueError(
            "the factory-mcp app has no known URL: set [mcp] public_url in the deployment profile"
        )
    link = _grant_seal.make_link(base, _grant_seal.Link(name, credential, key, kid, hours))
    return {"project": name, "credential": credential, "hours": hours, "url": link, "paste_to": inbox}


def credentials(operated_live: Callable[[], list[remote.Run]]) -> list[dict[str, Any]]:
    """Every live project and the grants it holds, from its report: names, expiry and state, never a value."""
    return [
        {
            "project": _factory_live.project_name(run)
            or _factory_base.run_labels(run).get(_factory_base.LABEL_PROJECT, run.name),
            "credentials": reduce_to_public(_read_credentials(run)).get("grants", []),
        }
        for run in operated_live()
    ]
