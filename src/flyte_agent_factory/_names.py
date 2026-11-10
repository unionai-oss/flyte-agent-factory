"""Environment variable names the factory owns.

Every factory variable is `AGENT_FACTORY_*`. A role-policy key `<KEY>` (the profile's `[policy]` table, or a
launch's policy) is the variable `AGENT_FACTORY_<KEY>`; those are open-ended, so they are not listed here.
What is listed are the fixed variables, the ones that are not policy. Variables that belong to Claude Code
itself (`CLAUDE_CODE_*`, `CLAUDE_CONFIG_DIR`) are not the factory's and are not listed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

log = logging.getLogger(__name__)

PREFIX = "AGENT_FACTORY_"

#: What the deploying shell selects and the deploy and test tooling read. Unlike the variables in
#: `POD_FIXED`, these are forwarded to a pod with the policy keys (except the profile selection, see
#: `agent._shell_policy_env`).
TOOLING_FIXED = frozenset(
    {
        "AGENT_FACTORY_PROFILE",
        "AGENT_FACTORY_PROFILE_TOML",
        "AGENT_FACTORY_PROFILES_DIR",
        "AGENT_FACTORY_DEPLOY_REPORT",
        "AGENT_FACTORY_LIVE_TESTS",
    }
)

#: The fixed variables that are not policy and not forwarded from the shell: what a deploy sets on a slot's
#: environment (`_profile.Profile.deploy_env`), and what the supervisor and its session set for themselves.
#: `AGENT_FACTORY_AUTHOR_ENGINE` (the author-engine pin, a policy key) and
#: `AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE` (the engine the run chose, set by the session) are different things.
POD_FIXED = frozenset(
    {
        "AGENT_FACTORY_BROWSER",
        "AGENT_FACTORY_CLAUDE_TOKEN_SECRET",
        "AGENT_FACTORY_CODEX_SECRET",
        "AGENT_FACTORY_CODEX_TOKEN_SECRET",
        "AGENT_FACTORY_CREDENTIALS_SECRET",
        "AGENT_FACTORY_ENV_NAME",
        "AGENT_FACTORY_REPO_KEYS",
        "AGENT_FACTORY_SECRETS",
        "AGENT_FACTORY_TOKEN_IN_ENV",
        "AGENT_FACTORY_SUBAGENT_ENV_NAME",
        "AGENT_FACTORY_BROWSER_ISOLATED",
        "AGENT_FACTORY_BROWSER_EXIT_NODE",
        "AGENT_FACTORY_BROWSER_TAILSCALE_SECRET",
        "AGENT_FACTORY_AGENT_REPO",
        "AGENT_FACTORY_BASE",
        "AGENT_FACTORY_BRIDGE_DIR",
        "AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE",
        "AGENT_FACTORY_ENGINE_SHA",
        "AGENT_FACTORY_STATE_URI",
        "AGENT_FACTORY_WORKSPACE",
    }
)

#: Every fixed, documented variable (`tests/test_docs_match_code.py` checks each is documented).
FIXED = TOOLING_FIXED | POD_FIXED


def getenv(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """Return `name` from `env` (default: the process environment), else `default`."""
    env = os.environ if env is None else env
    return env.get(name, default)


#: Aliases already warned about, so the warning is logged once per process.
_warned: set[str] = set()

COORDINATOR_ENGINE = "AGENT_FACTORY_COORDINATOR_ENGINE"


def coordinator_engine(env: Mapping[str, str] | None = None) -> str:
    """Return the engine the project's coordinator (its author session) is pinned to, or "".

    `COORDINATOR_ENGINE` is the name; `AUTHOR_ENGINE` is what the same policy key was called before the
    coordinator was selectable, and is read as an alias. Setting only the alias is logged once; setting both
    to different engines is an error, because there is no telling which one the owner meant.
    """
    env = os.environ if env is None else env
    canonical = getenv(COORDINATOR_ENGINE, env=env).strip().lower()
    alias = getenv("AGENT_FACTORY_AUTHOR_ENGINE", env=env).strip().lower()
    if canonical and alias and canonical != alias:
        raise ValueError(
            f"AUTHOR_ENGINE={alias} and COORDINATOR_ENGINE={canonical} disagree; "
            "set only COORDINATOR_ENGINE (AUTHOR_ENGINE is its deprecated alias)"
        )
    if alias and not canonical and "AGENT_FACTORY_AUTHOR_ENGINE" not in _warned:
        _warned.add("AGENT_FACTORY_AUTHOR_ENGINE")
        log.warning(
            "environment variable AGENT_FACTORY_AUTHOR_ENGINE is deprecated and will be removed; use %s",
            COORDINATOR_ENGINE,
        )
    return canonical or alias
