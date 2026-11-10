import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from flyte.io import Dir

from flyte_agent_factory import (
    _checkpoint,
    _contract,
    _instructions,
    _profile,
    _review,
    _reviewer,
    _session_config,
    _session_setup,
    _timeline,
)

ASSETS = Path(__file__).resolve().parents[1] / "src/flyte_agent_factory/_assets/browser"
spec = importlib.util.spec_from_file_location("personal_runtime", ASSETS / "personal.py")
assert spec is not None and spec.loader is not None
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_snapshot_restore(tmp_path):
    profile = tmp_path / "profile"
    (profile / "Default/Cache").mkdir(parents=True)
    (profile / "Default/Cookies").write_bytes(b"private cookies")
    (profile / "Default/Cache/data").write_text("cache")
    (profile / "SingletonLock").symlink_to("oldhost-123")
    uri = "memory://personal-test/profile.tar.gz"
    runtime.snapshot(profile, uri)
    restored = tmp_path / "restored"
    runtime.restore(restored, uri)
    assert (restored / "Default/Cookies").read_bytes() == b"private cookies"
    assert not (restored / "Default/Cache").exists()
    assert not (restored / "SingletonLock").is_symlink()


def test_lock_live_and_foreign_refused(tmp_path):
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")
    with pytest.raises(RuntimeError, match="still owns"):
        runtime.clear_stale_lock(tmp_path)
    lock.unlink()
    lock.symlink_to("other-host-123")
    with pytest.raises(RuntimeError, match="another host"):
        runtime.clear_stale_lock(tmp_path)


def test_dead_lock_removed(tmp_path, monkeypatch):
    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-123")

    def dead(*args):
        raise ProcessLookupError

    monkeypatch.setattr(runtime.os, "kill", dead)
    runtime.clear_stale_lock(tmp_path)
    assert not (tmp_path / "SingletonLock").is_symlink()


def test_auth_key_request():
    requests = []

    def request(req, timeout):
        requests.append(req)
        return io.BytesIO(
            json.dumps({"access_token": "token"} if len(requests) == 1 else {"key": "private-key"}).encode()
        )

    assert runtime.mint_auth_key("client", "secret", request) == "private-key"
    assert b"grant_type=client_credentials" in requests[0].data
    assert requests[1].get_header("Authorization") == "Bearer token"
    create = json.loads(requests[1].data)["capabilities"]["devices"]["create"]
    assert create == {"reusable": False, "ephemeral": True, "preauthorized": True, "tags": ["tag:k8s"]}


def test_auth_key_uses_the_configured_tailscale_tag(monkeypatch):
    monkeypatch.setenv("BROWSER_TAILSCALE_TAG", "tag:factory-browser")
    requests = []

    def request(req, timeout):
        requests.append(req)
        return io.BytesIO(
            json.dumps({"access_token": "token"} if len(requests) == 1 else {"key": "private-key"}).encode()
        )

    runtime.mint_auth_key("client", "secret", request)
    assert json.loads(requests[1].data)["capabilities"]["devices"]["create"]["tags"] == [
        "tag:factory-browser"
    ]


@pytest.mark.parametrize("tag", ["k8s", "tag:", "tag:Bad", "tag:a b", "tag:a,tag:b", ""])
def test_auth_key_refuses_a_malformed_tailscale_tag(monkeypatch, tag):
    monkeypatch.setenv("BROWSER_TAILSCALE_TAG", tag)

    def request(req, timeout):
        raise AssertionError("no request may be made with a malformed tag")

    with pytest.raises(ValueError, match="BROWSER_TAILSCALE_TAG"):
        runtime.mint_auth_key("client", "secret", request)


def test_exit_down_does_not_fetch_direct(monkeypatch):
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return b'{"BackendState":"Running","ExitNodeStatus":{"Online":false}}'

    monkeypatch.setattr(runtime.subprocess, "check_output", output)
    with pytest.raises(RuntimeError, match="blocked"):
        runtime.egress("home")
    assert len(calls) == 1
    assert calls[0][0] == "tailscale"


def test_browser_shell_syntax():
    for name in ("start.sh", "install.sh"):
        subprocess.run(["bash", "-n", str(ASSETS / name)], check=True)


@pytest.mark.parametrize("enabled,supervisor", [(False, True), (True, True), (True, False)])
def test_personal_config_and_guidance(tmp_path, monkeypatch, enabled, supervisor):
    from dataclasses import replace

    from flyte_agent_factory import _profile, browser

    profile = _profile.current()
    assert profile is not None
    personal_env = profile.slots[0].env
    profile = replace(
        profile,
        browser=_profile.Browser(
            identities=(_profile.BrowserIdentity("personal", "memory://profile"),) if enabled else ()
        ),
    )
    monkeypatch.setattr(_profile, "current", lambda: profile)
    # Import the environment definition in a fresh deploy interpreter, then emulate
    # the platform: only env_vars and mounted secrets reach the runtime pod.
    deploy_env = dict(os.environ)
    deploy_env.pop(_profile.PROFILE_ENV, None)
    deploy_env[_profile.TOML_ENV] = (Path(__file__).parent / "profiles/work.toml").read_text() + (
        f'\n[browser]\npersonal = {str(enabled).lower()}\nprofile_uri = "memory://profile"\n'
        + (f'personal_env = "{personal_env}"\n' if enabled else "")
    )
    deploy_env["AGENT_FACTORY_ENV_NAME"] = personal_env if supervisor else profile.slots[1].env
    code = (
        "from flyte_agent_factory import agent; import json; "
        "print(json.dumps({'env':agent.env.env_vars,'secrets':[s.key for s in agent.env.secrets]}))"
    )
    definition = json.loads(subprocess.check_output([sys.executable, "-c", code], env=deploy_env))
    pod_env = definition["env"]
    assert "AGENT_FACTORY_ENV_NAME" not in pod_env
    # Requirement change from the owner: every slot mounts the token; `personal_env` grants nothing.
    assert ("browser_personal_token" in definition["secrets"]) == enabled
    if "browser_personal_token" in definition["secrets"]:
        pod_env["BROWSER_PERSONAL_TOKEN"] = "test-token"
    # The grant is the run's, not the slot's: a slot that mounts the token still gives no entry without one.
    assert "BROWSER_PERSONAL_ENABLED" not in pod_env
    if enabled:
        pod_env["AGENT_FACTORY_BROWSER_IDENTITIES"] = "personal"
    monkeypatch.setattr(os, "environ", pod_env)
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: "http://private-browser")
    # A resumed project must also discard a stale entry from a different session.
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser-personal": {}}}))
    gate = _session_setup._start_browser_gate(cast(Any, SimpleNamespace(row=lambda *a: None)))
    try:
        path = _session_setup._write_mcp_config("http://shared-browser")
        servers = json.loads(path.read_text())["mcpServers"]
        assert ("browser-personal" in servers) == enabled
        memory = _instructions._project_memory(True, personal_browsers=gate.identities if gate else ())
        assert ("Personal browsers" in memory) == enabled
        assert servers["browser"]["url"] == "http://shared-browser/mcp"
        if enabled:
            assert gate is not None
            # No header at all: the gate adds the token, so it is neither in this file nor in the session.
            assert servers["browser-personal"] == {"type": "http", "url": f"{gate.url}/personal/mcp"}
            assert "test-token" not in path.read_text()
    finally:
        _session_setup._stop_browser_gate()


