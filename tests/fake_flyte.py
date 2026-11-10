"""The Flyte remote edge for the queue tests: runs that exist, listings, run creation.

Everything below the edge is real (`_queue` and `_factory` against a `file://` state directory).
The fake behaves as measured on the Union tenant: creating a run under a name that exists REPLACES it
(there is no RunAlreadyExistsError), a listing filters on phase, task and labels like the server, and an
abort reason reads back as None. Who aborted a run is on its root action's details (GetActionDetails of
`a0`), never on the run's (GetRunDetails leaves the action's `result` unset), and an API key is named by
its client id (`application.id.subject`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from connectrpc.code import Code
from connectrpc.errors import ConnectError
from flyteidl2.common import identifier_pb2, identity_pb2  # type: ignore[import-untyped]  # no py.typed marker
from flyteidl2.workflow import run_definition_pb2  # type: ignore[import-untyped]  # no py.typed marker


class _Status:
    """`Run.pb2.action.status` as the reconciler reads it: an `end_time` only once the run has ended."""

    def __init__(self):
        self.ended: datetime | None = None

    def HasField(self, name):  # the protobuf method's name
        return name == "end_time" and self.ended is not None

    @property
    def end_time(self):
        return SimpleNamespace(ToDatetime=lambda: self.ended.replace(tzinfo=None))  # type: ignore[union-attr]  # only read after `ended` is set


def ended_at(run, when: datetime) -> None:
    """Give `run` the end time a terminal run carries."""
    run.pb2.action.status.ended = when


def _abort_info(aborted_by: str):
    """Return the root action's `abort_info` as the server returns it, from `FakeRun.aborted_by`."""
    if aborted_by == "user":
        who = identity_pb2.EnrichedIdentity(
            user=identity_pb2.User(id=identifier_pb2.UserIdentifier(subject="u1"))
        )
    elif aborted_by:
        who = identity_pb2.EnrichedIdentity(
            application=identity_pb2.Application(id=identifier_pb2.ApplicationIdentifier(subject=aborted_by))
        )
    else:
        who = None
    return run_definition_pb2.AbortInfo(reason="", aborted_by=who)


class FakeRun:
    def __init__(
        self,
        name,
        task,
        phase="RUNNING",
        labels=None,
        inputs=None,
        outputs=None,
        abort_reason="",
        seq=0,
        aborted_by="",
    ):
        self.name = name
        self.task = task
        self.phase = phase
        # Where the real `flyte.remote.Run` keeps them: it has no `.labels` attribute.
        self.pb2 = SimpleNamespace(labels=dict(labels or {}), action=SimpleNamespace(status=_Status()))
        self.inputs_ = inputs or {}
        self.outputs_ = outputs
        self.abort_reason = abort_reason  # what abort() was given; the server does not hand it back
        # "user" (a person at the console), an API key's client id, or "" where the server names nobody
        self.aborted_by = aborted_by
        # one second apart and in creation order, as `Run.action.start_time` is
        self.action = SimpleNamespace(
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seq)
        )
        self.url = f"https://console.example/runs/{name}"
        self.report = ""  # None: a run that never wrote one
        self.task_version = ""  # the version of the task it was created from, fixed for the run's life

    def get_report(self):
        if self.report is None:
            raise RuntimeError("No report is available")  # as `flyte.remote.Run.get_report` does
        return self.report

    def to_dict(self):
        return {"action": {"metadata": {"task": {"id": {"name": self.task}}}}}

    def abort(self, reason):
        self.phase, self.abort_reason = "ABORTED", reason

    def inputs(self):
        return self.inputs_

    def outputs(self):
        if self.outputs_ is None:
            raise RuntimeError("no outputs")
        return self.outputs_

    def details(self):
        # GetRunDetails never sets the action's `result` oneof: neither the reason nor who aborted.
        return SimpleNamespace(action_details=SimpleNamespace(abort_info=SimpleNamespace(reason=None)))

    def root_details(self) -> SimpleNamespace:
        """`ActionDetails` of `a0`: `abort_info` only once the run is aborted, as the SDK's property."""
        return SimpleNamespace(abort_info=_abort_info(self.aborted_by) if self.phase == "ABORTED" else None)


