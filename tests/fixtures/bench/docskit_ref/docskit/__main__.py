"""Command line entry point."""

import argparse
import sys

from docskit import stats, text


def main(argv: list[str] | None = None) -> int:
    """Run the command in `argv` (default: `sys.argv[1:]`) and return the exit status.

    >>> main(["slug", "Hello, World!"])
    hello-world
    0
    """
    parser = argparse.ArgumentParser(prog="docskit")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("slug").add_argument("text")
    commands.add_parser("summary").add_argument("numbers", nargs="+", type=float)
    args = parser.parse_args(argv)
    if args.command == "slug":
        print(text.slugify(args.text))
    else:
        print(f"mean={stats.mean(args.numbers):g} median={stats.median(args.numbers):g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
