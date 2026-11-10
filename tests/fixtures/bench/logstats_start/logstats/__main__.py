"""Command line entry point: `python -m logstats FILE ...` prints the summary."""

import sys

from logstats.cli import main

if __name__ == "__main__":
    sys.exit(main())
