"""A project's delivered tree: `promote.toml`, `import`, and the scratch checkout the target gates run in."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _delivery
from tests._git import git

pytestmark = pytest.mark.real_factory_gates

MAKEFILE = "lint:\n\t@python check.py\n"
CHECK_OK = "import sys\nsys.exit(0)\n"


@pytest.fixture(autouse=True)
def operator(tmp_path_factory, monkeypatch):
    """The person running `promote.py`: their git identity, and no system or ambient git configuration."""
    config = tmp_path_factory.mktemp("gitconfig") / "config"
    config.write_text("[user]\n\tname = Op Erator\n\temail = operator@example.invalid\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(name, raising=False)


def seed_target(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    """A bare repository on `main` holding `files`; return it and its head."""
    remote = tmp_path / "target.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed))
    git(seed, "checkout", "-q", "-b", "main")
    commit(seed, files or {"Makefile": MAKEFILE, "check.py": CHECK_OK, "notes.txt": "one\ntwo\nthree\n"})
    git(seed, "push", "-q", "origin", "main")
    return remote, git(seed, "rev-parse", "HEAD")


def commit(repo: Path, files: dict[str, str], message: str = "change") -> None:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "main")
    commit(workspace, {"projects/.gitkeep": ""}, "workspace")
    return workspace


@pytest.fixture
def world(tmp_path):
    remote, head = seed_target(tmp_path)
    return remote, head, make_workspace(tmp_path)


def test_import_writes_the_tree_and_promote_toml_in_one_commit(world):
    remote, head, workspace = world
    before = git(workspace, "rev-list", "--count", "HEAD")

    delivery, message = _delivery.import_tree(str(remote), workspace / "projects/demo")

    assert delivery == _delivery.Delivery(str(remote), head, ".")
    assert (workspace / "projects/demo/notes.txt").read_text() == "one\ntwo\nthree\n"
    assert _delivery.load(workspace / "projects/demo") == delivery
    assert re.fullmatch(r"[0-9a-f]{40}", delivery.base)
    assert int(git(workspace, "rev-list", "--count", "HEAD")) == int(before) + 1
    assert (
        git(workspace, "log", "-1", "--format=%s")
        == message
        == f"import {remote}@{head[:12]} into projects/demo"
    )
    assert git(workspace, "status", "--porcelain") == ""
    assert "projects/demo/promote.toml" in git(workspace, "ls-files").splitlines()


def test_import_commits_only_its_own_paths(world):
    remote, _, workspace = world
    (workspace / "staged.txt").write_text("the operator's other work\n")
    git(workspace, "add", "staged.txt")

    _delivery.import_tree(str(remote), workspace / "projects/demo")

    assert git(workspace, "show", "--name-only", "--format=", "HEAD").count("staged.txt") == 0
    assert git(workspace, "diff", "--cached", "--name-only") == "staged.txt"


def test_import_refuses_a_destination_that_is_not_empty(world):
    remote, _, workspace = world
    project = workspace / "projects/demo"
    project.mkdir()
    (project / "mine.txt").write_text("work\n")
    with pytest.raises(_delivery.DeliveryError, match="not empty"):
        _delivery.import_tree(str(remote), project)
    assert not (project / "notes.txt").exists()


def test_a_project_with_only_its_harness_files_is_an_empty_destination(world):
    remote, _, workspace = world
    project = workspace / "projects/demo"
    project.mkdir()
    (project / "checks.sh").write_text("exit 0\n")
    _delivery.import_tree(str(remote), project)
    assert (project / "notes.txt").is_file()


def test_import_refuses_an_existing_promote_toml(world):
    remote, _, workspace = world
    _delivery.import_tree(str(remote), workspace / "projects/demo", path="tree")
    with pytest.raises(_delivery.DeliveryError, match="already delivers"):
        _delivery.import_tree(str(remote), workspace / "projects/demo", path="other")


@pytest.mark.parametrize("name", _delivery.HARNESS_NAMES)
def test_import_refuses_a_root_that_has_a_harness_name_unless_it_goes_in_a_subdirectory(tmp_path, name):
    remote, head = seed_target(tmp_path, {name: "x\n", "app.py": "x = 1\n"})
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"

    with pytest.raises(_delivery.DeliveryError, match=rf"{re.escape(name)}.*--path"):
        _delivery.import_tree(str(remote), project)
    assert not (project / "app.py").exists()

    delivery, _ = _delivery.import_tree(str(remote), project, path="upstream")
    assert delivery == _delivery.Delivery(str(remote), head, "upstream")
    assert (project / "upstream" / name).is_file()
    assert (project / "upstream/app.py").is_file()


def test_import_of_a_ref_takes_that_branch(world):
    remote, head, workspace = world
    seed = remote.parent / "seed"
    git(seed, "checkout", "-q", "-b", "next")
    commit(seed, {"notes.txt": "next\n"})
    git(seed, "push", "-q", "origin", "next")

    delivery, _ = _delivery.import_tree(str(remote), workspace / "projects/demo", ref="next")

    assert delivery.base == git(seed, "rev-parse", "next") != head
    assert (workspace / "projects/demo/notes.txt").read_text() == "next\n"


def test_a_new_repository_is_imported_without_a_clone(tmp_path):
    workspace = make_workspace(tmp_path)
    delivery, message = _delivery.import_tree(
        "https://example.invalid/team/new.git", workspace / "projects/n", new=True
    )
    assert delivery.base == "" and delivery.path == "."
    assert _delivery.load(workspace / "projects/n") == delivery
    assert message == "import https://example.invalid/team/new.git@new into projects/n"
    assert git(workspace, "ls-files", "projects/n") == "projects/n/promote.toml"


def test_a_target_without_commits_is_reported(tmp_path):
    empty = tmp_path / "empty.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(empty))
    workspace = make_workspace(tmp_path)
    with pytest.raises(_delivery.DeliveryError, match="no commits"):
        _delivery.import_tree(str(empty), workspace / "projects/demo")


def test_import_keeps_a_file_the_target_marks_export_ignore(tmp_path):
    """`git archive` would drop it: the import must carry everything the target tracks."""
    remote, _ = seed_target(
        tmp_path, {".gitattributes": "tests export-ignore\n", "tests/t.py": "x\n", "a.py": "y\n"}
    )
    workspace = make_workspace(tmp_path)
    _delivery.import_tree(str(remote), workspace / "projects/demo")
    assert (workspace / "projects/demo/tests/t.py").is_file()


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("not toml [", "not valid TOML"),
        ('base = ""\n', "needs `repo"),
        ('repo = "r"\nbase = "abc123"\n', "40-hex"),
        ('repo = "r"\npath = "../x"\n', "inside the project"),
        ('repo = "r"\npath = "/etc"\n', "inside the project"),
        ('repo = "r"\npath = 3\n', "`path`"),
    ],
)
def test_a_malformed_promote_toml_says_what_is_wrong(tmp_path, text, complaint):
    (tmp_path / "promote.toml").write_text(text)
    with pytest.raises(_delivery.DeliveryError, match=re.escape(complaint)):
        _delivery.load(tmp_path)


def test_promote_toml_defaults_and_absence(tmp_path):
    assert _delivery.load(tmp_path) is None
    (tmp_path / "promote.toml").write_text('repo = "r"\n')
    assert _delivery.load(tmp_path) == _delivery.Delivery("r", "", ".")
    (tmp_path / "promote.toml").write_text('repo = "r"\npath = "./sub//dir"\n')
    assert _delivery.load(tmp_path) == _delivery.Delivery("r", "", "sub/dir")


def test_write_round_trips_awkward_strings(tmp_path):
    original = _delivery.Delivery('https://h/"quoted"\\x', "a" * 40, "sub")
    _delivery.write(tmp_path, original)
    assert _delivery.load(tmp_path) == original


# --- the scratch checkout the target gates run in ---------------------------------------------


@pytest.fixture
def imported(world):
    remote, head, workspace = world
    project = workspace / "projects/demo"
    delivery, _ = _delivery.import_tree(str(remote), project)
    return project, delivery, workspace


def test_the_checkout_is_the_imported_commit_with_the_work_on_top(imported):
    project, delivery, workspace = imported
    (project / "checks.sh").write_text("exit 0\n")
    (project / "SUMMARY.md").write_text("# s\n")
    commit(workspace, {"projects/demo/notes.txt": "one\nTWO\nthree\n", "projects/demo/new.py": "x = 1\n"})

    with _delivery.target_checkout(project, delivery) as checkout:
        assert git(checkout, "rev-parse", "origin/HEAD") != git(checkout, "rev-parse", "HEAD")
        assert git(checkout, "merge-base", "HEAD", "origin/HEAD") == git(checkout, "rev-parse", "origin/HEAD")
        assert sorted(git(checkout, "diff", "--name-only", "origin/HEAD", "HEAD").splitlines()) == [
            "new.py",
            "notes.txt",
        ]
        assert git(checkout, "show", "origin/HEAD:notes.txt") == "one\ntwo\nthree"
        assert (checkout / "notes.txt").read_text() == "one\nTWO\nthree\n"
        # The harness files are not the target's.
        assert not {"checks.sh", "SUMMARY.md", "promote.toml"} & set(git(checkout, "ls-files").splitlines())
    assert not checkout.exists()


def test_the_checkout_sees_uncommitted_work_and_leaves_the_project_alone(imported):
    project, delivery, workspace = imported
    (project / "notes.txt").write_text("edited, not committed\n")
    (project / "untracked.py").write_text("y = 2\n")
    (project / "check.py").unlink()
    (workspace / ".gitignore").write_text("ignored.log\n")
    (project / "ignored.log").write_text("noise\n")
    before = git(workspace, "status", "--porcelain")

    with _delivery.target_checkout(project, delivery) as checkout:
        files = set(git(checkout, "ls-files").splitlines())
        assert (checkout / "notes.txt").read_text() == "edited, not committed\n"
        assert "untracked.py" in files and "ignored.log" not in files
        assert "check.py" not in files
        assert git(checkout, "diff", "--name-status", "origin/HEAD", "HEAD").splitlines() == [
            "D\tcheck.py",
            "M\tnotes.txt",
            "A\tuntracked.py",
        ]

    assert git(workspace, "status", "--porcelain") == before


def test_the_base_is_the_commit_that_first_added_promote_toml(imported):
    """Replaces `test_the_base_is_the_commit_that_last_changed_promote_toml`, which pinned the unsafe rule.

    Taking the newest commit that touched the manifest let a session commit a comment-only edit together with
    its work: the "imported" tree then already held the work and the review diff was empty. The baseline is
    the operator's import, whatever later commits do to the file; a changed manifest is refused outright.
    """
    project, delivery, workspace = imported
    commit(workspace, {"projects/demo/notes.txt": "one\nchanged\nthree\n"}, "work")
    with _delivery.target_checkout(project, loaded(project)) as checkout:
        assert git(checkout, "show", "origin/HEAD:notes.txt") == "one\ntwo\nthree"
        assert git(checkout, "diff", "--name-only", "origin/HEAD", "HEAD") == "notes.txt"


def test_a_comment_only_manifest_edit_committed_with_the_work_is_refused_not_trusted(imported):
    project, delivery, workspace = imported
    commit(
        workspace,
        {
            "projects/demo/notes.txt": "one\nchanged\nthree\n",
            "projects/demo/promote.toml": _toml(delivery, delivery.base) + "# tidy\n",
        },
        "work and a harmless-looking manifest edit",
    )
    with pytest.raises(_delivery.DeliveryError, match="not what the import"), contextlib.ExitStack() as stack:
        stack.enter_context(_delivery.target_checkout(project, loaded(project)))


def test_a_re_added_manifest_with_another_base_is_refused(imported):
    project, delivery, workspace = imported
    (project / "promote.toml").unlink()
    git(workspace, "add", "-A")
    git(workspace, "commit", "-q", "-m", "drop")
    commit(workspace, {"projects/demo/promote.toml": _toml(delivery, "b" * 40)}, "re-add with another base")
    with pytest.raises(_delivery.DeliveryError, match="not what the import"), contextlib.ExitStack() as stack:
        stack.enter_context(_delivery.target_checkout(project, loaded(project)))


@pytest.mark.parametrize(
    ("manifest", "brief"),
    [
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("git@github.com:Acme/Widgets.git", "https://github.com/acme/widgets"),
        ("/srv/git/target.git", "file:///srv/git/target.git/"),
    ],
)
def test_the_same_repository_in_any_spelling_matches_delivers_to(manifest, brief):
    _delivery.require_repo(_delivery.Delivery(manifest, ""), brief)


@pytest.mark.parametrize("brief", ["acme/other", "https://example.com/acme/widgets", "widgets"])
def test_another_repository_than_the_manifests_is_refused(brief):
    with pytest.raises(_delivery.DeliveryError, match="must be the same repository"):
        _delivery.require_repo(_delivery.Delivery("https://github.com/acme/widgets.git", ""), brief)


def loaded(project: Path) -> _delivery.Delivery:
    """The project's manifest as it is now, read again."""
    delivery = _delivery.load(project)
    assert delivery is not None
    return delivery


