"""Condition naming across task retries.

A retry is a new attempt of the same action, so a per-process counter produces
a name that already exists. On a months-long project retries are routine, which
makes this the normal path rather than an edge case.
"""

import pytest

from flyte_agent_factory import _conditions


class _FakeCondition:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def recorder(monkeypatch):
    """Stand in for the backend: a set of names already taken."""
    taken: set[str] = set()
    created: list[str] = []

    class FakeAlreadyExists(RuntimeError):
        pass

    async def fake_new_condition(name, /, **kwargs):
        if name in taken:
            raise FakeAlreadyExists(name)
        taken.add(name)
        created.append(name)
        return _FakeCondition(name)

    import flyte
    import flyte.errors

    monkeypatch.setattr(flyte.errors, "ConditionAlreadyExistsError", FakeAlreadyExists)
    monkeypatch.setattr(flyte, "new_condition", type("S", (), {"aio": staticmethod(fake_new_condition)}))
    monkeypatch.setattr(_conditions, "attempt_label", lambda: "a1")
    return taken, created


async def test_names_are_namespaced_and_padded(recorder):
    _, created = recorder
    condition, seq = await _conditions.open_condition("direct", 1, data_type=str)

    assert condition.name == "direct-a1-001"
    assert seq == 1
    assert created == ["direct-a1-001"]


async def test_collision_steps_forward_and_reports_the_new_seq(recorder):
    taken, _ = recorder
    taken.update({"direct-a1-001", "direct-a1-002"})

    condition, seq = await _conditions.open_condition("direct", 1, data_type=str)

    # The caller has to keep counting from here, or it collides again next time.
    assert condition.name == "direct-a1-003"
    assert seq == 3


async def test_gives_up_rather_than_looping_forever(recorder):
    taken, _ = recorder
    taken.update(f"direct-a1-{n:03d}" for n in range(1, _conditions.MAX_COLLISION_RETRIES + 2))

    with pytest.raises(RuntimeError, match="consecutive names were taken"):
        await _conditions.open_condition("direct", 1, data_type=str)


def test_attempt_label_is_safe_outside_a_task():
    # Local runs and tests have no task context; naming must still work.
    assert _conditions.attempt_label() == "a0"
