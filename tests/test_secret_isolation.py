"""Raw credentials stay out of the session's environment and out of published text."""

import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _auth,
    _author,
    _bridge,
    _checkpoint,
    _demo_checks,
    _gate_base,
    _gate_start,
    _gate_tests,
    _git,
    _session_config,
    _session_setup,
    _subagent_run,
    _timeline,
    agent,
)
from flyte_agent_factory._bridge import redact_tokens

ROOT = Path(__file__).resolve().parents[1]

# Every kind of mounted secret, under the name it is mounted as; values are unique and multi-line where
# the real ones are.
MOUNTED = {
    "CODEX_ACCESS_TOKEN": "codex-access",
    "AGENT_FACTORY_REPO_KEYS": '{"r": "GIT_DEPLOY_KEY_R"}',
    "GIT_DEPLOY_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----\nbody\n-----END OPENSSH PRIVATE KEY-----",
    "GIT_DEPLOY_KEY_R": "second-key",
    "CLAUDE_CODE_OAUTH_TOKEN": "setup-token",
    "LLM_GATEWAY_KEY": "gateway",
    "ANTHROPIC_API_KEY": "sk-ant-billing",
    "GITHUB_APP_PRIVATE_KEY": "app-pem",
    "GITHUB_BOARD_PAT": "board-pat",
    "MY_NPM_TOKEN": "npm",
    "_UNION_CLAUDE_CODE_OAUTH_TOKEN": "setup-token",
    "_UNION_CODEX_ACCESS_TOKEN": "codex-access",
    "_UNION_AGENT_DEPLOY_KEY": "deploy",
    "_UNION_SELFIMPROVE_DEPLOY_KEY": "deploy-2",
    "_UNION_LLM_GATEWAY_KEY": "gateway",
    "_UNION_ANTHROPIC_API_KEY": "project-key",
    "_UNION_SOMETHING_NEW": "a secret nobody listed",
}
KEPT = {
    "_UNION_EAGER_API_KEY": "flyte-api-key",  # attach() needs it to submit child actions
    "PROJECT_ANTHROPIC_API_KEY": "project-key",  # reserved for the project's own API calls
}
ENV = {
    **MOUNTED,
    **KEPT,
    "PATH": "/usr/bin",
    "HOME": "/home/x",
    "AGENT_FACTORY_SECRETS": "dk=GIT_DEPLOY_KEY,npm=MY_NPM_TOKEN,api=PROJECT_ANTHROPIC_API_KEY",
}


def test_session_env_holds_no_mounted_secret_but_the_keep_list():
    """Session env holds no mounted secret but the keep list."""
    env = _session_setup._session_env(dict(ENV))
    assert set(MOUNTED).isdisjoint(env)
    values = set(env.values())
    for name, value in MOUNTED.items():
        # Some secrets share a value with a kept one on purpose (the project key); compare by name there.
        if value not in KEPT.values():
            assert value not in values, name
    assert {k: env[k] for k in KEPT} == KEPT
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x"


def test_session_env_strips_every_browser_identity_token():
    """Identity tokens are the supervisor's alone: not the session's, nor any child's."""
    from dataclasses import replace

    from flyte_agent_factory import _profile, _secret_env

    profile = _session_config._PROFILE
    assert profile is not None
    browser = _profile.Browser(
        identities=(
            _profile.BrowserIdentity("github", "memory://g"),
            _profile.BrowserIdentity("my-bank", "memory://b"),
        )
    )
    tokens = {"BROWSER_GITHUB_TOKEN": "t1", "BROWSER_MY_BANK_TOKEN": "t2"}
    env = {**tokens, "PATH": "/usr/bin"}
    assert _secret_env.session_env(env, replace(profile, browser=browser)) == {"PATH": "/usr/bin"}
    # A pod deployed from another profile still hides the tokens its deploy listed.
    listed = {**env, "AGENT_FACTORY_BROWSER_IDENTITY_NAMES": "github,my-bank"}
    assert _secret_env.session_env(listed, profile) == {
        "PATH": "/usr/bin",
        "AGENT_FACTORY_BROWSER_IDENTITY_NAMES": "github,my-bank",
    }
    # The legacy identity's token is hidden too.
    legacy = replace(
        profile, browser=_profile.Browser(identities=(_profile.BrowserIdentity("personal", "m"),))
    )
    assert _secret_env.session_env({"BROWSER_PERSONAL_TOKEN": "t"}, legacy) == {}


