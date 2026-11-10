"""016: `standards/` is the owner's, read from the workspace and never written by a project."""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_scope,
    _gates,
    _instructions,
    _profile,
    _session_config,
    _standards,
    _timeline,
)
from tests import _standards as template
from tests._git import git

pytestmark = pytest.mark.real_factory_gates

FILES = template.FILES


@pytest.mark.parametrize("layout", ["sandbox", "root"])
@pytest.mark.parametrize("path", ["standards/engineering.md", "standards/languages/python.md"])
def test_standards_is_never_in_scope(layout, path):
    assert _gate_scope.scope_violations([path], "app", "May edit: **", layout=layout) == [path]
    assert _gate_scope.scope_violations([path], "app", f"May edit: {path}", layout=layout) == [path]
    assert _gate_scope.scope_violations(["standards_notes.md"], "app", "May edit: **", layout="root") == []


def test_a_commit_editing_standards_is_a_scope_violation(project):
    workspace = project[0]
    (workspace / "standards").mkdir()
    (workspace / "standards/engineering.md").write_text("# mine\n")
    git(workspace, "add", "standards")
    git(workspace, "commit", "-m", "loosen the rules")
    blocking = _gates.run(workspace, "app", "May edit: **", "pattern", "DONE", {}, _timeline._Timeline())
    assert "standards/ is the owner's" in blocking and "scope" in blocking


def test_an_uncommitted_new_file_in_standards_is_a_scope_violation(project):
    workspace = project[0]
    (workspace / "app/main.py").write_text("def answer():\n    return 1  # touched\n")
    git(workspace, "add", "app")
    git(workspace, "commit", "-m", "work")
    (workspace / "standards").mkdir()
    (workspace / "standards/extra.md").write_text("# mine\n")
    blocking = _gates.run(workspace, "app", "May edit: **", "pattern", "DONE", {}, _timeline._Timeline())
    assert "standards/extra.md" in blocking


def test_the_session_is_told_standards_are_never_editable():
    text = _instructions._SCOPE_NEVER_EDIT
    for name in ("standards/", "waivers.toml", "gardening.toml"):
        assert name in text


def workspace_with(tmp_path, monkeypatch, engineering: str | None):
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "_PROFILE", None)
    monkeypatch.setattr(_session_config, "_project_name", "app")
    (tmp_path / "projects/app").mkdir(parents=True)
    monkeypatch.setattr(_session_config, "_project_dir", lambda: tmp_path / "projects/app")
    if engineering is not None:
        directory = tmp_path / "standards"
        directory.mkdir()
        for name, text in FILES.items():
            (directory / name).parent.mkdir(parents=True, exist_ok=True)
            (directory / name).write_text(text)
        (directory / "engineering.md").write_text(engineering)


def test_the_workspaces_standards_are_the_ones_enforced(tmp_path, monkeypatch):
    extra = (
        "## WS-001 MUST\nWhy: the owner wants it.\n"
        f"Enforced: tool {sys.executable} -c 'raise SystemExit(1)'\nCheck: always fails\n"
    )
    workspace_with(tmp_path, monkeypatch, FILES["engineering.md"] + "\n" + extra)
    files = _session_config._applicable_standards()
    assert "WS-001" in {i for t in files.values() for i, _, _ in _standards.RULE.findall(t)}


def test_a_rule_removed_from_the_workspace_is_not_enforced(tmp_path, monkeypatch):
    kept = _standards.RULE.sub(lambda m: "" if m[1] == template.PROMOTION else m[0], FILES["engineering.md"])
    workspace_with(tmp_path, monkeypatch, kept)
    files = _session_config._applicable_standards()
    assert template.PROMOTION not in {i for t in files.values() for i, _, _ in _standards.RULE.findall(t)}


def test_without_workspace_standards_the_defaults_are_used_with_a_note(tmp_path, monkeypatch, capsys):
    """Kept name (#72): the engine has no default standards now; without workspace standards there are no
    rules, and a note says so."""
    workspace_with(tmp_path, monkeypatch, None)
    assert _session_config._applicable_standards() == {}
    assert "no standards configured" in capsys.readouterr().err


def load_uncaptured_engine_profile(tmp_path, monkeypatch):
    text = (Path(__file__).parent / "profiles/work.toml").read_text()
    workspace_with(tmp_path / "workspace", monkeypatch, None)
    path = tmp_path / "engine/profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text + '\n[standards]\ndir = "standards"\nlanguages = []\n')
    profile = _profile.load(str(path))
    assert profile.source_root == tmp_path / "engine"
    assert not profile.standards_payload
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    for name in ("installed", "cwd"):
        directory = tmp_path / name / "standards"
        directory.mkdir(parents=True)
        for file in ("engineering.md", "security.md"):
            (directory / file).write_text(f"# {name} distractor\n")
    monkeypatch.setattr(_standards, "ROOT", tmp_path / "installed")
    monkeypatch.chdir(tmp_path / "cwd")
    return profile


