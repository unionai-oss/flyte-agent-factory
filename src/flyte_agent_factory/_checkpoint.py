"""Workspace persistence and handoff: the checkpoint archive, its timer, and the slot's last act."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import signal
import tarfile
import tempfile
from pathlib import Path

from flyte.io import Dir

from flyte_agent_factory import (
    _auth,
    _inbox,
    _policy,
    _queue,
    _secret_env,
    _session_config,
    _state,
    _timeline,
)
from flyte_agent_factory._delegation import Emit
from flyte_agent_factory._errors import REMOTE_ERRORS

_WORKSPACE_ARCHIVE = "agent-factory-workspace.tar"
#: What an engine from before the rename wrote; still restored, for a workspace uploaded by one still running.
_LEGACY_WORKSPACE_ARCHIVE = "claude-flyte-workspace.tar"


async def _upload_workspace(workspace: Path) -> Dir:
    """Upload the workspace to a sub-agent, minus the supervisor's browser config."""

    def pack(target: Path) -> None:
        def skip_browser_config(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
            # Nothing a sub-agent receives should name the personal browser's endpoint.
            return None if member.name == "./.mcp.json" else member

        # Object storage carries file bytes, not POSIX modes. Git's index alone cannot
        # restore executable bits on uncommitted files or preserve mode-only changes.
        with tarfile.open(target / _WORKSPACE_ARCHIVE, "w") as archive:
            archive.add(workspace, arcname=".", filter=skip_browser_config)

    with tempfile.TemporaryDirectory() as staging:
        staged = Path(staging)
        await asyncio.to_thread(pack, staged)
        return await Dir.from_local(str(staged))


async def _download_workspace(workdir: Dir, workspace: Path) -> None:
    await workdir.download(str(workspace))
    archive_path = next(
        (p for p in (workspace / _WORKSPACE_ARCHIVE, workspace / _LEGACY_WORKSPACE_ARCHIVE) if p.is_file()),
        None,
    )
    # Direct callers and delegation jobs can still supply an ordinary directory.
    if archive_path is not None:

        def unpack() -> None:
            with tarfile.open(archive_path, "r:") as archive:
                # "tar", not "data": a workspace .venv links to the image's interpreter by absolute
                # path, which "data" refuses. "tar" still keeps members inside the workspace.
                archive.extractall(workspace, filter="tar")
            archive_path.unlink()

        await asyncio.to_thread(unpack)


async def _checkpoint_loop(
    *,
    state: _state.ProjectState,
    every: float,
    timeline: _timeline._Timeline,
    stop: asyncio.Event,
) -> None:
    """Record an immutable version of the volume periodically.

    `commit` does not unmount, so the session keeps writing through it. This
    replaces tarring the workspace into a bucket: there is nothing to pack, and
    each commit is a version the project can be forked from later.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            return
        except asyncio.TimeoutError:
            pass

        try:
            if await _state.save_checkpoint(state):
                timeline.row("💾", "checkpointed")
            else:
                timeline.row("·", "no checkpoint configured for this action")
        except REMOTE_ERRORS as e:  # a failed commit must not end the project
            timeline.row("⚠️", f"commit failed: {type(e).__name__}: {e}")
        await timeline.flush()


_PARK_RELEASE_DEFAULT_MIN = 60


def _park_release_s(emit: Emit | None = None) -> float:
    """Seconds a parked session (`MAX_NUDGES` unanswered) keeps its slot before it is released.

    A parked session held a slot for 15 hours until its owner stopped it. `AGENT_FACTORY_PARK_RELEASE_MIN`
    (the policy env, so per slot or per profile) sets the minutes; 0 keeps it parked for ever, and the
    callers read it so. A profile cannot set anything else (`_profile` validates it), but a hand-set env
    can: non-numeric, non-finite or negative values fall back to the default, reported through `emit`.
    """
    raw = _policy.read("PARK_RELEASE_MIN", str(_PARK_RELEASE_DEFAULT_MIN))
    try:
        minutes = float(raw)
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError("must be a finite number of minutes, 0 or more")
    except ValueError:
        # Not 0 and not inf: either would hold the slot for ever, which is what this setting prevents.
        if emit is not None:
            emit("⚠️", f"AGENT_FACTORY_PARK_RELEASE_MIN={raw} invalid; using {_PARK_RELEASE_DEFAULT_MIN}")
        return _PARK_RELEASE_DEFAULT_MIN * 60.0
    return minutes * 60


async def _git_head(workspace: Path) -> str | None:
    """Return the pushed commit, or None when there is nothing tracking upstream."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *_secret_env.git_argv("rev-parse", "--short", "HEAD"),
            cwd=str(workspace),
            env=_secret_env.child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return None
    return out.decode().strip() or None if proc.returncode == 0 else None


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill the process group too: children such as ssh can outlive their parent."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _git_out(workspace: Path, *args: str, timeout: float | None = None) -> tuple[int, str]:
    """Run git; (returncode, stdout). Never prompts, and a timeout kills the process.

    A cancelled `communicate()` alone leaves git (and its ssh) running, re-parented
    to init: one stuck pair per review round whenever the remote hangs.
    """
    spawn = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *_secret_env.git_argv(*args),
            cwd=str(workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                **_secret_env.session_env(dict(os.environ), _session_config._PROFILE),
                "GIT_TERMINAL_PROMPT": "0",
            },
            start_new_session=True,  # its own process group, so `_reap` can take ssh too
        )
    )
    try:
        # Shielded: git is running before the spawn hands it back, so a cancel in that window must still get
        # the process to kill rather than leak it (and its ssh) to init.
        proc = await asyncio.shield(spawn)
    except OSError:
        return 1, ""
    except asyncio.CancelledError:
        with contextlib.suppress(OSError):
            await _reap(await spawn)
        raise
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _reap(proc)
        return 124, ""
    except asyncio.CancelledError:
        await _reap(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace").strip()


_HANDOFF_GIT_TIMEOUT_S = 10.0


async def _handoff_git_out(workspace: Path, *args: str) -> str:
    # _git_out waits for pipe EOF during teardown. Handoff metadata must also stay bounded
    # when a detached tool holds an inherited pipe.
    try:
        proc = await asyncio.create_subprocess_exec(
            *_secret_env.git_argv(*args),
            cwd=str(workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                **_secret_env.session_env(dict(os.environ), _session_config._PROFILE),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
            start_new_session=True,
        )
    except OSError:
        return "Unavailable (git could not start)."
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_HANDOFF_GIT_TIMEOUT_S)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # typeshed does not declare the transport; asyncio sets it on every subprocess
        pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]  # see above: not in typeshed
        if pipe is not None:
            pipe.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return "Unavailable (git timed out)."
    if proc.returncode:
        return f"Unavailable (git exited {proc.returncode})."
    return out.decode(errors="replace").strip()


