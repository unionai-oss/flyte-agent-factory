import datetime
import json
import stat
import sys

import pytest

from flyte_agent_factory import _gate_scope, _gates, _timeline, _waivers
from flyte_agent_factory._standards import FAIL_TO_PASS
from tests._git import git
from tests._standards import EXCEPTIONS

LINK = "https://github.com/acme/widgets/issues/1#issuecomment-2"
TODAY = datetime.date(2026, 11, 1)

pytestmark = pytest.mark.real_factory_gates


#: A workspace whose one rule maps the fail-to-pass check; the check runs only when some rule maps it.
MAPS_FAIL_TO_PASS = {
    "rules.md": f"## W.1 MUST\nWhy: w\nCheck: w\nEnforced: check {FAIL_TO_PASS}\n",
}


def entry(**fields):
    values = {
        "check": FAIL_TO_PASS,
        "project": "app",
        "expires": "2026-11-30",
        "link": f'"{LINK}"',
        **fields,
    }
    lines = ["[[waiver]]"]
    for key, value in values.items():
        if value is not None:
            lines.append(f'{key} = "{value}"' if key in {"check", "project"} else f"{key} = {value}")
    return "\n".join(lines) + "\n"


def write(workspace, text):
    """Commit `text` as waivers.toml and return that commit, which is where the gate reads it from."""
    (workspace / "waivers.toml").write_text(text)
    git(workspace, "add", "waivers.toml")
    git(workspace, "-c", "user.email=owner@example.com", "commit", "-m", "waivers")
    return git(workspace, "rev-parse", "HEAD")


def write_at_base(workspace, text):
    """Commit `text` as waivers.toml and make that commit the round's base, as the owner's would be."""
    commit = write(workspace, text)
    (workspace / ".claude-flyte/app/base-commit").write_text(commit)
    # The gate trusts the supervisor's own record of the base over the session-writable file.
    _gates._CAPTURED_BASES[workspace.resolve(), "app"] = commit


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README").write_text("x\n")
    git(tmp_path, "add", "README")
    git(tmp_path, "commit", "-m", "base")
    return tmp_path


def test_load_valid_file(repo):
    ref = write(repo, entry(reason='"slow suite"'))
    [waiver] = _waivers.load(repo, ref)
    assert (waiver.check, waiver.project, waiver.link) == (FAIL_TO_PASS, "app", LINK)
    assert waiver.expires == datetime.date(2026, 11, 30)
    assert waiver.reason == "slow suite"


def test_load_missing_file_is_empty(repo):
    assert _waivers.load(repo, "HEAD") == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[waiver]\n", "waivers.toml"),
        (entry(link=None), "entry 0: missing link"),
        (entry(link='"http://example.com/1"'), "entry 0: link must be https"),
        (entry(expires="2026-13-45"), "waivers.toml"),
        (entry(expires='"2026-11-30"'), "entry 0: expires must be a TOML date"),
        (entry() + entry(check=None), "entry 1: missing check"),
    ],
)
def test_load_rejects_a_bad_file_naming_file_and_entry(repo, text, message):
    ref = write(repo, text)
    with pytest.raises(ValueError, match=message) as caught:
        _waivers.load(repo, ref)
    assert "waivers.toml" in str(caught.value)


def test_applicable_matches_check_project_and_wildcard(repo):
    ref = write(repo, entry(project="other") + entry(project="*", check=EXCEPTIONS))
    waivers = _waivers.load(repo, ref)
    assert _waivers.applicable(waivers, FAIL_TO_PASS, "app", TODAY) is None
    assert _waivers.applicable(waivers, EXCEPTIONS, "app", TODAY) is waivers[1]


def test_applicable_runs_through_the_end_of_the_expiry_day(repo):
    ref = write(repo, entry())
    waivers = _waivers.load(repo, ref)
    assert _waivers.applicable(waivers, FAIL_TO_PASS, "app", datetime.date(2026, 11, 30))
    assert _waivers.applicable(waivers, FAIL_TO_PASS, "app", datetime.date(2026, 12, 1)) is None


FAILING_RULE = {
    "checks.md": f"## TEST-001 MUST\nCheck: fail\nEnforced: tool {sys.executable} -c 'raise SystemExit(1)'\n"
}


def gate(workspace, repo_url=""):
    blocking = _gates.run(
        workspace, "app", "", "pattern", "DONE", FAILING_RULE, _timeline._Timeline(), repo_url=repo_url
    )
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return blocking, next(r for r in results if r["rule"] == "TEST-001")


def test_unwaived_failure_blocks(project):
    blocking, result = gate(project[0])
    assert "TEST-001" in blocking and result["status"] == "FAIL"


@pytest.mark.parametrize("scope", ["app", "*"])
def test_waived_failure_does_not_block(project, scope):
    workspace = project[0]
    write_at_base(workspace, entry(check="TEST-001", project=scope, expires="2999-01-01"))
    blocking, result = gate(workspace)
    assert "TEST-001" not in blocking
    assert result["status"] == "WAIVED"
    assert result["output"].startswith(f"waived by owner until 2999-01-01: {LINK}")