def test_a_secret_added_to_the_deploy_list_is_hidden_without_a_code_change():
    """A secret added to the deploy list is hidden without a code change."""
    env = _session_setup._session_env(
        {"NEW_SECRET_VAR": "v", "PATH": "/usr/bin", "AGENT_FACTORY_SECRETS": "n=NEW_SECRET_VAR"}
    )
    assert env == {"PATH": "/usr/bin", "AGENT_FACTORY_SECRETS": "n=NEW_SECRET_VAR"}


def test_an_empty_secrets_prefix_does_not_hide_the_whole_environment():
    """An empty secrets prefix does not hide the whole environment."""
    assert (
        _session_setup._session_env({"FLYTE_SECRETS_ENV_PREFIX": "", "PATH": "/usr/bin"})["PATH"]
        == "/usr/bin"
    )


def test_codex_author_env_holds_no_secret(monkeypatch, tmp_path):
    """Codex author env holds no secret."""
    seen = {}
    monkeypatch.setattr(
        _author, "CodexAuthor", lambda **kw: seen.update(kw) or type("P", (), {"thread_id": ""})()
    )
    monkeypatch.setattr(_author, "_prepare_codex_home", lambda: tmp_path)
    monkeypatch.setattr(_session_setup, "_export_runtime_env", lambda: dict(ENV))
    _author._start_codex_author(_timeline._Timeline(), model="", effort="")
    assert set(MOUNTED).isdisjoint(seen["env"])
    assert seen["env"]["CODEX_HOME"] == str(tmp_path)


def test_every_child_environment_is_built_by_session_env():
    """Every child environment is built by session env."""
    import inspect

    for func in (_subagent_run._run_codex, _author._login_with_access_token):
        assert "_session_env(" in inspect.getsource(func)


_FD_CHILD = (
    "import json, os, sys\n"
    "fd = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR')\n"
    "token = os.read(int(fd), 4096).decode() if fd else None\n"
    "json.dump({'token': token, 'env': 'CLAUDE_CODE_OAUTH_TOKEN' in os.environ, 'argv': sys.argv[1:]},"
    " open(os.environ['OUT'], 'w'))\n"
)


async def test_the_session_gets_the_setup_token_through_an_fd_not_env_or_argv(monkeypatch, tmp_path):
    """The session gets the setup token through an fd not env or argv."""
    out = tmp_path / "out.json"
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "setup-token-value")
    candidates = [[sys.executable, "-c", _FD_CHILD, "--flag"]]
    env = {**_session_setup._session_env(dict(os.environ)), "OUT": str(out)}
    proc = await _author._start_session(candidates, env=env, timeline=_timeline._Timeline())
    for _ in range(100):
        if out.exists() and out.read_text():
            break
        await asyncio.sleep(0.1)
    await proc.stop(grace=1.0)
    seen = json.loads(out.read_text())
    assert seen == {"token": "setup-token-value", "env": False, "argv": ["--flag"]}


async def test_the_pipe_is_drained_so_a_tool_child_finds_nothing(tmp_path):
    """The pipe is drained so a tool child finds nothing."""
    with _auth.token_fd({}, "tok") as (env, fds):
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import os; fd = int(os.environ['CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR']);"
            " print(os.read(fd, 99).decode() + '|' + os.read(fd, 99).decode())",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            pass_fds=fds,
        )
    out, _ = await proc.communicate()
    assert out.decode().strip() == "tok|"
    with pytest.raises(OSError):  # the parent closed its copy too
        os.fstat(int(env[_auth.TOKEN_FD_VAR]))


def test_the_env_fallback_flag_restores_the_old_hand_over(monkeypatch):
    """The env fallback flag restores the old hand over."""
    monkeypatch.setenv(_auth.TOKEN_IN_ENV_VAR, "1")
    with _auth.token_fd({"A": "b"}, "tok") as (env, fds):
        assert env == {"A": "b", "CLAUDE_CODE_OAUTH_TOKEN": "tok"} and fds == ()
    with _auth.token_fd({"A": "b"}, "") as (env, fds):
        assert env == {"A": "b"} and fds == ()