def _ended_path() -> Path:
    return _session_config.STATE_ROOT / "ended.json"


async def _note_checkpoint_location(timeline: _timeline._Timeline, run_name: str) -> None:
    """Tell the factory where this run's checkpoint is and which API key it runs with.

    The factory recreates a run aborted from inside it -- by this run's own key, which it can only
    tell by the client id noted here -- and resumes it from that checkpoint. Best effort: without the
    note such an abort is final, as it was before, so a failure here only shows on the timeline.
    """
    location, app = _state.checkpoint_location(), _auth.own_client_id()
    if not ((location or app) and _session_config._PROFILE and _session_config._PROFILE.state.uri):
        return
    try:
        await asyncio.wait_for(
            _inbox._in_daemon_thread(
                _queue.note_checkpoint, _session_config._PROFILE.state.uri, run_name, location, app
            ),
            timeout=30,
        )
    except (asyncio.TimeoutError, *REMOTE_ERRORS) as e:
        timeline.row("⚠️", f"could not record where this run's checkpoint is: {type(e).__name__}: {e}")


#: The last act must end: a hung dispatch or queue read would otherwise hold the run (or the
#: dashboard loop) open after its work is published.
_HAND_OVER_DISPATCH_TIMEOUT_S = 120.0
#: Seconds the completion notification may take before the hand-over moves on without it.
_NOTIFY_TIMEOUT_S = 15.0


_QUEUE_READ_TIMEOUT_S = 60.0


async def _hand_over_slot(timeline: _timeline._Timeline, run_name: str, *, completion_key: str = "") -> None:
    """Return the project's last act: start the next queued project on this slot.

    Nothing here may fail the run -- its work is already published -- and the queue sweep is the
    backstop for a dispatch that did not happen.
    """
    if _session_config._PROFILE and (
        _session_config._PROFILE.planner.admission == "planner"
        or _session_config._PROFILE.board
        or _session_config._PROFILE.github.configured
    ):
        from . import _planner_runtime

        try:
            # A daemon thread, not `to_thread`: a stalled notification must not outlive the timeout
            # as an executor thread, which `asyncio.run` would wait for and so hold this slot open.
            await asyncio.wait_for(
                _inbox._in_daemon_thread(
                    _planner_runtime.notify, _session_config._PROFILE, completion_key, run_name
                ),
                timeout=_NOTIFY_TIMEOUT_S,
            )
        except REMOTE_ERRORS:
            timeline.row("⚠️", "completion wake failed; the safety-net timer will scan again")
    if _session_config._PROFILE and _session_config._PROFILE.planner.admission == "planner":
        return
    if not (_session_config._PROFILE and _session_config._PROFILE.state.uri):
        return  # no launch queue in this deployment
    try:
        # Lazily: `_factory` needs a profile at import. `exclude_run` because this run is still
        # RUNNING, and would otherwise make its own slot look busy.
        from flyte_agent_factory import _factory

        started = await asyncio.wait_for(
            _inbox._in_daemon_thread(_factory.dispatch, exclude_run=run_name),
            timeout=_HAND_OVER_DISPATCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        timeline.row(
            "⚠️",
            f"could not start the next queued project: dispatch took over "
            f"{_HAND_OVER_DISPATCH_TIMEOUT_S:.0f}s",
        )
        return
    except (
        *REMOTE_ERRORS,
        ImportError,
    ) as e:  # dispatch runs the whole queue sweep; the sweep is the backstop
        timeline.row("⚠️", f"could not start the next queued project: {type(e).__name__}: {e}")
        return
    for started_run in started:
        timeline.row("▶️", f"started the next queued project: {started_run}")
