"""The browser MCP server for a Codex author: rendered from the Claude config, never copied."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import threading
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from flyte_agent_factory import (
    _local_browser,
    _profile,
    _session_config,
    _session_setup,
    _supervise,
    _usage,
    agent,
    browser,
)
from tests.test_codex_author import project as _codex_project

TOKEN_VALUE = "token-bytes-1234567890"


class FakeMcp:
    """A streamable-HTTP MCP server with one tool; records the headers of every request."""

    def __init__(self):
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"method": body["method"], "headers": dict(self.headers)})
                results = {
                    "initialize": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    "tools/list": {"tools": [{"name": "browser_navigate"}]},
                    "tools/call": {"content": [{"type": "text", "text": "page title: Example Domain"}]},
                }
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": results[body["method"]]})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()


# The `project` fixture of test_codex_author, under this module's own name for it.
project = _codex_project


@pytest.fixture
def mcp():
    server = FakeMcp()
    yield server
    server.close()


@pytest.fixture
def shared_browser(mcp, monkeypatch):
    """No in-pod Chromium, so the shared app is the browser, and it is the fake server."""
    monkeypatch.setattr(_local_browser, "available", lambda: False)
    monkeypatch.setattr(_session_setup, "browser_endpoint", lambda: mcp.endpoint)
    return mcp


def _codex_config(project):
    path = project.state_root / "codex" / "config.toml"
    return tomllib.loads(path.read_text()) if path.is_file() else None


async def _run(project, **kwargs):
    project.pump_that()
    return await agent.project.func("brief", **kwargs)


async def test_config_is_rendered_when_a_browser_exists(project, shared_browser):
    await _run(project, browser=True)
    assert _codex_config(project)["mcp_servers"]["browser"]["url"] == f"{shared_browser.endpoint}/mcp"


async def test_no_config_is_rendered_without_a_browser(project, shared_browser):
    await _run(project, browser=False)
    assert _codex_config(project) is None


async def test_no_config_when_neither_browser_works(project, monkeypatch):
    monkeypatch.setattr(_local_browser, "available", lambda: False)

    def broken():
        raise OSError("no service")

    monkeypatch.setattr(_session_setup, "browser_endpoint", broken)
    await _run(project, browser=True)
    assert _codex_config(project) is None
    assert project.thread()[1][0].startswith("Read .claude-flyte/BRIEF.md")  # the author started as today


async def test_the_codex_entry_matches_the_endpoint_and_headers_of_the_claude_config(project, monkeypatch):
    class Local:
        client_headers = {"Origin": "http://localhost:8931"}

        def __init__(self, **kwargs):
            pass

        async def start(self):
            return "http://localhost:8931"

        async def stop(self):
            pass

    monkeypatch.setattr(_local_browser, "available", lambda: True)
    monkeypatch.setattr(_local_browser, "LocalBrowser", Local)
    await _run(project, browser=True)
    claude = json.loads((project.workspace / ".mcp.json").read_text())["mcpServers"]["browser"]
    codex = _codex_config(project)["mcp_servers"]["browser"]
    assert codex["url"] == claude["url"] == "http://localhost:8931/mcp"
    assert codex["http_headers"] == claude["headers"] == {"Origin": "http://localhost:8931"}


def _grant(monkeypatch, *names):
    """Grant the run these identities: the profile has them, and the launch's env var names them."""
    profile = _session_config._PROFILE
    assert profile is not None and profile.browser is not None
    identities = tuple(_profile.BrowserIdentity(n, f"memory://{n}") for n in names)
    monkeypatch.setattr(
        _session_config,
        "_PROFILE",
        dataclasses.replace(profile, browser=dataclasses.replace(profile.browser, identities=identities)),
    )
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_IDENTITIES", ",".join(names))


async def test_the_personal_token_is_an_env_var_name_never_a_value(project, shared_browser, monkeypatch):
    """Stricter now: the entry holds neither the token nor its name; it points at the supervisor's gate."""
    _grant(monkeypatch, "personal")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", TOKEN_VALUE)
    await _run(project, browser=True)
    personal = _codex_config(project)["mcp_servers"]["browser-personal"]
    assert set(personal) == {"url"}
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/personal/mcp", personal["url"])
    for root in (project.workspace, project.state_root):
        for file in root.rglob("*"):
            if file.is_file() and file.name != "auth.json":
                assert TOKEN_VALUE.encode() not in file.read_bytes(), file


async def test_the_config_is_rerendered_on_restart_never_copied_from_the_checkpoint(
    project, mcp, monkeypatch
):
    monkeypatch.setattr(_local_browser, "available", lambda: False)
    monkeypatch.setattr(_session_setup, "browser_endpoint", lambda: "http://old-browser")
    await _run(project, browser=True)
    assert _codex_config(project)["mcp_servers"]["browser"]["url"] == "http://old-browser/mcp"

    # A new pod restores the old file with the checkpoint; the browser is now somewhere else.
    project.seen["resumed"] = True
    monkeypatch.setattr(_session_setup, "browser_endpoint", lambda: mcp.endpoint)
    await _run(project, browser=True)
    assert _codex_config(project)["mcp_servers"]["browser"]["url"] == f"{mcp.endpoint}/mcp"

    # ... and with no browser at all the stale entry does not survive.
    await _run(project, browser=False)
    assert _codex_config(project) is None


