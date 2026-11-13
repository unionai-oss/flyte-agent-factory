import re
import runpy
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import pytest
import yaml

from ops import package_skill


def _skill_text(archive_path, name):
    with zipfile.ZipFile(archive_path) as archive:
        return archive.read(f"{name}/SKILL.md").decode()


def test_committed_dist_matches_a_fresh_build(tmp_path):
    """The drift check: a chat-facing edit without `ops/package_skill.py` fails here."""
    package_skill.build(tmp_path)
    committed = {p.name: p.read_bytes() for p in package_skill.DIST.iterdir()}
    fresh = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert sorted(fresh) == ["VERSION", "agent-factory.zip", "since.zip"]
    assert fresh == committed, (
        "skills changed: run `uv run python ops/package_skill.py` and commit skills/dist"
    )


def test_build_is_deterministic(tmp_path):
    package_skill.build(tmp_path / "a")
    package_skill.build(tmp_path / "b")
    for name in ("agent-factory.zip", "since.zip", "VERSION"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_zip_layout_and_version_line(tmp_path):
    package_skill.build(tmp_path)
    digest = (tmp_path / "VERSION").read_text()
    assert re.fullmatch(r"[0-9a-f]{12}\n", digest)
    with zipfile.ZipFile(tmp_path / "agent-factory.zip") as archive:
        assert archive.namelist() == ["agent-factory/SKILL.md", "agent-factory/references/setup.md"]
        assert {i.date_time for i in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}
    with zipfile.ZipFile(tmp_path / "since.zip") as archive:
        assert archive.namelist() == ["since/SKILL.md"]
    for name in ("agent-factory", "since"):
        assert _skill_text(tmp_path / f"{name}.zip", name).endswith(f"\n\nSkill version: {digest}")


def test_digest_changes_with_any_source_file(tmp_path, monkeypatch):
    before = package_skill.compute_source_digest()
    root = tmp_path / "skills"
    (root / "x/references").mkdir(parents=True)
    (root / "x/SKILL.md").write_text("a\n")
    monkeypatch.setattr(package_skill, "SKILLS", root)
    first = package_skill.compute_source_digest()
    (root / "x/references/r.md").write_text("b\n")
    assert package_skill.compute_source_digest() not in (first, before)


@pytest.mark.parametrize("name", ["agent-factory", "since"])
def test_skill_frontmatter_is_valid_yaml_with_name_and_description(name):
    skill = (package_skill.SKILLS / name / "SKILL.md").read_text()
    _, frontmatter, _ = skill.split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    assert meta["name"] == name
    assert isinstance(meta["description"], str) and meta["description"]


def test_check_reports_current_stale_and_unknown(tmp_path, capsys):
    package_skill.build(tmp_path)
    assert package_skill.main(["--check", str(tmp_path / "agent-factory.zip")]) == 0
    assert capsys.readouterr().out.strip() == "current"

    skill = tmp_path / "SKILL.md"
    skill.write_text("body\n\nSkill version: 000000000000\n")
    assert package_skill.main(["--check", str(skill)]) == 1
    assert capsys.readouterr().out.strip() == (
        f"stale: installed 000000000000, source {package_skill.compute_source_digest()}"
    )

    skill.write_text("body without a version\n")
    assert package_skill.main(["--check", str(skill)]) == 2
    assert capsys.readouterr().out.strip() == "unknown: no Skill version line"


def test_check_reports_an_extra_file_in_an_installed_directory_as_stale(tmp_path, capsys):
    package_skill.build(tmp_path)
    with zipfile.ZipFile(tmp_path / "agent-factory.zip") as archive:
        archive.extractall(tmp_path / "installed")
    installed = tmp_path / "installed" / "agent-factory"
    assert package_skill.main(["--check", str(installed)]) == 0
    (installed / "extra.md").write_text("not part of the skill\n")
    assert package_skill.main(["--check", str(installed)]) == 1
    assert "extra.md differs from a fresh build" in capsys.readouterr().out


def test_check_says_where_the_problem_is_for_a_zip_with_SKILL_md_at_the_root(tmp_path, capsys):
    package_skill.build(tmp_path)
    flat = tmp_path / "flat.zip"
    with zipfile.ZipFile(tmp_path / "agent-factory.zip") as src, zipfile.ZipFile(flat, "w") as out:
        for info in src.infolist():
            if info.filename.endswith("/SKILL.md"):
                out.writestr("SKILL.md", src.read(info))
    assert package_skill.main(["--check", str(flat)]) == 2
    assert capsys.readouterr().out.strip() == "unknown: SKILL.md is not inside a <skill name>/ directory"


def test_check_reports_a_zip_with_edited_files_but_a_current_version_line_as_stale(tmp_path, capsys):
    package_skill.build(tmp_path)
    good = tmp_path / "agent-factory.zip"
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(edited, "w") as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith("references/setup.md"):
                data = b"edited content\n"
            out.writestr(info, data)
    assert package_skill.main(["--check", str(edited)]) == 1
    assert capsys.readouterr().out.startswith("stale: edited.zip differs from a fresh build")


@pytest.mark.parametrize("args", [["--setup"], ["--profile", "x"], ["--setup", "--profile", "x"]])
def test_package_requires_exactly_one_mode(args):
    # There is one mode now, building every skill; the old `--setup` / `--profile` modes are refused.
    with pytest.raises(SystemExit) as exc:
        package_skill.main(args)
    assert exc.value.code == 2


def test_retired_modes_are_gone():
    for flag in ("--setup", "--profile"):
        with pytest.raises(SystemExit) as exc:
            package_skill.main([flag, "x"])
        assert exc.value.code == 2


def test_render_rejects_unknown_placeholders():
    # Rendering is retired: no placeholder of any kind reaches a packaged skill.
    with zipfile.ZipFile(package_skill.DIST / "agent-factory.zip") as archive:
        skill = archive.read("agent-factory/SKILL.md").decode()
    assert "{{" not in skill and "}}" not in skill


def test_installed_skill_documentation_links_resolve():
    for path in package_skill.DIST.glob("*.zip"):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                content = archive.read(name).decode()
                assert not re.search(r"(?<![/\w])(?:docs/[\w/-]+\.md|profiles/README\.md)", content)
                for target in re.findall(r"\]\(([^)]+)\)", content):
                    if urlparse(target).scheme:
                        assert target.startswith("https://github.com/unionai-oss/flyte-agent-factory/")
                        relative = re.split(r"/(?:blob|tree)/main/", target)[1]
                        assert (package_skill.ROOT / relative.split("#")[0]).exists(), target
                        continue
                    assert str(PurePosixPath(name).parent / target) in archive.namelist()


def test_package_renders_profile_values_and_standard_skill_layout(tmp_path, monkeypatch):
    # Nothing profile-specific is rendered any more: the build is the same whatever profile is around, and
    # each zip has the standard `<skill>/SKILL.md` layout.
    monkeypatch.setenv("AGENT_FACTORY_PROFILE", str(tmp_path / "absent.toml"))
    package_skill.build(tmp_path)
    for name in ("agent-factory", "since"):
        assert (tmp_path / f"{name}.zip").read_bytes() == (package_skill.DIST / f"{name}.zip").read_bytes()
        with zipfile.ZipFile(tmp_path / f"{name}.zip") as archive:
            assert f"{name}/SKILL.md" in archive.namelist()


def test_setup_reference_is_available_in_the_installed_skill(tmp_path):
    package_skill.build(tmp_path)
    with zipfile.ZipFile(tmp_path / "agent-factory.zip") as archive:
        skill = archive.read("agent-factory/SKILL.md").decode()
        reference = archive.read("agent-factory/references/setup.md").decode()
    assert "references/setup.md" in skill
    assert "ops/bootstrap.py" in reference


def test_setup_package_needs_no_profile_and_cannot_launch(tmp_path, monkeypatch):
    # One package for everyone: it is built with no profile, and setup mode sends the chat to the
    # bootstrap rather than to `factory.launch`, which needs a deployed factory.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_FACTORY_PROFILE", raising=False)
    package_skill.build(tmp_path / "dist")
    with zipfile.ZipFile(tmp_path / "dist" / "agent-factory.zip") as archive:
        reference = archive.read("agent-factory/references/setup.md").decode()
    assert "ops/bootstrap.py" in reference


def _extract(tmp_path):
    package_skill.build(tmp_path)
    with zipfile.ZipFile(tmp_path / "agent-factory.zip") as archive:
        archive.extractall(tmp_path / "installed")
    return tmp_path / "installed" / "agent-factory"


def test_check_reports_an_edited_installed_skill_md_as_stale(tmp_path, capsys):
    skill = _extract(tmp_path) / "SKILL.md"
    skill.write_text(skill.read_text() + "more\n")
    assert package_skill.main(["--check", str(skill)]) == 1
    assert "SKILL.md differs from a fresh build" in capsys.readouterr().out


def test_check_reports_an_edited_installed_reference_as_stale(tmp_path, capsys):
    installed = _extract(tmp_path)
    reference = installed / "references" / "setup.md"
    reference.write_text(reference.read_text() + "more\n")
    for target in (installed, installed / "SKILL.md"):
        assert package_skill.main(["--check", str(target)]) == 1
        assert "references/setup.md differs from a fresh build" in capsys.readouterr().out


def test_check_accepts_an_unedited_installed_directory_even_when_renamed(tmp_path, capsys):
    installed = _extract(tmp_path)
    renamed = installed.rename(installed.parent / "my-copy")
    assert package_skill.main(["--check", str(renamed)]) == 0
    assert capsys.readouterr().out.strip() == "current"


def test_check_accepts_a_rezipped_skill_with_the_same_content(tmp_path, capsys):
    package_skill.build(tmp_path)
    good = tmp_path / "agent-factory.zip"
    rezipped = tmp_path / "rezipped.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(rezipped, "w", zipfile.ZIP_DEFLATED) as out:
        for info in reversed(src.infolist()):
            out.writestr(info.filename, src.read(info))
    assert package_skill.main(["--check", str(rezipped)]) == 0
    assert capsys.readouterr().out.strip() == "current"


def test_build_replaces_stale_zips(tmp_path):
    (tmp_path / "retired.zip").write_bytes(b"old")
    written = package_skill.build(tmp_path)
    assert not (tmp_path / "retired.zip").exists()
    assert sorted(p.name for p in written) == ["agent-factory.zip", "since.zip"]


def test_check_reports_a_missing_path(tmp_path, capsys):
    missing = tmp_path / "nope.zip"
    assert package_skill.main(["--check", str(missing)]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == f"error: {missing} does not exist"
    assert captured.out == ""


def test_main_without_arguments_builds_and_lists_each_zip(monkeypatch, capsys):
    # build() writes to the committed skills/dist; stand in for it so the test touches no repo files.
    zips = [package_skill.DIST / "a.zip", package_skill.DIST / "b.zip"]
    monkeypatch.setattr(package_skill, "build", lambda: zips)
    assert package_skill.main([]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Wrote {zips[0].relative_to(package_skill.ROOT)}",
        f"Wrote {zips[1].relative_to(package_skill.ROOT)}",
    ]


def test_running_the_script_exits_with_main_status(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["package_skill.py", "--check", str(tmp_path / "nope.zip")])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(package_skill.__file__)), run_name="__main__")
    assert exc.value.code == 2
