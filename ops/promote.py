"""Promote a reviewed workspace project to another repository as a pull request.

`promote.py <workspace> <project> <target-repo-url> <branch> [--dry-run]`. Uses YOUR git credentials; the
factory holds none for the target. See `flyte_agent_factory._promote` for the steps and the patch format.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flyte_agent_factory import _promote


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="checkout of the workspace repository")
    parser.add_argument("project", help="the project name: its patches are projects/<name>/patches/*.patch")
    parser.add_argument("target", help="URL of the repository to promote to")
    parser.add_argument("branch", help="branch to create or update in the target")
    parser.add_argument("--dry-run", action="store_true", help="do everything but push")
    args = parser.parse_args(argv)
    report = _promote.promote(args.workspace, args.project, args.target, args.branch, dry_run=args.dry_run)
    print(f"{report.stage}: {report.message}" + (f"\n{report.url}" if report.url else ""))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
