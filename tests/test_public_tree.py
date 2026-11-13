"""`ops/public_tree.py`: the public repository's first commit is the committed tree minus the private part."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from ops import public_tree


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    repo = tmp_path / "private"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "owner@example.invalid", cwd=repo)
    _git("config", "user.name", "Owner Person", cwd=repo)
    _git("remote", "add", "origin", "git@github.com:someone/private.git", cwd=repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hi')\n")
    (repo / ".claude-flyte" / "proj").mkdir(parents=True)
    (repo / ".claude-flyte" / "proj" / "ACCEPTANCE.md").write_text("private\n")
    (repo / "README.md").write_text("one\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "first", cwd=repo)
    (repo / "README.md").write_text("two\n")
    _git("commit", "-qam", "second, with history that must not go public", cwd=repo)
    # Neither an uncommitted edit nor an untracked file reaches the tree.
    (repo / "README.md").write_text("uncommitted\n")
    (repo / "notes.txt").write_text("untracked\n")
    return repo


def test_the_tree_is_the_committed_files_minus_the_private_ones(source, tmp_path):
    dest = tmp_path / "public"
    public_tree.build(dest, source)
    assert sorted(_git("ls-files", cwd=dest).splitlines()) == ["README.md", "src/app.py"]
    assert (dest / "README.md").read_text() == "two\n"
    assert not (dest / ".claude-flyte").exists() and not (dest / "notes.txt").exists()


def test_the_tree_has_one_commit_by_a_neutral_author_and_no_remote(source, tmp_path):
    dest = tmp_path / "public"
    sha = public_tree.build(dest, source)
    assert _git("rev-list", "--all", cwd=dest).splitlines() == [sha]
    assert _git("branch", "--show-current", cwd=dest) == "main"
    assert _git("remote", cwd=dest) == ""
    who = _git("log", "-1", "--format=%an <%ae>|%cn <%ce>|%s", cwd=dest)
    name, email = public_tree.AUTHOR
    assert who == f"{name} <{email}>|{name} <{email}>|{public_tree.MESSAGE}"
    assert "Owner Person" not in who and "owner@example.invalid" not in who


def test_it_refuses_a_destination_that_is_not_empty(source, tmp_path):
    dest = tmp_path / "public"
    dest.mkdir()
    (dest / "x").write_text("x")
    with pytest.raises(FileExistsError):
        public_tree.build(dest, source)


def test_a_file_tracked_despite_an_ignore_rule_does_not_reach_the_tree(tmp_path):
    # `git archive` extracts what is tracked; the fresh commit takes what is not ignored. A file in both
    # sets' gap (tracked with `add -f` under an ignore rule) must not be left on disk outside the commit.
    repo = tmp_path / "private"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "owner@example.invalid", cwd=repo)
    _git("config", "user.name", "Owner Person", cwd=repo)
    (repo / ".gitignore").write_text("dist/\n")
    (repo / "dist").mkdir()
    (repo / "dist" / "skill.zip").write_bytes(b"generated")
    _git("add", ".gitignore", cwd=repo)
    _git("add", "-f", "dist/skill.zip", cwd=repo)
    _git("commit", "-qm", "first", cwd=repo)
    dest = tmp_path / "public"
    public_tree.build(dest, repo)
    assert _git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=dest) == ""
    assert not (dest / "dist").exists()
    assert _git("ls-files", cwd=dest).splitlines() == [".gitignore"]


def test_the_users_git_attributes_do_not_change_what_is_committed(tmp_path, monkeypatch):
    # A global `* text` rule would turn a CRLF blob into LF in the public commit; it is the owner's setting,
    # not the repository's.
    repo = tmp_path / "private"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "owner@example.invalid", cwd=repo)
    _git("config", "user.name", "Owner Person", cwd=repo)
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    _git("add", "crlf.txt", cwd=repo)
    _git("commit", "-qm", "first", cwd=repo)
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "attributes").write_text("* text\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    dest = tmp_path / "public"
    public_tree.build(dest, repo)
    blob = subprocess.run(
        ["git", "cat-file", "-p", "HEAD:crlf.txt"], cwd=dest, capture_output=True, check=True
    )
    assert blob.stdout == b"a\r\nb\r\n"


def test_what_stays_private_is_tracked_wherever_it_exists():
    # A PRIVATE entry that is present but ignored rather than tracked is a stale rule. In the public tree
    # itself nothing private is present.
    root = public_tree.ROOT
    for name in public_tree.PRIVATE:
        if (root / name).exists():
            assert _git("ls-files", name, cwd=root), f"{name} is not tracked: nothing to leave out"
    assert all("/" not in name and name for name in public_tree.PRIVATE)


def test_the_cli_builds_and_reports_a_bad_destination(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(public_tree, "ROOT", source)
    assert public_tree.main(["build", str(tmp_path / "public")]) == 0
    assert public_tree.main(["build", str(tmp_path / "public")]) == 2
    assert "is not empty" in capsys.readouterr().err


def test_the_cli_build_says_where_it_built_at_which_commit_and_how_many_files(
    source, tmp_path, monkeypatch, capsys
):
    # CI's log pairs this count with the scan's "scanned N files": the same N means the scan read every file.
    monkeypatch.setattr(public_tree, "ROOT", source)
    dest = tmp_path / "public"
    assert public_tree.main(["build", str(dest)]) == 0
    sha = _git("rev-parse", "HEAD", cwd=dest)
    assert capsys.readouterr().out.strip() == f"built {dest} at {sha} (2 files)"


def _scan():
    spec = importlib.util.spec_from_file_location(
        "ops_scan_public", public_tree.ROOT / "ops" / "scan_public.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def public_head(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The public tree of this checkout's HEAD, as the cutover would publish it."""
    dest: Path = tmp_path_factory.mktemp("public") / "flyte-agent-factory"
    public_tree.build(dest)
    return dest


