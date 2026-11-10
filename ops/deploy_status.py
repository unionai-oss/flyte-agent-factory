"""Which commit of main is each agent task actually running, and what is not deployed yet.

The loop stalled twice waiting on a deploy (the deployment's operations log, inbox/007): merged
fixes sat inert because nothing said, where a human would see it, that the
deployed code was behind. Task versions cannot answer that -- the same commit
deployed twice gets two versions -- so this identifies the deployed commit by
content: download the task's code bundle, hash its Python files, and find the
newest commit on main whose `src/flyte_agent_factory` has exactly those blobs.

    python ops/deploy_status.py            # all agent tasks, human-readable
    python ops/deploy_status.py --json     # for the supervisor / scripts

Every env is deployed automatically on merge (the `factory-deployer` task), so a non-empty
"behind" list is information: a deploy is pending or has failed (see the deployer's run).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flyte_agent_factory import _deploy, _profile
from flyte_agent_factory._errors import REMOTE_ERRORS

_profile.select_from_argv()

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "flyte_agent_factory"
PROFILE = _profile.require()
# Only the slots' project tasks: the retired self-agent envs are never listed as behind.
TASKS = PROFILE.tasks


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout


def _fetch(timeout: float = 60) -> str:
    """Fetch origin, bounded; "" on success, else why (the caller falls back to the local ref).

    Its own process group, so a hung ssh dies with git: `PYTHONPATH=src uv run python ops/deploy_work.py`
    calls this after every deploy and must not hang on it.
    """
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(REPO), "fetch", "-q", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            start_new_session=True,
        )
    except OSError as e:
        return f"git fetch could not start: {e}"
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return f"git fetch timed out after {timeout:.0f}s"
    return "" if rc == 0 else f"git fetch exited {rc}"


def bundle_blobs(extracted: Path) -> dict[str, str]:
    """`{relative path under the package: git blob sha}` for the bundle's .py files."""
    root = next((p for p in extracted.rglob(PACKAGE) if p.is_dir()), None)
    if root is None:
        return {}
    out = {}
    for path in sorted(root.rglob("*.py")):
        sha = subprocess.run(
            ["git", "hash-object", str(path)], check=True, capture_output=True, text=True
        ).stdout.strip()
        out[str(path.relative_to(root))] = sha
    return out


def commit_blobs(commit: str) -> dict[str, str]:
    """Return the same map for `src/flyte_agent_factory` at `commit`."""
    listing = _git("ls-tree", "-r", commit, f"src/{PACKAGE}")
    out = {}
    for line in listing.splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith(".py"):
            out[path.removeprefix(f"src/{PACKAGE}/")] = meta.split()[2]
    return out


def match_commit(blobs: dict[str, str], commits: list[str]) -> str | None:
    """Return the newest commit that has every one of the bundle's files, byte for byte."""
    newest, _ = match_range(blobs, commits)
    return newest


def match_range(blobs: dict[str, str], commits: list[str]) -> tuple[str | None, str | None]:
    """(newest, oldest) of the run of consecutive commits that carry the bundle's files.

    A subset match, because a bundle carries only the modules that were loaded at
    deploy time (`copy_style="loaded_modules"`). So the deploy is *somewhere* in
    that run: a commit inside it that changed a module the bundle lacks is
    indistinguishable from the deploy itself. `commits` is newest first.
    """
    if not blobs:
        return None, None
    newest = oldest = None
    for commit in commits:
        have = commit_blobs(commit)
        if all(have.get(path) == sha for path, sha in blobs.items()):
            newest = newest or commit
            oldest = commit
        elif newest is not None:
            break
    return newest, oldest


def behind(commit: str, head: str) -> list[str]:
    """Return commits on main after `commit` that change the deployed package, newest first."""
    out = _git("log", "--format=%h %s", f"{commit}..{head}", "--", f"src/{PACKAGE}")
    return [line for line in out.splitlines() if line.strip()]


def _deployed_bundle(task_name: str, workdir: Path) -> tuple[str, Path]:
    from flyte import remote, storage

    task = remote.Task.get(task_name, auto_version="latest").fetch()
    args = list(task.pb2.spec.task_template.container.args)
    if "--tgz" not in args:
        raise LookupError("not fast-registered (no code bundle), so its commit cannot be identified")
    tgz = args[args.index("--tgz") + 1]
    local = workdir / "bundle.tgz"
    got = storage.get(tgz, str(local))
    if asyncio.iscoroutine(got):
        asyncio.run(got)
    with tarfile.open(local) as tar:  # filter="data": no traversal, no links
        tar.extractall(workdir / "x", filter="data")
    return task.version, workdir / "x"


