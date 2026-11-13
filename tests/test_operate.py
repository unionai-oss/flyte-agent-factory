"""The operator tool's one safety rule: never add a session to a live credential."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from obstore.exceptions import GenericError


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OPS = Path(__file__).resolve().parents[1] / "ops"
operate = _load("operate", OPS / "operate.py")


def _run(name, phase):
    return SimpleNamespace(name=name, phase=f"ActionPhase.{phase}", url="u")


def _args(brief="x"):
    return SimpleNamespace(brief=str(brief), name="proj", demo="")


@pytest.fixture
def brief_file(tmp_path):
    path = tmp_path / "brief.md"
    path.write_text("write {name}/app")
    return path


@pytest.fixture(autouse=True)
def watcher(monkeypatch):
    """`operate.py launch` starts a detached watcher; here it is a recording stub."""
    import sys

    stub = SimpleNamespace(started=[], LOG="log")
    stub.ensure_running = lambda: stub.started.append(1) or 42
    monkeypatch.setitem(sys.modules, "watch", stub)
    return stub


def test_launch_queues_at_the_cap(flyte_world, monkeypatch, brief_file, capsys):
    # Replaces test_launch_refuses_at_the_cap: capacity no longer refuses a launch, it queues it.
    # One slot per login still holds (taskgraph-03: two sessions on one OAuth login break each
    # other's refresh chain), so the cap is still the number of slots.
    assert operate.CAP == len(operate.SLOTS) == 2
    flyte_world.add("theirs", operate.SLOTS[0])
    flyte_world.add("p2", operate.SLOTS[1])
    operate.launch(_args(brief_file))
    assert "queued: proj is number 1 in the queue" in capsys.readouterr().out
    assert flyte_world.created == []


def test_the_cap_can_never_exceed_the_slots(monkeypatch):
    monkeypatch.setenv("OPERATE_CAP", "5")
    mod = _load("operate_cap", OPS / "operate.py")
    assert mod.CAP == len(mod.SLOTS)


def _slotted(name, task):
    return SimpleNamespace(
        name=name,
        phase="ActionPhase.RUNNING",
        url="u",
        to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task}}}}},
    )


def test_a_project_goes_to_the_slot_nobody_is_on(flyte_world, brief_file):
    flyte_world.add("p1", "agent-one.project")
    operate.launch(_args(brief_file))
    assert [c["task"] for c in flyte_world.created] == ["agent-two.project"]
    assert operate.free_slot([]) == "agent-one.project"
    assert operate.free_slot([_slotted("p2", "agent-two.project")]) == "agent-one.project"


def test_a_full_house_never_puts_two_sessions_on_one_login(flyte_world, brief_file):
    # Replaces test_force_never_puts_two_sessions_on_one_login: --force is gone, and a launch with every
    # slot busy now starts the watcher (it will run later) instead of exiting "every slot".
    flyte_world.add("p1", "agent-one.project")
    flyte_world.add("p2", "agent-two.project")
    operate.launch(_args(brief_file))
    assert flyte_world.created == [] and operate.sys.modules["watch"].started == [1]
    with pytest.raises(SystemExit):
        operate.main(["launch", "b.md", "--name", "n", "--force"])


def test_every_slot_is_a_live_operated_project(monkeypatch):
    runs = [
        _slotted("mine", "agent-own.project"),
        _slotted("a", "agent-one.project"),
        _slotted("b", "agent-two.project"),
    ]
    monkeypatch.setattr(operate, "live_runs", lambda: runs)
    assert [r.name for r in operate.operated_live()] == ["a", "b"]


def test_the_supervisor_itself_does_not_count_against_the_cap(monkeypatch):
    runs = {"mine": "agent-own.project", "p1": "agent-one.project"}
    monkeypatch.setattr(operate, "live_runs", lambda: [_run("mine", "RUNNING")])
    monkeypatch.setattr(operate, "_task_name", lambda r: runs[r.name])
    assert operate.operated_live() == []
    monkeypatch.setattr(operate, "live_runs", lambda: [_run("mine", "RUNNING"), _run("p1", "RUNNING")])
    assert [r.name for r in operate.operated_live()] == ["p1"]


def test_below_the_cap_launch_proceeds(flyte_world, monkeypatch, brief_file, capsys):
    # A dispatch that dies is not a failed launch: the request is queued and the sweep starts it.
    monkeypatch.setattr(
        operate.remote.Task, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reached"))
    )
    operate.launch(_args(brief_file))
    assert "dispatch deferred to the sweep: RuntimeError: reached" in capsys.readouterr().out


def test_projects_launch_into_the_work_environment():
    assert operate.TASK == "agent-one.project"


def test_the_tool_has_no_way_to_abort():
    source = (OPS / "operate.py").read_text()
    assert ".abort(" not in source and "abort" not in operate.main.__code__.co_names


watch = _load("watch_under_test", OPS / "watch.py")


def test_an_open_ask_is_reported_on_every_pass_with_its_age():
    """Run 2 sat blocked on the operator for 40+ minutes: one report is not enough."""
    seen: dict[str, float] = {}
    runs = {"r": "RUNNING"}
    conds = {"r": [("ask-a0-001", "ACTION_PHASE_PAUSED"), ("direct-a0-001", "ACTION_PHASE_PAUSED")]}
    first = watch.scan(runs, conds, seen, now=100.0)
    later = watch.scan(runs, conds, seen, now=700.0)
    assert first == ["[operator] BLOCKED r is waiting on YOU: ask-a0-001 open 0s"]
    assert later == ["[operator] BLOCKED r is waiting on YOU: ask-a0-001 open 600s"], "age must grow"


def test_the_standing_inbox_is_not_a_question():
    out = watch.scan({"r": "RUNNING"}, {"r": [("direct-a0-007", "ACTION_PHASE_PAUSED")]}, {}, now=1.0)
    assert out == []


def test_an_answered_ask_stops_being_reported_and_a_new_one_restarts_the_clock():
    seen: dict[str, float] = {}
    runs = {"r": "RUNNING"}
    watch.scan(runs, {"r": [("ask-a0-001", "ACTION_PHASE_PAUSED")]}, seen, now=0.0)
    assert watch.scan(runs, {"r": [("ask-a0-001", "ACTION_PHASE_SUCCEEDED")]}, seen, now=60.0) == []
    again = watch.scan(runs, {"r": [("ask-a0-002", "ACTION_PHASE_PAUSED")]}, seen, now=120.0)
    assert again == ["[operator] BLOCKED r is waiting on YOU: ask-a0-002 open 0s"]


def test_a_finished_run_is_announced():
    assert watch.scan({"r": "SUCCEEDED"}, {"r": []}, {}, now=0.0) == ["[operator] ENDED r: SUCCEEDED"]


def test_launch_cannot_happen_without_starting_the_watcher(flyte_world, brief_file):
    """The watcher is part of launching, not a separate habit."""
    operate.launch(_args(brief_file))
    assert operate.sys.modules["watch"].started == [1]
    assert [c["inputs"]["name"] for c in flyte_world.created] == ["proj"]


@pytest.mark.parametrize(("model", "expected"), [("haiku", {"model": "haiku"}), ("", {})])
def test_launch_passes_the_author_model_only_when_given(flyte_world, brief_file, model, expected):
    args = _args(brief_file)
    args.model = model
    operate.launch(args)
    inputs = flyte_world.created[0]["inputs"]
    assert {k: v for k, v in inputs.items() if k == "model"} == expected


@pytest.mark.parametrize("priority", ["low", "normal", "expedite"])
def test_launch_passes_the_priority_through(monkeypatch, brief_file, priority):
    seen = []

    def launch_project(*a, **k):
        seen.append(k.get("priority"))
        return {"status": "queued", "position": 1}

    monkeypatch.setattr(operate._factory, "prepare_launch", lambda *a, **k: None)
    monkeypatch.setattr(operate._factory, "launch_project", launch_project)
    args = _args(brief_file)
    args.priority = priority
    operate.launch(args)
    assert seen == [priority]


def test_the_launch_command_takes_a_priority_that_defaults_to_normal(monkeypatch):
    parsed: list[Any] = []
    monkeypatch.setattr(operate, "_init", lambda: None)
    monkeypatch.setattr(operate, "launch", parsed.append)
    operate.main(["launch", "b.md", "--name", "n"])
    operate.main(["launch", "b.md", "--name", "n", "--priority", "low"])
    assert [a.priority for a in parsed] == ["normal", "low"]
    with pytest.raises(SystemExit):
        operate.main(["launch", "b.md", "--name", "n", "--priority", "urgent"])


def test_the_watch_loop_survives_a_failed_poll():
    source = (OPS / "watch.py").read_text()
    assert "except Exception" in source and "poll failed" in source


def test_watcher_is_running_even_when_run_creation_dies(flyte_world, brief_file, capsys):
    """Watcher first: a launch that dies at run creation (wordstats-03) must still leave the watcher up."""
    flyte_world.fail_create = [RuntimeError("died after the run was created")]
    operate.launch(_args(brief_file))
    assert operate.sys.modules["watch"].started == [1]
    assert "died after the run was created" in capsys.readouterr().out  # queued; the sweep retries


def test_a_refused_launch_queues_nothing(flyte_world, brief_file):
    # What a refusal must do is leave nothing behind (test_a_refused_launch_starts_no_watcher covers the
    # watcher).
    args = _args(brief_file)
    args.name = "Bad Name"
    with pytest.raises(ValueError, match="name must be"):
        operate.launch(args)
    assert flyte_world.created == [] and not Path(flyte_world.queue.removeprefix("file://")).exists()


def test_an_interface_failure_says_no_run_was_created(flyte_world, brief_file):
    """wordstats-03: a proto dump was misread as a launch, and the supervisor's own run id reported as the
    project's."""
    flyte_world.fail_create = [
        ValueError("No transformers could reverse Flyte literal type union_type {\n ...")
    ]
    with pytest.raises(SystemExit, match="NO run was created.*uv run"):
        operate.launch(_args(brief_file))


