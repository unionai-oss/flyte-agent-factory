"""Create a workspace repository from the template shipped with the engine: `new_workspace.py <dest>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# A fresh clone runs under `uv run`, which does not install this project (`package = false`); without
# this the import below fails with ModuleNotFoundError (as in ops/bootstrap.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _workspace


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dest", type=Path, help="an absent or empty directory, or a fresh clone")
    parser.add_argument("--commit", action="store_true", help="commit the result in an existing clone")
    args = parser.parse_args(argv)
    try:
        _workspace.create(args.dest, commit=args.commit)
    except FileExistsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"created {args.dest}: replace OWNER in CODEOWNERS, then copy profile.example.toml to profile.toml "
        "at the workspace root and fill it in (docs/quickstart.md)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