def test_the_public_tree_of_head_names_no_owner(public_head):
    assert _scan().literal_hits(public_head) == []


def test_the_public_tree_of_head_is_clean_right_after_building(public_head):
    # Every file on disk is in the one commit: nothing untracked or ignored before the checks add caches.
    assert _git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=public_head) == ""


def test_the_scan_says_how_many_owner_literals_it_loaded_and_files_it_read(tmp_path, monkeypatch, capsys):
    # CI's log is the evidence that the guard ran with the owner's list over the whole tree, so it says both.
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    (tree / ".git" / "config").write_text("[core]\n")
    (tree / "docs").mkdir()
    (tree / "docs" / "a.md").write_text("nothing here\n")
    (tree / "README.md").write_text("still nothing\n")
    (tree / "logo.bin").write_bytes(b"\xff\xfe\x00\x81")
    owner = tmp_path / "owner-literals.txt"
    owner.write_text("# private\nnobody-in-particular\nexample-org-[0-9]+\n")
    monkeypatch.setenv("FACTORY_OWNER_LITERALS", str(owner))
    scan = _scan()
    monkeypatch.setattr(scan, "gitleaks_hits", lambda path: [])

    assert scan.main([str(tree)]) == 0

    out = capsys.readouterr()
    assert "literals: 2 owner literals" in out.err
    assert f"scanned 3 files under {tree} (1 binary, skipped)" in out.err
    assert out.out.strip() == "clean"


# The scan is the public-safety guard: a root it cannot read must fail it, never print "clean".


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_the_scan_rejects_a_root_that_is_not_a_directory(tmp_path, monkeypatch, capsys, kind):
    root = tmp_path / "public-tree"
    if kind == "file":
        root.write_text("not a tree\n")
    scan = _scan()
    ran: list[Path] = []

    def scanner(path: Path, extra: list[str] | None = None) -> list[str]:
        ran.append(path)
        return []

    monkeypatch.setattr(scan, "gitleaks_hits", scanner)
    monkeypatch.setattr(scan, "literal_hits", scanner)

    assert scan.main([str(root)]) != 0

    out = capsys.readouterr()
    assert "not a directory" in out.err
    assert "clean" not in out.out
    assert ran == []  # neither scanner ran


def test_the_scan_fails_a_non_empty_tree_it_read_no_file_of(tmp_path, monkeypatch, capsys):
    # Everything under the root is git data, which the scan skips: nothing was checked, so nothing is clean.
    root = tmp_path / "public-tree"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    scan = _scan()
    monkeypatch.setattr(scan, "gitleaks_hits", lambda path: [])

    assert scan.main([str(root)]) != 0

    out = capsys.readouterr()
    assert "read no file" in out.err
    assert "clean" not in out.out


def test_the_scan_fails_a_tree_of_binary_files_only(tmp_path, monkeypatch, capsys):
    # Binary files are skipped, so a tree of nothing else had no line checked.
    root = tmp_path / "public-tree"
    root.mkdir()
    (root / "logo.bin").write_bytes(b"\xff\xfe\x00\x81")
    scan = _scan()
    monkeypatch.setattr(scan, "gitleaks_hits", lambda path: [])

    assert scan.main([str(root)]) != 0
    assert "read no file" in capsys.readouterr().err


def test_there_is_no_dry_run_subcommand(tmp_path):
    # Checking a tree against a target's CI is `ops/promote.py deliver --dry-run`, for any target.
    with pytest.raises(SystemExit) as raised:
        public_tree.main(["dry-run", str(tmp_path / "public")])
    assert raised.value.code == 2
