"""The template's human-facing standard, run through the gate as a DONE would run it.

Rules are found by the script they run, never by ID: the engine names none of the owner's rule IDs.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gates, _standards
from tests._standards import FILES, RULES, TEMPLATE

HUMAN_FACING = [
    name for name in FILES if name.endswith(".md") and _standards._is_human_facing(name, FILES[name])
]
REGISTER = TEMPLATE.parent / "docs/ux-sources.md"
Status = _gate_base.Status

#: Dense prose no seven-year-old reads, which a children's prototype might still ship.
HARD_TEXT = (
    "Notwithstanding the aforementioned considerations, participants must acknowledge that "
    "environmental configuration parameters, including topographical characteristics and atmospheric "
    "conditions, substantially determine subsequent navigational opportunities. "
) * 8


def rules_for(script: str) -> list[str]:
    """The IDs of the template rules that run `checks/<script>`."""
    return [r.ident for r in RULES if r.kind == "script" and r.arg.split()[0] == f"checks/{script}"]


def rule_for(script: str) -> str:
    """The ID of the one template rule that runs `checks/<script>`."""
    [ident] = rules_for(script)
    return ident


@pytest.fixture
def standards(tmp_path: Path) -> Path:
    """Only the human-facing files and their scripts, so the scan does not also run the Python pack."""
    directory = tmp_path / "standards"
    directory.mkdir()
    for name in HUMAN_FACING:
        (directory / name).write_text(FILES[name])
    shutil.copytree(TEMPLATE / "checks", directory / "checks")
    return directory


def scan(project: Path, standards: Path) -> dict[str, _gates.Result]:
    return {r.rule: r for r in _gates.standards_scan(project, standards)}


def kids_prototype(root: Path, *, product: bool = True, text: str = HARD_TEXT) -> Path:
    project = root / "prototype"
    project.mkdir()
    (project / "index.html").write_text(
        f'<!doctype html><html lang="en"><head><title>World maker</title></head><body><main><p>{text}</p>'
        "</main></body></html>"
    )
    if product:
        (project / "product.toml").write_text(
            "[audience]\n"
            'users = "Children learning to read"\n'
            'age_band = "6-8"\n'
            'expertise = "novice"\n'
            "reading_grade = 2\n"
            'main_task = "Make a world and walk into its first room"\n'
            "[surfaces]\n"
            'pages = ["index.html"]\n'
        )
    return project


def test_the_template_has_a_human_facing_standard_with_scripted_rules():
    assert HUMAN_FACING, "no standards file is marked human-facing"
    for script in ("ux_audience.py", "ux_reading_level.py", "ux_walkthrough.py", "ux_render.py", "ux_cli.py"):
        idents = rules_for(script)
        assert idents
        assert all(r.must for r in RULES if r.ident in idents)


def test_a_childrens_prototype_with_hard_text_and_no_walkthrough_fails(tmp_path, standards):
    results = scan(kids_prototype(tmp_path), standards)
    assert results[rule_for("ux_audience.py")].status is Status.PASS
    reading = results[rule_for("ux_reading_level.py")]
    assert reading.status is Status.FAIL
    assert "index.html" in reading.output
    walkthrough = results[rule_for("ux_walkthrough.py")]
    assert walkthrough.status is Status.FAIL
    assert "WALKTHROUGH.md" in walkthrough.output


def test_a_declared_cli_that_accepts_bad_input_fails_and_one_that_rejects_it_passes(tmp_path, standards):
    project = tmp_path / "tool"
    project.mkdir()
    tool = project / "tool.py"
    tool.write_text(
        "import sys\n"
        "if sys.argv[1:] == ['--help']:\n    print('usage: tool')\n"
        "elif sys.argv[1:] == ['--version']:\n    print('tool 1')\n"
        "elif sys.argv[1:] == ['--bad']:\n"
        "    print('tool: unknown option --bad; try --help', file=sys.stderr)\n"
        "    sys.exit(2)\n"
    )
    (project / "product.toml").write_text(
        "[audience]\n"
        'users = "Operators"\nage_band = "adult"\nexpertise = "expert"\nreading_grade = 12\n'
        'main_task = "Run the tool"\n'
        "[surfaces]\n"
        f'cli = "{sys.executable} tool.py"\n'
        'cli_invalid = ["--bad"]\n'
        'cli_main = ["--version"]\n'
    )
    assert scan(project, standards)[rule_for("ux_cli.py")].status is Status.PASS
    tool.write_text("print('usage: tool')\n")  # accepts every argument, --version and --bad included
    result = scan(project, standards)[rule_for("ux_cli.py")]
    assert result.status is Status.FAIL
    assert "--bad" in result.output


def test_a_prototype_that_does_not_name_its_users_fails(tmp_path, standards):
    result = scan(kids_prototype(tmp_path, product=False), standards)[rule_for("ux_audience.py")]
    assert result.status is Status.FAIL
    assert "product.toml" in result.output
    for field in ("users", "age_band", "expertise", "reading_grade", "main_task"):
        assert f"audience.{field}: missing" in result.output, field


def test_work_with_no_human_facing_surface_is_not_applicable(tmp_path, standards):
    project = tmp_path / "library"
    project.mkdir()
    (project / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    results = scan(project, standards)
    scripted = [r.ident for r in RULES if r.file in HUMAN_FACING and r.kind == "script"]
    assert scripted
    for ident in scripted:
        assert results[ident].status is Status.NOT_APPLICABLE, (ident, results[ident].output)


def test_every_source_a_rule_names_is_in_the_register():
    register = REGISTER.read_text()
    keys = set(re.findall(r"^\| `([a-z0-9.-]+)` \|", register, re.M))
    for name in HUMAN_FACING:
        for line in re.findall(r"^Sources:(.*(?:\n(?!\n).*)*)", FILES[name], re.M):
            for key in re.split(r"[,\s]+", line.strip()):
                assert key in keys, (name, key)


def test_the_ux_standard_and_its_guide_grant_no_waiver():
    guide = Path(__file__).resolve().parents[1] / "docs/ux-standard.md"
    for path in (TEMPLATE / "ux.md", guide):
        assert not re.search(r"waiver|skip this check", path.read_text(), re.I), path