def _toml(delivery, base):
    return f'repo = "{delivery.repo}"\nbase = "{base}"\npath = "{delivery.path}"\n'


def test_a_tree_in_a_subdirectory_is_the_checkout_root(world):
    remote, _, workspace = world
    project = workspace / "projects/demo"
    delivery, _ = _delivery.import_tree(str(remote), project, path="upstream")
    commit(workspace, {"projects/demo/upstream/notes.txt": "sub\n", "projects/demo/outside.txt": "no\n"})

    with _delivery.target_checkout(project, delivery) as checkout:
        assert sorted(git(checkout, "ls-files").splitlines()) == ["Makefile", "check.py", "notes.txt"]
        assert git(checkout, "diff", "--name-only", "origin/HEAD", "HEAD") == "notes.txt"


def test_a_new_repository_has_the_work_and_nothing_to_compare_with(tmp_path):
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/n"
    delivery, _ = _delivery.import_tree("https://example.invalid/new.git", project, new=True)
    commit(workspace, {"projects/n/app.py": "x = 1\n"})

    with _delivery.target_checkout(project, delivery) as checkout:
        assert git(checkout, "ls-files") == "app.py"
        assert git(checkout, "branch", "-r") == ""


def test_an_uncommitted_promote_toml_is_refused_with_the_remedy(world):
    remote, head, workspace = world
    project = workspace / "projects/demo"
    project.mkdir()
    (project / "a.py").write_text("x\n")
    delivery = _delivery.Delivery(str(remote), head)
    _delivery.write(project, delivery)
    with pytest.raises(_delivery.DeliveryError, match="not committed"), contextlib.ExitStack() as stack:
        stack.enter_context(_delivery.target_checkout(project, delivery))


