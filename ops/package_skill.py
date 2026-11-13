"""Build the chat skills under skills/ into deterministic, installable zips in skills/dist/."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"
DIST = SKILLS / "dist"
VERSION_LINE = re.compile(r"^Skill version: ([0-9a-f]{12})$", re.MULTILINE)
# A fixed timestamp keeps the zips byte-identical between builds; the committed copy is compared in CI.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


def list_skill_names() -> list[str]:
    """Return the name of every skill source directory (those holding a SKILL.md), sorted."""
    return sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))


def list_source_files(name: str) -> list[Path]:
    """Return the files a skill ships: SKILL.md and everything under references/, sorted by path."""
    base = SKILLS / name
    files = [base / "SKILL.md", *(p for p in (base / "references").rglob("*") if p.is_file())]
    return sorted(files)


def compute_source_digest() -> str:
    """Return the first 12 hex digits of a sha256 over every skill source file (path, then bytes)."""
    digest = hashlib.sha256()
    for name in list_skill_names():
        for path in list_source_files(name):
            digest.update(path.relative_to(SKILLS).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def build_files(name: str, digest: str) -> dict[str, bytes]:
    """Return a skill's files by path inside the skill, with the source digest appended to SKILL.md."""
    files = {}
    for path in list_source_files(name):
        relative = path.relative_to(SKILLS / name).as_posix()
        data = path.read_bytes()
        if relative == "SKILL.md":
            data = data.rstrip(b"\n") + f"\n\nSkill version: {digest}\n".encode()
        files[relative] = data
    return files


def build_zip(name: str, digest: str) -> bytes:
    """Return the zip for one skill, with the source digest appended to its SKILL.md."""
    out = io.BytesIO()
    # Stored, not deflated: compressed bytes can differ between zlib builds and the drift test compares bytes.
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for relative, data in build_files(name, digest).items():
            info = zipfile.ZipInfo(f"{name}/{relative}", ZIP_TIME)
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return out.getvalue()


def build(dist: Path = DIST) -> list[Path]:
    """Write every skill zip and VERSION into `dist`; return the zip paths."""
    digest = compute_source_digest()
    dist.mkdir(parents=True, exist_ok=True)
    for stale in dist.glob("*.zip"):
        stale.unlink()
    written = []
    for name in list_skill_names():
        target = dist / f"{name}.zip"
        target.write_bytes(build_zip(name, digest))
        written.append(target)
    (dist / "VERSION").write_text(digest + "\n")
    return written


def read_installed(path: Path) -> tuple[str, dict[str, bytes]]:
    """Return the name and files (by path inside the skill) of an installed zip, directory or SKILL.md."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = [n for n in archive.namelist() if not n.endswith("/")]
            # Without a top-level directory the skill's name is unknown: "" says so to `check`.
            top = entries[0].split("/")[0] if entries and "/" in entries[0] else ""
            files = {n.split("/", 1)[-1] if top else n: archive.read(n) for n in entries}
        return top, files
    # An installed directory may have been renamed, so its name is only a hint (see below).
    directory = path.parent if path.is_file() else path
    # The file given is the skill's SKILL.md whatever its name, read as text so a binary file is an error.
    skill_md = path if path.is_file() else directory / "SKILL.md"
    files = {"SKILL.md": skill_md.read_text(encoding="utf-8").encode()}
    # Every file, not only references/: one the build does not make is drift, and `check` reports it.
    for file in directory.rglob("*"):
        if file.is_file() and file != skill_md and "__pycache__" not in file.parts:
            files[file.relative_to(directory).as_posix()] = file.read_bytes()
    if directory.name in list_skill_names():
        return directory.name, files
    front = re.search(r"^name: *(\S+)", files["SKILL.md"].decode(), re.MULTILINE)
    return (front[1] if front else directory.name), files


def check(path: Path) -> int:
    """Print whether an installed skill matches the source and return the exit code (0, 1 or 2)."""
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 2
    try:
        name, installed_files = read_installed(path)
    except (
        zipfile.BadZipFile,
        UnicodeDecodeError,
        OSError,
    ) as exc:  # a corrupt zip, a binary file, no access
        print(f"error: cannot read {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not name:
        print("unknown: SKILL.md is not inside a <skill name>/ directory")
        return 2
    match = VERSION_LINE.search(installed_files.get("SKILL.md", b"").decode(errors="replace"))
    if match is None:
        print("unknown: no Skill version line")
        return 2
    installed, source = match[1], compute_source_digest()
    if installed != source:
        print(f"stale: installed {installed}, source {source}")
        return 1
    # A current version line can still sit on edited files, so compare content, not the container: a zip
    # re-saved with other compression or entry order is the same skill.
    fresh = build_files(name, source) if name in list_skill_names() else {}
    for relative in sorted(fresh.keys() | installed_files.keys()):
        if fresh.get(relative) != installed_files.get(relative):
            # A zip is named as a whole (the file the person holds), with the path found after it.
            if zipfile.is_zipfile(path):
                print(f"stale: {path.name} differs from a fresh build of source {source} ({relative})")
            else:
                print(f"stale: {relative} differs from a fresh build of source {source}")
            return 1
    print("current")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Build the skill zips, or with --check compare an installed skill against the source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        type=Path,
        metavar="PATH",
        help="an installed skill zip, its unzipped directory, or its SKILL.md",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check(args.check)
    for path in build():
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
