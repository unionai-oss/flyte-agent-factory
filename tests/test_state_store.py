from __future__ import annotations

import json
import multiprocessing
import shutil
import subprocess
import time
from pathlib import Path
from typing import cast

import pytest

from flyte_agent_factory import _state_store as ss
from tests.state_git import (
    BRANCH,
    make_remote,
    make_store,
    rival_wins_first_push,
    take_production_lease,
    write_in_process,
)


def bare(remote: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(remote), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_roundtrip_and_delete(tmp_path):
    store = make_store(tmp_path)
    assert store.read("a/b.json") is None
    assert store.write({"a/b.json": b"1", "a/c.json": b"2"}, "add") is True
    assert store.read("a/b.json") == b"1"
    assert store.exists("a/c.json")
    assert store.write({"a/b.json": None}, "rm") is True
    assert not store.exists("a/b.json")
    assert store.list() == ["a/c.json"]
    assert store.write({"a/b.json": None}, "rm again") is False


def test_counts_pushes_and_rejected_pushes(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.write({"a": b"1"}, "first")
    assert (store.pushes, store.lost_races) == (1, 0)
    rival_wins_first_push(monkeypatch, store, tmp_path, {"b": b"2"})
    store.write({"c": b"3"}, "second")
    assert (store.pushes, store.lost_races) == (2, 1)  # the rival's push is counted on the rival


def test_list_prefix_is_directory_prefix(tmp_path):
    store = make_store(tmp_path)
    store.write({"queue/a/x": b"", "queue/ab/x": b"", "queue/a/y/z": b"", "other": b""}, "add")
    assert store.list("queue/a") == ["queue/a/x", "queue/a/y/z"]
    assert store.list("queue/a/") == ["queue/a/x", "queue/a/y/z"]
    assert store.list("queue") == ["queue/a/x", "queue/a/y/z", "queue/ab/x"]
    assert store.list("nope") == []


def test_missing_branch_is_empty(tmp_path):
    store = make_store(tmp_path)
    snap = store.snapshot(fresh=True)
    assert snap.tip == ""
    assert snap.list() == []
    assert store.read("x", fresh=True) is None


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "./a", "a/", ".."])
def test_bad_keys_rejected(tmp_path, key):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.write({key: b"x"}, "bad")
    with pytest.raises(ValueError):
        store.read(key)


def test_first_write_creates_orphan_branch_with_only_state_files(tmp_path):
    remote = make_remote(tmp_path)
    store = make_store(tmp_path, remote=remote)
    store.write({"queue/a/0001.json": b"{}"}, "first")
    store.write({"reserve.json": b"{}"}, "second")
    commits = bare(remote, "rev-list", BRANCH).split()
    assert len(commits) == 2
    root = commits[-1]
    assert bare(remote, "rev-list", "--parents", "-n1", root).split() == [root]
    assert bare(remote, "ls-tree", "-r", "--name-only", BRANCH).split() == [
        "queue/a/0001.json",
        "reserve.json",
    ]
    assert bare(remote, "for-each-ref", "--format=%(refname)").split() == [f"refs/heads/{BRANCH}"]


def test_identical_write_makes_no_commit(tmp_path):
    store = make_store(tmp_path)
    store.write({"k": b"v"}, "first")
    tip = store.snapshot().tip
    assert store.write({"k": b"v"}, "same") is False
    assert store.snapshot(fresh=True).tip == tip


def test_stale_cache_does_not_hide_a_change(tmp_path):
    # With a long ttl store A does not look at the remote; "nothing to do" must still be checked against it.
    remote = make_remote(tmp_path)
    a = make_store(tmp_path, remote=remote, name="a", ttl=3600)
    b = make_store(tmp_path, remote=remote, name="b")
    a.write({"k": b"v"}, "a")
    b.write({"k": b"other"}, "b")
    assert a.read("k") == b"v"  # within ttl: still the old view
    assert a.write({"k": b"v"}, "restore") is True
    assert b.read("k", fresh=True) == b"v"


