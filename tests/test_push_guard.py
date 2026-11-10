"""A session-configured checkout pushes to its workspace and to nothing else."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _push_guard
from flyte_agent_factory._grant_seal import GrantError
from flyte_agent_factory._grants import TOKEN_ENV, URL_ENV, GrantEnded, GrantServer
from tests.test_grants import SECRET, TOKEN, make_store, seal_for


def git(
    path: Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    return subprocess.run(
        cmd, cwd=path, capture_output=True, text=True, check=check, env={**os.environ, **(env or {})}
    )


@pytest.fixture
def session(tmp_path):
    """A checkout of the workspace (a bare repo) with the guard; a bare repo stands in for the engine."""
    workspace, engine = tmp_path / "workspace.git", tmp_path / "engine.git"
    for bare in (workspace, engine):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", "-q", str(workspace), str(checkout))
    git(checkout, "checkout", "-q", "-b", "main")
    (checkout / "a.txt").write_text("a\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "a")
    _push_guard.install(checkout, str(workspace))
    return checkout, workspace, engine


def test_push_to_the_workspace_succeeds(session):
    checkout, workspace, _ = session
    git(checkout, "push", "-q", "origin", "main")
    assert (
        git(workspace, "rev-parse", "main").stdout.strip()
        == git(checkout, "rev-parse", "main").stdout.strip()
    )


def test_push_to_another_url_is_refused(session):
    checkout, _, engine = session
    result = git(checkout, "push", str(engine), "main", check=False)
    assert result.returncode != 0 and "not the workspace repository" in result.stderr
    assert git(engine, "branch", "--list").stdout == ""


def test_push_through_a_second_remote_is_refused(session):
    checkout, _, engine = session
    git(checkout, "remote", "add", "engine", str(engine))
    assert git(checkout, "push", "engine", "main", check=False).returncode != 0
    assert git(engine, "branch", "--list").stdout == ""


def test_a_bare_push_goes_to_the_workspace(session):
    checkout, workspace, _ = session
    assert git(checkout, "config", "remote.pushDefault").stdout.strip() == "origin"
    git(checkout, "push", "-q", "origin", "main")
    (checkout / "b.txt").write_text("b\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "b")
    git(checkout, "push", "-q", "origin", "main")
    assert git(workspace, "log", "--format=%s", "main").stdout.split() == ["b", "a"]


def test_a_checkout_without_the_remote_gets_it_and_the_guard(tmp_path):
    """A resumed workspace restored without its `origin` still ends up pushing only to the workspace."""
    workspace, engine = tmp_path / "workspace.git", tmp_path / "engine.git"
    for bare in (workspace, engine):
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "-q", "-b", "main", str(checkout))
    (checkout / "a.txt").write_text("a\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "a")
    _push_guard.install(checkout, str(workspace))
    assert git(checkout, "remote", "get-url", "origin").stdout.strip() == str(workspace)
    assert git(checkout, "push", "-q", str(engine), "main", check=False).returncode != 0
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0


def test_no_verify_does_not_skip_the_guard(session):
    """`--no-verify` skips the hook, so the refusal must also come from git config."""
    checkout, _, engine = session
    result = git(checkout, "push", "--no-verify", str(engine), "HEAD:main", check=False)
    assert result.returncode != 0
    assert git(engine, "branch", "--list").stdout == ""


def test_no_verify_through_a_second_remote_is_refused(session):
    checkout, _, engine = session
    git(checkout, "remote", "add", "engine", str(engine))
    assert git(checkout, "push", "--no-verify", "engine", "HEAD:main", check=False).returncode != 0
    assert git(engine, "branch", "--list").stdout == ""


def test_no_verify_with_a_file_url_and_a_relative_path_is_refused(session):
    checkout, _, engine = session
    for url in (f"file://{engine}", str(Path("..") / engine.name)):
        result = git(checkout, "push", "--no-verify", url, "HEAD:main", check=False)
        assert result.returncode != 0, url
    assert git(engine, "branch", "--list").stdout == ""


def test_no_verify_push_to_the_workspace_still_works(session):
    checkout, workspace, _ = session
    git(checkout, "push", "-q", "--no-verify", "origin", "main")
    assert (
        git(workspace, "rev-parse", "main").stdout.strip()
        == git(checkout, "rev-parse", "main").stdout.strip()
    )


def test_fetch_from_the_engine_still_works(session):
    checkout, _, engine = session
    seed = engine.parent / "seed"
    git(engine.parent, "clone", "-q", str(engine), str(seed))
    (seed / "e.txt").write_text("e\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "e")
    git(seed, "push", "-q", "origin", "HEAD:main")
    git(checkout, "fetch", "-q", str(engine), "main")
    assert git(checkout, "log", "--format=%s", "FETCH_HEAD").stdout.split() == ["e"]


def test_install_twice_keeps_the_guard(session):
    checkout, workspace, engine = session
    _push_guard.install(checkout, str(workspace))
    assert git(checkout, "push", "--no-verify", str(engine), "HEAD:main", check=False).returncode != 0
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0


def test_an_scp_style_engine_url_resolves_to_the_refusal_and_the_workspace_to_itself(tmp_path):
    """GitHub's `git@host:org/repo.git` form is what real remotes use; only the workspace's survives."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git(checkout, "init", "-q")
    workspace = "git@github.com:acme/agent-workspace.git"
    git(checkout, "remote", "add", "origin", workspace)
    git(checkout, "remote", "add", "engine", "git@github.com:acme/flyte-agent-factory.git")
    _push_guard.install(checkout, workspace)
    assert git(checkout, "remote", "get-url", "--push", "origin").stdout.strip() == workspace
    assert "not the workspace repository" in git(checkout, "remote", "get-url", "--push", "engine").stdout