def test_the_sub_agent_launch_uses_the_fd():
    """The sub agent launch uses the fd."""
    import inspect

    assert "_auth.token_fd(" in inspect.getsource(agent.subagent.func)


def test_the_supervisor_goes_non_dumpable_before_anything_else(monkeypatch):
    """The supervisor goes non dumpable before anything else."""

    class Reached(Exception):
        pass

    def stop():
        raise Reached

    monkeypatch.setattr(_auth, "set_non_dumpable", stop)
    for call in (agent.project.func("brief", browser=False), agent.subagent.func("brief")):
        with pytest.raises(Reached):
            asyncio.run(call)


def test_set_non_dumpable_is_a_noop_off_linux(monkeypatch):
    """Set non dumpable is a noop off linux."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _auth.set_non_dumpable() is False


@pytest.mark.parametrize("control, expected", [([], "denied"), (["--control"], "readable")])
def test_a_child_cannot_read_a_non_dumpable_parents_environ(control, expected):
    """Run where the factory runs: Linux, as an unprivileged user (pods and the CI runner both are).

    Root can read any process's environ whatever its dumpable flag, so as root this would fail by design.
    """
    probe = ROOT / "ops/probes/non_dumpable.py"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, str(probe), *control], env=env, capture_output=True, text=True)
    assert result.stdout.strip() == expected and result.returncode == 0


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_private_makes_a_0600_file_in_a_0700_dir(tmp_path):
    """Write private makes a 0600 file in a 0700 dir."""
    target = tmp_path / "d" / "auth.json"
    target.parent.mkdir()
    target.parent.chmod(0o755)
    target.write_text("old")
    target.chmod(0o644)
    _auth.write_private(target, "new")
    assert target.read_text() == "new"
    assert (_mode(target), _mode(target.parent)) == (0o600, 0o700)


def test_deploy_key_is_0600_in_a_0700_dir(tmp_path, monkeypatch):
    """Deploy key is 0600 in a 0700 dir."""
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))
    monkeypatch.setenv("GIT_DEPLOY_KEY", "key body")
    installed = _git.install_deploy_key()
    assert installed is not None
    path = Path(installed)
    assert (_mode(path), _mode(path.parent)) == (0o600, 0o700)
    assert "GIT_DEPLOY_KEY" not in os.environ


def test_codex_home_is_0700_for_the_token_login(tmp_path, monkeypatch):
    """The home `codex login --with-access-token` writes into is 0700, and the token's auth.json is 0600."""
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(
        _author, "_CODEX_COMMAND", (sys.executable, str(Path(__file__).with_name("fake_codex.py")))
    )
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "a")
    home = _author._prepare_codex_home()
    assert _mode(home) == 0o700
    assert _mode(home / "auth.json") == 0o600


# --- redaction ----------------------------------------------------------------------------------- #


def _body_line() -> str:
    return base64.b64encode(bytes(range(7, 59))).decode()[:70]  # an OpenSSH body line: 70 base64 chars


JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"  # gitleaks:allow
SECRETS = {
    "jwt": JWT,
    "auth-json-access": '"access_token": "opaque-value-123"',
    "auth-json-id": '"id_token":"opaque-value-123"',
    "auth-json-refresh": "refresh_token=opaque-value-123",
    "rt": "rt.Ab3dEf6hIj9lMn2pQr5tUv8x.Zz1y2x3w4v5u",
    "pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
    "pem-unterminated": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA",  # gitleaks:allow
    "stray-body": _body_line(),
    "sk-ant": "sk-ant-api03-" + "Ab1_-" * 6,
    "ghs": "ghs_" + "a1B2" * 9,
    "ghp": "ghp_" + "a1B2" * 9,
    "gho": "gho_" + "a1B2" * 9,
    "github_pat": "github_pat_" + "a1B2" * 9,
    "slack-bot": "xoxb-1234567890-abcdefghij",
    "slack-user": "xoxp-1234567890-abcdefghij",
}


