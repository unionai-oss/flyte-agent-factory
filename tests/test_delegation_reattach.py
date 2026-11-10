"""A delegation outlives a supervisor restart: its worker action is reattached, and no work is dropped."""

import asyncio
import json
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte.remote
import pytest
from flyte.models import ActionPhase

from flyte_agent_factory import _bridge, _secret_env, _session_setup, child
from flyte_agent_factory import _delegation as delegation
from flyte_agent_factory._delegation import Delegations

# A `Delegations` or `_delegate_reattach` argument that the test's path never reaches.
UNUSED: Any = None


def git(repo: Path, *args: str) -> str:
    """Run `git` in `repo` and return its stripped output."""
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def repo(tmp_path):
    """Provide a committed repo standing in for the author's workspace."""
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "tracked").write_text("before")
    (root / ".gitignore").write_text(".claude-flyte/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def interrupted_job(
    bridge_root: Path,
    repo: Path,
    job_id: str = "delegate-cut",
    *,
    action: bool = True,
    answer_only: bool = False,
) -> Path:
    """Write what a supervisor leaves behind when it dies while the worker's child action is running."""
    _bridge._write_atomic(bridge_root / f"requests/{job_id}.json", {"id": job_id, "brief": "fix"})
    _bridge._write_atomic(bridge_root / f"answers/{job_id}.json", {"state": "running"})
    job = repo / ".claude-flyte/delegations" / job_id
    job.mkdir(parents=True)
    (job / "brief.md").write_text("fix")
    (job / "base").write_text("abc123\n")
    if answer_only:
        (job / "answer-only").touch()
    if action:
        (job / "action.json").write_text(json.dumps({"name": "a7", "run_name": "r1", "engine": "claude"}))
    return job


def answer(bridge_root: Path, job_id: str = "delegate-cut") -> dict[str, Any]:
    """Return the answer file the bridge holds for `job_id`."""
    return cast(dict[str, Any], json.loads((bridge_root / f"answers/{job_id}.json").read_text()))


async def settle(supervisor: Delegations) -> None:
    """Run the restart path of `supervisor` and wait for the jobs it reattached."""
    supervisor.resume()
    await asyncio.gather(*supervisor.pending)


async def test_submission_records_the_exact_action_in_the_job_folder(repo, bridge_root, monkeypatch):
    """A submission records the exact worker action in the job folder, and a retry replaces it."""
    monkeypatch.setattr(delegation, "_run_name", lambda: "r1")

    async def worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        child.notify_submitted("a3")
        # Already on disk while the action runs: that is what a restart reads.
        recorded = json.loads((repo / ".claude-flyte/delegations/delegate-new/action.json").read_text())
        assert recorded == {"name": "a3", "run_name": "r1", "engine": "claude"}
        child.notify_submitted("a4")  # the on-demand retry replaces the spot attempt
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("diff --git a/x b/x\n")
        return "ok"

    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *args: None)
    await supervisor.service({"id": "delegate-new", "brief": "fix"})
    saved = json.loads((supervisor.jobs / "delegate-new/action.json").read_text())
    assert saved["name"] == "a4"


async def test_restart_reattaches_a_running_action_and_collects_its_outputs(repo, bridge_root):
    """A restart reattaches a running action and collects its outputs."""
    job = interrupted_job(bridge_root, repo)
    seen = []

    async def reattach(target, run_name, name):
        seen.append((run_name, name))
        (target / "result").mkdir()
        (target / "result/factory.patch").write_text("diff --git a/x b/x\n")
        return "finished while the supervisor was down"

    supervisor = Delegations(bridge_root, repo, UNUSED, UNUSED, lambda *args: None, reattach=reattach)
    assert answer(bridge_root)["state"] == "running"
    assert not (job / "error.txt").exists()
    await settle(supervisor)
    assert seen == [("r1", "a7")]
    result = answer(bridge_root)
    assert result["state"] == "done" and result["summary"] == "finished while the supervisor was down"
    assert (job / "factory.patch").read_text() == "diff --git a/x b/x\n"
    assert "delegate-cut (claude, ok: 1 files)" in supervisor.notices["delegate-cut"]
    assert not (job / "error.txt").exists()


