"""Behaviour that lost its only test when the login code it sat beside was removed (015 item 1).

Each guard still exists after the bootstrap-login removal; these pin it so it cannot quietly go.
"""

import asyncio
import inspect
from typing import Any, cast

import flyte.errors
import pytest

from flyte_agent_factory import _auth, _author, _supervise_support, _timeline, agent


class _Proc:
    def __init__(self, login_expired: bool) -> None:
        self.login_expired = login_expired


async def test_a_non_recoverable_error_in_a_loop_ends_the_run_rather_than_one_loop():
    """A logged-out run once kept reporting RUNNING after the pump raised: the error became a note."""

    async def pump() -> None:
        raise flyte.errors.NonRecoverableError("the login has expired")

    stop, timeline = asyncio.Event(), _timeline._Timeline()
    fatal: list[BaseException] = []
    await _supervise_support._supervised(pump(), "pump", timeline, stop, fatal=fatal)

    assert stop.is_set()
    assert len(fatal) == 1 and isinstance(fatal[0], flyte.errors.NonRecoverableError)
    assert not any("loop stopped" in text for _, _, text in timeline._rows)


def test_the_project_task_raises_the_fatal_error_it_was_handed():
    assert "raise fatal[0]" in inspect.getsource(agent.project.func)


def test_an_expired_login_fails_the_run_with_the_setup_token_remedy():
    _author._assert_logged_in(cast(Any, _Proc(login_expired=False)))  # a live session passes
    with pytest.raises(flyte.errors.NonRecoverableError) as caught:
        _author._assert_logged_in(cast(Any, _Proc(login_expired=True)))
    message = str(caught.value)
    assert "claude setup-token" in message and "[secrets] claude_token" in message
    assert "*resumed*" in message and "Relaunch under a new `name`" in message


def test_a_token_session_drops_a_login_left_in_the_restored_config(tmp_path):
    """A checkpoint from a login deployment carries `.credentials.json`; a token session must not reuse it."""
    stale = _auth.credentials_path(tmp_path)
    stale.write_text('{"claudeAiOauth": {}}')
    _auth.drop_stale_login(tmp_path)
    assert not stale.exists()
    _auth.drop_stale_login(tmp_path)  # nothing left to drop is fine


def test_the_project_task_drops_the_stale_login_before_the_session_starts():
    assert "_auth.drop_stale_login(" in inspect.getsource(agent.project.func)
