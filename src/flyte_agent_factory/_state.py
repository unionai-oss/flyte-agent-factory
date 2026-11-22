"""Project state from native primitives: `Dir` inputs/outputs and `Checkpoint`.

Two different lifetimes, two different primitives:

- **Across attempts** (a retry after an eviction, same run) — `flyte.Checkpoint`.
  That is exactly what it is for: `save()` takes a directory and tars and
  uploads it itself, `load()` hands back the previous attempt's copy. The SDK
  owns the archiving, so there is none here.
- **Across runs** (resuming a project days later) — an ordinary `Dir` task
  input and output. The run returns its workspace; the next run takes it. The
  handle is typed data flowing between tasks, not a convention about bucket
  paths.

This replaced a Union `Volume`, which is the nicer design -- a real mounted
filesystem, nothing to archive -- but it mounts via the `smarter-devices/fuse`
device, and on this cluster all 13 fuse-capable nodes are at capacity with no
headroom to scale, so those pods never schedule. `Dir` and `Checkpoint` need
nothing from the node and schedule anywhere. If fuse capacity frees up, or the
brokered CSI mount path (`allow_volumes`, which needs no device) becomes
available, a volume is the better home for this.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import errno
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _secret_env, _workspace_archive

if TYPE_CHECKING:
    from flyte import Checkpoint
    from flyte.io import Dir

# Laid out under one root so a single checkpoint captures a consistent pair:
# the workspace and the conversation that produced it.
WORKSPACE_SUBDIR = "workspace"
CLAUDE_SUBDIR = "claude"


@dataclass
class ProjectState:
    root: Path
    resumed_from: str

    @property
    def workspace(self) -> Path:
        return self.root / WORKSPACE_SUBDIR

    @property
    def config_dir(self) -> Path:
        return self.root / CLAUDE_SUBDIR

    @property
    def resumed(self) -> bool:
        return bool(self.resumed_from)


def _checkpoint() -> Checkpoint | None:
    """Return the current action's checkpoint, or None outside a task context."""
    import flyte

    ctx = flyte.ctx()
    paths = getattr(ctx, "checkpoint_paths", None) if ctx else None
    if paths is None or not paths.checkpoint_path:
        return None
    return flyte.Checkpoint(
        checkpoint_dest=paths.checkpoint_path,
        checkpoint_src=paths.prev_checkpoint_path,
    )


def checkpoint_location() -> str:
    """Return where this action saves its checkpoint, or "" outside a task.

    The factory reads it to resume a run that was aborted: Flyte does not list a run's checkpoints.
    """
    ckpt = _checkpoint()
    return ckpt.remote_destination if ckpt is not None else ""


async def restore(*, root: Path, workspace_in: Dir | None = None) -> ProjectState:
    """Lay out the project's directories, newest surviving copy first.

    A checkpoint beats the input `Dir`: the checkpoint is from an earlier
    attempt of *this* run and therefore newer than whatever a previous run
    handed over, so restoring the input over it would discard work.
    """
    root.mkdir(parents=True, exist_ok=True)

    ckpt = _checkpoint()
    if ckpt is not None and ckpt.prev_exists():
        previous = await ckpt.load()
        if previous is not None:
            # `load` returns the unpacked tree; move its contents into place.
            for item in Path(previous).iterdir():
                target = root / item.name
                if target.exists():
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
                shutil.move(str(item), str(target))
            _ensure_layout(root)
            return ProjectState(root=root, resumed_from="checkpoint")

    resumed_from = ""
    if workspace_in is not None:
        workspace = root / WORKSPACE_SUBDIR
        workspace.mkdir(parents=True, exist_ok=True)
        await _workspace_archive.download(workspace_in, workspace)
        resumed_from = getattr(workspace_in, "path", "") or "workspace_in"

    _ensure_layout(root)
    return ProjectState(root=root, resumed_from=resumed_from)


def _ensure_layout(root: Path) -> None:
    (root / WORKSPACE_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / CLAUDE_SUBDIR).mkdir(parents=True, exist_ok=True)


