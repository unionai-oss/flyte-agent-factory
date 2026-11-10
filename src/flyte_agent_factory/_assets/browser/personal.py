"""Personal browser lifecycle. Never log credentials or profile contents."""

from __future__ import annotations

import asyncio
import fcntl
import gzip
import hmac
import html
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zlib
from collections.abc import Callable, Iterable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, ContextManager, Protocol, TypedDict, TypeVar, cast

import fsspec  # type: ignore[import-untyped]  # fsspec does not ship type information.
import obstore
from obstore.exceptions import AlreadyExistsError, NotFoundError, PreconditionError
from obstore.exceptions import BaseError as ObstoreError
from obstore.store import from_url

if TYPE_CHECKING:
    from obstore import PutMode, UpdateVersion
    from obstore.store import ObjectStore
    from typing_extensions import Unpack


_T = TypeVar("_T")

# What a profile snapshot can raise. fsspec backends share no base type (see `storage`), so this is
# the union of what obstore, the filesystem, tar and the lease's JSON record raise.
SNAPSHOT_ERRORS = (ObstoreError, OSError, RuntimeError, ValueError, tarfile.TarError)
# What a `tailscale` CLI call or a status document can raise: a failed or missing binary, a timeout,
# unparseable JSON, or a document without the expected keys and shapes.
TAILSCALE_ERRORS = (subprocess.SubprocessError, OSError, ValueError, KeyError, AttributeError, TypeError)


class _FileSystem(Protocol):
    # fsspec has no stubs; this is the binary file surface used by download.
    def open(self, path: str, mode: str) -> ContextManager[BinaryIO]: ...


class _LeaseRecord(TypedDict, total=False):
    owner: str
    expires: float
    archives: list[str]


class _Peer(TypedDict, total=False):
    TailscaleIPs: list[str]
    HostName: str
    DNSName: str
    ExitNode: bool
    Online: bool


class _Status(TypedDict, total=False):
    BackendState: str
    ExitNodeStatus: _Peer | None
    Peer: dict[str, _Peer] | None


class _TailscaleOptions(TypedDict, total=False):
    stderr: int


class _Request(Protocol):
    # Callable cannot describe the keyword-only timeout used by the injected request function.
    def __call__(self, request: urllib.request.Request, /, *, timeout: int) -> ContextManager[BinaryIO]: ...


EXCLUDED = {
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "GrShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "CacheStorage",
}
OAUTH_ENV = ("TAILSCALE_OAUTH_CLIENT_ID", "TAILSCALE_OAUTH_CLIENT_SECRET")
TS_SOCKET = str(Path.home() / ".browser-tailscale" / "tailscaled.sock")
SECRETS_DIR_ENV = "BROWSER_SECRETS_DIR"
MCP_PORT = 8931
MCP_PID_FILE = Path("/tmp/personal-mcp.pid")
# Tells mcp_supervisor.sh that the next playwright-mcp exit is a restart, not a crash.
MCP_RESTART_FILE = Path("/tmp/personal-mcp.restart")
VNC_DIR = Path("/tmp/personal-vnc")
RESTORE_RETRY_S = 120
# Ten minutes: a link relayed through chat takes over a minute to reach the human's phone.
VIEW_TICKET_TTL_S = 600
VIEW_SESSION_TTL_S = 3600


class StorageUnavailable(RuntimeError):
    """Storage could not be read. That says nothing about whether the archive is good."""


def take_secrets(names: Iterable[str]) -> list[str]:
    """Read secrets that start.sh staged in files, so they never sit in this process's environ.

    The environment is the fallback for local runs and tests. A secret passed in the
    environment stays readable in /proc/<pid>/environ for the process's whole life;
    clearing os.environ does not change that.
    """
    directory = os.environ.pop(SECRETS_DIR_ENV, "")
    if not directory:
        return [os.environ.pop(name, "") for name in names]
    values = []
    for name in names:
        path = Path(directory) / name
        values.append(path.read_text() if path.exists() else "")
        path.unlink(missing_ok=True)
    Path(directory).rmdir()
    return values


def child_env() -> dict[str, str]:
    """Return the environment for child processes, without the Tailscale OAuth credentials or bearer token."""
    return {k: v for k, v in os.environ.items() if k not in (*OAUTH_ENV, "BROWSER_PERSONAL_TOKEN")}


def clear_stale_lock(profile: Path) -> None:
    """Remove the stale Chromium singleton files of a profile whose owner has died.

    Raises RuntimeError when the lock is unrecognised, held, or belongs to another host or user.
    """
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        if lock.exists():
            raise RuntimeError("Unrecognized Chromium lock; refusing to open profile")
        return
    try:
        host, pid_text = os.readlink(lock).rsplit("-", 1)
        pid = int(pid_text)
    except (ValueError, PermissionError) as error:
        raise RuntimeError("Unrecognized Chromium lock; refusing to clear lock") from error
    if host != socket.gethostname():
        raise RuntimeError("Chromium profile belongs to another host; refusing to clear lock")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            (profile / name).unlink(missing_ok=True)
    except PermissionError as error:
        raise RuntimeError("Chromium lock is not ours; refusing to clear lock") from error
    else:
        raise RuntimeError("Chromium still owns the profile")