async def test_restart_reattaches_an_answer_only_job(repo, bridge_root):
    """A restart reattaches an answer-only job and records its answer."""
    interrupted_job(bridge_root, repo, answer_only=True)

    async def reattach(target, run_name, name):
        return "the answer"

    supervisor = Delegations(bridge_root, repo, UNUSED, UNUSED, lambda *args: None, reattach=reattach)
    await settle(supervisor)
    assert answer(bridge_root) == {"state": "done", "summary": "the answer"}


async def test_restart_with_a_failed_action_fails_with_its_error(repo, bridge_root):
    """A restart whose action failed fails the job with the action's own error."""
    job = interrupted_job(bridge_root, repo)

    async def reattach(target, run_name, name):
        raise RuntimeError("action a7 FAILED: out of memory")

    supervisor = Delegations(bridge_root, repo, UNUSED, UNUSED, lambda *args: None, reattach=reattach)
    await settle(supervisor)
    assert answer(bridge_root)["state"] == "failed"
    assert "out of memory" in (job / "error.txt").read_text()
    assert "supervisor restarted" not in (job / "error.txt").read_text()
    assert "failed" in supervisor.notices["delegate-cut"]


def test_restart_without_a_recorded_action_still_fails(repo, bridge_root):
    """A restart without a recorded action still fails the job as before."""
    job = interrupted_job(bridge_root, repo, action=False)
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED, reattach=cast(Any, lambda *args: None))
    assert (job / "error.txt").read_text() == "supervisor restarted"
    assert answer(bridge_root)["state"] == "failed"


def test_restart_without_a_reattach_hook_still_fails(repo, bridge_root):
    """A restart without a reattach hook still fails the job as before."""
    job = interrupted_job(bridge_root, repo)
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED)
    assert (job / "error.txt").read_text() == "supervisor restarted"


def test_a_reattachable_job_is_not_failed_by_the_unfinished_folder_sweep(repo, bridge_root):
    """A job with an action to watch is not failed by the sweep of unfinished job folders."""
    # A job whose answer file was never written is not "running", but it has an action to watch all the same.
    job = interrupted_job(bridge_root, repo)
    (bridge_root / "answers/delegate-cut.json").unlink()
    Delegations(bridge_root, repo, UNUSED, UNUSED, UNUSED, reattach=cast(Any, lambda *args: None))
    assert not (job / "error.txt").exists()


def edit_worker(change: Callable[[Path], None]) -> Callable[..., Any]:
    """Return a worker that leaves its repo in `result/repo`, as the sub-agent pod does, but no patch."""

    async def worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        (target / "result").mkdir()
        with tarfile.open(target / "repo.tar.gz") as archive:
            archive.extractall(target / "result", filter="data")
        change(target / "result" / "repo")
        return "ended its turn"

    return worker


async def test_a_worker_without_factory_patch_hands_back_its_work(repo, bridge_root):
    """A worker that ends without `factory.patch` still hands back its edits as a patch."""

    def change(root):
        (root / "tracked").write_text("after")
        (root / "new.py").write_text("print(1)\n")

    supervisor = Delegations(bridge_root, repo, edit_worker(change), lambda: "claude", lambda *args: None)
    await supervisor.service({"id": "delegate-lazy", "brief": "fix"})
    result = answer(bridge_root, "delegate-lazy")
    assert result["state"] == "done", result
    patch = (supervisor.jobs / "delegate-lazy/factory.patch").read_text()
    assert "+after" in patch and "diff --git a/new.py b/new.py" in patch
    assert "ended its turn" in result["summary"] and "no factory.patch" in result["summary"]
    # It is a patch against the delegation base: the author's tree takes it.
    subprocess.run(["git", "-C", str(repo), "apply", "--check"], input=patch.encode(), check=True)