def status(task_name: str, head: str, commits: list[str]) -> dict[str, Any]:
    """Return which main commit `task_name` is deployed from and how many deploy-relevant commits it lags."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            version, extracted = _deployed_bundle(task_name, Path(tmp))
        except (
            *REMOTE_ERRORS,
            LookupError,
            tarfile.TarError,
        ) as e:  # one bad task must not hide the others
            return {"task": task_name, "error": f"{type(e).__name__}: {e}"[:300]}
        blobs = bundle_blobs(extracted)
    newest, oldest = match_range(blobs, commits)
    row: dict[str, Any] = {"task": task_name, "version": version, "commit": newest}
    if newest is None:
        row["note"] = "deployed code matches no commit on main (an unmerged branch or local edits)"
        return row
    # Counted from the *oldest* candidate, so nothing is under-reported: a change in
    # the run to a module the bundle lacks may or may not be deployed.
    assert oldest is not None  # match_range returns both ends or neither
    row["behind"] = behind(oldest, head)
    if oldest != newest:
        unsure = behind(oldest, newest)
        if unsure:
            row["oldest"] = oldest
            row["uncertain"] = unsure
    return row


def render(rows: list[dict[str, Any]]) -> str:
    """Return one line per task from `status` rows, or why the task could not be checked."""
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['task']}: could not check ({r['error']})")
        elif r.get("commit") is None:
            lines.append(f"{r['task']} {r['version'][:8]}: {r['note']}")
        elif not r["behind"]:
            lines.append(f"{r['task']} {r['version'][:8]}: up to date (main@{r['commit'][:7]})")
        elif r.get("uncertain"):
            lines.append(
                f"{r['task']} {r['version'][:8]}: deployed somewhere in "
                f"main@{r['oldest'][:7]}..{r['commit'][:7]}; "
                f"up to {len(r['behind'])} src change(s) behind ({len(r['uncertain'])} uncertain: they touch "
                f"modules the bundle does not carry):"
            )
            lines += [f"    {c}" for c in r["behind"]]
        else:
            lines.append(
                f"{r['task']} {r['version'][:8]}: main@{r['commit'][:7]}, BEHIND by {len(r['behind'])} "
                f"src change(s):"
            )
            lines += [f"    {c}" for c in r["behind"]]
    return "\n".join(lines)


def deployer_runs_24h(now: datetime | None = None) -> int:
    """Count deployer runs in the preceding day, including webhook and reconcile runs."""
    from flyte import remote

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=24)
    return sum(
        1
        for _ in remote.Run.listall(
            task_name=_deploy.DEPLOYER_TASK,
            created_at=remote.TimeFilter(after=cutoff),
            limit=1_000_000,
        )
    )


def deployed_pair() -> dict[str, Any]:
    """Return the recorded release, independently of bundle-content matches for individual tasks."""
    from flyte_agent_factory import _deployer

    release = asyncio.run(_deployer.deployed_pair(PROFILE))
    if release is None:
        return {"task": _deploy.DEPLOY_PAIR, "deployed_pair": None}
    run, (sha, direction_sha) = release
    return {
        "task": _deploy.DEPLOY_PAIR,
        "run": run.name,
        "deployed_pair": {"sha": sha, "direction_sha": direction_sha or "none"},
    }


def main(argv: list[str] | None = None) -> int:
    """Print how far each deployed task is behind `--ref`; exit status says whether all are current."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ref", default="origin/main")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import operate  # type: ignore[import-not-found]  # sibling script, found via the sys.path insert above

    operate._init()
    stale = _fetch()
    if stale:
        print(f"warning: {stale}; using the local {args.ref}, which may be stale", file=sys.stderr)
    head = _git("rev-parse", args.ref).strip()
    commits = _git("rev-list", head).split()
    rows = [status(t, head, commits) for t in TASKS]
    try:
        count = deployer_runs_24h()
        measurement = {"task": _deploy.DEPLOYER_TASK, "runs_24h": count}
        measured = f"{_deploy.DEPLOYER_TASK}: {count} runs in the last 24h (5-minute poll baseline: 288/day)"
    except REMOTE_ERRORS as exc:
        measurement = {"task": _deploy.DEPLOYER_TASK, "error": f"could not count runs: {type(exc).__name__}"}
        measured = f"{_deploy.DEPLOYER_TASK}: {measurement['error']}"
    try:
        release = deployed_pair()
        pair = release["deployed_pair"]
        released = f"{_deploy.DEPLOY_PAIR}: {pair if pair else 'unknown (no readable success)'}"
    except REMOTE_ERRORS as exc:
        release = {"task": _deploy.DEPLOY_PAIR, "error": f"could not read release: {type(exc).__name__}"}
        released = f"{_deploy.DEPLOY_PAIR}: {release['error']}"
    print(
        json.dumps([*rows, measurement, release], indent=2)
        if args.json
        else f"{render(rows)}\n{measured}\n{released}"
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