def test_each_launch_gets_its_own_directory(flyte_world):
    """wordstats-03 re-delivered wordstats-02's directory and passed on its evidence."""
    brief = Path(__file__).resolve().parents[1] / "briefs" / "smoke-wordstats.md"
    operate.launch(SimpleNamespace(brief=str(brief), name="wordstats-04", demo=""))
    sent = flyte_world.created[0]["inputs"]
    assert "`wordstats-04/wordstats.py`" in sent["brief"]
    assert "{name}" not in sent["brief"] and "`wordstats/" not in sent["brief"]


def test_one_live_project_leaves_room_for_a_second(flyte_world, brief_file):
    assert operate.CAP == 2  # a second login per slot
    flyte_world.add("p1", "agent-one.project")
    operate.launch(_args(brief_file))
    assert len(flyte_world.created) == 1


def test_a_run_that_leaves_the_live_list_is_reported_ended():
    """wordstats-03 SUCCEEDED and the watcher said nothing: it only ever saw live runs."""
    announced = {"a", "b"}
    gone = watch.vanished(announced, {"b": "RUNNING"})
    assert gone == ["a"]
    assert watch.vanished(set(), {}) == []


def test_a_recent_launch_keeps_an_idle_watcher_alive():
    """wordstats-04: launch saw the watcher alive; it then exited idle and the run went unwatched."""
    assert watch.should_exit(quiet=20, idle_exit=20, kicked_at=0.0, now=1000.0, window=60.0)
    assert not watch.should_exit(quiet=20, idle_exit=20, kicked_at=990.0, now=1000.0, window=60.0)
    assert not watch.should_exit(quiet=3, idle_exit=20, kicked_at=0.0, now=1000.0, window=60.0)


