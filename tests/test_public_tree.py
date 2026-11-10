"""`ops/public_tree.py`: the public repository's first commit is the committed tree minus the private part."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

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


def test_the_dry_run_scans_then_runs_each_lane_after_the_other(tmp_path, monkeypatch):
    # One lane at a time: two xdist pools at once on a 4-CPU pod push the benchmark's inner pytest past its
    # exit limit (tests/test_bench_flow.py), which the same tests pass alone.
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd, cwd, env):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 3 if "-m" in cmd and "slow" in cmd else 0)

    monkeypatch.setattr(public_tree, "build", lambda dest: "f" * 40)
    monkeypatch.setattr(public_tree, "_git", lambda *a, **k: "a\nb")
    monkeypatch.setattr(subprocess, "run", run)
    assert public_tree.dry_run(tmp_path) == 1  # the slow lane failed
    scan, fast, slow = (cmd for cmd, _ in calls)
    assert scan[-2:] == ["ops/scan_public.py", str(tmp_path)]
    assert fast == ["bash", "factory-improvement/checks.sh"]
    assert "AGENT_FACTORY_RUN_SLOW" not in calls[1][1]
    assert slow[-5:] == ["pytest", "tests", "-q", "-m", "slow"]
    assert calls[2][1]["PYTHONPATH"] == "src"


@pytest.mark.parametrize("lane_code", [0, 3])
def test_the_dry_run_with_child_lanes_leaves_both_lanes_to_checks_sh(tmp_path, monkeypatch, lane_code):
    # checks.sh sends both lanes to child containers under AGENT_FACTORY_SLOW_LANE=child, so they overlap
    # without contending for this pod's CPUs; a second, sequential slow run would only repeat the work.
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(cmd, cwd, env):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, lane_code if cmd[0] == "bash" else 0)

    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setenv("AGENT_FACTORY_PYTEST_JUNIT_DIR", "/somewhere")
    monkeypatch.setattr(public_tree, "build", lambda dest: "f" * 40)
    monkeypatch.setattr(public_tree, "_git", lambda *a, **k: "" if a[0] == "status" else "a\nb")
    monkeypatch.setattr(subprocess, "run", run)
    assert public_tree.dry_run(tmp_path) == (0 if lane_code == 0 else 1)
    scan, checks = (cmd for cmd, _ in calls)
    assert scan[-2:] == ["ops/scan_public.py", str(tmp_path)]
    assert checks == ["bash", "factory-improvement/checks.sh"]
    assert calls[1][1]["AGENT_FACTORY_SLOW_LANE"] == "child"
    assert "AGENT_FACTORY_PYTEST_JUNIT_DIR" not in calls[1][1]


def test_the_dry_run_scan_reads_only_the_tree_and_leaves_nothing_in_it(tmp_path, monkeypatch):
    # The scanner's import of ops/personal_literals.py must not write a .pyc into the tree it scans, or the
    # scan counts (and the tree holds) a file the build never put in it.
    tree = tmp_path / "tree"
    (tree / "ops").mkdir(parents=True)
    for name in ("scan_public.py", "personal_literals.py", "gitleaks.toml"):
        (tree / "ops" / name).write_bytes((public_tree.ROOT / "ops" / name).read_bytes())
    _git("init", "-q", cwd=tree)
    _git("add", "-A", cwd=tree)
    _git("-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-qm", "tree", cwd=tree)
    before = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if ".git" not in p.parts)
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "git" or "ops/scan_public.py" in cmd:
            return real_run(cmd, **{"capture_output": True, **kwargs})
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(public_tree, "build", lambda dest: "f" * 40)
    monkeypatch.setattr(subprocess, "run", run)
    public_tree.dry_run(tree)
    assert sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*") if ".git" not in p.parts) == before


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


def test_the_dry_run_resolves_a_relative_destination_before_any_subprocess(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    built: list[Path] = []
    calls: list[tuple[list[str], str]] = []

    def run(cmd, cwd, env):
        calls.append((cmd, str(cwd)))
        return subprocess.CompletedProcess(cmd, 0)

    def build(dest: Path) -> str:
        built.append(dest)
        return "f" * 40

    monkeypatch.setattr(public_tree, "build", build)
    monkeypatch.setattr(public_tree, "_git", lambda *a, **k: "a")
    monkeypatch.setattr(subprocess, "run", run)
    public_tree.dry_run(Path("relative-public"))

    expected = tmp_path / "relative-public"
    assert built == [expected]
    scan, *_ = calls
    assert scan[0][-1] == str(expected)
    assert all(Path(cwd).is_absolute() for _, cwd in calls)


def test_a_dry_run_of_a_relative_destination_scans_every_file(tmp_path, monkeypatch):
    # End to end: the real build and the real scan, from a relative destination, read the whole tree. gitleaks
    # is a stub here: what is under test is which files the literal scan reads.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gitleaks").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.delenv("FACTORY_OWNER_LITERALS", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    real_run = subprocess.run
    scans: list[subprocess.CompletedProcess[str]] = []

    def run(cmd, **kwargs):
        if "factory-improvement/checks.sh" in cmd or cmd[-2:] == ["-m", "slow"]:
            return subprocess.CompletedProcess(cmd, 0)  # the checks and the slow lane are not under test
        if "ops/scan_public.py" not in cmd:
            return real_run(cmd, **kwargs)  # the build's own git
        scans.append(real_run(cmd, **kwargs, capture_output=True, text=True))
        return scans[-1]

    monkeypatch.setattr(subprocess, "run", run)
    assert public_tree.dry_run(Path("relative-public")) == 0
    dest = work / "relative-public"
    files = len(_git("ls-files", cwd=dest).splitlines())
    assert files > 100
    assert f"scanned {files} files under {dest} (0 binary, skipped)" in scans[0].stderr, scans[0].stderr


def test_the_scan_fails_a_tree_of_binary_files_only(tmp_path, monkeypatch, capsys):
    # Binary files are skipped, so a tree of nothing else had no line checked.
    root = tmp_path / "public-tree"
    root.mkdir()
    (root / "logo.bin").write_bytes(b"\xff\xfe\x00\x81")
    scan = _scan()
    monkeypatch.setattr(scan, "gitleaks_hits", lambda path: [])

    assert scan.main([str(root)]) != 0
    assert "read no file" in capsys.readouterr().err


# The built tree is what the owner pushes: the dry run's checks leave it exactly as built (round-1 review).


def _dry_run_with_fake_steps(
    source: Path, dest: Path, monkeypatch: pytest.MonkeyPatch, step: Any
) -> tuple[int, list[tuple[list[str], Path]]]:
    """Run `dry_run` on a tree built from `source`, with `step(cmd, cwd)` standing in for each check."""
    real_build = public_tree.build
    calls: list[tuple[list[str], Path]] = []
    real_run = subprocess.run

    def run(cmd, **kwargs):
        if cmd[0] == "git":
            return real_run(cmd, **kwargs)
        calls.append((cmd, Path(kwargs["cwd"])))
        step(cmd, Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(public_tree, "build", lambda d: real_build(d, source))
    monkeypatch.setattr(subprocess, "run", run)
    return public_tree.dry_run(dest), calls


def test_the_checks_run_in_a_clone_and_leave_the_built_tree_as_built(source, tmp_path, monkeypatch):
    def step(cmd: list[str], cwd: Path) -> None:
        # What checks.sh and pytest leave behind in the tree they run in.
        (cwd / ".venv").mkdir(exist_ok=True)
        (cwd / ".pytest_cache").mkdir(exist_ok=True)
        (cwd / "src" / "__pycache__").mkdir(exist_ok=True)
        (cwd / "ops.watch.lock").write_text("")

    dest = tmp_path / "public"
    code, calls = _dry_run_with_fake_steps(source, dest, monkeypatch, step)

    assert code == 0
    assert [cmd[-1] for cmd, _ in calls][0] == str(dest)  # the scan reads the built tree
    assert all(cwd != dest and dest not in cwd.parents for _, cwd in calls)
    assert _git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=dest) == ""
    assert sorted(_git("ls-files", cwd=dest).splitlines()) == ["README.md", "src/app.py"]


def test_a_dry_run_that_leaves_anything_in_the_built_tree_fails(source, tmp_path, monkeypatch, capsys):
    dest = tmp_path / "public"

    def step(cmd: list[str], cwd: Path) -> None:
        (dest / "stray.txt").write_text("a check wrote into the published tree\n")

    code, _ = _dry_run_with_fake_steps(source, dest, monkeypatch, step)

    assert code == 1
    out = capsys.readouterr().out
    assert "stray.txt" in out
    assert "dry run FAILED" in out
