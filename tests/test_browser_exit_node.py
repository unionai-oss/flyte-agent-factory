"""Only the optional in-pod browser uses the tailnet proxy."""

import asyncio
import json
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from flyte_agent_factory import _images, _local_browser, _profile, _session_config, _session_setup


def profile(table='exit_node = "egress"\ntailscale_secret = "my_tailnet_key"'):
    text = Path("profiles/example.toml").read_text()
    return _profile.parse(text.replace("[mcp]\n", f"[browser]\n{table}\n[mcp]\n"))


def test_profile_enables_browser_proxy_without_putting_credentials_in_env():
    p = profile()
    assert p.browser.tailscale_secret == "my_tailnet_key"
    env = p.deploy_env(p.slots[0])
    assert env["AGENT_FACTORY_BROWSER_EXIT_NODE"] == "egress"
    assert env["AGENT_FACTORY_BROWSER_TAILSCALE_SECRET"] == "my_tailnet_key"
    assert "my_tailnet_key" not in p.agent_secrets


@pytest.mark.parametrize(
    "table",
    ['tailscale_secret = "my_tailnet_key"', 'exit_node = "egress"', "tailscale_secret = 4"],
)
def test_in_pod_proxy_requires_both_settings(table):
    with pytest.raises(_profile.ProfileError):
        profile(table)


def test_personal_legacy_configuration_is_preserved():
    p = profile('personal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-a"\nexit_node = "egress"')
    assert p.browser.exit_node == "egress"
    assert "AGENT_FACTORY_BROWSER_EXIT_NODE" not in p.deploy_env(p.slots[0])


def test_disabled_deploy_env_and_mounts_are_unchanged(monkeypatch):
    from flyte_agent_factory import agent

    p = _profile.parse(Path("profiles/example.toml").read_text())
    # The deploy contract with the browser proxy off: nothing browser-exit-node related is added to it.
    assert p.deploy_env(p.slots[0]) == {
        "AGENT_FACTORY_ENV_NAME": "agent-a",
        "AGENT_FACTORY_SUBAGENT_ENV_NAME": "agent-a-sub",
        "AGENT_FACTORY_CLAUDE_TOKEN_SECRET": p.secrets.claude_token,
        "AGENT_FACTORY_BROWSER": "1",
        "AGENT_FACTORY_SECRETS": (
            "my_deploy_key=GIT_DEPLOY_KEY,my_gateway_key=LLM_GATEWAY_KEY,"
            "my_anthropic_key=PROJECT_ANTHROPIC_API_KEY,my_github_app_private_key=GITHUB_APP_PRIVATE_KEY"
        ),
    }
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    monkeypatch.delenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("AGENT_FACTORY_SECRETS", "my_deploy_key=GIT_DEPLOY_KEY")
    before = agent._secrets()
    assert len(before) == 1
    assert before[0].key == "my_deploy_key"
    assert before[0].as_env_var == "GIT_DEPLOY_KEY"
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_EXIT_NODE", "egress")
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", "my_tailnet_key")
    mounted = agent._secrets()
    assert mounted[:-1] == before
    assert mounted[-1].key == "my_tailnet_key"
    assert mounted[-1].as_env_var is None
    assert mounted[-1].mount == Path("/etc/flyte/secrets")


def test_session_environment_never_contains_tailscale_credentials(monkeypatch):

    monkeypatch.setenv("AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", "my_tailnet_key")
    env = {"MY_TAILNET_KEY": "private", "_UNION_MY_TAILNET_KEY": "private", "KEEP": "yes"}
    assert _session_setup._session_env(env) == {"KEEP": "yes"}


def test_tailscale_install_is_opt_in_and_checksum_pinned():
    factories: list[tuple[Callable[..., Any], dict[str, Any]]] = [
        (_images.agent_image, {"browser": True}),
        (_images.browser_image, {}),
    ]
    for factory, kwargs in factories:
        off = repr(factory(**kwargs)._layers)
        on = repr(factory(**kwargs, tailscale=True)._layers)
        assert "tailscale.tgz" not in off
        assert _images.TAILSCALE_SHA256 in on
        assert _images.TAILSCALE_VERSION in on
        assert "sha256sum -c -" in on
    assert "tailscale_" not in Path("src/flyte_agent_factory/_assets/browser/install.sh").read_text()