def test_personal_profile_requires_storage():
    from flyte_agent_factory import _profile

    text = (Path(__file__).parent / "profiles/work.toml").read_text()
    with pytest.raises(_profile.ProfileError, match="requires profile_uri"):
        _profile.parse(text + "\n[browser]\npersonal = true\n")
    browser = '\n[browser]\npersonal = true\nprofile_uri = "memory://profile"\n'
    profile = _profile.parse(text + browser)
    assert profile.browser.personal


@pytest.mark.parametrize(
    "state,online",
    [("Running", True), ("NeedsLogin", True), ("Stopped", True), ("Running", False), ("Error", False)],
)
async def test_forward_proxy_status_gate(state, online):
    import asyncio

    calls = []

    async def upstream(reader, writer):
        calls.append(True)
        writer.write(await reader.read(4))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def source():
        if state == "Error":
            raise OSError("status unavailable")
        return {"BackendState": state, "ExitNodeStatus": {"Online": online}}

    gate = runtime.ExitGate("home", source)
    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as target:
        port = target.sockets[0].getsockname()[1]
        async with await asyncio.start_server(
            lambda r, w: runtime.forward_proxy(r, w, gate, port), "127.0.0.1", 0
        ) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
            writer.write(b"test")
            await writer.drain()
            try:
                data = await asyncio.wait_for(reader.read(4), 30)
            except ConnectionResetError:
                data = b""
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError:
                pass
    assert data == (b"test" if state == "Running" and online else b"")
    assert bool(calls) == (state == "Running" and online)


def test_status_errors_and_cache(monkeypatch):
    calls = []
    clock = [10]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])

    def status():
        calls.append(True)
        if len(calls) == 1:
            return {"BackendState": "Running", "ExitNodeStatus": {"Online": True}}
        raise OSError("private error")

    gate = runtime.ExitGate("home", status)
    assert gate.ready()
    clock[0] += 1
    assert gate.ready()
    assert len(calls) == 1
    clock[0] += 1
    assert not gate.ready()
    assert len(calls) == 2


@pytest.mark.parametrize("node", ["home", "100.64.0.1", "fd7a:115c:a1e0::1"])
def test_exit_node_command_sequence(monkeypatch, node):
    calls = []

    def output(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"Peer": {"id": {"HostName": "home"}}}).encode()

    monkeypatch.setattr(runtime.subprocess, "check_output", output)
    runtime.connect_exit_node(node, "private")
    commands = [args[2:] for args, _ in calls]
    assert commands[0][0] == "up"
    assert not any(arg.startswith("--exit-node") for arg in commands[0])
    assert commands[-1] == ["set", f"--exit-node={node}", "--exit-node-allow-lan-access=false"]
    assert ["status", "--json"] in commands if node == "home" else len(commands) == 2


def test_credentials_not_in_children(monkeypatch):
    for name in (*runtime.OAUTH_ENV, "BROWSER_PERSONAL_TOKEN"):
        monkeypatch.setenv(name, "private")
    calls: list[dict[str, Any]] = []

    def check_output(args, **kw):
        calls.append(kw)
        return b"{}"

    monkeypatch.setattr(runtime.subprocess, "check_output", check_output)
    runtime.tailscale("status", "--json")
    runtime.egress("")
    for kwargs in calls:
        assert not {*runtime.OAUTH_ENV, "BROWSER_PERSONAL_TOKEN"} & kwargs["env"].keys()
    shell = (ASSETS / "start.sh").read_text()
    assert shell.index("unset TAILSCALE_OAUTH_CLIENT_ID") < shell.index("Xvfb :99 -screen")


@pytest.mark.parametrize("data", [b"garbage", b"\x1f\x8b\x08", b""])
@pytest.mark.parametrize("previous", [True, False])
def test_corrupt_restore_falls_back(tmp_path, data, previous, capsys):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"private cookies")
    uri = (tmp_path / "snapshot.tar.gz").as_uri()
    runtime.snapshot(profile, uri)
    if previous:
        (profile / "Cookies").write_bytes(b"new cookies")
        runtime.snapshot(profile, uri)
    (tmp_path / "snapshot.tar.gz").write_bytes(data)
    restored = tmp_path / "restored"
    runtime.restore(restored, uri)
    assert restored.is_dir()
    if previous:
        assert (restored / "Cookies").read_bytes() == b"private cookies"
    else:
        assert not list(restored.iterdir())
    assert "private cookies" not in capsys.readouterr().out


def test_lease_single_writer_expiry_and_release():
    from obstore.store import MemoryStore

    store = MemoryStore()
    first = runtime.Lease("memory://test/profile", store)
    second = runtime.Lease("memory://test/profile", store)
    assert first.renew(now=100)
    assert not second.renew(now=101)
    assert first.renew(now=200)
    assert not second.renew(now=221)
    assert second.renew(now=321)
    first.release()
    assert not first.renew(now=322)
    second.release()
    assert not first.renew(now=323)
    assert runtime.Lease("memory://test/profile", store).renew(now=323)


@pytest.mark.parametrize("target", ["malformed", "host-not-a-pid"])
def test_malformed_locks_not_removed(tmp_path, target):
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(target)
    with pytest.raises(RuntimeError, match="Unrecognized"):
        runtime.clear_stale_lock(tmp_path)
    assert lock.is_symlink()


