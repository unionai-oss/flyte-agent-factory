"""Publish every task's committed start to the profile's bench repository in one commit.

Prints `<task id> <git tree id>` per task. A task with an empty `start_tree` is the owner's to pin: copy
its printed id into `_bench_suite.py` after the first publish.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

#: Each task's start directory under tests/fixtures/bench/, by task id.
STARTS = {
    "bugfix-shortlinks": "shortlinks_start",
    "feature-expiry": "expiry_start",
    "refactor-logstats": "logstats_start",
    "docs-standards": "docskit_start",
    "vague": "shortlinks_start",
    "too-big": "too_big_start",
    "tf-bucket": "tfbucket_start",
    "ml-pipeline": "mlpipe_start",
}


def tree_id(seed: Path) -> str:
    """Hash the seed with git itself, using an isolated index and no generated files."""
    with tempfile.TemporaryDirectory(prefix="bench-seed-") as tmp:
        tree = Path(tmp)
        shutil.copytree(seed, tree / "seed", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run(["git", "init", "-q"], cwd=tree, check=True, capture_output=True)
        subprocess.run(["git", "add", "seed"], cwd=tree, check=True, capture_output=True)
        return subprocess.check_output(["git", "write-tree", "--prefix=seed/"], cwd=tree, text=True).strip()


def main() -> None:
    """Publish every task's start tree to the profile's bench repository in one commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    from flyte_agent_factory import _bench_suite, _profile

    profile = _profile.load(args.profile)
    if profile.bench is None:
        parser.error("the profile has no [bench] table")
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/bench"
    tasks = list(_bench_suite.TASKS.values())
    if missing := [task.id for task in tasks if task.id not in STARTS]:
        parser.error(f"no start directory in STARTS for {', '.join(missing)}")
    digests = {}
    for task in tasks:
        digests[task.id] = tree_id(fixtures / STARTS[task.id])
        if task.start_tree and digests[task.id] != task.start_tree:
            parser.error(f"{task.id} seed is tree {digests[task.id]}, not pinned tree {task.start_tree}")
    # clone handles an unborn HEAD and preserves the remote's default branch name.
    with tempfile.TemporaryDirectory(prefix="bench-seed-publish-") as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(
            ["git", "clone", "-q", profile.bench.repo, str(tree)],
            check=True,
            capture_output=True,
        )
        for task in tasks:
            destination = tree / task.workdir
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(
                fixtures / STARTS[task.id], destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
            subprocess.run(["git", "add", task.workdir], cwd=tree, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=tree, check=False)
        if changed.returncode:
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Factory benchmark",
                    "-c",
                    "user.email=benchmark@example.org",
                    "commit",
                    "-qm",
                    "Seed benchmark starts",
                ],
                cwd=tree,
                check=True,
            )
            ref = subprocess.check_output(["git", "symbolic-ref", "HEAD"], cwd=tree, text=True).strip()
            subprocess.run(["git", "push", profile.bench.repo, f"HEAD:{ref}"], cwd=tree, check=True)
    for task_id, digest in digests.items():
        print(task_id, digest)


if __name__ == "__main__":
    main()
