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