def commit(checkout: Path, name: str, content: str, message: str = "add") -> None:
    (checkout / name).write_text(content)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", message)


def test_a_commit_holding_an_aws_credential_shape_is_not_pushed(session):
    checkout, workspace, _ = session
    git(checkout, "push", "-q", "origin", "main")
    before = git(workspace, "rev-parse", "main").stdout
    commit(checkout, "creds.txt", f"aws_secret_access_key = {SECRET}\nAWS_SESSION_TOKEN={TOKEN}\n")
    short = git(checkout, "rev-parse", "--short=12", "HEAD").stdout.strip()
    result = git(checkout, "push", "origin", "main", check=False)
    assert result.returncode != 0
    assert short in result.stderr and "credential" in result.stderr and "of its diff" in result.stderr
    assert SECRET not in result.stderr and TOKEN not in result.stderr
    assert git(workspace, "rev-parse", "main").stdout == before
    commit(checkout, "creds.txt", "nothing here\n")  # a later commit does not hide the earlier one
    assert git(checkout, "push", "origin", "main", check=False).returncode != 0
    git(checkout, "reset", "-q", "--hard", "HEAD~2")
    commit(checkout, "creds.txt", "nothing here\n")
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0


def test_a_commit_message_holding_a_credential_shape_is_not_pushed(session):
    checkout, workspace, _ = session
    git(checkout, "push", "-q", "origin", "main")
    before = git(workspace, "rev-parse", "main").stdout
    commit(checkout, "b.txt", "b\n", f"fix\n\naws_secret_access_key={SECRET}")
    result = git(checkout, "push", "origin", "main", check=False)
    assert result.returncode != 0 and "commit message" in result.stderr and SECRET not in result.stderr
    assert git(workspace, "rev-parse", "main").stdout == before


def test_a_commit_holding_a_live_granted_value_is_not_pushed(session):
    checkout, workspace, _ = session
    store, _ = make_store()
    server = GrantServer(store)
    server.start()
    try:
        store.accept(seal_for(store))
        commit(checkout, "notes.txt", f"{TOKEN}\n")  # no field name, so only the live value gives it away
        result = git(checkout, "push", "origin", "main", check=False, env=server.env())
        assert result.returncode != 0 and "granted credential value" in result.stderr
        assert TOKEN not in result.stderr and SECRET not in result.stderr
        assert git(workspace, "branch", "--list").stdout == ""
        store.revoke("aws-marketplace")
        assert git(checkout, "push", "-q", "origin", "main", check=False, env=server.env()).returncode == 0
    finally:
        server.stop()


def test_a_fixture_key_id_alone_is_pushed(session):
    checkout, _, _ = session
    commit(checkout, "fixture.txt", "ASIA" + "ABCDEFGHIJKLMNOP\naws_session_token = abc123\n")
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0


