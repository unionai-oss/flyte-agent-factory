"""Print what the planner would launch from live facts, submitting nothing.

    python ops/plan_dry_run.py --profile NAME

Reads the board, the slots' live runs and their reports, and each repository's open pull requests
and file list, then prints the proposal as JSON: `launches`, `waiting` (with the structured
reason) and `unusable_slots`. It needs the board token (`GITHUB_BOARD_PAT`), the GitHub App key
(`GITHUB_APP_PRIVATE_KEY`) and Flyte credentials (`_UNION_EAGER_API_KEY`), by name only.
A launch outside the planner (a manual run) shows up as a live run with no issue and occupies its
whole repository.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from typing import Any

import flyte

from flyte_agent_factory import _profile

_profile.select_from_argv()  # before _factory reads the profile at import

from flyte_agent_factory import _factory, _factory_base, _planner  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first
from flyte_agent_factory._planner_facts import free_capacity  # noqa: E402, F401 -- after the profile; re-exported for tests
from flyte_agent_factory._planner_facts import read_facts as _read_facts  # noqa: E402 -- after the profile is chosen


def _init() -> None:
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"], project=_factory.PROJECT, domain=_factory.DOMAIN
    )


def read_facts(profile: _profile.Profile) -> _planner.Facts:
    """Return the planner's facts for `profile`, initialising the factory first."""
    _init()
    return _read_facts(profile)


def _name(item: _planner.BoardItem | _planner.ManualRequest) -> str:
    return f"{item.repo}#{item.number}" if isinstance(item, _planner.BoardItem) else item.name


def describe(plan: _planner.Plan) -> dict[str, Any]:
    """Return `plan` as plain data: launches, waiting items with reasons, and unusable slots."""
    return {
        "launches": [
            {
                "item": _name(launch.item),
                "engine": launch.engine,
                "login": launch.login,
                "predicted_files": list(launch.predicted_files),
                "reason": launch.reason,
            }
            for launch in plan.launches
        ],
        "waiting": [
            {
                "item": _name(wait.item),
                "reason": wait.reason.code,
                "with": wait.reason.with_identity,
                "detail": wait.reason.detail,
            }
            for wait in plan.waiting
        ],
        "unusable_slots": [{"login": reason.with_identity} for reason in plan.unusable_slots],
    }


def main(
    argv: list[str] | None = None, read: Callable[[_profile.Profile], _planner.Facts] = read_facts
) -> None:
    """Print, as JSON, what the planner would do now; nothing is launched."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    parser.parse_args(argv)
    print(json.dumps(describe(_planner.plan(read(_factory_base.PROFILE))), indent=2))


if __name__ == "__main__":
    main()
