"""File-based implementation jobs owned by the project supervisor."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, TypedDict, cast

import flyte

from . import _policy, _secret_env, child
from ._bridge import ANSWERS, REQUESTS, _write_atomic, redact_tokens
from ._bridge import PARTIAL_PATCH as PARTIAL_PATCH  # re-exported: workers and agent read it here
from ._bridge import PLAN as PLAN
from ._errors import REMOTE_ERRORS
from ._router import RouterDeferred

Emit = Callable[[str, str], None]


#: What running a worker can raise: remote calls and their downloads, the git and tar of a snapshot.
#: Handled where the job runs, as a failed job; anything else is a bug, which `Delegations.watch` reports.
_WORKER_ERRORS = (*REMOTE_ERRORS, subprocess.SubprocessError, tarfile.TarError)


class DelegateRequest(TypedDict, total=False):
    """A request file as the coordinator writes it; `scan` checks `id`, `brief` and `answer_only`."""

    id: str
    brief: str
    answer_only: bool


class ActiveJob(TypedDict):
    """A running job, for the dashboard."""

    id: str
    engine: str
    setting: str
    started_at: float


class EndedJob(ActiveJob):
    ok: bool
    ended_at: float
    elapsed_s: float


class Worker(Protocol):
    """Runs one job in `target` and returns its summary; `on_engine` reports the engine that really ran."""

    def __call__(
        self,
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> Awaitable[str]: ...


class Reattach(Protocol):
    """Waits for a submitted worker action, downloads its output to `target/result`, returns its summary.

    Raises if the action failed or was aborted.
    """

    def __call__(self, target: Path, run_name: str, name: str) -> Awaitable[str]: ...


def _run_name() -> str:
    ctx = flyte.ctx()
    return (ctx.action.run_name or "") if ctx and ctx.action else ""


#: Seconds a git call on a worker's tree may take. A tree is a repo copy plus whatever the worker added,
#: so minutes is generous; the bound is for a call that hangs, which would stall the job for good.
WORK_DIFF_TIMEOUT_S = 300
#: Seconds a git call on the author's workspace may take (`add -A` of a large tree is the slow one).
GIT_TIMEOUT_S = 300


def work_diff(repo: Path, base: str) -> bytes:
    """Everything changed in `repo` since `base`; `add -N` makes untracked files show in `git diff`.

    Disarms `repo/.git` first (`_secret_env.disarm_git_dir`), because the worker wrote it: give this
    only a throwaway copy, never a repo someone still uses. Raises ValueError for a `.git` that is not
    a plain directory, and the usual subprocess errors for a failed or timed-out git call.
    """
    _secret_env.disarm_git_dir(repo)
    env = _secret_env.child_env()
    subprocess.run(
        _secret_env.git_argv("-C", str(repo), "add", "-N", "."),
        env=env,
        check=True,
        capture_output=True,
        timeout=WORK_DIFF_TIMEOUT_S,
    )
    return subprocess.run(
        _secret_env.git_argv(
            "-C",
            str(repo),
            "diff",
            "--binary",
            # Belt and braces with the walk in `disarm_git_dir`: never spawn a child git for a gitlink,
            # even one the walk cannot reach (a gitlink already in `base`).
            "--ignore-submodules=all",
            *_secret_env.GIT_NO_DRIVERS,
            base,
        ),
        env=env,
        check=True,
        capture_output=True,
        timeout=WORK_DIFF_TIMEOUT_S,
    ).stdout


def usable_repo(workspace: Path) -> bool:
    if not (workspace / ".git").exists():
        return False
    return (
        subprocess.run(
            _secret_env.git_argv("-C", str(workspace), "rev-parse", "--verify", "HEAD"),
            env=_secret_env.child_env(),
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
        ).returncode
        == 0
    )


def delegate_limit(emit: Emit) -> int:
    try:
        value = int(_policy.read("DELEGATE_MAX", "3"))
        if value < 1:
            raise ValueError("must be positive")
        return value
    except ValueError as exc:
        emit("⚠️", f"AGENT_FACTORY_DELEGATE_MAX misconfigured: {exc}; using 3")
        return 3


def snapshot(workspace: Path, target: Path) -> str:
    def git(root: Path, *args: str, input: bytes | None = None) -> bytes:
        # The copied `.git` is the session's too, config and hooks included.
        result = subprocess.run(
            _secret_env.git_argv("-C", str(root), *args),
            input=input,
            env=_secret_env.child_env(),
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        stdout: bytes = result.stdout
        return stdout

    repo = target / "repo"
    repo.mkdir()
    # Copy only Git's view of the worktree; copying the whole directory would ship
    # ignored datasets and credentials. Keep history so three-way apply has a base.
    git_dir = Path(os.fsdecode(git(workspace, "rev-parse", "--absolute-git-dir")).strip())
    common_dir = Path(os.fsdecode(git(workspace, "rev-parse", "--git-common-dir")).strip())
    if not common_dir.is_absolute():
        common_dir = workspace / common_dir
    shutil.copytree(common_dir, repo / ".git", ignore=shutil.ignore_patterns("*.lock", "worktrees"))
    if git_dir.resolve() != common_dir.resolve():
        for name in ("HEAD", "index"):
            shutil.copy2(git_dir / name, repo / ".git" / name)
    paths = git(workspace, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw in set(paths.split(b"\0")) - {b""}:
        relative = Path(os.fsdecode(raw))
        source = workspace / relative
        dest = repo / relative
        if source.is_symlink():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(os.readlink(source))
        elif source.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    git(repo, "add", "-A")
    if git(repo, "status", "--porcelain"):
        git(
            repo,
            "-c",
            "user.name=Factory",
            "-c",
            "user.email=factory@localhost",
            "commit",
            "-m",
            "Delegation base",
        )
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    # Dirty snapshot blobs must also exist in the author's object store for --3way.
    objects = git(
        repo, "rev-list", "--objects", base, "--not", git(workspace, "rev-parse", "HEAD").decode().strip()
    )
    packed = git(repo, "pack-objects", "--stdout", input=objects)
    git(workspace, "index-pack", "--stdin", input=packed)
    with tarfile.open(target / "repo.tar.gz", "w:gz") as archive:
        archive.add(repo, arcname="repo")
    shutil.rmtree(repo)
    return base


def _append_line(path: Path, line: str) -> None:
    # One short O_APPEND write is atomic, and this job's worker is the only writer.
    with open(path, "a") as f:
        f.write(line + "\n")


class Delegations:
    def __init__(
        self,
        root: Path,
        workspace: Path,
        worker: Worker,
        engine: Callable[[], str | Awaitable[str]],
        emit: Emit,
        max_jobs: int = 3,
        setting: Callable[[str], str] | None = None,
        reattach: Reattach | None = None,
    ):
        if max_jobs < 1:
            raise ValueError("AGENT_FACTORY_DELEGATE_MAX must be positive")
        self.root, self.workspace = root, workspace
        self.worker, self.engine, self.emit = worker, engine, emit
        self.reattach_worker = reattach
        #: Jobs whose worker action was still running when the last supervisor died; `resume` watches them.
        self.resuming: list[str] = []
        self.slots = asyncio.Semaphore(max_jobs)
        self.jobs = workspace / ".claude-flyte" / "delegations"
        self.jobs.mkdir(parents=True, exist_ok=True)
        #: `engine -> "model/effort"`, only to describe a job on the dashboard.
        self.setting = setting
        #: Jobs running now, by id, and the last few that ended; both for the dashboard.
        self.active: dict[str, ActiveJob] = {}
        self.recent: list[EndedJob] = []
        # A delegation is a job folder holding a brief. `fail` also makes folders, for requests
        # that were invalid or never reached `service`; those were never counted live either.
        self.total = sum(1 for job in self.jobs.iterdir() if (job / "brief.md").is_file())
        self.notices: dict[str, str] = {}
        self.drained: dict[str, str] = {}
        self.seen: set[str] = set()
        self.pending: set[asyncio.Task[None]] = set()
        # A notice is kept in its job folder until delivered, so a pod restart between a job
        # finishing and the session being idle does not leave the coordinator waiting forever.
        for notice in self.jobs.glob("*/notice.txt"):
            self.notices[notice.parent.name] = notice.read_text()
        for path in (root / REQUESTS).glob("delegate-*.json"):
            answer = root / ANSWERS / path.name
            try:
                state = json.loads(answer.read_text())["state"]
            except (OSError, ValueError, KeyError, TypeError):
                state = "running"
            job = self.jobs / path.stem
            completed_answer = (job / "answer-only").exists() and (job / "summary.md").exists()
            if state == "running":
                if completed_answer:
                    _write_atomic(answer, {"state": "done", "summary": (job / "summary.md").read_text()})
                elif self.can_reattach(job):
                    self.resuming.append(path.stem)
                else:
                    self.fail(path.stem, "supervisor restarted")
        for job in self.jobs.iterdir():
            # The patch is copied after the summary, so a summary alone is a job cut off mid-copy.
            if (
                job.is_dir()
                and job.name not in self.resuming
                and not (job / "factory.patch").exists()
                and not (job / PARTIAL_PATCH).exists()
                and not ((job / "answer-only").exists() and (job / "summary.md").exists())
                and not (job / "error.txt").exists()
            ):
                self.fail(job.name, "supervisor restarted before this job finished")

    def can_reattach(self, job: Path) -> bool:
        """Whether the job's worker action was submitted, so it may still be running without us."""
        return self.reattach_worker is not None and (job / "action.json").is_file()

    def queue_notice(self, job_id: str, text: str) -> None:
        self.notices[job_id] = text
        (self.jobs / job_id / "notice.txt").write_text(text)

    def drain_notices(self) -> list[str]:
        """Hand over the pending notices; `requeue_notices` undoes it if delivery fails."""
        self.drained, self.notices = self.notices, {}
        for job_id in self.drained:
            (self.jobs / job_id / "notice.txt").unlink(missing_ok=True)
        return list(self.drained.values())

    def requeue_notices(self) -> None:
        for job_id, text in self.drained.items():
            self.queue_notice(job_id, text)
        self.drained = {}

    def fail(self, job_id: str, reason: str, engine: str | None = None) -> None:
        job = self.jobs / job_id
        job.mkdir(exist_ok=True)
        summary = job / "summary.md"
        if summary.exists():
            reason += "\n" + summary.read_text()
        # Once, before any copy: the answer and the notice reach the session and the report too.
        reason = redact_tokens(reason)
        (job / "error.txt").write_text(reason)
        # Never ask the router here: a failure report is not a call, and asking would log a
        # routing decision (and take a slot in the decision log) for work that is not being run.
        engine = engine or "unknown"
        self.queue_notice(
            job_id,
            f"{job_id} ({engine}, failed: {' '.join(reason.split())[:300]}) "
            f"— `delegate wait {job_id}` for the summary and patch path.",
        )
        _write_atomic(self.root / ANSWERS / f"{job_id}.json", {"state": "failed", "error": reason})

    async def run(self) -> None:
        self.resume()
        try:
            while True:
                self.scan()
                await asyncio.sleep(1)
        finally:
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)

    def resume(self) -> None:
        """Watch the worker actions that a restart interrupted; `__init__` found them."""
        for job_id in self.resuming:
            self.watch(asyncio.create_task(self.reattach(job_id)), job_id)
        self.resuming = []

    def watch(self, task: asyncio.Task[None], job_id: str) -> None:
        """Keep a job's task until it ends; a bug in it fails the job loudly, never leaves it running."""
        self.pending.add(task)

        def settled(task: asyncio.Task[None]) -> None:
            self.pending.discard(task)
            if task.cancelled() or (exc := task.exception()) is None:
                return
            traceback.print_exception(exc)
            self.fail(job_id, f"{type(exc).__name__}: {exc}")
            self.emit("⚠️", f"delegate {job_id} crashed: {type(exc).__name__}: {exc}")

        task.add_done_callback(settled)

    def scan(self) -> None:
        for path in sorted((self.root / REQUESTS).glob("delegate-*.json")):
            if path.name in self.seen or (self.root / ANSWERS / path.name).exists():
                continue
            self.seen.add(path.name)
            try:
                request = json.loads(path.read_text())
                if not isinstance(request, dict) or request.get("id") != path.stem:
                    raise ValueError("request id must match filename")
                if not isinstance(request.get("answer_only", False), bool):
                    raise TypeError("answer_only must be a boolean")
                if not isinstance(request.get("brief"), str):
                    raise TypeError("request requires a brief")
            except (OSError, ValueError, TypeError) as exc:
                self.fail(path.stem, f"invalid delegation request: {exc}")
                self.emit("⚠️", f"delegate {path.stem}: {exc}")
                continue
            # `request` passed the checks above: an object with a matching `id`, a string `brief`.
            self.watch(asyncio.create_task(self.service(cast(DelegateRequest, request))), request["id"])

    async def service(self, request: DelegateRequest) -> None:
        job_id = request["id"]
        job = self.jobs / job_id
        job.mkdir(exist_ok=True)
        (job / "brief.md").write_text(request["brief"])
        self.total += 1
        answer_only = request.get("answer_only", False)
        if answer_only:
            (job / "answer-only").touch()
        async with self.slots:
            _write_atomic(self.root / ANSWERS / f"{job_id}.json", {"state": "running"})
            started = time.monotonic()
            try:
                chosen = self.engine()
                engine = await chosen if inspect.isawaitable(chosen) else chosen
            except RouterDeferred as exc:
                # Every engine is out of allowance. Say so and let the coordinator resubmit after the reset.
                self.fail(job_id, f"deferred: {exc}", "no engine")
                self.emit("⏸️", f"delegate {job_id} deferred: {exc}")
                return
            self.active[job_id] = {
                "id": job_id,
                "engine": engine,
                "setting": self.setting(engine) if self.setting else "",
                "started_at": time.time(),
            }
            self.emit("🔧", f"delegate {job_id} started ({engine})")

            def on_engine(actual_engine: str, setting: str) -> None:
                nonlocal engine
                engine = actual_engine
                self.active[job_id].update({"engine": engine, "setting": setting})

            ok = False
            try:
                with tempfile.TemporaryDirectory(prefix="delegate-") as tmp:
                    target = Path(tmp)
                    base = await asyncio.to_thread(snapshot, self.workspace, target)
                    (job / "base").write_text(base + "\n")
                    # The worker diffs against it if it runs out of turns.
                    (target / "base").write_text(base + "\n")
                    brief = (
                        "First run `tar xzf repo.tar.gz && cd repo`. Work inside repo.\n\n"
                        + request["brief"]
                        + "\n\nRun the repository's tests and lint if present. Never weaken a test. "
                        "Read DESIGN.md if present. Write ../factory.patch with "
                        f"`git add -N . && git diff --binary {base} > ../factory.patch`. "
                        "Finish with a short summary: files changed, commands run and results."
                    )
                    if answer_only:
                        brief = (
                            "First run `tar xzf repo.tar.gz && cd repo`. Work inside repo.\n\n"
                            + request["brief"]
                            + "\n\nThis is read-only research. Do not change repository files. "
                            "Your final message is the entire answer; no patch is required."
                        )

                    # The sub-agent is a child of the same root action as every other delegate, so the
                    # job folder is the only place that says which action is this one's.
                    def record_action(name: str) -> None:
                        _append_line(job / "actions.txt", name)
                        # What a restarted supervisor needs to find this worker again; the latest
                        # submission is the live one (a spot attempt that missed its queue bound is replaced).
                        _write_atomic(
                            job / "action.json", {"name": name, "run_name": _run_name(), "engine": engine}
                        )

                    with child.track_submissions(record_action):
                        summary = await (
                            self.worker(target, brief, engine, answer_only=True, on_engine=on_engine)
                            if answer_only
                            else self.worker(target, brief, engine, on_engine=on_engine)
                        )
                    ok = await asyncio.to_thread(
                        self.hand_back, job_id, target, summary, engine, answer_only, started
                    )
            except _WORKER_ERRORS as exc:
                self.fail(job_id, f"{type(exc).__name__}: {exc}", engine)
                self.emit(
                    "⚠️", f"delegate {job_id} ({engine}) failed after {time.monotonic() - started:.0f}s: {exc}"
                )
            finally:
                self.retire(job_id, ok, started)

    async def reattach(self, job_id: str) -> None:
        """Pick up a job whose worker action outlived the supervisor, and finish it as `service` would."""
        job = self.jobs / job_id
        assert self.reattach_worker is not None  # `can_reattach` only lets such a job in
        saved = json.loads((job / "action.json").read_text())
        engine = saved.get("engine", "unknown")
        answer_only = (job / "answer-only").exists()
        async with self.slots:
            started = time.monotonic()
            self.active[job_id] = {
                "id": job_id,
                "engine": engine,
                "setting": self.setting(engine) if self.setting and engine != "unknown" else "",
                "started_at": time.time(),
            }
            self.emit("🔧", f"delegate {job_id} reattached to {saved['name']} ({engine})")
            ok = False
            try:
                with tempfile.TemporaryDirectory(prefix="delegate-") as tmp:
                    target = Path(tmp)
                    summary = await self.reattach_worker(target, saved["run_name"], saved["name"])
                    ok = await asyncio.to_thread(
                        self.hand_back, job_id, target, summary, engine, answer_only, started
                    )
            except _WORKER_ERRORS as exc:
                self.fail(job_id, f"{type(exc).__name__}: {exc}", engine)
                self.emit(
                    "⚠️", f"delegate {job_id} ({engine}) failed after {time.monotonic() - started:.0f}s: {exc}"
                )
            finally:
                self.retire(job_id, ok, started)

    def retire(self, job_id: str, ok: bool, started: float) -> None:
        """Move a job that ended from the dashboard's running list to its recent one."""
        job_info = self.active.pop(job_id, None)
        if job_info:
            self.recent = [
                *self.recent[-4:],
                {
                    **job_info,
                    "ok": ok,
                    "ended_at": time.time(),
                    "elapsed_s": time.monotonic() - started,
                },
            ]

    def hand_back(
        self, job_id: str, target: Path, summary: str, engine: str, answer_only: bool, started: float
    ) -> bool:
        """Record a finished worker's outputs (`target/result`) for the coordinator; True unless partial."""
        job = self.jobs / job_id
        result_dir = target / "result"
        summary = redact_tokens(summary)  # the coordinator reads it, and the run report may quote it
        # A worker that ran out of turns leaves a partial patch; it wins over a
        # `factory.patch` the worker may have started, which was not finished either.
        partial = not answer_only and (result_dir / PARTIAL_PATCH).exists()
        if not answer_only and not partial and not (result_dir / "factory.patch").exists():
            # A worker that ended its turn early still did its work: take the patch from its tree, as
            # a turn-limited one does. No tree at all is a real failure, and `copy2` below says so.
            repo = result_dir / "repo"
            if repo.is_dir():
                diff = work_diff(repo, (job / "base").read_text().strip())
                (result_dir / "factory.patch").write_bytes(diff)
                summary += (
                    "\n\nThe worker wrote no factory.patch; this patch is the diff of its working tree."
                    if diff
                    else "\n\nThe worker wrote no factory.patch and left no changes."
                )
        # The summary is the completion marker for answer jobs, so a restart
        # must never mistake a partially written answer for a finished one.
        (job / "summary.tmp").write_text(summary)
        (job / "summary.tmp").replace(job / "summary.md")
        if partial:
            shutil.copy2(result_dir / PLAN, job / PLAN)
            shutil.copy2(result_dir / PARTIAL_PATCH, job / PARTIAL_PATCH)
        elif not answer_only:
            shutil.copy2(result_dir / "factory.patch", job / "factory.patch")
        patch = job / (PARTIAL_PATCH if partial else "factory.patch")
        if partial:
            result = {
                "state": "partial",
                "summary": summary,
                "partial_patch": str(patch),
                "plan": str(job / PLAN),
            }
        else:
            result = {
                "state": "done",
                "summary": summary,
                **({} if answer_only else {"patch": str(patch)}),
            }
        _write_atomic(self.root / ANSWERS / f"{job_id}.json", result)
        files = (
            0
            if answer_only
            else sum(line.startswith("diff --git ") for line in patch.read_text().splitlines())
        )
        if partial:
            status = f"partial: hit the turn limit, {files} files so far"
            pointer = "the summary, partial patch path and plan.md of what remains"
        else:
            status = "answer ready" if answer_only else f"ok: {files} files"
            pointer = "the summary and patch path"
        self.queue_notice(job_id, f"{job_id} ({engine}, {status}) — `delegate wait {job_id}` for {pointer}.")
        self.emit(
            "🟡" if partial else "✅",
            f"delegate {job_id} ({engine}) {'hit its turn limit' if partial else 'finished'} in "
            f"{time.monotonic() - started:.0f}s ({files} files)",
        )
        return not partial
