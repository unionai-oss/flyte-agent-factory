"""The Codex login file: what counts as a dead login, and who may refresh it (#64, #65)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flyte_agent_factory import (
    _author,
    _codex_auth,
    _meter,
    _router,
    _session_config,
    _session_setup,
    _subagent_run,
    _timeline,
    _usage,
    agent,
)

FAKE = Path(__file__).parent / "fake_codex.py"


def _jwt(exp: float) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


def _login(*, exp: float | None = None, refreshed: str = "2026-10-30T00:00:00+00:00", refresh="r1") -> str:
    exp = time.time() + 3600 if exp is None else exp
    return json.dumps(
        {
            "tokens": {"access_token": _jwt(exp), "refresh_token": refresh, "id_token": "i"},
            "last_refresh": refreshed,
        }
    )


@pytest.mark.parametrize(
    "text,final,dead",
    [
        (
            "Your access token could not be refreshed because your refresh token was already used.",
            False,
            True,
        ),
        ("Please log out and sign in again.", False, True),
        ('{"error":{"code":"refresh_token_reused"}}', False, True),
        ("refresh_token_expired", False, True),
        # invalid_grant is OAuth's word for any refused grant: only Codex's own refresh counts (D4).
        ("token refresh failed: invalid_grant", False, True),
        ("POST https://auth.openai.com/oauth/token: 400 invalid_grant", False, True),
        ('mcp server "drive": oauth error invalid_grant', False, False),
        ("invalid_grant", False, False),
        # Codex prints a 401 for each websocket reconnect, most of which succeed: only a final one counts.
        ("Reconnecting... 2/5 (unexpected status 401 Unauthorized)", False, False),
        ("unexpected status 401 Unauthorized: Missing bearer", True, True),
        ("usage limit reached", True, False),
        ("stream disconnected before completion", True, False),
        ("", True, False),
    ],
)
def test_only_a_login_that_cannot_be_refreshed_is_an_auth_failure(text, final, dead):
    """Only a login that cannot be refreshed is an auth failure."""
    assert _codex_auth.is_auth_failure(text, final=final) is dead


def test_a_copy_for_a_sub_agent_has_an_empty_refresh_token_and_keeps_the_rest():
    """A copy for a sub agent has an empty refresh token and keeps the rest."""
    login = _login()
    stripped = json.loads(_codex_auth.strip_refresh_token(login))
    assert stripped["tokens"]["refresh_token"] == ""
    assert stripped["tokens"]["access_token"] == json.loads(login)["tokens"]["access_token"]
    assert _codex_auth.strip_refresh_token("not json") == "not json"


# --------------------------------------------------------------------------- #
# The author's home (C3)
# --------------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the author's Codex home at a temp dir with no token mounted, and return it."""
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex")
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_author, "_CODEX_COMMAND", (sys.executable, str(FAKE)))
    monkeypatch.delenv(_session_config.CODEX_TOKEN_ENV_VAR, raising=False)
    return tmp_path / "codex"


# --------------------------------------------------------------------------- #
# Sub-agents (C3) and access tokens (C5)
# --------------------------------------------------------------------------- #


