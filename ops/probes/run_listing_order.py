"""Measure A1 on the live tenant: two bursts of twenty creates and immediate run listings.

From an attached factory session with this checkout on PYTHONPATH:
    PYTHONPATH=src python ops/probes/run_listing_order.py > run-listing-order.json

The image must already exist. This deploys only the nonce-scoped probe environment. Each creator
is a child action of the session; it submits a labelled no-op run through the same API the deployer
uses. Nonces on every task input prevent cached child results from hiding a create or list.
Retain the JSON with the tenant/run link. Set _deployer.RUN_LISTING_CONSISTENT to true only if both
rounds pass; otherwise keep the git-ref fallback. No object-storage state or production triggers change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid
from datetime import timedelta
from typing import Any, cast

import flyte
import flyte.errors
from flyte import remote

from flyte_agent_factory.child import attach

env = flyte.TaskEnvironment(
    name="probe-run-listing",
    image=os.environ.get("FLYTE_INTERNAL_IMAGE", "python:3.12-slim"),
    resources=flyte.Resources(cpu=1, memory="512Mi"),
    interruptible=True,
)
bound = flyte.Timeout(max_runtime=timedelta(minutes=2), max_queued_time=timedelta(minutes=3))


@env.task(timeout=bound)
async def noop(nonce: str) -> str:
    """Return `nonce`; the task the probe launches."""
    return nonce


@env.task(timeout=bound)
async def create_and_list(cohort: str, nonce: str, task_name: str) -> dict[str, Any]:
    """Create a run in `cohort` and return its name with the cohort runs listed right after."""
    task = remote.Task.get(name=task_name, auto_version="latest")
    run = await flyte.with_runcontext(mode="remote", labels={"listing-probe": cohort}).run.aio(
        task, nonce=nonce
    )
    seen = [
        r.name
        async for r in remote.Run.listall.aio(
            task_name=task_name,
            with_labels={"listing-probe": cohort},
            sort_by=("created_at", "asc"),
            limit=100,
        )
    ]
    return {"run": run.name, "seen": seen, "nonce": nonce}


async def probe() -> dict[str, Any]:
    """Create runs concurrently and report whether each sees its older siblings in the listing."""
    if not os.environ.get("FLYTE_INTERNAL_IMAGE"):
        raise RuntimeError("run this probe in an attached session with FLYTE_INTERNAL_IMAGE")
    ctx = attach()
    nonce = uuid.uuid4().hex
    task_name = f"{env.name}.noop"
    await asyncio.to_thread(flyte.deploy, env)
    rounds = []
    for round_number in range(2):
        cohort = uuid.uuid4().hex

        async def create(cohort: str = cohort) -> dict[str, Any]:
            inputs = {"cohort": cohort, "nonce": uuid.uuid4().hex, "task_name": task_name}
            try:
                return cast("dict[str, Any]", await create_and_list.aio(**inputs))  # aio() is mistyped
            except flyte.errors.MaxQueuedTimeExceededError:
                # A fresh nonce forces the on-demand retry to execute even if a child cache is enabled.
                inputs["nonce"] = uuid.uuid4().hex
                retry = create_and_list.override(
                    interruptible=False, timeout=flyte.Timeout(max_runtime=timedelta(minutes=2))
                )
                return cast("dict[str, Any]", await retry.aio(**inputs))

        observations = await asyncio.gather(*(create() for _ in range(20)))
        order = [
            r.name
            async for r in remote.Run.listall.aio(
                task_name=task_name,
                with_labels={"listing-probe": cohort},
                sort_by=("created_at", "asc"),
                limit=100,
            )
        ]
        for row in observations:
            row["missing_older"] = (
                sorted(set(order[: order.index(row["run"])]) - set(row["seen"]))
                if row["run"] in order
                else ["own run missing from final listing"]
            )
            row["saw_self"] = row["run"] in row["seen"]
            row["stable_order"] = row["seen"] == [name for name in order if name in row["seen"]]
        passed = (
            len(order) == 20
            and len(set(order)) == 20
            and all(
                row["saw_self"] and row["stable_order"] and not row["missing_older"] for row in observations
            )
        )
        rounds.append(
            {"round": round_number + 1, "order": order, "observations": observations, "consistent": passed}
        )
    return {
        "nonce": nonce,
        "parent_run": ctx.action.run_name,
        "rounds": rounds,
        "RUN_LISTING_CONSISTENT": all(r["consistent"] for r in rounds),
    }


if __name__ == "__main__":
    with contextlib.redirect_stdout(sys.stderr):
        result = asyncio.run(probe())
    print(json.dumps(result, indent=2))