def storage(call: Callable[[], _T], deadline: float) -> _T:
    """Run one storage operation, retrying transient failures with backoff until `deadline`.

    Whatever fails here is a statement about the network or the bucket, never about the
    archive: connection resets, timeouts and 5xx all arrive as exceptions from fsspec.
    Treating them as a corrupt archive started Chromium on an empty profile and let
    retention delete the good generations.
    """
    delay = 1
    while True:
        try:
            return call()
        except Exception as error:  # every storage client raises its own types
            print(f"[personal] storage error: {type(error).__name__}", flush=True)
            if time.monotonic() + delay < deadline:
                time.sleep(delay)
                delay = min(delay * 2, 15)
                continue
            raise StorageUnavailable(type(error).__name__) from error


def download(fs: _FileSystem, path: str, target: BinaryIO) -> None:
    """Copy the object at `path` on `fs` into the open file `target`, replacing its contents."""
    target.seek(0)
    target.truncate()
    with fs.open(path, "rb") as source:
        shutil.copyfileobj(source, target)


def restore(profile: Path, uri: str, lease: Lease | None = None, retry_s: float = RESTORE_RETRY_S) -> None:
    """Restore the newest readable archive; only an archive we read and could not unpack is corrupt.

    Raises StorageUnavailable when storage stays unreachable for `retry_s`: the caller must not
    start Chromium on an empty profile in that case, or snapshot over what is stored.
    """
    fs, key = fsspec.core.url_to_fs(uri)
    deadline = time.monotonic() + retry_s
    candidates = lease.archives if lease and lease.archives else (key, key + ".prev")
    corrupt = False
    # Download first, then unpack locally: the network errors stay on one side of the line.
    with tempfile.TemporaryFile(dir=profile.parent) as archive:
        for candidate in candidates:
            if not storage(partial(fs.exists, candidate), deadline):
                continue
            storage(partial(download, fs, candidate, archive), deadline)
            archive.seek(0)
            try:
                # Extract off to the side: a truncated archive must not leave a half profile.
                with tempfile.TemporaryDirectory(dir=profile.parent) as staging:
                    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
                        tar.extractall(staging, filter="data")
                    shutil.rmtree(profile, ignore_errors=True)
                    shutil.move(staging, profile)
            except (EOFError, tarfile.TarError, zlib.error, gzip.BadGzipFile) as error:
                print(f"[personal] restore failed: {type(error).__name__}", flush=True)
                corrupt = True
                continue
            if lease:
                lease.restored = candidate
            if not lease and candidate != key:
                try:
                    fs.copy(candidate, key)
                except OSError as error:
                    print(f"[personal] recovery promotion failed: {type(error).__name__}", flush=True)
            return
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    if lease and corrupt:
        # Archives exist but none unpacked. What we publish next is not a successor of a
        # known-good profile, so retention must keep what was there (see Lease.publish).
        lease.trusted = False
        lease.inherited = list(lease.archives)


def snapshot(profile: Path, uri: str, lease: Lease | None = None) -> None:
    """Archive the profile (minus caches and singleton files) and publish it at `uri`.

    With a `lease` the archive is published through the lease's conditional write; without one the
    previous archive is kept as `.prev`.
    """
    fs, key = fsspec.core.url_to_fs(uri)
    pending: str | None = key + ".pending-" + uuid.uuid4().hex
    try:
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for directory, dirs, files in os.walk(profile):
                    dirs[:] = [d for d in dirs if d not in EXCLUDED and not d.startswith("Singleton")]
                    for name in files:
                        if name in EXCLUDED or name.startswith("Singleton"):
                            continue
                        path = Path(directory) / name
                        try:
                            tar.add(path, arcname=str(path.relative_to(profile)), recursive=False)
                        except FileNotFoundError:
                            # Chromium evicts cache and temporary files while it is running.
                            continue
            archive.seek(0)
            with fs.open(pending, "wb") as target:
                shutil.copyfileobj(archive, target)
        if lease is not None:
            # Publish the immutable archive through the lease's conditional write.
            # A delayed old writer cannot overwrite its successor's snapshot pointer.
            # pending is still the generated key; None is assigned only after publishing.
            lease.publish(cast(str, pending), key if fs.exists(key) else None)
            pending = None
            return
        if fs.exists(key):
            fs.copy(key, key + ".prev")
        fs.copy(pending, key)
    finally:
        if pending and fs.exists(pending):
            fs.rm(pending)


