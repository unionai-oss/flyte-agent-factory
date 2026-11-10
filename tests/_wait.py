"""Condition waits for tests: poll until something is true, with a generous bound.

A test must never assert that something happened within N milliseconds: under 12 parallel workers on a few
cores a thread or subprocess can be starved for seconds. Wait for the condition instead; the timeout is only
the point at which a hang is reported, so it is long.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


async def wait_for(
    predicate: Callable[[], object], timeout: float = 30.0, interval: float = 0.01, what: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"condition never became true: {what or predicate}"
        await asyncio.sleep(interval)


def wait_for_sync(
    predicate: Callable[[], object], timeout: float = 30.0, interval: float = 0.01, what: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"condition never became true: {what or predicate}"
        time.sleep(interval)
