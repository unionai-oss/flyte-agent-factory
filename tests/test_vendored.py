"""Vendored upstream trees: a project is judged only on what it changed in a copy of another repository."""

import shutil
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_start,
    _gate_tests,
    _gates,
    _instructions,
    _module_size,
    _session_config,
    _standards,
    _vendored,
)
from tests import _standards as template

pytestmark = pytest.mark.real_factory_gates

LIMIT = _module_size.MODULE_LINE_LIMIT
BIG = "src/flyte_agent_factory/_bench_suite.py"
BIG_LINES = 2169
TESTS = "tests/test_auth.py"
UPSTREAM_TEST = (
    "import pytest\n\n\n@pytest.mark.skip\ndef test_old():\n    assert True\n\n\n"
    "def test_auth():\n    assert True\n"
)
TREE = "flyte-agent-factory"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "x")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """Fetch only from a local `file://` upstream, into a cache of this test's own."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https", "file"))
    monkeypatch.setattr(_vendored, "_UNAVAILABLE", {})


@pytest.fixture
def upstream(tmp_path):
    root = tmp_path / "upstream"
    (root / BIG).parent.mkdir(parents=True)
    (root / BIG).write_text("x = 1\n" * BIG_LINES)
    (root / TESTS).parent.mkdir(parents=True)
    (root / TESTS).write_text(UPSTREAM_TEST)
    (root / "package.json").write_text("{}\n")
    (root / "app.js").write_text("1;\n")
    _git(root, "init", "-q")
    # GitHub serves filtered fetches; a local repository must be told to.
    _git(root, "config", "uploadpack.allowFilter", "true")
    return root, _commit(root)


def _manifest(project: Path, upstream_root: Path, commit: str, path: str = TREE, repo: str | None = None):
    repo = repo or upstream_root.as_uri()
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = "{path}"\nrepo = "{repo}"\ncommit = "{commit}"\n'
    )


@pytest.fixture
def project(tmp_path, upstream):
    """A project holding an identical copy of the upstream repository, and an empty start tree."""
    root, commit = upstream
    project = tmp_path / "ws/proj"
    shutil.copytree(root, project / TREE, ignore=shutil.ignore_patterns(".git"))
    (project / "own.py").write_text("y = 2\n")
    _manifest(project, root, commit)
    (tmp_path / "start").mkdir()
    return project


def _size(project: Path, tmp_path: Path) -> tuple[int, str]:
    return _module_size.check(project, tmp_path / "start", _gate_tests._is_test)


# -- factory:module-size --


def test_an_identical_vendored_module_over_the_limit_passes(project, tmp_path):
    code, output = _size(project, tmp_path)
    assert code == 0, output


def test_a_vendored_module_that_grew_fails_against_upstreams_count(project, tmp_path):
    with (project / TREE / BIG).open("a") as handle:
        handle.write("x = 1\n")
    code, output = _size(project, tmp_path)
    assert code == 1
    assert f"{TREE}/{BIG}: {BIG_LINES + 1} lines, was {BIG_LINES}" in output


def test_a_vendored_module_that_shrank_passes(project, tmp_path):
    (project / TREE / BIG).write_text("x = 1\n" * (BIG_LINES - 1))
    code, output = _size(project, tmp_path)
    assert code == 0, output
    assert f"({BIG_LINES - 1} lines, was {BIG_LINES})" in output


def test_a_new_over_limit_module_in_a_vendored_tree_fails(project, tmp_path):
    (project / TREE / "src/new_big.py").write_text("x = 1\n" * (LIMIT + 1))
    code, output = _size(project, tmp_path)
    assert code == 1
    assert f"{TREE}/src/new_big.py: {LIMIT + 1} lines, new file" in output


def test_without_a_manifest_the_vendored_module_fails_as_new(project, tmp_path):
    (project / "vendored.toml").unlink()
    code, output = _size(project, tmp_path)
    assert code == 1
    assert f"{TREE}/{BIG}: {BIG_LINES} lines, new file" in output


# -- factory:fail-to-pass (test removal) --


def _fail_to_pass(tmp_path: Path, changes: list[str]):
    return _gate_tests.fail_to_pass(
        tmp_path / "ws", "proj", "", changes, "", "", "DONE", start=tmp_path / "start", layout="sandbox"
    )


def test_an_identical_vendored_test_with_skips_is_not_the_projects(project, tmp_path):
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code is _gate_base.Status.NOT_APPLICABLE, output


def test_an_added_skip_in_a_vendored_test_fails(project, tmp_path):
    path = project / TREE / TESTS
    path.write_text(UPSTREAM_TEST + "\n\n@pytest.mark.skip\ndef test_new():\n    assert True\n")
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1
    assert "tests removed, deleted or skipped" in output
    assert "vendored.toml" in output  # the note says which trees were taken as upstream's


def test_a_removed_test_function_in_a_vendored_test_fails(project, tmp_path):
    (project / TREE / TESTS).write_text("import pytest\n\n\ndef test_auth():\n    assert True\n")
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1
    assert "tests removed, deleted or skipped" in output


def test_a_vendored_test_the_project_changed_is_one_of_its_changed_tests(project, tmp_path):
    (project / TREE / TESTS).write_text(UPSTREAM_TEST + "\n\ndef test_new():\n    assert False\n")
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1, output
    assert "tests removed" not in output and TESTS in output


def test_a_deleted_vendored_test_fails(project, tmp_path):
    (project / TREE / TESTS).unlink()
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1
    assert "tests removed, deleted or skipped" in output


def test_a_test_the_project_had_at_start_is_judged_against_start_not_upstream(project, tmp_path, upstream):
    """An older upstream commit without a test must not hide a test the project removed since its start."""
    root, _ = upstream
    start_copy = tmp_path / "start/proj" / TREE / TESTS
    start_copy.parent.mkdir(parents=True)
    start_copy.write_text(UPSTREAM_TEST + "\n\ndef test_mine():\n    assert True\n")
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1
    assert "tests removed, deleted or skipped" in output


def test_a_module_the_project_had_at_start_is_measured_against_start(project, tmp_path):
    before = tmp_path / "start" / TREE / BIG
    before.parent.mkdir(parents=True)
    before.write_text("x = 1\n" * (BIG_LINES - 10))
    code, output = _size(project, tmp_path)
    assert code == 1
    assert f"{BIG_LINES} lines, was {BIG_LINES - 10}" in output


# -- language detection --


def _languages(project: Path) -> set[str]:
    selected = _standards.select(template.FILES, _standards.Standards(), project)
    return {Path(n).stem for n in selected if n.startswith("languages/") and n.count("/") == 1}


@pytest.fixture
def django(tmp_path, upstream):
    """A Python project with an identical vendored `repo/` that carries `package.json` and a `.js` file."""
    root, commit = upstream
    project = tmp_path / "django-project"
    shutil.copytree(root, project / "repo", ignore=shutil.ignore_patterns(".git"))
    (project / "own.py").write_text("y = 2\n")
    return project, root, commit


def test_an_unchanged_vendored_tree_does_not_select_typescript(django):
    project, root, commit = django
    _manifest(project, root, commit, path="repo")
    assert _languages(project) >= {"python"}
    assert "typescript" not in _languages(project)


def test_without_a_manifest_the_vendored_package_json_selects_typescript(django):
    project, *_ = django
    assert "typescript" in _languages(project)


def test_an_unchanged_nested_checkout_does_not_select_typescript(django):
    project, *_ = django
    nested = project / "repo"
    _git(nested, "init", "-q", "-b", "main")
    _commit(nested)
    assert "typescript" not in _languages(project)
    (nested / "new.ts").write_text("export {};\n")
    assert "typescript" in _languages(project)


def test_a_project_owned_package_json_selects_typescript(django):
    project, root, commit = django
    _manifest(project, root, commit, path="repo")
    (project / "package.json").write_text("{}\n")
    assert "typescript" in _languages(project)


def test_a_changed_or_new_file_in_a_vendored_tree_counts(django):
    project, root, commit = django
    _manifest(project, root, commit, path="repo")
    (project / "repo/new.ts").write_text("export {};\n")
    assert "typescript" in _languages(project)
    (project / "repo/new.ts").unlink()
    (project / "repo/package.json").write_text('{"name": "changed"}\n')
    assert "typescript" in _languages(project)


# -- where upstream comes from --


def test_a_commit_on_the_remote_default_branch_needs_no_fetch(upstream, tmp_path, monkeypatch):
    root, commit = upstream
    # The project is a directory of a clone whose origin's default branch holds the commit.
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    shutil.copytree(root / "src", clone / "proj" / TREE / "src")
    monkeypatch.setattr(_vendored, "_fetch", lambda *a, **k: pytest.fail("fetched"))
    _manifest(clone / "proj", root, commit, repo="https://example.invalid/x")
    # The supervisor-recorded base the project was created at holds the commit.
    found = _vendored.load(clone / "proj", {".git"}, trusted_base=_git(clone, "rev-parse", "HEAD"))
    assert found.problems == []
    assert found.unchanged(f"{TREE}/{BIG}")


def test_a_commit_only_the_project_has_is_not_taken_as_upstream(upstream, tmp_path):
    """A session can commit anything; a commit no remote default branch holds is fetched from `repo`."""
    root, _ = upstream
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    (clone / "proj/pkg").mkdir(parents=True)
    (clone / "proj/pkg/big.py").write_text("x = 1\n" * (LIMIT + 1))
    forged = _commit(clone)  # on the session's branch, not on origin's
    _manifest(clone / "proj", root, forged, path="pkg", repo="https://example.invalid/x")
    found = _vendored.load(clone / "proj", {".git"})
    assert not found.unchanged("pkg/big.py")
    assert "unavailable" in found.describe()
    code, output = _module_size.check(clone / "proj", tmp_path / "start", _gate_tests._is_test)
    assert code == 1 and "pkg/big.py" in output


def test_a_commit_off_the_declared_repos_default_branch_is_not_upstream(project, upstream, tmp_path):
    """Any repository serves a commit of any branch by sha, the session's own pushed branch included."""
    root, _ = upstream
    _git(root, "checkout", "-q", "-b", "session-branch")
    (root / "src/forged.py").write_text("x = 1\n" * (LIMIT + 1))
    forged = _commit(root)
    shutil.copy(root / "src/forged.py", project / TREE / "src/forged.py")
    _git(root, "checkout", "-q", "-")
    _manifest(project, root, forged)
    found = _vendored.load(project, set())
    assert not found.unchanged(f"{TREE}/src/forged.py")
    assert "default branch" in found.describe()
    code, output = _size(project, tmp_path)
    assert code == 1 and "src/forged.py" in output