class Lease:
    """Conditional object writes arbitrate owners; fsspec's exists/write is not atomic."""

    def __init__(self, uri: str, store: ObjectStore | None = None) -> None:
        """Use the lease object at `uri`; `store` is an obstore store, built from `uri` when not given."""
        self.uri = uri
        parsed = urllib.parse.urlsplit(uri)
        self.store = store if store is not None else from_url(f"{parsed.scheme}://{parsed.netloc}")
        self.key = parsed.path.lstrip("/") + ".lease"
        self.owner = uuid.uuid4().hex
        self.version: UpdateVersion | None = None
        self.expires: float = 0
        self.lock = threading.Lock()
        self.archives: list[str] = []
        self.restored: str | None = None
        # False after a restore that found archives but could not unpack any of them.
        self.trusted = True
        self.inherited: list[str] = []

    def renew(self, now: float | None = None) -> bool:
        """Take or extend the lease for two minutes; return False when another owner holds it."""
        with self.lock:
            return self._renew(now)

    def _renew(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        try:
            current = obstore.get(self.store, self.key)
            record: _LeaseRecord = json.loads(bytes(current.bytes()))
            if self.version and (record["owner"] != self.owner or now >= self.expires):
                return False
            if record["owner"] != self.owner and record["expires"] > now:
                return False
            # Only obstore version keys enter this conditional-write mode.
            mode: PutMode = cast(
                "UpdateVersion",
                {k: current.meta[k] for k in ("e_tag", "version") if current.meta[k] is not None},
            )
        except (NotFoundError, FileNotFoundError):
            if self.version:
                return False
            mode = "create"
            record = {}
        self.archives = record.get("archives", [])
        payload = json.dumps({"owner": self.owner, "expires": now + 120, "archives": self.archives}).encode()
        try:
            # Filtering nulls preserves the UpdateVersion keys returned by obstore.
            self.version = cast(
                "UpdateVersion",
                {
                    k: v
                    for k, v in obstore.put(self.store, self.key, payload, mode=mode).items()
                    if v is not None
                },
            )
        except (AlreadyExistsError, PreconditionError):
            return False
        self.expires = now + 120
        return True

    def publish(self, key: str, legacy: str | None) -> None:
        """Record the archive at `key` as the newest through a conditional write.

        `legacy` is the pre-lease archive, kept as the fallback. Raises RuntimeError if the lease was lost.
        """
        with self.lock:
            if not self._renew():
                raise RuntimeError("Personal profile lease lost before snapshot publish")
            previous = self.restored or (self.archives[0] if self.archives else legacy)
            if self.trusted:
                archives = [key] + ([previous] if previous else [])
            else:
                # Never drop the generations we could not read: this profile did not come from
                # them, and a human may still need to salvage one. Only our own earlier
                # snapshot is replaced, so the list cannot grow.
                archives = [key] + self.inherited
            record = json.dumps({"owner": self.owner, "expires": self.expires, "archives": archives}).encode()
            try:
                version = obstore.put(self.store, self.key, record, mode=self.version)
            except PreconditionError as error:
                raise RuntimeError("Personal profile lease lost before snapshot publish") from error
            old = self.archives
            # Filtering nulls preserves the UpdateVersion keys returned by obstore.
            self.version = cast("UpdateVersion", {k: v for k, v in version.items() if v is not None})
            self.archives = archives
            self.restored = key
        # Only unreachable generations can be removed; canonical archives remain for migration.
        for obsolete in old:
            if obsolete not in archives and ".pending-" in obsolete:
                try:
                    fs, _ = fsspec.core.url_to_fs(self.uri)
                    fs.rm(obsolete)
                except OSError as error:
                    print(f"[personal] archive cleanup failed: {type(error).__name__}", flush=True)

    def release(self) -> None:
        """Expire our lease so the next owner need not wait for it."""
        with self.lock:
            self._release()

    def _release(self) -> None:
        if self.version:
            # Expire our version rather than deleting a successor's lease. Any storage failure
            # is survivable here: the lease expires on its own, and shutdown must proceed.
            try:
                obstore.put(
                    self.store,
                    self.key,
                    json.dumps({"owner": self.owner, "expires": 0, "archives": self.archives}).encode(),
                    mode=self.version,
                )
            except (ObstoreError, OSError, ValueError) as error:  # a lease we cannot release simply expires
                print(f"[personal] lease release failed: {type(error).__name__}", flush=True)


def tailscale(*args: str, **kwargs: Unpack[_TailscaleOptions]) -> bytes:
    """Run the `tailscale` CLI against our socket and return its output."""
    return subprocess.check_output(
        ["tailscale", f"--socket={TS_SOCKET}", *args], env=child_env(), timeout=10, **kwargs
    )


class ExitGate:
    """Decides whether traffic may leave: only while the configured exit node is up and in use."""

    def __init__(self, exit_node: str, status: Callable[[], _Status] | None = None) -> None:
        """Check `exit_node` (an IP, host or DNS name); `status` returns `tailscale status --json`."""
        self.exit_node = exit_node
        self.status = status or (lambda: json.loads(tailscale("status", "--json")))
        self.checked: float = 0
        self.allowed = False
        self.lock = threading.Lock()

    def ready(self) -> bool:
        """Return whether the exit node is online and selected; fail closed on any error.

        The answer is cached for two seconds.
        """
        with self.lock:
            if time.monotonic() - self.checked < 2:
                return self.allowed
            self.allowed = False
            try:
                status = self.status()
                active = status.get("ExitNodeStatus") or {}
                peers = status.get("Peer") or {}
                selected = any(
                    peer.get("ExitNode") and peer.get("Online") and peer_matches(peer, self.exit_node)
                    for peer in peers.values()
                )
                self.allowed = status.get("BackendState") == "Running" and (
                    active.get("Online") is True or selected
                )
            except TAILSCALE_ERRORS as error:  # fails closed: any status failure denies egress; class only
                print(f"[personal] exit gate status failed: {type(error).__name__}", flush=True)
                self.allowed = False
            self.checked = time.monotonic()
            return self.allowed


def peer_matches(peer: _Peer, name: str) -> bool:
    """Return whether the tailscale `peer` is `name`, by IP, host name or DNS name."""
    return (
        name in peer.get("TailscaleIPs", [])
        or name.rstrip(".") in (peer.get("HostName"), peer.get("DNSName", "").rstrip("."))
        or name == peer.get("DNSName", "").split(".")[0]
    )


async def forward_proxy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, gate: ExitGate, upstream_port: int = 1055
) -> None:
    """Forward one Chromium connection to the Tailscale proxy while the exit gate stays open.

    The gate is rechecked on every chunk and while idle, so open tabs stop when the exit node drops.
    """
    upstream = None
    try:
        if not await asyncio.to_thread(gate.ready):
            return
        remote_reader, upstream = await asyncio.open_connection("127.0.0.1", upstream_port)

        async def copy(source: asyncio.StreamReader, target: asyncio.StreamWriter) -> None:
            while data := await source.read(65536):
                if not await asyncio.to_thread(gate.ready):
                    return
                target.write(data)
                await target.drain()

        tasks = [
            asyncio.create_task(copy(reader, upstream)),
            asyncio.create_task(copy(remote_reader, writer)),
        ]
        try:
            # Recheck idle tunnels too: open tabs must stop working when the exit node drops.
            while await asyncio.to_thread(gate.ready):
                done, _ = await asyncio.wait(tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
                if done:
                    break
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        if upstream:
            upstream.close()
            await upstream.wait_closed()
        writer.close()
        await writer.wait_closed()


class GateProxy:
    """The local forwarder Chromium connects to; supervised so a dead one is noticed and replaced."""

    def __init__(self, gate: ExitGate, port: int = 1056, upstream_port: int = 1055) -> None:
        """Listen on `port` on localhost and forward to the Tailscale proxy on `upstream_port`."""
        self.gate = gate
        self.port = port
        self.upstream_port = upstream_port
        self.listening = threading.Event()
        self.stop = threading.Event()

    async def serve(self) -> None:
        """Accept connections and forward them until cancelled."""
        server = await asyncio.start_server(
            lambda r, w: forward_proxy(r, w, self.gate, self.upstream_port), "127.0.0.1", self.port
        )
        self.listening.set()
        async with server:
            await server.serve_forever()

    def run(self) -> None:
        """Serve until `stop` is set, restarting with backoff when the server dies."""
        backoff = 1
        while not self.stop.is_set():
            try:
                asyncio.run(self.serve())
            except (OSError, RuntimeError) as error:  # restart rather than leave Chromium without a proxy
                print(f"[personal] gate proxy restart: {type(error).__name__}", flush=True)
            if self.listening.is_set():
                backoff = 1
            self.listening.clear()
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)


