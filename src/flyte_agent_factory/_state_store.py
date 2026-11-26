"""The factory's state: files on an orphan branch of the workspace repository, with compare-and-swap writes.

See `docs/state-store.md`. Every write is one commit whose parent is the tip the writer read, pushed
without force; git accepts a non-forced push only as a fast-forward, so a push from a stale tip is
rejected and the writer fetches, re-decides and tries again. That ref update is the only
compare-and-swap there is, and it is all the dispatch lease needs (`transact`).

The local copy is a bare repository used as a cache, and commits are built without a worktree (a temporary
index and `commit-tree`), so a pod needs no checkout and no git identity. Plain `git` subprocesses: the
alternatives (dulwich, pygit2) would be a new dependency to carry a credential helper and ssh config that
`_git.install_deploy_key` already gives the `git` binary. Everything is synchronous, like `_queue`, so
plain functions and tasks with a running event loop can both call it.

In a project pod the project's clone installs the deploy key first, so the store's own call leaves it alone.
"""

from __future__ import annotations

import builtins
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, overload

from flyte_agent_factory import _git

if TYPE_CHECKING:
    from flyte_agent_factory import _profile_state

T = TypeVar("T")

#: Every state path is ROOT + "/" + key, the shape `_queue` already used for bucket URIs.
ROOT = "state://factory"
_TIMEOUT = 60
#: What `git write-tree` gives an empty index: the tree of a store with no files.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
#: Git's own spelling of a fetch for a branch the remote does not have yet: an empty store, not a failure.
_NO_REMOTE_REF = "couldn't find remote ref"
#: A push that lost the race. Anything else (a hook, no permission, no network) is a real failure.
_LOST_RACE = (
    "non-fast-forward",
    "fetch first",
    "stale info",
    "failed to update ref",
    "cannot lock ref",
    "incorrect old value provided",  # GitHub's server-side wording for the same thing
)
_IDENTITY = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
#: Writers of different files still collide on the tip, so many writers need many rounds. The sleep before
#: attempt n is uniform in [0, min(_BACKOFF_CAP, _BACKOFF * 2**(n-2))] ("full jitter"): with the default
#: `_RETRIES` that is at most about 220 s of sleeping. A wider cap spreads writers out when git commands
#: take longer under load; the finite retry limit still lets callers report sustained contention.
_RETRIES = 50
_BACKOFF = 0.05  # seconds
_BACKOFF_CAP = 5.0  # seconds
#: git's words for a cache whose objects or refs are damaged (`_locked` then clears it).
_CORRUPT = (
    "corrupt",
    "bad object",
    "missing object",
    "unable to read",
    "invalid sha1 pointer",
    "not a git repository",
    "loose object",
    "bad tree object",
)


class StateError(RuntimeError):
    """A git command failed, or compare-and-swap retries ran out."""


def key_of(path: str) -> str:
    """Return the key of a state path; ValueError for a path outside ROOT."""
    if path.rstrip("/") == ROOT:
        return ""
    if not path.startswith(ROOT + "/"):
        raise ValueError(f"{path!r} is not under {ROOT}")
    return path[len(ROOT) + 1 :].rstrip("/")


def _check_key(key: str) -> str:
    """Return `key` if it is a relative posix path with no empty, `.` or `..` component."""
    if not key or key.startswith("/") or any(p in ("", ".", "..") for p in key.split("/")) or "\0" in key:
        raise ValueError(f"invalid state key {key!r}")
    return key


