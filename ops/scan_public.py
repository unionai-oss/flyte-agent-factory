"""Check that a directory is fit to be public: no secrets (gitleaks) and no owner-specific literals.

`scan_public.py <path>`; exits non-zero on any finding, and on a root it cannot vouch for: one that is not a
directory, or a non-empty one in which it read no file. The literals are the ones `tests/test_profile.py`
enforces on the engine (`ops/personal_literals.py`: generic shapes, plus the owner's private list when
`FACTORY_OWNER_LITERALS` names it), with the same line allowlist. The output says which of the two it used.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
#: The default rules plus the values in this tree that look like secrets and are not.
GITLEAKS_CONFIG = HERE / "gitleaks.toml"


def _personal_literals() -> ModuleType:
    # By path: this runs as a script, and tests load it by path too, so `ops` is not a package on sys.path.
    spec = importlib.util.spec_from_file_location("personal_literals", HERE / "personal_literals.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_literals = _personal_literals()
LINE_ALLOWLIST: tuple[tuple[str, str], ...] = _literals.LINE_ALLOWLIST


def list_files_under(path: Path) -> list[Path]:
    """Every file under `path`, `.git` excluded: what the literal scan reads."""
    return sorted(p for p in path.rglob("*") if p.is_file() and ".git" not in p.relative_to(path).parts)


def _read_text(file: Path) -> str | None:
    try:
        return file.read_text()
    except UnicodeDecodeError:
        return None


def literal_hits(path: Path, extra: list[str] | None = None) -> list[str]:
    """`file:line` for each line under `path` (`.git` excluded) naming an owner; binary files are skipped.

    `extra` is the owner's list; by default the one `FACTORY_OWNER_LITERALS` names, if any.
    """
    found = _literals.pattern(extra)
    hits = []
    for file in list_files_under(path):
        text = _read_text(file)
        if text is None:
            continue
        name = file.relative_to(path).as_posix()
        hits += [
            f"{name}:{n}: personal literal"
            for n, line in enumerate(text.splitlines(), 1)
            if found.search(line) and not any(name == p and allowed in line for p, allowed in LINE_ALLOWLIST)
        ]
    return hits


def gitleaks_hits(path: Path) -> list[str]:
    """Return findings from gitleaks over the files, and over the history when `path` is a repository."""
    if not shutil.which("gitleaks"):
        raise RuntimeError(
            "gitleaks is not installed; install it (https://github.com/gitleaks/gitleaks) and rerun"
        )
    config = ["--config", str(GITLEAKS_CONFIG)]
    runs = [["gitleaks", "detect", "--source", str(path), "--no-git", "--redact", *config]]
    if (path / ".git").exists():
        runs.append(["gitleaks", "git", str(path), "--redact", *config])
    hits = []
    for cmd in runs:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            hits.append(
                f"{' '.join(cmd[:2])}: exit {proc.returncode}\n{(proc.stdout + proc.stderr).strip()[-1500:]}"
            )
    return hits


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    # A root that cannot be read is not clean: neither scanner runs on it.
    if not args.path.is_dir():
        print(f"scan_public: {args.path} is not a directory", file=sys.stderr)
        return 2
    try:
        owner = _literals.private_list()
        findings = literal_hits(args.path, owner) + gitleaks_hits(args.path)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"scan_public: {exc}", file=sys.stderr)
        return 2
    source = f"{len(owner)} owner literals" if owner else f"generic shapes only ({_literals.ENV} unset)"
    print(f"literals: {source}", file=sys.stderr)
    files = list_files_under(args.path)
    binary = sum(_read_text(file) is None for file in files)
    print(f"scanned {len(files)} files under {args.path} ({binary} binary, skipped)", file=sys.stderr)
    if len(files) == binary and any(args.path.iterdir()):
        print(f"scan_public: read no file under {args.path}, which is not empty", file=sys.stderr)
        return 2
    print("\n".join(findings) if findings else "clean")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