def connect_exit_node(exit_node: str, key: str) -> None:
    """Join the tailnet with `key` and select `exit_node`, waiting for it to appear if it is a name."""
    with tempfile.NamedTemporaryFile(mode="w") as auth:
        auth.write(key)
        auth.flush()
        tailscale(
            "up",
            f"--auth-key=file:{auth.name}",
            f"--hostname=browser-{os.urandom(6).hex()}",
            stderr=subprocess.DEVNULL,
        )
    try:
        ipaddress.ip_address(exit_node)
    except ValueError:
        for _ in range(60):
            status = json.loads(tailscale("status", "--json"))
            if any(peer_matches(p, exit_node) for p in status.get("Peer", {}).values()):
                break
            time.sleep(1)
        else:
            raise RuntimeError("Exit node was not discovered")
    tailscale(
        "set", f"--exit-node={exit_node}", "--exit-node-allow-lan-access=false", stderr=subprocess.DEVNULL
    )


def egress(exit_node: str, gate: ExitGate | None = None) -> str:
    """Return the public IP seen through the exit node, or directly when there is none.

    Raises RuntimeError when an exit node is configured but down.
    """
    args = ["curl", "--fail", "--silent", "--max-time", "8"]
    if exit_node:
        if not (gate or ExitGate(exit_node)).ready():
            raise RuntimeError("Configured exit node is down; browser access blocked")
        args += ["--proxy", "socks5h://127.0.0.1:1056"]
    return subprocess.check_output(args + ["https://api.ipify.org"], text=True, env=child_env()).strip()


