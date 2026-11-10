import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _auth, _context_guard, _instructions, _session_config

BIG = 30 * 1024


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (_context_guard.ENV, _context_guard.BYTES_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def files(tmp_path):
    (tmp_path / "big.py").write_text("x = 1\n" * (BIG // 6 + 1))
    (tmp_path / "big2.py").write_text("y = 2\n" * (BIG // 6 + 1))
    (tmp_path / "small.py").write_text("x = 1\n")
    (tmp_path / "big.png").write_bytes(b"\0" * BIG)
    return tmp_path


def decide(tool, tool_input, cwd):
    return _context_guard.deny_reason({"tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)})


def test_read_without_range_of_a_large_file_is_denied(files):
    reason = decide("Read", {"file_path": str(files / "big.py")}, files)

    assert reason is not None
    assert f"{BIG // 6 * 6 + 6:,} bytes" in reason
    assert "re-sent on every turn" in reason
    assert "sed -n '1,200p'" in reason
    assert "grep -n" in reason
    assert "offset and limit" in reason


@pytest.mark.parametrize("extra", [{"offset": 1}, {"limit": 100}, {"offset": 10, "limit": 50}])
def test_read_with_a_range_is_allowed(files, extra):
    assert decide("Read", {"file_path": str(files / "big.py"), **extra}, files) is None


def test_small_missing_and_binary_files_are_allowed(files):
    assert decide("Read", {"file_path": str(files / "small.py")}, files) is None
    assert decide("Read", {"file_path": str(files / "nope.py")}, files) is None
    assert decide("Read", {"file_path": str(files / "big.png")}, files) is None
    assert decide("Bash", {"command": "cat big.png"}, files) is None


@pytest.mark.parametrize("command", ["cat big.py", "cat big.py small.py", "less big.py", "more big.py"])
def test_plain_whole_file_bash_reads_are_denied(files, command):
    assert decide("Bash", {"command": command}, files) is not None


def test_relative_paths_resolve_against_the_hook_cwd(files):
    assert decide("Bash", {"command": f"cat {shlex.quote(str(files / 'big.py'))}"}, Path("/")) is not None


@pytest.mark.parametrize(
    "command",
    [
        "cat big.py | head -n 40",
        "cat big.py | grep x",
        "cat big.py > /dev/null",
        "cat big.py; echo done",
        "cat big.py && echo done",
        "sed -n '1,50p' big.py",
        "head -n 50 big.py",
        "tail -n 50 big.py",
        "cat small.py",
        "cat $F",
        "cat 'unterminated",
        "ls big.py",
    ],
)
def test_bounded_or_doubtful_bash_commands_are_allowed(files, command):
    assert decide("Bash", {"command": command}, files) is None


def test_other_tools_and_odd_input_are_allowed(files):
    assert decide("Edit", {"file_path": str(files / "big.py")}, files) is None
    assert decide("Read", {}, files) is None
    assert decide("Bash", {"command": 5}, files) is None


def test_threshold_is_overridable(files, monkeypatch):
    monkeypatch.setenv(_context_guard.BYTES_ENV, str(BIG * 10))
    assert decide("Read", {"file_path": str(files / "big.py")}, files) is None
    monkeypatch.setenv(_context_guard.BYTES_ENV, "3")
    assert decide("Read", {"file_path": str(files / "small.py")}, files) is not None
    monkeypatch.setenv(_context_guard.BYTES_ENV, "garbage")
    assert decide("Read", {"file_path": str(files / "big.py")}, files) is not None


def run_hook(stdin: str):
    return subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._context_guard"],
        input=stdin,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(Path(_context_guard.__file__).parents[1]), "PATH": ""},
    )


def test_hook_process_denies_with_claude_code_json(files):
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(files / "big.py")}, "cwd": str(files)}
    done = run_hook(json.dumps(payload))

    assert done.returncode == 0
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "bytes" in out["permissionDecisionReason"]


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", '{"tool_name": "Read", "tool_input": 3}'])
def test_hook_fails_open_on_malformed_input(stdin):
    done = run_hook(stdin)

    assert done.returncode == 0
    assert done.stdout == ""


def test_hook_allows_a_ranged_read_silently(files):
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(files / "big.py"), "limit": 5}}
    done = run_hook(json.dumps({**payload, "cwd": str(files)}))

    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize(
    ("brief", "expected"),
    [
        ("Record a demo of the feature", True),
        ("make a VIDEO", True),
        ("a Walkthrough for the team", True),
        ("screencast please", True),
        ("recording of the run", True),
        ("Fix the parser bug", False),
        ("demonstrate the parser", False),
        ("the demolition crew", False),
        ("", False),
    ],
)
def test_wants_demo(brief, expected):
    assert _context_guard.wants_demo(brief) is expected


def memory(monkeypatch, brief="Fix the parser bug", **kwargs):
    monkeypatch.setattr(_session_config, "_project_brief", brief)
    return _instructions._project_memory(
        True, console_token=Path("/tmp/console-token"), delegation_available=True, name="proj", **kwargs
    )


def test_claude_md_is_unchanged_when_the_guard_is_off(monkeypatch):
    outputs = []
    for value in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv(_context_guard.ENV, value)
        outputs.append(memory(monkeypatch))
    off = outputs[0]

    assert all(output == off for output in outputs)
    # The sections the guard removes are what the guard-off output has always had.
    assert "Recording a demo of this work" in off
    assert "Context hygiene" not in off
    assert off.count("Showing the Union console") == 2


@pytest.mark.parametrize("value", [None, "", "1", "true", "yes", "on", "garbage"])
def test_the_guard_is_on_by_default_and_for_anything_but_an_off_value(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv(_context_guard.ENV, value)
    assert _context_guard.enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "False", " off "])
