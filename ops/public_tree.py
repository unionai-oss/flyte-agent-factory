"""Build the public repository's first commit from this checkout: the tree the CI scan reads.

    python ops/public_tree.py build <dest>          # the fresh-history tree, one commit, no remote

The tree is `git archive HEAD` (tracked, committed files only: nothing untracked can leak in) minus `PRIVATE`
and anything the tree's own ignore rules exclude, committed once as a new repository with no history and no
remote. CI scans it with `ops/scan_public.py`. To check it against a target's own CI and send it there, use
`ops/promote.py deliver <dest> <branch> --base <sha> --to <url> [--dry-run]` (docs/public-cutover.md).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Tracked here but not public: other projects' harness files (criteria, plans, review verdicts), and root
#: `standards/`, the owner's own set, which #122 removed (the engine no longer reads it, docs/standards.md;
#: the workspace template ships the example set). Kept here so a set added back is still left out.
PRIVATE = (".claude-flyte", "standards")
#: The public repository's first commit. A neutral identity: the history of this one is not carried over.
AUTHOR = ("flyte-agent-factory maintainers", "maintainers@flyte-agent-factory.invalid")
MESSAGE = "Initial public release of flyte-agent-factory"


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _drop_ignored(dest: Path, env: dict[str, str]) -> None:
    """Delete what the tree's own ignore rules exclude, and the directories that leaves empty.

    `git archive` extracts every tracked file, including one tracked despite an ignore rule (the generated
    skill archive, rendered from a profile); the fresh commit leaves such a file out. By the repository's
    own declaration it is generated or private, so it does not go public, and the tree holds only what the
    commit holds.
    """
    ignored = _git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=dest, env=env)
    for rel in filter(None, ignored.split("\0")):
        path = dest / rel
        path.unlink()
        parent = path.parent
        while parent != dest and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def build(dest: Path, source: Path | None = None, rev: str = "HEAD") -> str:
    """Write `source`'s public tree at `rev` into `dest` (absent or empty) as one commit; return its sha."""
    if dest.exists() and any(dest.iterdir()):
        raise FileExistsError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    source = source or ROOT
    with tempfile.TemporaryFile() as archive:
        # The repository's own .gitattributes apply to what is extracted; the user's attributes file does not.
        subprocess.run(
            ["git", "-c", f"core.attributesFile={os.devnull}", "archive", "--format=tar", rev],
            cwd=source,
            stdout=archive,
            check=True,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            tar.extractall(dest, filter="tar")
    for name in PRIVATE:
        target = dest / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    name, email = AUTHOR
    # No global or system git config: the owner's identity, hooks or templates must not reach the tree.
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        # Nor the user's default excludes or attributes file: only the tree's own .gitignore decides what is
        # ignored, and its own .gitattributes how content is stored.
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "core.excludesFile",
        "GIT_CONFIG_VALUE_0": os.devnull,
        "GIT_CONFIG_KEY_1": "core.attributesFile",
        "GIT_CONFIG_VALUE_1": os.devnull,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    _git("init", "-q", "-b", "main", cwd=dest, env=env)
    _git("add", "-A", cwd=dest, env=env)
    _drop_ignored(dest, env)
    _git("commit", "-q", "--no-verify", "-m", MESSAGE, cwd=dest, env=env)
    leftover = _git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=dest, env=env)
    if leftover:
        raise RuntimeError(f"the tree holds files its commit does not:\n{leftover}")
    return _git("rev-parse", "HEAD", cwd=dest, env=env)


def describe_build(dest: Path, sha: str) -> str:
    """Return the line that says where the tree was built, at which commit, and how many files it holds."""
    return f"built {dest} at {sha} ({len(_git('ls-files', cwd=dest).splitlines())} files)"


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("build").add_argument("dest", type=Path)
    args = parser.parse_args(argv)
    try:
        dest = args.dest.resolve()
        print(describe_build(dest, build(dest)))
        return 0
    except (FileExistsError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"public_tree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