def test_permission_denied_lock_not_removed(tmp_path, monkeypatch):
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-123")

    def denied(*args):
        raise PermissionError

    monkeypatch.setattr(runtime.os, "kill", denied)
    with pytest.raises(RuntimeError, match="not ours"):
        runtime.clear_stale_lock(tmp_path)
    assert lock.is_symlink()


def test_snapshot_skips_disappeared_files_and_caches(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"cookies")
    (profile / "vanishes").write_bytes(b"temporary")
    for name in runtime.EXCLUDED:
        (profile / "Default" / name).mkdir(parents=True, exist_ok=True)
        (profile / "Default" / name / "data").write_bytes(b"cache")
    add = runtime.tarfile.TarFile.add

    def race(self, path, *args, **kwargs):
        if Path(path).name == "vanishes":
            raise FileNotFoundError
        return add(self, path, *args, **kwargs)

    monkeypatch.setattr(runtime.tarfile.TarFile, "add", race)
    uri = (tmp_path / "profile.tar.gz").as_uri()
    runtime.snapshot(profile, uri)
    restored = tmp_path / "restored"
    runtime.restore(restored, uri)
    assert [p.name for p in restored.iterdir()] == ["Cookies"]


def test_personal_bearer_auth():
    assert runtime.authorized("Bearer token", "token")
    assert not runtime.authorized("", "token")
    assert not runtime.authorized("Bearer wrong", "token")
    assert not runtime.authorized("Bearer töken", "token")


def test_tailscaled_restarts_and_uses_private_environment(tmp_path, monkeypatch):
    calls = []
    socket_path = tmp_path / "private/tailscaled.sock"
    monkeypatch.setattr(runtime, "TS_SOCKET", str(socket_path))
    for name in runtime.OAUTH_ENV:
        monkeypatch.setenv(name, "secret")
    gate = runtime.ExitGate("home", dict)
    network = runtime.Tailscale("home", "client", "secret", gate)
    monkeypatch.setattr(runtime, "mint_auth_key", lambda *args: "private-key")
    monkeypatch.setattr(runtime, "connect_exit_node", lambda *args: None)

    class DeadDaemon:
        def poll(self):
            return 1

    def launch(args, **kwargs):
        calls.append((args, kwargs))
        socket_path.touch()
        if len(calls) == 2:
            network.stop.set()
        return DeadDaemon()

    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    network.supervise()
    assert len(calls) == 2
    assert socket_path.parent.stat().st_mode & 0o777 == 0o700
    for args, kwargs in calls:
        assert "--no-logs-no-support" in args
        assert f"--socket={socket_path}" in args
        assert not set(runtime.OAUTH_ENV) & kwargs["env"].keys()
    assert not gate.allowed


def test_exit_node_discovery_is_bounded(monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def tailscale(*args, **kw):
        calls.append(args)
        return b'{"Peer":{}}'

    monkeypatch.setattr(runtime, "tailscale", tailscale)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="not discovered"):
        runtime.connect_exit_node("absent", "key")
    assert len(calls) == 61
    assert not any(args[0] == "set" for args in calls)


def test_snapshot_cannot_publish_without_lease(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"cookies")
    uri = (tmp_path / "profile.tar.gz").as_uri()

    class Lost:
        def publish(self, *args):
            raise RuntimeError("Personal profile lease lost before snapshot publish")

    with pytest.raises(RuntimeError, match="lease lost"):
        runtime.snapshot(profile, uri, Lost())
    assert not (tmp_path / "profile.tar.gz").exists()
    assert not list(tmp_path.glob("*.pending-*"))


def test_nginx_personal_routes_require_auth(tmp_path, monkeypatch):
    shell = (ASSETS / "start.sh").read_text()
    renderer = shell.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    template = (ASSETS / "nginx.conf.template").read_text()
    output = []
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/opt/browser/nginx.conf.template":
            return template
        return original_read(path, *args, **kwargs)

    monkeypatch.setenv("BROWSER_PERSONAL", "1")
    monkeypatch.setattr(runtime.os.sys, "argv", ["-", "8080", str(tmp_path)])
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", lambda self, text: output.append(text))
    exec(compile(renderer, "start.sh:renderer", "exec"), {})
    rendered = output[0]
    for route in ("location /mcp {", "location /output/ {"):
        location = rendered.split(route, 1)[1].split("}", 1)[0]
        assert "auth_request /_personal_gate;" in location
    gate = rendered.split("location = /_personal_gate {", 1)[1].split("}", 1)[0]
    assert "internal;" in gate
    assert "proxy_pass http://127.0.0.1:8932/exit-check;" in gate
    assert "proxy_pass_request_body off;" in gate


def test_unsupported_architecture_fails_install(tmp_path):
    uname = tmp_path / "uname"
    uname.write_text("#!/bin/sh\necho riscv64\n")
    uname.chmod(0o755)
    result = subprocess.run(
        ["sh", str(ASSETS / "install.sh")],
        env={
            **os.environ,
            "PATH": str(tmp_path) + ":" + os.environ["PATH"],
            "NODE_VERSION": "22",
            "PLAYWRIGHT_MCP_VERSION": "0.0.83",
        },
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 1
    assert "unsupported architecture riscv64" in result.stderr


@pytest.mark.parametrize("held", [True, False])
def test_shutdown_stops_browser_before_snapshot_and_logout(monkeypatch, held):
    calls = []
    monkeypatch.setattr(runtime, "stop_browser", lambda: calls.append("browser"))
    monkeypatch.setattr(runtime, "snapshot", lambda *args: calls.append("snapshot"))

    class Lease:
        def renew(self):
            calls.append("renew")
            return True

    class Network:
        def close(self):
            calls.append("logout")

    runtime.shutdown(None, None, Lease(), Network(), held)
    assert calls == (["browser", "renew", "snapshot", "logout"] if held else ["browser", "logout"])


def test_logout_timeout_and_environment(monkeypatch):
    for name in runtime.OAUTH_ENV:
        monkeypatch.setenv(name, "secret")
    calls = []

    def logout(*args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runtime.subprocess, "run", logout)
    network = runtime.Tailscale("home", "client", "secret", runtime.ExitGate("home", dict))
    network.close()
    assert network.stop.is_set()
    assert calls[0][0][0][-1] == "logout"
    assert calls[0][1]["timeout"] == 5
    assert not set(runtime.OAUTH_ENV) & calls[0][1]["env"].keys()


async def test_open_proxy_tunnel_closes_when_exit_node_drops():
    import asyncio

    status = {"BackendState": "Running", "ExitNodeStatus": {"Online": True}}
    gate = runtime.ExitGate("home", lambda: status)
    connections = []

    async def upstream(reader, writer):
        connections.append(writer)
        try:
            while data := await reader.read(4):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as target:
        port = target.sockets[0].getsockname()[1]
        async with await asyncio.start_server(
            lambda r, w: runtime.forward_proxy(r, w, gate, port), "127.0.0.1", 0
        ) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
            writer.write(b"test")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 30) == b"test"
            status["BackendState"] = "NeedsLogin"
            gate.checked = 0
            assert await asyncio.wait_for(reader.read(), 30) == b""
            writer.close()
            await writer.wait_closed()
    assert len(connections) == 1