def test_a_project_outside_any_repository_is_refused(tmp_path):
    (tmp_path / "p").mkdir()
    delivery = _delivery.Delivery("r", "a" * 40)
    with (
        pytest.raises(_delivery.DeliveryError, match="not a git repository"),
        contextlib.ExitStack() as stack,
    ):
        stack.enter_context(_delivery.target_checkout(tmp_path / "p", delivery))


def test_a_tree_path_that_leaves_the_project_is_refused(imported):
    project, _, _ = imported
    (project / "link").symlink_to(project.parent)
    with pytest.raises(_delivery.DeliveryError, match="leaves the project"):
        _delivery.tree_dir(project, _delivery.Delivery("r", "", "link"))


def test_delivery_does_not_import_promote():
    """`_promote` imports `_gates`, which imports `_delivery`: the other way round is a cycle."""
    source = Path(_delivery.__file__).read_text()
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "_promote" not in imported and "_gates" not in imported


# --- reading the stored objects, and the errors around it ---------------------------------------


def test_a_git_command_that_times_out_is_a_delivery_error(tmp_path, monkeypatch):
    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(_delivery.DeliveryError, match=r"git fetch origin timed out after 7s"):
        _delivery.git(tmp_path, "fetch", "origin", timeout=7)


def test_export_writes_symlinks_and_skips_a_submodule_commit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"a.py": "x = 1\n"})
    (repo / "link").symlink_to("a.py")
    git(repo, "add", "link")
    # A gitlink: a tree entry of kind `commit`, which has no stored files to write.
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},sub")
    git(repo, "commit", "-q", "-m", "link and submodule")

    dest = tmp_path / "out"
    _delivery.export_tree(repo, "HEAD", dest)

    assert (dest / "link").is_symlink() and os.readlink(dest / "link") == "a.py"
    assert (dest / "a.py").read_text() == "x = 1\n"
    assert not (dest / "sub").exists()


