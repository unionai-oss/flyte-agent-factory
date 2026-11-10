"""The engine names no owner's rule: any set of standards, with any IDs, gets the same gate behaviour."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gates, _module_size, _standards, _timeline
from tests._git import git
from tests._standards import FAIL_TO_PASS, RULES, TEMPLATE

ROOT = Path(__file__).resolve().parents[1]
Status = _gate_base.Status

pytestmark = pytest.mark.real_factory_gates

# Rules the template does not have: one of each declared kind that it leaves out.
EXTRA = """\
## X-1 MUST
Never print in source.

Why: w

Check: grep

Enforced: forbid \\bprint\\( in src/**/*.py

## X-2 MUST
The project's own script passes.

Why: w

Check: run it

Enforced: script checks/ok.py

## X-3 MUST
A script that fails.

Why: w

Check: run it

Enforced: script checks/fails.sh --strict
"""
SCRIPTS = {
    "checks/ok.py": "print('fine')\n",
    "checks/fails.sh": '#!/usr/bin/env bash\necho "strict: $1"\nexit 3\n',
}
OVER_THE_LIMIT = "x = 1\n" * (_module_size.MODULE_LINE_LIMIT + 1)
PASSING = {"src/app.py": '"""App."""\n\n\ndef answer() -> int:\n    """Return it."""\n    return 1\n'}
TREES: dict[str, tuple[dict[str, str], str]] = {
    "clean": (PASSING, ""),
    "lint violation": (
        {
            "src/app.py": PASSING["src/app.py"]
            + "\n\ndef f():\n    try:\n        pass\n    except:\n        pass\n"
        },
        "",
    ),
    "forbidden pattern": ({"src/app.py": PASSING["src/app.py"] + "print(answer())\n"}, ""),
    "oversized module": ({"src/big.py": OVER_THE_LIMIT}, ""),
    "pattern demo": (PASSING, "pattern"),
    "showcase demo": (PASSING, "showcase"),
}


def with_extra_rules(source: Path, into: Path) -> None:
    """Copy the template set into `into` with the rules of `EXTRA` and their scripts."""
    shutil.copytree(source, into)
    (into / "extra.md").write_text(EXTRA)
    for name, text in SCRIPTS.items():
        (into / name).parent.mkdir(parents=True, exist_ok=True)
        (into / name).write_text(text)


def renamed(source: Path, into: Path) -> dict[str, str]:
    """Copy `source` to `into` with every rule ID in a new scheme and the base files under new names.

    Returns the old ID -> new ID map.
    """
    shutil.copytree(source, into)
    idents = [r.ident for r in _standards.rules(_standards._read_standards(source))]
    new = {ident: f"ACME.{n}" for n, ident in enumerate(idents, 1)}
    # Longest first: `X-1` must not match inside `X-10`.
    pattern = re.compile(
        r"(?<![A-Za-z0-9._-])("
        + "|".join(sorted(map(re.escape, new), key=len, reverse=True))
        + r")(?![A-Za-z0-9_-])"
    )
    for path in into.rglob("*.md"):
        path.write_text(pattern.sub(lambda m: new[m[1]], path.read_text()))
    for old, name in (("engineering", "acme-rules"), ("security", "acme-safety"), ("extra", "acme-extra")):
        if (into / f"{old}.md").exists():
            (into / f"{old}.md").rename(into / f"{name}.md")
    return new


def results_for(directory: Path, tree: dict[str, str], demo: str, tmp_path: Path, tag: str):
    """Run the standards in `directory` over a project with the files of `tree`; return {rule: status}."""
    project = tmp_path / f"project-{tag}"
    for name, text in tree.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    workspace = tmp_path / f"workspace-{tag}"
    # Only the base files and language packs the project calls for are materialized, as at launch.
    base = sorted(p.stem for p in directory.glob("*.md") if p.stem not in {"README", "brand", "demo"})
    config = _standards.Standards(dir=str(directory), include=tuple(base), languages=("python",))
    files = _standards.capture(config, tmp_path)
    selected = _standards.materialize(workspace, project, config, files, demo=demo)
    return {
        r.rule: r.status
        for r in _gates.standards_results(
            project,
            selected,
            configs=workspace / ".claude-flyte/standards/languages/configs",
            size_reference=None,
        )
    }


def test_a_workspace_with_different_rule_ids_gets_the_same_gate_behaviour(tmp_path):
    original = tmp_path / "original"
    with_extra_rules(TEMPLATE, original)
    different = tmp_path / "different"
    ids = renamed(original, different)
    assert not set(ids) & set(ids.values())
    assert all(re.fullmatch(r"ACME\.\d+", v) for v in ids.values())
    # The base files are named differently, and the profile's `include` names them.
    assert not (different / "engineering.md").exists() and (different / "acme-rules.md").is_file()
    differences = []
    for label, (tree, demo) in TREES.items():
        tag = label.replace(" ", "-")
        before = results_for(original, tree, demo, tmp_path, f"a-{tag}")
        after = results_for(different, tree, demo, tmp_path, f"b-{tag}")
        mapped = {ids.get(rule, rule): status for rule, status in before.items()}
        if mapped != after:
            differences.append((label, mapped, after))
    assert not differences
    # The cases are not all alike: the comparison is made on something.
    statuses = {
        r: s
        for label, (tree, demo) in TREES.items()
        for r, s in results_for(original, tree, demo, tmp_path, f"c-{label}").items()
    }
    assert Status.FAIL in set(statuses.values())


def test_the_fail_to_pass_path_is_the_same_under_different_rule_ids(project, tmp_path):
    original = tmp_path / "original"
    shutil.copytree(TEMPLATE, original)
    different = tmp_path / "different"
    ids = renamed(original, different)
    workspace = project[0]
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    git(workspace, "add", "app")
    git(workspace, "commit", "-m", "change without tests")
    seen = {}
    for label, directory in (("original", original), ("different", different)):
        files = {
            n: t
            for n, t in _standards._read_standards(directory).items()
            if n.endswith("engineering.md") or n.endswith("acme-rules.md")
        }
        blocking = _gates.run(workspace, "app", "", "", "DONE", files, _timeline._Timeline())
        results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
        seen[label] = (
            next(r for r in results if r["rule"] == _standards.FAIL_TO_PASS)["status"],
            _standards.FAIL_TO_PASS in blocking,
        )
    assert seen["original"] == seen["different"] == ("FAIL", True)
    assert ids[FAIL_TO_PASS].startswith("ACME.")


def test_the_engine_names_none_of_the_owners_rule_ids():
    owners = {rule.ident for rule in RULES}
    pattern = re.compile(
        r"(?<![A-Za-z0-9._-])(?:" + "|".join(map(re.escape, sorted(owners))) + r")(?![A-Za-z0-9_-])"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    skipped = (
        "src/flyte_agent_factory/_assets/workspace_template/",
        "standards/",
        ".claude-flyte/",
        "briefs/",
    )
    found = []
    for name in filter(None, tracked):
        path = ROOT / name
        if name.startswith(skipped) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += [f"{name}: {m[0]}" for m in pattern.finditer(text)]
    assert not found, found[:20]


# --- the declared kinds, one rule at a time ---


def rule_text(ident: str, enforced: str, level: str = "MUST", extra: str = "") -> str:
    return f"## {ident} {level}\nBody.\n\nWhy: w\n\nCheck: c\n\n{extra}Enforced: {enforced}\n\n"


def gate(tmp_path, text: str, tree: dict[str, str] | None = None, scripts: dict[str, str] | None = None):
    """Run `text` as the one standards file over a project of `tree`; return {rule: Result}."""
    project = tmp_path / "project"
    shutil.rmtree(project, ignore_errors=True)  # a test may gate several trees
    project.mkdir()
    for name, body in (tree or {"a.py": "x = 1\n"}).items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(body)
    standards = tmp_path / "standards"
    for name, body in (scripts or {}).items():
        (standards / name).parent.mkdir(parents=True, exist_ok=True)
        (standards / name).write_text(body)
    (standards / "languages/configs").mkdir(parents=True, exist_ok=True)
    results = _gates.standards_results(
        project, {"rules.md": text}, configs=standards / "languages/configs", standards_dir=standards
    )
    return {r.rule: r for r in results}


def test_forbid_without_in_searches_every_text_file(tmp_path):
    results = gate(
        tmp_path, rule_text("A.1", r"forbid TODO\d"), {"a.py": "ok\n", "b/notes.txt": "x\nTODO1\n"}
    )
    assert results["A.1"].status is Status.FAIL
    assert "b/notes.txt:2: TODO1" in results["A.1"].output


def test_forbid_in_globs_limits_the_files_searched(tmp_path):
    text = rule_text("A.1", r"forbid TODO in src/**/*.py,*.md")
    tree = {"src/a.py": "TODO\n", "docs/b.py": "TODO\n", "README.md": "fine\n"}
    results = gate(tmp_path, text, tree)
    assert results["A.1"].output.splitlines() == ["src/a.py:1: TODO"]
    assert gate(tmp_path, text, {"docs/b.py": "TODO\n"})["A.1"].status is Status.PASS


def test_forbid_never_searches_harness_and_dependency_directories(tmp_path):
    tree = {f"{d}/x.py": "TODO\n" for d in (".git", ".venv", "node_modules", ".claude-flyte")}
    assert gate(tmp_path, rule_text("A.1", "forbid TODO"), tree)["A.1"].status is Status.PASS


def test_forbid_with_an_invalid_regex_fails_naming_the_rule(tmp_path):
    result = gate(tmp_path, rule_text("A.1", "forbid ("))["A.1"]
    assert result.status is Status.FAIL
    assert "not a regular expression" in result.output


@pytest.mark.parametrize(
    ("scripts", "arg", "status", "needle"),
    [
        ({"ok.py": "import sys\nprint('py', sys.argv[1:])\n"}, "ok.py --a", Status.PASS, "py ['--a']"),
        ({"ok.sh": 'echo "sh $1"\n'}, "ok.sh x", Status.PASS, "sh x"),
        ({"bad.py": "raise SystemExit(2)\n"}, "bad.py", Status.FAIL, "bad.py"),
        ({"bad.sh": "echo nope\nexit 1\n"}, "bad.sh", Status.FAIL, "nope"),
        ({}, "missing.py", Status.FAIL, "missing.py not found in the standards directory"),
        ({}, "../outside.py", Status.FAIL, "outside the standards directory"),
    ],
)
def test_script_rules_run_from_the_standards_directory(tmp_path, scripts, arg, status, needle):
    result = gate(tmp_path, rule_text("A.1", f"script {arg}"), scripts=scripts)["A.1"]
    assert result.status is status
    assert needle in result.output


def test_a_script_runs_in_the_project_directory(tmp_path):
    result = gate(
        tmp_path,
        rule_text("A.1", "script where.py"),
        scripts={"where.py": "import os\nprint(os.getcwd())\n"},
    )["A.1"]
    assert result.output.splitlines()[-1] == str((tmp_path / "project").resolve())


def test_lint_runs_only_its_codes_whatever_the_project_selects(tmp_path):
    tree = {"a.py": "import os\n\n\ndef f():\n    try:\n        pass\n    except:\n        pass\n"}
    config = '[tool.ruff.lint]\nselect = ["F"]\n'
    # E722 is selected by the rule, F401 is not; the project's own configuration does not change that.
    result = gate(tmp_path, rule_text("A.1", "lint ruff E722"), {**tree, "pyproject.toml": config})["A.1"]
    assert result.status is Status.FAIL
    assert "E722" in result.output and "F401" not in result.output
    assert "--isolated" in result.output


def test_lint_codes_are_protected_from_suppression(tmp_path):
    tree = {"a.py": "try:\n    pass\nexcept:  # noqa: E722 -- the reason is long enough\n    pass\n"}
    results = gate(tmp_path, rule_text("A.1", "lint ruff E722"), tree)
    assert results["factory:suppressions"].status is Status.FAIL
    assert "E722" in results["factory:suppressions"].output


def test_suppressions_run_only_when_a_must_rule_selects_codes(tmp_path):
    tree = {"a.py": "x = 1  # noqa: E722\n"}
    assert "factory:suppressions" not in gate(tmp_path, rule_text("A.1", "judgement"), tree)
    assert "factory:suppressions" not in gate(tmp_path, rule_text("A.1", "lint ruff E722", "SHOULD"), tree)
    selecting = rule_text("A.1", "command ruff check --select E722 .")
    assert "factory:suppressions" in gate(tmp_path, selecting, tree)


def test_a_bare_tool_is_reported_as_tool_unavailable(tmp_path):
    result = gate(tmp_path, rule_text("A.1", "tool"))["A.1"]
    assert result.status is Status.TOOL_UNAVAILABLE


def test_tool_with_a_command_is_an_alias_of_command(tmp_path):
    for kind in ("tool", "command"):
        result = gate(tmp_path, rule_text("A.1", f"{kind} {sys.executable} -c 'raise SystemExit(0)'"))
        assert result["A.1"].status is Status.PASS


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ("## A.1 MUST\nBody.\n\nWhy: w\n\nCheck: c\n", "exactly one Enforced"),
        (rule_text("A.1", "judgement") + "Enforced: judgement\n", "found 2"),
        (rule_text("A.1", "wish"), "unknown kind"),
        (rule_text("A.1", "check factory:nope"), "unknown check"),
        (rule_text("A.1", "lint eslint X"), "lint needs"),
        (rule_text("A.1", "command"), "command needs"),
        (rule_text("A.1", "script"), "script needs"),
    ],
)
def test_an_invalid_enforced_line_fails_naming_the_rule(tmp_path, body, needle):
    result = gate(tmp_path, body)["A.1"]
    assert result.status is Status.FAIL
    assert needle in result.output


def test_a_duplicate_id_fails_naming_it(tmp_path):
    results = gate(tmp_path, rule_text("A.1", "tool x") + rule_text("A.1", "tool y"))
    assert results["A.1"].status is Status.FAIL
    assert "more than once" in results["A.1"].output


def test_no_standards_is_recorded_as_not_applicable(tmp_path):
    results = gate(tmp_path, "")
    assert results["factory:standards"].status is Status.NOT_APPLICABLE
    assert "no standards configured" in results["factory:standards"].output
    assert set(results) == {"factory:standards"}


def test_a_built_in_check_runs_only_when_a_rule_maps_it(tmp_path):
    tree = {"src/big.py": OVER_THE_LIMIT}
    unmapped = gate(tmp_path, rule_text("A.1", "judgement"), tree)
    assert "factory:module-size" not in unmapped
    mapped = gate(
        tmp_path,
        rule_text("A.1", "check factory:module-size") + rule_text("A.2", "check factory:module-size"),
        tree,
    )
    assert mapped["factory:module-size"].status is Status.FAIL
    assert mapped["factory:module-size"].output.startswith("[A.1, A.2] ")


def test_the_language_summary_names_the_language_pack_rules(tmp_path):
    pack = rule_text("L.1", f"command {sys.executable} -c 'raise SystemExit(1)'")
    standards = tmp_path / "standards"
    (standards / "languages/configs").mkdir(parents=True)
    results = _gates.standards_results(
        tmp_path,
        {"languages/x.md": pack, "rules.md": rule_text("A.1", "check factory:language-summary")},
        configs=standards / "languages/configs",
    )
    summary = next(r for r in results if r.rule == "factory:language-summary")
    assert summary.status is Status.FAIL
    assert summary.output.startswith("[A.1] ") and "L.1 FAIL" in summary.output


def test_required_tools_cover_command_lint_and_script_rules():
    files = {
        "rules.md": rule_text("A.1", "lint ruff E722")
        + rule_text("A.2", "script checks/x.sh")
        + rule_text("A.3", "script checks/x.py")
        + rule_text("A.4", "command mypy . && shellcheck x.sh")
        + rule_text("A.5", "forbid x")
        + rule_text("A.6", "lint ruff E501", "SHOULD")
    }
    assert _gates.required_tools(files) == {
        "ruff": ["A.1"],
        "bash": ["A.2"],
        "mypy": ["A.4"],
        "shellcheck": ["A.4"],
    }


# --- the standards a run is given ---


def test_capture_with_no_standards_directory_is_empty(tmp_path):
    assert _standards.capture(_standards.Standards(), tmp_path) == {}


def test_a_replace_sentence_may_name_ids_with_full_stops(tmp_path):
    demo = (
        "Pattern demos replace ACME.1, ACME.2 and SEC.12.\n\n"
        + rule_text("D.1", "judgement")
        + rule_text("D.2", "judgement", extra="Applies: showcase\n")
    )
    files = {
        "rules.md": rule_text("ACME.1", "judgement") + rule_text("ACME.12", "judgement"),
        "demo.md": demo,
    }
    config = _standards.Standards(include=("rules",))
    pattern = _standards.select(files, config, tmp_path, demo="pattern")
    assert [r.ident for r in _standards.rules({"rules.md": pattern["rules.md"]})] == ["ACME.12"]
    assert "D.1" in pattern["demo.md"] and "D.2" in pattern["demo.md"]
    showcase = _standards.select(files, config, tmp_path, demo="showcase")
    assert "D.2" in showcase["demo.md"] and "D.1" not in showcase["demo.md"]
    assert "ACME.1 " in showcase["rules.md"]


def test_cli_scans_with_the_standards_flag_and_the_default_directory(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src/a.py").write_text("TODO\n")
    rules = rule_text("A.1", "forbid TODO")

    def scan(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "flyte_agent_factory._gates", "standards", str(project), *args],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "HOME": str(tmp_path)},
        )

    none = scan()
    assert none.returncode == 0 and "no standards configured" in none.stdout
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "rules.md").write_text(rules)
    flagged = scan("--standards", str(elsewhere))
    assert flagged.returncode == 1 and "A.1: FAIL" in flagged.stdout
    (project / "standards").mkdir()
    (project / "standards/rules.md").write_text(rules)
    default = scan()
    assert default.returncode == 1 and "A.1: FAIL" in default.stdout
    assert scan("--standards", str(tmp_path / "missing")).returncode == 2


# --- one quote-aware parser for command text ---


def cli(project: Path, standards: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the gate's CLI on `project` with `standards`, as a person (or CI) would."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "flyte_agent_factory._gates",
            "standards",
            str(project),
            "--standards",
            str(standards),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"), "HOME": str(tmp_path)},
    )


def cli_rule(tmp_path: Path, enforced: str) -> subprocess.CompletedProcess[str]:
    project = tmp_path / "project"
    standards = tmp_path / "standards"
    project.mkdir(exist_ok=True)
    standards.mkdir(exist_ok=True)
    (standards / "quality.md").write_text(rule_text("QA-1", enforced))
    return cli(project, standards, tmp_path)


def test_a_quoted_double_ampersand_is_one_argument(tmp_path):
    done = cli_rule(tmp_path, f"""command {sys.executable} -c "print('A && B')" """)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "QA-1: PASS" in done.stdout and "A && B" in done.stdout


def test_unbalanced_quotes_fail_the_rule_without_a_traceback(tmp_path):
    done = cli_rule(tmp_path, 'command "')
    assert done.returncode == 1
    assert "QA-1: FAIL" in done.stdout and "unbalanced quotes" in done.stdout
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("enforced", ["command a && ", "command && a", "command a & b", "tool a && && b"])
def test_an_empty_command_beside_the_separator_fails_the_rule(tmp_path, enforced):
    done = cli_rule(tmp_path, enforced)
    assert done.returncode == 1
    assert "QA-1: FAIL" in done.stdout and "empty command beside &&" in done.stdout


def test_split_commands_is_quote_aware():
    assert _standards.split_commands("a b && c") == [["a", "b"], ["c"]]
    assert _standards.split_commands("python -c 'print(1 && 2)'") == [["python", "-c", "print(1 && 2)"]]
    with pytest.raises(ValueError, match="unbalanced"):
        _standards.split_commands("a 'b")


def test_a_quoted_double_ampersand_does_not_name_a_second_tool():
    files = {"rules.md": rule_text("A.1", 'tool bash -c "x && y"')}
    assert _gates.required_tools(files) == {"bash": ["A.1"]}


# --- capture keeps the files the rules reference ---


def test_capture_keeps_a_root_script_a_rule_names_and_drops_other_root_files(tmp_path):
    (tmp_path / "quality.md").write_text(rule_text("QA-1", "script check.py"))
    (tmp_path / "check.py").write_text("print('CHECK RAN')\n")
    (tmp_path / "notes.txt").write_text("stray\n")
    config = _standards.Standards(dir=str(tmp_path), include=("quality",))
    assert set(_standards.capture(config, tmp_path)) == {"quality.md", "check.py"}


def test_a_root_script_survives_capture_and_materialize(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "quality.md").write_text(rule_text("QA-1", "script check.py"))
    (source / "check.py").write_text("print('CHECK RAN')\n")
    project = tmp_path / "project"
    project.mkdir()
    config = _standards.Standards(dir=str(source), include=("quality",))
    files = _standards.capture(config, tmp_path)
    workspace = tmp_path / "workspace"
    _standards.materialize(workspace, project, config, files)
    done = cli(project, workspace / ".claude-flyte/standards", tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "QA-1: PASS" in done.stdout and "CHECK RAN" in done.stdout


# --- declared evidence commands ---

EVIDENCE = rule_text("EVIDENCE-1", "check factory:ui-evidence")
BARE = rule_text("EVD-001", "tool")
OWN_CHECK = rule_text("EVD-002", "tool true")


def evidence_gate(tmp_path, declared=None, text=None, rules=EVIDENCE, tree=None):
    project = tmp_path / "project"
    shutil.rmtree(project, ignore_errors=True)
    project.mkdir()
    (project / "index.html").write_text("<html></html>\n")
    for name, body in (tree or {}).items():
        (project / name).write_text(body)
    if declared is not None:
        body = "".join(f'"{rule}" = {json.dumps(command)}\n' for rule, command in declared.items())
        (project / "factory-evidence.toml").write_text(text or f"[evidence]\n{body}")
    elif text:
        (project / "factory-evidence.toml").write_text(text)
    (tmp_path / "configs").mkdir(exist_ok=True)
    results = _gates.standards_results(project, {"rules.md": rules}, configs=tmp_path / "configs")
    return {r.rule: r for r in results}


def test_a_declared_ui_evidence_command_that_passes_passes_the_check(tmp_path):
    result = evidence_gate(tmp_path, {"EVIDENCE-1": "python -c 'print(123)'"})["factory:ui-evidence"]
    assert result.status is Status.PASS
    assert "123" in result.output and "EVIDENCE-1" in result.output


def test_a_declared_ui_evidence_command_that_fails_fails_the_check_with_its_output(tmp_path):
    command = "python -c 'print(\"no screenshot\"); raise SystemExit(1)'"
    result = evidence_gate(tmp_path, {"EVIDENCE-1": command})["factory:ui-evidence"]
    assert result.status is Status.FAIL
    assert "no screenshot" in result.output


def test_ui_evidence_with_nothing_declared_says_how_to_declare_it(tmp_path):
    result = evidence_gate(tmp_path)["factory:ui-evidence"]
    assert result.status is Status.TOOL_UNAVAILABLE
    assert "no project command configured for this evidence check" in result.output
    assert "factory-evidence.toml" in result.output and '"<RULE-ID>" = "<command>"' in result.output


def test_a_declaration_for_a_rule_that_is_not_an_evidence_rule_fails(tmp_path):
    results = evidence_gate(tmp_path, {"OTHER-1": "true"})
    assert results["factory:evidence"].status is Status.FAIL
    assert "OTHER-1" in results["factory:evidence"].output
    assert results["factory:ui-evidence"].status is Status.TOOL_UNAVAILABLE


BARE_RULES = BARE + OWN_CHECK


def test_a_bare_tool_rule_with_no_declared_command_is_reported_as_before(tmp_path):
    result = evidence_gate(tmp_path, rules=BARE_RULES)["EVD-001"]
    assert result.status is Status.TOOL_UNAVAILABLE
    assert "no project command configured for this evidence check" in result.output


def test_a_declared_command_passes_a_bare_tool_rule(tmp_path):
    results = evidence_gate(tmp_path, {"EVD-001": "true"}, rules=BARE_RULES)
    assert results["EVD-001"].status is Status.PASS
    assert "factory:evidence" not in results


def test_a_declared_command_that_fails_fails_a_bare_tool_rule_with_its_output(tmp_path):
    result = evidence_gate(tmp_path, {"EVD-001": "sh -c 'echo screenshot missing; exit 3'"}, rules=BARE_RULES)
    assert result["EVD-001"].status is Status.FAIL
    assert "screenshot missing" in result["EVD-001"].output


def test_a_declared_command_runs_in_the_project_dir(tmp_path):
    results = evidence_gate(tmp_path, {"EVD-001": "test -f marker"}, rules=BARE_RULES, tree={"marker": ""})
    assert results["EVD-001"].status is Status.PASS


def test_a_command_for_a_rule_that_has_its_own_check_is_rejected(tmp_path):
    results = evidence_gate(tmp_path, {"EVD-002": "true", "EVD-001": "true"}, rules=BARE_RULES)
    rejected = results["factory:evidence"]
    assert rejected.status is Status.FAIL
    assert "EVD-002" in rejected.output and "not an evidence rule" in rejected.output
    assert "EVD-001" not in rejected.output
    assert results["EVD-001"].status is Status.PASS


def test_a_command_for_an_unknown_rule_is_rejected(tmp_path):
    results = evidence_gate(tmp_path, {"EVD-099": "true"}, rules=BARE_RULES)
    assert results["factory:evidence"].status is Status.FAIL
    assert "EVD-099" in results["factory:evidence"].output


@pytest.mark.parametrize(
    "text",
    ["not toml [", '[evidence]\n"EVD-001" = 3\n', "evidence = 'x'\n"],
    ids=["syntax", "not-a-string", "not-a-table"],
)
def test_a_malformed_evidence_file_fails_clearly(tmp_path, text):
    results = evidence_gate(tmp_path, text=text, rules=BARE_RULES)
    assert results["factory:evidence"].status is Status.FAIL
    assert "factory-evidence.toml" in results["factory:evidence"].output
    assert results["EVD-001"].status is Status.TOOL_UNAVAILABLE


def test_the_gate_tells_a_project_how_to_declare_evidence(tmp_path):
    output = evidence_gate(tmp_path, rules=BARE_RULES)["EVD-001"].output
    assert "factory-evidence.toml" in output and '"EVD-001" = "<command>"' in output
