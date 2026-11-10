"""Scanner pins (`_scanners`): declared once, baked into the image, installed in CI with no live download."""

from __future__ import annotations

import dataclasses
import email.message
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import zipfile
from pathlib import Path

import pytest

from flyte_agent_factory import _images, _scanners

ROOT = Path(__file__).resolve().parents[1]


def _tar(body: bytes = b"#!/bin/sh\necho fake-gitleaks\n") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("gitleaks")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _zip(body: bytes = b"#!/bin/sh\necho fake-tflint\n") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tflint", body)
    return buffer.getvalue()


def _pinned(scanner: _scanners.Scanner, data: bytes) -> _scanners.Scanner:
    return dataclasses.replace(scanner, sha256=hashlib.sha256(data).hexdigest())


class Network:
    """A fetch that answers from a script of results, counting calls; and the sleeps between them."""

    def __init__(self, *results: bytes | Exception) -> None:
        self.results = list(results)
        self.urls: list[str] = []
        self.sleeps: list[float] = []

    def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        result = (
            self.results.pop(0)
            if self.results
            else urllib.error.HTTPError(url, 503, "down", email.message.Message(), None)
        )
        if isinstance(result, Exception):
            raise result
        return result

    def install(self, scanner: _scanners.Scanner, tmp_path: Path) -> Path:
        return _scanners.install(
            scanner, tmp_path / "cache", tmp_path / "bin", fetch=self.fetch, sleep=self.sleeps.append
        )


def test_the_pins_are_versions_and_sha256_digests_of_their_own_release():
    assert set(_scanners.SCANNERS) == {"gitleaks", "tflint"}
    for scanner in _scanners.SCANNERS.values():
        assert re.fullmatch(r"\d+\.\d+\.\d+", scanner.version)
        assert re.fullmatch(r"[a-f0-9]{64}", scanner.sha256)
        assert scanner.url.startswith("https://github.com/")
        assert f"/v{scanner.version}/" in scanner.url
        assert scanner.cache_key == f"scanner-{scanner.name}-{scanner.version}-{scanner.sha256}"


def test_no_other_file_declares_a_scanner_pin():
    # One place: a second copy of a version, digest or release URL could drift from it.
    pins = [s.version for s in _scanners.SCANNERS.values()] + [s.sha256 for s in _scanners.SCANNERS.values()]
    pins += ["gitleaks/releases/download", "tflint/releases/download"]
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "src", "tests", "ops", ".github", "factory-improvement"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    here = {"src/flyte_agent_factory/_scanners.py", "tests/test_scanners.py"}
    offenders = [
        f"{name}: {pin}"
        for name in tracked
        if name not in here and (ROOT / name).is_file()
        for pin in pins
        if pin in (ROOT / name).read_text(errors="replace")
    ]
    assert offenders == []


@pytest.mark.parametrize("browser", [False, True])
def test_the_image_bakes_both_scanners_checksum_verified_and_runs_them(browser):
    image = _images.agent_image(browser=browser)
    commands = [c for layer in image._layers for c in getattr(layer, "commands", ())]
    for scanner in _scanners.SCANNERS.values():
        [step] = [c for c in commands if scanner.url in c]
        assert step == _images.TOOLS[scanner.name] == _scanners.render_image_install_step(scanner)
        assert f"echo '{scanner.sha256}  /tmp/{scanner.archive}' | sha256sum -c - " in step
        assert "--retry 5" in step
        assert "--connect-timeout 30" in step
        assert f"--max-time {_scanners.TIMEOUT_S}" in step
        # The build runs the binary it installed, so an image that cannot run it does not build.
        assert step.endswith(f"&& {' '.join((scanner.name, *scanner.version_args))}")


def test_the_image_install_step_probes_the_version_the_way_the_tool_expects():
    gitleaks = dataclasses.replace(_scanners.GITLEAKS, sha256="0" * 64)
    tflint = dataclasses.replace(_scanners.TFLINT, sha256="0" * 64)

    assert _scanners.render_image_install_step(gitleaks).endswith("&& gitleaks version")
    assert _scanners.render_image_install_step(tflint).endswith("&& tflint --version")


