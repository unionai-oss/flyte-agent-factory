"""The third-party scanners the gates run (gitleaks, tflint): one pin each, and an installer for CI.

The one place their versions and checksums are declared. The factory image bakes them from these pins
(`_images.TOOLS`), so a pod or a test lane never downloads one; CI restores the verified archive from
`actions/cache`, keyed by `cache-key`, and only a cold cache downloads, with retries:

    python3 src/flyte_agent_factory/_scanners.py cache-key gitleaks tflint  # gitleaks=scanner-gitleaks-...
    python3 src/flyte_agent_factory/_scanners.py install --cache DIR --bin DIR gitleaks tflint

Standard library only, so CI can run it before any environment is built. A scanner that cannot be obtained
fails the install with the tool and URL named; nothing skips a scan in its place.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import sys
import tarfile
import time
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scanner:
    name: str
    version: str
    url: str
    #: Of the release archive, as published in the release's checksums file.
    sha256: str
    #: Arguments that print the version.
    version_args: tuple[str, ...]

    @property
    def archive(self) -> str:
        return self.url.rsplit("/", 1)[1]

    @property
    def cache_key(self) -> str:
        return f"scanner-{self.name}-{self.version}-{self.sha256}"


GITLEAKS = Scanner(
    "gitleaks",
    "8.30.1",
    "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz",
    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ("version",),
)
TFLINT = Scanner(
    "tflint",
    "0.55.1",
    "https://github.com/terraform-linters/tflint/releases/download/v0.55.1/tflint_linux_amd64.zip",
    "53379f38bc1e86c18885bfc85dc5fe2cd1f59729ae9a2afa16905189c1d67aa9",
    ("--version",),
)
SCANNERS = {s.name: s for s in (GITLEAKS, TFLINT)}

#: Download attempts on a cold cache, and the wait before the next (doubling each time).
ATTEMPTS = 6
BACKOFF_S = 5.0
TIMEOUT_S = 120


class Unavailable(Exception):
    """The scanner could not be obtained; the message names it and its URL."""


def render_image_install_step(scanner: Scanner) -> str:
    """Return the image build step that installs `scanner` to /usr/local/bin, checksum-verified."""
    tmp = f"/tmp/{scanner.archive}"
    unpack = (
        f"tar xzf {tmp} --no-same-owner -C /usr/local/bin {scanner.name}"
        if scanner.archive.endswith(".tar.gz")
        else f"unzip -o {tmp} {scanner.name} -d /usr/local/bin"
    )
    probe = " ".join((scanner.name, *scanner.version_args))
    return (
        f"curl -fsSL --connect-timeout 30 --max-time {TIMEOUT_S} "
        f"--retry 5 --retry-all-errors --retry-delay 5 -o {tmp} {scanner.url} "
        f"&& echo '{scanner.sha256}  {tmp}' | sha256sum -c - "
        f"&& {unpack} && chmod a+rx /usr/local/bin/{scanner.name} && rm {tmp} "
        f"&& {probe}"
    )


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
        data: bytes = response.read()
        return data


def _download(
    scanner: Scanner,
    fetch: Callable[[str], bytes],
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> bytes:
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            data = fetch(scanner.url)
        # Refused, reset, timed out, 5xx (HTTPError is an OSError) or cut short: all retried alike.
        except (OSError, http.client.HTTPException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            digest = hashlib.sha256(data).hexdigest()
            if digest == scanner.sha256:
                return data
            last = f"checksum {digest} differs from the pinned {scanner.sha256}"
        log(f"{scanner.name}: attempt {attempt}/{ATTEMPTS} at {scanner.url} failed ({last})")
        if attempt < ATTEMPTS:
            sleep(BACKOFF_S * 2 ** (attempt - 1))
    raise Unavailable(
        f"could not obtain {scanner.name} {scanner.version} from {scanner.url} after {ATTEMPTS} attempts "
        f"({last}); the scan cannot run without it"
    )


def _extract(scanner: Scanner, data: bytes, bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / scanner.name
    missing = f"{scanner.name}: {scanner.url} holds no {scanner.name} binary at the archive root"
    if scanner.archive.endswith(".tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            try:
                member = tar.extractfile(scanner.name)
            except KeyError:
                raise Unavailable(missing) from None
            # None: the member is a directory or other non-file entry (a symlink is followed, so it yields
            # its target).
            if member is None:
                raise Unavailable(missing)
            body = member.read()
    else:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            try:
                body = archive.read(scanner.name)
            except KeyError:
                raise Unavailable(missing) from None
    target.write_bytes(body)
    target.chmod(0o755)
    return target


def install(
    scanner: Scanner,
    cache_dir: Path,
    bin_dir: Path,
    *,
    fetch: Callable[[str], bytes] | None = None,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Install `scanner` into `bin_dir` from the archive cached in `cache_dir`, downloading only without one.

    The cached archive is checked against the pin like a download; a mismatch is discarded and fetched again.
    """
    fetch = fetch or _fetch
    sleep = sleep or time.sleep
    log = log or (lambda message: print(message, file=sys.stderr))
    cached = cache_dir / scanner.cache_key / scanner.archive
    data = cached.read_bytes() if cached.is_file() else None
    if data is not None and hashlib.sha256(data).hexdigest() != scanner.sha256:
        log(f"{scanner.name}: cached {cached} differs from the pinned checksum; downloading it again")
        data = None
    if data is None:
        data = _download(scanner, fetch, sleep, log)
        # Extract before caching: only an archive that verified and unpacked is saved for the next run.
        path = _extract(scanner, data, bin_dir)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return path
    log(f"{scanner.name} {scanner.version}: from the cache, checksum verified")
    return _extract(scanner, data, bin_dir)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="_scanners.py", description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("cache-key", help="print `<tool>=<cache key>` lines for $GITHUB_OUTPUT")
    key.add_argument("tools", nargs="+", choices=sorted(SCANNERS))
    put = commands.add_parser("install", help="install scanners from the cache, downloading on a miss")
    put.add_argument("--cache", type=Path, required=True)
    put.add_argument("--bin", type=Path, required=True)
    put.add_argument("tools", nargs="+", choices=sorted(SCANNERS))
    args = parser.parse_args(argv)
    if args.command == "cache-key":
        for tool in args.tools:
            print(f"{tool}={SCANNERS[tool].cache_key}")
        return 0
    for tool in args.tools:
        try:
            path = install(SCANNERS[tool], args.cache, args.bin)
        except Unavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"{tool} {SCANNERS[tool].version}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