@pytest.mark.parametrize("name", SECRETS)
def test_redact_tokens_masks_each_format(name):
    """Redact tokens masks each format."""
    secret = SECRETS[name]
    cleaned = redact_tokens(f"before\n{secret}\nafter")
    assert "<redacted>" in cleaned and cleaned.startswith("before\n")
    leaked = {"auth-json-access": "opaque", "auth-json-id": "opaque", "auth-json-refresh": "opaque"}.get(
        name, ""
    )
    assert (leaked or secret[-12:]) not in cleaned
    if name != "pem-unterminated":  # an unterminated key block runs to the end of the text, by design
        assert cleaned.endswith("\nafter")


def test_a_key_body_line_is_masked_inline_with_a_log_prefix():
    """A key body line is masked inline with a log prefix."""
    cleaned = redact_tokens(f"[session] assistant: {_body_line()}")
    assert cleaned == "[session] assistant: <redacted>"


def test_auth_json_keeps_its_field_names():
    """Auth json keeps its field names."""
    text = redact_tokens('{"access_token": "opaque", "account_id": "acct-1"}')
    assert text == '{"access_token": "<redacted>", "account_id": "acct-1"}'


def test_key_body_lines_after_an_escaped_newline_are_masked():
    """A JSON-escaped key whose header was cut off: each body line follows a literal backslash-n."""
    first, second = _body_line(), _body_line()[::-1]
    text = '{"key": "' + "\\n".join([first, second]) + '\\n-----END OPENSSH PRIVATE KEY-----"}'
    cleaned = redact_tokens(text)
    assert first not in cleaned and second not in cleaned
    assert cleaned.startswith('{"key": "<redacted>\\n<redacted>\\n')
    assert redact_tokens("path\\" + first) == "path\\<redacted>"


@pytest.mark.parametrize(
    "text",
    [
        "access_token = get_token()",
        "id_token: 12 tokens",
        "refresh_token=abc and more",
        "access_token = get_access_token_from_env()",
    ],
)
def test_code_and_prose_about_token_fields_survive(text):
    """Code and prose about token fields survive."""
    assert redact_tokens(text) == text


def test_a_url_keeps_its_parameters_after_a_redacted_token():
    """A url keeps its parameters after a redacted token."""
    url = "https://example.org/cb?access_token=Zm9vYmFyYmF6cXV4MTIzNDU2&state=1&x=2"
    assert redact_tokens(url) == "https://example.org/cb?access_token=<redacted>&state=1&x=2"


def test_a_failed_delegation_publishes_its_reason_redacted_everywhere(bridge_root, tmp_path):
    """A failed delegation publishes its reason redacted everywhere."""
    from flyte_agent_factory._delegation import Delegations

    async def worker(*args, **kwargs):
        raise AssertionError("not called")

    supervisor = Delegations(bridge_root, tmp_path, worker, lambda: "codex", lambda *args: None)
    supervisor.jobs.mkdir(parents=True, exist_ok=True)
    supervisor.fail("delegate-1", f"worker said {SECRETS['ghp']}", "codex")
    answer = (bridge_root / _bridge.ANSWERS / "delegate-1.json").read_text()
    notice = (supervisor.jobs / "delegate-1" / "notice.txt").read_text()
    error = (supervisor.jobs / "delegate-1" / "error.txt").read_text()
    for published in (answer, notice, error, *supervisor.notices.values()):
        assert "ghp_" not in published and "<redacted>" in published


def test_prose_shas_uuids_and_paths_survive():
    """Prose shas uuids and paths survive."""
    text = "\n".join(
        [
            "Fixed the retry loop in src/flyte_agent_factory/_bridge.py and bumped the timeout to 30s.",
            "commit " + hashlib.sha1(b"x").hexdigest(),
            "sha256 " + hashlib.sha256(b"x").hexdigest(),
            "run " + str(uuid.uuid4()),
            "/home/flyte/.claude-flyte/bridge/requests/delegate-1730000000000-ab12cd34.json",
            "tests/test_secret_isolation.py::test_prose_shas_uuids_and_paths_survive PASSED",
            "A" * 64,
            "thisisaverylongloweracewordwithnodigitsorcapitalsatallsoitisjustprose" * 1,
            "Bearer of bad news, sk-learn, rt.py and refresh_token handling are described here.",
            "https://example.org/a/very/long/path/that/goes/on/and/on/and/on/and/on/and/on/and/on/for/ever",
        ]
    )
    assert redact_tokens(text) == text