def test_an_older_commit_of_the_default_branch_is_upstream(project, upstream):
    root, commit = upstream
    (root / "later.txt").write_text("later\n")
    _commit(root)
    found = _vendored.load(project, set())
    assert found.problems == [] and found.unchanged(f"{TREE}/{BIG}")


def test_a_remembered_fetch_failure_expires(project, upstream, tmp_path, monkeypatch):
    root, _ = upstream
    root.rename(tmp_path / "gone")
    assert _vendored.load(project, set()).problems
    (tmp_path / "gone").rename(root)
    assert _vendored.load(project, set()).problems  # still remembered
    for key, (when, error) in list(_vendored._UNAVAILABLE.items()):
        _vendored._UNAVAILABLE[key] = (when - _vendored._UNAVAILABLE_FOR - 1, error)
    assert _vendored.load(project, set()).problems == []


def test_upstream_tests_left_out_of_a_copied_directory_are_listed(project, tmp_path):
    (project / TREE / TESTS).unlink()
    (project / TREE / "tests/test_mine.py").write_text("def test_mine():\n    assert True\n")
    found = _vendored.load(project, set())
    assert found.problems == []
    assert (
        f"{TREE}/{TESTS}" in found.describe() and "missing from a directory the copy has" in found.describe()
    )


def test_upstream_tests_of_a_directory_not_copied_are_not_listed(project):
    shutil.rmtree(project / TREE / "tests")
    assert "missing from a directory the copy has" not in _vendored.load(project, set()).describe()


