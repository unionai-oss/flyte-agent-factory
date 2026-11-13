"""Import another repository into a workspace project, and deliver the project's changes back as a PR.

    promote.py import  <target-url> <project-dir> [--ref BRANCH] [--path DIR] [--new]
    promote.py deliver <project-dir> <branch> [--to URL] [--fork URL] [--title TEXT]
                       [--base SHA] [--dco | --no-dco] [--fixes OWNER/REPO#N] [--dry-run] [--replace]

Uses YOUR git credentials and identity; the factory holds none for the target. `import` commits the
target's tree and `promote.toml` in the workspace (and pushes nothing). `deliver` makes one commit by you on a
branch of the target's current default branch, scans it, runs the target's own CI, pushes and opens the
pull request; it never merges. A branch that already exists on the target is replaced only if it is your
earlier promotion, or with `--replace`. For a new repository (`--new`) it pushes the first commit and opens no
pull request. See `flyte_agent_factory._promote` for the steps.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flyte_agent_factory import _delivery, _promote


def build_parser() -> argparse.ArgumentParser:
    """Return the command line; `tests/test_docs_promotion.py` reads the docs' usage against it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    imported = sub.add_parser("import", help="write the target's files into a project and commit them")
    imported.add_argument("target", help="URL of the repository the project delivers to")
    imported.add_argument("project", type=Path, help="the project directory in the workspace checkout")
    imported.add_argument("--ref", default="", help="branch or tag to import (default: the default branch)")
    imported.add_argument("--path", default=".", help="where the tree goes inside the project (default: .)")
    imported.add_argument("--new", action="store_true", help="a repository that does not exist yet: no clone")

    deliver = sub.add_parser(
        "deliver", help="push the project's committed tree to the target as a pull request"
    )
    deliver.add_argument("project", type=Path, help="the project directory in the workspace checkout")
    deliver.add_argument("branch", help="branch to create or update in the target (or the fork)")
    deliver.add_argument("--to", default="", help="URL of the target (default: promote.toml's repo)")
    deliver.add_argument("--fork", default="", help="URL of your fork to push the branch to instead")
    deliver.add_argument("--title", default="", help="commit and pull request title")
    deliver.add_argument(
        "--base", default="", help="commit the tree was made from (a project with no promote.toml)"
    )
    deliver.add_argument(
        "--dco",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="sign off the commit (default: when the target asks for it)",
    )
    deliver.add_argument(
        "--fixes",
        action="append",
        default=[],
        metavar="OWNER/REPO#N",
        help="a public issue the pull request resolves: a `Fixes` line in its body (repeatable)",
    )
    deliver.add_argument("--dry-run", action="store_true", help="do everything but push")
    deliver.add_argument(
        "--replace",
        action="store_true",
        help="overwrite the branch if it exists and is not your earlier promotion",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    args = build_parser().parse_args(argv)
    if args.command == "import":
        try:
            delivery, message = _delivery.import_tree(
                args.target, args.project, ref=args.ref, path=args.path, new=args.new
            )
        except _delivery.DeliveryError as exc:
            print(f"import: {exc}", file=sys.stderr)
            return 1
        print(f"{message}\nbase {delivery.base or '(new repository)'}; push the workspace to keep it.")
        return 0
    report = _promote.deliver(
        args.project,
        args.branch,
        to=args.to,
        fork=args.fork,
        title=args.title,
        base=args.base,
        dco=args.dco,
        fixes=args.fixes,
        dry_run=args.dry_run,
        replace=args.replace,
    )
    print(f"{report.stage}: {report.message}" + (f"\n{report.url}" if report.url else ""))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