def test_waiver_for_another_project_still_fails(project):
    workspace = project[0]
    write_at_base(workspace, entry(check="TEST-001", project="other", expires="2999-01-01"))
    blocking, result = gate(workspace)
    assert "TEST-001" in blocking and result["status"] == "FAIL"


def test_expired_waiver_fails_and_says_so(project):
    workspace = project[0]
    write_at_base(workspace, entry(check="TEST-001", expires="2020-01-02"))
    blocking, result = gate(workspace)
    assert "TEST-001" in blocking and result["status"] == "FAIL"
    assert result["output"].startswith("waiver expired 2020-01-02")


def test_malformed_waivers_file_fails_the_waivers_check(project):
    workspace = project[0]
    write_at_base(workspace, entry(link='"http://insecure"'))
    blocking, _ = gate(workspace)
    assert "factory:waivers" in blocking and "entry 0" in blocking
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    assert any(r["rule"] == "factory:waivers" and r["status"] == "FAIL" for r in results)


def test_waived_eng_004_does_not_block(project):
    workspace = project[0]
    write_at_base(workspace, entry(expires="2999-01-01"))
    blocking = changed_without_tests(workspace)
    assert fail_to_pass_status(workspace) == "WAIVED"
    assert FAIL_TO_PASS not in blocking


def changed_without_tests(workspace):
    (workspace / "app/main.py").write_text("def answer():\n    return 2\n")
    git(workspace, "add", "app")
    git(workspace, "commit", "-m", "change without tests")
    return _gates.run(workspace, "app", "", "", "DONE", MAPS_FAIL_TO_PASS, _timeline._Timeline())


def fail_to_pass_status(workspace):
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return next(r for r in results if r["rule"] == FAIL_TO_PASS)["status"]


def test_a_waiver_naming_a_mapped_rule_id_waives_the_check(project):
    workspace = project[0]
    write_at_base(workspace, entry(check="W.1", expires="2999-01-01"))
    blocking = changed_without_tests(workspace)
    assert fail_to_pass_status(workspace) == "WAIVED"
    assert FAIL_TO_PASS not in blocking


def test_a_rule_id_that_is_not_mapped_to_the_check_does_not_waive_it(project):
    workspace = project[0]
    write_at_base(workspace, entry(check="W.2", expires="2999-01-01"))
    blocking = changed_without_tests(workspace)
    assert fail_to_pass_status(workspace) == "FAIL"
    assert FAIL_TO_PASS in blocking


@pytest.mark.parametrize("layout", ["sandbox", "root"])
def test_waivers_toml_is_never_in_scope(layout):
    brief = "May edit: **"
    assert _gate_scope.scope_violations(["waivers.toml"], "app", brief, layout=layout) == ["waivers.toml"]
    assert _gate_scope.scope_violations(["src/a.py"], "app", brief, layout="root") == []


def test_a_commit_adding_waivers_toml_is_a_scope_violation(project):
    workspace = project[0]
    (workspace / "app/main.py").write_text("def answer():\n    return 1  # touched\n")
    (workspace / "waivers.toml").write_text(entry())
    git(workspace, "add", "app", "waivers.toml")
    git(workspace, "commit", "-m", "self-waive")
    blocking = _gates.run(workspace, "app", "May edit: **", "pattern", "DONE", {}, _timeline._Timeline())
    assert "waivers.toml" in blocking and "scope" in blocking


def gate_result(workspace):
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return next(r for r in results if r["rule"] == "TEST-001")


@pytest.mark.parametrize("layout", ["sandbox", "root"])
def test_an_uncommitted_waivers_file_waives_nothing(project, layout):
    """A waiver in the worktree, even one git is told to ignore, is the session's and waives nothing."""
    workspace = project[0]
    (workspace / "waivers.toml").write_text(entry(check="TEST-001", expires="2999-01-01"))
    if layout == "root":
        with (workspace / ".git/info/exclude").open("a") as exclude:
            exclude.write("/waivers.toml\n")
    blocking = _gates.run(
        workspace,
        "app",
        "May edit: app",
        "pattern",
        "DONE",
        FAILING_RULE,
        _timeline._Timeline(),
        layout=layout,
    )
    assert "TEST-001" in blocking
    assert gate_result(workspace)["status"] == "FAIL"