def test_an_unreadable_directory_does_not_make_an_applied_tree_unavailable(project):
    tests = project / TREE / "tests"
    tests.chmod(0)
    try:
        found = _vendored.load(project, set())
    finally:
        tests.chmod(0o755)
    assert found.problems == []
    assert found.unchanged(f"{TREE}/{BIG}")


def test_the_whole_project_cannot_be_declared_vendored(project, upstream):
    root, commit = upstream
    _manifest(project, root, commit, path=".")
    found = _vendored.load(project, set())
    assert [("." in p and "subdirectory" in p) for p in found.problems] == [True]


@pytest.mark.parametrize("path", ["loop", "a" * 5000, "v\\u0000end"], ids=["symlink-loop", "too-long", "nul"])
def test_an_unusable_path_is_a_problem_not_a_crash(project, upstream, path):
    root, commit = upstream
    (project / "loop").symlink_to("loop")
    _manifest(project, root, commit, path=path)
    found = _vendored.load(project, set())
    assert len(found.problems) == 1


def test_a_failed_fetch_is_not_retried_in_the_same_process(project, upstream, tmp_path, monkeypatch):
    root, _ = upstream
    root.rename(tmp_path / "gone")
    calls = []
    real = _vendored._git

    def counting(args, *a, **k):
        calls.append(args)
        return real(args, *a, **k)

    monkeypatch.setattr(_vendored, "_git", counting)
    _vendored.load(project, set())
    fetches = sum("fetch" in c for c in calls)
    _vendored.load(project, set())
    assert fetches == 1 and sum("fetch" in c for c in calls) == 1