def test_two_pods_writing_different_keys_both_survive(tmp_path):
    remote = make_remote(tmp_path)
    a = make_store(tmp_path, remote=remote, name="a")
    b = make_store(tmp_path, remote=remote, name="b")
    a.write({"one": b"1"}, "a")
    b.write({"two": b"2"}, "b")  # b has not fetched yet: its push is rejected, then re-applied
    assert a.list(fresh=True) == ["one", "two"]
    assert b.list() == ["one", "two"]


def test_transact_reruns_decide_on_the_new_tip(tmp_path):
    remote = make_remote(tmp_path)
    a = make_store(tmp_path, remote=remote, name="a")
    b = make_store(tmp_path, remote=remote, name="b")
    a.write({"base": b"0"}, "base")
    calls = []

    def decide(snap):
        calls.append(snap.tip)
        if len(calls) == 1:
            b.write({"from-b": b"b"}, "b commits meanwhile")
        return {"from-a": b"a"}, sorted(snap.list())

    assert a.transact(decide, "a") == ["base", "from-b"]
    assert len(calls) == 2 and calls[0] != calls[1]
    assert a.list(fresh=True) == ["base", "from-a", "from-b"]


def test_transact_without_changes_commits_nothing(tmp_path):
    store = make_store(tmp_path)
    store.write({"k": b"v"}, "first")
    tip = store.snapshot().tip
    assert store.transact(lambda snap: ({}, snap.read("k")), "noop") == b"v"
    assert store.snapshot(fresh=True).tip == tip


def test_transact_decide_error_propagates(tmp_path):
    store = make_store(tmp_path)

    def decide(snap):
        raise KeyError("boom")

    with pytest.raises(KeyError):
        store.transact(decide, "x")


def test_retries_exhausted_raises(tmp_path):
    remote = make_remote(tmp_path)
    a = make_store(tmp_path, remote=remote, name="a", retries=2)
    b = make_store(tmp_path, remote=remote, name="b")
    n = []

    def decide(snap):
        n.append(1)
        b.write({f"b{len(n)}": b""}, "b wins every round")
        return {"a": b""}, None

    with pytest.raises(ss.StateError, match="rejected 2 times"):
        a.transact(decide, "a")
    assert len(n) == 2