@pytest.mark.parametrize("enabled", [False, True])
def test_browser_launch_proxy_only_when_enabled(tmp_path, monkeypatch, enabled):
    browser = _local_browser.LocalBrowser(
        profile_dir=tmp_path / "profile",
        output_dir=tmp_path / "output",
        exit_node="egress" if enabled else "",
        tailscale_secret="my_tailnet_key" if enabled else "",
    )
    spawn = AsyncMock()
    monkeypatch.setattr(browser, "_start_tailscale", AsyncMock())
    monkeypatch.setattr(browser, "_spawn", spawn)
    monkeypatch.setattr(_local_browser, "_wait", AsyncMock(return_value=True))
    asyncio.run(browser.start())
    argv = spawn.call_args_list[-1].args[0]
    assert ("--proxy-server" in argv) == enabled
    if enabled:
        assert argv[argv.index("--proxy-server") + 1] == "socks5://127.0.0.1:1055"
        config = json.loads(Path(argv[argv.index("--config") + 1]).read_text())
        assert (
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
            in config["browser"]["launchOptions"]["args"]
        )
    asyncio.run(browser.stop())


def test_daemon_is_userspace_and_auth_is_passed_by_file(tmp_path, monkeypatch):
    from flyte_agent_factory import _browser_exit_node

    proxy = _browser_exit_node.BrowserExitNode("egress", "my_tailnet_key")
    spawn = AsyncMock()
    monkeypatch.setattr(proxy, "_spawn", spawn)
    command = AsyncMock(return_value={"BackendState": "Running", "ExitNodeStatus": {"Online": True}})
    monkeypatch.setattr(proxy, "_command", command)
    monkeypatch.setattr(_browser_exit_node, "_secret", lambda name: "private-key")
    monkeypatch.setattr(_local_browser, "_wait", AsyncMock(return_value=True))
    asyncio.run(proxy.start())
    argv = spawn.call_args.args[0]
    assert "--tun=userspace-networking" in argv
    assert "--socks5-server=127.0.0.1:1055" in argv
    assert any(arg.startswith("--statedir=/tmp/") for arg in argv)
    up = command.call_args_list[0].args
    selected = command.call_args_list[1].args
    assert selected == ("set", "--exit-node=egress", "--exit-node-allow-lan-access=false")
    assert any(arg.startswith("--auth-key=file:") for arg in up)
    assert "private-key" not in repr(command.call_args_list)
    state = proxy.directory
    asyncio.run(proxy.stop())
    assert not state.exists()


@pytest.mark.parametrize("available", [False, True])
def test_failed_proxy_does_not_fall_back_to_shared_app(monkeypatch, available):

    monkeypatch.setenv("AGENT_FACTORY_BROWSER_EXIT_NODE", "egress")
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", "my_tailnet_key")
    monkeypatch.setattr(_local_browser, "available", lambda: available)
    monkeypatch.setattr(
        _local_browser.LocalBrowser, "start", AsyncMock(side_effect=RuntimeError("proxy unavailable"))
    )
    endpoint = Mock(side_effect=AssertionError("must not use a direct browser"))
    monkeypatch.setattr(_session_setup, "browser_endpoint", endpoint)
    timeline = Mock()
    assert asyncio.run(_session_setup._start_browser(timeline)) == (False, None)
    endpoint.assert_not_called()
    assert "without browser" in timeline.row.call_args.args[1]


def test_offline_exit_node_is_rejected(monkeypatch):
    from flyte_agent_factory import _browser_exit_node

    proxy = _browser_exit_node.BrowserExitNode("egress", "my_tailnet_key")
    monkeypatch.setattr(proxy, "_spawn", AsyncMock())
    command = AsyncMock(return_value={"BackendState": "Running", "ExitNodeStatus": {"Online": False}})
    monkeypatch.setattr(proxy, "_command", command)
    monkeypatch.setattr(_browser_exit_node, "_secret", lambda name: "private-key")
    monkeypatch.setattr(_local_browser, "_wait", AsyncMock(return_value=True))
    with pytest.raises(RuntimeError, match="exit node"):
        asyncio.run(proxy.start())
    asyncio.run(proxy.stop())