def test_export_refuses_a_tree_entry_that_leaves_the_tree(tmp_path, monkeypatch):
    # Git itself will not store such a name, so the listing is faked.
    real = _delivery.git

    def listing(cwd, *args, **kwargs):
        if args[0] == "ls-tree":
            return f"100644 blob {'a' * 40}\t../escape.txt\0"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(_delivery, "git", listing)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"a.py": "x\n"})
    with pytest.raises(_delivery.DeliveryError, match=r"leaves the tree: '\.\./escape\.txt'"):
        _delivery.export_tree(repo, "HEAD", tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_export_reports_an_object_the_repository_does_not_have(tmp_path, monkeypatch):
    # The shape of a shallow or partial clone: the tree names a blob that is not stored.
    real = _delivery.git
    missing = "b" * 40

    def listing(cwd, *args, **kwargs):
        if args[0] == "ls-tree":
            return f"100644 blob {missing}\tgone.txt\0"
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(_delivery, "git", listing)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, {"a.py": "x\n"})
    with pytest.raises(_delivery.DeliveryError, match=rf"cannot read gone\.txt \({missing}\) of HEAD"):
        _delivery.export_tree(repo, "HEAD", tmp_path / "out")


def test_a_tree_missing_from_the_project_is_refused(imported):
    project, _, _ = imported
    nowhere = _delivery.Delivery("r", imported[1].base, "nowhere")
    with pytest.raises(_delivery.DeliveryError, match="'nowhere' is not in the project"):
        with contextlib.ExitStack() as stack:
            stack.enter_context(_delivery.target_checkout(project, nowhere))


