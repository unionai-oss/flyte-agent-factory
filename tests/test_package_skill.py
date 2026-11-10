import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlparse

import pytest
import yaml

from flyte_agent_factory import _profile
from ops import package_skill


def test_package_renders_profile_values_and_standard_skill_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(package_skill, "OUTPUT", tmp_path / "agent-factory.zip")
    assert package_skill.main(["--profile", "example"]) == 0
    profile = _profile.load("example")
    with zipfile.ZipFile(package_skill.OUTPUT) as archive:
        assert archive.namelist() == ["agent-factory/SKILL.md", "agent-factory/references/setup.md"]
        skill = archive.read("agent-factory/SKILL.md").decode()
    assert "name: agent-factory" in skill
    assert "{{" not in skill and "}}" not in skill
    assert profile.backend.project in skill and profile.backend.domain in skill
    assert profile.repo in skill
    for task in profile.tasks:
        assert task in skill


@pytest.mark.parametrize(
    "mode", [["--setup"], ["--profile", "example"], ["--profile", "example-claude-personal"]]
)
def test_skill_frontmatter_is_valid_yaml_with_name_and_description(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(package_skill, "OUTPUT", tmp_path / "agent-factory.zip")
    package_skill.main(mode)
    with zipfile.ZipFile(package_skill.OUTPUT) as archive:
        skill = archive.read("agent-factory/SKILL.md").decode()
    _, frontmatter, _ = skill.split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == "agent-factory"
    assert isinstance(meta["description"], str) and meta["description"]


def test_render_rejects_unknown_placeholders():
    with pytest.raises(ValueError, match="unknown placeholder"):
        package_skill.render_skill("{{unknown}}", _profile.load("example"))


def test_setup_reference_is_available_in_the_installed_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(package_skill, "OUTPUT", tmp_path / "agent-factory.zip")
    package_skill.main(["--profile", "example-claude-personal"])
    with zipfile.ZipFile(package_skill.OUTPUT) as archive:
        skill = archive.read("agent-factory/SKILL.md").decode()
        assert "references/setup.md" in skill
        guide = archive.read("agent-factory/references/setup.md").decode()
    assert guide == (package_skill.SOURCE.parent / "references/setup.md").read_text()


def test_setup_package_needs_no_profile_and_cannot_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(package_skill, "OUTPUT", tmp_path / "agent-factory.zip")

    def reject_profile(*args, **kwargs):
        pytest.fail("setup packaging must not load a deployment profile")

    monkeypatch.setattr(_profile, "load", reject_profile)
    assert package_skill.main(["--setup"]) == 0
    with zipfile.ZipFile(package_skill.OUTPUT) as archive:
        skill = archive.read("agent-factory/SKILL.md").decode()
        assert archive.read("agent-factory/references/setup.md")
    assert skill.startswith("---\nname: agent-factory\n")
    assert "references/setup.md" in skill
    assert "{{" not in skill and "}}" not in skill
    assert "factory.launch" not in skill
    assert "--profile" in skill
    # The cut at "## Deployment" must not leave a pointer to text that is no longer there.
    assert not re.search(r"\b(?:below|above)\b", skill)
    assert "operating instructions" not in skill
    match = re.search(r"^description: (.*)$", skill, re.MULTILINE)
    assert match
    description = match[1]
    assert not re.search(r"\b(?:launch|merge|promote)", description)


@pytest.mark.parametrize("args", [[], ["--setup", "--profile", "example"]])
def test_package_requires_exactly_one_mode(args):
    with pytest.raises(SystemExit) as exc:
        package_skill.main(args)
    assert exc.value.code == 2


def test_installed_skill_documentation_links_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(package_skill, "OUTPUT", tmp_path / "agent-factory.zip")
    for mode in (["--setup"], ["--profile", "example"]):
        package_skill.main(mode)
        with zipfile.ZipFile(package_skill.OUTPUT) as archive:
            for name in archive.namelist():
                content = archive.read(name).decode()
                assert not re.search(r"(?<![/\w])(?:docs/[\w/-]+\.md|profiles/README\.md)", content)
                for target in re.findall(r"\]\(([^)]+)\)", content):
                    if urlparse(target).scheme:
                        prefix = "https://github.com/unionai-oss/flyte-agent-factory/blob/main/"
                        assert target.startswith(prefix)
                        assert (package_skill.ROOT / target.removeprefix(prefix).split("#")[0]).is_file()
                        continue
                    assert str(PurePosixPath(name).parent / target) in archive.namelist()
