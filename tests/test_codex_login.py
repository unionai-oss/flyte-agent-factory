"""`codex_bootstrap_login`: a ChatGPT login signed in inside the cluster, stored as a slot's `codex_login`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flyte_agent_factory import _codex_login, _timeline


def test_the_link_and_a_complete_code_are_read_from_codex_output():
    out = "\x1b[1mOpen\x1b[0m https://auth.openai.com/codex/device\nand enter this code: ABCD-12345 \n"
    assert _codex_login.device_prompt(out) == ("https://auth.openai.com/codex/device", "ABCD-12345")
    # A code cut off at the end of a read is not a code yet.
    assert _codex_login.device_prompt("https://auth.openai.com/codex/device\ncode: ABCD-12")[1] == ""
    assert _codex_login.device_prompt("starting...") == ("", "")


def _login() -> str:
    return json.dumps({"tokens": {"access_token": "a.b.c", "refresh_token": "rt", "id_token": "x"}})


async def test_a_completed_sign_in_is_stored_as_the_secret(monkeypatch):
    async def device_login(home: Path, max_wait_s, timeline, attempt):
        (home / "auth.json").write_text(_login())
        return 0, "ok"

    stored: list[tuple[str, str]] = []

    async def store(name, value):
        stored.append((name, value))

    monkeypatch.setattr(_codex_login, "_device_login", device_login)
    summary = await _codex_login.bootstrap("codex_login_two", store=store, timeline=_timeline._Timeline())
    assert stored == [("codex_login_two", _login())]
    assert "codex_login_two" in summary


async def test_an_expired_code_starts_a_fresh_one_and_gives_up_after_the_attempts(monkeypatch):
    calls = []

    async def device_login(home, max_wait_s, timeline, attempt):
        calls.append(attempt)
        return 1, "code expired"

    async def store(name, value):
        raise AssertionError("nothing to store")

    monkeypatch.setattr(_codex_login, "_device_login", device_login)
    with pytest.raises(RuntimeError, match="did not complete in 2 attempts"):
        await _codex_login.bootstrap("x", store=store, timeline=_timeline._Timeline(), attempts=2)
    assert calls == [1, 2]