class Tailscale:
    """Runs the tailscale daemon and keeps it connected to the exit node."""

    def __init__(self, exit_node: str, client_id: str, client_secret: str, gate: ExitGate) -> None:
        """Hold the OAuth client credentials used to mint auth keys for `exit_node`."""
        self.exit_node = exit_node
        self.client_id = client_id
        self.client_secret = client_secret
        self.gate = gate
        self.daemon: subprocess.Popen[bytes] | None = None
        self.stop = threading.Event()

    def supervise(self) -> None:
        """Run the daemon and connect to the exit node, restarting with backoff until closed."""
        backoff = 1
        while not self.stop.is_set():
            try:
                Path(TS_SOCKET).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                Path(TS_SOCKET).parent.chmod(0o700)
                Path(TS_SOCKET).unlink(missing_ok=True)
                key = mint_auth_key(self.client_id, self.client_secret)
                self.daemon = subprocess.Popen(
                    [
                        "tailscaled",
                        "--tun=userspace-networking",
                        "--state=mem:",
                        f"--socket={TS_SOCKET}",
                        "--socks5-server=127.0.0.1:1055",
                        "--no-logs-no-support",
                    ],
                    env=child_env(),
                )
                for _ in range(100):
                    if Path(TS_SOCKET).exists():
                        break
                    if self.stop.wait(0.1) or self.daemon.poll() is not None:
                        raise RuntimeError("Tailscale did not start")
                connect_exit_node(self.exit_node, key)
                backoff = 1
                while not self.stop.wait(0.5):
                    if self.daemon.poll() is not None:
                        break
            except (*TAILSCALE_ERRORS, RuntimeError) as error:  # restart without leaking credentials
                print(f"[personal] tailscale restart: {type(error).__name__}", flush=True)
            finally:
                self.gate.allowed = False
                self.gate.checked = time.monotonic()
                if self.daemon and self.daemon.poll() is None:
                    self.daemon.terminate()
                    try:
                        self.daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.daemon.kill()
                        self.daemon.wait()
            if self.stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30)

    def close(self) -> None:
        """Log out of the tailnet and stop supervising."""
        try:
            subprocess.run(
                ["tailscale", f"--socket={TS_SOCKET}", "logout"],
                timeout=5,
                env=child_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self.stop.set()


_TAILSCALE_TAG = re.compile(r"tag:[a-z0-9-]+")


def mint_auth_key(client_id: str, client_secret: str, request: _Request = urllib.request.urlopen) -> str:
    """Return a single-use auth key for the tailnet, minted with the OAuth client credentials.

    The key is tagged `BROWSER_TAILSCALE_TAG` (default `tag:k8s`), which must be a tag the OAuth client owns.
    """
    tag = os.environ.get("BROWSER_TAILSCALE_TAG", "tag:k8s")
    if not _TAILSCALE_TAG.fullmatch(tag):
        raise ValueError(f"BROWSER_TAILSCALE_TAG must look like 'tag:name', not {tag!r}")
    data = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}
    ).encode()
    with request(
        urllib.request.Request("https://api.tailscale.com/api/v2/oauth/token", data=data), timeout=30
    ) as response:
        token_response: dict[str, str] = json.load(response)
        token = token_response["access_token"]
    payload = {
        "capabilities": {
            "devices": {
                "create": {"reusable": False, "ephemeral": True, "preauthorized": True, "tags": [tag]}
            }
        },
        "expirySeconds": 600,
    }
    req = urllib.request.Request(
        "https://api.tailscale.com/api/v2/tailnet/-/keys",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with request(req, timeout=30) as response:
        key_response: dict[str, str] = json.load(response)
        return key_response["key"]


def stop_browser() -> None:
    """Stop the playwright-mcp process group: SIGTERM, then SIGKILL after five seconds."""
    try:
        pid = int(MCP_PID_FILE.read_text())
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.killpg(pid, signal.SIGKILL)
    except (FileNotFoundError, ProcessLookupError):
        pass


def restart_browser(profile: Path, save: Callable[[], None], timeout: float = 60) -> None:
    """Restart playwright-mcp so a vanished client's profile lock is released.

    Chromium's profile stays on disk. mcp_supervisor.sh relaunches the server because the
    marker says this exit is deliberate; without it the exit would take the app down.
    """
    MCP_RESTART_FILE.touch()
    stop_browser()
    try:
        save()
    except SNAPSHOT_ERRORS as error:  # the snapshot is best effort, the profile is on disk
        print(f"[personal] snapshot before reset failed: {type(error).__name__}", flush=True)
    clear_stale_lock(profile)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not MCP_RESTART_FILE.exists():
            try:
                socket.create_connection(("127.0.0.1", MCP_PORT), timeout=1).close()
                return
            except OSError:
                pass
        time.sleep(0.5)
    # A marker left behind would turn the next real crash into a silent relaunch.
    MCP_RESTART_FILE.unlink(missing_ok=True)
    raise RuntimeError("playwright-mcp did not come back")


class McpSessions:
    """Which MCP clients are still around, judged by their traffic through the nginx gate.

    playwright-mcp admits one client per persistent profile and keeps its lock after a client
    vanishes (a supervisor restart), so every later initialize fails with "Browser is already in
    use". A new session that arrives when every known one has been silent for `idle_s` is
    taken as proof that the old client is gone.
    """

    def __init__(self, idle_s: float = 120, clock: Callable[[], float] = time.monotonic) -> None:
        """Treat a client as gone after `idle_s` seconds of silence, as measured by `clock`."""
        self.idle_s = idle_s
        self.clock = clock
        self.last_seen: dict[str, float] = {}
        self.lock = threading.Lock()

    def seen(self, session_id: str) -> None:
        """Record traffic from session `session_id`."""
        with self.lock:
            self.last_seen[session_id] = self.clock()

    def ended(self, session_id: str) -> None:
        """Forget session `session_id`, which closed itself."""
        with self.lock:
            self.last_seen.pop(session_id, None)

    def abandoned(self) -> bool:
        """Return whether there are known sessions and all have been silent past the idle limit."""
        with self.lock:
            now = self.clock()
            return bool(self.last_seen) and all(now - seen > self.idle_s for seen in self.last_seen.values())

    def clear(self) -> None:
        """Forget every session."""
        with self.lock:
            self.last_seen.clear()


_PAGE_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5}"
    "button{font-size:1.1rem;padding:.6rem 1.2rem}"
)
# The ticket is spent by this form's POST (submitted at once by the script, or by the button without
# script), never by the GET a chat app's link preview makes.
VIEW_LOGIN_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>'
    f"<title>Personal browser</title><style>{_PAGE_STYLE}</style></head><body>"
    "<h1>Personal browser</h1>"
    '<form id=login method="post" action="view-login">'
    '<input type=hidden name=ticket value="{ticket}">'
    "<button type=submit>Open the browser</button></form>"
    '<script nonce="{nonce}">document.getElementById("login").submit()</script>'
    "</body></html>"
)
VIEW_EXPIRED_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>'
    f"<title>Link expired</title><style>{_PAGE_STYLE}</style></head><body>"
    "<h1>This link has expired</h1>"
    f"<p>A view link works once, for {VIEW_TICKET_TTL_S // 60} minutes. This one has expired or was "
    "already used. Ask the supervisor for a new one.</p></body></html>"
)

