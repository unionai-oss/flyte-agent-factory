"""The workspace template: contents, creation, migration, and that it is fit to be public."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from flyte_agent_factory import _gardening, _standards, _waivers, _workspace

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = _workspace.TEMPLATE


def _ops(name: str):
    spec = importlib.util.spec_from_file_location(f"ops_{name}", ROOT / "ops" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> set[str]:
    return {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and not {"__pycache__", ".ruff_cache"} & set(p.parts)
    }


def test_template_has_the_documented_layout():
    for name in (
        "README.md",
        "profile.example.toml",
        "gardening.toml",
        "waivers.toml",
        ".gitignore",
        ".github/CODEOWNERS",
        "docs/branch-protection.md",
        "projects/.gitkeep",
        "standards/README.md",
    ):
        assert (TEMPLATE / name).is_file(), name
    assert not (TEMPLATE / "profile.toml").exists()
    owners = (TEMPLATE / ".github/CODEOWNERS").read_text()
    for path in ("/standards/", "/waivers.toml", "/gardening.toml"):
        assert f"{path} @OWNER" in owners


def test_template_config_files_parse_and_only_the_engine_is_gardened(tmp_path):
    # Waivers are read from a commit, so the template's file has to be in one.
    committed_copy = tmp_path / "copy"
    committed_copy.mkdir()
    shutil.copy(TEMPLATE / "waivers.toml", committed_copy)
    for args in (["init"], ["add", "waivers.toml"], ["commit", "-m", "template"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=committed_copy,
            check=True,
            capture_output=True,
        )
    assert tomllib.loads((TEMPLATE / "profile.example.toml").read_text())["repo"]["project"]
    assert _gardening.load(TEMPLATE, {}) == []  # gardening is opt-in: the template lists no target
    assert _waivers.load(committed_copy, "HEAD") == []


def test_template_standards_equal_the_engine_standards():
    """Kept name (#72): the template's set is the source now, so there is no engine copy to equal.

    The template is the source of the example set: a rule it cannot enforce is a defect.
    """
    files = _standards._read_standards(TEMPLATE / "standards")
    rules = _standards.rules(files)
    assert rules
    assert not [(r.ident, r.error) for r in rules if r.error]
    assert len({r.ident for r in rules}) == len(rules)
    assert all(len(re.findall(r"^Enforced:", r.body, re.MULTILINE)) == 1 for r in rules)


@pytest.fixture
def person(monkeypatch):
    """A git identity that is not the placeholder, as a person's configuration would give."""
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Pat Example")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "pat@example.com")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_new_workspace_creates_a_repository_with_one_commit(tmp_path, person):
    # Still one commit of the whole template; it is now the person's, never an "agent-workspace" placeholder.
    dest = tmp_path / "ws"
    assert _ops("new_workspace").main([str(dest)]) == 0
    # One listing: git may write .git/ files (maintenance.lock) between two.
    assert {p for p in _tree(dest) if not p.startswith(".git/")} == _tree(TEMPLATE)
    assert len(_git(dest, "log", "--oneline").splitlines()) == 1
    assert _git(dest, "log", "-1", "--format=%an <%ae>").strip() == "Pat Example <pat@example.com>"


def test_new_workspace_without_a_git_identity_fails_clearly_and_writes_nothing(tmp_path, monkeypatch, capsys):
    for var in ("AUTHOR_NAME", "AUTHOR_EMAIL", "COMMITTER_NAME", "COMMITTER_EMAIL"):
        monkeypatch.delenv(f"GIT_{var}", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-such-config"))
    assert _ops("new_workspace").main([str(tmp_path / "ws")]) == 1
    assert "no commit identity" in capsys.readouterr().err
    assert not (tmp_path / "ws").exists()


@pytest.fixture
def clone(tmp_path, person):
    """A clone of a bare repository holding GitHub's auto-created files, like `gh repo create
    --add-readme`."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=seed, check=True)
    (seed / "README.md").write_text("# auto\n")
    (seed / "LICENSE").write_text("MIT, mine\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "Initial commit")
    _git(seed, "push", "-q", str(bare), "main")
    subprocess.run(["git", "clone", "-q", str(bare), str(tmp_path / "ws")], check=True)
    return tmp_path / "ws"


def test_new_workspace_populates_a_clone_and_keeps_its_remote_and_license(clone):
    remote = _git(clone, "remote", "get-url", "origin")
    assert _ops("new_workspace").main([str(clone)]) == 0
    assert _git(clone, "remote", "get-url", "origin") == remote
    assert (clone / "LICENSE").read_text() == "MIT, mine\n"
    assert (clone / "README.md").read_text() == (TEMPLATE / "README.md").read_text()
    assert (clone / "profile.example.toml").is_file()
    assert len(_git(clone, "log", "--oneline").splitlines()) == 1  # nothing committed without --commit


def test_new_workspace_commits_in_a_clone_as_the_person_when_asked(clone):
    assert _ops("new_workspace").main([str(clone), "--commit"]) == 0
    authors = set(_git(clone, "log", "--format=%an <%ae>").splitlines())
    assert authors == {"Pat Example <pat@example.com>"}
    assert "agent-workspace" not in _git(clone, "log", "--format=%an %ae %cn %ce")
    assert _git(clone, "status", "--porcelain") == ""


def test_new_workspace_points_at_the_engines_profiles_folder(tmp_path, capsys, person):
    """Kept name: the message still says where the profile goes, repointed by the brief.

    The brief's "`ops/new_workspace.py` tells the user to write `profiles/<name>.toml`, while the template
    README and architecture-repos.md say `profile.toml`. Pick one." is settled as one name, profile.toml at
    the workspace root, everywhere; the engine's profiles folder is no longer where a person writes it.
    """
    assert _ops("new_workspace").main([str(tmp_path / "ws")]) == 0
    printed = capsys.readouterr().out
    assert "profile.example.toml to profile.toml" in printed and "profiles/<name>.toml" not in printed


def test_new_workspace_accepts_an_existing_empty_directory(tmp_path, person):
    (tmp_path / "ws").mkdir()
    assert _ops("new_workspace").main([str(tmp_path / "ws")]) == 0


def test_new_workspace_refuses_a_non_empty_directory(tmp_path, capsys, person):
    (tmp_path / "keep.txt").write_text("mine\n")
    assert _ops("new_workspace").main([str(tmp_path)]) == 1
    assert "refusing" in capsys.readouterr().err
    assert _tree(tmp_path) == {"keep.txt"}


def test_new_workspace_refuses_a_clone_with_other_files_untouched(clone, capsys):
    (clone / "mine.py").write_text("print(1)\n")
    before = _tree(clone)
    assert _ops("new_workspace").main([str(clone), "--commit"]) == 1
    assert "refusing" in capsys.readouterr().err
    assert _tree(clone) == before
    assert (clone / "README.md").read_text() == "# auto\n"


def test_new_workspace_refuses_a_readme_without_a_git_directory(tmp_path, capsys, person):
    (tmp_path / "README.md").write_text("mine\n")
    assert _ops("new_workspace").main([str(tmp_path)]) == 1
    assert (tmp_path / "README.md").read_text() == "mine\n"


# --- the public scan ---------------------------------------------------------------------------


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "copy"
    shutil.copytree(TEMPLATE, dest)
    return dest


def test_template_has_no_personal_literal():
    assert _ops("scan_public").literal_hits(TEMPLATE) == []


def test_a_personal_literal_in_a_copy_is_found(tmp_path, monkeypatch):
    copy = _copy(tmp_path)
    (copy / "README.md").write_text(
        "kept in acme-widgets-corp and " + "arn:aws:iam::" + "123456789012:role/x\n"
    )
    scan = _ops("scan_public")
    # A generic shape (the AWS account id) is found with no owner list at all...
    monkeypatch.delenv(scan._literals.ENV, raising=False)
    assert scan.literal_hits(copy) == ["README.md:1: personal literal"]
    assert scan.main([str(copy)]) != 0
    # ...and an owner's own identifier through the private list the variable names.
    (copy / "README.md").write_text("kept in acme-widgets-corp\n")
    assert scan.literal_hits(copy) == []
    listed = tmp_path / "owner-literals.txt"
    listed.write_text("acme-widgets\n")
    monkeypatch.setenv(scan._literals.ENV, str(listed))
    assert scan.literal_hits(copy) == ["README.md:1: personal literal"]


def test_the_scan_skips_the_allowlisted_lines_and_only_those(tmp_path, monkeypatch):
    scan = _ops("scan_public")
    path, text = scan.LINE_ALLOWLIST[0]
    [allowed] = [ln for ln in (ROOT / path).read_text().splitlines() if text in ln]
    # The allowlisted line holds an owner literal: make the scan's list one that matches it.
    owner = [allowed.split(text, 1)[1].split("(", 1)[0]]
    (tmp_path / path).parent.mkdir(parents=True)
    (tmp_path / path).write_text(allowed + "\n")
    assert scan.literal_hits(tmp_path, owner) == []
    (tmp_path / "other.py").write_text(allowed + "\n")
    assert scan.literal_hits(tmp_path, owner) == ["other.py:1: personal literal"]


def test_the_scan_says_which_literals_it_used(tmp_path, monkeypatch, capsys):
    scan = _ops("scan_public")
    monkeypatch.delenv(scan._literals.ENV, raising=False)
    monkeypatch.setattr(scan, "gitleaks_hits", lambda path: [])
    assert scan.main([str(tmp_path)]) == 0
    assert "generic shapes only" in capsys.readouterr().err
    monkeypatch.setenv(scan._literals.ENV, str(tmp_path / "missing.txt"))
    assert scan.main([str(tmp_path)]) == 2
    assert "is not a file" in capsys.readouterr().err


def test_template_passes_gitleaks(tmp_path):
    assert _ops("scan_public").gitleaks_hits(TEMPLATE) == []


@pytest.mark.parametrize(
    "secret",
    [
        "token = " + '"ghp_' + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + '"\n',
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"  # gitleaks:allow
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "-----END OPENSSH PRIVATE KEY-----\n",
    ],
)
def test_a_planted_secret_fails_gitleaks(tmp_path, secret):
    copy = _copy(tmp_path)
    (copy / "docs/leak.md").write_text(secret)
    assert _ops("scan_public").gitleaks_hits(copy)


def test_the_public_tree_of_head_passes_gitleaks(tmp_path):
    public_head = tmp_path / "flyte-agent-factory"
    _ops("public_tree").build(public_head)
    assert _ops("scan_public").gitleaks_hits(public_head) == []


def test_the_gitleaks_allowlist_is_narrow(tmp_path):
    # The benchmark digest it allows passes; any other value of that shape still fails.
    (tmp_path / "a.py").write_text(
        '"tests/test_api.py": "'
        + "75e9fd03"
        + "8e5e7dbd076486df5acafbdc79518bd891ec722d06a1b7a0dfd5d207"
        + '",\n'
    )
    assert _ops("scan_public").gitleaks_hits(tmp_path) == []
    (tmp_path / "a.py").write_text(
        '"tests/test_api.py": "'
        + "85e9fd03"
        + "8e5e7dbd076486df5acafbdc79518bd891ec722d06a1b7a0dfd5d207"
        + '",\n'
    )
    assert _ops("scan_public").gitleaks_hits(tmp_path)


def test_scan_fails_clearly_without_gitleaks(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _ops("scan_public").main([str(TEMPLATE)]) == 2
    assert "gitleaks is not installed" in capsys.readouterr().err


# --- migration ---------------------------------------------------------------------------------


@pytest.fixture
def old_layout(tmp_path):
    sandbox, direction = tmp_path / "sandbox", tmp_path / "direction"
    for path, text in {
        sandbox / "shortlinks/app.py": "print(1)\n",
        sandbox / "shortlinks/.git/HEAD": "ref\n",
        sandbox / "projects/logstats/app.py": "print(2)\n",
        sandbox / ".github/x.yml": "x\n",
        direction / "profiles/mine.toml": 'name = "mine"\n',
        direction / "profiles/example.toml": 'name = "example"\n',
        direction / "briefs/shortlinks.md": "brief\n",
        direction / "briefs/orphan.md": "old brief\n",
        direction / "OPERATIONS.md": "history\n",
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return sandbox, direction


def _migrate(*args, engine=None):
    module = _ops("migrate_workspace")
    if engine is not None:
        module.ENGINE_ROOT = engine
    return module.main(list(map(str, args)))


def test_migration_does_not_mistake_a_shipped_example_for_the_deployment_profile(old_layout, tmp_path):
    sandbox, direction = old_layout
    (direction / "profiles/example-claude-personal.toml").write_text('name = "claude-personal"\n')
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out) == 0
    assert (out / "profile.toml").read_text() == 'name = "mine"\n'


def test_migration_maps_projects_profile_briefs_and_history(old_layout, tmp_path):
    sandbox, direction = old_layout
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out) == 0
    assert (out / "projects/shortlinks/app.py").read_text() == "print(1)\n"
    assert (out / "projects/logstats/app.py").is_file()
    assert not (out / "projects/shortlinks/.git").exists()
    assert (out / "profile.toml").read_text() == 'name = "mine"\n'
    assert (out / "projects/shortlinks/BRIEF.md").read_text() == "brief\n"
    assert (out / "briefs/orphan.md").is_file() and (out / "history/OPERATIONS.md").is_file()
    assert (out / "standards/README.md").is_file()  # the template is the base
    checklist = (out / "MIGRATION.md").read_text()
    for step in ("allowed-repo", "deploy key", "board", "LAST"):
        assert step in checklist
    assert checklist.index("deploy key") < checklist.index("LAST")


def test_migration_dry_run_prints_the_plan_and_writes_nothing(old_layout, tmp_path, capsys):
    sandbox, direction = old_layout
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out, "--dry-run") == 0
    printed = capsys.readouterr().out
    assert "projects/shortlinks" in printed and "profile.toml" in printed and "dry run" in printed
    assert not out.exists()


def test_migration_refuses_a_non_empty_out(old_layout, tmp_path):
    sandbox, direction = old_layout
    (tmp_path / "out").mkdir()
    (tmp_path / "out/x").write_text("x")
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", tmp_path / "out") == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_migration_refuses_a_project_present_in_both_sandbox_layouts(old_layout, tmp_path, dry_run):
    sandbox, direction = old_layout
    (sandbox / "foo").mkdir()
    (sandbox / "foo/NOTES.md").write_text("notes\n")
    (sandbox / "projects/foo").mkdir()
    (sandbox / "projects/foo/OTHER.md").write_text("other\n")
    out = tmp_path / "out"
    args = ["--sandbox", sandbox, "--direction", direction, "--out", out, *(["--dry-run"] if dry_run else [])]
    with pytest.raises(SystemExit) as refused:
        _migrate(*args)
    assert refused.value.code not in (0, None)
    message = str(refused.value.code)
    assert str(sandbox / "foo") in message and str(sandbox / "projects/foo") in message
    assert not out.exists()


@pytest.fixture
def owner_content(old_layout):
    """The old layout plus the owner content that used to live in the engine repo."""
    _, direction = old_layout
    for rel, text in {
        "docs/operations-log.md": "runs\n",
        "briefs/selfimprove.md": "self\n",
        "briefs/image-labels.md": "labels\n",
        "briefs/demo-polynomial.md": "product brief\n",
        "ops/briefs/thing.md": "thing\n",
        "ops/briefs/factory/plan.md": "plan\n",
        "briefs/own.md": "own\n",
    }.items():
        (direction / rel).parent.mkdir(parents=True, exist_ok=True)
        (direction / rel).write_text(text)
    return old_layout


def test_migration_carries_the_operations_log_and_owner_briefs(owner_content, tmp_path):
    sandbox, direction = owner_content
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out) == 0
    assert (out / "history/operations-log.md").read_text() == "runs\n"
    assert (out / "briefs/selfimprove.md").read_text() == "self\n"
    assert (out / "briefs/image-labels.md").read_text() == "labels\n"
    # A brief the engine ships is not carried; every other brief is the owner's.
    assert not (out / "briefs/demo-polynomial.md").exists()
    assert not (out / "history/briefs/demo-polynomial.md").exists()


def test_migration_plans_every_brief_the_engine_does_not_ship(owner_content, tmp_path):
    sandbox, direction = owner_content
    planned = {
        str(dest): source.relative_to(direction).as_posix()
        for source, dest in _ops("migrate_workspace").plan(sandbox, direction, "")
        if source.is_relative_to(direction) and source.suffix == ".md"
    }
    assert planned["briefs/thing.md"] == "ops/briefs/thing.md"
    assert planned["history/briefs/factory/plan.md"] == "ops/briefs/factory/plan.md"
    assert planned["briefs/own.md"] == "briefs/own.md"
    assert not any("demo-polynomial" in dest for dest in planned)


def test_migration_dry_run_lists_the_owner_content(owner_content, tmp_path, capsys):
    sandbox, direction = owner_content
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out, "--dry-run") == 0
    printed = capsys.readouterr().out
    for dest in ("history/operations-log.md", "briefs/selfimprove.md", "briefs/image-labels.md"):
        assert f"-> {out / dest}\n" in printed
    assert not out.exists()


def test_migration_rerun_leaves_the_workspace_untouched(owner_content, tmp_path):
    sandbox, direction = owner_content
    out = tmp_path / "out"
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out) == 0
    (out / "briefs/selfimprove.md").write_text("edited in the workspace\n")
    assert _migrate("--sandbox", sandbox, "--direction", direction, "--out", out) == 1
    assert (out / "briefs/selfimprove.md").read_text() == "edited in the workspace\n"


@pytest.fixture
def engine(tmp_path):
    """An engine checkout whose history deleted two owner briefs and which still ships one product brief."""
    root = tmp_path / "engine"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for rel in ("briefs/demo-x.md", "briefs/gone.md", "ops/briefs/old-plan.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("b\n")
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "."], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "add"], cwd=root, check=True)
    subprocess.run([*git, "rm", "-q", "briefs/gone.md", "ops/briefs/old-plan.md"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "move out"], cwd=root, check=True)
    return root


@pytest.mark.parametrize("dry_run", [False, True])
def test_migration_warns_about_owner_files_missing_from_the_direction_checkout(
    old_layout, engine, tmp_path, capsys, dry_run
):
    sandbox, direction = old_layout
    (direction / "briefs").mkdir(exist_ok=True)
    (direction / "briefs/gone.md").write_text("self\n")
    args = ["--sandbox", sandbox, "--direction", direction, "--out", tmp_path / "out"]
    assert _migrate(*args, *(["--dry-run"] if dry_run else []), engine=engine) == 0
    warning = capsys.readouterr().err
    # Present files are not named; each absent one is, with how to get it from the engine's history.
    assert "briefs/gone.md" not in warning
    assert "docs/operations-log.md" not in warning  # the OPERATIONS.md spelling is there
    assert "ops/briefs/old-plan.md" in warning
    assert "briefs/demo-x.md" not in warning  # still shipped, so never lost
    for spellings in _ops("migrate_workspace")._HISTORY_DOCS:
        if spellings[-1] != "docs/operations-log.md":
            assert spellings[-1] in warning
    assert "git show <rev>:<path>" in warning
    assert "git history" in warning


def test_migration_does_not_warn_when_every_owner_file_is_present(owner_content, engine, tmp_path, capsys):
    sandbox, direction = owner_content
    ops = _ops("migrate_workspace")
    for rel in ("briefs/gone.md", "ops/briefs/old-plan.md"):
        (direction / rel).write_text("b\n")
    for spellings in ops._HISTORY_DOCS:
        (direction / spellings[-1]).parent.mkdir(parents=True, exist_ok=True)
        (direction / spellings[-1]).write_text("h\n")
    args = ["--sandbox", sandbox, "--direction", direction, "--out", tmp_path / "out"]
    assert _migrate(*args, engine=engine) == 0
    assert capsys.readouterr().err == ""