@pytest.fixture
def delegate(tmp_path, monkeypatch, home):
    """Return a runner for a Codex sub-agent on the fake binary, and the home it gets."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "pod-home")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    codex = tmp_path / "codex"
    codex.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    codex.chmod(0o755)
    monkeypatch.setattr(_session_setup, "_roles_dir", lambda: tmp_path / "roles")
    work = tmp_path / "work"
    (work / ".claude-flyte").mkdir(parents=True)

    async def run(**kwargs):
        return await _subagent_run._run_codex(
            work, tmp_path / "base", "brief", answer_only=True, role="", model="", **kwargs
        )

    (tmp_path / "base").mkdir()
    return SimpleNamespace(run=run, home=tmp_path / "pod-home" / ".codex-subagents" / "base")


async def test_a_sub_agent_whose_login_is_refused_is_rerouted_and_codex_is_marked_broken(
    delegate, monkeypatch, tmp_path
):
    """A sub agent whose login is refused is rerouted and codex is marked broken."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    monkeypatch.setenv("FAKE_CODEX_AUTH", "reused")
    with pytest.raises(RuntimeError) as refused:
        await delegate.run()
    assert _usage.USAGE_LIMIT_MARK in str(refused.value) and _usage._CODEX_LOGIN_UNUSABLE in str(
        refused.value
    )

    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    _usage._note_engine_limit("codex", str(refused.value))
    broken = store.latest["codex"]
    assert broken.error == _usage._CODEX_LOGIN_UNUSABLE
    assert broken.limited_until is not None
    assert broken.limited_until > time.time() + 3600
    # The router now keeps codex out for the hours a person needs to sign in again.
    decision = _router.route(
        "delegate", {"codex": broken}, available=("claude", "codex"), now=time.time(), preferred="codex"
    )
    assert decision.engine == "claude"


async def test_a_codex_access_token_is_installed_with_login_on_stdin_and_preferred(delegate, monkeypatch):
    """A codex access token is installed with login on stdin and preferred."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    seen: dict[str, Any] = {}
    real = subprocess.run

    def spy(argv, **kwargs):
        seen.update(argv=argv, input=kwargs["input"], env=kwargs["env"])
        return real(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert "turn 1" in await delegate.run()
    assert seen["input"] == "tok-123" and "tok-123" not in " ".join(seen["argv"])
    assert _session_config.CODEX_TOKEN_ENV_VAR not in seen["env"]
    # The home holds what `codex login --with-access-token` wrote.
    assert json.loads((delegate.home / "auth.json").read_text())["auth_mode"] == "access_token"


def test_the_authors_home_uses_the_access_token(home, monkeypatch):
    """The authors home uses the access token."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    _author._prepare_codex_home()
    assert json.loads((home / "auth.json").read_text())["auth_mode"] == "access_token"


def test_a_token_that_codex_rejects_stops_the_run_without_leaking_it(home, monkeypatch):
    """A token that codex rejects stops the run without leaking it."""
    import flyte.errors

    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    monkeypatch.setenv("FAKE_CODEX_LOGIN_FAIL", "1")
    with pytest.raises(flyte.errors.NonRecoverableError, match="codex_token") as raised:
        _author._prepare_codex_home()
    assert "tok-123" not in str(raised.value)


def test_pods_mount_the_access_token_and_name_its_account(monkeypatch):
    """Pods mount the access token and name its account."""
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_tok")
    mounted = {s.key: s.as_env_var for s in agent._credentials_secret()}
    assert mounted["codex_tok"] == "CODEX_ACCESS_TOKEN"
    assert _session_config._codex_account() == "codex codex_tok"
    assert agent._codex_token_secret_env() == {"AGENT_FACTORY_CODEX_TOKEN_SECRET": "codex_tok"}
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    assert agent._codex_token_secret_env() == {}
    assert "CODEX_ACCESS_TOKEN" not in {s.as_env_var for s in agent._credentials_secret()}


def test_a_token_only_slot_is_offered_codex(monkeypatch):
    """A token only slot is offered codex."""
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_tok")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", raising=False)
    assert _usage._author_engines() == ("codex",)


async def test_an_access_token_refused_as_unauthorized_is_unusable(delegate, monkeypatch):
    # `[secrets] codex_token` is the credential itself, not a stripped copy: a final 401 condemns it.
    """An access token refused as unauthorized is unusable."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    monkeypatch.setenv("FAKE_CODEX_AUTH", "401")
    with pytest.raises(RuntimeError, match=_usage._CODEX_LOGIN_UNUSABLE):
        await delegate.run()


async def test_an_unrelated_invalid_grant_before_a_crash_is_a_crash(delegate, monkeypatch):
    """An unrelated invalid grant before a crash is a crash."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    monkeypatch.setenv("FAKE_CODEX_STDERR", 'mcp server "drive": oauth error invalid_grant')
    monkeypatch.setenv("FAKE_CODEX_CRASH", "1")
    with pytest.raises(RuntimeError, match="codex sub-agent exited 3") as crashed:
        await delegate.run()
    assert _usage.USAGE_LIMIT_MARK not in str(crashed.value)