def test_an_unreadable_directory_does_not_break_detection(tmp_path):
    project = tmp_path / "proj"
    (project / "locked/inner").mkdir(parents=True)
    (project / "own.py").write_text("x = 1\n")
    (project / "locked").chmod(0)
    try:
        assert _vendored.load(project, set()).problems == []
    finally:
        (project / "locked").chmod(0o755)


def test_a_missing_commit_is_fetched_once_into_the_cache(project, upstream, tmp_path):
    root, commit = upstream
    first = _vendored.load(project, set())
    assert first.problems == [] and first.unchanged(f"{TREE}/{BIG}")
    assert (tmp_path / "cache/factory-gate/vendored").is_dir()
    root.rename(tmp_path / "gone")
    second = _vendored.load(project, set())
    assert second.problems == [] and second.unchanged(f"{TREE}/{BIG}")
    assert second.read_upstream(f"{TREE}/{TESTS}") == UPSTREAM_TEST.encode()
    assert second.count_upstream_lines(f"{TREE}/{BIG}") == BIG_LINES
    assert second.read_upstream(f"{TREE}/nothing.py") is None
    assert second.read_upstream("own.py") is None


def test_an_unavailable_upstream_leaves_the_files_the_projects(project, upstream, tmp_path):
    root, commit = upstream
    root.rename(tmp_path / "gone")
    found = _vendored.load(project, set())
    assert not found.unchanged(f"{TREE}/{BIG}")
    assert len(found.problems) == 1
    for part in (TREE, commit, root.as_uri(), "unavailable"):
        assert part in found.describe()
    code, output = _size(project, tmp_path)
    assert code == 1 and "unavailable" in output


def test_a_commit_upstream_does_not_have_is_unavailable(project, upstream):
    root, _ = upstream
    _manifest(project, root, "0" * 40)
    assert "unavailable" in _vendored.load(project, set()).describe()


# -- the manifest --