async def test_a_hostile_worker_tree_runs_no_configured_program_and_still_hands_back_its_work(
    repo, bridge_root, tmp_path
):
    """A worker's tree configures git programs; handing its work back runs none of them."""
    marks = tmp_path / "marks"
    marks.mkdir()

    def program(name: str) -> str:
        return f"sh -c 'echo ran > {marks / name}; cat'"

    def change(root):
        (root / "tracked").write_text("after")
        for key, name in (
            ("diff.evil.command", "extdiff"),
            ("filter.evil.clean", "filter"),
            ("core.fsmonitor", "fsmonitor"),
        ):
            subprocess.run(["git", "-C", str(root), "config", key, program(name)], check=True)
        (root / ".gitattributes").write_text("* diff=evil filter=evil\n")
        (root / ".git/info").mkdir(exist_ok=True)
        (root / ".git/info/attributes").write_text("* diff=evil filter=evil\n")
        for hook in ("pre-commit", "post-index-change"):
            (root / ".git/hooks").mkdir(exist_ok=True)
            script = root / ".git/hooks" / hook
            script.write_text(f"#!/bin/sh\necho ran > {marks / hook}\n")
            script.chmod(0o755)
        # A nested git dir is a second armed one: an embedded repo, and a gitfile into `.git/modules`.
        subprocess.run(["git", "init", "-q", str(root / "sub")], check=True)
        (root / ".git/modules").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", "--separate-git-dir", str(root / ".git/modules/sub2"), str(root / "sub2")],
            check=True,
        )
        for nested, mark in (("sub", "nested"), ("sub2", "modules")):
            (root / nested / "f").write_text("x\n")
            git = ["git", "-C", str(root / nested), "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run([*git, "add", "f"], check=True)
            subprocess.run([*git, "commit", "-q", "-m", "x"], check=True)
            subprocess.run([*git, "config", "filter.evil.clean", program(mark)], check=True)
            (root / nested / ".gitattributes").write_text("* filter=evil\n")
            (root / nested / "f").write_text("y\n")  # same length: only a content check sees it

    supervisor = Delegations(bridge_root, repo, edit_worker(change), lambda: "claude", lambda *args: None)
    await supervisor.service({"id": "delegate-hostile", "brief": "fix"})
    result = answer(bridge_root, "delegate-hostile")
    assert list(marks.iterdir()) == []
    assert result["state"] == "done", result
    patch = (supervisor.jobs / "delegate-hostile/factory.patch").read_text()
    assert "+after" in patch
    # The nested repos' work came back as plain files.
    assert "+y\n" in patch.split("+++ b/sub/f\n")[1] and "+y\n" in patch.split("+++ b/sub2/f\n")[1]
    subprocess.run(["git", "-C", str(repo), "apply", "--check"], input=patch.encode(), check=True)


def test_work_diff_refuses_a_git_dir_that_is_not_a_directory(tmp_path):
    """A `.git` gitfile could point at the author's own repo, so `work_diff` refuses to disarm it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "-C", str(elsewhere), "init", "-q"], check=True)
    config = (elsewhere / ".git/config").read_text()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {elsewhere / '.git'}\n")
    with pytest.raises(ValueError, match="tree"):
        delegation.work_diff(tree, "HEAD")
    assert (elsewhere / ".git/config").read_text() == config
    assert (elsewhere / ".git/hooks").is_dir()


def test_disarming_a_worker_tree_never_writes_or_deletes_through_its_symlinks(tmp_path):
    """Links in a worker's `.git` reach the supervisor's files; disarming removes links, not targets."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("config", "commondir"):
        (outside / name).write_text(f"author's {name}\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    (tree / ".git/config").unlink()
    (tree / ".git/config").symlink_to(outside / "config")
    (tree / ".git/commondir").symlink_to(outside / "commondir")
    _secret_env.disarm_git_dir(tree)
    assert (outside / "config").read_text() == "author's config\n"
    assert (outside / "commondir").read_text() == "author's commondir\n"
    assert not (tree / ".git/config").is_symlink() and "[core]" in (tree / ".git/config").read_text()
    assert not (tree / ".git/commondir").exists()


def test_disarming_leaves_a_worker_tree_with_no_git_dir_but_its_own(tmp_path):
    """A nested `.git` (repo, gitfile, symlink) or `.git/modules` is a git dir of the worker's making."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config").write_text("kept\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    subprocess.run(["git", "init", "-q", str(tree / "sub")], check=True)
    (tree / ".git/modules").mkdir()
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(tree / ".git/modules/sub2"), str(tree / "sub2")],
        check=True,
    )
    (tree / ".git/worktrees/w").mkdir(parents=True)
    (tree / "linked").mkdir()
    (tree / "linked/.git").symlink_to(outside, target_is_directory=True)
    _secret_env.disarm_git_dir(tree)
    assert [p for p in tree.rglob(".git")] == [tree / ".git"]
    assert not (tree / ".git/modules").exists() and not (tree / ".git/worktrees").exists()
    assert (outside / "config").read_text() == "kept\n"


@pytest.mark.parametrize("parent", ["info", "objects/info"])
def test_disarming_refuses_a_worker_tree_whose_git_subdirectory_is_a_symlink(tmp_path, parent):
    """`.git/info` linked elsewhere would make removing `info/attributes` delete the supervisor's file."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "attributes").write_text("kept\n")
    (outside / "alternates").write_text("kept\n")
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "-C", str(tree), "init", "-q"], check=True)
    shutil.rmtree(tree / ".git" / parent, ignore_errors=True)
    (tree / ".git" / parent).symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _secret_env.disarm_git_dir(tree)
    assert (outside / "attributes").read_text() == "kept\n"
    assert (outside / "alternates").read_text() == "kept\n"