_CHECKPOINT_GIT_TIMEOUT_S = 10.0
# `fsck --connectivity-only` reads only commits and trees: 0.6 s on a repository of 48k objects.
_CHECKPOINT_FSCK_TIMEOUT_S = 60.0


async def _git_in_checkpoint(cwd: Path, *args: str, timeout: float) -> tuple[int | None, bytes]:
    """Run git for a checkpoint and return (exit status, stdout); None when it did not start or finish."""
    # Not the supervisor's `_git_out`: importing it would create a cycle, and its teardown can
    # wait for inherited pipes, which must not hold up a checkpoint.
    try:
        proc = await asyncio.create_subprocess_exec(
            *_secret_env.git_argv(*args),
            cwd=cwd,
            env=_secret_env.child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("checkpoint git %s unavailable: %s", args[0], exc)
        return None, b""
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]  # asyncio has no public API for closing subprocess pipes.
        if pipe is not None:
            pipe.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1)
        if isinstance(exc, asyncio.CancelledError):
            raise
        logging.getLogger(__name__).warning("checkpoint git %s timed out", args[0])
        return None, b""
    return proc.returncode, output


async def _list_checkpoint_files(workspace: Path, *args: str) -> bytes | None:
    status, output = await _git_in_checkpoint(
        workspace, "ls-files", *args, "-z", timeout=_CHECKPOINT_GIT_TIMEOUT_S
    )
    if status != 0:
        logging.getLogger(__name__).warning("checkpoint git listing failed; copying the file tree")
        return None
    return output


async def _repository_is_intact(workspace: Path) -> bool:
    """Return False only when git ran and found the repository broken; a git that cannot run says nothing."""
    status, _ = await _git_in_checkpoint(
        workspace, "fsck", "--connectivity-only", "--no-dangling", timeout=_CHECKPOINT_FSCK_TIMEOUT_S
    )
    return status is None or status == 0


@dataclass
class CheckpointHealth:
    """How this pod's checkpoints went: the last good one, and the last one that failed and where.

    The timeline only scrolls; the status report carries this so a reader can tell how old the newest
    checkpoint is, and why the one after it was not written. None until one is taken in this pod.
    """

    last_ok_at: float | None = None
    last_ok_kind: str = ""
    #: {at, kind, step, elapsed_s, error} of the last failure, cleared by the next success.
    failure: dict[str, Any] | None = None

    def report(self, now: float | None = None) -> dict[str, Any]:
        """Return the health as the plain dict the status report carries."""
        now = time.time() if now is None else now
        return {
            "last_ok_at": self.last_ok_at,
            "last_ok_age_s": None if self.last_ok_at is None else max(0, int(now - self.last_ok_at)),
            "last_ok_kind": self.last_ok_kind,
            "last_failure": dict(self.failure) if self.failure else None,
        }

    def describe_failure(self, exc: BaseException, since: float) -> str:
        """Return `exc` for a timeline row, with the step and time of a checkpoint failed after `since`."""
        text = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        f = self.failure
        if f and f["at"] >= since:
            text += f" while {f['step']} (after {f['elapsed_s']:.0f}s)"
        return text


health = CheckpointHealth()


@dataclass
class _Attempt:
    """One checkpoint in flight; overlapping ones (limit and periodic) each keep their own step."""

    kind: str
    started: float  # monotonic
    step: str = ""  # so one cut off by its bound says where
    saved: bool = False  # uploaded: what fails after this (removing the staging copy) loses nothing


class _CopyStopped(Exception):
    """The checkpoint that wanted this copy was cancelled; stop writing into its staging directory."""


class CheckpointInconsistent(OSError):
    """The staged repository is broken although the project's own is not, even after copying it again."""


def _vanished(row: tuple[str, str, str]) -> bool:
    """Return whether a `shutil.Error` row's source was gone when it was read (ENOENT on that path)."""
    source, _, why = row
    return why.startswith(f"[Errno {errno.ENOENT}]") and repr(source) in why