def serve_two_grants(fetch_old) -> tuple[GrantServer, Any]:
    """A server holding `aws-old` (fetched through `fetch_old`) and `aws-marketplace`, both listed active."""
    store, _ = make_store()
    store.accept(seal_for(store, name="aws-old"))
    store.accept(seal_for(store))
    real = store.aws_response
    store.aws_response = lambda name: fetch_old(name) if name == "aws-old" else real(name)  # type: ignore[method-assign]
    server = GrantServer(store)
    server.start()
    return server, store


def test_one_grant_ending_mid_scan_does_not_turn_off_the_scan_for_the_others(session):
    checkout, workspace, _ = session

    def ended(name: str) -> dict[str, str]:
        raise GrantEnded(f"credential {name} ended: expired at 2027-01-15T08:00:00Z")

    server, _ = serve_two_grants(ended)
    try:
        commit(checkout, "notes.txt", f"{TOKEN}\n")  # aws-marketplace's value, no field name
        result = git(checkout, "push", "origin", "main", check=False, env=server.env())
        assert result.returncode != 0 and "granted credential value" in result.stderr
        assert "aws-old" in result.stderr and TOKEN not in result.stderr
        assert git(workspace, "branch", "--list").stdout == ""
    finally:
        server.stop()


def test_a_grant_that_cannot_be_read_refuses_the_push(session):
    checkout, workspace, _ = session

    def unreadable(name: str) -> dict[str, str]:
        raise GrantError(f"no credential named {name} has been granted to this project")

    server, _ = serve_two_grants(unreadable)
    try:
        commit(checkout, "b.txt", "nothing secret\n")
        result = git(checkout, "push", "origin", "main", check=False, env=server.env())
        assert result.returncode != 0 and "push refused" in result.stderr and "aws-old" in result.stderr
        assert TOKEN not in result.stderr and SECRET not in result.stderr
        assert git(workspace, "branch", "--list").stdout == ""
    finally:
        server.stop()


def test_a_grant_server_that_does_not_answer_refuses_the_push(session):
    checkout, workspace, _ = session
    env = {URL_ENV: "http://127.0.0.1:9", TOKEN_ENV: "t"}
    result = git(checkout, "push", "origin", "main", check=False, env=env)
    assert result.returncode != 0 and "push refused" in result.stderr and "did not answer" in result.stderr
    assert git(workspace, "branch", "--list").stdout == ""


def test_a_commit_git_cannot_read_refuses_the_push(session, monkeypatch, capsys):
    checkout, _, _ = session
    monkeypatch.chdir(checkout)
    monkeypatch.delenv(URL_ENV, raising=False)
    missing = "1" * 40
    stdin = f"refs/heads/main {missing} refs/heads/main {'0' * 40}\n"
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin.encode())))
    assert _push_guard.scan_main() == 1
    assert "push refused" in capsys.readouterr().err


def make_runtime_fake_secret() -> str:
    """A secret-shaped value built at run time, so no literal in this file looks like a credential."""
    return "".join(["wJalrXUtnFEMI", "/K7MDENG/bPxRfiCYz", "RUNTIMEFAKE0"])


@pytest.mark.parametrize(
    "content",
    [
        "++aws_secret_access_key={secret}\n",  # the patch line is +++aws_secret_access_key=..., not a header
        "+++ aws_secret_access_key={secret}\n",
        "++ b/x\n+++ b/y\naws_secret_access_key={secret}\n",
        "+++ b/a.txt\n@@ -1 +1 @@\n--- a/a.txt\n++aws_secret_access_key={secret}\n",
        "diff --git a/x b/x\n+++ b/x\n--- a/x\n@@ -1,2 +1,2 @@\nAWS_SECRET_ACCESS_KEY={secret}\n",
    ],
)
def test_an_added_line_starting_with_plus_signs_is_still_scanned(session, content):
    checkout, workspace, _ = session
    secret = make_runtime_fake_secret()
    commit(checkout, "c.txt", content.format(secret=secret))
    result = git(checkout, "push", "origin", "main", check=False)
    assert result.returncode != 0 and "of its diff" in result.stderr and secret not in result.stderr
    assert git(workspace, "branch", "--list").stdout == ""


def test_a_credential_in_a_second_hunk_after_header_like_content_is_found(session):
    checkout, _, _ = session
    secret = make_runtime_fake_secret()
    body = "".join(f"line {i}\n" for i in range(30))
    commit(checkout, "c.txt", body)
    git(checkout, "push", "-q", "origin", "main")
    edited = body.replace("line 1\n", "+++ b/c.txt\n")
    edited = edited.replace("line 28\n", f"++aws_secret_access_key={secret}\n")
    commit(checkout, "c.txt", edited)
    result = git(checkout, "push", "origin", "main", check=False)
    assert result.returncode != 0 and secret not in result.stderr