def test_gate_accepts_only_configured_active_peer():
    peer = {"HostName": "home", "Online": True, "ExitNode": True}
    status = {"BackendState": "Running", "Peer": {"id": peer}}
    assert runtime.ExitGate("home", lambda: status).ready()
    assert not runtime.ExitGate("other", lambda: status).ready()
    peer["ExitNode"] = False
    assert not runtime.ExitGate("home", lambda: status).ready()


def test_leased_snapshots_keep_two_generations_and_recover(tmp_path):
    from obstore.store import MemoryStore

    store = MemoryStore()
    uri = f"memory://leased-{tmp_path.name}/profile.tar.gz"
    lease = runtime.Lease(uri, store)
    assert lease.renew()
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"one")
    runtime.snapshot(profile, uri, lease)
    (profile / "Cookies").write_bytes(b"two")
    runtime.snapshot(profile, uri, lease)
    (profile / "Cookies").write_bytes(b"three")
    runtime.snapshot(profile, uri, lease)
    fs, key = runtime.fsspec.core.url_to_fs(uri)
    assert len(fs.glob(key + ".pending-*")) == 2
    with fs.open(lease.archives[0], "wb") as target:
        target.write(b"truncated")
    lease.release()
    replacement = runtime.Lease(uri, store)
    assert replacement.renew()
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, replacement)
    assert (restored / "Cookies").read_bytes() == b"two"
    runtime.snapshot(restored, uri, replacement)
    assert replacement.archives[1] == lease.archives[1]
    assert len(fs.glob(key + ".pending-*")) == 2


def test_delayed_old_owner_cannot_publish_after_takeover(tmp_path, monkeypatch):
    from obstore.store import MemoryStore

    store = MemoryStore()
    old = runtime.Lease("memory://test/profile", store)
    new = runtime.Lease("memory://test/profile", store)
    assert old.renew(now=100)
    assert new.renew(now=221)
    original_put = runtime.obstore.put
    monkeypatch.setattr(runtime.time, "time", lambda: 222)
    with pytest.raises(RuntimeError, match="lease lost"):
        old.publish("old-archive", None)
    record = json.loads(bytes(runtime.obstore.get(store, new.key).bytes()))
    assert record["owner"] == new.owner
    assert record["archives"] == []

    # Lose ownership after renewal but before the conditional pointer write.
    def raced_put(target, path, data, **kwargs):
        if json.loads(data).get("archives") == ["new-archive"]:
            successor = {"owner": "successor", "expires": 500, "archives": ["successor-archive"]}
            original_put(target, path, json.dumps(successor).encode())
        return original_put(target, path, data, **kwargs)

    monkeypatch.setattr(runtime.obstore, "put", raced_put)
    with pytest.raises(RuntimeError, match="lease lost"):
        new.publish("new-archive", None)
    record = json.loads(bytes(runtime.obstore.get(store, new.key).bytes()))
    assert record["archives"] == ["successor-archive"]


def test_live_lease_blocks_startup_until_bounded_timeout(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.setenv("BROWSER_PROFILE_URI", "memory://test/profile")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "test-token")
    restored = []
    launched = []
    monkeypatch.setattr(runtime, "restore", lambda *args: restored.append(True))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *args, **kw: launched.append(True))
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: None)
    clock = iter([0, 181])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))

    class Occupied:
        def renew(self):
            return False

    monkeypatch.setattr(runtime, "Lease", lambda uri: Occupied())
    with pytest.raises(RuntimeError, match="lease acquisition timed out"):
        runtime.main()
    assert not restored
    assert not launched


# --- re-QA round 3 -----------------------------------------------------------------------------


def _render_nginx(tmp_path, monkeypatch, port="8080", personal="1"):
    shell = (ASSETS / "start.sh").read_text()
    renderer = shell.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]
    template = (ASSETS / "nginx.conf.template").read_text()
    output = []
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if str(path) == "/opt/browser/nginx.conf.template":
            return template
        return original_read(path, *args, **kwargs)

    monkeypatch.setenv("BROWSER_PERSONAL", personal)
    monkeypatch.setattr(runtime.os.sys, "argv", ["-", port, str(tmp_path)])
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(Path, "write_text", lambda self, text: output.append(text))
    exec(compile(renderer, "start.sh:renderer", "exec"), {})
    return output[0]


def _control(**overrides):
    class Gate:
        def ready(self):
            return True

    args = {
        "token": "test-token",
        "exit_node": "",
        "gate": Gate(),
        "proxy": None,
        "sessions": runtime.McpSessions(),
        "views": runtime.ViewAccess("vncpass1"),
        "restart": lambda: None,
    }
    return runtime.Control(**{**args, **overrides})


@pytest.fixture
def control_server():
    servers = []

    def start(control):
        server = runtime.ThreadingHTTPServer(("127.0.0.1", 0), runtime.handler_for(control))
        runtime.threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()


def _request(port, path, method="GET", headers=None, body=None):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    return response.status, dict(response.getheaders()), body