# --- every publishing path ------------------------------------------------------------------------ #


def test_timeline_rows_are_redacted_in_every_copy():
    """Timeline rows are redacted in every copy."""
    timeline = _timeline._Timeline()
    timeline.row("📝", f"DONE: used {SECRETS['ghp']} and {JWT}")
    shown = json.dumps(timeline.snapshot()) + timeline.render()
    assert "ghp_" not in shown and "eyJ" not in shown
    assert "<redacted>" in timeline.notes[-1]


def test_flyte_note_text_is_redacted_before_it_reaches_the_report(bridge_root, src_path):
    """Flyte note text is redacted before it reaches the report."""
    subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._bridge", "note", f"found {SECRETS['sk-ant']} in env"],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": src_path, "AGENT_FACTORY_BRIDGE_DIR": str(bridge_root)},
        check=True,
    )
    assert _bridge.Bridge(bridge_root).drain_notes() == ["found <redacted> in env"]


async def test_ask_human_question_is_redacted(bridge_root, monkeypatch):
    """Ask human question is redacted."""
    rows = []
    bridge = _bridge.Bridge(bridge_root, on_event=lambda icon, text: rows.append(text))

    async def refuse(*args, **kwargs):
        raise RuntimeError("stop here")

    monkeypatch.setattr(_bridge, "open_condition", refuse)
    request = {"id": "ask-1", "question": f"Is {SECRETS['ghs']} the right token?", "timeout_s": 1}
    await bridge._service(request, 1)
    assert rows[0] == "asked: Is <redacted> the right token?"
    assert "ghs_" not in json.dumps(bridge.questions)


async def test_sub_agent_answer_is_redacted():
    """Sub agent answer is redacted."""
    assert await _subagent_run._subagent_result(f"key {SECRETS['ghp']}", None) == "key <redacted>"


def test_the_session_transcript_publisher_uses_the_same_function():
    """The session transcript publisher uses the same function."""
    from flyte_agent_factory import _session_log

    assert _session_log.redact_tokens is redact_tokens  # type: ignore[attr-defined]  # re-exported by import only


# --- what the supervisor starts within the session's reach ------------------------------------------ #

#: A dump of a child's environment holds this exactly when a seeded secret reached it.
SEEDED = "sekret-"


def _seeded_in(text: str) -> list[str]:
    """Return the seeded values in `text`; an assertion on this never prints the rest of an environment."""
    return re.findall(rf"{SEEDED}\d+", text)


_DUMP_ENV = "import json, os, sys; open(sys.argv[1], 'a').write(json.dumps(dict(os.environ)) + '\\n')"


@pytest.fixture
def seeded(monkeypatch):
    """Every mounted secret in this process's environment, under a value a dump can be searched for."""
    for i, name in enumerate(MOUNTED):
        monkeypatch.setenv(name, f"{SEEDED}{i:02d}")
    monkeypatch.setenv("AGENT_FACTORY_SECRETS", ENV["AGENT_FACTORY_SECRETS"])