def test_default_retries_survive_sustained_contention(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    writer = ss.StateStore(str(remote), BRANCH, tmp_path / "writer.git", ttl=0.0)
    rival = make_store(tmp_path, remote=remote, name="rival")
    monkeypatch.setattr(time, "sleep", lambda _: None)
    calls = []

    def decide(snap):
        calls.append(snap.tip)
        if len(calls) <= 25:
            rival.write({f"rival/{len(calls)}": b"v"}, "rival wins")
        return {"writer": str(len(snap.list())).encode()}, len(snap.list())

    assert writer.transact(decide, "writer") == 25
    assert writer.lost_races == 25
    assert writer.read("writer", fresh=True) == b"25"
    assert len(writer.list()) == 26


def test_rejected_push_invalidates_cached_reads_before_retry(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    writer = make_store(tmp_path, remote=remote, name="writer", ttl=3600)
    rival_wins_first_push(monkeypatch, writer, tmp_path, {"rival": b"v"})
    seen: list[bytes | None] = []
    original_sleep = time.sleep

    def read_between_attempts(delay):
        if writer.lost_races and not seen:
            seen.append(None)
            seen[0] = writer.read("rival")
        else:
            original_sleep(delay)

    monkeypatch.setattr(time, "sleep", read_between_attempts)
    writer.write({"writer": b"v"}, "writer")
    assert seen == [b"v"]


def test_failed_push_invalidates_cached_reads(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    writer = make_store(tmp_path, remote=remote, name="writer", ttl=3600)
    rival = make_store(tmp_path, remote=remote, name="rival")
    writer.write({"seed": b"v"}, "seed")
    real_run = writer._run

    def reject_after_remote_update(*args, **kwargs):
        if args[0] == "push":
            rival.write({"rival": b"v"}, "rival")
            return subprocess.CompletedProcess(args, 1, b"", b"permission denied")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(writer, "_run", reject_after_remote_update)
    with pytest.raises(ss.StateError, match="push failed"):
        writer.write({"writer": b"v"}, "writer")
    assert writer.read("rival") == b"v"


def test_failed_local_update_after_push_invalidates_cached_reads(tmp_path, monkeypatch):
    writer = make_store(tmp_path, ttl=3600)
    writer.write({"seed": b"v"}, "seed")
    real_run = writer._run
    failed: list[bool] = []

    def fail_local_update(*args, **kwargs):
        if args[0] == "update-ref" and not failed:
            failed.append(True)
            return subprocess.CompletedProcess(args, 1, b"", b"cannot update ref")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(writer, "_run", fail_local_update)
    with pytest.raises(ss.StateError, match="update-ref"):
        writer.write({"committed": b"v"}, "write")
    assert writer.read("committed") == b"v"


def test_failed_fetch_does_not_make_stale_cache_fresh(tmp_path):
    remote = make_remote(tmp_path)
    writer = make_store(tmp_path, remote=remote, ttl=3600)
    writer.write({"key": b"value"}, "seed")
    remote.rename(tmp_path / "offline.git")
    with pytest.raises(ss.StateError, match="fetch failed"):
        writer.read("key", fresh=True)
    with pytest.raises(ss.StateError, match="fetch failed"):
        writer.read("key")


def test_failed_deleted_ref_cleanup_does_not_make_cache_fresh(tmp_path, monkeypatch):
    remote = make_remote(tmp_path)
    writer = make_store(tmp_path, remote=remote, ttl=3600)
    writer.write({"key": b"value"}, "seed")
    bare(remote, "update-ref", "-d", f"refs/heads/{BRANCH}")
    real_run = writer._run

    def fail_cleanup(*args, **kwargs):
        if args[:2] == ("update-ref", "-d"):
            return subprocess.CompletedProcess(args, 1, b"", b"cannot delete ref")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(writer, "_run", fail_cleanup)
    with pytest.raises(ss.StateError, match="update-ref"):
        writer.read("key", fresh=True)
    with pytest.raises(ss.StateError, match="update-ref"):
        writer.read("key")


def test_rejecting_hook_is_a_state_error_not_a_retry(tmp_path):
    remote = make_remote(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    hook.chmod(0o755)
    store = make_store(tmp_path, remote=remote)
    with pytest.raises(ss.StateError, match="push failed"):
        store.write({"k": b"v"}, "x")


def test_unreachable_remote_is_a_state_error(tmp_path):
    store = make_store(tmp_path, remote=tmp_path / "nonexistent.git")
    with pytest.raises(ss.StateError, match="fetch failed"):
        store.read("k", fresh=True)


def test_concurrent_processes_one_lease(tmp_path):
    remote = make_remote(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        jobs = [
            pool.apply_async(take_production_lease, (str(remote), str(tmp_path / f"base{i}"), f"holder{i}"))
            for i in range(4)
        ]
        holders = [job.get(timeout=60) for job in jobs]
    assert len(set(holders)) == 1
    assert holders[0].startswith("holder")
    commits = bare(remote, "log", "--format=%s", BRANCH).splitlines()
    assert commits == [f"lease {holders[0]}"]


def test_many_processes_writing_different_keys_all_succeed(tmp_path):
    remote = make_remote(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    procs, writes = 8, 10
    with ctx.Pool(procs) as pool:
        jobs = [
            pool.apply_async(
                write_in_process, (str(remote), str(tmp_path / f"cache{i}.git"), f"w{i}", writes)
            )
            for i in range(procs)
        ]
        assert [job.get(timeout=120) for job in jobs] == [writes] * procs
    keys = bare(remote, "ls-tree", "-r", "--name-only", BRANCH).splitlines()
    assert sorted(keys) == sorted(f"w{i}/{n}" for i in range(procs) for n in range(writes))


def test_lost_race_wording_of_github_counts_as_a_lost_race():
    assert "incorrect old value provided" in ss._LOST_RACE


def test_fs_adapter(tmp_path):
    fs = make_store(tmp_path).fs()
    fs.makedirs(f"{ss.ROOT}/queue/a", exist_ok=True)
    fs.pipe_file(f"{ss.ROOT}/queue/a/0001.started", json.dumps({"at": 12.5}).encode())
    fs.pipe_file(f"{ss.ROOT}/queue/a/0001.json", b"not json")
    fs.pipe_file(f"{ss.ROOT}/queue/a/0002.failed", json.dumps({"at": "x"}).encode())
    assert fs.exists(f"{ss.ROOT}/queue/a/0001.json")
    assert fs.cat_file(f"{ss.ROOT}/queue/a/0001.json") == b"not json"
    assert fs.find(f"{ss.ROOT}/queue/a") == [
        f"{ss.ROOT}/queue/a/{n}" for n in ("0001.json", "0001.started", "0002.failed")
    ]
    detail = fs.find(f"{ss.ROOT}/queue", detail=True)
    assert {k.rsplit("/", 1)[1]: v["mtime"] for k, v in detail.items()} == {
        "0001.json": 0.0,
        "0001.started": 12.5,
        "0002.failed": 0.0,
    }
    fs.rm(f"{ss.ROOT}/queue/a/0001.json")
    assert not fs.exists(f"{ss.ROOT}/queue/a/0001.json")


def test_fs_absent_paths_raise_file_not_found(tmp_path):
    fs = make_store(tmp_path).fs()
    with pytest.raises(FileNotFoundError):
        fs.cat_file(f"{ss.ROOT}/nope")
    with pytest.raises(FileNotFoundError):
        fs.rm(f"{ss.ROOT}/nope")
    assert fs.find(f"{ss.ROOT}/nope") == []


def test_key_of():
    assert ss.key_of("state://factory/queue/a/0001.json") == "queue/a/0001.json"
    assert ss.key_of("state://factory") == ""
    with pytest.raises(ValueError):
        ss.key_of("gs://bucket/queue/a")
    with pytest.raises(ValueError):
        ss.key_of("state://factoryx/a")


def test_default_branch():
    assert ss.default_branch("my-proj", "development") == "factory-state/my-proj-development"
    assert ss.default_branch("a b", "x..y") == "factory-state/a-b-x.y"
    assert ss.default_branch("", "") == "factory-state/default"


def test_current_builds_from_env_and_configure_resets(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_STATE_REMOTE", "git@example.com:o/r.git")
    monkeypatch.setenv("AGENT_FACTORY_STATE_BRANCH", "factory-state/x")
    ss.configure(None)
    try:
        store = cast(ss.StateStore, ss.current())  # the env names a git remote: the git store
        assert (store.remote, store.branch) == ("git@example.com:o/r.git", "factory-state/x")
        assert store.cache_dir.parent == tmp_path / ".cache" / "flyte-agent-factory" / "state"
        assert ss.current() is store
        monkeypatch.setenv("AGENT_FACTORY_STATE_BRANCH", "factory-state/y")
        assert cast(ss.StateStore, ss.current()).branch == "factory-state/y"
        mine = make_store(tmp_path)
        ss.configure(mine)
        assert ss.current() is mine
        ss.configure(None)
        assert ss.current() is not mine
    finally:
        ss.configure(None)


def test_current_branch_hook_and_env_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_STATE_REMOTE", "r")
    monkeypatch.delenv("AGENT_FACTORY_STATE_BRANCH", raising=False)
    ss.configure(None)
    try:
        branch = "factory-state/profile"
        assert cast(ss.StateStore, ss.current(branch=branch)).branch == branch
        monkeypatch.setenv("AGENT_FACTORY_STATE_BRANCH", "factory-state/env")
        assert cast(ss.StateStore, ss.current(branch=branch)).branch == "factory-state/env"
    finally:
        ss.configure(None)


GOOD_BRANCHES = [
    "main",
    "factory-state/a-b",
    "a/b/c",
    "a.b",
    "x/y.z",
    "a_b",
    "v1.0",
    "ünï",
    "a@b",
    "a.lockx",
    "@x",
]
BAD_BRANCHES = [
    "",
    "HEAD",
    ".x",
    "x/.y",
    "x.",
    "a/b.lock/c",
    "x.lock",
    "a..b",
    "a@{b",
    "a//b",
    "/a",
    "a/",
    "a b",
    "a\tb",
    "a\x7fb",
    "a~b",
    "a^b",
    "a:b",
    "a?b",
    "a*b",
    "a[b",
    "a\\b",
    "-a",
    "a/-b/.",
]


@pytest.mark.parametrize("name", GOOD_BRANCHES + BAD_BRANCHES)
def test_valid_branch_agrees_with_git(name):
    git = subprocess.run(["git", "check-ref-format", "--branch", name], capture_output=True).returncode == 0
    assert ss.valid_branch(name) is git
    assert git is (name in GOOD_BRANCHES)


def test_current_refuses_a_branch_git_would_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_STATE_REMOTE", "r")
    monkeypatch.setenv("AGENT_FACTORY_STATE_BRANCH", "a..b")
    ss.configure(None)
    with pytest.raises(ss.StateError, match="AGENT_FACTORY_STATE_BRANCH"):
        ss.current()


def corrupt_head(cache: Path) -> None:
    (cache / "HEAD").write_text("ref: refs/he")


def replace_with_file(cache: Path) -> None:
    shutil.rmtree(cache)
    cache.write_text("garbage")


@pytest.mark.parametrize("corrupt", [corrupt_head, replace_with_file])
def test_a_corrupt_cache_is_rebuilt(tmp_path, corrupt):
    remote = make_remote(tmp_path)
    make_store(tmp_path, remote=remote, name="seed").write({"k": b"1"}, "seed")
    store = make_store(tmp_path, remote=remote)
    assert store.read("k") == b"1"
    corrupt(store.cache_dir)
    reopened = make_store(tmp_path, remote=remote)  # a new process: nothing remembers the cache was fine
    assert reopened.read("k", fresh=True) == b"1"
    assert reopened.write({"k2": b"2"}, "after") is True
    assert bare(remote, "show", f"{BRANCH}:k2") == "2"


def test_a_lone_at_sign_is_refused_though_git_expands_it():
    # `check-ref-format --branch @` succeeds because @ means HEAD there; as a stored branch name it is wrong
    assert not ss.valid_branch("@")


def _garble_objects(cache: Path) -> None:
    for path in (cache / "objects").rglob("*"):
        if path.is_file() and len(path.parent.name) == 2:  # a loose object
            path.chmod(0o644)
            path.write_bytes(b"garbage")


@pytest.mark.parametrize("damage", [_garble_objects, lambda cache: shutil.rmtree(cache / "objects")])
def test_a_cache_damaged_while_open_heals_after_one_failed_call(tmp_path, damage):
    """Damage after the store opened its cache costs at most the call that meets it, never later ones."""
    remote = make_remote(tmp_path)
    make_store(tmp_path, remote=remote, name="seed").write({"k": b"1"}, "seed")
    store = make_store(tmp_path, remote=remote, ttl=0.0)
    assert store.read("k") == b"1"
    damage(store.cache_dir)
    try:
        store.read("k")
    except ss.StateError:
        pass  # the call that found the damage may fail
    assert store.read("k") == b"1"
    assert store.write({"k2": b"2"}, "after") is True
    assert bare(remote, "show", f"{BRANCH}:k2") == "2"


def test_a_snapshot_held_across_a_cache_wipe_raises_a_state_error(tmp_path):
    store = make_store(tmp_path)
    store.write({"k": b"1"}, "seed")
    snap = store.snapshot(fresh=True)
    store._wipe()
    with pytest.raises(ss.StateError, match="missing object"):
        snap.read("k")
    assert store.read("k", fresh=True) == b"1"
