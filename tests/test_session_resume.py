"""A session that resumes a workspace written by an earlier version rewrites what the harness owns."""

import json

from flyte_agent_factory import _auth, _context_guard, _gates, _session_config, _session_setup
from flyte_agent_factory._session_setup import _write_managed_block

# A module the harness no longer names; the point is that stale content is rewritten, not which string it was.
STALE_MODULE = "stale_pkg_that_is_gone"


def test_resume_replaces_a_previous_marker_block_in_place_and_is_then_stable(tmp_path):
    memory = tmp_path / "CLAUDE.md"
    previous = f"{_gates.PREVIOUS_MEMORY_BEGIN}\nold harness text\n{_gates.PREVIOUS_MEMORY_END}"
    memory.write_text(f"# Project\n\nrules before\n\n{previous}\n\nrules after\n")

    _write_managed_block(memory, "new harness text")
    once = memory.read_text()
    _write_managed_block(memory, "new harness text")

    assert memory.read_text() == once
    assert once.count(_gates.MEMORY_BEGIN) == once.count(_gates.MEMORY_END) == 1
    assert _gates.PREVIOUS_MEMORY_BEGIN not in once and _gates.PREVIOUS_MEMORY_END not in once
    assert "old harness text" not in once and "new harness text" in once
    assert once.startswith("# Project\n\nrules before\n\n") and once.endswith("\n\nrules after\n")
    # In place: the block stays between the two project texts.
    assert once.index("rules before") < once.index(_gates.MEMORY_BEGIN) < once.index("rules after")


def test_a_file_with_both_marker_pairs_updates_the_current_block_only(tmp_path):
    memory = tmp_path / "CLAUDE.md"
    memory.write_text(
        f"{_gates.PREVIOUS_MEMORY_BEGIN}\nx\n{_gates.PREVIOUS_MEMORY_END}\n"
        f"{_gates.MEMORY_BEGIN}\ny\n{_gates.MEMORY_END}\n"
    )
    _write_managed_block(memory, "z")
    assert memory.read_text().count("z") == 1 and "\ny\n" not in memory.read_text()


def test_stale_wrapper_scripts_are_rewritten_on_start(tmp_path):
    for name in ("ask-human", "flyte-note", "end-project", "ci-status", "delegate", "credential-grant"):
        (tmp_path / name).write_text(f"#!/bin/sh\nexec python -m {STALE_MODULE}\n")
    _session_setup._install_bridge_commands(tmp_path, delegation_available=True, grants=True)
    for script in tmp_path.iterdir():
        assert STALE_MODULE not in script.read_text(), script.name
        assert "flyte_agent_factory." in script.read_text(), script.name


def test_a_stale_settings_hook_is_rewritten_on_start(tmp_path):
    stale = {
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": f"python -m {STALE_MODULE}"}]}]}
    }
    (tmp_path / "settings.json").write_text(json.dumps(stale))
    # The guard is off: no hook is installed, and the stale one does not survive.
    path = _auth.write_settings(tmp_path, extra=_context_guard.settings_extra({_context_guard.ENV: "0"}))
    assert STALE_MODULE not in path.read_text() and "hooks" not in json.loads(path.read_text())
    # The guard is on: the hook is the current one.
    path = _auth.write_settings(tmp_path, extra=_context_guard.settings_extra({}))
    assert STALE_MODULE not in path.read_text() and "flyte_agent_factory._context_guard" in path.read_text()


def test_a_stale_browser_entry_in_mcp_json_is_rewritten_on_start(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    stale = {"mcpServers": {"browser": {"command": "python", "args": ["-m", STALE_MODULE]}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(stale))
    path = _session_setup._write_mcp_config("http://shared-browser")
    assert STALE_MODULE not in path.read_text()
    assert json.loads(path.read_text())["mcpServers"]["browser"]["url"] == "http://shared-browser/mcp"
