"""Probe whether a reviewer-shaped child action can be placed.

Compares the reusable (actor) shape with a plain task of the same resources.

Finding: plain tasks place in seconds; reusable ones sit in WAITING_FOR_RESOURCES and, in
the agent image, finally die with `exec: "unionai-actor-bridge": executable file not found`. Child actions
of the calling run; short bounds.
"""

import asyncio
import os
from datetime import timedelta
from typing import Any

import flyte
import flyte.errors

from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory.child import attach

attach()
IMG = os.environ.get("FLYTE_INTERNAL_IMAGE") or os.environ["_F_IMG_ID"]
RES = flyte.Resources(cpu=(1, 4), memory=("2Gi", "8Gi"), disk="50Gi")
BOUND = flyte.Timeout(max_queued_time=timedelta(minutes=3))

plain = flyte.TaskEnvironment(name="probe-plain", image=IMG, resources=RES, interruptible=True)
actor = flyte.TaskEnvironment(
    name="probe-actor",
    image=IMG,
    resources=RES,
    interruptible=True,
    reusable=flyte.ReusePolicy(
        replicas=(0, 1), concurrency=2, idle_ttl=timedelta(minutes=2), scaledown_ttl=timedelta(minutes=1)
    ),
)


@plain.task
async def hello_plain() -> str:
    """Return a fixed string from a plain task."""
    return "plain ok"


@actor.task
async def hello_actor() -> str:
    """Return a fixed string from a reusable (actor) task."""
    return "actor ok"


async def attempt(name: str, task: Any, interruptible: bool) -> str:
    """Run `task` with the given interruptibility and return the outcome as a line of text."""
    try:
        out = await task.override(interruptible=interruptible, timeout=BOUND).aio()
        return f"{name} interruptible={interruptible}: {out}"
    except REMOTE_ERRORS as e:  # a failure here is a finding, recorded in the result
        return f"{name} interruptible={interruptible}: {type(e).__name__}: {str(e)[:200]}"


async def main() -> None:
    """Try both task shapes, interruptible or not, and print what happened to each."""
    results = await asyncio.gather(
        attempt("plain", hello_plain, True),
        attempt("plain", hello_plain, False),
        attempt("actor", hello_actor, True),
        attempt("actor", hello_actor, False),
    )
    print("\n".join(results))


asyncio.run(main())