@pytest.mark.parametrize(
    ("path", "repo", "commit", "fragment"),
    [
        (TREE, "http://example.com/x", "a" * 40, "https://"),
        (TREE, "https://example.com/x", "abc123", "40-hex"),
        ("../elsewhere", "https://example.com/x", "a" * 40, ".."),
        ("/etc", "https://example.com/x", "a" * 40, "relative"),
        ("missing", "https://example.com/x", "a" * 40, "directory"),
    ],
)
def test_an_invalid_entry_is_a_problem_and_owns_nothing(project, path, repo, commit, fragment, monkeypatch):
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https",))
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = "{path}"\nrepo = "{repo}"\ncommit = "{commit}"\n'
    )
    found = _vendored.load(project, set())
    assert [fragment in p for p in found.problems] == [True]
    assert not found.unchanged(f"{TREE}/{BIG}")


def test_invalid_toml_is_a_problem(project):
    (project / "vendored.toml").write_text("[[tree\n")
    found = _vendored.load(project, set())
    assert "not valid TOML" in found.describe()


def test_a_symlinked_tree_leaving_the_project_is_refused(project, tmp_path):
    (project / "link").symlink_to(tmp_path / "upstream")
    _manifest(project, tmp_path / "upstream", "a" * 40, path="link")
    assert "inside the project" in _vendored.load(project, set()).describe()


def test_a_vendored_test_left_out_of_a_copied_directory_fails(project, tmp_path):
    """No diff shows a test that was never committed, so a copy without it would hide a removed test."""
    (project / TREE / TESTS).unlink()
    code, output = _fail_to_pass(tmp_path, ["proj/own.py"])
    assert code == 1
    assert f"tests removed, deleted or skipped without brief authorization: proj/{TREE}/{TESTS}" in output


def test_a_test_the_start_tree_had_is_a_change_not_an_omission(project, tmp_path):
    """Missing from the copy but present at start: it is judged as the change it is, not as never copied."""
    (project / TREE / TESTS).unlink()
    start_copy = tmp_path / "start/proj" / TREE / TESTS
    start_copy.parent.mkdir(parents=True)
    start_copy.write_text(UPSTREAM_TEST)
    code, output = _fail_to_pass(tmp_path, ["proj/own.py"])
    assert "tests removed" not in output
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code == 1
    assert f"tests removed, deleted or skipped without brief authorization: proj/{TREE}/{TESTS}" in output


def test_a_whole_copy_with_its_tests_removes_none(project, tmp_path):
    code, output = _fail_to_pass(tmp_path, ["proj/own.py"])
    assert "tests removed" not in output


def test_every_fail_to_pass_outcome_says_what_was_taken_as_upstream(project, tmp_path, upstream):
    code, output = _fail_to_pass(tmp_path, [f"proj/{TREE}/{TESTS}"])
    assert code is _gate_base.Status.NOT_APPLICABLE and "vendored.toml" in output
    root, _ = upstream
    _manifest(project, root, "abc")  # an invalid entry shows on an outcome that does not fail, too
    (project / "notes.md").write_text("notes\n")
    code, output = _fail_to_pass(tmp_path, ["proj/notes.md"])
    assert code is _gate_base.Status.NOT_APPLICABLE
    assert "commit 'abc' must be a full 40-hex sha" in output


def test_a_snapshot_never_fetches_or_creates_the_cache(project, tmp_path, monkeypatch):
    monkeypatch.setattr(_vendored, "_fetch_now", lambda *a, **k: pytest.fail("fetched"))
    found = _vendored.load(project, set(), fetch=False)
    assert not found.unchanged(f"{TREE}/{BIG}")
    assert "not fetched yet" in found.describe()
    assert not (tmp_path / "cache").exists()
    # A miss is not a failure: remembering it would hold off the gate's own fetch.
    assert _vendored._UNAVAILABLE == {}


