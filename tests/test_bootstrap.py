"""The guided-setup bootstrap, run against fakes: every step, resuming, read-only modes, secrets, OSS."""

from __future__ import annotations

import importlib.util
import re
import runpy
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import cast

import pytest

from flyte_agent_factory import _bootstrap, _bootstrap_github, _profile
from flyte_agent_factory._bootstrap_base import (
    APP_LOST,
    BootstrapError,
    Context,
    Settings,
    Step,
    run_captured,
    write_to_terminal,
)
from flyte_agent_factory._bootstrap_flyte import DeployScripts, FlyteSdk
from tests import _bootstrap_fakes as fakes
from tests._bootstrap_fakes import (
    APP_PEM,
    CLAUDE_TOKEN,
    FakeGitHub,
    World,
    make_context,
)

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
    # A home of its own per test: the deploy-key step makes its key directory in the home directory and
    # sweeps up the ones a killed run left there, so a shared home lets parallel tests delete each other's.
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def run(world, *extra, token=CLAUDE_TOKEN):
    """One bootstrap run; return (exit code, printed lines)."""
    out: list[str] = []
    code = _bootstrap.main(
        [*ARGS, "--dir", str(world.root / "clone"), *extra],
        make_context=lambda settings: make_context(world, settings, out, token),
    )
    return code, out


def statuses(out):
    return {
        line.split("] ", 1)[1].split(":", 1)[0]: line[1 : line.index("]")] for line in out if line[:1] == "["
    }


