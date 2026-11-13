"""The guided-setup docs agree on the seven-step sequence, hand over one command and ask for no secret."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUICKSTART = "docs/quickstart.md"
SKILL_SETUP = "skills/agent-factory/references/setup.md"

#: The owner's own setup, in order. Both the quickstart walk-through and the skill's setup reference use these
#: bold lead-ins, so a person and the chat describe the same step by the same name.
OWNER_STEPS = [
    "**Private workspace.**",
    "**Profile at the workspace root.**",
    "**Write deploy key.**",
    "**Your own GitHub App.**",
    "**Protected main.**",
    "**Board.**",
    "**First deploy.**",
]

#: Pages whose setup text must agree: the profile is `profile.toml` at the workspace root, nothing else.
SETUP_PAGES = [
    "README.md",
    QUICKSTART,
    "docs/setup.md",
    "docs/chat-operator.md",
    "docs/github-app.md",
    "docs/architecture-repos.md",
    "docs/install-audit.md",
    SKILL_SETUP,
    "src/flyte_agent_factory/_assets/workspace_template/README.md",
]


def read(path):
    return (ROOT / path).read_text()


def numbered_steps(text):
    return re.findall(r"^\d+\. (\*\*[^*]+\*\*)", text, re.MULTILINE)


def test_each_of_the_seven_owner_steps_appears_in_order_in_quickstart_and_skill():
    walkthrough = read(QUICKSTART).split("## What /agent-factory walks you through", 1)[1]
    assert numbered_steps(walkthrough.split("## Prerequisites", 1)[0]) == OWNER_STEPS
    skill = read(SKILL_SETUP).split("## What the bootstrap sets up, in the command's order", 1)[1]
    assert numbered_steps(skill.split("\n## ", 1)[0]) == OWNER_STEPS


def test_the_walkthrough_hands_over_one_command_and_the_skill_does_too():
    for path in (QUICKSTART, SKILL_SETUP):
        text = read(path)
        assert "one copy-paste command" in text.lower().replace("**", "") or "ONE copy-paste command" in text
        commands = [
            block
            for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
            if "ops/bootstrap.py --endpoint" in block
        ]
        # The first-run one-liner, and the redeploy and key ones, which are that same line plus a flag.
        keys = ("--chat-key", "--deploy-key")
        first = [c for c in commands if "--redeploy" not in c and not c.strip().endswith(keys)]
        assert len(first) == 1, f"{path}: expected exactly one bootstrap one-liner"
        [command] = first
        for again in commands:
            allowed = [command.strip(), *(f"{command.strip()} {flag}" for flag in ("--redeploy", *keys))]
            assert again.strip() in allowed
        assert command.count("\n") == 1, f"{path}: the one-liner must be a single line"
        for part in ("git clone -q", "uv sync -q --python 3.12", "--project", "--workspace", "--state-uri"):
            assert part in command, f"{path}: one-liner lacks {part}"


def test_no_setup_page_asks_for_a_credential_in_chat():
    asks = re.compile(
        r"(?<!-)\b(?:send|paste|share|type|give (?:me|chat|the operator))\b[^.\n]{0,40}"
        r"(?<!non-)\b(?:token|key|secret|password|pem)\b",
        re.I,
    )
    refusals = re.compile(r"\b(?:never|not|no|without|don't|nor|rather than|instead of)\b", re.I)
    for path in SETUP_PAGES:
        for sentence in re.split(r"(?<=[.;:])\s+", read(path).replace("\n", " ")):
            if asks.search(sentence):
                assert refusals.search(sentence), f"{path}: {sentence.strip()}"
    skill = read(SKILL_SETUP)
    assert "Never ask for a credential" in skill
    assert "never use the MCP `create_secret` for engine credentials" in skill
    assert "It never asks for a credential" in " ".join(read(QUICKSTART).split())


def test_github_app_page_lists_the_permissions_and_what_it_enables():
    page = read("docs/github-app.md")
    for permission in (
        r"Contents \| read",
        r"Metadata \| read",
        r"Pull requests \| write",
        r"Commit statuses \| write",
        r"Checks \| read",
        r"Issues \| write",
    ):
        assert re.search(permission, page), permission
    for enabled in ("CI statuses", "draft pull request", "[deploy] auto", "[direction]", "intake_label"):
        assert enabled in page, enabled
    assert "manifest" in page and "no shared public App" in page
    assert "What works without it" in page and "organization owner" in page
    flat = " ".join(page.split())
    assert (
        "the only GitHub credential setup requires" in flat and "everything the App adds is optional" in flat
    )
    assert "assumes" in flat and "still open" in flat and "#179 decides" in flat
    assert "the only GitHub credential setup requires" in " ".join(read(QUICKSTART).split())
    assert "](github-app.md)" in read("docs/README.md")


def test_board_steps_name_the_link_and_the_default_repository_setting():
    for path in (QUICKSTART, SKILL_SETUP):
        text = read(path)
        assert "Link a project" in text and "Default repository" in text, path
        assert "gh project create" in text or "user-owned board" in text, path
        assert "unrelated" in text and "copies of issues" in text, path
    assert "gh project link" in read(QUICKSTART) and "gh auth refresh -s project" in read(QUICKSTART)


def test_the_board_is_the_standard_backlog_in_every_page_a_person_sees():
    template = "src/flyte_agent_factory/_assets/workspace_template/README.md"
    for path in (QUICKSTART, SKILL_SETUP, "docs/setup.md", template):
        text = " ".join(read(path).split())
        assert "standard backlog" in text and "unrelated projects" in text, path
        assert "copies of issues pulled from tracked repositories" in text, path


def test_chat_operator_documents_both_skills_and_the_drift_check():
    page = read("docs/chat-operator.md")
    assert "`/agent-factory`" in page and "`/since`" in page
    for term in ("Setup.", "Takeover.", "Check-ins.", "operator/STATE.md"):
        assert term in page, term
    assert (
        "ops/package_skill.py --check" in page and "skills/dist/VERSION" in page and "Skill version" in page
    )
    # The skill has two modes (its "Start" section); check-ins are part of operating, not a third mode.
    assert "three modes" not in page
    for mode in ("Set up", "Operate"):
        assert f"**{mode}.**" in read("skills/agent-factory/SKILL.md") and mode in page, mode


def test_factory_handoff_is_mentioned_only_as_retired():
    for path in SETUP_PAGES + ["docs/README.md", "docs/operating.md", "docs/factory-mcp-setup.md"]:
        for number, line in enumerate(read(path).splitlines(), 1):
            if "factory-handoff" in line:
                assert "retired" in line, f"{path}:{number}: {line.strip()}"


def test_profile_toml_is_the_only_workspace_profile_name():
    retired = re.compile(
        r"profiles/mine\.toml|--profile mine\b|AGENT_FACTORY_PROFILE=mine\b|"
        r"package_skill\.py --(?:setup|profile)"
    )
    for path in SETUP_PAGES:
        for number, line in enumerate(read(path).splitlines(), 1):
            assert not retired.search(line), f"{path}:{number}: {line.strip()}"
    assert "`profile.toml`" in read(QUICKSTART) and 'profile = "profile.toml"' in read(QUICKSTART)
    assert "ops/new_workspace.py" in read(QUICKSTART)


def test_the_docs_say_the_template_repository_does_not_exist_yet():
    text = read(QUICKSTART)
    assert "unionai-oss/flyte-agent-workspace" in text and "does not exist yet" in text
    assert "bundled" in text


def test_the_documented_key_commands_are_the_ones_the_bootstrap_prints():
    from flyte_agent_factory._bootstrap_base import Settings
    from flyte_agent_factory._bootstrap_keys import build_command

    example = Settings(
        endpoint="flyte.example.com",
        project="my-agents",
        workspace="you/agent-workspace",
        state_uri="s3://my-agent-state/factory/mine",
    )
    docs = {
        "--chat-key": (SKILL_SETUP, "docs/chat-operator.md", "docs/factory-mcp-setup.md"),
        "--deploy-key": (SKILL_SETUP, QUICKSTART),
    }
    for flag, paths in docs.items():
        for path in paths:
            assert f"```bash\n{build_command(example, flag)}\n```" in read(path), f"{path}: {flag}"
    # The key modes share the first-run line's clone-and-sync prefix, so they cannot drift from it.
    first_run = next(
        line
        for line in read(SKILL_SETUP).splitlines()
        if line.startswith("d=~/") and "--redeploy" not in line
    )
    assert build_command(example) == first_run


def test_every_setup_page_says_the_deploy_key_is_the_only_required_github_credential():
    for path in (QUICKSTART, "docs/setup.md", "docs/github-app.md"):
        assert "only GitHub credential" in read(path), path
        assert "](github-app.md)" in read(path) or path == "docs/github-app.md", path