async def test_a_worker_without_factory_patch_or_changes_is_done_with_no_changes(repo, bridge_root):
    """A worker that ends with neither `factory.patch` nor edits is done with no changes."""
    supervisor = Delegations(
        bridge_root, repo, edit_worker(lambda root: None), lambda: "claude", lambda *args: None
    )
    await supervisor.service({"id": "delegate-idle", "brief": "fix"})
    result = answer(bridge_root, "delegate-idle")
    assert result["state"] == "done", result
    assert "no changes" in result["summary"].lower()
    assert (supervisor.jobs / "delegate-idle/factory.patch").read_text() == ""
    assert not (supervisor.jobs / "delegate-idle/error.txt").exists()


async def test_a_reattached_worker_without_factory_patch_hands_back_its_work(repo, bridge_root, tmp_path):
    """A reattached worker without `factory.patch` still hands back its edits as a patch."""
    job = interrupted_job(bridge_root, repo)
    (repo / "tracked").write_text("dirty")  # the base the snapshot would have taken
    base = delegation.snapshot(repo, tmp_path)
    (job / "base").write_text(base + "\n")

    async def reattach(target, run_name, name):
        (target / "result").mkdir()
        with tarfile.open(tmp_path / "repo.tar.gz") as archive:
            archive.extractall(target / "result", filter="data")
        (target / "result/repo/new.py").write_text("print(1)\n")
        return "ended its turn"

    supervisor = Delegations(bridge_root, repo, UNUSED, UNUSED, lambda *args: None, reattach=reattach)
    await settle(supervisor)
    assert answer(bridge_root)["state"] == "done"
    assert "diff --git a/new.py b/new.py" in (job / "factory.patch").read_text()