def test_safe_file_headers_and_header_like_content_do_not_refuse_the_push(session):
    checkout, workspace, _ = session
    commit(checkout, "c.txt", "+++ b/c.txt\n--- a/c.txt\n++ plain\n@@ -1 +1 @@\n")
    (checkout / "a.txt").write_text("changed\n")  # a modified file: real ---/+++ headers
    (checkout / "d.txt").write_text("new\n")  # a new file: --- /dev/null
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "more")
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0
    assert git(workspace, "rev-parse", "main").stdout == git(checkout, "rev-parse", "HEAD").stdout


def run_hook(checkout: Path, workspace: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    hook = checkout / ".git" / "hooks" / "pre-push"
    return subprocess.run(
        [str(hook), "origin", str(workspace)], cwd=checkout, input=stdin, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "line",
    [
        "refs/heads/main",
        f"refs/heads/main {'1' * 40} refs/heads/main",
        f"refs/heads/main {'1' * 40} refs/heads/main {'0' * 40} extra",
        f"{'0' * 40}",
    ],
)
def test_a_malformed_pre_push_line_refuses_without_echoing_it(session, line):
    checkout, workspace, _ = session
    result = run_hook(checkout, workspace, f"{line}\n")
    assert result.returncode == 1
    assert result.stderr.startswith("push refused:") and len(result.stderr.strip().splitlines()) == 1
    assert line not in result.stderr


def test_a_malformed_line_after_a_valid_one_still_refuses(session):
    checkout, workspace, _ = session
    head = git(checkout, "rev-parse", "HEAD").stdout.strip()
    stdin = f"refs/heads/main {head} refs/heads/main {'0' * 40}\nbroken\n"
    assert run_hook(checkout, workspace, stdin).returncode == 1


def test_pre_push_input_that_is_not_utf8_refuses_cleanly_without_echoing_it(session):
    checkout, workspace, _ = session
    hook = checkout / ".git" / "hooks" / "pre-push"
    stdin = b"refs/heads/main \xff\xfe" + b"1" * 38 + b" refs/heads/main " + b"0" * 40 + b"\n"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8:strict"}
    result = subprocess.run(
        [str(hook), "origin", str(workspace)], cwd=checkout, input=stdin, capture_output=True, env=env
    )
    assert result.returncode == 1
    err = result.stderr.decode("utf-8", "replace")
    assert err.startswith("push refused:") and len(err.strip().splitlines()) == 1
    assert "Traceback" not in err and "xff" not in err and "\ufffd" not in err


def test_a_deletion_line_still_succeeds(session):
    checkout, workspace, _ = session
    result = run_hook(checkout, workspace, f"(delete) {'0' * 40} refs/heads/main {'1' * 40}\n")
    assert result.returncode == 0 and result.stderr == ""


@pytest.mark.parametrize("separator", ["\f", "\v", "\r", "\x1c", "\x85", "\u2028", "\u2029", "\r\r"])
@pytest.mark.parametrize("lookalike", ["+", "-", " ", "+++ b/x"])
def test_a_credential_after_a_line_separator_inside_an_added_line_is_still_found(
    session, separator, lookalike
):
    """Only LF ends a physical diff line; other separators must not shift the hunk's line count."""
    checkout, workspace, _ = session
    secret = make_runtime_fake_secret()
    content = f"first{separator}{lookalike}\nsecond{separator}{lookalike}\naws_secret_access_key={secret}\n"
    commit(checkout, "c.txt", content)
    result = git(checkout, "push", "origin", "main", check=False)
    assert result.returncode != 0 and "of its diff" in result.stderr and secret not in result.stderr
    assert git(workspace, "branch", "--list").stdout == ""


def test_separators_inside_added_lines_do_not_refuse_a_clean_push(session):
    checkout, workspace, _ = session
    commit(checkout, "c.txt", "one\f+two\v-three\r four\u2028five\n")
    assert git(checkout, "push", "-q", "origin", "main", check=False).returncode == 0
    assert git(workspace, "rev-parse", "main").stdout == git(checkout, "rev-parse", "HEAD").stdout
