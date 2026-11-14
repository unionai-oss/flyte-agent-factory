"""Factory gates use local tools and never spend an LLM review round."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _gate_tests,
    _gates,
    _review,
    _reviewer,
    _session_config,
    _session_setup,
    _standards,
    _supervise,
    _timeline,
    _workspace,
    agent,
)
from flyte_agent_factory._bridge import Bridge
from flyte_agent_factory._process import ClaudeProcess
from tests import _standards as template
from tests._git import git
from tests._wait import wait_for

pytestmark = pytest.mark.real_factory_gates

#: A standards set that maps just the fail-to-pass check, which the gate runs only when a rule maps it.
TESTED = {"engineering.md": template.rule_text(template.FAIL_TO_PASS)}
FAIL_TO_PASS_ROW = f"factory:fail-to-pass: [{template.FAIL_TO_PASS}] "


def test_fail_to_pass_uses_head_tests_with_base_code(project):
    workspace, base = project
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    (workspace / "app/test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    changes = ["app/main.py", "app/test_main.py"]
    code, result = _gate_tests.fail_to_pass(workspace, "app", base, changes, "change answer", "", "DONE")
    assert code == 0 and "head tests pass; base tests fail" in result
    assert (workspace / "app/main.py").read_text().endswith("return 2\n")


def test_gate_rejects_tests_that_pass_on_base(project):
    workspace, base = project
    code, result = _gate_tests.fail_to_pass(workspace, "app", base, ["app/test_main.py"], "", "", "DONE")
    assert code == 1 and "observed exit 0" in result


def test_head_failure_blocks(project):
    workspace, base = project
    (workspace / "app/test_main.py").write_text("def test_answer():\n    assert False\n")
    code, result = _gate_tests.fail_to_pass(workspace, "app", base, ["app/test_main.py"], "", "", "DONE")
    assert code == 1 and "head tests fail" in result


@pytest.mark.parametrize("note,demo", [("DONE: refactor: no behaviour change", ""), ("DONE", "pattern")])
def test_exemptions_are_recorded(project, note, demo):
    workspace, _ = project
    git(workspace, "add", "app")
    timeline = _timeline._Timeline()
    assert _gates.run(workspace, "app", "", demo, note, TESTED, timeline) == ""
    result = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    gate = next(r for r in result if r["rule"] == "factory:fail-to-pass")
    assert "exempt" in gate["output"].lower()


@pytest.mark.parametrize("action", ["delete", "skip", "remove"])
def test_removed_tests_block_even_exemptions(project, action):
    workspace, base = project
    test = workspace / "app/test_main.py"
    if action == "delete":
        test.unlink()
    elif action == "skip":
        test.write_text(
            "import pytest\n"
            + test.read_text().replace("def test_answer", "@pytest.mark.skip\ndef test_answer")
        )
    else:
        test.write_text("")
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_main.py"], "", "pattern", "refactor: no behaviour change"
    )
    assert code == 1 and "without brief authorization" in result


def test_authorized_test_removal(project):
    workspace, base = project
    (workspace / "app/test_main.py").unlink()
    code, _ = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_main.py"], "Delete tests: app/test_main.py", "pattern", "DONE"
    )
    assert code == 0


def test_scope_blocks_root_other_projects_and_rename_source():
    assert _gate_scope.scope_violations(
        ["app/main.py", "other/main.py", "README.md"], "app", "update app"
    ) == [
        "other/main.py",
        "README.md",
    ]
    assert (
        _gate_scope.scope_violations(
            ["app/main.py", "shared/config.toml"], "app", "May edit: shared/config.toml"
        )
        == []
    )


def test_scope_checks_committed_deletions(project):
    workspace, _ = project
    (workspace / "other").mkdir()
    (workspace / "other/main.py").write_text("x = 1\n")
    git(workspace, "add", "other")
    git(workspace, "commit", "-m", "outside change")
    (workspace / ".claude-flyte/app/base-commit").write_text(git(workspace, "rev-parse", "HEAD"))
    (workspace / "other/main.py").unlink()
    git(workspace, "add", "other")
    git(workspace, "commit", "-m", "delete other project")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert "outside project/brief: other/main.py" in result


def test_tool_must_failure_and_unavailable_are_visible(project):
    workspace, _ = project
    rules = {
        "checks.md": (
            f"## TEST-001 MUST\nCheck: fail\nEnforced: tool {sys.executable} -c 'print(123); raise "
            f"SystemExit(1)'\n\n## TEST-002 MUST\nCheck: absent\nEnforced: tool "
            f"definitely-not-a-factory-binary\n"
        )
    }
    result = _gates.run(workspace, "app", "", "pattern", "DONE", rules, _timeline._Timeline())
    assert "123" in result and "TEST-001" in result
    assert "TEST-002: image defect:" in result
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    assert any(r["rule"] == "TEST-002" and r["status"] == "tool unavailable" for r in results)


@pytest.mark.parametrize(
    "evidence",
    [
        "command: python app.py | output: ValueError: empty input",
        "test: test_bad_input | output: AssertionError: expected 2 got 1",
        "missing: app/report.csv",
        'clause: "export a CSV" | deliverable: no CSV exporter',
        "reproduction: input 2 -> output 1",
    ],
)
def test_evidence_keeps_findings_blocking(evidence):
    blocking, advisory = _review._evidenced_findings(f"FINDING: bug\nEvidence: {evidence}\nVERDICT: FAIL")
    assert len(blocking) == 1 and advisory == []


def test_unevidenced_findings_are_downgraded_individually():
    blocking, advisory = _review._evidenced_findings(
        "FINDING: bug\nEvidence: missing: app/output\nFINDING: rename this\nVERDICT: FAIL"
    )
    assert len(blocking) == len(advisory) == 1
    assert "rename this" in advisory[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("compatibility", [False, True])
async def test_craft_fail_is_advisory_unless_policy_restores_it(monkeypatch, compatibility):
    monkeypatch.setenv("AGENT_FACTORY_CRAFT_BLOCKING", str(compatibility).lower())

    async def reviewer(**kwargs):
        return "bad name\nBLOCKING: 3\nVERDICT: FAIL", None

    monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)
    advisory: list[str] = []
    blocking: list[int] = []
    _, passed, _ = await _review._one_judge(
        "craft",
        "inspect",
        cast("Dir[Any]", object()),  # the reviewer is stubbed; the snapshot is never read
        "brief",
        "",
        1,
        _timeline._Timeline(),
        nonblocking=advisory,
        blocking=blocking,
    )
    assert passed is not compatibility
    assert blocking == ([3] if compatibility else [0])
    assert bool(advisory) is not compatibility


@pytest.mark.parametrize("demo", ["", "pattern", "showcase"])
def test_demo_selection_and_flyte_detection(tmp_path, demo):
    (tmp_path / "main.py").write_text("from flyte import task\n")
    config = _standards.Standards(dir=str(template.TEMPLATE))
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path, demo=demo)
    assert "platforms/flyte.md" not in selected
    text = _standards.review_text(selected, budget=100000)
    assert ("demo.md" in selected) is bool(demo)
    if demo == "pattern":
        assert f"{template.FAIL_TO_PASS} MUST" not in text
        assert f"{template.DEPLOYMENT_CONFIG} MUST" not in text
        assert f"{template.DEMO_TEST} MUST" in text
    else:
        assert f"{template.FAIL_TO_PASS} MUST" in text and f"{template.DEPLOYMENT_CONFIG} MUST" in text
        assert (f"{template.SHOWCASE_RULE} MUST" in text) is bool(demo)
        assert f"{template.DEMO_TEST} MUST" not in text


@pytest.mark.asyncio
async def test_tool_gate_bounces_done_without_review_round(project, monkeypatch):
    from tests.test_nudge import _Proc, _Queue

    workspace, _ = project
    monkeypatch.setattr(_session_config, "_project_name", "app")
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.001)
    monkeypatch.setattr(_session_config, "_project_brief", "change app")
    monkeypatch.setattr(_session_config, "_demo_kind", "pattern")
    monkeypatch.setattr(
        _session_config,
        "_applicable_standards",
        lambda: {"test.md": f"## TEST-001 MUST\nEnforced: tool {sys.executable} -c 'raise SystemExit(1)'\n"},
    )
    (workspace / "app/checks.sh").write_text("exit 0\n")
    criteria = workspace / _session_config._acceptance_rel("app")
    criteria.write_text("criteria\n")
    git(workspace, "add", "app")
    git(workspace, "commit", "-m", "checks")
    reviewed = []

    async def flush(*args, **kwargs):
        return None

    monkeypatch.setattr(_timeline._Timeline, "flush", flush)

    async def review(*args):
        reviewed.append(args)
        return True, "", None, ""

    monkeypatch.setattr(_review, "_review_round", review)
    import asyncio

    bridge, proc, timeline, stop = _Queue(), _Proc(), _timeline._Timeline(), asyncio.Event()
    bridge.push("DONE: delivered")
    task = asyncio.create_task(
        _supervise._pump(
            cast("Bridge", bridge),  # the nudge fakes stand in for the real bridge and process
            cast("ClaudeProcess", proc),
            timeline,
            stop,
            workspace=workspace,
            brief="change app",
        )
    )
    try:
        await wait_for(lambda: any("TEST-001" in m for m in proc.submitted))
        assert any("TEST-001" in m for m in proc.submitted)
        assert reviewed == []
        assert any("no review round was used" in m for m in proc.submitted)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=30)


@pytest.mark.parametrize(
    "brief,package,kind",
    [
        ("Build a production service, not a demo", "", ""),
        ("Build a service; no demo required", "", ""),
        ("Build a demo", "", ""),
        ("# Gateway demo", "", ""),
        ("Do not build a demo.", "", ""),
        ("## Demos in the wild\nBuild it", "", ""),
        ("Build it", "gateway", "pattern"),
        ("Demo kind: showcase\nBuild it", "", "showcase"),
        ("Intro\ndemo kind: Pattern\nBuild it", "", "pattern"),
        ("Demo kind: showcase (full product)", "", "showcase"),
        ("Demo kind: showcase\nBuild it", "gateway", "showcase"),
        ("Not a demo kind: showcase", "", ""),
    ],
)
def test_demo_launch_selection(brief, package, kind):
    assert _standards.demo_kind(brief, package) == kind


def test_non_python_gate_reuses_project_checks(project):
    workspace, base = project
    (workspace / "app/value.txt").write_text("2\n")
    (workspace / "app/test_value.sh").write_text('test "$(cat app/value.txt)" = 2\n')
    (workspace / "app/checks.sh").write_text("bash app/test_value.sh\n")
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_value.sh", "app/value.txt"], "", "", "DONE"
    )
    assert code == 0 and "base tests fail" in result


def test_test_names_include_class_context():
    before = "class TestA:\n    def test_same(self): pass\nclass TestB:\n    def test_same(self): pass\n"
    after = "class TestA:\n    def test_same(self): pass\n"
    assert _gate_tests._test_names(before) - _gate_tests._test_names(after) == {"TestB.test_same"}


@pytest.mark.asyncio
async def test_convergence_excludes_advisory_craft_and_unevidenced_findings(monkeypatch, tmp_path):
    async def reviewer(*, brief, **kwargs):
        if "**correctness** reviewer" in brief:
            return (
                "FINDING: missing result\nEvidence: missing: app/result.csv\nBLOCKING: 9\nVERDICT: FAIL",
                None,
            )
        if "**completeness** reviewer" in brief:
            return "FINDING: I prefer a different design\nBLOCKING: 7\nVERDICT: FAIL", None
        return "naming could improve\nBLOCKING: 6\nVERDICT: FAIL", None

    async def snapshot(*args):
        return object()

    async def facts():
        return "live state"

    monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)
    monkeypatch.setattr(Dir, "from_local", snapshot)
    monkeypatch.setattr(_review, "_review_facts", facts)
    passed, findings = await _review._evaluate(tmp_path, "brief", "", 1, _timeline._Timeline())
    assert not passed and _session_config._round_scores == [1]
    assert "Advisory notes" in findings and "craft advisory" in findings
    assert "no evidence" in findings


def test_demo_replacements_come_from_content(tmp_path):
    config = _standards.Standards(dir=str(template.TEMPLATE))
    files = _standards.capture(config, tmp_path)
    files["demo.md"] = files["demo.md"].replace(
        f"{template.FAIL_TO_PASS} (production test gate)", f"{template.PROMOTION} (scratch work)"
    )
    selected = _standards.select(files, config, tmp_path, demo="pattern")
    text = _standards.review_text(selected, budget=100000, must_only=True)
    assert f"{template.PROMOTION} MUST" not in text and f"{template.FAIL_TO_PASS} MUST" in text


def test_project_instructions_have_one_supported_path():
    assert not hasattr(agent, "_project_instruction_imports")


@pytest.mark.parametrize("configured", [False, True])
def test_tool_gate_uses_project_ruff_config_or_baseline(project, monkeypatch, configured):
    workspace, _ = project
    commands = []
    if configured:
        (workspace / "app/pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")

    def run(args, cwd, **kwargs):
        commands.append(args)
        return 0, ""

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {"python.md": "## LINT-099 MUST\nEnforced: tool ruff check .\n"}
    assert (
        _gates.run(workspace, "app", "", "", "refactor: no behaviour change", rules, _timeline._Timeline())
        == ""
    )
    command = next(c for c in commands if c[0] == "ruff")
    assert ("--config" in command) is not configured
    if not configured:
        assert command[-1] == str(workspace / ".claude-flyte/standards/languages/configs/ruff.toml")


# --- review-gate round 2 ---------------------------------------------------------------------


def _statuses(workspace, name="app"):
    results = json.loads((workspace / f".claude-flyte/{name}/standards-results.json").read_text())
    return {r["rule"]: r for r in results}


@pytest.fixture
def remote_workspace(tmp_path):
    """A clone of a shared origin whose other projects keep committing to main."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    for repo in (seed,):
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "test")
    (seed / "app").mkdir()
    (seed / "app/main.py").write_text("def answer():\n    return 1\n")
    (seed / "README.md").write_text("shared\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "base")
    git(seed, "push", "origin", "HEAD:main")
    workspace = tmp_path / "workspace"
    git(tmp_path, "clone", str(origin), str(workspace))
    git(workspace, "config", "user.email", "app@agent-factory.localhost")
    git(workspace, "config", "user.name", "test")
    return seed, workspace


def _upstream_touches_other_projects(seed):
    (seed / "other").mkdir()
    (seed / "other/x.py").write_text("x = 1\n")
    (seed / "README.md").write_text("shared, edited upstream\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "someone else's work")
    git(seed, "push", "origin", "HEAD:main")


def test_scope_ignores_upstream_commits_pulled_in_by_the_mandated_rebase(remote_workspace):
    seed, workspace = remote_workspace
    _gates.capture_base(workspace, "app")
    (workspace / "app/feature.py").write_text("x = 1\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "feature")
    _upstream_touches_other_projects(seed)
    git(workspace, "fetch")
    git(workspace, "rebase", "origin/main")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result == ""
    assert _statuses(workspace)["scope"]["status"] == "PASS"


def test_resumed_project_without_a_stored_base_uses_history_root_not_head(remote_workspace):
    _, workspace = remote_workspace
    (workspace / "other").mkdir()
    (workspace / ".claude-flyte/other").mkdir(parents=True)
    (workspace / "other/mine.py").write_text("x = 1\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "edits another project")
    assert not (workspace / ".claude-flyte/app/base-commit").exists()
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert "outside project/brief: other/mine.py" in result


def test_scope_blocks_other_projects_and_shared_files_but_only_flags_stray_files():
    changes = ["app/main.py", "other/main.py", "README.md", "CLAUDE.md", "notes.txt", "pyproject.toml"]
    assert _gate_scope.scope_violations(changes, "app", "update app") == [
        "other/main.py",
        "README.md",
        "CLAUDE.md",
        "pyproject.toml",
    ]
    assert _gate_scope.scope_flagged(changes, "app", "update app") == ["notes.txt"]
    assert _gate_scope.scope_violations(["README.md"], "app", "May edit: README.md") == []


def test_stray_root_file_is_recorded_but_does_not_block(project):
    workspace, _ = project
    (workspace / "notes.txt").write_text("scratch\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "stray note")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result == ""
    scope = _statuses(workspace)["scope"]
    assert scope["status"] == "PASS" and "notes.txt" in scope["output"]


def test_no_git_makes_the_scope_and_test_gates_not_applicable(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/main.py").write_text("x = 1\n")
    result = _gates.run(tmp_path, "app", "", "", "DONE", TESTED, _timeline._Timeline())
    assert result == ""
    statuses = _statuses(tmp_path)
    assert statuses["scope"]["status"] == "not applicable"
    assert "no git" in statuses["scope"]["output"]
    assert statuses["factory:fail-to-pass"]["status"] == "not applicable"


def test_project_without_a_test_framework_is_not_forced_to_declare_a_refactor(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra/main.tf").write_text('resource "x" "y" {}\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    _gates.capture_base(tmp_path, "infra")
    git(tmp_path, "config", "user.email", "infra@agent-factory.localhost")
    (tmp_path / "infra/vpc.tf").write_text('resource "x" "z" {}\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "vpc")
    result = _gates.run(tmp_path, "infra", "", "", "DONE", TESTED, _timeline._Timeline())
    assert result == ""
    gate = _statuses(tmp_path, "infra")["factory:fail-to-pass"]
    assert gate["status"] == "not applicable"
    assert "no Python source changes" in gate["output"]


def test_python_source_change_without_tests_still_needs_a_test(project):
    workspace, base = project
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    code, result = _gate_tests.fail_to_pass(workspace, "app", base, ["app/main.py"], "", "", "DONE")
    assert code == 1 and "no tests for new behaviour" in result


def test_capture_base_is_not_refreshed_by_a_later_start(project):
    workspace, base = project
    (workspace / "app/extra.py").write_text("x = 1\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "later")
    _gates.capture_base(workspace, "app")
    assert (workspace / ".claude-flyte/app/base-commit").read_text() == base


def test_never_skip_or_weaken_tests_is_not_permission_to_remove_them(project):
    workspace, base = project
    (workspace / "app/test_main.py").unlink()
    brief = "Never skip or weaken tests.\nRemove the dead code and delete the old tests of nothing."
    code, result = _gate_tests.fail_to_pass(workspace, "app", base, ["app/test_main.py"], brief, "", "DONE")
    assert code == 1 and "without brief authorization" in result


@pytest.mark.parametrize("brief", ["Tests may be removed: the legacy ones", "Delete tests: app/test_main.py"])
def test_explicit_brief_line_permits_test_removal(project, brief):
    workspace, base = project
    (workspace / "app/test_main.py").unlink()
    code, _ = _gate_tests.fail_to_pass(workspace, "app", base, ["app/test_main.py"], brief, "pattern", "DONE")
    assert code == 0


def test_the_word_skip_in_a_comment_is_not_a_skipped_test(project):
    workspace, base = project
    test = workspace / "app/test_main.py"
    test.write_text("# we never skip or xfail anything here\n" + test.read_text())
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_main.py"], "", "pattern", "DONE"
    )
    assert code == 0, result


@pytest.mark.parametrize(
    "marker",
    [
        '@pytest.mark.skipif(True, reason="later")\n',
        "@pytest.mark.xfail\n",
        "@unittest.skip('later')\n",
    ],
)
def test_added_skip_decorators_are_detected_structurally(project, marker):
    workspace, base = project
    test = workspace / "app/test_main.py"
    test.write_text(
        "import pytest, unittest\n" + test.read_text().replace("def test_answer", marker + "def test_answer")
    )
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_main.py"], "", "pattern", "DONE"
    )
    assert code == 1 and "without brief authorization" in result


def test_pytest_skip_call_is_detected_structurally(project):
    workspace, base = project
    test = workspace / "app/test_main.py"
    test.write_text(
        "import pytest\n" + test.read_text().replace("    assert", '    pytest.skip("later")\n    assert')
    )
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/test_main.py"], "", "pattern", "DONE"
    )
    assert code == 1 and "without brief authorization" in result


def test_subprocess_output_that_is_not_utf8_is_decoded_leniently(tmp_path):
    code, output = _gate_base._run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe tail')"], tmp_path
    )
    assert code == 0 and output.endswith(" tail")


def test_mypy_baseline_applies_to_the_project_venv_binary(project, monkeypatch):
    workspace, _ = project
    binary = workspace / "app/.venv/bin/mypy"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    commands = []

    def run(args, cwd, **kwargs):
        commands.append(args)
        return 0, ""

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {"python.md": "## LINT-099 MUST\nEnforced: tool mypy .\n"}
    _gates.run(workspace, "app", "", "", "refactor: no behaviour change", rules, _timeline._Timeline())
    command = next(c for c in commands if Path(c[0]).name == "mypy")
    assert command[0] == str(binary)
    assert command[-2:] == [
        "--config-file",
        str(workspace / ".claude-flyte/standards/languages/configs/mypy.ini"),
    ]


def test_eng_003_is_craft_guidance_not_a_gate(project):
    workspace, _ = project
    (workspace / "notes-03").mkdir()
    rules = {"engineering.md": template.rule_text(template.PROMOTION)}
    assert _gates.run(workspace, "app", "", "pattern", "DONE", rules, _timeline._Timeline()) == ""
    assert template.PROMOTION not in _statuses(workspace)


def test_eng_012_is_not_applicable_without_a_ui(project):
    workspace, _ = project
    rules = {"engineering.md": template.rule_text(template.UI_EVIDENCE)}
    assert _gates.run(workspace, "app", "", "pattern", "DONE", rules, _timeline._Timeline()) == ""
    assert _statuses(workspace)["factory:ui-evidence"]["status"] == "not applicable"
    (workspace / "app/index.html").write_text("<html></html>")
    _gates.run(workspace, "app", "", "pattern", "DONE", rules, _timeline._Timeline())
    gate = _statuses(workspace)["factory:ui-evidence"]
    assert gate["status"] == "tool unavailable"
    assert gate["output"].startswith(f"[{template.UI_EVIDENCE}] ")


def test_unavailable_tool_is_not_announced_twice(project):
    workspace, _ = project
    timeline = _timeline._Timeline()
    rules = {"checks.md": "## TEST-002 MUST\nCheck: absent\nEnforced: tool definitely-not-a-factory-binary\n"}
    _gates.run(workspace, "app", "", "pattern", "DONE", rules, timeline)
    rows = [text for _, _, text in timeline._rows if text.startswith("TEST-002")]
    assert rows and all("tool unavailable: tool unavailable" not in r for r in rows)
    assert not _statuses(workspace)["TEST-002"]["output"].startswith("tool unavailable")


def test_ruff_baseline_exempts_tests_from_docstrings(tmp_path):
    configs = _workspace.TEMPLATE / "standards/languages/configs"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/lib.py").write_text('"""Module."""\n\n\ndef public():\n    """Do it."""\n')
    (tmp_path / "tests/test_lib.py").write_text("def test_public():\n    assert True\n")
    command = [sys.executable, "-m", "ruff", "check", "--select", "D", "--config", str(configs / "ruff.toml")]
    code, output = _gate_base._run([*command, "."], tmp_path)
    assert code == 0, output


QA_REPRO = (
    "FINDING: minor: README typo\n"
    "Evidence: missing: app/CHANGELOG.md\n"
    "Everything else checked out.\n"
    "BLOCKING: 0\n"
    "VERDICT: PASS"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", ["correctness", "completeness"])
async def test_an_evidenced_minor_finding_never_flips_a_pass(monkeypatch, judge):
    async def reviewer(**kwargs):
        return QA_REPRO, None

    monkeypatch.setattr(_reviewer, "_call_reviewer", reviewer)
    advisory: list[str] = []
    blocking: list[int] = []
    _, passed, _ = await _review._one_judge(
        judge,
        "inspect",
        cast("Dir[Any]", object()),  # the reviewer is stubbed; the snapshot is never read
        "brief",
        "",
        1,
        _timeline._Timeline(),
        nonblocking=advisory,
        blocking=blocking,
    )
    assert passed is True
    assert blocking == [0]
    assert any("README typo" in note for note in advisory)


def test_only_findings_the_reviewer_marked_blocking_can_block():
    blocking, advisory = _review._evidenced_findings(QA_REPRO)
    assert blocking == [] and len(advisory) == 1
    blocking, advisory = _review._evidenced_findings(
        "FINDING: minor: typo\nEvidence: missing: app/CHANGELOG.md\n"
        "FINDING: crash on empty input\nEvidence: command: app --empty | output: Traceback\n"
        "BLOCKING: 1\nVERDICT: FAIL"
    )
    assert len(blocking) == 1 and "crash" in blocking[0]
    assert len(advisory) == 1 and "typo" in advisory[0]


# --- review-gate round 3 ---


def test_project_changes_survive_push_and_exclude_upstream(remote_workspace):
    seed, workspace = remote_workspace
    (seed / "other").mkdir()
    (seed / "other/a.txt").write_text("upstream\n")
    (seed / "app/test_main.py").write_text("def test_old():\n    assert True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed test")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "pull")
    _gates.capture_base(workspace, "app")
    git(workspace, "config", "user.email", "app@agent-factory.localhost")
    (workspace / "other/a.txt").write_text("project\n")
    (workspace / "app/test_main.py").unlink()
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "project changes")
    (seed / "other/upstream.txt").write_text("human\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "upstream")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "fetch")
    git(workspace, "rebase", "origin/main")
    git(workspace, "push", "origin", "HEAD:main")
    result = _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline())
    assert "outside project/brief: other/a.txt" in result
    assert "upstream.txt" not in result
    assert "tests removed, deleted or skipped" in result


def test_fail_to_pass_after_push_keeps_upstream_code(remote_workspace):
    seed, workspace = remote_workspace
    _gates.capture_base(workspace, "app")
    git(workspace, "config", "user.email", "app@agent-factory.localhost")
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    (workspace / "app/test_main.py").write_text(
        "from main import answer\nfrom upstream import value\n\ndef test_answer():\n    assert answer() == "
        "value\n"
    )
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "feature")
    (seed / "app/upstream.py").write_text("value = 2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "upstream dependency")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "fetch")
    git(workspace, "rebase", "origin/main")
    git(workspace, "push", "origin", "HEAD:main")
    assert _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline()) == ""
    assert "head tests pass; base tests fail" in _statuses(workspace)["factory:fail-to-pass"]["output"]


def test_mentions_do_not_allow_scope_and_new_directories_are_advisory():
    assert _gate_scope.scope_violations(
        ["README.md", "other/a.py"], "app", "See README.md and other/a.py"
    ) == [
        "README.md",
        "other/a.py",
    ]
    assert (
        _gate_scope.scope_violations(["README.md", "other/a.py"], "app", "May edit: README.md, other/a.py")
        == []
    )
    assert _gate_scope.scope_flagged(
        ["docs/guide.md", ".github/workflows/check.yml"], "app", "", {"other"}
    ) == [
        "docs/guide.md",
        ".github/workflows/check.yml",
    ]


def test_new_python_without_tests_blocks(remote_workspace):
    _, workspace = remote_workspace
    base = git(workspace, "rev-parse", "HEAD")
    code, output = _gate_tests.fail_to_pass(workspace, "app", base, ["app/main.py"], "", "", "DONE")
    assert code == 1 and "no tests for new behaviour" in output


@pytest.mark.parametrize(
    "label", ["Advisory lock leaks", "minor bug", "nit picker fails", "non-blocking operation hangs"]
)
def test_severity_requires_a_label_colon_and_accepts_indented_evidence(label):
    blocking, advisory = _review._evidenced_findings(
        f"FINDING: {label}\n    Evidence: test: test_lock | output: failed\nVERDICT: FAIL"
    )
    assert len(blocking) == 1 and advisory == []


@pytest.mark.parametrize("label", ["minor:", "nit:", "advisory:", "non-blocking:"])
def test_explicit_severity_is_advisory(label):
    blocking, advisory = _review._evidenced_findings(
        f"FINDING: {label} wording\n    Evidence: missing: app/file\nVERDICT: FAIL"
    )
    assert blocking == [] and len(advisory) == 1


def test_not_applicable_activity_has_one_prefix(tmp_path):
    timeline = _timeline._Timeline()
    _gates.run(tmp_path, "app", "", "", "DONE", TESTED, timeline)
    rows = [row for _, _, row in timeline._rows]
    assert "scope: not applicable: no git" in rows
    assert f"factory:fail-to-pass: not applicable: [{template.FAIL_TO_PASS}] no git" in rows


def test_unowned_new_top_level_directory_is_flagged(project):
    workspace, _ = project
    (workspace / "guides").mkdir()
    (workspace / "guides/start.txt").write_text("guide\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "new guide")
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""
    assert "flagged, not blocking: guides/start.txt" in _statuses(workspace)["scope"]["output"]


def test_upstream_test_removal_is_not_attributed_to_project(remote_workspace):
    seed, workspace = remote_workspace
    test = seed / "app/test_main.py"
    test.write_text("def test_old():\n    assert True\n\ndef test_kept():\n    assert True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "initial tests")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "pull")
    _gates.capture_base(workspace, "app")
    test.write_text("def test_kept():\n    assert True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "upstream removes old test")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "pull")
    (workspace / "app/test_main.py").write_text(test.read_text() + "\ndef test_new():\n    assert True\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "project adds a test")
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""


def test_directory_created_by_upstream_is_owned(remote_workspace):
    seed, workspace = remote_workspace
    _gates.capture_base(workspace, "app")
    _upstream_touches_other_projects(seed)
    git(workspace, "pull")
    (workspace / "other/x.py").write_text("x = 2\n")
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "edits upstream project")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert "outside project/brief: other/x.py" in result


def test_explicit_scope_permission_accepts_any_file_extension_and_bare_directory():
    paths = ["root.py", "root.txt", "external/main.py"]
    assert _gate_scope.scope_violations(paths, "app", "May edit: root.py, root.txt, external") == []
    assert _gate_scope.scope_flagged(paths, "app", "May edit: root.py, root.txt, external") == []


# --- review-gate round 4 -----------------------------------------------------------------------


def _commit(workspace, message):
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", message)


def _push_after_upstream_moves(seed, workspace):
    """The mandated rebase and push, with someone else's commit landing in between."""
    (seed / "upstream.txt").write_text(f"{len(list(seed.iterdir()))}\n")
    _commit(seed, "upstream moves")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "fetch")
    git(workspace, "rebase", "origin/main")
    git(workspace, "push", "origin", "HEAD:main")


@pytest.fixture
def pushed_project(remote_workspace):
    """A cloned project whose base already holds a passing test, so removals can be judged."""
    seed, workspace = remote_workspace
    (seed / "app/test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 1\n"
    )
    _commit(seed, "seed test")
    git(seed, "push", "origin", "HEAD:main")
    git(workspace, "pull")
    _gates.capture_base(workspace, "app")
    return seed, workspace


def test_skip_added_then_removed_is_judged_by_the_net_change(pushed_project):
    seed, workspace = pushed_project
    test = workspace / "app/test_main.py"
    original = test.read_text()
    test.write_text(
        "import pytest\n" + original.replace("def test_answer", "@pytest.mark.skip\ndef test_answer")
    )
    _commit(workspace, "skip while debugging")
    test.write_text(original)
    _commit(workspace, "unskip")
    _push_after_upstream_moves(seed, workspace)
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""


def test_new_test_file_added_then_deleted_is_judged_by_the_net_change(pushed_project):
    seed, workspace = pushed_project
    extra = workspace / "app/test_extra.py"
    extra.write_text("def test_extra():\n    assert True\n")
    _commit(workspace, "add scratch test")
    extra.unlink()
    _commit(workspace, "drop scratch test")
    _push_after_upstream_moves(seed, workspace)
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""


def test_net_removal_of_a_preexisting_test_still_blocks_after_push(pushed_project):
    seed, workspace = pushed_project
    (workspace / "app/test_main.py").unlink()
    _commit(workspace, "remove test")
    (workspace / "app/other.py").write_text("x = 1\n")
    _commit(workspace, "unrelated")
    _push_after_upstream_moves(seed, workspace)
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result == (
        f"{FAIL_TO_PASS_ROW}tests removed, deleted or skipped without brief authorization: app/test_main.py"
    )


def test_net_skip_of_a_preexisting_test_still_blocks_after_push(pushed_project):
    seed, workspace = pushed_project
    test = workspace / "app/test_main.py"
    test.write_text(
        "import pytest\n" + test.read_text().replace("def test_answer", "@pytest.mark.skip\ndef test_answer")
    )
    _commit(workspace, "skip")
    _push_after_upstream_moves(seed, workspace)
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert "tests removed, deleted or skipped without brief authorization: app/test_main.py" in result


def test_scope_is_judged_by_the_net_change(pushed_project):
    seed, workspace = pushed_project
    readme = workspace / "README.md"
    original = readme.read_text()
    readme.write_text("edited\n")
    (workspace / "app/feature.py").write_text("x = 1\n")
    _commit(workspace, "touch shared file")
    readme.write_text(original)
    _commit(workspace, "put it back")
    _push_after_upstream_moves(seed, workspace)
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""


def test_changes_that_cancel_out_leave_nothing_to_test(pushed_project):
    seed, workspace = pushed_project
    main, test = workspace / "app/main.py", workspace / "app/test_main.py"
    before = main.read_text(), test.read_text()
    main.write_text("def answer():\n    return 2\n")
    test.write_text(before[1].replace("== 1", "== 2"))
    _commit(workspace, "change answer")
    main.write_text(before[0])
    test.write_text(before[1])
    _commit(workspace, "change it back")
    _push_after_upstream_moves(seed, workspace)
    assert _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline()) == ""
    assert _statuses(workspace)["factory:fail-to-pass"]["status"] == "not applicable"


IDENTITY = "app@agent-factory.localhost"
NO_ATTRIBUTION = (
    "attribution: no commits attributed to project app: commit as " + IDENTITY + " "
    "(GIT_AUTHOR_EMAIL is set for you; do not override --author)"
)


def test_work_committed_under_another_identity_is_not_a_pass(project):
    workspace, _ = project
    git(workspace, "config", "user.email", "someone@example.com")
    (workspace / "app/feature.py").write_text("x = 1\n")
    _commit(workspace, "feature, wrong identity")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result == NO_ATTRIBUTION


def test_uncommitted_work_with_nothing_attributed_is_not_a_pass(project):
    workspace, _ = project
    (workspace / "app/feature.py").write_text("x = 1\n")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result.splitlines() == [
        NO_ATTRIBUTION,
        "uncommitted: commit and push before DONE: app/feature.py",
    ]


def test_uncommitted_files_in_the_project_block_even_with_attributed_commits(project):
    workspace, _ = project
    (workspace / "app/feature.py").write_text("x = 1\n")
    _commit(workspace, "feature")
    (workspace / "app/later.py").write_text("y = 1\n")
    (workspace / "app/main.py").write_text("def answer():\n    return 3\n")
    result = _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline())
    assert result == "uncommitted: commit and push before DONE: app/main.py, app/later.py"


def test_nothing_changed_and_nothing_attributed_is_still_clean(project):
    workspace, _ = project
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, _timeline._Timeline()) == ""


def test_legacy_project_with_nothing_attributed_is_unknown_not_pass(remote_workspace):
    _seed, workspace = remote_workspace
    git(workspace, "config", "user.email", "legacy@example.com")
    (workspace / "app/feature.py").write_text("x = 1\n")
    _commit(workspace, "legacy shared-identity commit")
    assert not (workspace / ".claude-flyte/app/base-commit").exists()
    timeline = _timeline._Timeline()
    assert _gates.run(workspace, "app", "", "pattern", "DONE", TESTED, timeline) == ""
    statuses = _statuses(workspace)
    assert statuses["attribution"]["status"] == "unknown (legacy history)"
    assert "PASS" not in {statuses[r]["status"] for r in ("attribution", "scope", "factory:fail-to-pass")}
    assert any("unknown (legacy history)" in text for _, _, text in timeline._rows)
    assert statuses["attribution"]["output"] == (
        "no commits carry app@agent-factory.localhost, so the project's changes cannot be told apart"
    )
    assert not any(text.count("unknown (legacy history)") > 1 for _, _, text in timeline._rows)


def _new_project(tmp_path, source="def f():\n    return 1\n"):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("shared\n")
    _commit(tmp_path, "base")
    _gates.capture_base(tmp_path, "fresh")
    git(tmp_path, "config", "user.email", "fresh@agent-factory.localhost")
    (tmp_path / "fresh").mkdir()
    (tmp_path / "fresh/lib.py").write_text(source)
    _commit(tmp_path, "new project")


def test_refactor_exemption_does_not_cover_a_brand_new_python_project(tmp_path):
    _new_project(tmp_path)
    result = _gates.run(
        tmp_path, "fresh", "", "", "refactor: no behaviour change", TESTED, _timeline._Timeline()
    )
    assert result == (
        f"{FAIL_TO_PASS_ROW}no tests for new behaviour; no added or changed behaviour tests; "
        "declare refactor: no behaviour change only for a refactor"
    )


def test_refactor_exemption_still_covers_a_project_with_existing_tests(project):
    workspace, _ = project
    (workspace / "app/main.py").write_text("def answer():\n    return 1  # tidied\n")
    _commit(workspace, "tidy")
    note = "refactor: no behaviour change"
    assert _gates.run(workspace, "app", "", "", note, TESTED, _timeline._Timeline()) == ""
    assert "EXEMPT" in _statuses(workspace)["factory:fail-to-pass"]["output"]


def test_every_gate_message_carries_one_rule_prefix(project):
    workspace, _ = project
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    _commit(workspace, "change without a test")
    result = _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline())
    assert result == (
        f"{FAIL_TO_PASS_ROW}no tests for new behaviour; no added or changed behaviour tests; "
        "declare refactor: no behaviour change only for a refactor"
    )
    timeline = _timeline._Timeline()
    _gates.run(workspace, "app", "", "", "DONE", TESTED, timeline)
    doubled = "factory:fail-to-pass: FAIL: factory:fail-to-pass"
    assert not any(doubled in text for _, _, text in timeline._rows)
    assert f"factory:fail-to-pass: FAIL: [{template.FAIL_TO_PASS}] no tests for new behaviour" in "\n".join(
        t for _, _, t in timeline._rows
    )