def _watch_files(tmp_path, monkeypatch):
    for name in ("KICK", "PIDFILE", "READY", "LOG"):
        monkeypatch.setattr(watch, name, tmp_path / name.lower())


def test_ensure_running_always_kicks(tmp_path, monkeypatch):
    import os

    _watch_files(tmp_path, monkeypatch)
    (tmp_path / "pidfile").write_text(str(os.getpid()))
    (tmp_path / "ready").write_text(f"{os.getpid()} {watch.code_fingerprint()}")
    monkeypatch.setattr(watch, "_is_watcher", lambda pid: True)  # "alive": no new watcher is started
    monkeypatch.setattr(watch, "_spawn", lambda: pytest.fail("a current watcher must be kept"))
    assert watch.ensure_running() == os.getpid()
    assert (tmp_path / "kick").exists()


def test_a_watcher_on_older_code_is_replaced_once_its_successor_is_up(tmp_path, monkeypatch):
    """login-slots: launch kept a watcher from before the deploy, which watched only slot 1."""
    _watch_files(tmp_path, monkeypatch)
    (tmp_path / "pidfile").write_text("111")
    (tmp_path / "ready").write_text("111 oldcode")
    killed = []

    def spawn():
        (tmp_path / "ready").write_text(f"222 {watch.code_fingerprint()}")
        return 222

    monkeypatch.setattr(watch, "_spawn", spawn)
    monkeypatch.setattr(watch, "_is_watcher", lambda pid: pid in (111, 222) and pid not in killed)
    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: killed.append(pid))
    assert watch.ensure_running(wait_s=1) == 222
    assert killed == [111], "the old watcher goes only after the new one is ready"
    assert (tmp_path / "pidfile").read_text() == "222"


@pytest.mark.parametrize("successor", ["dies", "never_ready"])
def test_a_successor_that_does_not_come_up_leaves_the_old_watcher(tmp_path, monkeypatch, successor):
    """qa on watcher-reload: new code that compiles but crashes at startup must not cost the watcher."""
    _watch_files(tmp_path, monkeypatch)
    (tmp_path / "pidfile").write_text("111")
    (tmp_path / "ready").write_text("111 oldcode")
    killed = []
    monkeypatch.setattr(watch, "_spawn", lambda: 222)
    alive = {111} if successor == "dies" else {111, 222}
    monkeypatch.setattr(watch, "_is_watcher", lambda pid: pid in alive and pid not in killed)
    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: killed.append(pid))
    assert watch.ensure_running(wait_s=0.6) == 111
    assert 111 not in killed
    assert killed == ([] if successor == "dies" else [222]), (
        "a hung successor is not left as a second watcher"
    )
    assert (tmp_path / "pidfile").read_text() == "111"


def test_no_watcher_at_all_refuses_rather_than_launching_unwatched(tmp_path, monkeypatch):
    _watch_files(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "_spawn", lambda: 222)
    monkeypatch.setattr(watch, "_is_watcher", lambda pid: False)
    with pytest.raises(RuntimeError, match="no watcher could be started"):
        watch.ensure_running(wait_s=0.2)


