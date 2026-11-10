"""Role names: the session sub-agent roles with their old aliases, and the review loop's display names.

Roles are named by the verb of what they do (#80). The job-title names stay installed as aliases for the
015 rename window, and reports written before the rename still carry the old names, so both directions
are mapped here rather than at each reader.
"""

from __future__ import annotations

#: alias -> the role it stands for. Removing the aliases is a gardening item after the rename window.
ROLE_ALIASES = {"product": "scope", "designer": "shape", "dev": "build", "qa": "verify"}

#: The ledger and the verdict rows are keyed by the reviewer purpose, which also names the policy keys
#: (`JUDGE_ENGINE`, `PLANNER_ENGINE`, ...), so the purposes keep their names and only the report's
#: display changes. Older reports carry the old names; `display_role` maps those too.
_REVIEW_DISPLAY = {
    "correctness": "judge-correctness",
    "completeness": "judge-completeness",
    "craft": "judge-craft",
    "planner": "plan",
    "arbiter": "arbitrate",
}


def canonical_role(agent_type: str) -> str:
    """Return the new role an `agentType` stands for; any other type (`general-purpose`, ...) is unchanged."""
    return ROLE_ALIASES.get(agent_type, agent_type)


def display_role(purpose: str) -> str:
    """Return the name a report shows for a reviewer purpose or a session role."""
    return _REVIEW_DISPLAY.get(purpose) or canonical_role(purpose)