class FakeAction:
    """`flyte.remote.Action` / `ActionDetails` as far as the reattach hook uses them."""

    def __init__(self, phase: ActionPhase, outputs: tuple[str, Any] | None = None, error: str = "") -> None:
        """Hold the phase, outputs and error message the hook reads."""
        self.phase, self._outputs, self.error_message = phase, outputs, error
        self.waited = False
        # `get` is syncified in the SDK, so it has `.aio`; `Action.wait` is a plain coroutine method.
        self.get = SimpleNamespace(aio=self._get)

    async def _get(self, run_name, name):
        assert (run_name, name) == ("r1", "a7")
        return self

    async def wait(self, quiet=False):
        """Record that the hook waited for the action."""
        self.waited = True

    def done(self):
        """Say whether the action reached a final phase."""
        return self.phase in (
            ActionPhase.SUCCEEDED,
            ActionPhase.FAILED,
            ActionPhase.ABORTED,
            ActionPhase.TIMED_OUT,
        )

    async def outputs(self):
        """Return the outputs the action was built with."""
        return self._outputs


def fake_remote(monkeypatch: pytest.MonkeyPatch, action: object) -> None:
    """Replace the SDK's remote action classes with `action`."""
    monkeypatch.setattr(flyte.remote, "Action", action)
    monkeypatch.setattr(flyte.remote, "ActionDetails", action)


class FakeDir:
    """A downloadable directory of text files."""

    def __init__(self, files: dict[str, str]) -> None:
        """Hold the files `download` writes."""
        self.files = files

    async def download(self, path):
        """Write the files under `path`."""
        for name, text in self.files.items():
            (Path(path) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(path) / name).write_text(text)


async def test_the_production_hook_waits_for_the_action_and_downloads_its_output(tmp_path, monkeypatch):
    """The production hook waits for the action and downloads its output."""
    action = FakeAction(
        ActionPhase.SUCCEEDED, ("all done", FakeDir({"factory.patch": "diff --git a/x b/x\n"}))
    )
    fake_remote(monkeypatch, action)
    summary = await _session_setup._delegate_reattach(UNUSED)(tmp_path, "r1", "a7")
    assert action.waited and summary == "all done"
    assert (tmp_path / "result/factory.patch").read_text() == "diff --git a/x b/x\n"


@pytest.mark.parametrize("phase", [ActionPhase.FAILED, ActionPhase.ABORTED, ActionPhase.TIMED_OUT])
async def test_the_production_hook_raises_the_actions_error_when_it_did_not_succeed(
    tmp_path, monkeypatch, phase
):
    """The production hook raises the action's error when it did not succeed."""
    fake_remote(monkeypatch, FakeAction(phase, error="out of memory"))
    with pytest.raises(RuntimeError, match=r"a7 ended .*out of memory"):
        await _session_setup._delegate_reattach(UNUSED)(tmp_path, "r1", "a7")


def test_the_fake_has_the_shape_of_the_real_sdk_class():
    """The fake action has the shape of the real SDK class."""
    import inspect

    import flyte.remote

    assert inspect.iscoroutinefunction(flyte.remote.Action.wait)
    assert not hasattr(flyte.remote.Action.wait, "aio")
    assert hasattr(flyte.remote.Action.get, "aio") and hasattr(flyte.remote.ActionDetails.get, "aio")
    assert inspect.iscoroutinefunction(flyte.remote.ActionDetails.outputs)
    assert callable(flyte.remote.ActionDetails.done)


async def test_cancelling_a_reattach_leaves_the_job_running_and_reattachable(repo, bridge_root, monkeypatch):
    """Cancelling a reattach leaves the job running and reattachable."""
    job = interrupted_job(bridge_root, repo)

    class StillRunning(FakeAction):
        async def wait(self, quiet=False):
            # What the SDK does: `Action.wait` swallows the cancellation and returns.
            self.waited = True

    fake_remote(monkeypatch, StillRunning(ActionPhase.RUNNING))
    supervisor = Delegations(
        bridge_root,
        repo,
        UNUSED,
        UNUSED,
        lambda *args: None,
        reattach=_session_setup._delegate_reattach(UNUSED),
    )
    supervisor.resume()
    await asyncio.sleep(0)
    for task in supervisor.pending:
        task.cancel()
    await asyncio.gather(*supervisor.pending, return_exceptions=True)
    assert answer(bridge_root)["state"] == "running"
    assert supervisor.can_reattach(job)