def default_branch(project: str, domain: str) -> str:
    """Return `factory-state/<project>-<domain>`, made a valid git ref name."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{project}-{domain}")
    name = re.sub(r"\.{2,}", ".", name).strip(".-") or "default"
    if name.endswith(".lock"):  # a ref component may not end in it
        name += "-"
    return f"factory-state/{name}"


_BRANCH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def valid_branch(name: str) -> bool:
    """Return whether git accepts `name` as a branch name: `git check-ref-format --branch`, without a process.

    Written out because the profile loader checks it on every load, and a name git refuses would only fail
    later, at the first push. `@{-1}` (the previous branch) is not a name and is refused; nor is `HEAD`.
    """
    if not name or name in ("@", "HEAD") or name.startswith("-") or name.endswith(("/", ".")):
        return False
    if _BRANCH_FORBIDDEN.search(name) or ".." in name or "@{" in name:
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock") for part in name.split("/"))


def _redact(text: str) -> str:
    """Hide credentials in a URL (`https://token@host/...`) before it reaches an error message."""
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", text)


class SnapshotView(Protocol):
    """What a `decide` function reads: the files as of one moment, never changed by later writes."""

    tip: str  # names the state read: a commit sha for git, a digest of the listing for objects

    def read(self, key: str) -> bytes | None: ...
    def read_many(self, keys: builtins.list[str]) -> dict[str, bytes]: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str = "") -> builtins.list[str]: ...


class Store(Protocol):
    """What callers use of the state: files by key, snapshots, and compare-and-swap transactions.

    Implemented by `StateStore` (git), `_state_bucket.BucketStore` (an object store) and the volume backend.
    `fresh` asks for a read no older than the call; a backend that always reads current state ignores it.
    """

    #: Whether `transact` can delete a key conditionally. Object stores cannot, so a caller that deletes a key
    #: contended between writers (`_queue.release_lease`) writes a tombstone there instead.
    conditional_delete: bool

    def read(self, key: str, *, fresh: bool = False) -> bytes | None: ...
    def exists(self, key: str, *, fresh: bool = False) -> bool: ...
    def list(self, prefix: str = "", *, fresh: bool = False) -> builtins.list[str]: ...
    def snapshot(self, *, fresh: bool = False) -> SnapshotView: ...
    def write(self, changes: Mapping[str, bytes | None], message: str) -> bool: ...
    def transact(
        self, decide: Callable[[SnapshotView], tuple[Mapping[str, bytes | None], T]], message: str
    ) -> T: ...
    def fs(self) -> StateFS: ...
    def recent_changes(self, limit: int) -> builtins.list[dict[str, object]]:
        """Return the newest changes, newest first: dicts with `at` (epoch seconds), `message` and `keys`."""
        ...


class Snapshot:
    """The files of one commit. Immutable: later writes do not change what it returns."""

    def __init__(self, store: StateStore, tip: str, blobs: Mapping[str, str]) -> None:
        self._store = store
        self.tip = tip  # the commit sha, "" for a store with no branch yet
        self._blobs = blobs  # key -> blob sha

    def read(self, key: str) -> bytes | None:
        """Return the file's content, or None when absent."""
        sha = self._blobs.get(_check_key(key))
        return None if sha is None else self._store._cat([sha])[0]

    def read_many(self, keys: builtins.list[str]) -> dict[str, bytes]:
        """Return the content of each of `keys` that exists, in one git process."""
        present = [k for k in keys if k in self._blobs]
        return dict(zip(present, self._store._cat([self._blobs[k] for k in present]), strict=True))

    def exists(self, key: str) -> bool:
        return _check_key(key) in self._blobs

    def list(self, prefix: str = "") -> builtins.list[str]:
        """Return the sorted keys under `prefix`: "queue/a" matches "queue/a/x", not "queue/ab/x"."""
        if not prefix.strip("/"):
            return sorted(self._blobs)
        prefix = _check_key(prefix.rstrip("/"))
        return sorted(k for k in self._blobs if k == prefix or k.startswith(prefix + "/"))