def test_base_passing_and_head_failing_messages_have_one_prefix(project):
    workspace, _ = project
    (workspace / "app/test_more.py").write_text(
        "from main import answer\n\ndef test_more():\n    assert answer() == 1\n"
    )
    _commit(workspace, "test that already passes")
    result = _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline())
    assert result.startswith(f"{FAIL_TO_PASS_ROW}base must have a failing test (exit 1), observed exit 0\n")
    (workspace / "app/test_more.py").write_text("def test_more():\n    assert False\n")
    _commit(workspace, "failing test")
    result = _gates.run(workspace, "app", "", "", "DONE", TESTED, _timeline._Timeline())
    assert result.startswith(f"{FAIL_TO_PASS_ROW}head tests fail: ")
    assert not re.search(r"(factory:fail-to-pass|scope): \1: ", result)


def test_scope_and_tool_messages_are_exact(project):
    workspace, _ = project
    (workspace / "README.md").write_text("shared\n")
    _commit(workspace, "edit a shared file")
    rules = {
        "checks.md": "## TEST-001 MUST\nCheck: fail\nEnforced: tool "
        + sys.executable
        + " -c 'print(123); raise SystemExit(1)'\n"
    }
    result = _gates.run(workspace, "app", "", "pattern", "DONE", rules, _timeline._Timeline())
    assert result == (
        "scope: outside project/brief: README.md\n"
        f"TEST-001: {sys.executable} -c 'print(123); raise SystemExit(1)'\n123\n"
    )