class FakeTask(str):
    version: str | None = None

    def override(self, **kwargs):
        return self


class FakeFlyte:
    """Install with `FakeFlyte(monkeypatch, factory)`; `runs` holds every run in creation order."""

    def __init__(self, monkeypatch, factory, *, fake_headroom=True):
        self.factory = factory
        self.runs: dict[str, FakeRun] = {}
        self.created: list[dict[str, Any]] = []  # {"name", "task", "context", "inputs"} of every create call
        self.fail_create: list[Exception] = []  # raised by the next create calls, in order
        self.before_create = None  # called once, then cleared, just before the next run exists
        self.after_create = None  # called once, then cleared, just after the next run exists
        self.versions: dict[str, int] = {}  # deployed version of each task; see `deploy`
        self.seq = 0
        self.listings = 0  # Run.listall calls
        monkeypatch.setattr(factory.remote.Run, "listall", self._listall)
        monkeypatch.setattr(factory.remote.Run, "get", self._get)
        monkeypatch.setattr(factory.remote.ActionDetails, "get", self._action_details)
        monkeypatch.setattr(factory.remote.Task, "get", self._task_get)
        monkeypatch.setattr(factory.flyte, "with_runcontext", self._with_runcontext)
        if fake_headroom:
            from flyte_agent_factory import _factory_meters

            monkeypatch.setattr(_factory_meters, "slot_headroom", lambda slots: {})

    def deploy(self, task) -> None:
        """A new deploy of `task`: later `Task.get` calls resolve the new version, live runs keep theirs."""
        self.versions[task] = self.versions.get(task, 1) + 1

    def _task_get(self, name, **kw):
        task = FakeTask(name)
        task.version = f"v{self.versions.get(name, 1)}"  # what `auto_version="latest"` resolves to
        return task

    def add(self, name, task, **kw) -> FakeRun:
        self.seq += 1
        self.runs.pop(name, None)  # a replaced run is a new run: it goes last
        self.runs[name] = FakeRun(name, task, seq=self.seq, **kw)
        return self.runs[name]

    def _listall(
        self, *, in_phase=None, task_name=None, with_labels=None, with_label_keys=None, limit=100, **_
    ):
        self.listings += 1
        found = [
            r
            for r in reversed(list(self.runs.values()))  # newest first
            if (not in_phase or r.phase in in_phase)
            and (not task_name or r.task == task_name)
            and all(r.pb2.labels.get(k) == v for k, v in (with_labels or {}).items())
            and all(k in r.pb2.labels for k in (with_label_keys or []))
        ]
        return found[:limit]

    def _get(self, name):
        if name not in self.runs:
            raise ConnectError(Code.NOT_FOUND, f"run {name} not found")
        return self.runs[name]

    def _action_details(self, uri=None, /, run_name=None, name=None):
        if run_name not in self.runs or name != "a0":
            raise ConnectError(Code.NOT_FOUND, f"action {run_name}/{name} not found")
        return self.runs[run_name].root_details()

    def _with_runcontext(self, **context):
        def run(task, **inputs):
            if self.fail_create:
                raise self.fail_create.pop(0)
            name = context.get("name") or f"auto{len(self.runs)}"
            hook, self.before_create = self.before_create, None
            if hook:
                hook()
            self.created.append({"name": name, "task": task, "context": context, "inputs": inputs})
            run = self.add(name, task, labels=context.get("labels"), inputs=inputs)
            run.task_version = getattr(task, "version", "")
            hook, self.after_create = self.after_create, None
            if hook:
                hook()
            return run

        return SimpleNamespace(run=run)