@pytest.mark.parametrize("profile_local", [False, True])
def test_uncaptured_engine_profile_standards_reach_runtime_gates(tmp_path, monkeypatch, profile_local):
    profile = load_uncaptured_engine_profile(tmp_path, monkeypatch)
    files = {
        "engineering.md": (
            "## ORIGIN-001 MUST\nWhy: prove the selected rules run.\n"
            f"Enforced: command {sys.executable} -c 'raise SystemExit(1)'\n"
        ),
        "security.md": "# origin security\n",
    }
    repository_rules = tmp_path / "engine/standards"
    repository_rules.mkdir()
    for name, text in files.items():
        (repository_rules / name).write_text(text)
    if profile_local:
        directory = profile.source_dir / "standards"
        directory.mkdir()
        files = {name: text + "\nProfile-local rules.\n" for name, text in files.items()}
        for name, text in files.items():
            (directory / name).write_text(text)

    shipped = _profile.parse(profile.deployment_text(), "serialized")
    assert _standards.decode(shipped.standards_payload) == files
    assert _session_config._standards_diagnostics() == {"configured": True}
    assert not (_session_config.WORKSPACE / ".claude-flyte/standards").exists()
    assert _session_config._applicable_standards() == files
    directory = _session_config.WORKSPACE / ".claude-flyte/standards"
    materialized = {str(p.relative_to(directory)): p.read_text() for p in directory.rglob("*") if p.is_file()}
    assert materialized == files
    results = _gates.standards_results(
        _session_config._project_dir(), files, configs=directory / "languages/configs"
    )
    assert any(result.status == _gate_base.Status.FAIL and result.rule == "ORIGIN-001" for result in results)


def test_uncaptured_direction_profile_runtime_matches_its_deployment(tmp_path, monkeypatch):
    """A hand-loaded direction profile with `dir` unset uses its checkout's `standards/` at runtime too."""
    # The example profile with auto-deploy on, which `[direction]` needs.
    text = (
        (Path(__file__).parents[1] / "profiles/example.toml")
        .read_text()
        .replace("auto = false", "auto = true", 1)
    )
    text = text.replace(
        '# factory = "git@github.com:you/factory.git"', 'factory = "git@github.com:you/factory.git"', 1
    )
    workspace_with(tmp_path / "workspace", monkeypatch, None)
    tree = tmp_path / "deploy-fetch-1/tree"
    path = tree / "profiles/name.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        text + '\n[direction]\nrepo = "https://github.com/you/direction"\nprofile = "profiles/name.toml"\n'
    )
    files = {"engineering.md": "# direction engineering\n", "security.md": "# direction security\n"}
    (tree / "standards").mkdir()
    for name, body in files.items():
        (tree / "standards" / name).write_text(body)
    profile = _profile.load(str(path))
    assert profile.source_root == tree and not profile.standards_payload and not profile.standards.dir
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.chdir(tmp_path)

    assert profile.captured_standards() == files
    shipped = _profile.parse(profile.deployment_text(), "serialized")
    assert _standards.decode(shipped.standards_payload) == files
    assert _session_config._standards_diagnostics() == {"configured": True}
    assert _session_config._applicable_standards() == files


def test_uncaptured_engine_profile_missing_standards_ignore_unrelated_roots(tmp_path, monkeypatch):
    profile = load_uncaptured_engine_profile(tmp_path, monkeypatch)
    diagnostics = _session_config._standards_diagnostics()
    assert diagnostics["configured"] is None
    warning = diagnostics["warning"]
    assert isinstance(warning, str)
    assert "dir does not exist" in warning
    assert str(profile.source_dir / "standards") in warning
    with pytest.raises(ValueError, match="dir does not exist"):
        _session_config._applicable_standards()


def test_uncaptured_engine_profile_invalid_local_standards_do_not_use_repository_rules(tmp_path, monkeypatch):
    profile = load_uncaptured_engine_profile(tmp_path, monkeypatch)
    directory = tmp_path / "engine/standards"
    directory.mkdir()
    for name in ("engineering.md", "security.md"):
        (directory / name).write_text("# valid repository rules\n")
    (profile.source_dir / "standards").mkdir()
    assert _session_config._standards_diagnostics()["configured"] is None
    with pytest.raises(ValueError, match="missing base file: engineering.md"):
        _session_config._applicable_standards()


