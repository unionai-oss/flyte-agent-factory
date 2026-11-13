"""The uv and the Python a target's workflow sets up (`astral-sh/setup-uv`, `actions/setup-python`).

The pod's own uv may be older than the one the target's lock file needs (`Failed to parse uv.lock`), so a
job gets the uv its workflow installs, kept in a cache under `$HOME`. Why a job could not get its tools is
returned as text, never raised: the gate reports that job NOT RUN rather than failing it for the pod's lack.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import _gate_base

_RELEASES = "https://github.com/astral-sh/uv/releases"
_EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
_PYTHON_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")
_TIMEOUT = 900
#: What running `make` once for a (home, PATH, ...) key came to: why not, and what it changed in the env. A
#: network that is down is not retried (and waited for) by every command of the run, and a later command
#: gets the same PATH edits without redoing the work.
_DONE: dict[tuple[object, ...], tuple[str, dict[str, str]]] = {}


def once(key: tuple[object, ...], env: dict[str, str], make: Callable[[], str]) -> str:
    """Run `make` (which edits `env` in place and returns why it failed, or "") once per `key`."""
    if key not in _DONE:
        before = dict(env)
        reason = make()
        _DONE[key] = (reason, {name: value for name, value in env.items() if before.get(name) != value})
    reason, changes = _DONE[key]
    env.update(changes)
    return reason


def triple() -> str:
    """Return the platform name uv's release archives carry."""
    return f"{platform.machine().replace('arm64', 'aarch64')}-unknown-linux-gnu"


def _download(url: str) -> bytes:
    """Fetch a URL. Its own function so a test can take the network away."""
    with urllib.request.urlopen(url, timeout=60) as response:
        data: bytes = response.read()
    return data


def prepare(env: dict[str, str], uv: str, python: str) -> str:
    """Put the workflow's uv and Python first on `env["PATH"]`; return why not, or "".

    `uv` is "" (the workflow sets none up), "latest" or an exact version; `python` is "" or `X.Y[.Z]`.
    """
    if not env.get("HOME"):
        return "HOME is not set: nowhere to keep uv"
    if uv and (reason := once(("uv", env["HOME"], env.get("PATH"), uv), env, lambda: _ensure_uv(env, uv))):
        return reason
    if python and _PYTHON_VERSION.fullmatch(python):
        return once(
            ("python", env["HOME"], env.get("PATH"), python), env, lambda: _ensure_python(env, python)
        )
    return ""  # setup-python of a version this cannot name (`3.x`, a file): the pod's Python is used


def _ensure_uv(env: dict[str, str], version: str) -> str:
    if version != "latest" and not _EXACT_VERSION.fullmatch(version):
        return f"cannot install uv `{version}`: the gate installs an exact version or the latest"
    directory = Path(env["HOME"]) / ".cache/flyte-agent-factory/uv" / version
    # A cached uv is run only if this code verified it: one cached before checksums were checked, or put
    # there by anything else, is downloaded and checked again.
    if version == "latest" or not (directory / "uv").is_file() or not (directory / _VERIFIED).is_file():
        if failure := _install_uv(directory, version):
            return failure
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
    return ""


#: Written beside a uv whose archive matched its published checksum: the cache's proof of that.
_VERIFIED = "verified.sha256"


def _install_uv(directory: Path, version: str) -> str:
    name = f"uv-{triple()}.tar.gz"
    base = f"{_RELEASES}/latest/download" if version == "latest" else f"{_RELEASES}/download/{version}"
    try:
        archive = _download(f"{base}/{name}")
        try:
            published = _download(f"{base}/{name}.sha256").decode().split()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            published = []
        if not published:
            # An archive nothing vouches for is not run: the job is NOT RUN rather than trusting it.
            return f"uv {version}: the release publishes no checksum"
        digest = hashlib.sha256(archive).hexdigest()
        if digest != published[0].lower():
            return f"uv {version}: the downloaded archive does not match its published checksum"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / _VERIFIED).unlink(missing_ok=True)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile() and Path(member.name).name in {"uv", "uvx"}:
                    # Renamed into place: another job may be running the cached one, and a running
                    # binary cannot be rewritten ("Text file busy").
                    target = directory / Path(member.name).name
                    partial = directory / f".{target.name}.{os.getpid()}.partial"
                    partial.write_bytes(tar.extractfile(member).read())  # type: ignore[union-attr]
                    partial.chmod(0o755)
                    partial.replace(target)
        if (directory / "uv").is_file():
            (directory / _VERIFIED).write_text(f"{digest}\n")
    except (OSError, tarfile.TarError, UnicodeDecodeError) as exc:
        return f"uv {version} cannot be installed: {exc}"
    return "" if (directory / "uv").is_file() else f"uv {version}: the release archive holds no uv"


def _ensure_python(env: dict[str, str], version: str) -> str:
    """Make `python` and `python3` the requested CPython, from the machine or fetched with uv."""
    if not shutil.which("uv", path=env.get("PATH")):
        return ""  # nothing to fetch a Python with: the pod's is used
    home = Path(env["HOME"])

    def uv(*args: str) -> tuple[int, str]:
        return _gate_base._run(["uv", "python", *args], home, env=env, timeout=_TIMEOUT)

    code, found = uv("find", "--no-project", version)
    if code:
        code, output = uv("install", version)
        if code:
            return f"`uv python install {version}` failed with exit {code}:\n{output[-1500:]}"
        code, found = uv("find", "--no-project", version)
        if code:
            return f"no Python {version} after installing it:\n{found[-1500:]}"
    interpreter = found.strip().splitlines()[-1]
    directory = home / ".cache/flyte-agent-factory/python" / version
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        link = directory / name
        link.unlink(missing_ok=True)
        link.symlink_to(interpreter)
    env["PATH"] = str(directory) + os.pathsep + env.get("PATH", "")
    return ""
