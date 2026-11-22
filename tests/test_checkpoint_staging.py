"""A checkpoint stages a filtered copy of the project; a timeout or a busy tree must not break it (#45).

Live, a usage-limit checkpoint failed every time with `OSError: [Errno 39] Directory not empty` on its
staging directory: the caller's timeout cancelled the checkpoint while the copy was still running in a
worker thread, and the staging directory's cleanup raced the copy that kept writing into it.
"""

import asyncio
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _state


def _project(root: Path, *, dirs: int = 120, files: int = 8) -> _state.ProjectState:
    """A project with a conversation, so the checkpoint stages a filtered copy."""
    objects = root / "workspace" / ".git" / "objects"
    for d in range(dirs):
        folder = objects / f"{d:02x}"
        folder.mkdir(parents=True)
        for f in range(files):
            (folder / f"object-{f}").write_bytes(os.urandom(512))
    (root / "author-conversation.json").write_text(json.dumps({"engine": "claude"}))
    return _state.ProjectState(root, "")


@pytest.fixture
def staging(tmp_path, monkeypatch):
    """Where staging directories go, so a test can see what a checkpoint left behind."""
    where = tmp_path / "tmp"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where


@pytest.fixture
def slow_source(monkeypatch):
    """Scanning the project's own directories is slow, so a copy is still running when a timeout fires."""
    real = os.scandir
    sources: list[Path] = []

    def scandir(path="."):
        if not isinstance(path, int) and any(Path(os.fsdecode(path)).is_relative_to(s) for s in sources):
            time.sleep(0.01)
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return sources


async def test_a_checkpoint_cut_off_by_its_timeout_leaves_nothing_and_says_timeout(
    tmp_path, monkeypatch, staging, slow_source
):
    state = _project(tmp_path / "state")
    slow_source.append(state.root)
    uploaded = []

    async def save(path):
        uploaded.append(path)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    for _ in range(3):  # the race was not every time; three cut-offs in a row all have to be clean
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_state.save_checkpoint(state, exclude_private=True), timeout=0.15)
        # The copy stopped and its staging directory is gone by the time the checkpoint returns.
        assert list(staging.iterdir()) == []
    assert uploaded == []
    failure = _state.health.failure
    assert failure is not None and failure["step"] == "staging a copy"
    assert failure["error"].startswith("cancelled: its time bound ran out")
    assert failure["elapsed_s"] >= 0.1


async def test_a_checkpoint_survives_files_vanishing_during_the_copy(tmp_path, monkeypatch, staging):
    """`git gc` and subagents delete files during the copy; what is gone is left out of the checkpoint."""
    state = _project(tmp_path / "state", dirs=6, files=4)
    objects = state.workspace / ".git" / "objects"
    real = os.scandir
    vanished: list[Path] = []

    def scandir(path="."):
        if isinstance(path, int):  # `rmtree` scans by descriptor
            return real(path)
        entries = list(real(path))
        here = Path(os.fsdecode(path))
        if here.parent == objects and not vanished:
            # Listed, then removed before it is copied: a loose object packed away.
            gone = here / "object-0"
            gone.unlink()
            vanished.append(gone)
        if here == objects:
            # A whole directory removed after it was listed.
            doomed = objects / "05"
            for f in doomed.iterdir():
                f.unlink()
            doomed.rmdir()
        return contextlib.nullcontext(iter(entries))

    monkeypatch.setattr(os, "scandir", scandir)
    captured = tmp_path / "captured"

    async def save(path):
        os.replace(path, captured)
        path.mkdir()

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    assert await _state.save_checkpoint(state, exclude_private=True) is True
    assert vanished
    assert (captured / "workspace" / ".git" / "objects" / "00" / "object-1").is_file()
    assert (captured / "author-conversation.json").is_file()
    assert list(staging.iterdir()) == []