def test_a_watcher_says_it_is_ready_only_after_initialising():
    source = (OPS / "watch.py").read_text()
    main = source[source.index("def main()") :]
    assert main.index("operate._init()") < main.index("READY.write_text")


def test_the_fingerprint_covers_both_files(tmp_path):
    a, b = tmp_path / "watch.py", tmp_path / "operate.py"
    a.write_text("v1")
    b.write_text("x")
    before = watch.code_fingerprint((a, b))
    b.write_text("x2")
    assert watch.code_fingerprint((a, b)) != before


def test_the_loop_does_not_report_a_still_live_run_as_ended():
    source = (OPS / "watch.py").read_text()
    assert "if phase in operate.LIVE:" in source


def test_a_live_pid_that_is_not_a_watcher_is_not_trusted():
    """taskgraph-01: a pidfile from a just-killed watcher made launch skip starting one."""
    import os

    assert not watch._is_watcher(os.getpid())  # pytest is alive, but it is not watch.py
    assert not watch._is_watcher(2**22 + 7)  # no such process


def test_a_demo_launch_names_its_package(flyte_world, brief_file):
    args = _args(brief_file)
    args.demo = "gateway-pii"
    operate.launch(args)
    assert flyte_world.created[0]["inputs"]["demo_package"] == "gateway-pii"


def test_a_project_waiting_for_a_node_holds_its_slot(monkeypatch):
    """qa on login-slots: WAITING_FOR_RESOURCES was not live, so a second launch took the same login."""
    from flyte.models import ActionPhase

    assert "WAITING_FOR_RESOURCES" in ActionPhase.__members__
    waiting = _slotted("p1", "agent-one.project")
    waiting.phase = "ActionPhase.WAITING_FOR_RESOURCES"
    monkeypatch.setattr(operate.remote.Run, "listall", lambda **k: [waiting])
    assert [r.name for r in operate.operated_live()] == ["p1"]
    assert operate.free_slot(operate.operated_live()) == "agent-two.project"