@pytest.mark.parametrize("workspace_rules", [None, "# workspace rules\n"])
def test_snapshot_standards_diagnostics_use_effective_workspace_before_gates(
    tmp_path, monkeypatch, workspace_rules
):
    from flyte_agent_factory import _profile

    workspace_with(tmp_path, monkeypatch, workspace_rules)
    profile = _profile.load("tests/profiles/work.toml")
    profile = replace(profile, standards_payload=_standards.encode({}))
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    timeline = _timeline._Timeline()
    configured = workspace_rules is not None
    assert timeline.snapshot()["standards"]["configured"] is configured
    files = _session_config._applicable_standards()
    assert bool(files) is configured
    timeline.row("⚠️", "no standards configured")
    for i in range(600):
        timeline.row("🔧", f"later event {i}")
    assert all("no standards configured" not in text for _, _, text in timeline._rows)
    data = timeline.snapshot()
    assert data["standards"]["configured"] is configured
    assert ("no standards configured" in data["standards"].get("warning", "")) is (not configured)


def test_standards_warning_uses_materialized_selection(tmp_path, monkeypatch, capsys):
    from flyte_agent_factory import _profile

    workspace_with(tmp_path, monkeypatch, None)
    profile = _profile.load("tests/profiles/work.toml")
    profile = replace(profile, standards_payload=_standards.encode({"unselected.md": "# unused\n"}))
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    assert _session_config._applicable_standards() == {}
    assert "no standards configured" in capsys.readouterr().err


def test_snapshot_survives_incomplete_workspace_standards(tmp_path, monkeypatch):
    from flyte_agent_factory import _dashboard

    workspace_with(tmp_path, monkeypatch, None)
    (tmp_path / "standards").mkdir()
    (tmp_path / "standards/security.md").write_text("# workspace security\n")
    timeline = _timeline._Timeline()
    timeline.row("🔧", "work continues")
    data = _dashboard.from_report(timeline.render())
    assert data is not None
    assert data["standards"]["configured"] is None
    assert "missing base file: engineering.md" in data["standards"]["warning"]
    assert any(row["text"] == "work continues" for row in data["timeline"])
    assert timeline.snapshot()["standards"] == data["standards"]
    with pytest.raises(ValueError, match="missing base file: engineering.md"):
        _session_config._applicable_standards()


@pytest.mark.parametrize(
    "error, reason",
    [
        (PermissionError("private read details"), "PermissionError"),
        (ValueError("private payload contents"), "invalid standards bundle or text"),
        (
            UnicodeDecodeError("utf-8", b"private payload contents", 0, 1, "invalid"),
            "invalid standards bundle or text",
        ),
        (ValueError("[standards] missing base file: " + "x" * 50_000), "missing base file"),
    ],
)
def test_standards_diagnostics_bound_errors_without_exposing_payloads(tmp_path, monkeypatch, error, reason):
    workspace_with(tmp_path, monkeypatch, None)

    def invalid():
        raise error

    monkeypatch.setattr(_session_config, "_captured_standards", invalid)
    diagnostics = _session_config._standards_diagnostics()
    assert diagnostics["configured"] is None
    warning = diagnostics["warning"]
    assert isinstance(warning, str)
    assert reason in warning
    assert "private" not in warning
    assert len(warning) <= 512
    with pytest.raises(type(error)) as raised:
        _session_config._applicable_standards()
    assert raised.value is error


def test_standards_diagnostics_do_not_hide_programming_errors(tmp_path, monkeypatch):
    workspace_with(tmp_path, monkeypatch, None)

    def broken():
        raise TypeError("programming error")

    monkeypatch.setattr(_session_config, "_captured_standards", broken)
    with pytest.raises(TypeError, match="programming error"):
        _session_config._standards_diagnostics()


@pytest.mark.parametrize("workspace_rules", [None, "# workspace rules\n", ""])
def test_standards_diagnostics_honor_workspace_precedence_over_populated_bundle(
    tmp_path, monkeypatch, workspace_rules
):
    from flyte_agent_factory import _profile

    workspace_with(tmp_path, monkeypatch, workspace_rules)
    if workspace_rules == "":
        for path in (tmp_path / "standards").rglob("*"):
            if path.is_file():
                path.unlink()
    profile = replace(
        _profile.load("tests/profiles/work.toml"),
        standards_payload=_standards.encode({"engineering.md": "# captured rules\n"}),
    )
    if workspace_rules == "":
        profile = replace(profile, standards=replace(profile.standards, include=(), languages=()))
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    files = _session_config._applicable_standards()
    assert _session_config._standards_diagnostics()["configured"] is bool(files)
    assert bool(files) is (workspace_rules != "")