def test_a_snapshot_selects_languages_without_fetching(django, tmp_path):
    project, root, commit = django
    _manifest(project, root, commit, path="repo")
    selected = _standards.select(template.FILES, _standards.Standards(), project, fetch=False)
    # Nothing fetched yet: the copy is judged as the project's, and the gate's own selection fetches.
    assert "languages/typescript.md" in selected
    assert not (tmp_path / "cache").exists()


def test_a_snapshot_reads_a_cache_the_gate_already_filled(project, upstream, tmp_path):
    root, _ = upstream
    assert _vendored.load(project, set()).problems == []
    root.rename(tmp_path / "gone")
    found = _vendored.load(project, set(), fetch=False)
    assert found.problems == [] and found.unchanged(f"{TREE}/{BIG}")


def test_a_forged_remote_tracking_branch_is_not_upstream(upstream, tmp_path):
    """A session can point `refs/remotes/origin/*` at its own commit; only the recorded base is trusted."""
    root, _ = upstream
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    base = _git(clone, "rev-parse", "HEAD")
    (clone / "proj/pkg").mkdir(parents=True)
    (clone / "proj/pkg/big.py").write_text("x = 1\n" * (LIMIT + 1))
    forged = _commit(clone)
    _git(clone, "update-ref", "refs/remotes/origin/HEAD", forged)
    _git(clone, "update-ref", "refs/remotes/origin/master", forged)
    _git(clone, "update-ref", "refs/remotes/origin/main", forged)
    _manifest(clone / "proj", root, forged, path="pkg", repo="https://example.invalid/x")
    found = _vendored.load(clone / "proj", {".git"}, trusted_base=base)
    assert not found.unchanged("pkg/big.py")
    assert "unavailable" in found.describe()
    work = _gate_tests.GateWork(trusted_base=base)
    code, output = _module_size.check(clone / "proj", tmp_path / "start", _gate_tests._is_test, work)
    assert code == 1 and "pkg/big.py" in output


def test_a_replace_ref_is_ignored(upstream, tmp_path):
    """`git replace` would make the trusted commit read as a tree the session wrote."""
    root, _ = upstream
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    base = _git(clone, "rev-parse", "HEAD")
    (clone / "src/forged.py").write_text("x = 1\n" * (LIMIT + 1))
    forged = _commit(clone)
    _git(clone, "reset", "-q", "--hard", base)
    _git(clone, "replace", base, forged)
    shutil.copytree(root / "src", clone / "proj" / TREE / "src")
    (clone / "proj" / TREE / "src/forged.py").write_text("x = 1\n" * (LIMIT + 1))
    _manifest(clone / "proj", root, base, repo="https://example.invalid/x")
    found = _vendored.load(clone / "proj", {".git"}, trusted_base=base)
    assert found.unchanged(f"{TREE}/{BIG}")
    assert not found.unchanged(f"{TREE}/src/forged.py")