@pytest.mark.parametrize("bullet", ["- ", "* ", "  - ", "  * "])
def test_evidence_may_be_a_bulleted_line(bullet):
    blocking, advisory = _review._evidenced_findings(
        f"FINDING: crash\n{bullet}Evidence: command: app --empty | output: Traceback\nVERDICT: FAIL"
    )
    assert len(blocking) == 1 and advisory == []


def test_evidence_still_needs_its_fields_when_bulleted():
    blocking, advisory = _review._evidenced_findings(
        "FINDING: crash\n- Evidence: it looked wrong\nVERDICT: FAIL"
    )
    assert blocking == [] and len(advisory) == 1


GOLDEN_STANDARDS_RESULTS = r"""[
  {
    "rule": "attribution",
    "status": "unknown (legacy history)",
    "output": "no commits carry app@agent-factory.localhost, so the project's changes cannot be told apart"
  },
  {
    "rule": "scope",
    "status": "unknown (legacy history)",
    "output": "no commits carry app@agent-factory.localhost, so the project's changes cannot be told apart"
  },
  {
    "rule": "factory:fail-to-pass",
    "status": "unknown (legacy history)",
    "output": "[%(fail_to_pass)s] no commits carry %(who)s, so the project's changes cannot be told apart"
  },
  {
    "rule": "LANG-901",
    "status": "PASS",
    "output": "true\n"
  },
  {
    "rule": "LANG-902",
    "status": "FAIL",
    "output": "false\n"
  },
  {
    "rule": "LANG-903",
    "status": "tool unavailable",
    "output": "image defect: definitely-not-a-factory-binary missing; required by LANG-903"
  },
  {
    "rule": "factory:ui-evidence",
    "status": "not applicable",
    "output": "[%(ui_evidence)s] no UI in the project"
  },
  {
    "rule": "factory:language-summary",
    "status": "FAIL",
    "output": "[%(language_summary)s] language formatter/linter gate: %(languages)s"
  }
]""" % {
    "fail_to_pass": template.FAIL_TO_PASS,
    "ui_evidence": template.UI_EVIDENCE,
    "language_summary": template.LANGUAGE_SUMMARY,
    "who": "app@agent-factory.localhost",
    "languages": "LANG-901 PASS, LANG-902 FAIL, LANG-903 tool unavailable",
}


