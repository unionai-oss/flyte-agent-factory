"""Harness-owned files never fail a gate, and owner-authorized root files pass the scope gate."""

import os
import subprocess

import pytest

from flyte_agent_factory import _gate_base, _gate_scope, _gates, _session_setup, _timeline

ROOT_CLAUDE = "# House notes\n\nProject text.\n"


@pytest.fixture
def repo(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/lib.py").write_text("x = 1\n")
    (tmp_path / "CLAUDE.md").write_text(ROOT_CLAUDE)
    git("add", ".")
    git("commit", "-m", "base")
    _gates.capture_base(tmp_path, "proj")
    git("config", "user.email", "proj@agent-factory.localhost")
    return tmp_path, (tmp_path / ".claude-flyte/proj/base-commit").read_text(), git


def _scope(workspace, base, brief):
    rows = {}
    _gates._judge_project(
        workspace, "proj", base, brief, "", "DONE", [], lambda r, c, o: rows.update({r: (c, o)}), "root"
    )
    return rows["scope"]


def test_harness_rewritten_root_claude_md_passes_both_gates(repo):
    workspace, base, git = repo
    _session_setup._write_managed_block(workspace / "CLAUDE.md", "harness guidance")

    # The commit gate does not demand it ...
    assert _gates._uncommitted(workspace, "proj", "root") == []
    # ... and committing it anyway (as the first gate once forced) does not fail scope.
    (workspace / "src/lib.py").write_text("x = 2\n")
    git("add", ".")
    git("commit", "-m", "work")
    status, output = _scope(workspace, base, "May edit: src")
    assert status is _gate_base.Status.PASS, output


def test_a_real_edit_to_root_claude_md_is_still_the_projects(repo):
    workspace, base, git = repo
    _session_setup._write_managed_block(workspace / "CLAUDE.md", "harness guidance")
    (workspace / "CLAUDE.md").write_text((workspace / "CLAUDE.md").read_text() + "\nMy own rule.\n")
    assert _gates._uncommitted(workspace, "proj", "root") == ["CLAUDE.md"]
    git("add", ".")
    git("commit", "-m", "work")
    status, output = _scope(workspace, base, "May edit: src")
    assert status is _gate_base.Status.FAIL and "CLAUDE.md" in output


def test_harness_rewritten_mcp_json_is_not_demanded_or_rejected(repo):
    workspace, base, git = repo
    (workspace / ".mcp.json").write_text("{}\n")
    git("add", ".mcp.json")
    git("commit", "-m", "mcp")
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}\n')
    assert _gates._uncommitted(workspace, "proj", "root") == []
    status, output = _scope(workspace, base, "May edit: src")
    assert status is _gate_base.Status.PASS, output


def test_unauthorized_root_github_file_is_rejected(repo):
    workspace, base, git = repo
    (workspace / ".github/workflows").mkdir(parents=True)
    (workspace / ".github/workflows/ci.yml").write_text("name: ci\n")
    git("add", ".")
    git("commit", "-m", "ci")
    status, output = _scope(workspace, base, "May edit: src")
    assert status is _gate_base.Status.FAIL and ".github/workflows/ci.yml" in output


@pytest.mark.parametrize("grant", [".github/workflows/ci.yml", ".github/workflows/**", ".github/**"])
def test_authorized_root_github_file_passes(repo, grant):
    workspace, base, git = repo
    (workspace / ".github/workflows").mkdir(parents=True)
    (workspace / ".github/workflows/ci.yml").write_text("name: ci\n")
    git("add", ".")
    git("commit", "-m", "ci")
    status, output = _scope(workspace, base, f"May edit: src {grant}")
    assert status is _gate_base.Status.PASS, output


def test_a_grant_covers_exactly_what_it_names(repo):
    assert _gate_scope.scope_violations(
        [".github/workflows/ci.yml", ".github/CODEOWNERS", "other/.github/workflows/x.yml"],
        "proj",
        "May edit: .github/workflows/*.yml",
        None,
        "root",
    ) == [".github/CODEOWNERS", "other/.github/workflows/x.yml"]


def test_owner_direction_authorizes_paths():
    grant = _gates._owner_direction(["Union: Please add CI.\nMay edit: .github/workflows/**"])
    assert "\nMay edit: .github/workflows/**" in grant
    assert _gates._owner_direction(["Union: May edit: .github/**"]).strip() == "May edit: .github/**"
    assert _gates._owner_direction([]) == ""