async def test_a_real_copy_error_still_fails_the_checkpoint(tmp_path, monkeypatch, staging):
    """Only a file that is gone is forgiven: one that exists but cannot be read fails the checkpoint."""
    state = _project(tmp_path / "state", dirs=1, files=1)
    (state.workspace / "unreadable").write_text("x")
    real_copy = shutil.copy2

    def copy2(source, target, *, follow_symlinks=True):
        if Path(source).name == "unreadable":
            raise PermissionError(13, "Permission denied", source)
        return real_copy(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(shutil, "copy2", copy2)

    async def save(path):
        raise AssertionError("a failed copy must not be uploaded")

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    with pytest.raises(OSError, match="unreadable"):
        await _state.save_checkpoint(state, exclude_private=True)
    assert list(staging.iterdir()) == []
    assert _state.health.failure is not None and _state.health.failure["step"] == "staging a copy"


async def test_a_checkpoint_cancelled_before_its_copy_starts_still_returns_and_cleans_up(
    tmp_path, monkeypatch, staging
):
    """The shared executor is busy (many call sites share it); the cancel lands while the copy is queued.

    The two sleeps are the busy executor and a copy that is still running when the cancel lands.
    """
    state = _project(tmp_path / "state", dirs=1, files=1)

    async def save(path):
        raise AssertionError("a cancelled checkpoint must not be uploaded")

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    monkeypatch.setattr(_state, "_stage", lambda *args: time.sleep(0.2))

    async def unlisted(*args):
        return None  # as `git ls-files` answers here (no HEAD), without a spawn racing the 0.05s bound

    monkeypatch.setattr(_state, "_list_checkpoint_files", unlisted)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
    busy = loop.run_in_executor(None, time.sleep, 0.3)
    with pytest.raises(TimeoutError):
        # Outer bound turns the old hang into a failure instead of a stuck test.
        await asyncio.wait_for(
            asyncio.wait_for(_state.save_checkpoint(state, exclude_private=True), timeout=0.05), timeout=2
        )
    await busy
    assert list(staging.iterdir()) == []
    assert _state.health.failure is not None and _state.health.failure["step"] == "staging a copy"


async def test_two_overlapping_checkpoints_each_record_their_own_step(tmp_path, monkeypatch, staging):
    """A limit checkpoint failing mid-copy while the periodic one is uploading names *its* step."""
    state = _project(tmp_path / "state", dirs=1, files=1)
    uploading, release = threading.Event(), asyncio.Event()
    real_stage, copies = _state._stage, []

    def stage(src, dst, ignore, stop):
        copies.append(src)
        if len(copies) == 1:  # the first copy is the limit checkpoint's
            uploading.wait(5)
            raise PermissionError(13, "Permission denied", "x")
        real_stage(src, dst, ignore, stop)

    async def save(path):
        uploading.set()
        await release.wait()

    monkeypatch.setattr(_state, "_stage", stage)
    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    limit = asyncio.create_task(_state.save_checkpoint(state, exclude_private=True, kind="limit"))
    await asyncio.sleep(0)  # let the limit checkpoint reach its copy first
    periodic = asyncio.create_task(_state.save_checkpoint(state, exclude_private=True, kind="full"))
    with pytest.raises(PermissionError):
        await limit
    assert _state.health.failure is not None
    assert _state.health.failure["kind"] == "limit" and _state.health.failure["step"] == "staging a copy"
    release.set()
    assert await periodic is True


async def test_health_records_the_last_good_checkpoint_and_the_last_failure(tmp_path, monkeypatch, staging):
    state = _project(tmp_path / "state", dirs=1, files=1)
    calls = []

    async def save(path):
        calls.append(path)
        if len(calls) == 2:
            raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    before = time.time()
    assert await _state.save_checkpoint(state, exclude_private=True, kind="limit") is True
    ok_at = _state.health.last_ok_at
    assert ok_at is not None and ok_at >= before
    assert _state.health.last_ok_kind == "limit"

    with pytest.raises(ConnectionError):
        await _state.save_checkpoint(state, exclude_private=True, kind="periodic")
    failure = _state.health.failure
    assert failure is not None
    assert failure["kind"] == "periodic" and failure["step"] == "uploading"
    assert failure["error"] == "ConnectionError: bucket unreachable"
    # A failure does not move the last good one.
    assert _state.health.last_ok_at == ok_at

    report = _state.health.report(now=ok_at + 90)
    assert report["last_ok_age_s"] == 90
    assert report["last_failure"]["step"] == "uploading"
    assert _state.health.describe_failure(ConnectionError("bucket unreachable"), before) == (
        "ConnectionError: bucket unreachable while uploading (after 0s)"
    )
    # A failure from before `since` is someone else's: the row then names only the exception.
    assert _state.health.describe_failure(TimeoutError(), time.time() + 60) == "TimeoutError"


async def test_a_bound_that_runs_out_while_the_staging_copy_is_removed_still_reports_the_checkpoint_saved(
    tmp_path, monkeypatch, staging
):
    """Copy and upload fit the bound, the removal does not: the checkpoint was saved and says so."""
    state = _project(tmp_path / "state", dirs=1, files=1)
    uploaded = []

    async def save(path):
        uploaded.append(sorted(p.name for p in path.iterdir()))

    real_remove = _state._remove_staging

    def remove(directory):
        time.sleep(0.5)  # a large staging copy, removed past the bound
        real_remove(directory)

    monkeypatch.setattr(_state, "_remove_staging", remove)
    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    started = time.monotonic()
    assert await _state.save_checkpoint(state, exclude_private=True, kind="limit", timeout=0.25) is True
    assert time.monotonic() - started >= 0.5  # it waited for the removal rather than leaving it running
    assert uploaded == [["author-conversation.json", "workspace"]]
    assert _state.health.failure is None and _state.health.last_ok_kind == "limit"
    assert list(staging.iterdir()) == []


async def test_a_checkpoint_cut_off_by_its_own_bound_names_the_step_and_removes_the_staging_copy(
    tmp_path, monkeypatch, staging, slow_source
):
    state = _project(tmp_path / "state")
    slow_source.append(state.root)
    uploaded = []

    async def save(path):
        uploaded.append(path)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    with pytest.raises(TimeoutError):
        await _state.save_checkpoint(state, exclude_private=True, kind="limit", timeout=0.15)
    assert list(staging.iterdir()) == []
    assert uploaded == []
    failure = _state.health.failure
    assert failure is not None and failure["step"] == "staging a copy" and failure["kind"] == "limit"
    assert failure["error"].startswith("timed out")
    assert _state.health.last_ok_at is None


async def test_a_directory_deleted_and_recreated_during_the_copy_does_not_fail_the_checkpoint(
    tmp_path, monkeypatch, staging
):
    """git prunes `objects/ab` after it was listed and a commit recreates it before anyone looks again."""
    state = _project(tmp_path / "state", dirs=3, files=1)
    target = state.workspace / ".git" / "objects" / "01"
    real = os.scandir

    def scandir(path="."):
        if not isinstance(path, int) and Path(os.fsdecode(path)) == target and target.exists():
            shutil.rmtree(target)
            try:
                return real(path)  # the copy reads it: FileNotFoundError
            finally:
                target.mkdir()
                (target / "new").write_bytes(b"y")
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    captured = tmp_path / "captured"

    async def save(path):
        shutil.copytree(path, captured)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    assert await _state.save_checkpoint(state, exclude_private=True) is True
    assert (captured / "workspace" / ".git" / "objects" / "00" / "object-0").is_file()
    assert (captured / "workspace" / ".git" / "objects" / "02" / "object-0").is_file()
    assert _state.health.failure is None
    assert list(staging.iterdir()) == []


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", *args], cwd=cwd, capture_output=True, text=True
    )


