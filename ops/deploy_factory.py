"""Deploy only the factory apps from a reviewed git ref.

That is factory, factory-mcp and factory-deployer, plus factory-webhook
when the profile sets `[github] webhook_secret` (and factory-issues when it also sets `maintainers`)
and factory-reconcile when it has `[board]` or the GitHub App.

Set [mcp] public_url before deploying.

    python ops/deploy_factory.py --ref origin/main [--dry-run]
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
ALLOWED = set(_deploy.factory_envs(PROFILE))
_CHILD = _deploy.FACTORY_CHILD
check_environments = _deploy.check_environments


def main(argv: list[str] | None = None) -> int:
    """Deploy the factory apps from `--ref`; see the module docstring."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-localhost", action="store_true")
    args = ap.parse_args(argv)
    if os.environ.get("FACTORY_MCP_ALLOW_LOCALHOST") and not args.allow_localhost:
        ap.error("FACTORY_MCP_ALLOW_LOCALHOST requires --allow-localhost")
    subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "origin"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--verify", f"{args.ref}^{{commit}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    env = _deploy.factory_env(os.environ, PROJECT, DOMAIN)
    with _deploy.worktree(REPO, sha, "deploy-factory-") as tree:
        print(
            f"deploying {', '.join(sorted(ALLOWED))} from {args.ref} ({sha})"
            f"{' [dry run]' if args.dry_run else ''}",
            flush=True,
        )
        return _deploy.deploy_factory(
            PROFILE, tree / "src", project=PROJECT, domain=DOMAIN, env=env, dry_run=args.dry_run
        )


if __name__ == "__main__":
    sys.exit(main())
