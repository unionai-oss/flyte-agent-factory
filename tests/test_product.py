"""`product.toml` (audience and brand) and the human-facing standards files: docs/standards.md."""

from __future__ import annotations

import importlib.util
import tomllib
from typing import Any

import pytest

from flyte_agent_factory import _contract, _instructions, _product, _review, _session_config, _standards
from tests._standards import FILES, TEMPLATE

AUDIENCE = """\
[audience]
users = "Children learning to read, and the parent who sets the game up"
age_band = "6-8"
expertise = "novice"
reading_grade = 2
main_task = "Create a world and walk into its first room"
"""


def judged(ident: str, text: str = "Be warm.") -> str:
    return f"## {ident} MUST\n{text}\n\nWhy: w\n\nCheck: look\n\nEnforced: judgement\n\n"


WORKSPACE_BRAND = "# Workspace brand\n\n" + judged("WB.1", "Workspace voice.")
BASE = {"engineering.md": FILES["engineering.md"], "brand.md": WORKSPACE_BRAND}
CONFIG = _standards.Standards(include=("engineering", "ux"))


def selected(project, files=None) -> dict[str, str]:
    return _standards.select(files or BASE, CONFIG, project, fetch=False)


@pytest.fixture
def project(tmp_path, monkeypatch):
    path = tmp_path / "app"
    path.mkdir()
    monkeypatch.setattr(_session_config, "_project_dir", lambda: path)
    monkeypatch.setattr(_session_config, "_captured_standards", lambda: BASE)
    monkeypatch.setattr(_session_config, "_applicable_standards", lambda: selected(path))
    return path


# --- parsing ---


def test_a_valid_audience_parses():
    product = _product.parse(AUDIENCE + '[brand]\nfile = "none"\n[surfaces]\npages = ["a.html"]\n')
    assert product.problems == ()
    assert product.audience == _product.Audience(
        "Children learning to read, and the parent who sets the game up",
        "6-8",
        "novice",
        2,
        "Create a world and walk into its first room",
        True,
    )
    assert product.brand_file == "none"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("not = [valid", "not valid TOML"),
        ("", "[audience] is missing"),
        (
            AUDIENCE.replace(
                'users = "Children learning to read, and the parent who sets the game up"\n', ""
            ),
            "users is missing",
        ),
        (AUDIENCE.replace('"novice"', '"beginner"'), "expertise must be one of"),
        (AUDIENCE.replace("reading_grade = 2", "reading_grade = 19"), "reading_grade must be an integer"),
        (AUDIENCE.replace("reading_grade = 2", "reading_grade = true"), "reading_grade must be an integer"),
        (AUDIENCE.replace("reading_grade = 2", 'reading_grade = "2"'), "reading_grade must be an integer"),
        (AUDIENCE + "[brand]\nfile = 3\n", "[brand] file must be"),
    ],
)
def test_an_invalid_product_file_is_reported_not_raised(text, problem):
    product = _product.parse(text)
    assert any(problem in p for p in product.problems), product.problems
    assert product.audience is None or "[brand]" in problem


def test_an_unknown_audience_key_is_reported_but_keeps_the_audience():
    product = _product.parse(AUDIENCE + "reading_level = 2\n")
    assert product.audience is not None and product.audience.reading_grade == 2
    assert any("unknown key ignored: reading_level" in p for p in product.problems)


# --- brand ---


def test_without_product_toml_the_workspace_brand_is_kept(project):
    assert selected(project)["brand.md"] == WORKSPACE_BRAND


def test_a_product_without_a_brand_table_keeps_the_workspace_brand(project):
    (project / "product.toml").write_text(AUDIENCE)
    assert selected(project)["brand.md"] == WORKSPACE_BRAND


def test_a_project_brand_file_replaces_the_workspace_brand_for_this_project_only(project):
    (project / "BRAND.md").write_text("# Mine\n\n" + judged("P.1", "Gentle."))
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    assert "Gentle." in selected(project)["brand.md"]
    assert "Workspace voice." not in selected(project)["brand.md"]
    assert BASE["brand.md"] == WORKSPACE_BRAND


def test_a_project_brand_file_can_be_added_where_the_workspace_has_none(project):
    (project / "BRAND.md").write_text("# Mine\n")
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    assert selected(project, {"engineering.md": BASE["engineering.md"]})["brand.md"] == "# Mine\n"


def test_none_removes_the_brand(project):
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "none"\n')
    assert "brand.md" not in selected(project)


def test_a_brand_file_of_tokens_without_rules_is_passed_as_text(project):
    (project / "BRAND.md").write_text("# Tokens\n\nAccent: #ff0066\n")
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    assert selected(project)["brand.md"] == "# Tokens\n\nAccent: #ff0066\n"
    assert _product.find_problems(project) == []