def test_view_is_gated_in_rendered_nginx(tmp_path, monkeypatch):
    rendered = _render_nginx(tmp_path, monkeypatch)
    view = rendered.split("location /view/ {", 1)[1].split("}", 1)[0]
    assert "auth_request /_view_gate;" in view
    assert 'proxy_set_header Cookie "";' in view
    # No location may serve the websocket (or noVNC) outside that gate.
    assert "websockify" not in rendered.replace("/view/", "").split("location /view/")[0]
    gate = rendered.split("location = /_view_gate {", 1)[1].split("}", 1)[0]
    assert "internal;" in gate and "proxy_pass http://127.0.0.1:8932/view-auth;" in gate
    # The shared app's /view/ stays under platform auth alone.
    shared = _render_nginx(tmp_path, monkeypatch, personal="0")
    assert "_view_gate" not in shared


def test_the_view_is_served_with_a_policy_that_blocks_third_party_scripts(tmp_path, monkeypatch):
    # The platform's edge injects analytics into pages, which read the noVNC URL (and its password).
    view = _render_nginx(tmp_path, monkeypatch).split("location /view/ {", 1)[1].split("}", 1)[0]
    assert "add_header Content-Security-Policy \"default-src 'self';" in view and " always;" in view
    assert "add_header Referrer-Policy no-referrer always;" in view


def test_the_vnc_password_is_handed_over_in_the_fragment_not_the_query(control_server):
    views = runtime.ViewAccess("vncpass1")
    port = control_server(_control(views=views))
    status, headers, _ = _login(port, views.mint_ticket())
    assert status == 303
    path, _, fragment = headers["Location"].partition("#")
    assert path == "/view/vnc.html" and "password=vncpass1" in fragment and "autoconnect=1" in fragment


def test_view_ticket_and_cookie_flow(control_server):
    now = [1000.0]
    views = runtime.ViewAccess("vncpass1", clock=lambda: now[0])
    port = control_server(_control(views=views))
    bearer = {"Authorization": "Bearer test-token"}
    assert _request(port, "/view-auth")[0] == 401
    assert _request(port, "/view-auth", headers={"Authorization": "Bearer wrong"})[0] == 401
    assert _request(port, "/view-auth", headers=bearer)[0] == 204
    assert _request(port, "/view-ticket")[0] == 401
    status, _, ticket = _request(port, "/view-ticket", headers=bearer)
    assert status == 200
    # A GET (what a link preview fetches) only shows the page; the ticket is spent by its POST.
    for _ in range(2):
        status, headers, page = _request(port, f"/view-login?ticket={ticket.decode()}")
        assert status == 200 and headers["Content-Type"].startswith("text/html")
        assert "Set-Cookie" not in headers and "vncpass1" not in page.decode()
        assert 'method="post"' in page.decode() and ticket.decode() in page.decode()
    status, headers, _ = _login(port, ticket.decode())
    assert status == 303
    assert "password=vncpass1" in headers["Location"] and "path=view%2Fwebsockify" in headers["Location"]
    cookie = headers["Set-Cookie"]
    assert {"HttpOnly", "Secure", "SameSite=Lax", "Path=/view/"} <= {p.strip() for p in cookie.split(";")}
    session = {"Cookie": cookie.split(";")[0]}
    assert _request(port, "/view-auth", headers=session)[0] == 204
    # A ticket works once, a forged cookie never, and a session expires.
    _assert_expired_page(_login(port, ticket.decode()))
    _assert_expired_page(_request(port, f"/view-login?ticket={ticket.decode()}"))
    name, _, value = session["Cookie"].partition("=")
    forged = {"Cookie": f"{name}={value[:-1]}0" if not value.endswith("0") else f"{name}={value[:-1]}1"}
    assert _request(port, "/view-auth", headers=forged)[0] == 401
    now[0] += runtime.VIEW_SESSION_TTL_S + 1
    assert _request(port, "/view-auth", headers=session)[0] == 401
    expired = views.mint_ticket()
    now[0] += runtime.VIEW_TICKET_TTL_S + 1
    _assert_expired_page(_request(port, f"/view-login?ticket={expired}"))
    _assert_expired_page(_login(port, expired))
    _assert_expired_page(_request(port, "/view-login"))
    _assert_expired_page(_login(port, "forged"))


def _login(port, ticket):
    """POST the view-login form, as the page does."""
    body = urllib.parse.urlencode({"ticket": ticket})
    return _request(
        port,
        "/view-login",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )


def _assert_expired_page(response):
    """A bad, used or expired ticket gets a readable page (not an empty 401 a phone saves as a file)."""
    status, headers, page = response
    assert status == 401 and headers["Content-Type"].startswith("text/html")
    assert "expired" in page.decode() and "new" in page.decode()
    assert "Set-Cookie" not in headers


def test_a_malformed_view_login_post_is_a_bad_request(control_server):
    port = control_server(_control(views=runtime.ViewAccess("p")))
    status, headers, page = _request(
        port, "/view-login", method="POST", headers={"Content-Length": "nope"}, body=None
    )
    assert status == 400 and headers["Content-Type"].startswith("text/html")
    assert "could not be read" in page.decode() and "expired" not in page.decode()


def test_view_tickets_last_about_ten_minutes():
    """A link relayed through chat takes over a minute to reach the human: two minutes was too short."""
    assert runtime.VIEW_TICKET_TTL_S == 600
    now = [0.0]
    views = runtime.ViewAccess("p", clock=lambda: now[0])
    ticket = views.mint_ticket()
    now[0] += 590
    assert views.is_valid_ticket(ticket)
    assert views.redeem(ticket) is not None


NGINX = os.environ.get("NGINX_BIN") or __import__("shutil").which("nginx")