VIEW_BAD_REQUEST_PAGE = (
    "<!doctype html><html><head><meta charset=utf-8>"
    '<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>'
    f"<title>Bad request</title><style>{_PAGE_STYLE}</style></head><body>"
    "<h1>That request could not be read</h1>"
    "<p>Open the view link again, or ask the supervisor for a new one.</p></body></html>"
)


class ViewAccess:
    """Browser access to the noVNC view, which cannot send a bearer header.

    The human reaches /view/ through the platform's public URL. Whether the Union app proxy
    forwards a user identity we can trust is not documented, so access is a short-lived signed
    cookie instead, minted by redeeming a one-time ticket that only a bearer holder can obtain.
    The VNC password is handed over in the redirect, after that check, and nowhere else.
    """

    COOKIE = "browser_view"

    def __init__(self, vnc_password: str, clock: Callable[[], float] = time.time) -> None:
        """Sign view cookies with a fresh key; `vnc_password` is handed to the noVNC page."""
        self.vnc_password = vnc_password
        self.clock = clock
        self.key = secrets.token_bytes(32)
        self.tickets: dict[str, float] = {}
        self.lock = threading.Lock()

    def mint_ticket(self) -> str:
        """Return a new single-use ticket, valid for `VIEW_TICKET_TTL_S` seconds."""
        with self.lock:
            now = self.clock()
            self.tickets = {t: expires for t, expires in self.tickets.items() if expires > now}
            ticket = secrets.token_urlsafe(24)
            self.tickets[ticket] = now + VIEW_TICKET_TTL_S
            return ticket

    def is_valid_ticket(self, ticket: str) -> bool:
        """Return whether `ticket` is unused and unexpired, without spending it."""
        with self.lock:
            return self.tickets.get(ticket, 0) > self.clock()

    def redeem(self, ticket: str) -> str | None:
        """Return a session cookie value for a valid unused ticket, else None."""
        with self.lock:
            expires = self.tickets.pop(ticket, 0)
        if expires <= self.clock():
            return None
        return self._sign(int(self.clock()) + VIEW_SESSION_TTL_S)

    def _sign(self, expiry: int) -> str:
        return f"{expiry}.{hmac.new(self.key, str(expiry).encode(), 'sha256').hexdigest()}"

    def valid_cookie(self, header: str) -> bool:
        """Return whether the Cookie `header` carries an unexpired, correctly signed view cookie."""
        morsel = http.cookies.SimpleCookie(header).get(self.COOKIE)
        if morsel is None:
            return False
        expiry, _, _ = morsel.value.partition(".")
        return (
            expiry.isdigit()
            and int(expiry) > self.clock()
            and hmac.compare_digest(morsel.value.encode(), self._sign(int(expiry)).encode())
        )


