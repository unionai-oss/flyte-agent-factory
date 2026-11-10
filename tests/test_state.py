"""Project state from `Dir` inputs/outputs and `flyte.Checkpoint`.

Two lifetimes: a checkpoint carries state across *attempts* of one run, and a
`Dir` output carries it across *runs*. The ordering between them is the part
worth pinning down — restoring the older one over the newer would silently
discard work.
"""

import shutil
import types
from pathlib import Path
from typing import Any, cast

import pytest

from flyte_agent_factory import _checkpoint, _session_config, _state


class _FakeDir:
    def __init__(self, source: Path, path: str = "s3://bucket/previous-run"):
        self.source = source
        self.path = path
        self.downloaded_to: Path | None = None

    async def download(self, target):
        self.downloaded_to = Path(target)
        shutil.copytree(self.source, self.downloaded_to, dirs_exist_ok=True)
        return str(self.downloaded_to)


class _FakeCheckpoint:
    def __init__(self, previous: Path | None = None):
        self.previous = previous
        self.saved: list[Path] = []

    def prev_exists(self):
        return self.previous is not None

    async def load(self):
        return self.previous

    async def save(self, data):
        self.saved.append(Path(data))


@pytest.fixture
def no_checkpoint(monkeypatch):
    monkeypatch.setattr(_state, "_checkpoint", lambda: None)


def _previous_run_workspace(tmp_path: Path) -> Path:
    source = tmp_path / "from-previous-run"
    source.mkdir()
    (source / "README.md").write_text("from the previous run\n")
    return source


async def test_new_project_lays_out_both_trees(tmp_path, no_checkpoint):
    state = await _state.restore(root=tmp_path / "state")

    assert not state.resumed
    assert state.workspace.is_dir()
    assert state.config_dir.is_dir()
    # One root, so a single checkpoint captures the work and the conversation
    # that produced it together.
    assert state.workspace.parent == state.config_dir.parent == state.root


async def test_a_dir_input_resumes_a_previous_run(tmp_path, no_checkpoint):
    incoming = _FakeDir(_previous_run_workspace(tmp_path))

    state = await _state.restore(root=tmp_path / "state", workspace_in=cast(Any, incoming))  # a fake `Dir`

    assert state.resumed
    assert state.resumed_from == "s3://bucket/previous-run"
    assert (state.workspace / "README.md").read_text() == "from the previous run\n"


async def test_a_checkpoint_beats_the_dir_input(tmp_path, monkeypatch):
    """The checkpoint is newer, so restoring the input over it would lose work.

    The checkpoint comes from an earlier attempt of *this* run; the `Dir` input
    comes from a previous run that finished before it.
    """
    checkpointed = tmp_path / "checkpointed"
    (checkpointed / _state.WORKSPACE_SUBDIR).mkdir(parents=True)
    (checkpointed / _state.CLAUDE_SUBDIR).mkdir(parents=True)
    (checkpointed / _state.WORKSPACE_SUBDIR / "README.md").write_text("mid-run work\n")
    monkeypatch.setattr(_state, "_checkpoint", lambda: _FakeCheckpoint(previous=checkpointed))

    incoming = _FakeDir(_previous_run_workspace(tmp_path))
    state = await _state.restore(root=tmp_path / "state", workspace_in=cast(Any, incoming))  # a fake `Dir`

    assert state.resumed_from == "checkpoint"
    assert (state.workspace / "README.md").read_text() == "mid-run work\n"
    assert incoming.downloaded_to is None, "the stale input should not have been downloaded"


async def test_checkpoint_saves_the_whole_root(tmp_path, monkeypatch):
    fake = _FakeCheckpoint()
    monkeypatch.setattr(_state, "_checkpoint", lambda: fake)
    state = await _state.restore(root=tmp_path / "state")

    assert await _state.save_checkpoint(state) is True
    # The SDK archives a directory itself, so we hand it the root and no more.
    assert fake.saved == [state.root]