@pytest.mark.parametrize(
    "value", ["../outside.md", "/etc/hostname", "sub/../../outside.md", "missing.md", "sub"]
)
def test_a_brand_path_outside_the_project_or_not_a_file_is_reported_and_ignored(project, value):
    (project / "sub").mkdir()
    (project.parent / "outside.md").write_text(judged("O.1", "Escaped."))
    (project / "product.toml").write_text(AUDIENCE + f'[brand]\nfile = "{value}"\n')
    assert selected(project)["brand.md"] == WORKSPACE_BRAND
    assert any("workspace brand is kept" in p for p in _product.find_problems(project))


def test_a_symlink_out_of_the_project_is_ignored(project):
    (project.parent / "outside.md").write_text(judged("O.1", "Escaped."))
    (project / "BRAND.md").symlink_to(project.parent / "outside.md")
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    assert selected(project)["brand.md"] == WORKSPACE_BRAND


def test_only_judgement_rules_are_taken_from_a_project_brand_file(project):
    mechanical = "## P.2 MUST\nNo.\n\nWhy: w\n\nCheck: run\n\nEnforced: command touch pwned\n\n"
    (project / "BRAND.md").write_text("# Mine\n\n" + judged("P.1", "Gentle.") + mechanical)
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    brand = selected(project)["brand.md"]
    assert "P.1" in brand and "P.2" not in brand
    assert any("P.2 dropped" in p for p in _product.find_problems(project))
    assert [r.kind for r in _standards.rules({"brand.md": brand})] == ["judgement"]


def test_a_project_brand_rule_may_not_reuse_a_workspace_rule_id(project):
    taken = _standards.rules({"engineering.md": FILES["engineering.md"]})[0].ident
    (project / "BRAND.md").write_text("# Mine\n\n" + judged("P.1", "Gentle.") + judged(taken, "Clash."))
    (project / "product.toml").write_text(AUDIENCE + '[brand]\nfile = "BRAND.md"\n')
    files = selected(project)
    assert "Clash." not in files["brand.md"] and "P.1" in files["brand.md"]
    assert len([r for r in _standards.rules(files) if r.ident == taken]) == 1
    assert any(f"{taken} dropped" in p for p in _product.find_problems(project, [taken]))


# --- audience in the prompts ---


def test_nothing_is_added_without_product_toml(project):
    assert _session_config._describe_product_users() == ""
    assert "This product's users" not in _review._qa_prompt("correctness", "x", "brief", "", 1)


def test_the_audience_is_in_the_session_contract_and_every_review_prompt(project):
    (project / "product.toml").write_text(AUDIENCE)
    block = _session_config._describe_product_users()
    for needle in (
        "Children learning to read",
        "6-8",
        "novice",
        "grade 2",
        "first room",
        "overrides any audience",
    ):
        assert needle in block
    assert block in _contract._contract_prompt("brief", "")
    assert block in _instructions._project_memory(False)
    for judge in ("correctness", "completeness", "craft"):
        assert block in _review._qa_prompt(judge, "x", "brief", "", 1)


def test_an_invalid_product_file_says_what_is_missing(project):
    (project / "product.toml").write_text(AUDIENCE.replace('age_band = "6-8"\n', ""))
    block = _session_config._describe_product_users()
    assert "age_band is missing" in block
    assert "Children learning to read" not in block
    assert block in _review._qa_prompt("correctness", "x", "brief", "", 1)


# --- human-facing files ---

UX = "# UX\n\napplies: human-facing\n\n" + judged("UX.1", "Readable.")


def test_a_file_can_declare_itself_human_facing():
    files = {"ux.md": UX, "engineering.md": judged("E.1"), "brand.md": WORKSPACE_BRAND}
    flags = {r.file: r.human_facing for r in _standards.rules(files)}
    assert flags == {"ux.md": True, "engineering.md": False, "brand.md": True}
    assert all(r.brand for r in _standards.rules(files) if r.file == "brand.md")


def test_the_key_is_case_insensitive_and_only_read_before_the_first_rule():
    assert _standards.rules({"a.md": "Applies: Human-Facing\n\n" + judged("A.1")})[0].human_facing
    late = judged("A.1", "Text.\napplies: human-facing")
    assert not _standards.rules({"a.md": late})[0].human_facing


def test_the_applies_line_is_not_rule_text():
    (rule,) = _standards.rules({"ux.md": UX})
    assert "applies" not in rule.body and not rule.error


