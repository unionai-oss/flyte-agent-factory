"""Deploy the agent's code to the profile's work slots -- and nothing else.

Operated projects run on a slot's `<env>.project`, so a fix to the supervisor helps
nobody until it is deployed there. That round trip went through a human every
time (the deployment's operations log, runs 1-2), so it is scripted here, with the recipe copied
from the deployed task's own spec rather than remembered.

Everything deployment-specific comes from the profile (`--profile` or
$AGENT_FACTORY_PROFILE; see profiles/README.md):

    env name  one per slot                    (the profile's [[slots]])
    token     [secrets] claude_token          (the one work token every slot runs on)
    secrets   [secrets]: deploy key, gateway key, API key  (sandbox repo only)
              [repo_keys]: one deploy key per further repo  (e.g. the factory repo)

Each slot is a separate environment with its own sub-agent environment, all on the one
work token.

It deploys a git ref (default `origin/main`, i.e. what a human merged) from a
throwaway worktree, so local edits never ship. It refuses any other environment
name. Each slot's sub-agent environment is redeployed alongside, because
`project` calls it; a running run keeps the version it started with.

    python ops/deploy_work.py                 # deploy origin/main
    python ops/deploy_work.py --ref <sha>     # a specific merged commit
    python ops/deploy_work.py --dry-run
    python ops/deploy_work.py --slot <env name>      # one slot only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from flyte_agent_factory import _deploy, _profile

_profile.select_from_argv()  # before the profile is read below

REPO = Path(__file__).resolve().parent.parent
PROFILE = _profile.require()
PROJECT = os.environ.get("OPERATE_PROJECT", PROFILE.backend.project)
DOMAIN = os.environ.get("OPERATE_DOMAIN", PROFILE.backend.domain)

ENV_NAME = PROFILE.slots[0].env
#: env name -> sub-agent env name.
SLOTS = _deploy.slot_table(PROFILE)
#: What a slot is deployed with (originally read from the deployed task's spec), including
#: the Codex token mounted into sub-agents: a second engine with its own meter.
RECIPE = PROFILE.deploy_env(PROFILE.slots[0])
#: Environments this script may touch. Anything else in the deployment aborts it.
ALLOWED = _deploy.slot_envs(PROFILE)


def recipe_env(base: dict[str, str] | None = None, slot: str = ENV_NAME) -> dict[str, str]:
    """Return the child's environment: the caller's, with the recipe for `slot` forced over it."""
    return _deploy.recipe_env(PROFILE, slot, base)


def check_recipe(env: dict[str, str]) -> None:
    """Refuse anything that is not a work slot on the profile's work token."""
    _deploy.check_recipe(PROFILE, env)


def main(argv: list[str] | None = None) -> int:
    """Deploy the work apps of the selected slots from `--ref`."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slot", choices=sorted(SLOTS), action="append", help="default: every slot")
    args = ap.parse_args(argv)
    slots = args.slot or list(SLOTS)

    subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "origin"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", args.ref], check=True, capture_output=True, text=True
    ).stdout.strip()
    _deploy.check_slots(PROFILE, slots)  # refuse before anything is deployed
    with _deploy.worktree(REPO, sha, "deploy-work-") as tree:
        rc = _deploy.deploy_slots(
            PROFILE,
            tree / "src",
            slots,
            project=PROJECT,
            domain=DOMAIN,
            ref=f"{args.ref} ({sha})",
            dry_run=args.dry_run,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