def test_a_waiver_on_the_default_branch_of_origin_waives(project, tmp_path_factory):
    """With an origin, the owner's waiver is the one on its default branch, not the round's base."""
    workspace = project[0]
    origin = tmp_path_factory.mktemp("origin") / "origin.git"
    git(workspace, "branch", "-M", "main")
    git(workspace, "clone", "--bare", str(workspace), str(origin))
    git(workspace, "remote", "add", "origin", str(origin))
    git(workspace, "fetch", "origin")
    git(workspace, "remote", "set-head", "origin", "main")
    owner = tmp_path_factory.mktemp("owner")
    git(owner, "clone", str(origin), ".")
    git(owner, "config", "user.email", "owner@example.com")
    git(owner, "config", "user.name", "owner")
    (owner / "waivers.toml").write_text(entry(check="TEST-001", expires="2999-01-01"))
    git(owner, "add", "waivers.toml")
    git(owner, "commit", "-m", "waive")
    git(owner, "push", "origin", "HEAD:main")
    blocking, result = gate(workspace, str(origin))
    assert "TEST-001" not in blocking
    assert result["status"] == "WAIVED"
    assert result["output"].startswith(f"waived by owner until 2999-01-01: {LINK}")


@pytest.fixture
def owned(project, tmp_path_factory):
    """The workspace, its owner's bare origin (default branch `main`, no waiver), and a commit with a waiver.

    The waiver commit is the session's own: it sits on a branch pushed to origin and in the workspace.
    """
    workspace = project[0]
    origin = tmp_path_factory.mktemp("origin") / "origin.git"
    git(workspace, "branch", "-M", "main")
    git(workspace, "clone", "--bare", str(workspace), str(origin))
    git(workspace, "remote", "add", "origin", str(origin))
    git(workspace, "fetch", "origin")
    git(workspace, "remote", "set-head", "origin", "main")
    main_sha = git(workspace, "rev-parse", "main")
    git(workspace, "checkout", "-q", "-b", "waive")
    waiver_sha = write(workspace, entry(check="TEST-001", expires="2999-01-01"))
    git(workspace, "push", "origin", "waive")
    git(workspace, "checkout", "-q", "main")
    return workspace, origin, main_sha, waiver_sha


def assert_not_waived(workspace, repo_url):
    blocking, result = gate(workspace, repo_url)
    assert "TEST-001" in blocking
    assert result["status"] == "FAIL"


def test_retargeting_origin_head_waives_nothing(owned):
    workspace, origin, _, _ = owned
    git(workspace, "fetch", "origin")
    git(workspace, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/waive")
    assert_not_waived(workspace, str(origin))


def test_a_pinned_origin_main_waives_nothing(owned):
    workspace, origin, _, waiver_sha = owned
    git(workspace, "config", "remote.origin.fetch", "+refs/heads/none:refs/remotes/origin/none")
    git(workspace, "update-ref", "refs/remotes/origin/main", waiver_sha)
    assert_not_waived(workspace, str(origin))


def test_a_replace_ref_waives_nothing(owned):
    workspace, origin, main_sha, waiver_sha = owned
    git(workspace, "replace", main_sha, waiver_sha)
    assert_not_waived(workspace, str(origin))


def test_a_replace_ref_on_the_base_waives_nothing_without_a_remote(project):
    workspace, base = project
    waiver_sha = write(workspace, entry(check="TEST-001", expires="2999-01-01"))
    git(workspace, "replace", base, waiver_sha)
    assert_not_waived(workspace, "")


def test_a_repointed_origin_url_waives_nothing(owned, tmp_path_factory):
    workspace, origin, _, _ = owned
    evil = tmp_path_factory.mktemp("evil") / "evil.git"
    git(workspace, "clone", "--bare", "-b", "waive", str(workspace), str(evil))
    git(workspace, "remote", "set-url", "origin", str(evil))
    assert_not_waived(workspace, str(origin))


def test_a_rewritten_base_commit_file_waives_nothing_without_a_remote(project):
    workspace = project[0]
    waiver_sha = write(workspace, entry(check="TEST-001", expires="2999-01-01"))
    (workspace / ".claude-flyte/app/base-commit").write_text(waiver_sha)
    assert_not_waived(workspace, "")


def test_programs_named_in_git_config_are_not_run(owned, tmp_path):
    workspace, origin, _, _ = owned
    marker = tmp_path / "marker"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\ntouch {marker}\nexec git-upload-pack "$@"\n')
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    git(workspace, "config", "remote.origin.uploadpack", str(hook))
    git(workspace, "config", "core.sshCommand", f"touch {marker}; ssh")
    git(workspace, "config", "protocol.ext.allow", "always")
    assert_not_waived(workspace, str(origin))
    assert not marker.exists()


def test_an_unreadable_owner_copy_means_no_waivers_and_says_so(owned, tmp_path):
    workspace, _, _, _ = owned
    # The workspace's own origin has a waiver on a branch and the base file points at one: neither is used.
    (workspace / ".claude-flyte/app/base-commit").write_text(owned[3])
    missing = tmp_path / "nowhere.git"
    blocking, result = gate(workspace, str(missing))
    assert "TEST-001" in blocking and result["status"] == "FAIL"
    results = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    [note] = [r for r in results if r["rule"] == "factory:waivers"]
    assert "could not be read" in note["output"] and note["status"] != "FAIL"