@pytest.mark.skipif(not NGINX, reason="needs an nginx binary (set NGINX_BIN)")
def test_unauthenticated_websocket_upgrade_to_view_is_refused(tmp_path, monkeypatch, control_server):
    """Real nginx with the shipped config: the gate, personal.py's handler, and a fake noVNC behind it."""
    import http.server
    import time as _time

    backend_hits = []

    class Backend(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            backend_hits.append((self.path, self.headers.get("Cookie")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"novnc")

        def log_message(self, *args):
            pass

    try:
        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 6080), Backend)
        views = runtime.ViewAccess("vncpass1")
        # nginx's config hardcodes the control port.
        control = _control(views=views)
        control_http = runtime.ThreadingHTTPServer(("127.0.0.1", 8932), runtime.handler_for(control))
    except OSError:
        pytest.skip("ports 6080/8932 are busy")
    for server in (backend, control_http):
        runtime.threading.Thread(target=server.serve_forever, daemon=True).start()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    rendered = _render_nginx(tmp_path, monkeypatch, port=str(port)).replace(
        "pid /tmp/nginx.pid;", f"pid {tmp_path}/nginx.pid;"
    )
    for name in ("body", "proxy", "fastcgi", "uwsgi", "scgi"):
        rendered = rendered.replace(f"/tmp/nginx-{name}", f"{tmp_path}/nginx-{name}")
    (tmp_path / "nginx-body").mkdir()
    config = tmp_path / "nginx.conf"
    config.write_bytes(rendered.encode())  # Path.write_text is stubbed by the renderer helper
    nginx = subprocess.Popen([NGINX, "-c", str(config), "-p", str(tmp_path)], stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                _time.sleep(0.1)
        upgrade = {
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",  # gitleaks:allow
            "Sec-WebSocket-Version": "13",
        }
        for path in ("/view/websockify", "/view/vnc.html", "/view/"):
            assert _request(port, path, headers=upgrade)[0] == 401
            assert _request(port, path)[0] == 401
        assert (
            _request(port, "/view/websockify", headers={**upgrade, "Cookie": "browser_view=1.00"})[0] == 401
        )
        assert backend_hits == []
        # The ticket flow through nginx, then the same upgrade with the cookie reaches the backend.
        status, _, ticket = _request(port, "/view-ticket", headers={"Authorization": "Bearer test-token"})
        assert status == 200
        assert _request(port, f"/view-login?ticket={ticket.decode()}")[0] == 200
        status, headers, _ = _login(port, ticket.decode())
        assert status == 303
        cookie = headers["Set-Cookie"].split(";")[0]
        assert _request(port, "/view/websockify", headers={**upgrade, "Cookie": cookie})[0] == 200
        # The platform cookie must not reach websockify.
        assert backend_hits == [("/websockify", None)]
    finally:
        nginx.terminate()
        nginx.wait(timeout=10)
        backend.shutdown()
        control_http.shutdown()


def test_bearer_token_never_written_to_workspace(tmp_path, monkeypatch):
    from dataclasses import replace

    profile = _profile.current()
    assert profile is not None
    profile = replace(
        profile, browser=_profile.Browser(identities=(_profile.BrowserIdentity("personal", "memory://p"),))
    )
    monkeypatch.setenv("AGENT_FACTORY_BROWSER_IDENTITIES", "personal")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "token-bytes-1234567890")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "_PROFILE", profile)
    gate = _session_setup._start_browser_gate(cast(Any, SimpleNamespace(row=lambda *a: None)))
    try:
        assert gate is not None
        path = _session_setup._write_mcp_config("http://shared-browser")
        server = json.loads(path.read_text())["mcpServers"]["browser-personal"]
        assert server == {"type": "http", "url": f"{gate.url}/personal/mcp"}
    finally:
        _session_setup._stop_browser_gate()
    for file in tmp_path.rglob("*"):
        if file.is_file():
            assert b"token-bytes-1234567890" not in file.read_bytes()


async def test_sub_agent_uploads_exclude_mcp_config(tmp_path, monkeypatch):

    (tmp_path / ".mcp.json").write_text("{}")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/.mcp.json").write_text("{}")
    (tmp_path / "code.py").write_text("x = 1")
    uploaded = []

    async def from_local(path):
        import tarfile

        with tarfile.open(Path(path) / _checkpoint._WORKSPACE_ARCHIVE) as archive:
            uploaded.append(sorted(name.removeprefix("./") for name in archive.getnames() if name != "."))
        return object()

    monkeypatch.setattr(Dir, "from_local", from_local)
    await _checkpoint._upload_workspace(tmp_path)
    assert uploaded == [["code.py", "src", "src/.mcp.json"]]
    # Both sub-agent snapshots go through it: the QA review and the planner.
    uploaded.clear()

    async def broken(**_):
        raise RuntimeError("pod never scheduled")

    monkeypatch.setattr(_reviewer, "_call_reviewer", broken)
    with pytest.raises(_reviewer.ReviewUnavailable):
        await _review._evaluate(tmp_path, "brief", "", 1, _timeline._Timeline())
    await _contract._plan_next_round(tmp_path, "brief", "", "findings", 1, _timeline._Timeline())
    assert uploaded and all(".mcp.json" not in listing for listing in uploaded)


PERSONAL_TOML = (Path(__file__).parent / "profiles/work.toml").read_text()


@pytest.mark.parametrize("section", [False, True])
def test_personal_env_defined_only_when_profile_enables_it(section):
    from flyte_agent_factory import _profile

    deploy_env = dict(os.environ)
    deploy_env.pop(_profile.PROFILE_ENV, None)
    deploy_env["AGENT_FACTORY_ENV_NAME"] = "x"
    deploy_env[_profile.TOML_ENV] = PERSONAL_TOML + (
        '\n[browser]\npersonal = true\nprofile_uri = "memory://profile"\npersonal_env = "agent-one"\n'
        if section
        else ""
    )
    code = (
        "from flyte_agent_factory import agent, browser; import json; from flyte._environment import "
        "list_loaded_environments as loaded; print(json.dumps([e.name for e in loaded()]))"
    )
    names = json.loads(subprocess.check_output([sys.executable, "-c", code], env=deploy_env).splitlines()[-1])
    assert ("claude-browser-personal" in names) == section
    assert "claude-browser" in names


def test_transient_storage_errors_are_retried_not_treated_as_corruption(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"private cookies")
    uri = (tmp_path / "snapshot.tar.gz").as_uri()
    runtime.snapshot(profile, uri)
    fs, _ = runtime.fsspec.core.url_to_fs(uri)
    real_open = fs.open
    failures = [ConnectionResetError(), TimeoutError(), OSError("503 Service Unavailable")]

    def flaky(*args, **kwargs):
        if failures:
            raise failures.pop(0)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(type(fs), "open", lambda self, *a, **k: flaky(*a, **k))
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)
    restored = tmp_path / "restored"
    runtime.restore(restored, uri)
    assert not failures
    assert (restored / "Cookies").read_bytes() == b"private cookies"