def test_a_tree_the_import_did_not_hold_says_the_clone_may_be_shallow(imported):
    project, delivery, _ = imported
    (project / "other").mkdir()
    (project / "other/a.py").write_text("x\n")
    elsewhere = _delivery.Delivery(delivery.repo, delivery.base, "other")
    with pytest.raises(_delivery.DeliveryError, match=r"cannot read the tree imported at [0-9a-f]{12}"):
        with contextlib.ExitStack() as stack:
            stack.enter_context(_delivery.target_checkout(project, elsewhere))


def test_the_checkout_drops_a_directory_the_work_deleted(tmp_path):
    remote, _ = seed_target(tmp_path, {"pkg/mod.py": "x = 1\n", "keep.py": "y = 2\n"})
    workspace = make_workspace(tmp_path)
    project = workspace / "projects/demo"
    delivery, _ = _delivery.import_tree(str(remote), project)
    shutil.rmtree(project / "pkg")

    with _delivery.target_checkout(project, delivery) as checkout:
        assert git(checkout, "diff", "--name-status", "origin/HEAD", "HEAD") == "D\tpkg/mod.py"
        assert not (checkout / "pkg").exists()


def test_an_import_path_that_leaves_the_project_through_a_symlink_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "link").symlink_to(outside)
    with pytest.raises(_delivery.DeliveryError, match="path 'link' leaves the project"):
        _delivery.import_tree("https://example.invalid/x.git", project, path="link", new=True)
    assert list(outside.iterdir()) == []