def test_the_human_facing_sentence_names_the_selected_files():
    text = _standards.describe_human_facing_files(
        {"ux.md": UX, "brand.md": WORKSPACE_BRAND, "e.md": judged("E.1")}
    )
    assert text.startswith("Rules from ux.md and brand.md apply only to human-facing deliverables")
    assert "screenshots at the stated viewports" in text
    assert _standards.describe_human_facing_files({"e.md": judged("E.1")}) == ""
    assert _standards.describe_human_facing_files({"brand.md": "# tokens only\n"}) == ""


def test_the_sentence_reaches_the_review_contract_and_session_prompts(project, monkeypatch):
    # No profile in the test, so the default `include` applies: the human-facing file is one of those.
    files = {"engineering.md": UX, "brand.md": WORKSPACE_BRAND}
    monkeypatch.setattr(_session_config, "_captured_standards", lambda: files)
    monkeypatch.setattr(_session_config, "_applicable_standards", lambda: selected(project, files))
    sentence = "Rules from engineering.md and brand.md apply only to human-facing deliverables"
    assert sentence in _contract._contract_prompt("brief", "")
    assert sentence in _review._qa_prompt("craft", "x", "brief", "", 1)
    assert sentence in _instructions._project_memory(False)
    assert "human-facing deliverables" not in _review._qa_prompt("correctness", "x", "brief", "", 1)


def test_no_human_facing_file_means_no_sentence(project, monkeypatch):
    files = {"engineering.md": BASE["engineering.md"]}
    monkeypatch.setattr(_session_config, "_captured_standards", lambda: files)
    monkeypatch.setattr(_session_config, "_applicable_standards", lambda: selected(project, files))
    assert "human-facing" not in _contract._contract_prompt("brief", "")
    assert "human-facing" not in _review._qa_prompt("craft", "x", "brief", "", 1)
    assert _session_config._describe_human_facing_files() == ""


def load_ux_common() -> Any:
    checks = TEMPLATE / "checks"
    spec = importlib.util.spec_from_file_location("_ux_common", checks / "_ux_common.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


AUDIENCE_FIELDS = {
    "users": '"Children learning to read"',
    "age_band": '"6-8"',
    "expertise": '"novice"',
    "reading_grade": "2",
    "main_task": '"Read the story"',
}


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"expertise": '" novice "'},
        {"users": '"  Teachers  "'},
        {"expertise": '"beginner"'},
        {"expertise": "3"},
        {"expertise": '""'},
        {"reading_grade": "19"},
        {"reading_grade": "true"},
        {"reading_grade": "2.5"},
        {"users": None},
        {"main_task": '"   "'},
        {"age_band": '"12-14"'},
        {"age_band": '"13-17"'},
        {"age_band": '"13+"'},
        {"age_band": '"65+"'},
        {"age_band": '"adults"'},
        {"age_band": '"8-12, 65+"'},
        {"age_band": '"8-6"'},
        {"age_band": '"teenagers and parents"'},
        {"age_band": '"teenagers and parents"', "under_13": "true"},
        {"age_band": '"teenagers and parents"', "under_13": "false"},
        {"age_band": '"6-8"', "under_13": "true"},
        {"age_band": '"6-8"', "under_13": "false"},
        {"age_band": '"13-17"', "under_13": "true"},
        {"age_band": '"adult"', "under_13": "false"},
        {"under_13": '"yes"'},
        {"under_13": "1"},
    ],
)
def test_the_engine_and_the_audience_script_agree_on_the_audience(change):
    fields = {**AUDIENCE_FIELDS, **change}
    text = "[audience]\n" + "".join(f"{k} = {v}\n" for k, v in fields.items() if v is not None)
    engine_valid = _product.parse(text).audience is not None
    script_valid = load_ux_common().find_audience_problems(tomllib.loads(text)) == []
    assert engine_valid == script_valid, (change, _product.parse(text).problems)


@pytest.mark.parametrize(
    ("age_band", "under_13", "expected"),
    [
        ("6-8", "", True),
        ("12-14", "", True),
        ("13-17", "", False),
        ("13+", "", False),
        ("adult", "", False),
        ("teenagers and parents", "under_13 = true\n", True),
        ("teenagers and parents", "under_13 = false\n", False),
    ],
)
def test_the_engine_reads_whether_children_under_13_are_included(age_band, under_13, expected):
    text = AUDIENCE.replace('age_band = "6-8"\n', f'age_band = "{age_band}"\n{under_13}')
    audience = _product.parse(text).audience
    assert audience is not None and audience.under_13 is expected


def test_the_audience_block_says_when_children_under_13_are_included(project):
    (project / "product.toml").write_text(AUDIENCE)
    assert "Includes children under 13: yes" in _product.format_audience_block(project)
    (project / "product.toml").write_text(AUDIENCE.replace('"6-8"', '"adult"'))
    assert "Includes children under 13: no" in _product.format_audience_block(project)