async def test_a_git_gc_during_the_copy_leaves_an_intact_repository_in_the_checkpoint(
    tmp_path, monkeypatch, staging, caplog
):
    """`git gc` packs loose objects while the copy is inside `.git/objects`: the copy alone misses them."""
    root = tmp_path / "state"
    work = root / "workspace"
    work.mkdir(parents=True)
    assert _git(work, "init", "-q").returncode == 0
    for i in range(20):
        (work / f"f{i}.txt").write_text(f"content {i}\n" * 50)
        _git(work, "add", ".")
        assert _git(work, "commit", "-qm", f"c{i}").returncode == 0
    (root / "author-conversation.json").write_text(json.dumps({"engine": "claude"}))
    real, fired = os.scandir, list[str]()

    def scandir(path="."):
        here = "" if isinstance(path, int) else os.fsdecode(path)
        if not fired and re.search(r"/\.git/objects/[0-9a-f]{2}$", here) and here.startswith(str(work)):
            fired.append(here)
            assert _git(work, "gc", "-q", "--prune=now").returncode == 0
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    captured = tmp_path / "captured"

    async def save(path):
        shutil.copytree(path, captured, symlinks=True)

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    assert await _state.save_checkpoint(_state.ProjectState(root, ""), exclude_private=True)
    assert fired
    assert "copying .git again" in caplog.text  # the first copy was broken, and the check caught it
    assert _git(captured / "workspace", "fsck").returncode == 0
    log = _git(captured / "workspace", "log", "--oneline")
    assert log.returncode == 0 and len(log.stdout.splitlines()) == 20
    assert list(staging.iterdir()) == []