def test_the_cache_is_not_a_promisor(project, upstream):
    """A promisor remote would fetch any missing object on demand, mid-rule and unbounded."""
    root, _ = upstream
    assert _vendored.load(project, set()).problems == []
    cache = _vendored._cache_dir_for(root.as_uri())
    config = subprocess.run(
        ["git", f"--git-dir={cache}", "config", "--list", "--local"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.lower()
    assert "promisor" not in config and "partialclone" not in config


def test_the_supervisor_warms_the_cache_its_snapshots_read(project, upstream, tmp_path, monkeypatch):
    """The gate fetches off the event loop; the standards selection on the loop then only reads the cache."""
    root, _ = upstream
    bases: list[str] = []

    def trusted_base(workspace: Path, name: str) -> str:
        bases.append(name)
        return ""

    monkeypatch.setattr(_session_config, "_project_dir", lambda: project)
    monkeypatch.setattr(_gates, "_trusted_base", trusted_base)
    _session_config.warm_vendored_cache()
    assert bases  # a project with a manifest resolves the gate's trusted base
    root.rename(tmp_path / "gone")
    assert _vendored.load(project, set(), fetch=False).unchanged(f"{TREE}/{BIG}")
    (project / _vendored.MANIFEST).unlink()
    bases.clear()
    assert _session_config._vendored_base() == "" and not bases


# -- end to end --


def test_the_standards_gate_judges_only_the_projects_change(project, tmp_path):
    rules = {"engineering.md": "## SIZE-1 MUST\nEnforced: check factory:module-size\n"}

    def size() -> _gates.Result:
        [result] = _gates.standards_results(project, rules, configs=tmp_path, start=tmp_path / "start")
        return result

    assert size().status is _gate_base.Status.PASS
    with (project / TREE / BIG).open("a") as handle:
        handle.write("x = 1\n")
    failed = size()
    assert failed.status is _gate_base.Status.FAIL
    assert f"{BIG_LINES + 1} lines, was {BIG_LINES}" in failed.output


def test_every_project_is_told_how_to_declare_a_vendored_tree(monkeypatch):
    # Vendored copies are not only for projects that deliver to a target repo.
    monkeypatch.setattr(_session_config, "_project_brief", "An ordinary brief.\n")
    memory = _instructions._project_memory(False, name="proj", layout="sandbox")
    assert "`proj/vendored.toml`" in memory and "no waiver is needed" in memory


# -- failures of git and of the manifest's shape --


@pytest.mark.parametrize("failure", [OSError("git is gone"), subprocess.TimeoutExpired("git", 60)])
def test_upstream_content_git_cannot_read_is_unknown_not_a_crash(project, monkeypatch, failure):
    found = _vendored.load(project, set())
    assert found.read_upstream(f"{TREE}/{TESTS}") == UPSTREAM_TEST.encode()

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", broken)
    assert found.read_upstream(f"{TREE}/{TESTS}") is None


def test_a_cache_git_cannot_initialise_is_an_error_with_gits_words(upstream, monkeypatch):
    root, commit = upstream
    real = _vendored._git

    def git(args, *a, **k):
        return (1, "fatal: cannot init\n") if "init" in args else real(args, *a, **k)

    monkeypatch.setattr(_vendored, "_git", git)
    with pytest.raises(_gate_start._GateError, match="^fatal: cannot init$"):
        _vendored._fetch_now(root.as_uri(), commit)


@pytest.mark.parametrize(
    ("said", "message"), [("fatal: no filter\n", "fatal: no filter"), ("", "fetch exited 1")]
)
def test_a_fetch_of_the_default_branch_history_that_fails_says_why(upstream, monkeypatch, said, message):
    root, commit = upstream
    real = _vendored._git

    def git(args, *a, **k):
        return (1, said) if "--filter=tree:0" in args else real(args, *a, **k)

    monkeypatch.setattr(_vendored, "_git", git)
    with pytest.raises(_gate_start._GateError) as error:
        _vendored._fetch_now(root.as_uri(), commit)
    assert str(error.value) == message


def test_a_commit_git_cannot_list_is_an_error(upstream):
    root, _ = upstream
    with pytest.raises(_gate_start._GateError, match="not a tree object"):
        _vendored._list_blobs([f"--git-dir={root / '.git'}"], root, "0" * 40)


def test_a_tree_that_is_not_an_array_of_tables_is_a_problem(project):
    (project / "vendored.toml").write_text('tree = "flyte-agent-factory"\n')
    found = _vendored.load(project, set())
    assert found.problems == ["vendored.toml: `tree` must be an array of tables ([[tree]])"]


def test_an_entry_with_a_field_that_is_not_a_string_is_a_problem_and_owns_nothing(project, upstream):
    root, commit = upstream
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = 7\nrepo = "{root.as_uri()}"\ncommit = "{commit}"\n'
        f'\n[[tree]]\npath = "{TREE}"\nrepo = "{root.as_uri()}"\ncommit = "{commit}"\n'
    )
    found = _vendored.load(project, set())
    assert found.problems == ["vendored.toml tree 1: `path`, `repo` and `commit` must all be strings"]
    # The valid entry after it is still honoured.
    assert found.unchanged(f"{TREE}/{BIG}")
