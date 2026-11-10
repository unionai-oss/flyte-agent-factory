"""Coordinator-only live Ready/priority/completion/restart smoke; never part of offline checks.

Use a dedicated scratch profile with one slot, planner admission, and no other Ready
items or active projects. Configure [direction], [board], the GitHub App and webhook,
then deploy that profile. Supply GITHUB_APP_PRIVATE_KEY, GITHUB_BOARD_PAT and
_UNION_EAGER_API_KEY in the environment using the normal secret tooling.

    uv run python ops/probes/planner_smoke.py --profile SCRATCH --repo OWNER/REPO --nonce UNIQUE

The repository must be configured in that profile and disposable. This creates two
issues and board cards, labels the second priority, wakes admission, checks that it
launches first, restarts the webhook app, and waits for both generations to finish.
It prints issue/run URLs for QA to retain. Inspect the exact-head QA outputs/PRs
separately: terminal Flyte success is not a QA verdict. The script leaves the issues,
cards and runs for inspection; the coordinator closes them after review.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from collections.abc import Callable
from functools import partial
from typing import TypeVar

import flyte
from flyte import remote

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _board, _factory, _factory_base, _github, _issues, _planner_runtime  # noqa: E402 -- after the profile

_T = TypeVar("_T")


def wait_for(read: Callable[[], _T], description: str, timeout: float = 3600) -> _T:
    """Return `read()` once it is truthy, polling every 3 seconds; RuntimeError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if value:
            return value
        time.sleep(3)
    raise RuntimeError(f"timed out waiting for {description}")


def wait_for_projects(issues: list[str], runs: Callable[[str], list[remote.Run]]) -> None:
    """Wait for each issue's single project run to launch and finish, printing its URL."""
    for issue in reversed(issues):
        found = wait_for(partial(runs, issue), f"launch of {issue}")
        if len(found) != 1:
            raise RuntimeError(f"duplicate generation for {issue}")
        run = found[0]
        wait_for(
            partial(
                lambda name: _factory_base._phase(remote.Run.get(name=name)) in _planner_runtime.TERMINAL,
                run.name,
            ),
            f"completion of {run.name}",
        )
        print(run.url, flush=True)


def main() -> None:
    """Run the planner smoke test against `--repo`, tagging its issues with `--nonce`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{8,40}", args.nonce):
        parser.error("nonce must be 8-40 lowercase letters, digits or hyphens")
    profile = _factory_base.PROFILE
    if profile.planner.admission != "planner" or len(profile.slots) != 1:
        parser.error("use a dedicated one-slot scratch profile in planner mode")
    if args.repo.lower() not in _issues.repo_targets(profile):
        parser.error("scratch repository must be configured in the profile")
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"],
        project=_factory.PROJECT,
        domain=_factory.DOMAIN,
    )
    key = os.environ["GITHUB_APP_PRIVATE_KEY"]
    app = _github.GithubApp(profile.github.app_id, key)
    board = _board.BoardClient(os.environ[_board.TOKEN_ENV])
    assert profile.board is not None
    board.bootstrap(profile.board.owner, profile.board.owner_type, profile.board.number, create=False)
    if any(row["status"] == "Ready" for row in board.items()) or _factory.operated_live():
        raise RuntimeError("scratch board and slots must be idle before the smoke")
    if app.search_issues(args.repo, args.nonce):
        raise RuntimeError("nonce already used; choose a fresh one")
    issues: list[str] = []
    cards = []
    for suffix in ("ordinary", "priority"):
        body = (
            f"Planner smoke {args.nonce} {suffix}. Create smoke/{args.nonce}-{suffix}.txt "
            f"containing exactly '{args.nonce} {suffix}'. Run a check of its contents, obtain "
            "independent QA and open a PR. Do not merge it.\n\n"
            f"## Files\n- smoke/{args.nonce}-{suffix}.txt\n"
        )
        issue = app.create_issue(
            args.repo,
            f"Planner smoke {args.nonce} {suffix}",
            body,
            labels=[profile.github.intake_label, *(["priority"] if suffix == "priority" else [])],
        )
        ref = f"{args.repo}#{issue['number']}"
        issues.append(ref)
        cards.append(board.add_item(board.issue_node_id(ref)))
        print(issue["html_url"], flush=True)
    # Ready is set only after both cards exist; the ordinary card remains earlier in board order.
    for card in reversed(cards):
        board.set_status(card, "Ready")
    for _ in range(20):
        _planner_runtime.notify(profile, key)

    def runs(issue: str) -> list[remote.Run]:
        return list(remote.Run.listall(with_labels={"factory-issue": _issues.issue_label(issue)}))

    first = wait_for(lambda: runs(issues[1]), "priority launch")
    if len(first) != 1 or runs(issues[0]):
        raise RuntimeError("priority item did not exclusively take the first free slot")
    webhook = remote.App.get(name="factory-webhook", project=_factory.PROJECT, domain=_factory.DOMAIN)
    webhook.deactivate(wait=True)
    webhook.activate(wait=True)
    _planner_runtime.notify(profile, key)
    wait_for_projects(issues, runs)
    _planner_runtime.notify(profile, key)
    time.sleep(10)
    if any(len(runs(issue)) != 1 for issue in issues):
        raise RuntimeError("completion/restart produced a duplicate generation")
    print("Smoke observations collected; QA must inspect outcomes and exact-head review evidence.")


if __name__ == "__main__":
    main()