def _golden_rules():
    return {
        "languages/checks.md": (
            "## LANG-901 MUST\nEnforced: tool true\n"
            "## LANG-902 MUST\nEnforced: tool false\n"
            "## LANG-903 MUST\nEnforced: tool definitely-not-a-factory-binary\n"
        ),
        "engineering.md": (
            f"## {template.FAIL_TO_PASS} MUST\nEnforced: check factory:fail-to-pass\n"
            f"## {template.UI_EVIDENCE} MUST\nEnforced: check factory:ui-evidence\n"
            f"## {template.LANGUAGE_SUMMARY} MUST\nEnforced: check factory:language-summary\n"
        ),
    }


def test_standards_results_json_is_unchanged_by_the_status_enum(project):
    """Captured from the integer-coded gates before `Status` replaced them: PASS, FAIL, tool
    unavailable, not applicable and unknown (legacy history) in one run."""
    workspace, _ = project
    (workspace / ".claude-flyte/app/base-commit").unlink()
    _gates.run(workspace, "app", "", "", "DONE", _golden_rules(), _timeline._Timeline())
    written = (workspace / ".claude-flyte/app/standards-results.json").read_text()
    assert written == GOLDEN_STANDARDS_RESULTS


def test_status_values_are_the_labels_in_standards_results_json():
    assert {s.name: s.value for s in _gate_base.Status} == {
        "PASS": "PASS",
        "FAIL": "FAIL",
        "NOT_APPLICABLE": "not applicable",
        "UNKNOWN": "unknown (legacy history)",
        "TOOL_UNAVAILABLE": "tool unavailable",
        "WAIVED": "WAIVED",
        "NOT_RUN": "not run",
        "HARNESS_FAULT": "harness fault",
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (0, "PASS"),
        (1, "FAIL"),
        (2, "FAIL"),
        (124, "FAIL"),
        (127, "TOOL_UNAVAILABLE"),
    ],
)
def test_status_from_exit(code, status):
    assert _gate_base.Status.from_exit(code) is _gate_base.Status[status]