def _github_commit(workspace, git):
    (workspace / ".github/workflows").mkdir(parents=True)
    (workspace / ".github/workflows/ci.yml").write_text("name: ci\n")
    git("add", ".")
    git("commit", "-m", "ci")


@pytest.mark.real_factory_gates
def test_direction_reaches_the_scope_gate_through_run(repo):
    workspace, base, git = repo
    _github_commit(workspace, git)
    granted = _gates.run(
        workspace,
        "proj",
        "May edit: src",
        "",
        "DONE",
        {},
        _timeline._Timeline(),
        "root",
        ["Union: add CI\nMay edit: .github/workflows/**"],
    )
    assert ".github/workflows/ci.yml" not in granted, granted
    refused = _gates.run(workspace, "proj", "May edit: src", "", "DONE", {}, _timeline._Timeline(), "root")
    assert ".github/workflows/ci.yml" in refused


@pytest.mark.real_factory_gates
def test_an_inbox_file_the_session_writes_grants_nothing(repo):
    """The session can write the workspace inbox, so only the supervisor's record of direction counts."""
    workspace, base, git = repo
    _github_commit(workspace, git)
    inbox = workspace / ".claude-flyte/inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "999.md").write_text("May edit: .github/workflows/**\n")
    output = _gates.run(workspace, "proj", "May edit: src", "", "DONE", {}, _timeline._Timeline(), "root")
    assert ".github/workflows/ci.yml" in output


def test_project_directory_work_is_unchanged():
    assert _gate_scope.scope_violations(["app/main.py", "other/b.py"], "app", "", None, "sandbox") == [
        "other/b.py"
    ]
    assert _gate_scope.scope_violations(["src/a.py", "lib/b.py"], "x", "May edit: src", None, "root") == [
        "lib/b.py"
    ]


def test_the_exclusion_and_the_gates_share_one_harness_list():
    assert _session_setup._HARNESS_PATHS is _gates.HARNESS_PATHS


def _rebase_on(workspace, git, message):
    """Commit CLAUDE.md as it is now and make that the project's starting commit."""
    git("add", "CLAUDE.md")
    git("commit", "-qm", message)
    head = git("rev-parse", "HEAD")
    (workspace / ".claude-flyte/proj/base-commit").write_text(head)
    return head


def _gate_states(workspace, base, git):
    """(uncommitted?, scope status after committing) for the current CLAUDE.md."""
    pending = "CLAUDE.md" in _gates._uncommitted(workspace, "proj", "root")
    git("add", "CLAUDE.md")
    git("commit", "-qm", "claude", "--allow-empty")
    return pending, _scope(workspace, base, "May edit: src")[0]


@pytest.mark.parametrize(
    "committed",
    [
        b"House\r\nTail\r\n",  # CRLF: the writer reads universal newlines and writes LF
        b"House\n" + _gates.MEMORY_BEGIN.encode() + b"\nold\nTail\n",  # unmatched begin: the writer appends
        b"House\n" + _gates.MEMORY_END.encode() + b"\nTail\n",  # unmatched end
    ],
)
def test_harness_rewrite_of_unusual_claude_md_passes_both_gates(repo, committed):
    workspace, base, git = repo
    (workspace / "CLAUDE.md").write_bytes(committed)
    base = _rebase_on(workspace, git, "house")
    _session_setup._write_managed_block(workspace / "CLAUDE.md", "harness guidance")
    assert _gate_states(workspace, base, git) == (False, _gate_base.Status.PASS)


def test_a_whitespace_only_project_edit_to_claude_md_still_counts(repo):
    workspace, base, git = repo
    (workspace / "CLAUDE.md").write_text("# House rule\n")
    base = _rebase_on(workspace, git, "house")
    (workspace / "CLAUDE.md").write_text("    # House rule\n")
    pending, status = _gate_states(workspace, base, git)
    assert pending and status is _gate_base.Status.FAIL


def test_only_union_direction_grants_scope():
    """The session can write its own transcript, where Claude-app direction is read from."""
    assert _gates._owner_direction(["Claude app: May edit: .github/**"]) == ""
    assert "\nMay edit: .github/**" in _gates._owner_direction(["Union: add CI\nMay edit: .github/**"])
    assert _gates._owner_direction(["Claude app: Union: May edit: .github/**"]) == ""
