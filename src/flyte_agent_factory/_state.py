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
import contextlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import _secret_env

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
        await workspace_in.download(str(workspace))
        resumed_from = getattr(workspace_in, "path", "") or "workspace_in"

    _ensure_layout(root)
    return ProjectState(root=root, resumed_from=resumed_from)


def _ensure_layout(root: Path) -> None:
    (root / WORKSPACE_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / CLAUDE_SUBDIR).mkdir(parents=True, exist_ok=True)


_CHECKPOINT_GIT_TIMEOUT_S = 10.0


async def _list_checkpoint_files(workspace: Path, *args: str) -> bytes | None:
    # Not the supervisor's `_git_out`: importing it would create a cycle, and its teardown can
    # wait for inherited pipes, which must not hold up a checkpoint.
    try:
        proc = await asyncio.create_subprocess_exec(
            *_secret_env.git_argv("ls-files", *args, "-z"),
            cwd=workspace,
            env=_secret_env.child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("checkpoint file listing unavailable: %s", exc)
        return None
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=_CHECKPOINT_GIT_TIMEOUT_S)
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
        logging.getLogger(__name__).warning("checkpoint git listing timed out; copying the file tree")
        return None
    if proc.returncode:
        logging.getLogger(__name__).warning("checkpoint git listing failed; copying the file tree")
        return None
    return output


async def save_checkpoint(state: ProjectState, *, exclude_private: bool = False) -> bool:
    """Record the project so far, so a retry resumes instead of restarting."""
    ckpt = _checkpoint()
    if ckpt is None:
        return False
    # `save` tars and uploads a directory itself.
    if not exclude_private and not (state.root / "author-conversation.json").is_file():
        await ckpt.save(state.root)
        return True
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

    def exclude(directory: str, names: list[str]) -> list[str]:
        parent = Path(directory)
        return [
            name
            for name in names
            if parent / name in ignored | ignored_dirs
            and ".claude-flyte" not in (parent / name).relative_to(state.root).parts
        ]

    # The SDK archives whole directories. Stage a filtered copy rather than moving live files out
    # from under the credential refresher, or archiving ignored training datasets.
    with tempfile.TemporaryDirectory(prefix="author-checkpoint-") as directory:
        snapshot = Path(directory) / "state"
        await asyncio.to_thread(shutil.copytree, state.root, snapshot, ignore=exclude, symlinks=True)
        await ckpt.save(snapshot)
    return True


async def publish(state: ProjectState) -> Dir:
    """Hand the workspace back as a `Dir`, for a later run to resume from."""
    from flyte.io import Dir

    return await Dir.from_local(str(state.workspace))
