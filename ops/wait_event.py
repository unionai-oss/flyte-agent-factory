"""Block until a run needs attention, then print why and exit.

Exits on: a new open (PAUSED) `ask-*` condition (the standing `direct-*` inbox is ignored), a terminal
phase, or `--max` seconds.
The operator re-runs it after dealing with each event, so one process = one event.
"""

from __future__ import annotations

import argparse
import os
import time

import flyte
from flyte import remote

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first

DONE = ("SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT")


def main() -> None:
    """Wait until the run has a new open ask or ends, or `--max` seconds pass, and print which."""
    p = argparse.ArgumentParser()
    p.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    p.add_argument("run")
    p.add_argument("--seen", default="", help="comma-separated condition names already handled")
    p.add_argument("--max", type=int, default=1500)
    p.add_argument("--every", type=int, default=30)
    args = p.parse_args()
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"],
        project=_factory.PROJECT,
        domain=_factory.DOMAIN,
    )
    seen = {s for s in args.seen.split(",") if s}
    deadline = time.time() + args.max
    while time.time() < deadline:
        run = remote.Run.get(args.run)
        phase = str(run.phase).split(".")[-1]
        for c in remote.Condition.listall(run_name=args.run):
            if "PAUSED" in c.phase and c.name not in seen and not c.name.startswith("direct-"):
                print(f"OPEN CONDITION {c.name}")
                return
        if phase in DONE:
            print(f"RUN {phase}")
            return
        time.sleep(args.every)
    print("TIMEOUT (nothing new)")


if __name__ == "__main__":
    main()