async def test_no_checkpoint_context_is_not_an_error(tmp_path, no_checkpoint):
    # Local runs have no checkpoint paths; the project must still work.
    state = await _state.restore(root=tmp_path / "state")
    assert await _state.save_checkpoint(state) is False


async def test_publish_hands_back_the_workspace(tmp_path, no_checkpoint, monkeypatch):
    state = await _state.restore(root=tmp_path / "state")
    (state.workspace / "out.txt").write_text("done\n")

    captured = {}

    class _Dir:
        @staticmethod
        async def from_local(path):
            captured["path"] = path
            return types.SimpleNamespace(path=path)

    monkeypatch.setitem(__import__("sys").modules, "flyte.io", types.SimpleNamespace(Dir=_Dir))
    published = await _state.publish(state)

    # The workspace, not the state root: the config dir holds the transcript
    # and has no business in a downstream run's inputs.
    assert captured["path"] == str(state.workspace)
    assert published.path == str(state.workspace)


def test_the_checkpoint_location_is_where_this_action_saves(monkeypatch):
    """The checkpoint location is the path this action saves to."""
    import flyte

    paths = types.SimpleNamespace(checkpoint_path="s3://b/run/a0/checkpoint", prev_checkpoint_path=None)
    monkeypatch.setattr(flyte, "ctx", lambda: types.SimpleNamespace(checkpoint_paths=paths))
    assert _state.checkpoint_location() == "s3://b/run/a0/checkpoint"
    monkeypatch.setattr(flyte, "ctx", lambda: None)
    assert _state.checkpoint_location() == ""


async def test_the_run_leaves_its_checkpoint_location_for_the_factory(monkeypatch, queue_state):
    """The run leaves its checkpoint location where the factory reads it."""
    from flyte_agent_factory import _auth, _factory_base, _queue

    monkeypatch.setattr(_session_config, "_PROFILE", _factory_base.PROFILE)
    monkeypatch.setattr(_state, "checkpoint_location", lambda: "s3://b/run/a0/checkpoint")
    rows = []
    timeline = types.SimpleNamespace(row=lambda *a: rows.append(a))
    monkeypatch.setattr(_auth, "own_client_id", lambda: "pods-key-id")
    await _checkpoint._note_checkpoint_location(cast(Any, timeline), "alpha-1-0-aaaaaa")
    assert (
        _queue.read_checkpoint_uri(_factory_base.PROFILE.state.uri, "alpha-1-0-aaaaaa")
        == "s3://b/run/a0/checkpoint"
    )
    assert _queue.read_checkpoint_uri(_factory_base.PROFILE.state.uri, "never-noted") == ""
    assert _queue.read_app_client_id(_factory_base.PROFILE.state.uri, "alpha-1-0-aaaaaa") == "pods-key-id"
    assert _queue.read_app_client_id(_factory_base.PROFILE.state.uri, "never-noted") == ""
    assert rows == []


def test_the_run_notes_its_key_only_by_client_id(monkeypatch):
    """The factory compares who aborted a run with the run's own key; the secret part never leaves it."""
    import base64

    from flyte_agent_factory import _auth

    key = base64.b64encode(b"tenant.example:pods-key-id:s3cr3t-part:org1").decode()
    monkeypatch.delenv("EAGER_API_KEY", raising=False)
    monkeypatch.setenv("_UNION_EAGER_API_KEY", key)
    assert _auth.own_client_id() == "pods-key-id"
    monkeypatch.delenv("_UNION_EAGER_API_KEY")
    monkeypatch.setenv("EAGER_API_KEY", key)  # the SDK's fallback name
    assert _auth.own_client_id() == "pods-key-id"
    monkeypatch.setenv("EAGER_API_KEY", "not-a-key")
    assert _auth.own_client_id() == ""
    monkeypatch.delenv("EAGER_API_KEY")
    assert _auth.own_client_id() == ""
