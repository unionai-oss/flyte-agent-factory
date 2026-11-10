"""The suite never reads or writes the live run's state.

In a project pod the real home's `state/` is the run's own volume (meter history and refusals, checkpointed
metrics). conftest gives the session its own base; tests that reload the agent may move it to their tmp dir.
"""

import os
import pwd
from pathlib import Path

# `tests.conftest` would run it a second time and make a second base directory.
from conftest import TEST_BASE  # type: ignore[import-not-found]  # tests/ is not on mypy's path

from flyte_agent_factory import _session_config


def test_the_session_base_is_its_own():
    assert os.environ["AGENT_FACTORY_BASE"] == str(TEST_BASE)


def test_the_agent_never_points_at_the_live_runs_state():

    live = Path(pwd.getpwuid(os.getuid()).pw_dir) / "state"
    assert not _session_config.STATE_ROOT.resolve().is_relative_to(live.resolve())
    assert not _session_config.BASE_DIR.resolve() == live.resolve().parent


def test_the_bridge_never_points_at_the_live_supervisors():
    """Keep tests off the live bridge.

    The live supervisor's bridge is `$HOME/.claude-flyte/bridge`; a `Delegations` built there by a test
    marks every live delegation request "failed: supervisor restarted" (it did, 2026-10-30).
    """
    from flyte_agent_factory import _bridge, agent

    live = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude-flyte"
    assert not _bridge.BRIDGE_DIR.resolve().is_relative_to(live.resolve())
    # agent re-imports it without exporting it, so mypy cannot see the name; the value is what is checked
    assert not vars(agent)["BRIDGE_DIR"].resolve().is_relative_to(live.resolve())