def _plain_git(path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_child_env_is_the_session_env_of_this_process(seeded):
    """Child env is the session env of this process."""
    from flyte_agent_factory import _secret_env

    env = _secret_env.child_env(EXTRA="1")
    assert _seeded_in(json.dumps(env)) == []
    assert env["EXTRA"] == "1" and env["PATH"] == os.environ["PATH"]
    assert set(_session_setup._session_env(dict(os.environ))) == set(env) - {"EXTRA"}


def test_a_gate_tool_runs_without_secrets(seeded, tmp_path):
    """A gate tool runs without secrets."""

    out = tmp_path / "env.json"
    code, _ = _gate_base._run([sys.executable, "-c", _DUMP_ENV, str(out)], tmp_path)
    assert code == 0 and _seeded_in(out.read_text()) == []


def test_the_done_gate_runs_session_written_tests_without_secrets(seeded, tmp_path):
    """The done gate runs session written tests without secrets."""
    from flyte_agent_factory import _gates

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _plain_git(workspace, "init", "-q")
    _plain_git(workspace, "config", "user.email", "test@example.com")
    _plain_git(workspace, "config", "user.name", "test")
    app = workspace / "app"
    app.mkdir()
    (app / "main.py").write_text("def answer():\n    return 1\n")
    _plain_git(workspace, "add", "app")
    _plain_git(workspace, "commit", "-q", "-m", "base")
    _gates.capture_base(workspace, "app")
    base = (workspace / ".claude-flyte/app/base-commit").read_text()
    dump = tmp_path / "env.jsonl"
    # The session's test reads its environment; neither the head nor the base run may find a secret.
    (app / "test_main.py").write_text(
        "import json, os\n"
        "from main import answer\n\n"
        "def test_answer():\n"
        f"    open({str(dump)!r}, 'a').write(json.dumps(dict(os.environ)) + '\\n')\n"
        "    assert answer() == 2\n"
    )
    (app / "main.py").write_text("def answer():\n    return 2\n")
    code, result = _gate_tests.fail_to_pass(
        workspace, "app", base, ["app/main.py", "app/test_main.py"], "change answer", "", "DONE"
    )
    assert code == 0, result
    assert len(dump.read_text().splitlines()) == 2
    assert _seeded_in(dump.read_text()) == []


_SPY = (
    "import json, os, sys\n"
    "open({log!r}, 'a').write(json.dumps(dict(os.environ)) + '\\n')\n"
    # As a textconv driver it is handed a file and must print it.
    "if len(sys.argv) == 2 and os.path.isfile(sys.argv[1]):\n"
    "    sys.stdout.write(open(sys.argv[1]).read())\n"
)


@pytest.fixture
def hostile_repo(tmp_path):
    """Build a workspace whose `.git/config` and hooks name a program that records the environment it got."""
    repo = tmp_path / "ws"
    repo.mkdir()
    log = tmp_path / "spy.jsonl"
    spy = tmp_path / "spy"
    spy.write_text(f"#!{sys.executable}\n" + _SPY.format(log=str(log)))
    spy.chmod(0o755)
    _plain_git(repo, "init", "-q")
    _plain_git(repo, "config", "user.email", "test@example.com")
    _plain_git(repo, "config", "user.name", "test")
    (repo / "pkg").mkdir()
    (repo / "pkg/data.txt").write_text("one\n")
    (repo / ".gitattributes").write_text("* diff=leak\n")
    _plain_git(repo, "add", ".")
    _plain_git(repo, "commit", "-q", "-m", "base")
    (repo / "pkg/data.txt").write_text("two\n")
    _plain_git(repo, "commit", "-q", "-am", "change")
    for key in ("core.fsmonitor", "diff.leak.textconv"):
        _plain_git(repo, "config", key, str(spy))
    for hook in ("pre-commit", "post-commit", "post-checkout", "post-merge", "reference-transaction"):
        (repo / ".git/hooks" / hook).symlink_to(spy)
    (repo / "pkg/data.txt").write_text("three\n")  # dirty, so status and diff consult the fsmonitor
    return repo, log


def test_the_hostile_repo_runs_its_programs_under_plain_git(seeded, hostile_repo):
    """The control: without the supervisor's guard, git hands the seeded secrets to the spy."""
    repo, log = hostile_repo
    subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, check=True)
    assert _seeded_in(log.read_text())
    log.unlink()
    # The textconv driver alone, which the config overrides do not cover: diff options must.
    unhooked = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "show", "HEAD"]
    subprocess.run(unhooked, cwd=repo, capture_output=True, check=True)
    assert _seeded_in(log.read_text())