def write_vnc_password() -> str:
    """Return a per-boot x11vnc password, in a private file x11vnc reads with -passwdfile."""
    VNC_DIR.mkdir(mode=0o700, exist_ok=True)
    VNC_DIR.chmod(0o700)
    # VNC authentication only looks at the first eight characters.
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    password = "".join(secrets.choice(alphabet) for _ in range(8))
    descriptor = os.open(VNC_DIR / "passwd", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(password)
    return password


def shutdown(
    profile: Path, uri: str, lease: Lease, network: Tailscale | None, held: bool, ready: bool = True
) -> None:
    """Stop the browser and, if this process holds the lease and restored the profile, save it.

    The network is closed either way.
    """
    stop_browser()
    try:
        # An unrestored profile is not the user's profile: publishing it would bury their archives.
        if held and ready and lease.renew():
            snapshot(profile, uri, lease)
    finally:
        if network:
            network.close()


def authorized(header: str, token: str) -> bool:
    """Return whether the Authorization `header` is the bearer `token`, compared in constant time."""
    return hmac.compare_digest(header.encode(), ("Bearer " + token).encode())


class Control:
    """What the control endpoint on 127.0.0.1:8932 needs; nginx is its only caller."""

    def __init__(
        self,
        token: str,
        exit_node: str,
        gate: ExitGate,
        proxy: GateProxy | None,
        sessions: McpSessions,
        views: ViewAccess,
        restart: Callable[[], None],
    ) -> None:
        """Bundle the token, exit-node state and callbacks the HTTP handler needs."""
        self.token = token
        self.exit_node = exit_node
        self.gate = gate
        self.proxy = proxy
        self.sessions = sessions
        self.views = views
        self.restart = restart
        self.restart_lock = threading.Lock()
        self.ip: str | None = None
        self.ip_checked: float = 0
        self.ip_lock = threading.Lock()

    def reset(self) -> None:
        """Restart the browser and forget its MCP sessions, one restart at a time."""
        with self.restart_lock:
            self.restart()
            self.sessions.clear()


def handler_for(control: Control) -> type[BaseHTTPRequestHandler]:
    """Return the HTTP handler class serving the status, view and admin endpoints for `control`."""

    class Handler(BaseHTTPRequestHandler):
        def reply(
            self,
            code: int,
            body: bytes = b"",
            content_type: str | None = None,
            headers: Iterable[tuple[str, str]] = (),
        ) -> None:
            self.send_response(code)
            if content_type:
                self.send_header("Content-Type", content_type)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def bearer(self) -> bool:
            return authorized(self.headers.get("Authorization", ""), control.token)

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            if path == "/view-login":
                return self.serve_view_login_page(urllib.parse.parse_qs(query).get("ticket", [""])[0])
            if path == "/view-auth":
                # Subrequest from nginx for /view/ (including the websocket upgrade).
                allowed = self.bearer() or control.views.valid_cookie(self.headers.get("Cookie", ""))
                return self.reply(204 if allowed else 401)
            if not self.bearer():
                return self.reply(401)
            if path == "/auth":
                return self.reply(204)
            if path == "/view-ticket":
                return self.reply(200, control.views.mint_ticket().encode(), "text/plain")
            result: dict[str, str | None] = {
                "exit_node": "up" if control.exit_node else "disabled",
                "egress_ip": None,
            }
            try:
                if control.exit_node and not control.gate.ready():
                    raise RuntimeError("Exit node unavailable")
                if control.proxy and not control.proxy.listening.is_set():
                    raise RuntimeError("Gate proxy is not running")
                if path == "/exit-check":
                    self.track_mcp_session()
                else:
                    with control.ip_lock:
                        if time.monotonic() - control.ip_checked >= 60:
                            control.ip = egress(control.exit_node, control.gate)
                            control.ip_checked = time.monotonic()
                result["egress_ip"] = control.ip
                code = 200
            except (
                *TAILSCALE_ERRORS,
                RuntimeError,
            ) as error:  # do not expose upstream errors or credentials; only the class is printed
                print(f"[personal] status check failed: {type(error).__name__}", flush=True)
                result = {"exit_node": "down", "egress_ip": None}
                code = 503
            result["gate"] = "down" if control.proxy and not control.proxy.listening.is_set() else "up"
            self.reply(code, json.dumps(result).encode(), "application/json")

        def do_POST(self) -> None:
            if self.path.partition("?")[0] == "/view-login":
                try:
                    length = min(max(0, int(self.headers.get("Content-Length") or 0)), 4096)
                except ValueError:
                    return self.send_html(400, VIEW_BAD_REQUEST_PAGE)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
                return self.redeem_view_login(form.get("ticket", [""])[0])
            if not self.bearer():
                return self.reply(401)
            if self.path != "/admin/reset":
                return self.reply(404)
            try:
                control.reset()
            except (*SNAPSHOT_ERRORS, subprocess.SubprocessError) as error:  # report the class only
                print(f"[personal] reset failed: {type(error).__name__}", flush=True)
                return self.reply(503, b'{"reset": false}', "application/json")
            self.reply(200, b'{"reset": true}', "application/json")

        def track_mcp_session(self) -> None:
            session = self.headers.get("Mcp-Session-Id")
            method = self.headers.get("X-Original-Method", "")
            if session:
                (control.sessions.ended if method == "DELETE" else control.sessions.seen)(session)
            elif method == "POST" and control.sessions.abandoned():
                # An initialize while every earlier client has gone quiet.
                control.reset()

        def send_html(self, code: int, page: str, nonce: str = "") -> None:
            script = f" 'nonce-{nonce}'" if nonce else " 'none'"
            self.reply(
                code,
                page.encode(),
                "text/html; charset=utf-8",
                headers=(
                    ("Cache-Control", "no-store"),
                    ("Referrer-Policy", "no-referrer"),
                    (
                        "Content-Security-Policy",
                        "default-src 'none'; style-src 'unsafe-inline'; "
                        f"script-src{script}; form-action 'self'",
                    ),
                ),
            )

        def serve_view_login_page(self, ticket: str) -> None:
            """GET: a page whose form spends the ticket, so a link preview's fetch does not."""
            if not control.views.is_valid_ticket(ticket):
                return self.send_html(401, VIEW_EXPIRED_PAGE)
            nonce = secrets.token_urlsafe(16)
            self.send_html(
                200,
                VIEW_LOGIN_PAGE.replace("{ticket}", html.escape(ticket, quote=True)).replace(
                    "{nonce}", nonce
                ),
                nonce,
            )

        def redeem_view_login(self, ticket: str) -> None:
            cookie = control.views.redeem(ticket)
            if cookie is None:
                return self.send_html(401, VIEW_EXPIRED_PAGE)
            # path= is relative to the host root; noVNC would otherwise look for /websockify.
            # In the fragment, never the query: a fragment is not sent to any server, and nothing logs it.
            target = "/view/vnc.html#" + urllib.parse.urlencode(
                {
                    "autoconnect": 1,
                    "resize": "scale",
                    "show_dot": 1,
                    "path": "view/websockify",
                    "password": control.views.vnc_password,
                }
            )
            attributes = f"Path=/view/; Max-Age={VIEW_SESSION_TTL_S}; HttpOnly; Secure; SameSite=Lax"
            self.reply(
                303,
                headers=(
                    ("Location", target),
                    ("Set-Cookie", f"{ViewAccess.COOKIE}={cookie}; {attributes}"),
                    ("Cache-Control", "no-store"),
                ),
            )

        def log_message(self, *args: object) -> None:
            pass

    return Handler


def main() -> None:
    """Restore the profile, start the browser and its network gate, and serve until stopped."""
    profile = Path(os.environ["BROWSER_PROFILE_DIR"])
    uri = os.environ["BROWSER_PROFILE_URI"]
    token, *credentials = take_secrets(("BROWSER_PERSONAL_TOKEN", *OAUTH_ENV))
    if not uri or not token:
        raise RuntimeError("Personal browser requires profile storage and a bearer token")
    owner = (profile.parent / "personal-browser.lock").open("w")
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    lease = Lease(uri)
    deadline = time.monotonic() + 180
    while not lease.renew():
        if time.monotonic() >= deadline:
            raise RuntimeError("Personal profile lease acquisition timed out")
        if stop.wait(2):
            return
    server = None
    network = None
    held = True
    # Set once restore has answered. Until then the local profile is not the user's, so it
    # must not be published, and Chromium must not run on it.
    restored = False
    exit_node = os.environ.get("BROWSER_EXIT_NODE", "")
    gate = ExitGate(exit_node)
    heartbeat_stop = threading.Event()

    def heartbeat() -> None:
        nonlocal held
        while not heartbeat_stop.wait(30):
            try:
                renewed = lease.renew()
                held = held and renewed
            except (
                ObstoreError,
                OSError,
                ValueError,
                KeyError,
            ) as error:  # uncertain ownership must stop Chromium; the class is printed
                print(f"[personal] lease renew failed: {type(error).__name__}", flush=True)
                held = False
            if not held:
                stop_browser()
                stop.set()
                return

    def watchdog() -> None:
        nonlocal held
        # A storage request can stall longer than the lease; do not wait for its timeout.
        while not heartbeat_stop.wait(1):
            if not held or time.time() >= lease.expires - 10:
                held = False
                stop_browser()
                stop.set()
                return

    def save() -> None:
        if held and restored and lease.renew():
            snapshot(profile, uri, lease)

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        clear_stale_lock(profile)
        restore(profile, uri, lease)
        restored = True
        clear_stale_lock(profile)
        proxy = None
        if exit_node:
            # take_secrets returns exactly the two OAuth credentials.
            network = Tailscale(exit_node, *credentials, gate)  # type: ignore[call-arg, arg-type]
            threading.Thread(target=network.supervise, daemon=True).start()
            proxy = GateProxy(gate)
            threading.Thread(target=proxy.run, daemon=True).start()

        control = Control(
            token,
            exit_node,
            gate,
            proxy,
            McpSessions(float(os.environ.get("BROWSER_MCP_IDLE_S", "120"))),
            ViewAccess(write_vnc_password()),
            lambda: restart_browser(profile, save),
        )
        server = ThreadingHTTPServer(("127.0.0.1", 8932), handler_for(control))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if not held or stop.is_set():
            raise RuntimeError("Personal profile lease lost during startup")
        Path("/tmp/personal-ready").touch()
        last_snapshot = time.monotonic()
        while not stop.wait(30):
            if time.monotonic() - last_snapshot >= 600:
                try:
                    snapshot(profile, uri, lease)
                except SNAPSHOT_ERRORS as error:  # report class only
                    print(f"[personal] snapshot failed: {type(error).__name__}", flush=True)
                last_snapshot = time.monotonic()
    finally:
        Path("/tmp/personal-ready").unlink(missing_ok=True)
        try:
            shutdown(profile, uri, lease, network, held, restored)
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=10)
            lease.release()
            if server:
                server.shutdown()


if __name__ == "__main__":
    try:
        main()
    except (
        *SNAPSHOT_ERRORS,
        subprocess.SubprocessError,
        KeyError,
    ) as error:  # upstream exceptions may contain credentials
        print(f"[personal] lifecycle failed: {type(error).__name__}", flush=True)
        raise SystemExit(1) from None