async def test_a_codex_author_calls_a_browser_tool_through_the_rendered_config(
    project, shared_browser, monkeypatch
):
    monkeypatch.setenv("FAKE_CODEX_MCP", "1")
    answers = []

    async def pump(bridge, proc, tl, stop, *, kickoff="", **kwargs):
        while kickoff and proc.thread_id is None:  # the first turn has started
            await asyncio.sleep(0.01)
        await proc.wait()
        answers.append(proc.last_message)
        stop.set()

    monkeypatch.setattr(_supervise, "_pump", pump)
    await agent.project.func("brief", browser=True)
    assert answers == ["browser: tools=['browser_navigate'] result=page title: Example Domain"]
    assert [r["method"] for r in shared_browser.requests] == ["initialize", "tools/list", "tools/call"]


async def test_the_bearer_header_reaches_the_server_from_the_environment(
    project, shared_browser, monkeypatch
):
    _grant(monkeypatch, "personal")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", TOKEN_VALUE)
    monkeypatch.setenv("FAKE_CODEX_MCP", "1")
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: shared_browser.endpoint)

    async def pump(bridge, proc, tl, stop, *, kickoff="", **kwargs):
        while kickoff and proc.thread_id is None:  # the first turn has started
            await asyncio.sleep(0.01)
        await proc.wait()
        stop.set()

    monkeypatch.setattr(_supervise, "_pump", pump)
    await agent.project.func("brief", browser=True)
    assert {r["headers"].get("Authorization") for r in shared_browser.requests} == {
        None,
        f"Bearer {TOKEN_VALUE}",
    }


@pytest.mark.parametrize("tunnel_up", [True, False])
async def test_an_exit_node_slot_offers_the_personal_browser_only_through_a_working_tunnel(
    project, shared_browser, monkeypatch, tunnel_up
):
    _grant(monkeypatch, "personal")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", TOKEN_VALUE)
    monkeypatch.setattr(browser, "_EXIT_NODE", "home-router")
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: "http://private-browser")
    reply = json.dumps({"exit_node": "up" if tunnel_up else "down"}).encode()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return reply

    real_urlopen = urllib.request.urlopen

    def urlopen(request, timeout):
        if isinstance(request, str) or request.full_url.startswith("http://127.0.0.1"):
            return real_urlopen(request, timeout=timeout)  # the gate's own traffic is not the health check
        assert request.full_url == "http://private-browser/health"
        assert request.get_header("Authorization") == f"Bearer {TOKEN_VALUE}"
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    await _run(project, browser=True)
    servers = _codex_config(project)["mcp_servers"]
    assert ("browser-personal" in servers) == tunnel_up


async def test_an_exit_node_slot_with_an_unreachable_app_renders_no_personal_entry(
    project, shared_browser, monkeypatch
):
    _grant(monkeypatch, "personal")
    monkeypatch.setattr(browser, "_EXIT_NODE", "home-router")
    # CI has no cluster namespace file; this pod does. Never depend on which one runs the test.
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", "/nonexistent/namespace")

    real_urlopen = urllib.request.urlopen

    def unreachable(request, timeout):
        if isinstance(request, str) or request.full_url.startswith("http://127.0.0.1"):
            return real_urlopen(request, timeout=timeout)
        raise OSError("503")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    await _run(project, browser=True)
    assert "browser-personal" not in _codex_config(project)["mcp_servers"]


async def test_a_claude_author_writes_no_codex_config_and_the_same_mcp_json(project, monkeypatch, tmp_path):
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_session_config, "CODEX_HOME_DIR", tmp_path / "unused-codex-home")
    (tmp_path / "unused-codex-home").mkdir()
    path = _session_setup._write_mcp_config("http://shared-browser", {"Origin": "http://x"})
    assert json.loads(path.read_text()) == {
        "mcpServers": {
            "browser": {"type": "http", "url": "http://shared-browser/mcp", "headers": {"Origin": "http://x"}}
        }
    }
    assert list((tmp_path / "unused-codex-home").iterdir()) == []


def test_the_rendered_toml_is_valid_for_odd_values():
    text = browser.codex_mcp_config(
        {
            "browser": {
                "type": "http",
                "url": 'http://h/"mcp"',
                "headers": {"X-Y": 'a"b\\c', "Authorization": "Bearer ${T}"},
            }
        }
    )
    assert tomllib.loads(text)["mcp_servers"]["browser"] == {
        "url": 'http://h/"mcp"',
        "http_headers": {"X-Y": 'a"b\\c'},
        "bearer_token_env_var": "T",
    }


def test_the_real_codex_cli_reads_the_rendered_config(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("codex"):
        pytest.skip("no codex CLI")
    (tmp_path / "config.toml").write_text(
        browser.codex_mcp_config(
            {
                "browser": {
                    "type": "http",
                    "url": "http://localhost:8931/mcp",
                    "headers": {"Origin": "http://localhost:8931"},
                },
                "browser-personal": {
                    "type": "http",
                    "url": "http://p/mcp",
                    "headers": {"Authorization": "Bearer ${BROWSER_PERSONAL_TOKEN}"},
                },
            }
        )
    )
    out = subprocess.run(
        ["codex", "mcp", "list", "--json"],
        env={"PATH": __import__("os").environ["PATH"], "CODEX_HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    listed = {s["name"]: s for s in json.loads(out.stdout)}
    assert listed["browser"]["transport"]["url"] == "http://localhost:8931/mcp"
    assert listed["browser-personal"]["transport"]["bearer_token_env_var"] == "BROWSER_PERSONAL_TOKEN"


def test_personal_exit_ready_is_false_when_the_app_cannot_be_located(monkeypatch):
    monkeypatch.setattr(browser, "_EXIT_NODE", "home-router")
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", "/nonexistent/namespace")
    assert browser.identity_exit_ready("personal", "a-token") is False


def test_identity_exit_ready_is_true_without_an_exit_node(monkeypatch):
    monkeypatch.setattr(browser, "_EXIT_NODE", "")
    assert browser.identity_exit_ready("personal", "a-token") is True
