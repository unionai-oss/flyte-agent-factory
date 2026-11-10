"""Single-writer scheduling, including the submission windows Flyte cannot deduplicate."""

import asyncio
from types import SimpleNamespace

import pytest

from flyte_agent_factory import _admission


class Records:
    def __init__(self):
        self.value = {}

    def read(self):
        return dict(self.value)

    def write(self, value):
        self.value = dict(value)


class Backend:
    def __init__(self):
        self.runs = []
        self.submissions = []
        self.finished = asyncio.Event()
        self.entered = asyncio.Event()
        self.ambiguous = False

    async def list_runs(self, correlation=None):
        return [r for r in self.runs if correlation is None or r.correlation == correlation]

    async def submit(self, correlation):
        self.submissions.append(correlation)
        run = SimpleNamespace(name=f"planner-{len(self.runs)}", correlation=correlation)
        self.runs.append(run)
        if self.ambiguous:
            raise TimeoutError("accepted but acknowledgement lost")
        return run

    async def wait(self, run):
        self.entered.set()
        await self.finished.wait()
        self.runs.remove(run)


async def test_burst_waits_for_terminal_and_runs_one_more_pass():
    records, backend = Records(), Backend()
    consumer = _admission.Consumer(records, backend)
    task = asyncio.create_task(consumer.run())
    try:
        consumer.wake()
        await asyncio.wait_for(backend.entered.wait(), 30)
        for _ in range(100):
            consumer.wake()
        await asyncio.sleep(0)
        assert len(backend.submissions) == 1
        backend.finished.set()
        for _ in range(100):
            if len(backend.submissions) == 2 and records.value.get("done"):
                break
            await asyncio.sleep(0)
        assert len(backend.submissions) == 2
        assert records.value["done"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_restart_adopts_queued_planner_before_submitting():
    records, backend = Records(), Backend()
    records.write({"correlation": "original", "run": "survivor", "done": False})
    survivor = SimpleNamespace(name="survivor", correlation="original", phase="QUEUED")
    backend.runs.append(survivor)
    consumer = _admission.Consumer(records, backend)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(backend.entered.wait(), 30)
        assert backend.submissions == []
        backend.finished.set()
        for _ in range(100):
            if backend.submissions:
                break
            await asyncio.sleep(0)
        assert len(backend.submissions) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_invisible_ambiguous_submission_never_expires():
    records, backend = Records(), Backend()
    records.write({"correlation": "invisible", "run": "", "done": False})
    consumer = _admission.Consumer(records, backend)
    for _ in range(3):
        with pytest.raises(_admission.UnresolvedSubmission):
            await consumer.recover()
    assert backend.submissions == []
    assert records.value["correlation"] == "invisible"
    backend.runs.append(SimpleNamespace(name="late", correlation="invisible"))
    backend.finished.set()
    await consumer.recover()
    assert records.value["done"]


async def test_accepted_but_unacknowledged_submission_is_adopted():
    records, backend = Records(), Backend()
    backend.ambiguous = True
    consumer = _admission.Consumer(records, backend)
    with pytest.raises(TimeoutError):
        await consumer.pass_once()
    assert records.value["correlation"] == backend.submissions[0]
    assert not records.value["done"]
    backend.finished.set()
    await consumer.recover()
    assert len(backend.submissions) == 1
    assert records.value["done"]


async def test_multiple_active_planners_stop_admission():
    backend = Backend()
    backend.runs = [SimpleNamespace(name=n, correlation=n) for n in ("one", "two")]
    consumer = _admission.Consumer(Records(), backend)
    with pytest.raises(_admission.AdmissionInvariant, match="multiple active planners"):
        await consumer.recover()
    assert backend.submissions == []


async def test_failed_intent_write_prevents_submission():
    class BrokenRecords(Records):
        def write(self, value):
            raise OSError("GitHub unavailable")

    backend = Backend()
    consumer = _admission.Consumer(BrokenRecords(), backend)
    with pytest.raises(OSError):
        await consumer.pass_once()
    assert backend.submissions == []


async def test_adopts_surviving_planner_when_acknowledgement_record_was_lost():
    records, backend = Records(), Backend()
    backend.runs.append(SimpleNamespace(name="orphan", correlation="original"))
    backend.finished.set()
    await _admission.Consumer(records, backend).recover()
    assert backend.submissions == []
    assert records.value == {"correlation": "original", "run": "orphan", "done": True}


async def test_conflicting_survivor_and_intent_fail_closed():
    records, backend = Records(), Backend()
    records.write({"correlation": "older", "run": "", "done": False})
    backend.runs.append(SimpleNamespace(name="newer", correlation="newer"))
    with pytest.raises(_admission.UnresolvedSubmission):
        await _admission.Consumer(records, backend).recover()
    assert backend.submissions == []


async def test_submission_acknowledgement_write_failure_does_not_resubmit(monkeypatch):
    class FailingAcknowledgement(Records):
        def write(self, value):
            if value.get("run") and not self.value.get("run"):
                raise OSError("record write failed after submit")
            super().write(value)

    records, backend = FailingAcknowledgement(), Backend()
    consumer = _admission.Consumer(records, backend)
    with pytest.raises(OSError):
        await consumer.pass_once()
    assert records.value["correlation"] == backend.submissions[0]
    monkeypatch.setattr(records, "write", lambda value: setattr(records, "value", dict(value)))
    backend.finished.set()
    await consumer.recover()
    assert len(backend.submissions) == 1
    assert records.value["done"]


async def test_async_records_keep_intent_before_submit_and_done_after_terminal():
    records, backend = Records(), Backend()
    written = []

    async def read():
        await asyncio.sleep(0)
        return records.read()

    async def write(value):
        await asyncio.sleep(0)
        records.write(value)
        written.append(dict(value))

    consumer = _admission.Consumer(SimpleNamespace(read=read, write=write), backend)
    task = asyncio.create_task(consumer.run())
    try:
        await asyncio.wait_for(backend.entered.wait(), 30)
        assert written[0] == {"correlation": backend.submissions[0], "run": "", "done": False}
        assert written[-1]["run"] == backend.runs[0].name
        assert not written[-1]["done"]
        backend.finished.set()
        for _ in range(100):
            if records.value.get("done"):
                break
            await asyncio.sleep(0)
        assert records.value["done"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