async def test_a_refused_refresh_in_stderr_before_a_crash_is_a_dead_login(delegate, monkeypatch):
    """A refused refresh in stderr before a crash is a dead login."""
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "POST https://auth.openai.com/oauth/token: 400 invalid_grant")
    monkeypatch.setenv("FAKE_CODEX_CRASH", "1")
    with pytest.raises(RuntimeError, match=_usage._CODEX_LOGIN_UNUSABLE):
        await delegate.run()


async def test_a_direct_sub_agent_whose_login_is_dead_tells_the_supervisor_to_mark_codex_broken(
    tmp_path, monkeypatch
):
    """The direct path reroutes inside the child; its report must still say why codex was dropped."""
    import flyte.report

    reports = []

    async def log_report(text, **kwargs):
        reports.append(text)

    async def dead(*args, **kwargs):
        raise RuntimeError(
            f"{_usage.USAGE_LIMIT_MARK} {_usage._CODEX_LOGIN_UNUSABLE}: refresh token was already used"
        )

    async def no_meters():
        return {}

    monkeypatch.setattr(flyte.report.log, "aio", log_report)
    monkeypatch.setattr(_subagent_run, "_run_codex", dead)
    monkeypatch.setattr(_session_setup, "_install_bridge_commands", lambda: None)
    monkeypatch.setattr(_session_setup, "_exclude_harness", lambda work: None)
    monkeypatch.setattr(_subagent_run, "_read_direct_meters", no_meters)
    monkeypatch.setattr(_session_config, "_routes", [])
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="": str(tmp_path / "slot"))
    (tmp_path / "slot").mkdir()
    # Codex only, so the reroute has nowhere to go and defers: the last report is the one the parent reads.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    with pytest.raises(_router.RouterDeferred):
        await agent.subagent.func(brief="b", engine="codex")

    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    _usage._ingest_subagent_usage(reports[-1], "dev", 0, _timeline._Timeline())
    broken = store.latest["codex"]
    assert broken.error == _usage._CODEX_LOGIN_UNUSABLE
    assert broken.limited_until is not None
    assert broken.limited_until > time.time() + 3600


def test_an_ordinary_direct_refusal_is_not_read_as_a_dead_login(tmp_path, monkeypatch):
    """An ordinary direct refusal is not read as a dead login."""
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)
    monkeypatch.setattr(_usage, "_record_usage", lambda *a, **k: None)
    until = time.time() + 1800
    trailer = f"{_usage._USAGE_MARK}{json.dumps({'refusals': {'codex': until}})}-->"
    _usage._ingest_subagent_usage(trailer, "dev", 0, _timeline._Timeline())
    assert store.latest["codex"].limited_until == until and not store.latest["codex"].error


# --- a ChatGPT login (`[[slots]] codex_login`): the author keeps the chain, copies cannot refresh ---------


def _chatgpt_login(exp: float, refresh: str = "rt-1", last: str = "2026-11-04T00:00:00+00:00") -> str:
    return json.dumps(
        {
            "tokens": {"access_token": _jwt(exp), "refresh_token": refresh, "id_token": "x"},
            "last_refresh": last,
        }
    )


def test_a_login_file_is_told_apart_from_an_access_token():
    assert _codex_auth.is_login(_chatgpt_login(time.time() + 3600))
    assert not _codex_auth.is_login("tok-123")
    assert not _codex_auth.is_login('{"OPENAI_API_KEY": "sk"}')