def remote_log(world):
    return subprocess.run(
        ["git", "log", "--format=%an|%s", "main"],
        cwd=world.bare("pat/agent-workspace"),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()


def test_first_run_does_every_step_in_order(world):
    code, out = run(world)
    assert code == 0, "\n".join(out)
    status = statuses(out)
    assert list(status) == [s.id for s in _bootstrap.build_steps()]
    assert status["tools"] == "skip"
    for step in ("signin", "project", "workspace", "template", "ruleset", "deploy-key", "engine-token"):
        assert status[step] == "done", out
    for step in ("github-app", "board", "profile", "deploy", "mcp-url", "connect"):
        assert status[step] == "done", out
    assert {"agent_factory_claude_token", "agent_factory_deploy_key", "agent_factory_github_app_key"} == set(
        world.secrets
    )
    assert world.secrets["agent_factory_github_app_key"] == APP_PEM
    assert world.secrets["agent_factory_deploy_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "agent-factory" in world.deploy_keys and "agent-workspace-main" in world.rulesets
    # Pushed, and by the person: no placeholder identity anywhere in the history.
    log = remote_log(world)
    assert log and {entry.split("|")[0] for entry in log} == {"Pat Example"}
    assert any("profile.toml" in entry for entry in log)
    profile = _profile.load(str(world.root / "clone/profile.toml"))
    assert profile.mcp is not None and profile.mcp.public_url == "https://factory-mcp.example.test"
    assert any("connector URL https://factory-mcp.example.test/flyte-mcp/mcp" in line for line in out)


def test_a_second_run_changes_nothing(world):
    assert run(world)[0] == 0
    before = list(world.events)
    code, out = run(world)
    assert code == 0, out
    assert world.events == before
    status = statuses(out)
    assert {v for k, v in status.items() if k != "connect"} == {"skip"}


def test_an_interruption_at_any_change_is_resumed_without_duplicates(world, tmp_path):
    probe = _events_of_a_full_run(tmp_path / "probe")
    total = len(probe)
    assert total > 10
    for at in range(1, total + 1):
        w = World(tmp_path / f"w{at}")
        w.root.mkdir()
        w.interrupt_at = at
        code, out = run(w)
        assert code == 3 and any("Interrupted" in line for line in out), (at, out)
        w.interrupt_at = None
        code, out = run(w)
        key_lost = probe[at - 1] in ("exchange_manifest_code", "create_secret agent_factory_github_app_key")
        if key_lost:
            # The one window with no way back: GitHub makes the App when the person submits the form and
            # hands its key over once, so a crash between the two, or before the key is stored, loses it.
            # The rerun must not register a second App (GitHub refuses the name): it names the App and the
            # page to delete it on, and once the person has, the next run registers afresh.
            assert code == 3 and statuses(out)["github-app"] == "blocked", (at, out)
            assert "https://github.com/settings/apps/agent-factory-pat/advanced" in out[-1], (at, out)
            assert w.count("register_app") == 1
            w.apps.clear()
            code, out = run(w)
        assert code == 0, (at, out)
        creates = [e for e in w.events if e.startswith(("create_", "register_app", "deploy"))]
        # One of each kind of thing, except the factory deploys (work+factory, then factory again for the
        # URL).
        once = [e for e in creates if not e.startswith("deploy")]
        if key_lost:
            once.remove("register_app")
            assert w.count("register_app") == 2
        assert len(once) == len(set(once)), (at, [e for e in once if once.count(e) > 1])
        assert w.count("create_secret") == 3, (at, w.events)
        assert w.count("add_deploy_key") - w.count("delete_deploy_key") == 1, (at, w.events)


def _events_of_a_full_run(root):
    root.mkdir()
    w = World(root)
    assert run(w)[0] == 0
    return w.events


def test_status_is_read_only_and_reports_each_check(world):
    code, out = run(world, "--status")
    assert code == 3 and world.events == []
    assert statuses(out)["tools"] == "done" and statuses(out)["signin"] == "todo"
    assert not (world.root / "clone").exists()
    assert run(world)[0] == 0
    before = list(world.events)
    code, out = run(world, "--status")
    assert code == 0 and world.events == before
    assert set(statuses(out).values()) == {"done", "skip"}


def test_dry_run_prints_the_plan_and_changes_nothing(world):
    code, out = run(world, "--dry-run")
    assert code == 3 and world.events == []
    assert not (world.root / "clone").exists()
    text = "\n".join(out)
    assert "would create the private repository pat/agent-workspace" in text
    assert "would register your GitHub App" in text


def test_oss_does_everything_but_the_deploy_and_says_what_is_missing(world):
    code, out = run(world, "--backend", "oss", "--no-app")
    assert code == 3
    status = statuses(out)
    for step in ("signin", "project", "workspace", "template", "deploy-key", "engine-token", "profile"):
        assert status[step] in ("done", "skip"), out
    assert status["deploy"] == "blocked" and "mcp-url" not in status
    text = "\n".join(out)
    for phrase in ("API keys", "remote image builder", "MCP authentication", "docs/oss-flyte-compat.md"):
        assert phrase in text
    assert world.count("deploy") == 0
    assert _profile.load(str(world.root / "clone/profile.toml")).backend.kind == "oss"


def test_no_app_and_no_board_does_not_say_the_board_is_kept(world):
    code, out = run(world, "--no-app", "--no-board")
    line = next(entry for entry in out if "] github-app:" in entry)
    assert code == 0 and APP_LOST in line and "--no-board" in line


def test_no_app_skips_the_app_and_says_what_is_lost(world):
    code, out = run(world, "--no-app")
    assert code == 0, out
    assert world.count("register_app") == 0 and "agent_factory_github_app_key" not in world.secrets
    assert any(line.startswith("[skip] github-app") and "lost" in line for line in out)
    lost = next(line for line in out if line.startswith("[skip] github-app"))
    for item in (
        "CI visibility",
        "draft pull requests",
        "factory/verifier",
        "auto-deploy",
        "[direction]",
        "issue intake",
    ):
        assert item in lost, lost
    text = (world.root / "clone/profile.toml").read_text()
    assert "[github]" not in text and "[direction]" not in text
    _profile.load(str(world.root / "clone/profile.toml"))


def test_the_written_profile_validates_with_the_app_and_points_the_deployer_at_itself(world):
    assert run(world)[0] == 0
    profile = _profile.load(str(world.root / "clone/profile.toml"))
    assert profile.name == "agent-workspace"
    assert profile.github.app_id == "424242" and profile.deploy.auto
    assert (
        profile.direction is not None and profile.direction.repo == "git@github.com:pat/agent-workspace.git"
    )
    assert profile.direction.profile == "profile.toml"
    assert profile.repo == "git@github.com:pat/agent-workspace.git"


def test_codex_engine_profile_validates(world):
    code, out = run(world, "--engine", "codex")
    assert code == 3 and statuses(out)["engine-token"] == "todo" and "Codex login" in "\n".join(out)
    world.secrets["codex_login_agent_factory"] = "login-json"  # made by hand, as docs/secrets.md says
    code, out = run(world, "--engine", "codex")
    assert code == 0, out
    assert "codex_login" in (world.root / "clone/profile.toml").read_text()
    _profile.load(str(world.root / "clone/profile.toml"))


def test_a_resumed_profile_only_gains_missing_keys(world):
    assert run(world, "--no-app")[0] == 0
    path = world.root / "clone/profile.toml"
    text = path.read_text().replace('domain = "development"', 'domain = "staging"  # mine')
    text = text.replace("[mcp]\n", "[mcp]\n# my note\n")
    path.write_text(text)
    subprocess.run(["git", "commit", "-qam", "edit"], cwd=world.root / "clone", check=True)
    _bootstrap_profile = __import__("flyte_agent_factory._bootstrap_profile", fromlist=["x"])
    settings = _bootstrap._build_parser().parse_args(ARGS)
    from flyte_agent_factory._bootstrap_base import Settings

    s = Settings(settings.endpoint, settings.project, settings.workspace, settings.state_uri)
    added = _bootstrap_profile.fill_missing(path, s, "424242")
    assert "github.app_id" in added and "deploy.auto" in added
    result = path.read_text()
    assert 'domain = "staging"  # mine' in result and "# my note" in result
    assert tomllib.loads(result)["github"]["app_id"] == "424242"
    assert _bootstrap_profile.fill_missing(path, s, "424242") == []


def test_the_profile_is_created_exclusively_and_never_overwritten(world, tmp_path):
    from flyte_agent_factory import _bootstrap_profile as bp
    from flyte_agent_factory._bootstrap_base import Settings

    s = Settings("h.example.test", "p", "pat/agent-workspace", "s3://b/p")
    path = tmp_path / "profile.toml"
    path.write_text('name = "mine"\n')
    assert bp.write(path, s, None).startswith("added")
    assert path.read_text().startswith('name = "mine"\n')


def test_an_empty_token_is_a_todo_not_a_crash(world):
    code, out = run(world, token="")
    assert code == 3
    assert statuses(out)["engine-token"] == "todo" and "deploy" not in statuses(out)
    assert "agent_factory_claude_token" not in world.secrets
    assert out[-1].startswith("next: run `claude setup-token`")


def test_a_forbidden_ruleset_prints_the_settings_steps_and_the_run_goes_on(world):
    world.ruleset_forbidden = True
    code, out = run(world)
    assert code == 3
    status = statuses(out)
    assert status["ruleset"] == "todo" and status["deploy"] == "done"
    assert any("Settings > Rules > Rulesets" in line for line in out)


def test_a_board_without_the_project_scope_says_how_to_get_it(world):
    world.board_scope_missing = True
    code, out = run(world)
    assert code == 3 and statuses(out)["board"] in ("todo", "blocked")
    assert statuses(out)["deploy"] == "done"
    assert any("gh auth refresh -s project" in line for line in out)


def test_no_board_skips_the_board(world):
    code, out = run(world, "--no-board")
    assert code == 0 and world.board_linked is False
    assert any(line.startswith("[skip] board") for line in out)


def test_a_missing_deploy_api_key_stops_before_the_deploy_with_the_one_command(world):
    world.api_key = False
    code, out = run(world)
    assert code == 3 and statuses(out)["deploy"] == "blocked"
    assert world.count("deploy") == 0
    assert any("Local Union deployment bootstrap" in line for line in out)


def test_a_deploy_key_on_github_without_its_secret_is_replaced_not_duplicated(world):
    world.interrupt_at = None
    assert run(world, "--no-app")[0] == 0
    world.secrets.pop("agent_factory_deploy_key")
    code, out = run(world, "--no-app")
    assert code == 0, out
    assert world.count("delete_deploy_key") == 1 and list(world.deploy_keys) == ["agent-factory"]


def test_a_secret_without_its_deploy_key_is_blocked_not_rotated(world):
    assert run(world, "--no-app")[0] == 0
    world.deploy_keys.clear()
    code, out = run(world, "--no-app")
    assert code == 3 and statuses(out)["deploy-key"] == "blocked"
    text = next(line for line in out if line.startswith("next:"))
    assert "flyte delete secret agent_factory_deploy_key -p my-agents -d development" in text
    assert not re.search(r"<[^>]*>", text)


def test_the_deploy_key_directory_is_private_and_removed(world, monkeypatch):
    made: list[tuple[Path, int]] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = Path(real(*args, **kwargs))
        made.append((path, path.stat().st_mode & 0o777))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    assert run(world, "--no-app")[0] == 0
    assert [mode for _, mode in made] == [0o700] and not made[0][0].exists()


def test_a_failed_secret_store_removes_the_directory_and_the_public_key(world, monkeypatch):
    made: list[Path] = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        made.append(Path(real(*args, **kwargs)))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    world.secret_error = "agent_factory_deploy_key"
    code, out = run(world, "--no-app")
    assert code == 3 and statuses(out)["deploy-key"] == "blocked"
    assert world.deploy_keys == {} and made and not made[0].exists()
    world.secret_error = None
    assert run(world, "--no-app")[0] == 0


def test_no_secret_value_is_ever_printed(world, capsys):
    code, out = run(world)
    assert code == 0
    printed = "\n".join(out) + capsys.readouterr().out + capsys.readouterr().err
    for name, value in world.secrets.items():
        for line in value.splitlines():
            assert line.strip() == "" or line not in printed or len(line) < 12, name
    assert CLAUDE_TOKEN not in printed and "sentinel-app-pem" not in printed
    assert "BEGIN OPENSSH PRIVATE KEY" not in printed


def test_a_failing_secret_store_does_not_leak_the_value(world, monkeypatch):
    class Leaky(RuntimeError):
        pass

    real = make_context

    def with_leak(w, settings, out, token):
        ctx = real(w, settings, out, token)
        original = ctx.flyte.create_secret

        def create(name, value):
            original(name, value)
            raise Leaky(f"failed to store {name}")  # adapters never put the value in a message

        monkeypatch.setattr(ctx.flyte, "create_secret", create)
        return ctx

    out: list[str] = []
    code = _bootstrap.main(
        [*ARGS, "--dir", str(world.root / "clone")],
        make_context=lambda settings: with_leak(world, settings, out, CLAUDE_TOKEN),
    )
    assert code == 3 and CLAUDE_TOKEN not in "\n".join(out)


def test_the_real_flyte_adapter_reports_only_the_error_class(monkeypatch):
    from flyte import remote

    from flyte_agent_factory._bootstrap_base import Settings
    from flyte_agent_factory._bootstrap_flyte import FlyteSdk

    sdk = FlyteSdk(Settings("h", "p", "o/r", "s3://b/p"))
    sdk._ready = True

    def create(name, value):
        raise RuntimeError(f"request body was {value}")

    monkeypatch.setattr(remote.Secret, "create", create)
    with pytest.raises(BootstrapError) as raised:
        sdk.create_secret("s", "sentinel-value")
    assert "sentinel-value" not in str(raised.value) and raised.value.__cause__ is None


# --- the manifest flow ---------------------------------------------------------------------------------


def test_a_callback_with_the_wrong_state_is_rejected():
    with pytest.raises(BootstrapError, match="wrong state"):
        _bootstrap_github.read_callback_code("expected", "code=abc&state=forged")
    with pytest.raises(BootstrapError, match="wrong state"):
        _bootstrap_github.read_callback_code("expected", "code=abc")
    assert _bootstrap_github.read_callback_code("expected", "code=abc&state=expected") == "abc"


def test_the_local_server_ignores_a_forged_redirect_and_accepts_the_real_one():
    flow = _bootstrap_github.ManifestFlow("pat", "agent-workspace", False, state="s3cr3t")
    base = flow.start_url.rstrip("/")
    import threading

    result: list[str] = []
    thread = threading.Thread(target=lambda: result.append(flow.wait(attempts=30)))
    thread.start()
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "https://github.com/settings/apps/new?state=s3cr3t" in page and "agent-factory-pat" in page
    with pytest.raises(urllib.error.HTTPError) as forged:
        urllib.request.urlopen(base + "/callback?code=evil&state=nope")
    assert forged.value.code == 400
    urllib.request.urlopen(base + "/callback?code=good&state=s3cr3t").read()
    thread.join(5)
    assert result == ["good"]


def test_the_manifest_asks_for_the_documented_permissions_and_no_webhook():
    m = _bootstrap_github.build_manifest("pat", "agent-workspace", "http://127.0.0.1:1/callback")
    assert m["default_permissions"] == {
        "contents": "read", "metadata": "read", "pull_requests": "write",
        "statuses": "write", "checks": "read", "issues": "write",
    }  # fmt: skip
    hooks = m["hook_attributes"]
    assert isinstance(hooks, dict) and hooks["active"] is False and m["public"] is False
    assert m["name"] == "agent-factory-pat" and m["redirect_url"] == "http://127.0.0.1:1/callback"
    org = _bootstrap_github.build_registration_url("acme", True, "x")
    assert org == "https://github.com/organizations/acme/settings/apps/new?state=x"


# --- the command line ----------------------------------------------------------------------------------


def test_bad_arguments_are_refused(world):
    bad_values = (
        ["--workspace", "nodash"],
        ["--state-uri", "bucket/path"],
        ["--state-uri", "az://c/p"],
        ["--state-uri", "s3://"],
        ["--project", ""],
        ["--domain", ""],
    )
    for bad in bad_values:
        argv = [a for a in ARGS]
        if bad[0] in argv:
            argv[argv.index(bad[0]) + 1] = bad[1]
        else:
            argv += bad
        with pytest.raises(SystemExit) as refused:
            _bootstrap.main(
                argv, make_context=lambda settings: make_context(world, settings, [], CLAUDE_TOKEN)
            )
        assert refused.value.code == 2
    assert world.events == []


@pytest.mark.parametrize("scheme", _profile._STATE_SCHEMES)
def test_every_state_scheme_the_profile_accepts_is_accepted(scheme, world):
    argv = [a if a != "s3://example-bucket/state" else f"{scheme}://example-bucket/state" for a in ARGS]
    code = _bootstrap.main(
        [*argv, "--dry-run", "--dir", str(world.root / "clone")],
        make_context=lambda settings: make_context(world, settings, [], CLAUDE_TOKEN),
    )
    assert code in (0, 3)


@pytest.mark.parametrize(
    "change",
    [
        ["--project", "my-agents-2"],
        ["--domain", "staging"],
        ["--endpoint", "other.example.test"],
        ["--state-uri", "s3://other/x"],
    ],
)
def test_a_rerun_with_another_project_domain_or_endpoint_is_blocked_before_any_change(world, change):
    assert run(world, "--no-app")[0] == 0
    before = list(world.events)
    out: list[str] = []
    argv = [a for a in ARGS]
    if change[0] in argv:
        argv[argv.index(change[0]) + 1] = change[1]
    else:
        argv += change
    code = _bootstrap.main(
        [*argv, "--dir", str(world.root / "clone"), "--no-app"],
        make_context=lambda settings: make_context(world, settings, out, CLAUDE_TOKEN),
    )
    assert code == 3 and statuses(out)["profile"] == "blocked", out
    assert world.events == before
    text = next(line for line in out if line.startswith("next:"))
    command = text.split("values: ", 1)[1]
    assert "--project my-agents " in command and "--state-uri s3://example-bucket/state" in command
    assert "--endpoint flyte.example.test" in command and "--domain" not in command
    assert not re.search(r"<[^>]*>", text) and change[1] not in command


def test_the_ops_script_only_runs_under_main():
    spec = importlib.util.spec_from_file_location("ops_bootstrap", ROOT / "ops" / "bootstrap.py")
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))  # importing must not run or exit


