"""Opening Flyte conditions with names that stay unique across task retries.

A condition name has to be unique within its action, and a retry is a new
attempt of the *same* action -- so the obvious `direct-001`, `direct-002`
counter collides the moment the pod restarts and the counter resets. Since
retries are how this agent survives eviction on a months-long project, that
collision is the normal case rather than an edge one.

Names are therefore namespaced by attempt number, and a collision is still
retried rather than killing the project.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Generic, Literal, TypedDict, TypeVar, Unpack

if TYPE_CHECKING:
    from flyte._condition import ConditionWebhook, _Condition


_T = TypeVar("_T", bool, int, float, str)


class _ConditionOptions(TypedDict, Generic[_T], total=False):
    prompt: str
    prompt_type: Literal["text", "markdown"]
    data_type: type[_T]
    description: str
    timeout: timedelta | int | float | None
    webhook: ConditionWebhook | None


MAX_COLLISION_RETRIES = 50


def attempt_label() -> str:
    """Return a short, stable tag for the current attempt, e.g. `a1`."""
    import flyte

    try:
        ctx = flyte.ctx()
        return f"a{ctx.attempt_number}" if ctx is not None else "a0"
    except (RuntimeError, AttributeError):  # outside a task context (local runs, tests)
        return "a0"


async def open_condition(
    base: str, seq: int, /, **kwargs: Unpack[_ConditionOptions[_T]]
) -> tuple[_Condition[_T], int]:
    """Create a condition named `<base>-<attempt>-<seq>`, stepping past collisions.

    Returns `(condition, seq)` -- the sequence number is returned because a
    collision advances it, and the caller has to keep counting from where this
    left off.
    """
    import flyte
    from flyte.errors import ConditionAlreadyExistsError

    label = attempt_label()
    for _ in range(MAX_COLLISION_RETRIES):
        try:
            condition = await flyte.new_condition.aio(f"{base}-{label}-{seq:03d}", **kwargs)
            return condition, seq
        except ConditionAlreadyExistsError:
            seq += 1
    raise RuntimeError(
        f"could not open a {base!r} condition: {MAX_COLLISION_RETRIES} consecutive names were taken"
    )