@pytest.mark.parametrize("failure", [OSError("missing tailscaled"), RuntimeError("auth failed")])
def test_proxy_failure_never_launches_chromium(tmp_path, monkeypatch, failure):
    browser = _local_browser.LocalBrowser(
        profile_dir=tmp_path / "p",
        output_dir=tmp_path / "o",
        exit_node="egress",
        tailscale_secret="my_tailnet_key",
    )
    spawn = AsyncMock()
    monkeypatch.setattr(browser, "_start_tailscale", AsyncMock(side_effect=failure))
    monkeypatch.setattr(browser, "_spawn", spawn)
    with pytest.raises(type(failure)):
        asyncio.run(browser.start())
    spawn.assert_not_called()
    asyncio.run(browser.stop())


def test_auth_file_is_private_and_removed_after_failure(monkeypatch):
    from flyte_agent_factory import _browser_exit_node

    proxy = _browser_exit_node.BrowserExitNode("egress", "my_tailnet_key")
    monkeypatch.setattr(proxy, "_spawn", AsyncMock())
    monkeypatch.setattr(_browser_exit_node, "_secret", lambda name: "private-key")
    monkeypatch.setattr(_local_browser, "_wait", AsyncMock(return_value=True))

    async def authenticate(*args):
        auth = Path(
            next(arg.removeprefix("--auth-key=file:") for arg in args if arg.startswith("--auth-key="))
        )
        assert auth.read_text() == "private-key"
        assert auth.stat().st_mode & 0o777 == 0o600
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(proxy, "_command", authenticate)
    with pytest.raises(RuntimeError, match="authentication failed"):
        asyncio.run(proxy.start())
    assert not (proxy.directory / "auth-key").exists()
    asyncio.run(proxy.stop())


def test_probe_uses_browser_page_and_explicit_direct_http(monkeypatch, capsys):
    from ops.probes import browser_exit_node as probe

    monkeypatch.setattr(_local_browser.LocalBrowser, "start", AsyncMock())
    monkeypatch.setattr(_local_browser.LocalBrowser, "stop", AsyncMock())
    browser_call = Mock(return_value={"result": {"content": [{"text": '### Result\n"203.0.113.1"'}]}})
    monkeypatch.setattr(probe, "_rpc", browser_call)
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b"198.51.100.1"
    opener = Mock()
    opener.open.return_value = response
    build = Mock(return_value=opener)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    monkeypatch.setattr("sys.argv", ["probe", "--exit-node", "egress", "--secret", "my_tailnet_key"])
    probe.main()
    assert capsys.readouterr().out == "203.0.113.1\n198.51.100.1\n"
    assert build.call_args.args[0].proxies == {}
    assert "page.goto" in browser_call.call_args.args[1][0][1]["arguments"]["code"]


def test_failed_proxy_removes_a_checkpointed_direct_browser(tmp_path, monkeypatch):

    config = tmp_path / ".mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"browser": {"url": "http://shared-browser/mcp"}, "keep": {}}})
    )
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_EXIT_NODE", "egress")
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_TAILSCALE_SECRET", "my_tailnet_key")
    monkeypatch.setattr(_local_browser, "available", lambda: False)
    assert asyncio.run(_session_setup._start_browser(Mock())) == (False, None)
    assert json.loads(config.read_text())["mcpServers"] == {"keep": {}}


def test_personal_app_and_in_pod_proxy_can_coexist():
    p = profile(
        'personal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-a"\n'
        'exit_node = "egress"\ntailscale_secret = "my_tailnet_key"'
    )
    assert p.deploy_env(p.slots[0])["AGENT_FACTORY_BROWSER_TAILSCALE_SECRET"] == "my_tailnet_key"
