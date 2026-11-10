"""Create a workspace repository from the template shipped with the engine: `new_workspace.py <dest>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from flyte_agent_factory import _workspace


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", type=Path, help="an absent or empty directory")
    args = parser.parse_args(argv)
    try:
        _workspace.create(args.dest)
    except FileExistsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    print(
        f"created {args.dest}: replace OWNER in CODEOWNERS, then copy an example from the engine's profiles/ "
        "to its profiles/<name>.toml and fill it in (docs/quickstart.md, step 3)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