async def test_a_staged_repository_still_broken_after_a_recopy_fails_the_checkpoint_naming_the_step(
    tmp_path, monkeypatch, staging
):
    state = _project(tmp_path / "state", dirs=1, files=1)
    (state.workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    checked = []

    async def intact(workspace):
        checked.append(workspace)
        return workspace == state.workspace  # the project's own repository is fine; the copies are not

    async def save(path):
        raise AssertionError("a broken copy must not be uploaded")

    monkeypatch.setattr(_state, "_repository_is_intact", intact)
    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    with pytest.raises(_state.CheckpointInconsistent):
        await _state.save_checkpoint(state, exclude_private=True, kind="limit")
    assert len(checked) == 3 and checked[-1] == state.workspace  # copy, recopy, then the source
    failure = _state.health.failure
    assert failure is not None and failure["step"] == "checking the staged repository"
    assert failure["error"].startswith("CheckpointInconsistent")
    assert list(staging.iterdir()) == []


def _paused_on_a_limit(tmp_path, monkeypatch):
    """A coordinator whose usage limit defers it (a hard pin to an engine it cannot use), as in #45."""
    from flyte_agent_factory import _meter, _pins, _session_config, _usage
    from tests.test_nudge import _agent_time

    now = _agent_time().time()
    (tmp_path / "state").mkdir(exist_ok=True)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    monkeypatch.setattr(_usage, "_meters", None)
    monkeypatch.setattr(_usage, "_hard_pins", True)
    monkeypatch.setattr(_pins, "engine_input", "codex")
    monkeypatch.setattr(_usage, "_author_engines", lambda: ("claude",))
    monkeypatch.setattr(_usage, "_child_meter", lambda e: _meter.Meter(e, "test", now, 100))
    return now


async def test_a_pause_on_the_limit_writes_a_usable_checkpoint_and_says_so(tmp_path, monkeypatch, staging):
    from flyte_agent_factory import _author, _timeline
    from flyte_agent_factory._author_session import AuthorSession
    from tests.test_author_handoff import Process

    now = _paused_on_a_limit(tmp_path, monkeypatch)
    state = _project(tmp_path / "state", dirs=4, files=3)
    (state.workspace / "work.py").write_text("uncommitted work")
    captured = tmp_path / "captured"
    attempts = []

    async def save(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise ConnectionError("bucket unreachable")
        os.replace(path, captured)
        path.mkdir()

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save, remote_destination=""))
    session = AuthorSession(Process(), tmp_path / "pending.json")
    timeline = _timeline._Timeline()
    callback = _author._author_handoff(session, state, timeline, brief="b")

    assert not await callback(now + 1000, "")
    rows = [text for _, _, text in timeline._rows]
    failed = [text for text in rows if text.startswith("limit checkpoint failed")]
    assert failed == [
        "limit checkpoint failed: ConnectionError: bucket unreachable while uploading (after 0s)"
    ]
    report = timeline.snapshot()["checkpoints"]
    assert report["last_ok_at"] is None
    assert report["last_failure"]["kind"] == "limit" and report["last_failure"]["step"] == "uploading"

    # The next pump tick of the same pause tries again, and this time it is written.
    assert not await callback(now + 1000, "")
    assert len(attempts) == 2
    assert "limit checkpoint saved" in [text for _, _, text in timeline._rows]
    assert (captured / "workspace" / "work.py").read_text() == "uncommitted work"
    assert (captured / "author-conversation.json").is_file()
    assert list(staging.iterdir()) == []
    report = timeline.snapshot()["checkpoints"]
    assert report["last_ok_kind"] == "limit" and report["last_failure"] is None
    assert report["last_ok_age_s"] is not None and report["last_ok_age_s"] <= 5

    # Saved once per pause: a third tick has nothing new to record.
    assert not await callback(now + 1000, "")
    assert len(attempts) == 2