class StateStore:
    """One branch of one remote, cached in the bare repository `cache_dir`."""

    conditional_delete = True  # a delete is a commit on the tip, like any write

    def __init__(
        self,
        remote: str,
        branch: str,
        cache_dir: Path,
        *,
        ttl: float = 1.0,
        retries: int = _RETRIES,
        author: str = "flyte-agent-factory",
    ) -> None:
        self.remote = remote
        self.branch = branch
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.retries = retries
        self._author = author
        # A commit is named by its parent, tree, message, identity and second. Two stores that make the same
        # change on the same tip in the same second would otherwise make the *same commit*, both pushes
        # would "succeed" (the second is an up-to-date no-op), and one of two increments would be lost.
        self._writer = secrets.token_hex(4)
        self._ref = f"refs/remotes/origin/{branch}"
        self._lock = threading.RLock()
        self._depth = 0  # how many times this thread holds the lock; only the outermost takes the file lock
        self._fetched = float("-inf")  # time.monotonic() of the last fetch
        self._snap: Snapshot | None = None
        self._ready = False
        # Read by the benchmark (`ops/state_bench.py`); they change nothing about how a write behaves.
        self.pushes = 0  # commits this store pushed
        self.lost_races = 0  # pushes rejected because another writer moved the branch first

    # -- reads ---------------------------------------------------------------------------------------

    def read(self, key: str, *, fresh: bool = False) -> bytes | None:
        return self.snapshot(fresh=fresh).read(key)

    def exists(self, key: str, *, fresh: bool = False) -> bool:
        return self.snapshot(fresh=fresh).exists(key)

    def list(self, prefix: str = "", *, fresh: bool = False) -> builtins.list[str]:
        return self.snapshot(fresh=fresh).list(prefix)

    def snapshot(self, *, fresh: bool = False) -> Snapshot:
        """Return the branch tip as one consistent commit; fetch first if `fresh` or older than `ttl`."""
        with self._locked():
            if fresh or time.monotonic() - self._fetched > self.ttl:
                self._fetch()
            tip = self._tip()
            if self._snap is None or self._snap.tip != tip:
                self._snap = Snapshot(self, tip, self._ls_tree(tip))
            return self._snap

    # -- writes --------------------------------------------------------------------------------------

    def write(self, changes: Mapping[str, bytes | None], message: str) -> bool:
        """Commit `changes` (None deletes); return False when they change nothing."""
        for key in changes:
            _check_key(key)
        # A plain write re-applies unchanged on a new tip, so it is a transaction that ignores the tip.
        # Its first look at the tip may be up to `ttl` old: if that shows nothing to do, `_cas` looks again.
        return self._cas(lambda _snap: (changes, True), message, fresh=False)[1]

    def transact(
        self, decide: Callable[[SnapshotView], tuple[Mapping[str, bytes | None], T]], message: str
    ) -> T:
        """Commit what `decide` returns against a fresh tip; if the push loses, call `decide` again.

        `decide` must be pure apart from what it returns: it can run several times.
        """
        return self._cas(decide, message, fresh=True)[0]

    def fs(self) -> StateFS:
        return StateFS(self)

    def recent_changes(self, limit: int) -> builtins.list[dict[str, object]]:
        """Return the newest `limit` commits of the branch, newest first, from `git log`."""
        with self._locked():
            self.snapshot(fresh=True)  # fetch; the log is of the cached branch
            tip = self._tip()
            if not tip or limit < 1:
                return []
            # \x01 starts a commit, \x02 splits time from subject; --name-only follows, one key a line.
            out = self._git("log", f"--max-count={limit}", "--name-only", "--format=%x01%ct%x02%s", tip)
        changes = []
        for chunk in out.decode(errors="replace").split("\x01")[1:]:
            head, _, names = chunk.partition("\n")
            at, _, message = head.partition("\x02")
            changes.append(
                {"at": float(at), "message": message, "keys": sorted(n for n in names.split("\n") if n)}
            )
        return changes

    def _cas(
        self,
        decide: Callable[[SnapshotView], tuple[Mapping[str, bytes | None], T]],
        message: str,
        *,
        fresh: bool,
    ) -> tuple[T, bool]:
        """Return `decide`'s result and whether a commit was pushed."""
        for attempt in range(1, self.retries + 1):
            if attempt > 1:
                time.sleep(random.uniform(0, min(_BACKOFF_CAP, _BACKOFF * 2 ** (attempt - 2))))
            with self._locked():
                snap = self.snapshot(fresh=fresh or attempt > 1)
                changes, result = decide(snap)
                for key in changes:
                    _check_key(key)
                commit = self._commit(snap.tip, changes, message)
                if commit is None:
                    if fresh or attempt > 1 or self.ttl <= 0:
                        return result, False
                    fresh = True  # decided on a tip up to `ttl` old: make sure before saying "nothing to do"
                    continue
                pushed = False
                try:
                    pushed = self._push(commit)
                finally:
                    if not pushed:
                        self._fetched = float("-inf")
                if pushed:
                    self.pushes += 1
                    fetched = self._fetched
                    self._fetched = float("-inf")
                    self._git("update-ref", self._ref, commit)
                    self._fetched = fetched
                    return result, True
                self.lost_races += 1
                if attempt == self.retries:
                    self._fetch()  # Preserve the winner's tip if the retry budget is exhausted.
        raise StateError(f"{self.branch}: push rejected {self.retries} times; giving up")

    def _commit(self, tip: str, changes: Mapping[str, bytes | None], message: str) -> str | None:
        """Build the commit for `changes` on `tip` without a worktree; None when the tree would not change."""
        with tempfile.TemporaryDirectory(dir=self.cache_dir) as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if tip:
                self._git("read-tree", tip, env=env)
            for key, body in sorted(changes.items()):
                if body is None:
                    # Mode 0 removes the entry; `--force-remove` would need a worktree, and there is none.
                    self._git(
                        "update-index", "--index-info", input=f"0 {'0' * 40}\t{key}\n".encode(), env=env
                    )
                    continue
                blob = self._git("hash-object", "-w", "--stdin", input=body).decode().strip()
                # --replace: a file may take the place of a directory of files and the other way round.
                self._git(
                    "update-index", "--add", "--replace", "--cacheinfo", f"100644,{blob},{key}", env=env
                )
            tree = self._git("write-tree", env=env).decode().strip()
        if tree == (self._git("rev-parse", f"{tip}^{{tree}}").decode().strip() if tip else _EMPTY_TREE):
            return None
        identity = {
            "GIT_AUTHOR_NAME": self._author,
            "GIT_AUTHOR_EMAIL": f"{self._author}+{self._writer}@users.noreply.invalid",
            "GIT_COMMITTER_NAME": self._author,
            "GIT_COMMITTER_EMAIL": f"{self._author}+{self._writer}@users.noreply.invalid",
        }
        parent = ["-p", tip] if tip else []  # no parent: the first commit makes the branch an orphan
        return self._git("commit-tree", tree, *parent, "-m", message, env=identity).decode().strip()

    def _push(self, commit: str) -> bool:
        """Push `commit` as the new tip, without force. False when someone else moved the branch first."""
        _git.install_deploy_key(self.remote)
        # No `+` and no --force: a non-fast-forward rejection IS the compare-and-swap.
        proc = self._run("push", "--quiet", "--porcelain", "origin", f"{commit}:refs/heads/{self.branch}")
        if proc.returncode == 0:
            return True
        out = (proc.stdout + proc.stderr).decode(errors="replace")
        if any(marker in out for marker in _LOST_RACE):
            return False
        raise StateError(self._failure("push", out))

    # -- the local cache -----------------------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialise the cache across threads (a lock) and processes of one pod (an flock on a file in it)."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            if self.cache_dir.is_file():  # garbage where the directory should be; it is only a cache
                self.cache_dir.unlink()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_dir / "state.lock", "w") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    self._init()
                    yield
                except StateError as exc:
                    if any(sign in str(exc) for sign in _CORRUPT):
                        # The cache lost or garbled an object after it was opened. This call has failed
                        # already; clearing the cache makes the next one fetch afresh rather than fail too.
                        self._wipe()
                    raise
                finally:
                    self._depth = 0
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _init(self) -> None:
        if self._ready and (self.cache_dir / "HEAD").exists():  # another process may have cleared it
            return
        if not self._valid_cache():
            self._wipe()
            self._git("init", "--bare", "--quiet", str(self.cache_dir))
        self._git("config", "remote.origin.url", self.remote)
        self._ready = True

    def _wipe(self) -> None:
        """Empty the cache directory, to be re-initialised by the next `_init`.

        Only a cache (the remote is the truth), so starting over loses nothing. state.lock is kept:
        other processes of this pod may be waiting on it.
        """
        for child in self.cache_dir.iterdir():
            if child.name != "state.lock":
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        self._ready, self._snap, self._fetched = False, None, float("-inf")

    def _valid_cache(self) -> bool:
        """Return whether `cache_dir` is a usable bare repository; False for a missing or corrupt one."""
        if not (self.cache_dir / "HEAD").exists():
            return False
        proc = self._run("rev-parse", "--git-dir")
        return proc.returncode == 0 and (self.cache_dir / "objects").is_dir()

    def _fetch(self) -> None:
        self._fetched = float("-inf")
        _git.install_deploy_key(self.remote)
        spec = f"+refs/heads/{self.branch}:{self._ref}"
        proc = self._run("fetch", "--no-tags", "--quiet", "origin", spec)
        if proc.returncode == 0:
            self._fetched = time.monotonic()
            return
        err = proc.stderr.decode(errors="replace")
        if _NO_REMOTE_REF not in err:
            raise StateError(self._failure("fetch", err))
        self._git("update-ref", "-d", self._ref)  # the branch is gone (or was never made): an empty store
        self._fetched = time.monotonic()

    def _tip(self) -> str:
        proc = self._run("rev-parse", "--verify", "--quiet", f"{self._ref}^{{commit}}")
        return proc.stdout.decode().strip() if proc.returncode == 0 else ""

    def _ls_tree(self, tip: str) -> dict[str, str]:
        if not tip:
            return {}
        out = self._git("ls-tree", "-r", "-z", tip)
        blobs = {}
        for entry in out.split(b"\0"):
            if entry:
                meta, _, path = entry.partition(b"\t")
                blobs[path.decode()] = meta.split()[2].decode()
        return blobs

    def _cat(self, shas: builtins.list[str]) -> builtins.list[bytes]:
        """Return the content of each blob, in order, from one `git cat-file --batch`."""
        if not shas:
            return []
        with self._locked():
            out = self._git("cat-file", "--batch", input=("\n".join(shas) + "\n").encode())
        bodies, pos = [], 0
        for sha in shas:
            end = out.index(b"\n", pos)
            header = out[pos:end].split()
            if len(header) != 3:  # `<sha> missing`: the cache lost it (cleared since this snapshot was taken)
                raise StateError(f"{self.branch}: missing object {sha} in the cache; read again")
            size = int(header[2])
            bodies.append(out[end + 1 : end + 1 + size])
            pos = end + size + 2
        return bodies

    def _run(
        self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        # os.environ, not `_secret_env.child_env()`: that one loads the profile (a file read that can fail)
        # to hide credentials from a session, and this runs in the supervisor, where `install_deploy_key`
        # has already moved the key out of the environment into the file `GIT_SSH_COMMAND` names.
        full = {**os.environ, **_IDENTITY, **(env or {})}
        try:
            return subprocess.run(
                ["git", "--git-dir", str(self.cache_dir), *args],
                input=input,
                capture_output=True,
                env=full,
                timeout=_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise StateError(f"git {args[0]} timed out after {_TIMEOUT}s") from exc

    def _git(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None) -> bytes:
        proc = self._run(*args, input=input, env=env)
        if proc.returncode != 0:
            raise StateError(self._failure(args[0], proc.stderr.decode(errors="replace")))
        return proc.stdout

    def _failure(self, command: str, output: str) -> str:
        return (
            f"git {command} failed for {_redact(self.remote)} {self.branch}: {_redact(output.strip()[-500:])}"
        )


class StateFS:
    """The filesystem calls the queue makes, over a Store, for full paths that start with ROOT."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def makedirs(self, path: str, *, exist_ok: bool) -> None:
        """Do nothing: git has no empty directories."""

    def pipe_file(self, path: str, body: bytes) -> None:
        self._store.write({key_of(path): body}, f"write {key_of(path)}")

    @overload
    def find(self, path: str, *, detail: Literal[True]) -> dict[str, dict[str, object]]: ...
    @overload
    def find(self, path: str, *, detail: Literal[False] = False) -> list[str]: ...
    def find(self, path: str, *, detail: bool = False) -> list[str] | dict[str, dict[str, object]]:
        snap = self._store.snapshot()
        keys = snap.list(key_of(path))
        paths = [f"{ROOT}/{k}" for k in keys]
        if not detail:
            return paths
        bodies = snap.read_many(keys)
        return {f"{ROOT}/{k}": {"mtime": _at(bodies[k])} for k in keys}

    def cat_file(self, path: str) -> bytes:
        body = self._store.read(key_of(path))
        if body is None:
            raise FileNotFoundError(path)
        return body

    def rm(self, path: str) -> None:
        key = key_of(path)

        def decide(snap: SnapshotView) -> tuple[Mapping[str, bytes | None], None]:
            if not snap.exists(key):
                raise FileNotFoundError(path)
            return {key: None}, None

        self._store.transact(decide, f"remove {key}")

    def exists(self, path: str) -> bool:
        return self._store.exists(key_of(path))


def _at(body: bytes) -> float:
    """Return the `at` of a JSON object (queue markers carry it), else 0.0."""
    try:
        at = json.loads(body).get("at")
    except (ValueError, AttributeError):
        return 0.0
    return float(at) if isinstance(at, int | float) and not isinstance(at, bool) else 0.0


_configured: Store | None = None
_built: dict[tuple[str, ...], Store] = {}
_built_lock = threading.Lock()


def configure(store: Store | None) -> None:
    """Set the process-wide store, or clear it (and the ones `current` built) with None."""
    global _configured
    with _built_lock:
        _configured = store
        _built.clear()


def current(*, branch: str = "") -> Store:
    """Return the configured store, else one built from the environment and the profile.

    The git backend: the environment wins over `branch`, which wins over the profile's `[state] branch`.
    A profile with `[state] bucket` or `volume` selects that backend instead, unless
    `AGENT_FACTORY_STATE_REMOTE` is set: that override is for git (operator tools, tests) and is never
    forwarded to a pod.
    """
    if _configured is not None:
        return _configured
    # Lazily: `_profile` imports a lot of the package, and this module must be importable by all of it.
    from flyte_agent_factory import _profile

    remote = os.environ.get("AGENT_FACTORY_STATE_REMOTE", "")
    from_env = os.environ.get("AGENT_FACTORY_STATE_BRANCH", "")
    if from_env and not valid_branch(from_env):
        raise StateError(f"AGENT_FACTORY_STATE_BRANCH is not a valid git branch name: {from_env!r}")
    name = from_env or branch
    profile = None
    if not remote or not name:
        profile = _profile.current()
        if profile is None:
            raise StateError("no state remote: set AGENT_FACTORY_STATE_REMOTE or select a profile")
        if profile.state.backend != "git" and not remote:
            return _object_store(profile.state)
        remote = remote or profile.repo
        name = name or profile.state.branch or default_branch(profile.backend.project, profile.backend.domain)
    return git_store(remote, name)


def git_store(remote: str, name: str) -> Store:
    """Return the git store of branch `name` on `remote`, one per process (and cache directory)."""
    base = os.environ.get("AGENT_FACTORY_BASE") or os.environ.get("HOME") or "/tmp"
    with _built_lock:
        if (remote, name, base) not in _built:
            digest = hashlib.sha1((remote + name).encode(), usedforsecurity=False).hexdigest()[:12]
            cache = Path(base) / ".cache" / "flyte-agent-factory" / "state" / f"{digest}.git"
            _built[remote, name, base] = StateStore(remote, name, cache)
        return _built[remote, name, base]


def _object_store(state: _profile_state.State) -> Store:
    """Return the bucket or volume store `[state]` names (one per process and setting)."""
    from flyte_agent_factory import _profile_state, _state_bucket, _state_volume

    key = (state.backend, state.bucket, state.endpoint, state.region, state.volume)
    with _built_lock:
        if key not in _built:
            if state.backend == "bucket":
                # The bucket's own check, again at the point of use: the Flyte task this runs in knows its
                # raw-data prefix even when the profile does not say which that is.
                _profile_state.refuse_flyte_bucket(
                    state.bucket, (*state.flyte_bucket, *_profile_state.runtime_flyte_prefixes())
                )
                _built[key] = _state_bucket.open_store(
                    state.bucket,
                    endpoint=state.endpoint,
                    region=state.region,
                    retention_days=state.retention_days,
                )
            else:
                _built[key] = _state_volume.open_store(state.volume, retention_days=state.retention_days)
        return _built[key]