def test_unreachable_storage_is_not_an_empty_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "Cookies").write_bytes(b"local cookies")
    uri = (tmp_path / "snapshot.tar.gz").as_uri()
    fs, _ = runtime.fsspec.core.url_to_fs(uri)

    def unreachable(*args, **kwargs):
        raise ConnectionResetError

    monkeypatch.setattr(type(fs), "exists", unreachable)
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)
    with pytest.raises(runtime.StorageUnavailable):
        runtime.restore(profile, uri, retry_s=120)
    assert (profile / "Cookies").read_bytes() == b"local cookies"


def test_unreachable_storage_neither_starts_chromium_nor_snapshots(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(profile))
    monkeypatch.setenv("BROWSER_PROFILE_URI", "memory://test/profile")
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "test-token")
    snapshots, released, launched = [], [], []

    class Held:
        expires = float("inf")

        def renew(self):
            return True

        def release(self):
            released.append(True)

    def unreachable(*args):
        raise runtime.StorageUnavailable("ConnectionResetError")

    monkeypatch.setattr(runtime, "Lease", lambda uri: Held())
    monkeypatch.setattr(runtime, "restore", unreachable)
    monkeypatch.setattr(runtime, "snapshot", lambda *args: snapshots.append(args))
    monkeypatch.setattr(runtime, "ThreadingHTTPServer", lambda *a, **k: launched.append(True))
    monkeypatch.setattr(runtime.signal, "signal", lambda *args: None)
    ready = Path("/tmp/personal-ready")
    ready.unlink(missing_ok=True)
    with pytest.raises(runtime.StorageUnavailable):
        runtime.main()
    assert not snapshots and not launched and not ready.exists()
    assert released


def test_retention_keeps_unreadable_generations_until_a_restored_profile_replaces_them(tmp_path):
    from obstore.store import MemoryStore

    store = MemoryStore()
    uri = f"memory://retention-{tmp_path.name}/profile.tar.gz"
    fs, _ = runtime.fsspec.core.url_to_fs(uri)
    lease = runtime.Lease(uri, store)
    assert lease.renew()
    profile = tmp_path / "profile"
    profile.mkdir()
    for content in (b"one", b"two"):
        (profile / "Cookies").write_bytes(content)
        runtime.snapshot(profile, uri, lease)
    older, newest = lease.archives[1], lease.archives[0]
    lease.release()
    for name in (older, newest):
        with fs.open(name, "wb") as target:
            target.write(b"truncated")
    # Every generation is a successfully read, corrupt archive: start empty, but keep them.
    second = runtime.Lease(uri, store)
    assert second.renew()
    restored = tmp_path / "restored"
    runtime.restore(restored, uri, second)
    assert not list(restored.iterdir())
    assert not second.trusted
    for _ in range(3):
        (restored / "Cookies").write_bytes(b"signed in again")
        runtime.snapshot(restored, uri, second)
    assert second.archives[1:] == [newest, older]
    assert fs.exists(older) and fs.exists(newest)
    second.release()
    # A later start restores the new profile, which makes it the lineage: retention is normal again.
    third = runtime.Lease(uri, store)
    assert third.renew()
    runtime.restore(tmp_path / "again", uri, third)
    assert third.trusted
    runtime.snapshot(tmp_path / "again", uri, third)
    runtime.snapshot(tmp_path / "again", uri, third)
    assert newest not in third.archives and not fs.exists(newest)


def test_lease_release_survives_storage_errors():
    from obstore.store import MemoryStore

    class Unreachable(MemoryStore):
        pass

    lease = runtime.Lease("memory://test/profile", MemoryStore())
    assert lease.renew()

    def reset(*args, **kwargs):
        raise ConnectionResetError

    original = runtime.obstore.put
    runtime.obstore.put = reset
    try:
        lease.release()
    finally:
        runtime.obstore.put = original


class FakeMcp:
    """A stand-in `playwright-mcp`: listens on a port, and says which process is serving."""

    SCRIPT = (
        "import http.server, os, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(str(os.getpid()).encode())\n"
        "    def log_message(self, *a): pass\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), H).serve_forever()\n"
    )

    def __init__(self, tmp_path, monkeypatch):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.pid_file = tmp_path / "mcp.pid"
        self.restart_file = tmp_path / "mcp.restart"
        (tmp_path / "fake.py").write_text(self.SCRIPT)
        monkeypatch.setattr(runtime, "MCP_PID_FILE", self.pid_file)
        monkeypatch.setattr(runtime, "MCP_RESTART_FILE", self.restart_file)
        monkeypatch.setattr(runtime, "MCP_PORT", self.port)
        self.supervisor = subprocess.Popen(
            [
                "bash",
                str(ASSETS / "mcp_supervisor.sh"),
                str(self.pid_file),
                str(self.restart_file),
                sys.executable,
                str(tmp_path / "fake.py"),
                str(self.port),
            ]
        )
        self.wait_listening()

    def wait_listening(self):
        import time as _time

        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                _time.sleep(0.1)
        raise AssertionError("fake playwright-mcp never listened")

    def serving_pid(self):
        return _request(self.port, "/")[2].decode()

    def close(self):
        self.supervisor.terminate()
        try:
            os.killpg(int(self.pid_file.read_text()), signal_kill())
        except (OSError, ValueError):
            pass


def signal_kill():
    import signal

    return signal.SIGKILL


@pytest.fixture
def fake_mcp(tmp_path, monkeypatch):
    mcp = FakeMcp(tmp_path, monkeypatch)
    yield mcp
    mcp.close()


def test_abandoned_client_triggers_playwright_restart_on_next_initialize(fake_mcp, tmp_path, control_server):
    now = [0.0]
    sessions = runtime.McpSessions(idle_s=120, clock=lambda: now[0])
    saved = []
    profile = tmp_path / "profile"
    profile.mkdir()
    control = _control(
        sessions=sessions, restart=lambda: runtime.restart_browser(profile, lambda: saved.append(True))
    )
    port = control_server(control)
    bearer = {"Authorization": "Bearer test-token"}

    def gate(method, session=None):
        headers = {**bearer, "X-Original-Method": method}
        if session:
            headers["Mcp-Session-Id"] = session
        return _request(port, "/exit-check", headers=headers)[0]

    first_server = fake_mcp.serving_pid()
    assert gate("POST") == 200  # the first initialize: nothing to recover from
    assert gate("POST", "old-client") == 200
    # The old client is quiet but recent: a second initialize must not kill its browser.
    now[0] = 60
    assert gate("POST") == 200
    assert fake_mcp.serving_pid() == first_server
    # Silent beyond the limit: the next initialize restarts playwright-mcp.
    now[0] = 500
    assert gate("POST") == 200
    assert fake_mcp.serving_pid() != first_server
    assert saved == [True]
    assert not fake_mcp.restart_file.exists()
    assert sessions.last_seen == {}
    # DELETE ends a session cleanly, so nothing is left to be judged abandoned.
    assert gate("POST", "new-client") == 200
    assert gate("DELETE", "new-client") == 200
    now[0] = 5000
    pid = fake_mcp.serving_pid()
    assert gate("POST") == 200
    assert fake_mcp.serving_pid() == pid


