"""The promotion docs match `ops/promote.py` and no longer describe the retired paths."""

import argparse
import re
from pathlib import Path

import pytest

from ops import promote

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_README = ROOT / "src/flyte_agent_factory/_assets/workspace_template/README.md"
SKILL = ROOT / "skills/agent-factory/SKILL.md"
ARCHITECTURE_REPOS = ROOT / "docs/architecture-repos.md"
PROMOTION_LINK = "architecture-repos.md#promotion"

# quickstart.md, setup.md and the skill's references belong to guided setup: only the engine special cases
# (GUIDED_SETUP_RETIRED) are checked there, not the whole RETIRED list.
GUIDED_SETUP = [
    ROOT / "docs/quickstart.md",
    ROOT / "docs/setup.md",
    *sorted((ROOT / "skills/agent-factory/references").glob("*.md")),
]
IN_SCOPE = [
    ROOT / "README.md",
    *sorted(p for p in (ROOT / "docs").glob("*.md") if p not in GUIDED_SETUP),
    SKILL,
    TEMPLATE_README,
    ROOT / "profiles/README.md",
]
# Every file that may mention `ops/promote.py`.
PROMOTE_MENTIONS = [
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    *sorted((ROOT / "skills/agent-factory").rglob("*.md")),
    TEMPLATE_README,
]

RETIRED = [
    "factory_projects",
    "engine_busy",
    "patches/*.patch",
    "git format-patch",
    "git am",
    "factory:target-patches",
    "public_tree.py dry-run",
    "engine lane",
    "factory lane",
    "slot with the factory repo",
    # Engine special cases #168 removed: gardening launched on the engine, board references and the App
    # installation tied to it, the engine as the default gardening target.
    "launches the project on the `[repo] factory`",
    "count only for the factory repository",
    "both the engine and direction repositories",
    "`[repo] factory` repository by default",
    # Spelled in two parts so `git grep` for the retired variable finds no use of it, this list included.
    "AGENT_FACTORY_SLOW_LANE" + "_PYTHON",
]


# The engine special cases and the pending App-free deployer, which no longer exist.
GUIDED_SETUP_RETIRED = [
    "factory_projects",
    "engine_busy",
    "launch an ordinary project on the allowed factory repo",
    "launched on a slot with the factory repo",
    "slot with the factory repo",
    "engine is the only gardening target",
    "App-free deployer",
]


def section(path, heading, level):
    """The text under `heading` up to the next heading of the same or a higher level."""
    text = path.read_text()
    own = f"{'#' * level} {re.escape(heading)}"
    match = re.search(rf"^{own}\n(.*?)(?=^#{{1,{level}}} |\Z)", text, re.M | re.S)
    assert match, f"{path.name} has no '{heading}' section"
    return match.group(1)


def cli():
    """{subcommand: its option strings}, read from the real parser."""
    parser = promote.build_parser()
    (subparsers,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return {
        name: {opt for action in sub._actions for opt in action.option_strings}
        for name, sub in subparsers.choices.items()
    }


@pytest.mark.parametrize("path", IN_SCOPE, ids=lambda p: p.name)
def test_retired_terms_are_gone(path):
    text = path.read_text()
    assert [term for term in RETIRED if term in text] == []


@pytest.mark.parametrize("path", GUIDED_SETUP, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_guided_setup_has_no_engine_special_cases(path):
    text = path.read_text()
    assert [term for term in GUIDED_SETUP_RETIRED if term in text] == []


def test_promotion_section_uses_only_real_subcommands_and_flags():
    commands = cli()
    body = section(ARCHITECTURE_REPOS, "Promotion", 2)
    used = re.findall(r"promote\.py (\w+)", body)
    assert set(used) == set(commands), "the section should show every subcommand"
    for sub in used:
        assert sub in commands
    # Checked against both subcommands' options together: the section lists them in prose, not per command.
    known = set().union(*commands.values())
    flags = set(re.findall(r"`?(--[a-z][a-z-]*)", body))
    assert flags, "the section should name the flags"
    assert flags <= known, f"not in promote.py: {sorted(flags - known)}"


@pytest.mark.parametrize("path", PROMOTE_MENTIONS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_promote_mention_uses_a_real_subcommand_and_flags(path):
    commands = cli()
    known = set().union(*commands.values())
    # A mention runs from `promote.py` to the closing backtick, a blank line or the end of the code block.
    for sub, rest in re.findall(r"promote\.py (\w+)([^`]*?)(?=`|\n\n|\Z)", path.read_text(), re.S):
        assert sub in commands, f"{path.name}: promote.py {sub} is not a subcommand"
        flags = set(re.findall(r"(--[a-z][a-z-]*)", rest))
        assert flags <= known, f"{path.name}: not in promote.py: {sorted(flags - known)}"


@pytest.mark.parametrize(
    "path",
    [
        ROOT / "README.md",
        ROOT / "docs/architecture.md",
        ROOT / "docs/chat-operator.md",
        SKILL,
        TEMPLATE_README,
    ],
    ids=lambda p: p.name,
)
def test_promotion_is_linked_not_restated(path):
    assert PROMOTION_LINK in path.read_text()


def test_cutover_section_3_installs_nothing_on_the_public_repo():
    body = section(ROOT / "docs/public-cutover.md", "3. Point the factory at it", 2)
    lowered = body.lower()
    forbidden = (
        "install the app",
        "install it on",
        "add a deploy key",
        "new deploy key",
        "add the app's webhook",
    )
    for phrase in forbidden:
        assert phrase not in lowered
    assert "no app, deploy key or webhook" in lowered
    assert "[deploy] repo" in body and "webhook = false" in body


def mermaid_blocks(path):
    return re.findall(r"```mermaid\n.*?```", path.read_text(), re.S)


def test_readme_and_architecture_share_one_diagram():
    readme, architecture = (mermaid_blocks(ROOT / p) for p in ("README.md", "docs/architecture.md"))
    assert len(readme) == 1 and len(architecture) == 1
    assert readme == architecture
    diagram = readme[0]
    assert "workspace" in diagram.lower()
    assert "ops/promote.py deliver" in diagram
    assert "[deploy] repo" in diagram and "deployer" in diagram.lower()


@pytest.mark.parametrize("path", IN_SCOPE, ids=lambda p: p.name)
def test_no_thirty_minute_deploy_poll(path):
    # `_deploy.reconcile_minutes` is 60 with `webhook_secret` and `[deploy] webhook`, else 5; never 30.
    assert not re.search(r"(every|from 5 to|to) 30 minutes", path.read_text())


def test_skill_does_not_launch_engine_changes_on_the_factory_repo():
    text = SKILL.read_text()
    assert "launch an ordinary project on the allowed factory repo" not in text
    assert "ops/promote.py import" in section(SKILL, "Factory changes and deployment", 2)


def test_skill_promotion_closes_the_loop_on_the_public_issue():
    # Owner, 2026-11-10: the operator links the workspace item to the public issue, writes "Fixes" into the PR
    # body, and after the merge confirms the issue closed and marks the item Done with the merged PR's link.
    body = section(SKILL, "Factory changes and deployment", 2)
    assert "--fixes" in body and "Fixes <owner>/<repo>#N" in body
    assert "closed" in body and "Done" in body and "merged pull request" in body