def test_two_launches_at_once_leave_one_watcher(tmp_path, monkeypatch):
    """qa on watcher-reload: racing launches each spawned a successor and killed the other's."""
    import threading

    _watch_files(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "LOCK", tmp_path / "lock")
    (tmp_path / "pidfile").write_text("111")
    (tmp_path / "ready").write_text("111 oldcode")
    spawned: list[int] = []
    killed: list[int] = []

    def spawn():
        pid = 200 + len(spawned)
        spawned.append(pid)
        (tmp_path / "ready").write_text(f"{pid} {watch.code_fingerprint()}")
        return pid

    monkeypatch.setattr(watch, "_spawn", spawn)
    monkeypatch.setattr(
        watch, "_is_watcher", lambda pid: pid not in killed and (pid == 111 or pid in spawned)
    )
    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: killed.append(pid))
    got = []
    threads = [threading.Thread(target=lambda: got.append(watch.ensure_running(wait_s=2))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spawned == [200], "the second launch must find the first one's watcher current"
    assert got == [200, 200] and killed == [111]
    assert (tmp_path / "pidfile").read_text() == "200"


def test_a_watcher_never_writes_or_deletes_another_watchers_pidfile():
    source = (OPS / "watch.py").read_text()
    main = source[source.index("def main()") :]
    assert "PIDFILE.write_text" not in main
    assert "int(PIDFILE.read_text()) == os.getpid()" in main


def test_an_interrupted_launch_takes_its_successor_with_it(tmp_path, monkeypatch):
    """qa on watcher-reload: Ctrl-C during the wait left the successor running beside the old watcher."""
    _watch_files(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, "LOCK", tmp_path / "lock")
    killed = []
    monkeypatch.setattr(watch, "_spawn", lambda: 222)
    monkeypatch.setattr(watch, "_is_watcher", lambda pid: True)
    monkeypatch.setattr(watch.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(watch.time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        watch.ensure_running(wait_s=5)
    assert killed == [222]


def test_silent_runs_report_once_an_hour_and_reset_on_activity():
    state: dict[str, Any] = {}

    def poll(now, report="report", phase="RUNNING", questions=None):
        return watch.scan(
            {"r": phase},
            {"r": questions or []},
            {},
            now,
            activity={"r": report},
            activity_seen=state,
            silent_after=60,
        )

    assert poll(0) == []
    assert poll(59) == []
    assert poll(60) == ["[operator] SILENT r no activity for 1m"]
    assert poll(3659) == []
    assert poll(3660) == ["[operator] SILENT r no activity for 61m"]
    assert poll(3661, "new report") == []
    assert poll(3721, "new report") == ["[operator] SILENT r no activity for 1m"]
    assert poll(3722, "new report", phase="PAUSED") == []
    assert poll(3782, "new report", phase="PAUSED") == ["[operator] SILENT r no activity for 1m"]
    question = [("ask-a0-001", "ACTION_PHASE_PAUSED")]
    assert not any("SILENT" in line for line in poll(3783, "new report", "PAUSED", question))
    assert poll(3843, "new report", "PAUSED", question) == [
        "[operator] BLOCKED r is waiting on YOU: ask-a0-001 open 0s"
    ]
    assert poll(3844, "new report", "PAUSED") == []
    assert any("SILENT" in line for line in poll(3904, "new report", "PAUSED"))
    assert poll(3844, phase="SUCCEEDED") == ["[operator] ENDED r: SUCCEEDED"]
    assert "r" not in state


def test_unavailable_reports_and_standing_inbox_do_not_reset_silence():
    state: dict[str, Any] = {}
    runs = {"r": "RUNNING"}
    watch.scan(runs, {}, {}, 0, activity={"r": "report"}, activity_seen=state, silent_after=60)
    assert watch.scan(
        runs,
        {"r": [("direct-a0-001", "ACTION_PHASE_PAUSED")]},
        {},
        60,
        activity={},
        activity_seen=state,
        silent_after=60,
    ) == ["[operator] SILENT r no activity for 1m"]
    assert watch.scan({}, {}, {}, 61, activity_seen=state) == []
    assert state["r"][1] == 0
    assert watch.scan(runs, {}, {}, 3660, activity_seen=state) == ["[operator] SILENT r no activity for 61m"]
    watch.scan({"r": "SUCCEEDED"}, {}, {}, 3661, activity_seen=state)
    assert state == {}


@pytest.mark.parametrize(
    "status",
    [
        "idle for {n}m -- send direction",
        "waiting on 2 child actions ({n}m since update)",
        "QA round 2 reviewing ({n}m so far)",
        "paused on the Claude usage limit for {n}m",
    ],
)
def test_report_counters_do_not_count_as_activity(status):
    state: dict[str, Any] = {}
    for now, counter in [(0, 5), (60, 6)]:
        report = (
            f"<p><b>Status:</b> {status.format(n=counter)}</p>"
            f"<table><tr><td><code>10:00:00</code></td><td>idle for {counter}m</td></tr></table>"
        )
        lines = watch.scan(
            {"r": "RUNNING"},
            {},
            {},
            now,
            activity={"r": watch.report_activity(report)},
            activity_seen=state,
            silent_after=60,
        )
    assert lines == ["[operator] SILENT r no activity for 1m"]
    assert watch.report_activity(report) != watch.report_activity(
        report.replace("idle for", "session progressed for")
    )


def test_supervisor_timer_rows_do_not_count_as_activity():
    def row(t, icon, text):
        return f"<tr><td><code>{t}</code></td><td>{icon}</td><td>{text}</td></tr>"

    base = row("10:00:00", "📝", "DONE: shipped")
    later = (
        row("11:05:03", "💾", "checkpointed")
        + row("10:51:11", "🔑", "refreshed login stored in &#x27;slot&#x27;")
        + row("10:40:00", "·", "no checkpoint configured for this action")
        + base
    )
    assert watch.report_activity(f"<table>{base}</table>") == watch.report_activity(f"<table>{later}</table>")
    assert watch.report_activity(f"<table>{base}</table>") != watch.report_activity(
        f"<table>{row('11:06:00', '📝', 'fixed round 1')}{base}</table>"
    )


def test_silence_survives_watcher_replacement(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STATE", tmp_path / "state.json")
    state: dict[str, Any] = {}
    watch.scan({"r": "RUNNING"}, {}, {}, 0, activity_seen=state)
    watch.save_activity(state)
    restored = watch.load_activity()
    assert restored == state
    assert watch.scan({"r": "RUNNING"}, {}, {}, 3600, activity_seen=restored) == [
        "[operator] SILENT r no activity for 60m"
    ]
    watch.save_activity(restored)
    assert watch.scan({"r": "RUNNING"}, {}, {}, 3630, activity_seen=watch.load_activity()) == []


def test_the_slot_env_knows_its_declaring_file_under_a_flyte_home():
    """The SDK takes any path containing "/flyte/" for its own and then cannot anchor `include`.

    Pods run as the `flyte` user, so a checkout under /home/flyte hit that and the bundle failed.
    """
    from pathlib import Path

    from flyte_agent_factory import agent

    assert Path(str(agent.env._declaring_file)).resolve() == Path(str(agent.__file__)).resolve()


def test_slot_bundle_carries_lazy_queue_imports(tmp_path):
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""\
        import ast
        import asyncio
        import inspect
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tarfile
        from unittest.mock import patch

        from flyte._code_bundle.bundle import build_code_bundle
        from flyte._code_bundle._includes import collect_env_include_files
        from flyte_agent_factory import _checkpoint, _supervise_support, agent

        root = Path(agent.__file__).resolve().parents[1]
        destination = Path(sys.argv[1])
        # Ruff discovery is optional: production must also work with only loaded modules.
        with patch("flyte._code_bundle._utils._ruff_is_available", return_value=False):
            bundle = asyncio.run(build_code_bundle(
                root, dryrun=True, copy_bundle_to=destination,
                additional_files=collect_env_include_files((agent.env, agent.subagent_env, agent.setup_env)),
            ))
        extracted = destination / "bundle"
        with tarfile.open(bundle.tgz) as archive:
            archive.extractall(extracted, filter="data")

        modules = set()
        for function in (_supervise_support._refresh_queue, _checkpoint._hand_over_slot):
            for node in ast.walk(ast.parse(inspect.getsource(function))):
                if isinstance(node, ast.ImportFrom) and node.module == "flyte_agent_factory":
                    modules.update("flyte_agent_factory." + alias.name for alias in node.names)
        assert "flyte_agent_factory._factory" in modules
        clean = dict(os.environ, PYTHONPATH=str(extracted))
        subprocess.run(
            [sys.executable, "-c", "import importlib, pathlib, sys; "
             "modules = [importlib.import_module(name) for name in sys.argv[1:]]; "
             "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(pathlib.Path.cwd()) "
             "for m in modules)", *sorted(modules)],
            cwd=extracted, env=clean, check=True,
        )
    """)
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_package_module_loads_with_the_agent():
    """The deploy bundles only modules loaded at import time: a lazily imported `_delegation`
    was left out and every project died at startup with ModuleNotFoundError."""
    import subprocess
    import sys

    code = (
        "import sys, pkgutil, flyte_agent_factory, flyte_agent_factory.agent\n"
        "mods = {m.name for m in pkgutil.iter_modules(flyte_agent_factory.__path__)}\n"
        "print(','.join(sorted(m for m in mods if 'flyte_agent_factory.' + m not in sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    # voice.py is a standalone tool run inside the image, never imported by a task.
    # The factory, MCP, webhook and deployer modules (and the deploy helpers only they import) deploy
    # independently through the reviewed-ref gate, as does the reconciler. `_queue` ships with the
    # supervisor, which still reads the launch queue (state step 1). `_github` is not among them:
    # `_profile` imports it to validate `[repo] factory`, so every deployed bundle carries it.
    # `_deploy_child` is never loaded from a bundle: the deployer runs it from the fetched engine checkout.
    # The benchmark (`_bench*`) deploys as factory-bench and factory-bench-check, never inside a slot.
    # `_planner` is admission, run by the factory side (the webhook app), never by a slot.
    # `_board_events`, `_loop` and `_loop_flow` also run only on the factory side.
    # `_promote` runs from chat through ops/promote.py. `_slow_lane` runs as a subprocess and ships
    # via include.
    # The `_factory_*` modules are `_factory`'s own and ship with it (the slot env's `include`).
    # `_bootstrap*` is the setup command (ops/bootstrap.py) run from an engine checkout on a person's machine.
    # `_grant_page` is `_factory_mcp`'s (the factory side); `_grants_cli` is the `credential-grant` command,
    # run by name in the session, so it is in the slot env's `include` too.
    assert out.stdout.strip() == (
        "_admission,_backlog,_bench,_bench_check,_bench_flow,_bench_logstats_files,_bench_stats,_bench_store,"
        "_bench_suite,_board_events,_bootstrap,_bootstrap_base,_bootstrap_finish,_bootstrap_flyte,"
        "_bootstrap_github,_bootstrap_keys,_bootstrap_profile,_bootstrap_steps,_deploy,_deploy_apps,_deploy_child,_deployer,"
        "_factory,_factory_base,"
        "_factory_grants,_factory_live,_factory_mcp,_factory_meters,_factory_policy,_factory_recreate,"
        "_factory_status,_grant_page,_grants_cli,_loop,_loop_flow,"
        "_planner,_planner_facts,_planner_runtime,_promote,_reconcile,_slow_lane,_webhook,voice"
    )


def test_operate_task_prefers_a_slot_but_never_hides_the_other(monkeypatch):
    monkeypatch.setattr(operate, "TASK", "agent-two.project")
    assert operate.SLOTS == ("agent-one.project", "agent-two.project")
    monkeypatch.setattr(operate, "_task_name", lambda r: r)
    assert operate.free_slot([]) == "agent-two.project"
    assert operate.free_slot(["agent-two.project"]) == "agent-one.project"
    assert operate.free_slot(["agent-one.project", "agent-two.project"]) is None


def test_live_runs_lists_newest_first(monkeypatch):
    seen = {}

    def listall(**kwargs):
        seen.update(kwargs)
        return [SimpleNamespace(phase="ActionPhase.RUNNING"), SimpleNamespace(phase="ActionPhase.SUCCEEDED")]

    monkeypatch.setattr(operate.remote.Run, "listall", listall)
    assert len(operate.live_runs()) == 1
    assert seen["sort_by"] == ("created_at", "desc")


def test_launch_policy_is_forwarded_to_run_environment(flyte_world, brief_file):
    args = _args(brief_file)
    args.policy = ["COMPACT_BETWEEN_ROUNDS=true", "AGENT_FACTORY_JUDGE_MODEL=sonnet"]
    operate.launch(args)
    assert flyte_world.created[0]["context"]["env_vars"] == {
        "AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS": "true",
        "AGENT_FACTORY_JUDGE_MODEL": "sonnet",
    }


def test_launch_policy_rejects_nonpolicy_key_before_launch():
    args = _args()
    args.policy = ["bad-key=value"]
    with pytest.raises(ValueError, match="uppercase policy key"):
        operate.launch(args)


@pytest.mark.parametrize("key", ["PROFILE", "PROFILE_TOML", "PROFILES_DIR"])
def test_launch_rejects_profile_policy_keys(key):
    args = _args()
    args.policy = [f"{key}=value"]
    with pytest.raises(operate._profile.ProfileError, match="not starting with PROFILE"):
        operate.launch(args)


def test_status_report_strips_style_and_script(monkeypatch, capsys):
    run = _run("r", "RUNNING")
    run.get_report = lambda: "<style>SECRET_CSS</style><script>SECRET_JSON</script><p>visible</p>"
    monkeypatch.setattr(operate.remote.Run, "get", lambda _: run)
    monkeypatch.setattr(operate, "_conditions", lambda _: [])
    operate.status(SimpleNamespace(run="r", report=1000))
    output = capsys.readouterr().out
    assert "visible" in output
    assert "SECRET" not in output


def test_overview_headers_mobile_and_last_round(monkeypatch):
    from flyte_agent_factory import _dashboard

    data = {"phase": {"kind": "building"}, "review": {"scores": [3, 1]}}
    monkeypatch.setattr(operate._factory_base, "report_html", lambda _: _dashboard.status_script(data))
    row = operate._row(_run("r", "RUNNING"), 100)
    assert row["round"] == 2
    view = {"rows": [row], "slots": {}, "generated_at": 100}
    assert "PROJECT" in operate.overview_text(view)
    page = operate.overview_html(view)
    assert "@media (max-width:" in page
    assert "data-label=" in page


def test_status_does_not_flag_the_direction_inbox_as_a_question(monkeypatch, capsys):
    run = _run("r", "RUNNING")
    monkeypatch.setattr(operate.remote.Run, "get", lambda _: run)
    conditions = [
        SimpleNamespace(name="direct-a0-001", phase="ACTION_PHASE_PAUSED"),
        SimpleNamespace(name="ask-a0-003", phase="ACTION_PHASE_PAUSED"),
    ]
    monkeypatch.setattr(operate, "_conditions", lambda _: conditions)
    operate.status(SimpleNamespace(run="r", report=0))
    lines = capsys.readouterr().out.splitlines()
    assert "needs an answer" not in next(line for line in lines if "direct-a0-001" in line)
    assert "needs an answer" in next(line for line in lines if "ask-a0-003" in line)


def test_launch_rejects_the_removed_unattended_flag(capsys):
    with pytest.raises(SystemExit) as exit_:
        operate.main(["launch", "b.md", "--name", "n", "--unattended"])
    assert exit_.value.code == 2
    assert "unattended" in capsys.readouterr().err


def test_the_queue_and_cancel_subcommands(flyte_world, monkeypatch, brief_file, capsys):
    monkeypatch.setattr(operate, "_init", lambda: None)
    flyte_world.add("p1", "agent-one.project")
    flyte_world.add("p2", "agent-two.project")
    operate.main(["launch", str(brief_file), "--name", "waiting-one"])
    operate.main(["queue"])
    out = capsys.readouterr().out
    assert "1. waiting-one (new, default model) waiting" in out
    assert "agent-one.project: p1 (running) p1" in out
    operate.main(["cancel", "waiting-one"])
    assert "cancelled waiting-one" in capsys.readouterr().out
    operate.main(["queue"])
    assert "queue is empty" in capsys.readouterr().out
    with pytest.raises(ValueError, match="not queued"):
        operate.main(["cancel", "waiting-one"])


def test_the_overview_shows_the_queue(flyte_world, monkeypatch, brief_file, capsys, tmp_path):
    monkeypatch.setattr(operate, "_init", lambda: None)
    flyte_world.add("p1", "agent-one.project")
    flyte_world.add("p2", "agent-two.project")
    brief_file.write_text("<script>alert(1)</script> first line\nsecond")
    operate.main(["launch", str(brief_file), "--name", "waiting-one"])
    monkeypatch.setattr(operate._factory, "recent_runs", list)
    page = tmp_path / "overview.html"
    operate.main(["overview", "--html", str(page)])
    out = capsys.readouterr().out
    assert "QUEUED" in out and "1. waiting-one" in out
    html = page.read_text()
    assert "<h4>Queued</h4>" in html and "waiting-one" in html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_overview_still_shows_projects_when_the_queue_is_unreadable(monkeypatch, capsys):
    monkeypatch.setattr(operate, "_init", lambda: None)  # the suite profile has no [state] uri
    monkeypatch.setattr(operate._factory, "recent_runs", list)
    operate.main(["overview"])
    out = capsys.readouterr().out
    assert "queue unavailable" in out and "no live or recent projects" in out


def test_the_overview_still_shows_projects_when_object_storage_fails(monkeypatch, capsys):
    def waiting_rows(*_):
        raise GenericError("Generic S3 error: Error performing list")

    monkeypatch.setattr(operate, "_init", lambda: None)
    monkeypatch.setattr(operate._factory_base, "queue_root", lambda: "s3://bucket/queue")
    monkeypatch.setattr(operate._factory, "waiting_rows", waiting_rows)
    monkeypatch.setattr(operate._factory, "recent_runs", list)
    operate.main(["overview"])
    out = capsys.readouterr().out
    assert "queue unavailable: GenericError" in out and "no live or recent projects" in out


@pytest.mark.parametrize("name", ["Bad Name", "ab-", "my--app"])
def test_a_refused_launch_starts_no_watcher(flyte_world, brief_file, name):
    # Same intent as the old test_a_refused_launch_starts_no_watcher: a launch that is going to be refused
    # must not leave a watcher running. Validation happens before the watcher starts.
    args = _args(brief_file)
    args.name = name
    with pytest.raises(ValueError, match="name must"):
        operate.launch(args)
    assert operate.sys.modules["watch"].started == []


def test_a_launch_refused_for_its_repo_or_a_running_name_starts_no_watcher(flyte_world, brief_file):
    flyte_world.add("p1-proj", operate.SLOTS[0], labels={"factory-project": "proj"})
    with pytest.raises(ValueError, match="direct-"):
        operate.launch(_args(brief_file))
    assert operate.sys.modules["watch"].started == [] and flyte_world.created == []


def test_a_launch_with_a_missing_brief_file_starts_no_watcher(flyte_world, tmp_path):
    with pytest.raises(OSError):
        operate.launch(_args(tmp_path / "nope.md"))
    assert operate.sys.modules["watch"].started == []


def test_stalled_completion_notification_cannot_keep_the_process_alive():
    """`to_thread` leaves the worker running after `wait_for` gives up, and `asyncio.run` then waits for
    it at exit: a stuck notification held the slot open for as long as it stalled."""
    import os
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""
        import asyncio, dataclasses, time
        from types import SimpleNamespace

        import pytest

        from flyte_agent_factory import _checkpoint, _factory, _profile, _session_config, agent
        from tests.fake_flyte import FakeFlyte

        world = FakeFlyte(pytest.MonkeyPatch(), _factory)
        world.before_create = lambda: time.sleep(60)
        _checkpoint._NOTIFY_TIMEOUT_S = 1.0  # the seam for the production 15 s; the property is the same
        board = _profile.Board("acme", "org", 1, "board-pat")
        _session_config._PROFILE = dataclasses.replace(
            _session_config._PROFILE, board=board, state=_profile.State(), planner=_profile.Planner()
        )
        timeline = SimpleNamespace(row=lambda *args: None)
        asyncio.run(_checkpoint._hand_over_slot(timeline, "run-1"))
    """)
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            # 40s: the (shortened) notification timeout plus start-up, which under a loaded parallel run took
            # most of the old 5s margin. Still well short of the 60s stall a held-open process would wait out.
            timeout=40,
            env={**os.environ, "PYTHONPATH": "src:."},
        )
    except subprocess.TimeoutExpired:
        raise AssertionError("the process outlived the notification timeout by more than 25 s") from None
    assert result.returncode == 0, result.stdout + result.stderr


def test_slot_bundle_carries_every_factory_module_launched_by_gates(tmp_path):
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent("""\
        import ast
        import asyncio
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tarfile
        from unittest.mock import patch

        from flyte._code_bundle.bundle import build_code_bundle
        from flyte._code_bundle._includes import collect_env_include_files
        from flyte_agent_factory import agent

        root = Path(agent.__file__).resolve().parents[1]
        modules = set()
        for path in (root / "flyte_agent_factory").glob("_gate*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.List, ast.Tuple)):
                    for first, second in zip(node.elts, node.elts[1:]):
                        if (isinstance(first, ast.Constant) and first.value == "-m"
                            and isinstance(second, ast.Constant) and isinstance(second.value, str)
                            and second.value.startswith("flyte_agent_factory.")):
                            modules.add(second.value)
        assert modules, "no gate launch commands discovered"
        destination = Path(sys.argv[1])
        with patch("flyte._code_bundle._utils._ruff_is_available", return_value=False):
            bundle = asyncio.run(build_code_bundle(
                root, dryrun=True, copy_bundle_to=destination,
                additional_files=collect_env_include_files((agent.env, agent.subagent_env, agent.setup_env)),
            ))
        extracted = destination / "bundle"
        with tarfile.open(bundle.tgz) as archive:
            archive.extractall(extracted, filter="data")
        for module in sorted(modules):
            result = subprocess.run(
                [sys.executable, "-P", "-m", module, "--help"],
                cwd=extracted, env=dict(os.environ, PYTHONPATH=str(extracted)),
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, module + "\\n" + result.stdout + result.stderr
            assert "usage:" in result.stdout
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