async def test_the_hook_waits_out_the_read_model_lag_after_the_watch_stream_ends(tmp_path, monkeypatch):
    """The hook keeps reading when the read model lags the end of the watch stream."""
    # `Action.wait` follows the watch stream; `ActionDetails.get` reads a model that can lag it, so the
    # first read after the wait may still say RUNNING. That is not a cancellation: the job must finish.
    done = FakeAction(ActionPhase.SUCCEEDED, ("all done", FakeDir({"factory.patch": "diff --git a/x b/x\n"})))
    reads = iter([FakeAction(ActionPhase.RUNNING), FakeAction(ActionPhase.RUNNING)])

    async def details(run_name, name):
        return next(reads, done)

    monkeypatch.setattr(_session_setup, "_REATTACH_SETTLE_DELAYS_S", (0, 0, 0))
    monkeypatch.setattr(flyte.remote, "Action", done)
    monkeypatch.setattr(flyte.remote, "ActionDetails", SimpleNamespace(get=SimpleNamespace(aio=details)))
    assert await _session_setup._delegate_reattach(UNUSED)(tmp_path, "r1", "a7") == "all done"


async def test_a_read_model_that_never_settles_fails_the_job_loudly(tmp_path, monkeypatch):
    """A read model that never settles fails the job loudly."""

    async def details(run_name, name):
        return FakeAction(ActionPhase.RUNNING)

    monkeypatch.setattr(_session_setup, "_REATTACH_SETTLE_DELAYS_S", (0, 0))
    monkeypatch.setattr(flyte.remote, "Action", FakeAction(ActionPhase.SUCCEEDED))
    monkeypatch.setattr(flyte.remote, "ActionDetails", SimpleNamespace(get=SimpleNamespace(aio=details)))
    with pytest.raises(RuntimeError, match="a7 ended on the watch stream but still shows running"):
        await _session_setup._delegate_reattach(UNUSED)(tmp_path, "r1", "a7")


async def test_a_failure_after_an_engine_switch_names_the_engine_that_ran(repo, bridge_root):
    """A failure after an engine switch names the engine that ran."""

    async def worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        assert on_engine is not None
        on_engine("codex", "gpt-5 high")
        raise RuntimeError("worker died")

    lines = []
    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *row: lines.append(row))
    await supervisor.service({"id": "delegate-switch", "brief": "fix"})
    assert "(codex, failed:" in supervisor.notices["delegate-switch"]
    assert any("delegate-switch (codex) failed" in row[1] for row in lines)


async def test_a_bug_in_a_worker_fails_its_job_loudly_instead_of_leaving_it_running(
    repo, bridge_root, capsys
):
    """A bug in a worker fails its job loudly instead of leaving it running."""

    # Only the errors a worker is expected to raise are handled where it runs; anything else
    # is a bug, which must still end the job (so `delegate wait` returns) and print its traceback.
    async def worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        raise TypeError("worker bug")

    supervisor = Delegations(bridge_root, repo, worker, lambda: "claude", lambda *row: None)
    _bridge._write_atomic(bridge_root / "requests/delegate-bug.json", {"id": "delegate-bug", "brief": "fix"})
    supervisor.scan()
    await asyncio.gather(*supervisor.pending, return_exceptions=True)
    await asyncio.sleep(0)
    assert answer(bridge_root, "delegate-bug")["state"] == "failed"
    assert "TypeError: worker bug" in answer(bridge_root, "delegate-bug")["error"]
    assert "Traceback" in capsys.readouterr().err
    assert not supervisor.active