def _stage(
    src: Path, dst: Path, ignore: Callable[[str, list[str]], list[str]], stop: threading.Event
) -> list[str]:
    """Copy the project into the staging directory, stopping at the next file once `stop` is set.

    The tree is live: a file or directory deleted after it was listed (`git gc`, a subagent's scratch)
    is left out rather than failing the checkpoint, and returned. It counts as deleted when reading it
    failed with "no such file" on that very path, whatever is there by now: a `.git/objects/xx` that git
    removes and recreates is still a directory the copy could not read. Any other copy error fails it.
    """
    vanished: list[str] = []

    def copy(source: str, target: str, *, follow_symlinks: bool = True) -> object:
        if stop.is_set():
            raise _CopyStopped
        try:
            return shutil.copy2(source, target, follow_symlinks=follow_symlinks)
        except FileNotFoundError as exc:
            if exc.filename != source:
                raise
            vanished.append(source)
            return target

    def ignore_or_stop(directory: str, names: list[str]) -> list[str]:
        if stop.is_set():
            raise _CopyStopped
        return ignore(directory, names)

    try:
        shutil.copytree(src, dst, ignore=ignore_or_stop, copy_function=copy, symlinks=True)
    except shutil.Error as exc:
        # Each row is (source, target, why).
        rows = [row for row in exc.args[0] if not _vanished(row)]
        if rows:
            raise shutil.Error(rows) from None
        vanished.extend(row[0] for row in exc.args[0])
    if vanished:
        in_git = sum(f"{os.sep}.git{os.sep}" in path for path in vanished)
        logging.getLogger(__name__).warning(
            "checkpoint: %d paths deleted during the copy were left out (%d under .git)",
            len(vanished),
            in_git,
        )
    return vanished


def _restage(
    src: Path, dst: Path, ignore: Callable[[str, list[str]], list[str]], stop: threading.Event
) -> None:
    shutil.rmtree(dst, ignore_errors=True)
    _stage(src, dst, ignore, stop)


def _remove_staging(directory: str) -> None:
    # Runs on the copy's own single worker, so only after the copy has stopped: removing it while the
    # copy still writes is what failed with "Directory not empty" (#45).
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        logging.getLogger(__name__).warning("checkpoint staging directory not removed: %s", exc)


async def save_checkpoint(
    state: ProjectState,
    *,
    exclude_private: bool = False,
    kind: str = "periodic",
    timeout: float | None = None,
) -> bool:
    """Record the project so far, so a retry resumes instead of restarting.

    `kind` names it in `health`: "periodic" (the periodic and final ones), "limit" or "handoff".
    `timeout` bounds listing, copying, checking and uploading; once the upload is done, removing the
    staging copy is not bounded, so a checkpoint that was saved is never reported lost. A failure,
    including running out of that bound (`TimeoutError`) or a cancel, is recorded in `health` with the
    step it was in, then raised.
    """
    ckpt = _checkpoint()
    if ckpt is None:
        return False
    attempt = _Attempt(kind, time.monotonic())
    bound = asyncio.timeout(timeout)
    try:
        async with bound:

            def saved() -> None:
                bound.reschedule(None)
                health.last_ok_at, health.last_ok_kind, health.failure = time.time(), kind, None
                attempt.saved, attempt.step = True, "removing the staging copy"

            await _save(ckpt, state, exclude_private, attempt, saved)
    except BaseException as exc:
        if not attempt.saved:
            if isinstance(exc, TimeoutError) and bound.expired():
                why = f"timed out: its {timeout:g}s bound ran out"
            elif isinstance(exc, asyncio.CancelledError):
                why = "cancelled: its time bound ran out, or the pod is stopping"
            else:
                why = f"{type(exc).__name__}: {exc}"
            health.failure = {
                "at": time.time(),
                "kind": kind,
                "step": attempt.step,
                "elapsed_s": round(time.monotonic() - attempt.started, 1),
                "error": why[:300],
            }
        raise
    return True