def test_only_a_failure_blocks():
    assert {s for s in _gate_base.Status if s.blocks} == {
        _gate_base.Status.FAIL,
        _gate_base.Status.TOOL_UNAVAILABLE,
    }


def test_status_is_not_comparable_with_an_exit_code():
    assert _gate_base.Status.PASS != 0  # type: ignore[comparison-overlap]  # the point is comparing with an int
    assert _gate_base.Status.FAIL != 1  # type: ignore[comparison-overlap]  # the point is comparing with an int
    assert _gate_base.Status.TOOL_UNAVAILABLE != 127  # type: ignore[comparison-overlap]  # the point is comparing with an int
    with pytest.raises(TypeError):
        _gate_base.Status.FAIL < 2  # type: ignore[operator]  # noqa: B015


@pytest.mark.parametrize("value", [_gate_base.Status.NOT_APPLICABLE, _gate_base.Status.PASS, True, "0", None])
def test_from_exit_takes_only_exit_codes(value):
    # qa: from_exit(Status.NOT_APPLICABLE) returned FAIL, so a later caller dropping the isinstance guard
    # would silently turn a non-blocking result into a blocking one.
    with pytest.raises(TypeError):
        _gate_base.Status.from_exit(value)


# --- standards entry point (CI self-gate) ----------------------------------------------------------


@pytest.fixture
def python_repo(tmp_path):
    """A tiny Python project that every default Python rule passes on."""
    git(tmp_path, "init")
    (tmp_path / "lib.py").write_text(
        '"""A tiny library."""\n\n\ndef answer() -> int:\n    """Return it."""\n    return 1\n'
    )
    (tmp_path / "test_lib.py").write_text(
        "from lib import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 1\n"
    )
    return tmp_path