def test_status_and_dry_run_exclude_each_other(world):
    with pytest.raises(SystemExit) as refused:
        _bootstrap.main([*ARGS, "--status", "--dry-run"], make_context=lambda s: make_context(world, s, []))
    assert refused.value.code == 2
    assert world.events == []


def test_a_bucket_only_state_uri_is_accepted_as_the_message_says(world, capsys):
    with pytest.raises(SystemExit):
        _bootstrap.main([*ARGS[:-1], "s3://", "--dry-run"])
    assert "<scheme>://<bucket>[/<prefix>]" in capsys.readouterr().err
    lines: list[str] = []
    code = _bootstrap.main(
        [*[a if a != "s3://example-bucket/state" else "s3://example-bucket" for a in ARGS], "--dry-run",
         "--dir", str(world.root / "clone")],
        make_context=lambda s: make_context(world, s, lines),
    )  # fmt: skip
    assert code in (0, 3) and "--state-uri" not in capsys.readouterr().err
    assert any(row.startswith("[todo]") and "would" in row for row in lines), lines


def test_the_ops_script_exits_with_the_bootstraps_code(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", "--help"])
    with pytest.raises(SystemExit) as done:
        runpy.run_path(str(ROOT / "ops" / "bootstrap.py"), run_name="__main__")
    assert done.value.code == 0 and "--endpoint" in capsys.readouterr().out


def test_the_module_exits_with_its_mains_code_when_run_as_a_script(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["_bootstrap", "--help"])
    monkeypatch.delitem(
        sys.modules, "flyte_agent_factory._bootstrap"
    )  # a fresh copy: no "found in sys.modules"
    with pytest.raises(SystemExit) as done:
        runpy.run_module("flyte_agent_factory._bootstrap", run_name="__main__")
    assert done.value.code == 0 and "--endpoint" in capsys.readouterr().out


def test_an_endpoint_without_a_host_is_refused(capsys):
    with pytest.raises(SystemExit) as refused:
        _bootstrap.main([*ARGS[2:], "--endpoint", "https:///"])
    assert refused.value.code == 2
    assert "--endpoint must be a host" in capsys.readouterr().err


def test_the_command_line_runs_against_the_real_adapters(monkeypatch, tmp_path):
    seen: list[Context] = []

    def fake_run(ctx: Context, table: object) -> int:
        seen.append(ctx)
        return 0

    monkeypatch.setattr(_bootstrap, "run", fake_run)
    assert _bootstrap.main([*ARGS, "--dir", str(tmp_path / "clone")]) == 0
    [ctx] = seen
    assert isinstance(ctx.github, _bootstrap_github.GhCli)
    assert isinstance(ctx.flyte, FlyteSdk) and isinstance(ctx.deployer, DeployScripts)
    assert ctx.settings.endpoint == "flyte.example.test"


class _BrokenCheck:
    """A step whose live check cannot be answered. It has no `plan` or `do`: reaching one fails."""

    id = "broken"
    informational = False
    advisory = False

    def check(self, ctx):
        raise BootstrapError("cannot ask")


class _NeverReached:
    """The step after the broken one: it has no methods, so a run that gets to it fails the test."""

    id = "fine"
    informational = False
    advisory = False


def test_a_step_whose_check_fails_stops_a_real_run_before_the_next_step(world):
    out: list[str] = []
    ctx = make_context(world, Settings("h", "p", "pat/ws", "s3://b/p"), out)
    assert _bootstrap.run(ctx, [cast(Step, _BrokenCheck()), cast(Step, _NeverReached())]) == 3
    assert out == [
        "[blocked] broken: check failed: BootstrapError: cannot ask",
        "next: check failed: BootstrapError: cannot ask",
    ]


def test_write_to_terminal_opens_the_controlling_terminal(monkeypatch, tmp_path):
    fake_tty = tmp_path / "tty"
    real_open = open
    monkeypatch.setattr(
        "builtins.open", lambda path, *a, **kw: real_open(fake_tty if path == "/dev/tty" else path, *a, **kw)
    )
    write_to_terminal("the key\n")
    assert fake_tty.read_text() == "the key\n"


def test_run_captured_returns_the_output_instead_of_printing_it(capsys):
    proc = run_captured([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"])
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "out\n", "err\n")
    assert capsys.readouterr().out == ""


def test_a_forbidden_ruleset_update_is_reported_like_a_forbidden_create(world):
    assert run(world)[0] == 0 and world.rulesets["agent-workspace-main"] is False
    world.ruleset_forbidden = True
    code, out = run(world, "--no-app")
    assert code == 3 and statuses(out)["ruleset"] == "todo"
    assert world.rulesets["agent-workspace-main"] is False, "the refused update changed nothing"


def test_the_fakes_refuse_a_repository_that_is_not_there_and_a_command_nobody_expects(world):
    gh = FakeGitHub(world)
    with pytest.raises(BootstrapError, match="Could not resolve to a Repository"):
        gh.deploy_key_titles("pat/missing")
    with pytest.raises(AssertionError, match="unexpected command"):
        fakes._run_command(world, ["rm", "-rf", "/"])


def test_an_existing_policy_with_the_right_binding_is_reused_not_remade(world):
    flyte = make_context(world, Settings("h", "my-agents", "pat/ws", "s3://b/p"), []).flyte
    flyte.config_path.write_text("admin:\n  endpoint: dns:///h\n")
    flyte.ensure_policy("p")
    flyte.ensure_policy("p")
    assert world.count("create_policy") == 1
