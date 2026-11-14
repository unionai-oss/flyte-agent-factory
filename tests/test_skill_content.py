import re
import zipfile

from ops import package_skill


def _packaged(name):
    with zipfile.ZipFile(package_skill.DIST / f"{name}.zip") as archive:
        return archive.read(f"{name}/SKILL.md").decode()


def test_agent_factory_skill_has_the_required_sections_in_order():
    skill = _packaged("agent-factory")
    headings = re.findall(r"^## (.+)$", skill, re.MULTILINE)
    required = [
        "Start: setup or take over",
        "Taking over",
        "Operator state",
        "Where the factory's values come from",
        "Engineering judgement versus owner decisions",
        "Opening pull requests for factory branches",
        "Profile changes and deploys",
        "Stop",
        "Related commands",
    ]
    positions = [headings.index(title) for title in required]
    assert positions[:3] == sorted(positions[:3]) and positions[:3][0] < positions[3]
    assert "/since" in skill
    assert "references/setup.md" in skill
    assert "operator/STATE.md" in skill


def test_handoff_command_is_mentioned_only_as_retired():
    for name in ("agent-factory", "since"):
        for line in _packaged(name).splitlines():
            if "factory-handoff" in line:
                assert "retired" in line


def test_retired_task_names_are_not_named_as_current():
    for name in ("agent-factory", "since"):
        text = _packaged(name)
        assert "sync_board" not in text and "drain_outbox" not in text


def test_since_skill_reads_only_what_the_factory_mcp_gives():
    skill = _packaged("since")
    assert "factory.queue_list" in skill and "factory.status" in skill
    assert "operator/STATE.md" in skill
    assert "—" not in skill


def _packaged_file(name, relative):
    with zipfile.ZipFile(package_skill.DIST / f"{name}.zip") as archive:
        return archive.read(f"{name}/{relative}").decode()


def _flat(text):
    # Matched on words, not line breaks, so re-wrapping a paragraph does not break the test.
    return " ".join(text.split())


def test_operator_carries_out_what_the_person_asks_with_its_own_tools():
    # Owner: "The factory operator behavior I want is to be able to tell you and you merge this in."
    skill = _packaged("agent-factory")
    start = skill.index("### Do what the person asks, with your own tools")
    rule = _flat(skill[start : skill.index("\n## ", start)])
    assert all(word in rule for word in ("merge", "sync", "promote", "deliver"))
    assert "carry it out yourself" in rule
    assert "do not ask them to confirm the action they just asked for" in rule
    assert "GitHub connector" in rule and "`ops/promote.py`" in rule and "factory MCP" in rule
    assert "only when no tool you hold can reach the target" in rule
    assert "say which tool or access is missing" in rule
    assert "extra spend, publishing or a credential still needs their explicit word" in rule
    setup = _flat(_packaged_file("agent-factory", "references/setup.md"))
    assert (
        "run the `--redeploy` command below once yourself where your shell holds their deploy credentials"
        in setup
    )
    assert "give the `--redeploy` command below" not in setup and "give this one command" not in setup


def test_engine_work_runs_in_the_workspace_and_launch_name_matches_its_folder():
    skill = _packaged("agent-factory")
    start = skill.index("## Factory changes and deployment")
    body = _flat(skill[start : skill.index("\n## ", start + 1)])
    assert "never launches a project against the engine repository and never pushes to it" in body
    assert "The only write is your `deliver` (step 4), on the person's word" in body
    assert "ops/promote.py import <engine-url> <workspace checkout>/projects/<name>" in body
    assert "ops/promote.py deliver <workspace checkout>/projects/<name>" in body
    assert "**The launch name must equal the engine folder's name.**" in body
    assert "judged against an empty folder" in body
    assert "#224" in body and "its own engine folder named after its launch" in body