async def test_a_limit_checkpoint_cut_off_by_its_bound_names_the_step_and_the_time(
    tmp_path, monkeypatch, staging, slow_source
):
    from flyte_agent_factory import _author, _timeline
    from flyte_agent_factory._author_session import AuthorSession
    from tests.test_author_handoff import Process

    now = _paused_on_a_limit(tmp_path, monkeypatch)
    state = _project(tmp_path / "state")
    slow_source.append(state.root)
    monkeypatch.setattr(_author, "_LIMIT_CHECKPOINT_TIMEOUT_S", 0.2)

    async def save(path):
        raise AssertionError("the copy never finished, so nothing may be uploaded")

    monkeypatch.setattr(_state, "_checkpoint", lambda: SimpleNamespace(save=save))
    timeline = _timeline._Timeline()
    callback = _author._author_handoff(
        AuthorSession(Process(), tmp_path / "pending.json"), state, timeline, brief="b"
    )
    assert not await callback(now + 1000, "")
    failed = [text for _, _, text in timeline._rows if text.startswith("limit checkpoint failed")]
    assert len(failed) == 1
    assert failed[0].startswith("limit checkpoint failed: TimeoutError while staging a copy (after 0s)")
    assert "OSError" not in failed[0]
    assert list(staging.iterdir()) == []


def test_status_carries_the_age_of_the_last_good_checkpoint():
    from flyte_agent_factory import _factory_status

    failure = {"at": 950.0, "kind": "limit", "step": "uploading", "elapsed_s": 3.0, "error": "x"}
    data = {"checkpoints": {"last_ok_at": 400.0, "last_ok_kind": "periodic", "last_failure": failure}}
    assert _factory_status.report_checkpoint_fields(data, 1000.0) == {
        "checkpoints": {
            "last_ok_at": 400.0,
            "last_ok_age_s": 600,
            "last_ok_kind": "periodic",
            "last_failure": failure,
        }
    }
    # Older reports have none, and a garbled one must not fail the status.
    assert _factory_status.report_checkpoint_fields({}, 1000.0) == {"checkpoints": None}
    assert _factory_status.report_checkpoint_fields({"checkpoints": {"last_ok_at": "soon"}}, 1000.0)[
        "checkpoints"
    ] == {
        "last_ok_at": None,
        "last_ok_age_s": None,
        "last_ok_kind": "",
        "last_failure": None,
    }


def test_the_report_is_stable_within_a_minute_whatever_the_checkpoint_age():
    """Two renders in the same minute are the same page, even when a second ticks over between them."""
    from flyte_agent_factory import _timeline

    t0 = 1_700_000_000 // 60 * 60 + 30.0
    _state.health.last_ok_at = t0 - 1200 + 0.002
    timeline = _timeline._Timeline()
    first, second = timeline.snapshot(t0), timeline.snapshot(t0 + 0.9)
    assert first["checkpoints"] == second["checkpoints"]
    assert first["checkpoints"]["last_ok_age_s"] == 1169  # as of the report's minute, `generated_at`
    # Saved after the minute began: as of that minute it is no age at all, never a negative one.
    _state.health.last_ok_at = t0 - 10
    assert timeline.snapshot(t0)["checkpoints"]["last_ok_age_s"] == 0


@pytest.mark.parametrize("at", [float("nan"), float("inf"), float("-inf")])
def test_a_last_good_checkpoint_time_that_is_not_finite_does_not_fail_the_status(at):
    from flyte_agent_factory import _factory_status

    data = json.loads(json.dumps({"checkpoints": {"last_ok_at": at, "last_ok_kind": "periodic"}}))
    assert _factory_status.report_checkpoint_fields(data, 1000.0) == {
        "checkpoints": {
            "last_ok_at": None,
            "last_ok_age_s": None,
            "last_ok_kind": "periodic",
            "last_failure": None,
        }
    }
