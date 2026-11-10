"""One app-local planner consumer; durable intents belong to GitHub, execution to Flyte."""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast, overload

if TYPE_CHECKING:
    from . import _github

log = logging.getLogger(__name__)


class AdmissionInvariant(RuntimeError):
    """Admission cannot proceed without violating single-writer ownership."""


class UnresolvedSubmission(RuntimeError):
    """A submission may have been accepted but cannot yet be found by its label."""


class Intent(TypedDict, total=False):
    correlation: str
    run: str
    done: bool


class Records(Protocol):
    def read(self) -> Intent | Awaitable[Intent]: ...
    def write(self, value: Intent) -> None | Awaitable[None]: ...


class PlannerRun(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def correlation(self) -> str: ...


class Backend(Protocol):
    async def list_runs(self, correlation: str | None = None) -> list[PlannerRun]: ...
    async def submit(self, correlation: str) -> PlannerRun: ...
    async def wait(self, run: PlannerRun) -> None: ...


class LockRun(Protocol):
    @property
    def phase(self) -> object: ...


class Consumer:
    """Coalesce wakeups and adopt surviving planners before submitting another.

    `records` reads/writes the current durable intent. `backend` lists live planners (or all phases
    for a correlation), submits a labelled planner, and waits for its terminal outcome. These are
    separate from scheduling so an HTTP delivery never owns a submission or its cancellation.
    Record methods may return awaitables; remote I/O must be offloaded by the caller.
    """

    def __init__(self, records: Records, backend: Backend) -> None:
        self.records = records
        self.backend = backend
        self.dirty = asyncio.Event()

    def wake(self) -> None:
        """Request a full scan; a wake during a pass schedules one more pass."""
        self.dirty.set()

    @overload
    async def _record(self, method: Literal["read"]) -> Intent: ...

    @overload
    async def _record(self, method: Literal["write"], value: Intent, /) -> None: ...

    async def _record(self, method: str, *args: Intent) -> Intent | None:
        result = getattr(self.records, method)(*args)
        # The overloads restrict dynamic dispatch to the two Records methods.
        return cast(Intent | None, await result if inspect.isawaitable(result) else result)

    async def _finish(self, intent: Intent, run: PlannerRun) -> None:
        intent = {**intent, "run": run.name}
        await self._record("write", intent)
        await self.backend.wait(run)
        await self._record("write", {**intent, "done": True})

    async def recover(self) -> None:
        """Adopt active or ambiguously submitted planners, without inferring failure from absence."""
        intent = await self._record("read")
        live = await self.backend.list_runs()
        if len(live) > 1:
            raise AdmissionInvariant("multiple active planners; operator resolution required")
        unresolved = intent and not intent.get("done", False)
        if unresolved:
            matches = await self.backend.list_runs(intent["correlation"])
            if len(matches) > 1:
                raise AdmissionInvariant(
                    "multiple planners for one correlation; operator resolution required"
                )
            if not matches:
                raise UnresolvedSubmission("planner submission unresolved; operator resolution required")
            run = matches[0]
            if live and live[0].name != run.name:
                raise AdmissionInvariant("active planner conflicts with unresolved intent")
        elif live:
            run = live[0]
            intent = {"correlation": run.correlation, "run": run.name, "done": False}
        else:
            return
        await self._finish(intent, run)

    async def pass_once(self) -> None:
        """Persist before submitting; any submit exception leaves the intent unresolved."""
        intent: Intent = {"correlation": secrets.token_hex(16), "run": "", "done": False}
        await self._record("write", intent)
        run = await self.backend.submit(intent["correlation"])
        await self._finish(intent, run)

    async def run(self) -> None:
        """Recover first and scan on startup, then consume dirty wakeups until shutdown."""
        self.wake()
        while True:
            try:
                await self.recover()
                await self.dirty.wait()
                self.dirty.clear()
                await self.pass_once()
            except AdmissionInvariant:
                log.exception("Planner admission stopped")
                raise
            except Exception:  # the consumer must retain ambiguous intents across API failures
                log.exception("Planner admission blocked; will reconcile before any further submission")
                self.wake()
                await asyncio.sleep(5)


class GitRecord:
    """JSON in a commit message, guarded by GitHub's create/fast-forward ref CAS.

    Like the deployer lock, sibling commits let concurrent writers race without a
    read/delete window. Call read before updating; a lost CAS stops admission.
    """

    def __init__(self, app: _github.GithubApp, repo: str, ref: str, base: str) -> None:
        self.app, self.repo, self.ref, self.base = app, repo, ref, base
        self.parent: str | None = None
        self.path = f"/repos/{repo}/git"

    def read(self) -> dict[str, object]:
        import json

        from . import _github

        try:
            current = self.app._call(self.repo, "GET", f"{self.path}/ref/{self.ref}")
        except _github.GithubError as exc:
            if exc.status != 404:
                raise
            self.parent = None
            return {}
        self.parent = current["object"]["sha"]
        commit = self.app._call(self.repo, "GET", f"{self.path}/commits/{self.parent}")
        # These records also hold planner migrations and work intents, so their keys vary by ref.
        return cast(dict[str, object], json.loads(commit["message"]))

    def write(self, value: Mapping[str, object]) -> None:
        import json

        from . import _github

        source = self.app._call(self.repo, "GET", f"{self.path}/commits/{self.base}")
        commit = self.app._call(
            self.repo,
            "POST",
            f"{self.path}/commits",
            json={
                "message": json.dumps(value),
                "tree": source["tree"]["sha"],
                "parents": [self.parent or self.base],
            },
        )
        try:
            if self.parent:
                self.app._call(
                    self.repo,
                    "PATCH",
                    f"{self.path}/refs/{self.ref}",
                    json={"sha": commit["sha"], "force": False},
                )
            else:
                self.app._call(
                    self.repo,
                    "POST",
                    f"{self.path}/refs",
                    json={"ref": f"refs/{self.ref}", "sha": commit["sha"]},
                )
        except _github.GithubError as exc:
            if exc.status not in (409, 422):
                raise
            raise AdmissionInvariant("admission record changed concurrently; reconcile before retry") from exc
        self.parent = commit["sha"]

    def delete(self) -> None:
        self.app._call(self.repo, "DELETE", f"{self.path}/refs/{self.ref}")
        self.parent = None


def acquire_lock(record: GitRecord, run_name: str, lookup: Callable[[str], LockRun]) -> bool:
    """No expiry: only Flyte's authoritative terminal phase permits reclaim."""
    holder = record.read()
    if holder:
        if holder["run"] == run_name:
            return True
        # This ref is owned by acquire_lock, which writes a string run name.
        run = lookup(cast(str, holder["run"]))
        if str(run.phase).split(".")[-1] not in ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            return False
    try:
        record.write({"run": run_name})
    except AdmissionInvariant:
        return False
    return True