def test_the_switch_turns_the_guard_off(monkeypatch, value):
    monkeypatch.setenv(_context_guard.ENV, value)
    assert not _context_guard.enabled()
    assert _context_guard.settings_extra() is None
    assert _context_guard.session_env() == {}


def test_an_unset_switch_gives_the_guarded_claude_md(monkeypatch):
    unset = memory(monkeypatch)
    monkeypatch.setenv(_context_guard.ENV, "1")

    assert unset == memory(monkeypatch)
    assert "Context hygiene" in unset


def test_claude_md_without_a_demo_brief_drops_the_demo_sections(monkeypatch):
    monkeypatch.setenv(_context_guard.ENV, "0")
    off = memory(monkeypatch)
    monkeypatch.setenv(_context_guard.ENV, "1")
    on = memory(monkeypatch)

    assert len(on) < len(off) - 10_000
    assert "Recording a demo of this work" not in on
    # The console sign-in is still needed without a demo: it stays, once.
    assert on.count("Showing the Union console") == 1
    assert "Context hygiene" in on
    hygiene = on.split("## Context hygiene")[1].split("\n## ")[0].strip().splitlines()
    assert len(hygiene) <= 8
    assert "browser_start_video" in on  # the browser's own tools are still described


def test_claude_md_with_a_demo_brief_keeps_the_recording_section_with_one_console_block(monkeypatch):
    monkeypatch.setenv(_context_guard.ENV, "1")
    on = memory(monkeypatch, brief="Build it and record a demo")

    assert "Recording a demo of this work" in on
    assert on.count("Showing the Union console") == 1
    assert "Context hygiene" in on


def test_claude_md_keeps_the_recording_section_for_a_demo_kind_launch_whatever_the_brief_says(monkeypatch):
    monkeypatch.setenv(_context_guard.ENV, "1")
    monkeypatch.setattr(_session_config, "_demo_kind", "pattern")
    on = memory(monkeypatch, brief="Build a narrated tour of the run-report breakdown for prospects.")
    assert "Recording a demo of this work" in on
    assert on.count("Showing the Union console") == 1
    assert "Context hygiene" in on


def test_settings_carry_the_hook_only_when_the_guard_is_on(tmp_path, monkeypatch):
    unset = json.loads(
        _auth.write_settings(
            tmp_path / "d", hold_dialogs=False, extra=_context_guard.settings_extra()
        ).read_text()
    )
    assert unset["hooks"]["PreToolUse"][0]["matcher"] == "Read|Bash"

    monkeypatch.setenv(_context_guard.ENV, "0")
    plain = _auth.write_settings(tmp_path / "a", hold_dialogs=False).read_bytes()
    off = _auth.write_settings(
        tmp_path / "b", hold_dialogs=False, extra=_context_guard.settings_extra()
    ).read_bytes()
    assert off == plain
    assert "hooks" not in json.loads(plain)

    monkeypatch.setenv(_context_guard.ENV, "true")
    on = json.loads(
        _auth.write_settings(
            tmp_path / "c", hold_dialogs=False, extra=_context_guard.settings_extra()
        ).read_text()
    )
    (entry,) = on["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Read|Bash"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"] == f"{shlex.quote(sys.executable)} -m flyte_agent_factory._context_guard"
    assert on["pluginConfigs"] and on["permissions"]


def test_auto_compaction_env_only_when_the_guard_is_on(monkeypatch):
    assert _context_guard.session_env() == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}
    monkeypatch.setenv(_context_guard.ENV, "0")
    assert _context_guard.session_env() == {}
    monkeypatch.setenv(_context_guard.ENV, "1")
    assert _context_guard.session_env() == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}


def test_the_hygiene_note_names_delegate_answer_only_where_delegation_exists(monkeypatch):
    without = _instructions._project_memory(False)
    with_delegation = _instructions._project_memory(False, delegation_available=True)

    assert "Context hygiene" in without and "Context hygiene" in with_delegation
    assert "delegate --answer" not in without
    assert "to a sub-agent and read its conclusion" in without
    assert "to `delegate --answer` or a sub-agent and read its conclusion" in with_delegation