def test_the_gitleaks_on_path_is_the_pinned_version_and_runs():
    out = subprocess.run(["gitleaks", "version"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == _scanners.GITLEAKS.version


def test_the_tflint_on_path_is_the_pinned_version_and_runs():
    out = subprocess.run(["tflint", "--version"], capture_output=True, text=True, check=True).stdout
    assert out.splitlines()[0] == f"TFLint version {_scanners.TFLINT.version}"


@pytest.mark.parametrize(
    ("scanner", "archive"),
    [(_scanners.GITLEAKS, _tar()), (_scanners.TFLINT, _zip())],
    ids=["gitleaks", "tflint"],
)
def test_a_cold_cache_downloads_verifies_caches_and_installs(tmp_path, scanner, archive):
    scanner = _pinned(scanner, archive)
    network = Network(archive)

    path = network.install(scanner, tmp_path)

    assert network.urls == [scanner.url]
    assert path == tmp_path / "bin" / scanner.name
    assert f"fake-{scanner.name}" in subprocess.run([path], capture_output=True, text=True).stdout
    assert (tmp_path / "cache" / scanner.cache_key / scanner.archive).read_bytes() == archive


@pytest.mark.parametrize(
    ("scanner", "archive"),
    [(_scanners.GITLEAKS, _tar()), (_scanners.TFLINT, _zip())],
    ids=["gitleaks", "tflint"],
)
def test_a_cache_hit_makes_no_request(tmp_path, scanner, archive):
    scanner = _pinned(scanner, archive)
    Network(archive).install(scanner, tmp_path)
    (tmp_path / "bin").joinpath(scanner.name).unlink()
    offline = Network()  # every request would fail

    path = offline.install(scanner, tmp_path)

    assert offline.urls == []
    assert path.read_bytes() == f"#!/bin/sh\necho fake-{scanner.name}\n".encode()


def test_an_archive_without_the_binary_at_its_root_fails_naming_the_tool_and_is_not_cached(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("gitleaks-8/gitleaks")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    archive = buffer.getvalue()
    scanner = _pinned(_scanners.GITLEAKS, archive)

    with pytest.raises(_scanners.Unavailable, match=r"holds no gitleaks") as raised:
        Network(archive).install(scanner, tmp_path)

    assert scanner.url in str(raised.value)
    assert not (tmp_path / "cache" / scanner.cache_key).exists()
    assert not (tmp_path / "bin" / "gitleaks").exists()


def test_a_cached_archive_that_fails_its_checksum_is_downloaded_again(tmp_path):
    archive = _tar()
    scanner = _pinned(_scanners.GITLEAKS, archive)
    cached = tmp_path / "cache" / scanner.cache_key / scanner.archive
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"truncated")
    network = Network(archive)

    network.install(scanner, tmp_path)

    assert network.urls == [scanner.url]
    assert cached.read_bytes() == archive


def test_a_cold_download_retries_with_backoff_until_one_succeeds(tmp_path):
    archive = _zip()
    scanner = _pinned(_scanners.TFLINT, archive)
    down = urllib.error.HTTPError(scanner.url, 504, "Gateway Time-out", email.message.Message(), None)
    network = Network(down, OSError("connection reset"), archive)

    network.install(scanner, tmp_path)

    assert len(network.urls) == 3
    assert network.sleeps == [_scanners.BACKOFF_S, 2 * _scanners.BACKOFF_S]


def test_the_cold_download_rides_out_a_multi_minute_release_outage():
    # GitHub release URLs returned 504s for minutes at a time (2026-11-08/09); 35 s of backoff was not enough.
    assert sum(_scanners.BACKOFF_S * 2**i for i in range(_scanners.ATTEMPTS - 1)) >= 120


def test_a_download_that_never_succeeds_fails_naming_the_tool_and_url(tmp_path):
    network = Network()

    with pytest.raises(_scanners.Unavailable) as failure:
        network.install(_scanners.GITLEAKS, tmp_path)

    assert _scanners.ATTEMPTS >= 3
    assert len(network.urls) == _scanners.ATTEMPTS
    assert network.sleeps == [_scanners.BACKOFF_S * 2**i for i in range(_scanners.ATTEMPTS - 1)]
    assert "gitleaks" in str(failure.value) and _scanners.GITLEAKS.url in str(failure.value)
    assert not (tmp_path / "bin" / "gitleaks").exists()


def test_a_download_whose_checksum_differs_is_never_installed(tmp_path):
    network = Network(*[_tar(b"#!/bin/sh\necho tampered\n")] * _scanners.ATTEMPTS)

    with pytest.raises(_scanners.Unavailable, match="checksum"):
        network.install(_scanners.GITLEAKS, tmp_path)

    assert not (tmp_path / "bin" / "gitleaks").exists()
    assert not (tmp_path / "cache" / _scanners.GITLEAKS.cache_key).exists()


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    # As CI runs it: the file, by path, with only the standard library.
    script = ROOT / "src" / "flyte_agent_factory" / "_scanners.py"
    return subprocess.run([sys.executable, "-I", str(script), *args], capture_output=True, text=True)


def test_the_cli_prints_the_cache_key_for_github_output():
    proc = _cli("cache-key", "gitleaks", "tflint")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"gitleaks={_scanners.GITLEAKS.cache_key}\ntflint={_scanners.TFLINT.cache_key}\n"


def test_the_cli_installs_from_the_cache_with_downloads_failing(tmp_path, monkeypatch, capsys):
    archive = _tar()
    scanner = _pinned(_scanners.GITLEAKS, archive)
    Network(archive).install(scanner, tmp_path)
    monkeypatch.setitem(_scanners.SCANNERS, "gitleaks", scanner)
    monkeypatch.setattr(_scanners, "_fetch", Network().fetch)
    monkeypatch.setattr(
        time, "sleep", lambda seconds: pytest.fail(f"a cache hit slept {seconds}s: it downloaded")
    )

    args = ["install", "--cache", str(tmp_path / "cache"), "--bin", str(tmp_path / "out"), "gitleaks"]
    assert _scanners.main(args) == 0

    assert (tmp_path / "out" / "gitleaks").read_bytes() == b"#!/bin/sh\necho fake-gitleaks\n"
    assert "from the cache, checksum verified" in capsys.readouterr().err


def test_the_cli_fails_clearly_when_a_scanner_cannot_be_obtained(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_scanners, "_fetch", Network().fetch)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    args = ["install", "--cache", str(tmp_path / "cache"), "--bin", str(tmp_path / "bin"), "tflint"]
    assert _scanners.main(args) == 1

    err = capsys.readouterr().err
    assert f"error: could not obtain tflint {_scanners.TFLINT.version} from {_scanners.TFLINT.url}" in err
    assert not (tmp_path / "bin" / "tflint").exists()