def test_admin_reset_requires_bearer_and_restarts(fake_mcp, tmp_path, control_server):
    profile = tmp_path / "profile"
    profile.mkdir()
    port = control_server(_control(restart=lambda: runtime.restart_browser(profile, lambda: None)))
    before = fake_mcp.serving_pid()
    assert _request(port, "/admin/reset", "POST")[0] == 401
    _request(port, "/admin/reset", "GET", {"Authorization": "Bearer test-token"})  # a GET must not reset
    assert fake_mcp.serving_pid() == before
    assert _request(port, "/admin/reset", "POST", {"Authorization": "Bearer test-token"})[0] == 200
    assert fake_mcp.serving_pid() != before


def test_playwright_exit_without_a_reset_still_takes_the_app_down(tmp_path, monkeypatch):
    mcp = FakeMcp(tmp_path, monkeypatch)
    try:
        os.killpg(int(mcp.pid_file.read_text()), signal_kill())
        assert mcp.supervisor.wait(timeout=10) == 1
    finally:
        mcp.close()


def test_gate_proxy_is_restarted_and_health_reports_it(monkeypatch, control_server):
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    class Gate:
        def ready(self):
            return True

    proxy = runtime.GateProxy(Gate(), port=free_port)
    original = runtime.GateProxy.serve
    crashes: list[bool] = []

    async def crash_once(self):
        if not crashes:
            crashes.append(True)
            raise RuntimeError("proxy thread died")
        await original(self)

    monkeypatch.setattr(runtime.GateProxy, "serve", crash_once)
    port = control_server(_control(exit_node="home", proxy=proxy))
    bearer = {"Authorization": "Bearer test-token"}
    # Dead (or not yet up): health fails closed and says why.
    status, _, body = _request(port, "/exit-check", headers=bearer)
    assert status == 503 and json.loads(body)["gate"] == "down"
    runtime.threading.Thread(target=proxy.run, daemon=True).start()
    try:
        # (time.sleep is stubbed above, so wait on the event, not the clock.)
        assert proxy.listening.wait(timeout=10)
        assert crashes
        status, _, body = _request(port, "/exit-check", headers=bearer)
        assert status == 200 and json.loads(body)["gate"] == "up"
    finally:
        proxy.stop.set()


def test_secrets_are_read_from_staged_files_and_removed(tmp_path, monkeypatch):
    staged = tmp_path / "secrets"
    staged.mkdir()
    (staged / "BROWSER_PERSONAL_TOKEN").write_text("the-token")
    (staged / "TAILSCALE_OAUTH_CLIENT_ID").write_text("the-id")
    monkeypatch.setenv(runtime.SECRETS_DIR_ENV, str(staged))
    names = ("BROWSER_PERSONAL_TOKEN", *runtime.OAUTH_ENV)
    assert runtime.take_secrets(names) == ["the-token", "the-id", ""]
    assert not staged.exists()
    assert runtime.SECRETS_DIR_ENV not in os.environ
    # Without staged files (tests, local runs) the environment is the fallback.
    monkeypatch.setenv("BROWSER_PERSONAL_TOKEN", "from-env")
    assert runtime.take_secrets(names)[0] == "from-env"
    assert "BROWSER_PERSONAL_TOKEN" not in os.environ


def test_start_script_reexecs_without_secrets_in_its_environment(tmp_path):
    """The process image that keeps running must not carry the secrets in /proc/<pid>/environ."""
    shell = (ASSETS / "start.sh").read_text()
    head = shell.split('PORT="${BROWSER_APP_PORT:-8080}"', 1)[0]
    script = tmp_path / "start.sh"
    script.write_text(
        head + 'tr "\\0" "\\n" < /proc/self/environ\n'
        'echo "FILE:$(cat "$BROWSER_SECRETS_DIR/BROWSER_PERSONAL_TOKEN")"\n'
        'echo "MODE:$(stat -c %a "$BROWSER_SECRETS_DIR" "$BROWSER_SECRETS_DIR/BROWSER_PERSONAL_TOKEN")"\n'
    )
    env = {
        **os.environ,
        "BROWSER_PERSONAL": "1",
        "BROWSER_PERSONAL_TOKEN": "secret-token-value",
        "TAILSCALE_OAUTH_CLIENT_SECRET": "secret-oauth-value",
    }
    env.pop("BROWSER_SECRETS_DIR", None)
    output = subprocess.check_output(["bash", str(script)], env=env, text=True)
    environ, _, files = output.partition("FILE:")
    assert "secret-token-value" not in environ and "secret-oauth-value" not in environ
    assert files.startswith("secret-token-value")
    assert "700\n600" in files


def test_view_link_asks_for_a_ticket_with_the_bearer_token(monkeypatch):
    from flyte_agent_factory import browser

    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def urlopen(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization")))
        return Response(b"one-time-ticket")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    link = browser.view_link_from("http://private-browser/", "https://apps.example/x/", "test-token")
    assert link == "https://apps.example/x/view-login?ticket=one-time-ticket"
    assert seen == [("http://private-browser/view-ticket", "Bearer test-token")]


def test_browser_module_imports_without_kubernetes():
    # The deploy and launch environments import agent.py without kubernetes installed.
    code = "import sys; sys.modules['kubernetes'] = None; import flyte_agent_factory.browser"
    env = {**os.environ, "PYTHONPATH": "src"}
    env.pop("AGENT_FACTORY_PROFILE", None)
    result = subprocess.run(
        [sys.executable, "-c", code], check=False, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