def _standards_cli(directory):
    # The tools the gate runs (ruff, mypy, pytest) are the ones installed beside this interpreter.
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    env["PYTHONPATH"] = str(Path(_gates.__file__).parents[1])
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "flyte_agent_factory._gates",
            "standards",
            str(directory),
            "--standards",
            str(template.TEMPLATE),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_standards_cli_exits_zero_on_a_clean_project(python_repo):
    result = _standards_cli(python_repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{template.RUFF_AND_FORMAT}: PASS" in result.stdout


def test_standards_cli_exits_one_on_a_ruff_violation(python_repo):
    lib = python_repo / "lib.py"
    lib.write_text(lib.read_text().replace('"""\n', '"""\n\nimport os\n', 1))
    result = _standards_cli(python_repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{template.RUFF_AND_FORMAT}: FAIL" in result.stdout
    assert "F401" in result.stdout


def test_standards_cli_prints_what_the_factory_gate_records(python_repo, monkeypatch, capsys):
    # The project configures its own ruff and mypy, so neither run appends a baseline config path; the
    # CLI's baseline lives in a temporary directory, the factory's in the workspace.
    (python_repo / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n"
    )
    real_run = _gate_base._run

    def run(args, cwd, **kwargs):
        if args[0] == "git":
            return real_run(args, cwd, **kwargs)
        # Deterministic, unlike pytest's timings; one rule fails so a blocking result is compared too.
        return (1 if "B006" in args else 0), f"ran {' '.join(Path(a).name for a in args)}\n"

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    assert _gates.main(["standards", str(python_repo), "--standards", str(template.TEMPLATE)]) == 1
    printed = capsys.readouterr().out

    config = _standards.Standards(dir=str(template.TEMPLATE))
    files = _standards.select(_standards.capture(config, python_repo), config, python_repo)
    _gates.run(python_repo, "app", "", "", "DONE", files, _timeline._Timeline(), "root")
    judged = {"attribution", "scope", "factory:fail-to-pass", "uncommitted"}
    # A list, not `_statuses`: the formatter rule is two commands, so two records under one rule.
    results = json.loads((python_repo / ".claude-flyte/app/standards-results.json").read_text())
    recorded = [r for r in results if r["rule"] not in judged]
    assert {r["rule"] for r in recorded} >= {
        template.RUFF_AND_FORMAT,
        template.PYTEST,
        template.MUTABLE_DEFAULTS,
        "factory:language-summary",
    }
    expected = "".join(
        f"{r['rule']}: {r['status']}\n" + "".join(f"    {line}\n" for line in r["output"].splitlines())
        for r in recorded
    )
    assert printed == expected


def test_workflow_actions_are_pinned_to_a_commit():
    import yaml

    workflows = sorted((Path(__file__).parents[1] / ".github/workflows").glob("*.yml"))
    assert workflows
    uses = []
    for path in workflows:
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            uses += [job["uses"]] if "uses" in job else []
            uses += [step["uses"] for step in job.get("steps", []) if "uses" in step]
    assert uses
    # A tag can be moved to other code; a commit cannot.
    assert [u for u in uses if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", u)] == []


def test_tool_timeout_comes_from_the_rule(project, monkeypatch):
    workspace, _ = project
    timeouts = {}

    def run(args, cwd, **kwargs):
        if args[0] == "git":
            return 0, ""
        timeouts[args[0]] = kwargs.get("timeout", 300)
        return 0, ""

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {
        "checks.md": "## TEST-003 MUST\nEnforced: tool slow-suite\nTimeout: 1200\n\n"
        "## TEST-004 MUST\nEnforced: tool quick-lint && other-lint\n"
    }
    _gates.standards_results(workspace / "app", rules, configs=workspace)
    assert timeouts == {"slow-suite": 1200, "quick-lint": 300, "other-lint": 300}


@pytest.mark.parametrize(
    "prose", ["Timeouts must be set on all network operations.", "Timeout handling is required."]
)
def test_timeout_prose_does_not_prevent_a_rule_from_running(tmp_path, monkeypatch, prose):
    ran = []

    def run(args, cwd, **kwargs):
        ran.append(args)
        return 0, "checked"

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {"checks.md": f"## TEST-007 MUST\n{prose}\nEnforced: tool some-tool\n"}
    [result] = _gates.standards_results(tmp_path, rules, configs=tmp_path)
    assert (result.rule, result.status) == ("TEST-007", _gate_base.Status.PASS)
    assert result.output == "some-tool\nchecked"
    assert ran == [["some-tool"]]


def test_shipped_pytest_rule_allows_a_long_suite():
    # The factory repo's own suite takes about 7.5 minutes; every other tool rule keeps the default.
    timed = {
        ident: cast(re.Match[str], re.search(r"^Timeout: (\d+)$", body, re.MULTILINE))[1]
        for text in template.FILES.values()
        for ident, _, body in _standards.RULE.findall(text)
        if re.search(r"^Timeout: ", body, re.MULTILINE)
    }
    assert timed == {template.PYTEST: "1200"}


def test_a_timed_out_command_reports_its_own_limit(tmp_path):
    code, output = _gate_base._run([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, timeout=1)
    assert (code, output) == (124, "command timed out after 1 seconds")


@pytest.mark.parametrize("make", [lambda p: p / "missing", lambda p: p / "lib.py"])
def test_standards_cli_rejects_a_path_that_is_not_a_directory(python_repo, make):
    result = _standards_cli(make(python_repo))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "not a directory" in result.stderr
    assert result.stdout == ""


def test_crlf_standards_still_block(tmp_path, monkeypatch):
    standards = tmp_path / "std"
    for name, text in template.FILES.items():
        (standards / name).parent.mkdir(parents=True, exist_ok=True)
        (standards / name).write_bytes(text.replace("\n", "\r\n").encode())
    project = tmp_path / "app"
    project.mkdir()
    (project / "m.py").write_text("import os\n")
    monkeypatch.setattr(_gate_base, "_run", lambda args, cwd, **kwargs: (1, "m.py:1:8: F401"))
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    config = _standards.settings({"dir": str(standards)})
    files = _standards.select(_standards.capture(config, tmp_path), config, project)
    results = _gates.standards_results(project, files, configs=standards / "languages/configs")
    assert any(r.rule == template.RUFF_AND_FORMAT and r.status is _gate_base.Status.FAIL for r in results)


@pytest.mark.parametrize(
    "line",
    [
        "Timeout: 0",
        "Timeout: abc",
        "Timeout: 1s",
        "Timeout:1",
        "Timeout : 1",
        "Timeout: 1 ",
        "timeout: 1",
        "  Timeout: 1",
        "Timeout: -1",
        "Timeout: 1.5",
        "Timeout: \u0661",
        "Timeout: 7201",
        "Timeout: 99999999999999999999",
        "Timeout: 1\nTimeout: 2",
    ],
)
def test_a_malformed_timeout_fails_the_rule_without_running_it(tmp_path, monkeypatch, line):
    ran = []

    def run(args, cwd, **kwargs):
        ran.append(args)
        return 0, ""

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {"checks.md": f"## TEST-005 MUST\nEnforced: tool some-tool\n{line}\n"}
    [result] = _gates.standards_results(tmp_path, rules, configs=tmp_path)
    assert (result.rule, result.status) == ("TEST-005", _gate_base.Status.FAIL)
    assert "Timeout" in result.output and not ran


def test_the_largest_timeout_is_accepted(tmp_path, monkeypatch):
    seen = []

    def run(args, cwd, **kwargs):
        seen.append(kwargs["timeout"])
        return 0, ""

    monkeypatch.setattr(_gate_base, "_run", run)
    monkeypatch.setattr(shutil, "which", lambda binary: "/bin/" + binary)
    rules = {
        "checks.md": f"## TEST-006 MUST\nEnforced: tool some-tool\nTimeout: {_gate_base._MAX_TOOL_TIMEOUT}\n"
    }
    [result] = _gates.standards_results(tmp_path, rules, configs=tmp_path)
    assert result.status is _gate_base.Status.PASS and seen == [7200]


def test_gate_labels_filter_prose_and_bound_titles(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "app").mkdir()
    (tmp_path / "app/README.custom").touch()
    output = (
        "version 1.2.3 https://example.com/help.html example.com "
        "README.custom main.py:12 src/deleted.xyz:7 " + "x" * 800
    )
    monkeypatch.setattr(
        _gates,
        "standards_results",
        lambda *args, **kwargs: [SimpleNamespace(rule="lint", status=_gate_base.Status.FAIL, output=output)],
    )
    labels = []
    timeline = SimpleNamespace(
        review_round=0, metric_label=lambda **label: labels.append(label), row=lambda *a: None
    )
    _gates.run(tmp_path, "app", "", "", "DONE", TESTED, timeline)
    labels = [label for label in labels if label["name"] == "lint"]
    assert [(label["file"], label["line"]) for label in labels] == [
        ("README.custom", None),
        ("main.py", 12),
        ("src/deleted.xyz", 7),
    ]
    assert all(len(label["title"]) == 500 for label in labels)


def _label_gate(tmp_path, monkeypatch, output):
    from types import SimpleNamespace

    (tmp_path / "app").mkdir(exist_ok=True)
    monkeypatch.setattr(
        _gates,
        "standards_results",
        lambda *args, **kwargs: [SimpleNamespace(rule="lint", status=_gate_base.Status.FAIL, output=output)],
    )
    labels = []
    timeline = SimpleNamespace(
        review_round=0, metric_label=lambda **label: labels.append(label), row=lambda *a: None
    )
    result = _gates.run(tmp_path, "app", "", "", "DONE", TESTED, timeline)
    return result, [label for label in labels if label["name"] == "lint"]


def test_a_path_like_token_too_long_for_the_filesystem_does_not_crash_the_gate(tmp_path, monkeypatch):
    # `Path.is_file` raises OSError(ENAMETOOLONG) on a 256-character component.
    long_token = "x" * 256 + ".py"
    result, labels = _label_gate(tmp_path, monkeypatch, f"bad {long_token}:3 and ok.py:4")
    assert result == f"lint: bad {long_token}:3 and ok.py:4"
    assert [(label["file"], label["line"]) for label in labels] == [("ok.py", 4)]


def test_label_recording_never_changes_the_gate_result(tmp_path, monkeypatch):
    from types import SimpleNamespace

    (tmp_path / "app").mkdir()
    monkeypatch.setattr(
        _gates,
        "standards_results",
        lambda *args, **kwargs: [
            SimpleNamespace(rule="lint", status=_gate_base.Status.FAIL, output="main.py:1")
        ],
    )

    def broken(**label):
        raise OSError(36, "File name too long")

    timeline = SimpleNamespace(review_round=0, metric_label=broken, row=lambda *a: None)
    assert _gates.run(tmp_path, "app", "", "", "DONE", TESTED, timeline) == "lint: main.py:1"


@pytest.mark.parametrize("token", ["3/4.5", "2026/10.25", "ops/team.lead"])
def test_numbers_and_prose_with_a_slash_are_not_file_labels(tmp_path, monkeypatch, token):
    _, labels = _label_gate(tmp_path, monkeypatch, f"ratio {token} seen in src/real.py:2")
    assert [label["file"] for label in labels] == ["src/real.py"]


def test_a_long_path_is_still_a_label_and_the_timeline_caps_it_at_300(tmp_path, monkeypatch):
    path = "a" * 200 + "/" + "b" * 200 + ".py"
    _, labels = _label_gate(tmp_path, monkeypatch, f"{path}:9")
    assert [label["file"] for label in labels] == [path]
    timeline = _timeline._Timeline()
    timeline.metric_label(**labels[0])
    assert len(timeline.metric_labels[0]["file"]) == 300


def test_timeline_caps_label_fields():
    timeline = _timeline._Timeline()
    timeline.metric_label(kind="judge", file="f" * 900, title="t" * 900, evidence_kind="e" * 900)
    [label] = timeline.metric_labels
    assert (len(label["file"]), len(label["title"]), len(label["evidence_kind"])) == (300, 200, 200)


def test_missing_required_tool_is_an_image_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda command: None)
    files = {"shell.md": "## SHELL-2 MUST\nCheck scripts.\n\nEnforced: command shellcheck\n"}
    result = _gates.standards_results(tmp_path, files, configs=tmp_path)[0]
    assert result.status == _gate_base.Status.TOOL_UNAVAILABLE
    assert result.status.blocks
    assert result.output == "image defect: shellcheck missing; required by SHELL-2"


def test_startup_reports_missing_applicable_tools_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(shutil, "which", lambda command: None if command == "ruff" else command)
    files = {
        "python.md": (
            "## FMT-1 MUST\nLint.\n\nEnforced: tool ruff check . && ruff format --check .\n\n"
            "## FMT-2 MUST\nLint.\n\nEnforced: command ruff check --select B006 .\n\n"
            "## LINT-099 SHOULD\nOptional.\n\nEnforced: tool optional\n"
        )
    }
    timeline = _timeline._Timeline()
    _session_setup._check_image_tools(files, timeline)
    message = "image defect: ruff missing; required by FMT-1, FMT-2"
    assert any(icon == "🚨" and text == message for _, icon, text in timeline._rows)
    assert message in timeline.snapshot()["risks"]


def test_pytest_rule_uses_the_isolated_installed_tool(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(shutil, "which", lambda command: "/opt/gate-tools/bin/" + command)

    def run(command, *a, **kw):
        commands.append(command)
        return 0, "ok"

    monkeypatch.setattr(_gate_base, "_run", run)
    files = {"python.md": "## TESTS-1 MUST\nTests.\n\nEnforced: tool python -m pytest\n"}
    result = _gates.standards_results(tmp_path, files, configs=tmp_path)[0]
    assert result.status == _gate_base.Status.PASS
    assert commands == [["pytest"]]


def test_pytest_rule_prefers_the_project_tool(tmp_path, monkeypatch):
    local = tmp_path / ".venv/bin/pytest"
    local.parent.mkdir(parents=True)
    local.touch()
    commands = []
    monkeypatch.setattr(shutil, "which", lambda command: str(command))

    def run(command, *a, **kw):
        commands.append(command)
        return 0, "ok"

    monkeypatch.setattr(_gate_base, "_run", run)
    files = {"python.md": "## TESTS-1 MUST\nTests.\n\nEnforced: tool python -m pytest\n"}
    _gates.standards_results(tmp_path, files, configs=tmp_path)
    assert commands == [[str(local)]]


def test_missing_pytest_module_is_an_image_defect(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda command: None if command == "pytest" else command)
    monkeypatch.setattr(_gate_base, "_run", lambda *a, **kw: (1, "python: No module named pytest"))
    files = {"python.md": "## TESTS-1 MUST\nTests.\n\nEnforced: tool python -m pytest\n"}
    result = _gates.standards_results(tmp_path, files, configs=tmp_path)[0]
    assert result.status == _gate_base.Status.TOOL_UNAVAILABLE
    assert result.status.blocks
    assert "image defect: pytest missing; required by TESTS-1" in result.output


def test_the_startup_tool_check_never_stops_the_supervisor():

    timeline = _timeline._Timeline()
    _session_setup._check_image_tools(None, timeline)  # no standards resolved: nothing to check
    _session_setup._check_image_tools({"languages/x.md": "Enforced: tool"}, timeline)  # malformed pack


def _suppression_results(project):
    rules = {
        "engineering.md": template.rule_text(template.EXCEPTIONS),
        "languages/python.md": template.rule_text(template.MUTABLE_DEFAULTS),
    }
    return _gates.standards_results(project, rules, configs=template.TEMPLATE / "languages/configs")


@pytest.mark.parametrize(
    "source, diagnostic",
    [
        ("x = 1  # noqa\n", "specific codes"),
        ("x = 1  # noqa: F401 # short\n", "10 characters"),
        ("def f(x=[]):  # noqa: B006 # shared state is intentional\n    pass\n", "B006"),
        ("try:\n    work()\nexcept Exception:\n    pass\n", "broad catch"),
        ("from contextlib import suppress\nwith suppress(Exception):\n    work()\n", "suppress"),
        ("import contextlib as c\nwith c.suppress(BaseException):\n    work()\n", "suppress"),
        ("from contextlib import suppress as quiet\nwith quiet(Exception):\n    work()\n", "suppress"),
        (
            "try:\n    work()\nexcept Exception:  # noqa: BLE001 # loop boundary keeps processing\n"
            "    logger.exception('failed')\n",
            "loop boundary",
        ),
        (
            "while True:\n    try:\n        work()\n"
            "    except Exception:  # noqa: BLE001 # loop boundary keeps processing\n        pass\n",
            "broad catch",
        ),
    ],
)
def test_suppression_policy_rejects_invalid_source(tmp_path, source, diagnostic):
    (tmp_path / "main.py").write_text(source)
    result = next(r for r in _suppression_results(tmp_path) if r.rule == "factory:suppressions")
    assert result.status is _gate_base.Status.FAIL
    assert "main.py:" in result.output
    assert diagnostic in result.output


@pytest.mark.parametrize(
    "source",
    [
        "while True:\n    try:\n        work()\n"
        "    except Exception:  # noqa: BLE001 # loop boundary must keep processing\n"
        "        logger.exception('work failed')\n",
        "x = '# noqa'\n",
        "import example  # noqa: F401 # imported for registration\n",
        "import example  # noqa: F401 -- imported for registration\n",
        "try:\n    work()\nexcept ValueError:\n    pass\n",
    ],
)
def test_suppression_policy_accepts_documented_exceptions(tmp_path, source):
    (tmp_path / "main.py").write_text(source)
    result = next(r for r in _suppression_results(tmp_path) if r.rule == "factory:suppressions")
    assert result.status is _gate_base.Status.PASS


def test_eng_007_cannot_be_disabled_by_project_ruff_config(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.ruff.lint]\nignore = ["BLE001"]\nexclude = ["main.py"]\n')
    (tmp_path / "main.py").write_text("try:\n    work()\nexcept Exception:\n    pass\n")
    result = next(r for r in _suppression_results(tmp_path) if r.rule == template.EXCEPTIONS)
    assert result.status is _gate_base.Status.FAIL
    assert "--isolated" in result.output
    assert "BLE001" in result.output


@pytest.mark.parametrize(
    "source, status",
    [
        ("# ruff: noqa: B006 # shared state is intentional\n", _gate_base.Status.FAIL),
        ("# Documentation explains when noqa is appropriate.\n", _gate_base.Status.PASS),
        (
            "while True:\n    try:\n        work()\n    except Exception:\n"
            "        if False:\n            logger.exception('failed')\n",
            _gate_base.Status.FAIL,
        ),
        ("try:\n    work()\nexcept Exception:\n    raise\n", _gate_base.Status.PASS),
        (
            "while True:\n    try:\n        work()\n"
            "    except Exception:  # noqa: BLE001 -- keep processing other items\n"
            "        continue\n        logger.exception('failed')\n",
            _gate_base.Status.FAIL,
        ),
        (
            "while True:\n    try:\n        work()\n"
            "    except Exception:  # noqa: BLE001 # keep processing other items\n"
            "        logger.error('failed', exc_info=True)\n",
            _gate_base.Status.PASS,
        ),
        (
            "while True:\n    def work():\n        try:\n            fail()\n"
            "        except Exception:  # noqa: BLE001 # keep processing other items\n"
            "            logger.exception('failed')\n",
            _gate_base.Status.FAIL,
        ),
    ],
)
def test_suppression_policy_checks_directives_and_handler_structure(tmp_path, source, status):
    (tmp_path / "main.py").write_text(source)
    result = next(r for r in _suppression_results(tmp_path) if r.rule == "factory:suppressions")
    assert result.status is status


_OLD_DEBT = "import example  # noqa\n\ntry:\n    work()\nexcept Exception:\n    pass\n"


def _repo_with_debt(workspace, layout):
    """A committed base that already breaks the suppression policy, as the engine repo does."""
    git(workspace, "init")
    git(workspace, "config", "user.email", "test@example.com")
    git(workspace, "config", "user.name", "test")
    project = _gate_base.project_dir(workspace, "app", layout)
    project.mkdir(exist_ok=True)
    (project / "old.py").write_text(_OLD_DEBT)
    git(workspace, "add", "-A")
    git(workspace, "commit", "-m", "base")
    _gates.capture_base(workspace, "app")
    git(workspace, "config", "user.email", "app@agent-factory.localhost")
    return project


def _suppressions_after_run(workspace, layout):
    rules = {
        "engineering.md": template.rule_text(template.EXCEPTIONS),
        "languages/python.md": template.rule_text(template.MUTABLE_DEFAULTS),
    }
    _gates.run(
        workspace, "app", "", "", "refactor: no behaviour change", rules, _timeline._Timeline(), layout
    )
    return _statuses(workspace)["factory:suppressions"]


@pytest.mark.parametrize("layout", ["sandbox", "root"])
def test_suppressions_ignore_debt_the_project_did_not_add(tmp_path, layout):
    project = _repo_with_debt(tmp_path, layout)
    (project / "new.py").write_text("import example  # noqa: F401 -- imported for registration\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "clean change")
    result = _suppressions_after_run(tmp_path, layout)
    assert result["status"] == "PASS", result["output"]


@pytest.mark.parametrize("layout", ["sandbox", "root"])
@pytest.mark.parametrize(
    "addition, line, diagnostic",
    [
        ("import other  # noqa\n", 8, "specific codes"),
        ("try:\n    work()\nexcept Exception:\n    pass\n", 10, "broad catch"),
    ],
)
def test_suppressions_report_only_the_lines_the_project_added(tmp_path, layout, addition, line, diagnostic):
    project = _repo_with_debt(tmp_path, layout)
    (project / "old.py").write_text(_OLD_DEBT + "\n" + addition)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "new debt")
    result = _suppressions_after_run(tmp_path, layout)
    assert result["status"] == "FAIL"
    [problem] = result["output"].splitlines()
    assert problem.startswith(f"old.py:{line}: ") and diagnostic in problem


@pytest.mark.parametrize(
    "edit, line",
    [
        (("import example  # noqa\n", "import example, other  # noqa\n"), 1),
        # Any line of a swallowing handler counts, not only the `except` line.
        (("    pass\n", "    pass  # still swallowed\n"), 5),
    ],
)
def test_suppressions_report_existing_debt_on_a_line_the_project_edits(tmp_path, edit, line):
    project = _repo_with_debt(tmp_path, "sandbox")
    (project / "old.py").write_text(_OLD_DEBT.replace(*edit))
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "touch debt")
    result = _suppressions_after_run(tmp_path, "sandbox")
    assert result["status"] == "FAIL"
    assert [r.split(":")[1] for r in result["output"].splitlines()] == [str(line)]


def test_suppressions_check_every_line_without_a_base_and_say_so(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/old.py").write_text(_OLD_DEBT)
    result = _suppressions_after_run(tmp_path, "sandbox")
    assert result["status"] == "FAIL"
    assert "old.py:1:" in result["output"] and "old.py:5:" in result["output"]
    assert "every line" in result["output"]


def test_suppressions_treat_an_untracked_file_as_added(tmp_path):
    project = _repo_with_debt(tmp_path, "sandbox")
    (project / "scratch.py").write_text("x = 1  # noqa\n")
    result = _suppressions_after_run(tmp_path, "sandbox")
    assert result["status"] == "FAIL"
    [problem] = result["output"].splitlines()
    assert problem.startswith("scratch.py:1: ")


def test_fail_to_pass_gives_the_suite_the_test_timeout_not_the_lint_one(project, monkeypatch):
    """A round that touched many test files runs most of the suite: it needs the test timeout."""
    workspace, base = project
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    (workspace / "app/test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 2\n"
    )
    timeouts = []
    real = _gate_base._run

    def run(command, cwd, **kwargs):
        if "pytest" in command:
            timeouts.append(kwargs.get("timeout"))
        return real(command, cwd, **kwargs)

    monkeypatch.setattr(_gate_base, "_run", run)
    code, _ = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/main.py", "app/test_main.py"], "", "", "DONE"
    )
    assert code == 0
    assert timeouts == [_gate_base.TEST_TIMEOUT, _gate_base.TEST_TIMEOUT] and _gate_base.TEST_TIMEOUT == 1200


@pytest.mark.parametrize(
    "source, status",
    [
        # Handing the exception to a future re-raises it where the future is awaited (as concurrent.futures).
        (
            "def run():\n    try:\n        work()\n"
            "    except BaseException as e:  # noqa: BLE001 -- handed to the awaiting coroutine\n"
            "        future.set_exception(e)\n",
            _gate_base.Status.PASS,
        ),
        (
            "def run():\n    try:\n        work()\n"
            "    except BaseException as e:  # noqa: BLE001 -- handed to the awaiting coroutine\n"
            "        settle(lambda err=e: future.done() or future.set_exception(err))\n",
            _gate_base.Status.PASS,
        ),
        # Calling set_exception with something else is not handing this exception on.
        (
            "def run():\n    try:\n        work()\n"
            "    except BaseException as e:  # noqa: BLE001 -- handed to the awaiting coroutine\n"
            "        future.set_exception(RuntimeError('failed'))\n",
            _gate_base.Status.FAIL,
        ),
        (
            "def run():\n    try:\n        work()\n    except Exception:  # noqa: BLE001 -- swallowed\n"
            "        future.set_result(None)\n",
            _gate_base.Status.FAIL,
        ),
    ],
)
def test_a_broad_handler_may_hand_its_exception_to_a_future(tmp_path, source, status):
    (tmp_path / "main.py").write_text(source)
    result = next(r for r in _suppression_results(tmp_path) if r.rule == "factory:suppressions")
    assert result.status is status, result.output


# --- the managed memory block and commit attribution ------------------------------------------------------


def test_the_memory_markers_are_current_and_the_previous_pair_is_only_read():
    assert (_gates.MEMORY_BEGIN, _gates.MEMORY_END) == (
        "<!-- agent-factory: begin -->",
        "<!-- agent-factory: end -->",
    )
    assert (
        _gates.PREVIOUS_MEMORY_BEGIN != _gates.MEMORY_BEGIN
        and _gates.PREVIOUS_MEMORY_END != _gates.MEMORY_END
    )
    assert not hasattr(_gates, "LEGACY_COMMIT_DOMAIN")


def test_apply_managed_block_moves_a_previous_block_to_the_current_markers_in_place():
    previous = f"{_gates.PREVIOUS_MEMORY_BEGIN}\nold\n{_gates.PREVIOUS_MEMORY_END}"
    result = _gates.apply_managed_block(f"before\n{previous}\nafter\n", " new ")
    assert result == f"before\n{_gates.MEMORY_BEGIN}\nnew\n{_gates.MEMORY_END}\nafter\n"
    assert _gates.apply_managed_block(result, "new") == result


def test_a_committed_previous_block_rewritten_by_the_harness_is_harness_only():
    previous = f"{_gates.PREVIOUS_MEMORY_BEGIN}\nold\n{_gates.PREVIOUS_MEMORY_END}"
    committed = f"# Rules\n\n{previous}\n\nmore rules\n"
    current = _gates.apply_managed_block(committed, "new")
    assert _gates.MEMORY_BEGIN in current and _gates.PREVIOUS_MEMORY_BEGIN not in current
    assert _gates._harness_only("CLAUDE.md", current.encode(), committed.encode())
    # A project edit next to the migrated block still counts.
    assert not _gates._harness_only("CLAUDE.md", (current + "extra\n").encode(), committed.encode())


def _commit_as(path, email, message):
    _git_clean_env(path, "-c", f"user.email={email}", "commit", "--allow-empty", "-m", message)


def _git_clean_env(path, *args):
    # Without the session's own author variables, which would override `user.email`.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _attributed(path, name, base):
    code, output = _gates._project_commits(path, name, base)
    assert code == 0
    return sorted(_git_clean_env(path, "log", "-1", "--format=%s", sha) for sha in output.splitlines())


def test_attribution_counts_any_localhost_identity_of_the_project_and_nothing_else(tmp_path):
    _git_clean_env(tmp_path, "init")
    _git_clean_env(tmp_path, "config", "user.name", "t")
    _commit_as(tmp_path, "base@example.com", "base")
    base = _git_clean_env(tmp_path, "rev-parse", "HEAD")
    for email, message in (
        (_gates.commit_identity("app"), "current domain"),
        ("app@older.localhost", "older domain"),
        ("app@example.com", "shared identity"),
        (_gates.commit_identity("other"), "another project"),
        (f"x{_gates.commit_identity('app')}", "name with a prefix"),
        ("app@agent-factory.localhost.example.com", "domain that only starts like it"),
    ):
        _commit_as(tmp_path, email, message)
    assert _attributed(tmp_path, "app", base) == ["current domain", "older domain"]


def test_attribution_does_not_treat_the_name_as_a_pattern(tmp_path):
    _git_clean_env(tmp_path, "init")
    _git_clean_env(tmp_path, "config", "user.name", "t")
    _commit_as(tmp_path, "base@example.com", "base")
    base = _git_clean_env(tmp_path, "rev-parse", "HEAD")
    _commit_as(tmp_path, "a.b@agent-factory.localhost", "literal dot")
    _commit_as(tmp_path, "aXb@agent-factory.localhost", "any character")
    assert _attributed(tmp_path, "a.b", base) == ["literal dot"]


def test_new_commits_are_authored_under_the_agent_factory_identity():
    assert _gates.commit_identity("app") == "app@agent-factory.localhost"
