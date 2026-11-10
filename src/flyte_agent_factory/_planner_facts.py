"""Shared live readers for dry runs and the admission task."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from flyte import remote

from . import _board, _deploy, _factory, _factory_base, _factory_meters, _github, _loop, _planner, _queue
from ._meter import Meter

if TYPE_CHECKING:
    from ._profile import Profile


def free_capacity(
    profile: Profile,
    live: list[remote.Run],
    meters: dict[str, list[Meter]],
    reserves: Mapping[str, float] | None = None,
) -> list[_planner.Capacity]:
    """Each free slot's Claude and Codex tokens, tagged with the slot so it takes one launch.

    `meters` is `_factory_meters.slot_meters`: the readings in each slot's newest run's report.
    `reserves` is each engine's reserve constant (`_queue.read_reserves`).
    """
    reserves = reserves or {}
    busy = {_factory_base._task_name(run) for run in live}
    free = [slot for slot in profile.slots if slot.task not in busy][
        : max(0, _factory_base.cap() - len(busy))
    ]
    # A refusal belongs to the credential, including observations from busy slots.
    readings: dict[str, Meter] = {}
    for slot in profile.slots:
        for meter in meters.get(slot.task, []):
            account = f"codex {profile.codex_secret(slot)}" if meter.account == "codex" else meter.account
            previous = readings.get(account)
            if previous is None:
                readings[account] = meter
                continue
            newest = max((previous, meter), key=lambda m: m.observed_at)
            until = max(previous.limited_until or 0, meter.limited_until or 0)
            readings[account] = (
                replace(newest, limited_until=until) if until > (newest.limited_until or 0) else newest
            )
    found: list[_planner.Capacity] = []
    for slot in free:
        claude = _factory_meters.match_claude_meter(profile, slot, readings, meters)
        found.append(
            _planner.Capacity(
                "claude", profile.secrets.claude_token, claude, slot.task, reserves.get("claude", 0.0)
            )
        )
        if codex_secret := profile.codex_secret(slot):
            codex = readings.get(f"codex {codex_secret}")
            found.append(
                _planner.Capacity("codex", codex_secret, codex, slot.task, reserves.get("codex", 0.0))
            )
    return found


def live_children(profile: Profile) -> list[_planner.ActiveRun]:
    """Keep surviving child edits occupied even after their project run ends."""
    found: list[_planner.ActiveRun] = []
    for slot in profile.slots:
        # A time window would release files of a long-lived child whose parent ended earlier.
        for parent in remote.Run.listall(task_name=slot.task, limit=1000000):
            children = [
                action
                for action in remote.Action.listall(
                    for_run_name=parent.name, in_phase=_factory_base.SERVER_LIVE
                )
                if action.parent_name and _factory_base._phase(action) in _factory_base.LIVE
            ]
            if not children:
                continue
            repo = parent.inputs().get("repo") or profile.repo
            repo = profile.repos.get(repo, repo)
            if not repo:
                raise ValueError(f"cannot determine repository of surviving children of {parent.name}")
            labels = _factory_base.run_labels(parent)
            # Actions do not expose their input contracts through the remote API. Unknown
            # child scope therefore occupies its parent's whole repository, including QA.
            found.extend(
                _planner.ActiveRun(
                    name=f"{parent.name}/{action.name}",
                    repo=repo,
                    issue=labels.get(_planner.LABEL_ISSUE, ""),
                    generation=labels.get(_planner.LABEL_GEN, ""),
                    phase=_factory_base._phase(action),
                    labels=labels,
                )
                for action in children
            )
    return found


def _github_repos(profile: Profile) -> list[str]:
    """`owner/repo` of every profile repository that is on GitHub."""
    found: list[str] = []
    for url in profile.repos.values():
        try:
            found.append(_github.parse_repo(url))
        except _github.GithubError:
            continue  # no pull requests or file list to read for it
    return found


def read_facts(profile: Profile) -> _planner.Facts:
    """Return the live facts; every call is a read."""
    missing = [name for name in (_board.TOKEN_ENV, _deploy.GITHUB_KEY_ENV) if not os.environ.get(name)]
    if profile.board is None or missing:
        raise ValueError(f"planner needs a [board] in the profile and these variables: {', '.join(missing)}")
    board = _board.BoardClient(os.environ[_board.TOKEN_ENV])
    board.bootstrap(profile.board.owner, profile.board.owner_type, profile.board.number, create=False)
    app = _github.GithubApp(profile.github.app_id, os.environ[_deploy.GITHUB_KEY_ENV])
    repos = _github_repos(profile)
    live = _factory.operated_live()
    facts = _planner.facts_from(
        board.items(),
        live,
        [pull for repo in repos for pull in app.open_pulls(repo)],
        free_capacity(
            profile,
            live,
            _factory_meters.slot_meters(profile.tasks),
            _queue.read_reserves(profile.state.uri) if profile.state.uri else {},
        ),
        [],
        tracked_paths={repo: app.tracked_paths(repo) for repo in repos},
        repo_aliases={
            alias: profile.repos[key] for key, aliases in profile.repo_aliases.items() for alias in aliases
        },
        default_repo=repos[0] if repos else profile.repo,
        now=time.time(),
    )
    return replace(
        facts,
        active_runs=(*facts.active_runs, *live_children(profile)),
        engine_repo=profile.repos.get("factory", ""),
        engine_projects=profile.factory_projects,
        loop_enabled=profile.loop.enabled,
        loop_killed=profile.loop.enabled and _loop.killed(profile, app),
        author_engine=profile.policy.get("COORDINATOR_ENGINE", "").strip().lower() or "claude",
    )