async def test_supervisor_git_calls_run_none_of_the_workspace_programs(seeded, hostile_repo, tmp_path):
    """Supervisor git calls run none of the workspace programs."""
    from flyte_agent_factory import _delegation, _gates, _state

    repo, log = hostile_repo
    base = _plain_git(repo, "rev-list", "--max-parents=0", "HEAD")
    head = _plain_git(repo, "rev-parse", "HEAD")
    assert _gates.is_git_repo(repo)
    assert _gates.changed_lines(repo, base) is not None
    assert _gates._uncommitted(repo, "pkg", "root") == ["pkg/data.txt"]
    with _gate_start._start_tree(repo, base, [head]) as start:
        assert (start / "pkg/data.txt").read_text() == "one\n"
    _session_setup._exclude_harness(repo)
    assert _demo_checks._committed_files(repo, repo / "pkg") == [repo / "pkg/data.txt"]
    assert await _checkpoint._git_head(repo)
    assert (await _checkpoint._git_out(repo, "status", "--porcelain"))[0] == 0
    assert (await _checkpoint._git_out(repo, "commit", "-q", "--allow-empty", "-m", "hooked"))[0] == 0
    assert "data.txt" in await _checkpoint._handoff_git_out(repo, "status", "--short")
    assert "data.txt" in await _checkpoint._handoff_git_out(repo, "diff", "HEAD", "--stat")
    assert b"pkg/data.txt" in (await _state._list_checkpoint_files(repo, "--cached") or b"")
    assert _delegation.usable_repo(repo)
    target = tmp_path / "snapshot"
    target.mkdir()
    _delegation.snapshot(repo, target)
    assert not log.exists() or _seeded_in(log.read_text()) == []
    assert not log.exists(), "a program named by the workspace's git config or hooks ran"


async def test_the_browser_processes_get_no_secret(seeded, tmp_path, monkeypatch):
    """The browser processes get no secret."""
    from unittest.mock import AsyncMock

    from flyte_agent_factory import _local_browser

    browser = _local_browser.LocalBrowser(profile_dir=tmp_path / "profile", output_dir=tmp_path / "output")
    spawn = AsyncMock()
    monkeypatch.setattr(browser, "_spawn", spawn)
    monkeypatch.setattr(_local_browser, "_wait", AsyncMock(return_value=True))
    await browser.start()
    envs = [call.args[1] for call in spawn.call_args_list]
    assert len(envs) == 2 and all(env["DISPLAY"] == _local_browser.DISPLAY for env in envs)
    assert _seeded_in(json.dumps(envs)) == []


async def test_the_tailnet_daemon_and_cli_get_no_secret(seeded, monkeypatch):
    """The tailnet daemon and cli get no secret."""
    from flyte_agent_factory import _browser_exit_node

    seen = []
    real = asyncio.create_subprocess_exec

    async def record(*argv, **kwargs):
        seen.append(kwargs.get("env"))
        return await real("true", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    proxy = _browser_exit_node.BrowserExitNode("egress", "my_tailnet_key")
    await proxy._spawn(["tailscaled"])
    await proxy._command("set", "--exit-node=egress")
    await proxy.stop()
    assert len(seen) == 2 and None not in seen
    assert _seeded_in(json.dumps(seen)) == []


async def test_the_codex_app_server_probe_gets_no_secret(seeded, tmp_path, monkeypatch):
    """The codex app server probe gets no secret."""
    from flyte_agent_factory import _meter
    from tests.test_meter import APP_SERVER_RESULT, _fake_codex, codex_auth

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_RESULT", json.dumps(APP_SERVER_RESULT))
    command = _fake_codex(tmp_path, "ok")
    script = tmp_path / "app_server.py"
    dump = tmp_path / "env.json"
    copy = f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))"
    script.write_text(script.read_text().replace("assert json.load(", f"{copy}\nassert json.load("))
    await _meter.read_codex(codex_auth(), command=command)
    seen = json.loads(dump.read_text())
    assert _seeded_in(json.dumps(seen)) == [] and seen["CODEX_HOME"]


def test_every_subprocess_call_in_the_new_modules_is_time_bound():
    """Every `subprocess.run` / `check_output` in the modules a session's files reach has a `timeout`."""
    # `Popen` is left out: a timeout belongs to its `wait` / `communicate`, which an AST walk of the
    # constructor cannot check. None of these modules starts one; the assertion below keeps it so.
    package = Path(agent.__file__).parent
    unbound = []
    for name in ("_delegation", "_ci_status", "child", "_codex_auth", "_secret_env"):
        tree = ast.parse((package / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            target = node.func.value
            if not (isinstance(target, ast.Name) and target.id == "subprocess"):
                continue
            assert node.func.attr != "Popen", f"{name}:{node.lineno} starts a Popen; bound its wait"
            if node.func.attr in ("run", "check_output", "check_call", "call"):
                if "timeout" not in {kw.arg for kw in node.keywords}:
                    unbound.append(f"{name}:{node.lineno}")
    assert unbound == []
