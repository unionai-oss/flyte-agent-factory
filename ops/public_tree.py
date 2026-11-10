"""Build the public repository's first commit from this checkout, and check it: the cutover dry run.

    python ops/public_tree.py build <dest>          # the fresh-history tree, one commit, no remote
    python ops/public_tree.py dry-run [<dest>]      # build, then the full checks and ops/scan_public.py on it

The tree is `git archive HEAD` (tracked, committed files only: nothing untracked can leak in) minus `PRIVATE`
and anything the tree's own ignore rules exclude, committed once as a new repository with no history and no
remote. `dry-run` then runs `ops/scan_public.py` on the tree (gitleaks over the files and the one-commit
history, and the personal-literal list), and `factory-improvement/checks.sh`, then the slow lane after it, in
a temporary clone of it. With `AGENT_FACTORY_SLOW_LANE=child`, checks.sh runs both lanes in separate child
actions instead. A failure in any check or a change to the built tree fails the dry run. It pushes nothing:
publishing is the owner's step (docs/public-cutover.md).
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


def _python_in_tree() -> list[str]:
    """Return the interpreter of the tree's own locked environment, as checks.sh runs it."""
    return ["uv", "run", "python"]


def dry_run(dest: Path) -> int:
    """Build the tree at `dest`, scan it and run the full checks on it; return 0 only if all pass.

    The checks run in a temporary clone of the tree's one commit, so `dest` stays exactly as built (no
    `.venv`, caches or files the tests write); the dry run fails if it does not.
    """
    # Absolute before anything runs: a relative path given to a step whose cwd is elsewhere names nothing.
    dest = dest.resolve()
    sha = build(dest)
    print(describe_build(dest, sha), flush=True)
    env = dict(os.environ)
    # AGENT_FACTORY_SLOW_LANE=child is the caller's request to spend child-action CPUs: checks.sh then sends
    # both pytest lanes to containers of their own (they overlap without contending here) and the sequential
    # slow step below would only repeat the slow lane. Else checks.sh runs its fast lane only, slow after.
    child_lanes = env.get("AGENT_FACTORY_SLOW_LANE") == "child"
    for name in ("AGENT_FACTORY_RUN_SLOW", "AGENT_FACTORY_PYTEST_JUNIT_DIR"):
        env.pop(name, None)
    if not child_lanes:
        env.pop("AGENT_FACTORY_SLOW_LANE", None)
    slow = [*_python_in_tree(), "-m", "pytest", "tests", "-q", "-m", "slow"]
    results = {}
    with tempfile.TemporaryDirectory(prefix="public-tree-checks-") as scratch:
        work = Path(scratch) / "tree"
        _git("clone", "-q", "--no-hardlinks", str(dest), str(work), cwd=Path(scratch))
        # The scan reads `dest` itself. Then the lanes one at a time rather than checks.sh's two at once
        # (AGENT_FACTORY_RUN_SLOW=1): on a 4-CPU pod two xdist pools push the benchmark's inner pytest past
        # its exit limit; alone they pass.
        steps: tuple[tuple[str, list[str], dict[str, str]], ...] = (
            # No bytecode: the scanner's own files need not be compiled to scan a tree.
            (
                "scan_public",
                [sys.executable, "ops/scan_public.py", str(dest)],
                {"PYTHONDONTWRITEBYTECODE": "1"},
            ),
            ("checks", ["bash", "factory-improvement/checks.sh"], {}),
            ("slow lane", slow, {"PYTHONPATH": "src"}),
        )
        for label, cmd, extra in steps[:2] if child_lanes else steps:
            print(f"--- {label}: {' '.join(cmd)}", flush=True)
            results[label] = subprocess.run(cmd, cwd=work, env={**env, **extra}).returncode
    leftover = _git("status", "--porcelain", "--ignored", "--untracked-files=all", cwd=dest)
    if leftover:
        print(f"--- the built tree no longer holds only its commit:\n{leftover}", flush=True)
    results["tree as built"] = 1 if leftover else 0
    status = " ".join(f"{label.replace(' ', '_')}={code}" for label, code in results.items())
    print(f"dry run {'PASSED' if not any(results.values()) else 'FAILED'}: {status}", flush=True)
    return 1 if any(results.values()) else 0


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("build").add_argument("dest", type=Path)
    sub.add_parser("dry-run").add_argument("dest", type=Path, nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.step == "build":
            dest = args.dest.resolve()
            print(describe_build(dest, build(dest)))
            return 0
        dest = args.dest or Path(tempfile.mkdtemp(prefix="public-tree-")) / "flyte-agent-factory"
        return dry_run(dest)
    except (FileExistsError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"public_tree: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