async def _save(
    ckpt: Checkpoint, state: ProjectState, exclude_private: bool, attempt: _Attempt, saved: Callable[[], None]
) -> None:
    # `save` tars and uploads a directory itself.
    if not exclude_private and not (state.root / "author-conversation.json").is_file():
        attempt.step = "uploading"
        await ckpt.save(state.root)
        saved()
        return
    attempt.step = "listing files"
    ignored = set()
    if (state.workspace / ".git").exists():
        output = await _list_checkpoint_files(state.workspace, "--others", "--ignored", "--exclude-standard")
        if output is not None:
            ignored = {state.workspace / name for name in output.decode().split("\0") if name}

    ignored_dirs = set()
    for file in ignored:
        for parent in file.parents:
            if parent == state.workspace:
                break
            ignored_dirs.add(parent)
    if ignored_dirs:
        output = await _list_checkpoint_files(state.workspace, "--cached", "--others", "--exclude-standard")
        if output is None:
            ignored.clear()
            ignored_dirs.clear()
        else:
            for name in output.decode().split("\0"):
                if name:
                    ignored_dirs.difference_update((state.workspace / name).parents)
    # Once, not per name: a `.venv` lists tens of thousands of ignored files.
    skipped = ignored | ignored_dirs

    def exclude(directory: str, names: list[str]) -> list[str]:
        parent = Path(directory)
        return [
            name
            for name in names
            if parent / name in skipped
            and ".claude-flyte" not in (parent / name).relative_to(state.root).parts
        ]

    # The SDK archives whole directories. Stage a filtered copy rather than moving live files out
    # from under the credential refresher, or archiving ignored training datasets.
    attempt.step = "staging a copy"
    directory = tempfile.mkdtemp(prefix="author-checkpoint-")
    snapshot = Path(directory) / "state"
    stop = threading.Event()
    # Not the loop's shared executor: a cancel while the copy is still queued there drops the job, and
    # nothing would ever run the removal. One worker keeps the removal behind the copy, in order.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint-copy")
    loop = asyncio.get_running_loop()
    try:
        # The thread outlives a cancelled await, so the copy is told to stop and the directory is removed
        # after it (same worker, in order), whatever ends this block.
        await loop.run_in_executor(pool, _stage, state.root, snapshot, exclude, stop)
        staged = snapshot / state.workspace.relative_to(state.root)
        if (staged / ".git" / "HEAD").is_file():
            # A `git gc` during the copy can leave a staged repository missing objects it needs; a
            # file it removed is left out above, so check the copy rather than trusting it.
            attempt.step = "checking the staged repository"
            if not await _repository_is_intact(staged):
                logging.getLogger(__name__).warning(
                    "checkpoint: staged repository broken; copying .git again"
                )
                await loop.run_in_executor(
                    pool, _restage, state.workspace / ".git", staged / ".git", exclude, stop
                )
                if not await _repository_is_intact(staged):
                    if await _repository_is_intact(state.workspace):
                        raise CheckpointInconsistent(
                            "the staged repository fails `git fsck --connectivity-only` after a recopy"
                        )
                    # The project's own repository is broken too: the checkpoint is a faithful copy of it.
                    logging.getLogger(__name__).warning("checkpoint: the project's repository fails git fsck")
        attempt.step = "uploading"
        await ckpt.save(snapshot)
        saved()
    finally:
        stop.set()
        try:
            # Shielded: a second cancel must not skip the removal; the worker finishes it regardless.
            await asyncio.shield(loop.run_in_executor(pool, _remove_staging, directory))
        finally:
            pool.shutdown(wait=False)


async def publish(state: ProjectState) -> Dir:
    """Hand the workspace back as a `Dir`, for a later run to resume from.

    It is one tar inside the `Dir`: object storage drops file modes, and a resumed `.venv` needs its own.
    """
    return await _workspace_archive.upload(state.workspace)
