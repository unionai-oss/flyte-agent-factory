"""The verb-named session roles (#80): the installed set, the aliases, the prompts, the coordinator text."""

import pytest

from flyte_agent_factory import _instructions, _roles, _session_setup

NEW_ROLES = ("scope", "discover", "prototype", "shape", "build", "verify")


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path / "no-image"))
    installed = _session_setup._seed_team(tmp_path)
    return tmp_path / ".claude" / "agents", installed


def test_the_installed_set_has_the_six_roles_and_the_four_aliases(agents):
    folder, installed = agents
    assert set(installed) == {*NEW_ROLES, "product", "designer", "dev", "qa"}
    assert {p.stem for p in folder.glob("*.md")} == set(installed)


@pytest.mark.parametrize(("alias", "role"), sorted(_roles.ROLE_ALIASES.items()))
def test_an_alias_is_its_roles_prompt_with_one_line_naming_the_role(agents, alias, role):
    folder, _ = agents
    new_text = (folder / f"{role}.md").read_text()
    alias_text = (folder / f"{alias}.md").read_text()
    header, _, body = new_text.partition("\n---\n")
    alias_header, _, alias_body = alias_text.partition("\n---\n")
    # Claude Code finds the agent by this name, so the alias carries its own.
    assert f"name: {alias}\n" in alias_header + "\n"
    assert alias_header.replace(f"name: {alias}", f"name: {role}") == header
    assert alias_body == f"Alias of `{role}`; use `{role}`.\n\n" + body


def test_every_role_file_names_what_it_writes_and_reads(agents):
    folder, _ = agents
    expected = {
        "scope": ("ACCEPTANCE.md",),
        "discover": ("DISCOVERY.md", "ask-human", "default"),
        "prototype": ("DISCOVERY.md", "at least two", "screenshots", "harness directory", "throwaway"),
        "shape": ("DESIGN-RATIONALE.md", "DISCOVERY.md", "ACCEPTANCE.md", "prototypes/"),
        "build": ("DESIGN-RATIONALE.md", "DISCOVERY.md", "ACCEPTANCE.md"),
        "verify": ("not independent", "acceptance criteria"),
    }
    for role, phrases in expected.items():
        text = (folder / f"{role}.md").read_text()
        assert text.startswith(f"---\nname: {role}\ndescription: ")
        for phrase in phrases:
            assert phrase in text, (role, phrase)


def test_the_new_prompts_state_their_trigger_in_the_description(agents):
    folder, _ = agents
    discover = (folder / "discover.md").read_text().split("---")[1]
    prototype = (folder / "prototype.md").read_text().split("---")[1]
    assert "problem-brief" in discover and "DISCOVERY.md" in discover
    assert "a person will see or use" in prototype and "at least two" in prototype


def test_a_codex_sub_agent_resolves_an_old_role_name_to_the_new_file(agents):
    assert _roles.canonical_role("qa") == "verify"
    assert (_session_setup._roles_dir() / f"{_roles.canonical_role('qa')}.md").is_file()


def test_the_coordinator_text_lists_the_roles_with_triggers_and_the_problem_brief_order():
    for delegation in (False, True):
        memory = _instructions._project_memory(browser_enabled=False, delegation_available=delegation)
        for role, output in (
            ("scope", "ACCEPTANCE.md"),
            ("discover", "DISCOVERY.md"),
            ("prototype", "prototypes/"),
            ("shape", "DESIGN-RATIONALE.md"),
            ("build", "code and tests"),
            ("verify", "evidence-cited verdicts"),
        ):
            assert f"`{role}` (" in memory and output in memory, role
        assert (
            "`scope`, `discover`, `prototype` (when a person will see or use the result), `shape`, "
            "`build`, `verify`"
        ) in memory
        assert "problem-brief issue" in memory
        assert "`product`, `designer`, `dev` and `qa` still work as aliases" in memory


def test_display_names_for_the_review_loop_and_for_older_reports():
    shown = {
        p: _roles.display_role(p) for p in ("correctness", "completeness", "craft", "planner", "arbiter")
    }
    assert shown == {
        "correctness": "judge-correctness",
        "completeness": "judge-completeness",
        "craft": "judge-craft",
        "planner": "plan",
        "arbiter": "arbitrate",
    }
    assert _roles.display_role("contract") == "contract"
    assert _roles.display_role("judge-craft") == "judge-craft"
    assert _roles.display_role("qa") == "verify"
