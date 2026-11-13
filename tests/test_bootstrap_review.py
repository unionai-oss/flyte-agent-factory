"""Behaviour the round-1 review asked for in the guided-setup bootstrap, one test per finding."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _bootstrap
from flyte_agent_factory._bootstrap_base import APP_LOST, BootstrapError
from tests._bootstrap_fakes import CLAUDE_TOKEN, World, make_context

ROOT = Path(__file__).resolve().parents[1]
ARGS = [
    "--endpoint", "flyte.example.test", "--project", "my-agents",
    "--workspace", "pat/agent-workspace", "--state-uri", "s3://example-bucket/state",
]  # fmt: skip


@pytest.fixture(autouse=True)
def person(monkeypatch, tmp_path_factory):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Pat Example")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "pat@example.com")
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def run(world, *extra, args=ARGS):
    out: list[str] = []
    code = _bootstrap.main(
        [*args, "--dir", str(world.root / "clone"), *extra],
        make_context=lambda settings: make_context(world, settings, out, CLAUDE_TOKEN),
    )
    return code, out


def squashed(text: str) -> str:
    return " ".join(text.split())


# B1: one constant says what --no-app costs, and every place that describes it quotes that constant.
def test_the_help_and_the_documents_quote_what_no_app_loses(capsys):
    with pytest.raises(SystemExit):
        _bootstrap.main(["--help"])
    assert APP_LOST in squashed(capsys.readouterr().out)
    for doc in ("docs/quickstart.md", "docs/github-app.md", "skills/agent-factory/references/setup.md"):
        assert APP_LOST in squashed((ROOT / doc).read_text()), doc


# B3: the chat-operator page says what the skill does at takeover and at every check-in.
def test_chat_operator_page_describes_takeover_and_check_ins_as_the_skill_does():
    page = squashed((ROOT / "docs/chat-operator.md").read_text())
    skill = squashed((ROOT / "skills/agent-factory/SKILL.md").read_text())
    for fact in (
        '"Factory check-in"',
        "every 30 to 60 minutes",
        "operator/STATE.md",
        "does nothing and does not reschedule itself",
        "any chat takes over",
        "idempotent",
        "retires the",
        "factory.queue_list",
        "factory.status",
        "open pull requests and their CI",
        "factory-deployer.deploy_if_changed",
        "live sources win",
        "records this chat as the operator",
        "schedules its own check-in",
        "`/since`-style summary",
        "Agent Factory YYYY-MM-DD",
        "` (retired)`",
    ):
        assert fact in page, fact
    for fact in ("Factory check-in", "30 to 60 minutes", "live sources win", "Agent Factory YYYY-MM-DD"):
        assert fact in skill, fact  # the page quotes what the skill does


def test_the_page_says_one_change_marks_both_zips_stale():
    page = squashed((ROOT / "docs/chat-operator.md").read_text())
    assert "a change to either skill marks both zips stale" in page
    assert "PYTHONPATH" not in page


# B4: `--engine codex` ends at engine-token with one command that works before any factory exists.
def test_codex_engine_prints_one_filled_in_command_for_the_login_secret(world):
    code, out = run(world, "--engine", "codex")
    assert code == 3
    line = next(line for line in out if line.startswith("[todo] engine-token"))
    assert "codex login --device-auth" in line
    assert "create secret codex_login_agent_factory" in line
    assert "-p my-agents -d development" in line and "--from-file" in line
    command = line.split("run this again: ", 1)[1]
    assert subprocess.run(["bash", "-n", "-c", command]).returncode == 0
    assert 'rm -rf "$d"' in command  # the login file never outlives the command


# B5: the endpoint is compared by host, whatever form it was given in.
@pytest.mark.parametrize(
    "given", ["https://flyte.example.test", "https://flyte.example.test/", "dns:///flyte.example.test"]
)
def test_an_endpoint_given_as_a_url_resumes_after_the_config_was_written(world, given):
    assert run(world)[0] == 0
    swapped = [given if arg == "flyte.example.test" else arg for arg in ARGS]
    code, out = run(world, args=swapped)
    assert code == 0, out
    assert any(line.startswith("[skip] signin") for line in out)


def test_a_config_for_another_host_is_not_accepted_when_one_contains_the_other(world):
    # A Flyte config left from another factory, for a host that contains this one, and no profile yet.
    (world.root / "flyte-config.yaml").write_text("admin:\n  endpoint: dns:///flyte.example.test\n")
    parent = ["example.test" if arg == "flyte.example.test" else arg for arg in ARGS]
    code, out = run(world, args=parent)
    assert code == 3
    assert any(line.startswith("[blocked] signin") and "points at" in line for line in out), out


def test_a_rerun_for_another_host_is_blocked_by_the_committed_profile(world):
    assert run(world)[0] == 0
    parent = ["example.test" if arg == "flyte.example.test" else arg for arg in ARGS]
    code, out = run(world, args=parent)
    assert code == 3
    # The committed profile names the host too, and its step comes before the sign-in: it blocks first,
    # naming the host it has.
    assert any(
        line.startswith("[blocked] profile") and "endpoint flyte.example.test" in line for line in out
    ), out


@pytest.mark.parametrize(
    ("given", "host"),
    [
        ("flyte.example.com", "flyte.example.com"),
        ("HTTPS://Flyte.Example.com/console?x=1", "flyte.example.com"),
        ("dns:///flyte.example.com:443", "flyte.example.com:443"),
        ("localhost:30080", "localhost:30080"),
    ],
)
def test_the_endpoint_is_reduced_to_its_host(given, host):
    from flyte_agent_factory._bootstrap_base import normalise_endpoint

    assert normalise_endpoint(given) == host


# B6: no machine without flyteplugins-union can make the key; every place says so in the same words.
def test_the_missing_key_message_and_the_documents_state_the_same_platform_requirement(world):
    from flyte_agent_factory._bootstrap_base import API_KEY_REQUIREMENT

    world.api_key = False
    code, out = run(world)
    assert code == 3
    line = next(line for line in out if line.startswith("[blocked] deploy"))
    assert API_KEY_REQUIREMENT in line and "Linux x86_64" in line
    assert "_UNION_EAGER_API_KEY" in line and "Local Union deployment bootstrap" in line
    for doc in (
        "docs/quickstart.md",
        "skills/agent-factory/references/setup.md",
        "docs/factory-mcp-setup.md",
    ):
        assert API_KEY_REQUIREMENT in squashed((ROOT / doc).read_text()), doc
    assert "on Union, nothing:" not in (ROOT / "docs/quickstart.md").read_text()


# B7: every subprocess in the bootstrap is bounded, and a timeout reads as a plain message.
def _calls_without_timeout(path: Path) -> list[str]:
    import ast

    bad = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target in ("subprocess.run", "self._run", "self._call") and not any(
            kw.arg == "timeout" for kw in node.keywords
        ):
            bad.append(f"{path.name}:{node.lineno} {target}")
    return bad


def test_every_subprocess_call_in_the_bootstrap_passes_a_timeout():
    src = ROOT / "src/flyte_agent_factory"
    files = [*src.glob("_bootstrap*.py"), src / "_workspace.py"]
    bad = [call for path in files for call in _calls_without_timeout(path)]
    # `_call` takes its timeout as a parameter with a default, so its own `self._run(...)` passes one.
    assert bad == [call for call in bad if "self._call" in call and "_bootstrap_github" in call], bad


def test_a_gh_command_that_hangs_ends_as_a_clear_error():
    from flyte_agent_factory import _bootstrap_github

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    gh = _bootstrap_github.GhCli(run=hang)
    for call in (gh.authenticated, lambda: gh.repo_visibility("pat/ws"), lambda: gh.app_exists("pat", "s")):
        with pytest.raises(BootstrapError, match="did not answer within"):
            call()


def test_a_flyte_config_command_that_hangs_ends_as_a_clear_error(tmp_path, monkeypatch):
    from flyte_agent_factory._bootstrap_base import Settings
    from flyte_agent_factory._bootstrap_flyte import FlyteSdk

    def hang(cmd, **kwargs):
        assert kwargs["timeout"] > 0
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    sdk = FlyteSdk(Settings("h", "p", "o/r", "s3://b/p"), tmp_path / "config.yaml")
    with pytest.raises(BootstrapError, match="did not finish within"):
        sdk.create_config()


def test_ssh_keygen_that_hangs_ends_as_a_clear_error(world, monkeypatch):
    real = subprocess.run

    def hang(cmd, **kwargs):
        if cmd[0] == "ssh-keygen":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return real(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", hang)
    code, out = run(world, "--no-app")
    assert code == 3 and any(
        line.startswith("[blocked] deploy-key") and "ssh-keygen did not finish" in line for line in out
    ), out


def test_the_identity_check_is_bounded(monkeypatch, tmp_path):
    from flyte_agent_factory import _workspace

    monkeypatch.delenv("GIT_AUTHOR_NAME")

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="timed out"):
        _workspace.require_identity(tmp_path)


# B8: a skill that cannot be read is reported with exit 2, never a traceback.
def test_check_reports_a_corrupt_zip_a_binary_file_and_an_unreadable_file(tmp_path, capsys, monkeypatch):
    import zipfile

    from ops import package_skill

    package_skill.build(tmp_path)
    good = (tmp_path / "agent-factory.zip").read_bytes()
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(
        good.replace(b"Skill version: ", b"Skill versioN: ")
    )  # the bytes change, the CRC does not
    assert zipfile.is_zipfile(corrupt)
    assert package_skill.main(["--check", str(corrupt)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "BadZipFile" in err

    binary = tmp_path / "SKILL.md"
    binary.write_bytes(b"\xff\xfe\x00not utf-8 \x80")
    assert package_skill.main(["--check", str(binary)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "UnicodeDecodeError" in err

    unreadable = tmp_path / "other.md"
    unreadable.write_text("x")
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("no")))
    assert package_skill.main(["--check", str(unreadable)]) == 2
    assert "PermissionError" in capsys.readouterr().err


def test_the_check_command_exits_2_with_an_error_line_on_a_corrupt_zip(tmp_path):
    from ops import package_skill

    package_skill.build(tmp_path)
    bad = tmp_path / "bad.zip"
    bad.write_bytes((tmp_path / "since.zip").read_bytes().replace(b"Skill version: ", b"Skill versioN: "))
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ops/package_skill.py"), "--check", str(bad)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2 and proc.stderr.startswith("error: ") and "Traceback" not in proc.stderr


# B9: no function in the bootstrap's files keeps a noun-only name from before the review.
def test_the_functions_the_review_named_are_verb_phrases_now():
    import ast

    old = {
        "_api_key", "_installed", "_left", "_elsewhere", "wanted", "lacking", "_filled", "_app_id", "_slug",
        "_missing", "_names", "_state", "_config", "_line", "_parser", "_slots", "_table", "_value", "_page",
        "_handler", "_template_paths", "_recorded_keys", "_ruleset_id", "_key_lost", "profile_digest",
        "install_url", "registration_url", "ruleset_body", "app_name", "app_id_of", "app_keys_present",
        "mcp_url_set", "callback_code", "steps", "manifest", "source_digest", "source_files", "skill_names",
        "installed_version",
    }  # fmt: skip
    files = [*(ROOT / "src/flyte_agent_factory").glob("_bootstrap*.py")]
    files += [ROOT / "ops" / name for name in ("bootstrap.py", "package_skill.py", "new_workspace.py")]
    left = {
        f"{path.name}:{node.name}"
        for path in files
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name in old
    }
    assert not left, left


# N1: new_workspace.py runs from a fresh clone without PYTHONPATH.
def test_new_workspace_runs_without_pythonpath(tmp_path):
    env = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "ops/new_workspace.py"), str(tmp_path / "ws")],
        capture_output=True, text=True, env=env, cwd="/",
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-500:]
    assert (tmp_path / "ws/profile.example.toml").is_file()
    assert "at the workspace root" in proc.stdout
    for doc in ("docs/quickstart.md", "docs/setup.md"):
        assert "PYTHONPATH=src uv run python ops/new_workspace.py" not in (ROOT / doc).read_text()


# N2, N3: the install audit lists the unrun real-account rows; the move onto [direction] has both commands.
def test_the_install_audit_lists_the_real_account_rows_as_not_run():
    page = (ROOT / "docs/install-audit.md").read_text()
    todo = page.split("## Still to prove externally", 1)[1].split("These steps are", 1)[0]
    for item in ("ruleset", "board", "Default repository", "App installed", "`[direction]`"):
        assert any(item in line and "NOT RUN" in line for line in todo.splitlines()), item
    assert "needs cleanup" not in page


def test_moving_onto_direction_points_at_the_one_redeploy_command():
    # The move is the bootstrap's --redeploy run, which deploys with --profile <clone>/profile.toml;
    # the docs give that one filled-in command rather than the two hand deploy scripts.
    for doc in ("docs/quickstart.md", "skills/agent-factory/references/setup.md"):
        text = " ".join((ROOT / doc).read_text().split())
        move = text[text.index("Moving an") :]
        assert "`[direction]`" in move[:80] and "`--redeploy`" in move[:400], doc
        assert "ops/deploy_factory.py --profile" not in text, doc
        assert "--redeploy" in text and "ops/bootstrap.py --endpoint" in text, doc


# N4: --status on a fresh install says the repository-dependent steps wait on it.
def test_status_on_a_fresh_install_says_board_and_deploy_key_wait_on_the_repository(world):
    code, out = run(world, "--status")
    assert code == 3
    for step in ("deploy-key", "ruleset", "board"):
        line = next(line for line in out if f"] {step}:" in line)
        assert line.startswith("[todo]") and "waits on the repository pat/agent-workspace" in line, line
    assert "Could not resolve" not in "\n".join(out)
    assert world.events == []


# N5
def test_the_template_readme_start_section_names_both_skills():
    readme = (ROOT / "src/flyte_agent_factory/_assets/workspace_template/README.md").read_text()
    start = readme.split("## Start", 1)[1].split("\n## ", 1)[0]
    assert "`agent-factory`" in start and "`since`" in start


# N6: a failure while clearing old keys shows its own error; unexpected errors are not swallowed.
def test_a_failed_cleanup_of_old_keys_reports_its_own_error(monkeypatch, tmp_path):
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from tests.test_bootstrap_repair import _deployer, _engine_root, _install_fake_union, _UnionServer

    server = _UnionServer()
    _install_fake_union(monkeypatch, server)
    clone = _engine_root(tmp_path)
    subprocess.run(
        ["git", "config", "--local", "--add", "agent-factory.deploy-api-key", "old-client"],
        cwd=clone,
        check=True,
    )
    server.keys["old-client"] = "x"
    import sys as _sys

    key_class = _sys.modules["flyteplugins.union.remote"].ApiKey

    def unavailable(cls, client_id):
        raise ConnectError(Code.UNAVAILABLE, "connection reset")

    monkeypatch.setattr(key_class, "delete", classmethod(unavailable))
    with pytest.raises(ConnectError, match="connection reset"):
        _deployer(clone).deploy(clone / "profile.toml")


def test_a_key_this_tool_did_not_record_is_never_deleted(monkeypatch, tmp_path):
    from tests.test_bootstrap_repair import _deployer, _engine_root, _install_fake_union, _UnionServer

    server = _UnionServer()
    server.keys["agent-factory-deploy"] = "the person's own"
    _install_fake_union(monkeypatch, server)
    with pytest.raises(BootstrapError, match="delete the key in Union by hand"):
        _deployer(_engine_root(tmp_path)).deploy(tmp_path / "profile.toml")
    assert server.deleted == [] and "agent-factory-deploy" in server.keys


def test_a_bug_is_not_reported_as_an_expected_failure():
    from flyte_agent_factory._bootstrap_base import EXPECTED_ERRORS

    expected: tuple[type[BaseException], ...] = EXPECTED_ERRORS
    assert KeyError not in expected and ValueError not in expected


def test_a_reply_gh_gives_that_is_not_json_is_a_bootstrap_error():
    from flyte_agent_factory import _bootstrap_github

    gh = _bootstrap_github.GhCli(
        run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="<html>proxy</html>", stderr="")
    )
    with pytest.raises(BootstrapError, match="other than JSON"):
        gh.deploy_key_titles("pat/ws")
    with pytest.raises(BootstrapError):
        gh.exchange_manifest_code("abc")


# N7: dead code is gone.
def test_dead_code_is_gone():
    from flyte_agent_factory import _bootstrap_base, _bootstrap_flyte, _bootstrap_github

    assert not hasattr(_bootstrap_github.GhCli, "repo_exists")
    assert "repo_exists" not in _bootstrap_base.GitHubOps.__dict__
    assert not hasattr(_bootstrap_base, "OK") and not hasattr(_bootstrap_flyte, "_LIST_ALL")


# N8: the board message counts what it lists.
def test_the_board_message_counts_its_clicks_correctly(world):
    code, out = run(world)
    assert code == 0
    line = next(line for line in out if line.startswith("[done] board"))
    assert "three clicks remain" in line and "two clicks" not in line
    for click in ("Default repository", "Projects tab", "built-in workflows"):
        assert click in line


# N9: the existing board is looked for beyond gh's first page.
def test_the_board_lookup_asks_for_more_than_the_first_page_of_projects():
    from flyte_agent_factory import _bootstrap_github

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["project", "list"]:
            out = '{"projects": [{"title": "agent-workspace backlog", "number": 7, "url": "https://x/7"}]}'
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    url = _bootstrap_github.GhCli(run=fake_run).create_board(
        "pat", "agent-workspace", "agent-workspace backlog"
    )
    listing = next(c for c in calls if c[1:3] == ["project", "list"])
    assert int(listing[listing.index("--limit") + 1]) >= 1000
    assert url == "https://x/7" and not any(c[1:3] == ["project", "create"] for c in calls)


# N10: a read-only run never starts the sign-in.
def test_a_read_only_run_does_not_open_the_browser_when_there_is_no_cached_login(tmp_path, monkeypatch):
    from flyte.remote._client.auth._keyring import KeyringStore

    from flyte_agent_factory._bootstrap_base import NotSignedIn, Settings
    from flyte_agent_factory._bootstrap_flyte import FlyteSdk

    config = tmp_path / "config.yaml"
    config.write_text("admin:\n  endpoint: dns:///flyte.example.test\n")
    monkeypatch.setattr(KeyringStore, "retrieve", staticmethod(lambda endpoint, disable=False: None))
    import flyte

    monkeypatch.setattr(flyte, "init_from_config", lambda *a, **k: pytest.fail("the SDK was started"))
    settings = Settings("flyte.example.test", "p", "o/r", "s3://b/p", status_only=True)
    with pytest.raises(NotSignedIn, match="read-only"):
        FlyteSdk(settings, config).sign_in()
    # a client-secret config never opens a browser, so it is not held back
    config.write_text("admin:\n  endpoint: dns:///h\n  authType: ClientSecret\n")
    assert FlyteSdk(settings, config)._can_sign_in_without_a_browser()


def test_status_does_not_sign_in(world):
    assert run(world)[0] == 0
    world.signed_in = False
    before = list(world.events)
    code, out = run(world, "--status")
    assert code == 3 and world.events == before
    assert any(line.startswith("[todo] signin") and "not signed in" in line for line in out), out


# N11: a key directory that a live bootstrap uses is left alone.
def test_a_key_directory_of_a_running_bootstrap_survives_the_sweep(tmp_path):
    import os

    from flyte_agent_factory._bootstrap_steps import _remove_stale_key_dirs

    live = tmp_path / f".agent-factory-key-{os.getpid()}-abc"
    dead = tmp_path / ".agent-factory-key-2147483646-abc"
    unnamed = tmp_path / ".agent-factory-key-abc123_x"
    for path in (live, dead, unnamed):
        path.mkdir(mode=0o700)
    _remove_stale_key_dirs(tmp_path)
    assert live.is_dir() and not dead.exists() and not unnamed.exists()


# The deploy watchdog stops the whole process group: a deploy script's own child (a hung `git fetch`) that
# keeps the output pipe open must not hold the bootstrap past the bound.
def test_the_deploy_watchdog_stops_the_scripts_children_too(monkeypatch, tmp_path):
    import time

    from flyte_agent_factory import _bootstrap_flyte

    (tmp_path / "ops").mkdir()
    hung = "import subprocess, time\nsubprocess.Popen(['sleep', '30'])\ntime.sleep(30)\n"
    (tmp_path / "ops" / "deploy_factory.py").write_text(hung)
    monkeypatch.setattr(_bootstrap_flyte, "_DEPLOY_SECONDS", 1)
    deployer = _bootstrap_flyte.DeployScripts(env={}, engine_root=tmp_path, say=lambda line: None)
    started = time.monotonic()
    with pytest.raises(BootstrapError, match="stopped after 1s"):
        deployer._run_script("deploy_factory.py", tmp_path / "profile.toml", {})
    assert time.monotonic() - started < 10