def test_the_fresher_copy_wins_and_a_tie_keeps_the_one_on_disk():
    old = _chatgpt_login(time.time() + 3600, last="2026-11-04T00:00:00+00:00")
    new = _chatgpt_login(time.time() + 7200, refresh="rt-2", last="2026-11-04T01:00:00+00:00")
    assert _codex_auth.pick_fresher_login(new, old) == new
    assert _codex_auth.pick_fresher_login(old, new) == new
    assert _codex_auth.pick_fresher_login(old, old) == old


def test_the_author_writes_the_mounted_login_and_keeps_a_fresher_one_already_on_disk(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", home)
    mounted = _chatgpt_login(time.time() + 3600)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, mounted)
    called = []
    monkeypatch.setattr(_author, "_login_with_access_token", lambda *a: called.append(a))
    assert _author._prepare_codex_home() == home
    assert (home / "auth.json").read_text() == mounted and not called
    assert oct((home / "auth.json").stat().st_mode & 0o777) == "0o600"
    fresher = _chatgpt_login(time.time() + 7200, refresh="rt-2", last="2026-11-04T05:00:00+00:00")
    (home / "auth.json").write_text(fresher)
    _author._prepare_codex_home()
    assert (home / "auth.json").read_text() == fresher


def test_an_access_token_is_still_installed_with_codex_login(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "codex-home")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    called = []
    monkeypatch.setattr(_author, "_login_with_access_token", lambda home, token: called.append(token))
    _author._prepare_codex_home()
    assert called == ["tok-123"]


async def test_only_a_fresher_refreshable_login_is_written_back_and_only_once(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", home)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_login_two")
    monkeypatch.setattr(_author, "_stored_codex_digest", "")
    mounted = _chatgpt_login(time.time() + 3600)
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, mounted)
    stored: list[tuple[str, str]] = []

    async def create(name, value):
        stored.append((name, value))

    import flyte.remote

    monkeypatch.setattr(flyte.remote.Secret.create, "aio", create)
    timeline = _timeline._Timeline()
    (home / "auth.json").write_text(mounted)
    await _author._persist_codex_login(timeline)
    assert stored == []  # unchanged: nothing to write
    (home / "auth.json").write_text(
        _chatgpt_login(time.time() + 7200, refresh="", last="2026-11-04T06:00:00+00:00")
    )
    await _author._persist_codex_login(timeline)
    assert stored == []  # a copy that cannot refresh never replaces the chain
    fresher = _chatgpt_login(time.time() + 7200, refresh="rt-2", last="2026-11-04T06:00:00+00:00")
    (home / "auth.json").write_text(fresher)
    await _author._persist_codex_login(timeline)
    await _author._persist_codex_login(timeline)
    assert stored == [("codex_login_two", fresher)]


async def test_an_access_token_slot_never_writes_anything_back(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setenv(_session_config.CODEX_TOKEN_ENV_VAR, "tok-123")
    (tmp_path / "auth.json").write_text(_chatgpt_login(time.time() + 7200, refresh="rt-9"))
    stored = []

    async def create(name, value):
        stored.append(name)

    import flyte.remote

    monkeypatch.setattr(flyte.remote.Secret.create, "aio", create)
    await _author._persist_codex_login(_timeline._Timeline())
    assert stored == []


async def test_a_backend_that_refuses_to_overwrite_a_secret_gets_a_delete_first(monkeypatch):
    import flyte.remote

    calls: list[str] = []

    async def create(name, value):
        calls.append(f"create {name}")
        if calls.count(f"create {name}") == 1:
            raise RuntimeError("already exists")

    async def delete(name):
        calls.append(f"delete {name}")

    monkeypatch.setattr(
        flyte.remote,
        "Secret",
        SimpleNamespace(create=SimpleNamespace(aio=create), delete=SimpleNamespace(aio=delete)),
    )
    await _author._replace_secret("codex_login_two", "{}")
    assert calls == ["create codex_login_two", "delete codex_login_two", "create codex_login_two"]
