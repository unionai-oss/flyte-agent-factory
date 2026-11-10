"""The ask-human bridge: the file protocol between the session and the supervisor.

The session subprocess and the supervisor that holds the Flyte task context
never share memory, so the contract between them is a directory. These tests
drive the real CLI as a subprocess and play the supervisor's part by hand.
"""

import asyncio
import json
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import (
    _bridge,
    _bridge_triage,
    _browser_gate,
    _inbox,
    _judgement,
    _session_config,
    _supervise_support,
    _timeline,
)
from tests._wait import wait_for, wait_for_sync


def _run_ask(root, src_path, args):
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": src_path,
        "AGENT_FACTORY_BRIDGE_DIR": str(root),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "flyte_agent_factory._bridge", "ask", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _answer_next(root, payload, *, timeout=60):
    """Play the supervisor: wait for a request, write the answer back."""

    def unanswered():
        for path in sorted((root / _bridge.REQUESTS).glob("*.json")):
            request = json.loads(path.read_text())
            if not (root / _bridge.ANSWERS / f"{request['id']}.json").exists():
                return request

    wait_for_sync(unanswered, timeout, 0.05, "a request on the bridge")
    request = unanswered()
    _bridge._write_atomic(root / _bridge.ANSWERS / f"{request['id']}.json", {"id": request["id"], **payload})
    return request


def test_free_text_question(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--timeout", "120", "What storage should login use?"])
    request = _answer_next(bridge_root, {"value": "the OS keyring"})
    out, _ = proc.communicate(timeout=120)

    assert request["kind"] == "text"
    assert request["question"] == "What storage should login use?"
    assert out.strip() == "the OS keyring"
    assert proc.returncode == 0


@pytest.mark.parametrize(
    ("value", "expected_output", "expected_code"),
    [(True, "approved", 0), (False, "declined", 1)],
)
def test_approval_decision_is_in_the_exit_code(bridge_root, src_path, value, expected_output, expected_code):
    # The exit code matters: it is how a `&&` in the model's shell command
    # respects a refusal without having to parse stdout.
    proc = _run_ask(bridge_root, src_path, ["--approve", "--timeout", "120", "Force-push to main?"])
    request = _answer_next(bridge_root, {"value": value})
    out, _ = proc.communicate(timeout=120)

    assert request["kind"] == "approve"
    assert out.strip() == expected_output
    assert proc.returncode == expected_code


def test_supervisor_error_reaches_the_model(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--timeout", "120", "will fail"])
    _answer_next(bridge_root, {"error": "ConditionFailedError: backend said no"})
    _, err = proc.communicate(timeout=120)

    assert proc.returncode == 2
    assert "could not ask" in err


def test_unanswered_question_times_out(bridge_root, src_path):
    # An unanswered question must not wedge the session forever; the supervisor
    # closes the condition shortly after the CLI gives up.
    started = time.monotonic()
    proc = _run_ask(bridge_root, src_path, ["--timeout", "3", "nobody will answer"])
    _, err = proc.communicate(timeout=120)

    assert proc.returncode == 3
    assert time.monotonic() - started > 2, "it waited for the timeout rather than giving up at once"
    assert "will be closed in Union" in err


def test_notes_drain_once_and_in_order(bridge_root, src_path):
    for message in ("wired up the polling loop", "tests green"):
        subprocess.run(
            [sys.executable, "-m", "flyte_agent_factory._bridge", "note", message],
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": src_path,
                "AGENT_FACTORY_BRIDGE_DIR": str(bridge_root),
            },
            check=True,
        )

    bridge = _bridge.Bridge(bridge_root)
    assert bridge.drain_notes() == ["wired up the polling loop", "tests green"]
    assert bridge.drain_notes() == [], "notes were replayed into the report"


def test_scan_services_each_request_exactly_once(bridge_root, monkeypatch):
    serviced: list[tuple[str, int]] = []

    async def fake_service(self, request, seq):
        serviced.append((request["question"], seq))

    monkeypatch.setattr(_bridge.Bridge, "_service", fake_service)

    for n in range(3):
        _bridge._write_atomic(
            bridge_root / _bridge.REQUESTS / f"{n}.json",
            {"id": str(n), "question": f"q{n}", "kind": "text"},
        )

    async def scan_twice():
        bridge = _bridge.Bridge(bridge_root)
        bridge._scan()
        bridge._scan()  # must be a no-op: a re-serviced ask duplicates conditions
        await wait_for(lambda: len(serviced) >= 3)
        await asyncio.sleep(0.1)  # and a duplicate, if there were one, has time to show up

    asyncio.run(scan_twice())

    assert sorted(q for q, _ in serviced) == ["q0", "q1", "q2"]
    assert len({seq for _, seq in serviced}) == 3, "condition names would collide"


def test_redact_env_hides_credential_values(monkeypatch):
    """Printing a variable to confirm it is set must not print its value.

    A probe once dumped `_UNION_EAGER_API_KEY` into run logs this way. Logs
    outlive the run and are readable by everyone with project access.
    """
    monkeypatch.setenv("_UNION_EAGER_API_KEY", "super-secret-value")
    monkeypatch.setenv("GIT_DEPLOY_KEY", "-----BEGIN OPENSSH PRIVATE KEY-----")
    monkeypatch.setenv("CLAUDE_CREDENTIALS_JSON", '{"claudeAiOauth": {}}')
    monkeypatch.setenv("HOME", "/home/flyte")

    env = _bridge.redact_env()

    for name in ("_UNION_EAGER_API_KEY", "GIT_DEPLOY_KEY", "CLAUDE_CREDENTIALS_JSON"):
        assert "redacted" in env[name], f"{name} leaked"
        assert "secret" not in env[name].lower() or "redacted" in env[name]
    # Non-credential variables stay readable, which is the point of the helper.
    assert env["HOME"] == "/home/flyte"


def test_redact_removes_a_secret_from_text_that_came_back():
    """Three credential leaks here were echoed text, not a dumped environment.

    Playwright MCP returns the code it was asked to run, so a tool call that
    sets an Authorization header hands the header straight back.
    """
    from flyte_agent_factory._bridge import redact

    key = "union.example.org:demo-EAGER_API_KEY-abcdef0123456789"
    echoed = f"### Ran Playwright code\nsetExtraHTTPHeaders({{ Authorization: 'Bearer {key}' }})"

    scrubbed = redact(echoed, key)
    assert key not in scrubbed
    assert "abcdef0123456789" not in scrubbed
    assert "setExtraHTTPHeaders" in scrubbed, "only the secret should be removed"


def test_redact_can_keep_a_short_prefix_to_tell_credentials_apart():
    from flyte_agent_factory._bridge import redact

    key = "sk-ant-0123456789abcdef"
    scrubbed = redact(f"using {key} now", key, keep=7)
    assert scrubbed.startswith("using sk-ant-<redacted")
    assert "0123456789abcdef" not in scrubbed


def test_redact_ignores_unset_and_tiny_values():
    """Passing an unset variable must not blank the whole string.

    `redact(text, os.environ.get("MISSING", ""))` is the obvious call site, and
    an empty needle would otherwise match everywhere.
    """
    from flyte_agent_factory._bridge import redact

    assert redact("nothing secret here", "") == "nothing secret here"
    assert redact("a short value: abc", "abc") == "a short value: abc"


def test_a_late_answer_is_delivered_not_discarded():
    """An answer arriving after the asker gave up used to go nowhere.

    A project asked for a hostname it could not discover, carried on correctly
    when nobody answered in time, and then sat idle for two hours while the
    answer sat recorded against a condition nothing was listening to. An answer
    inside the grace window is delivered late with a note saying so; past it the
    question is closed and nothing listens any more (a condition left open
    forever is what made stale questions look answerable).
    """
    import inspect

    from flyte_agent_factory._bridge import Bridge

    service = inspect.getsource(Bridge._service)
    assert "_write_late" in service
    assert not hasattr(Bridge, "_deliver_late"), "nothing may keep listening after the close"

    deliver = inspect.getsource(Bridge._write_late)
    assert "late answer to a question you already moved past" in deliver.lower()
    assert "already chose a different route" in deliver


def test_the_inbox_path_is_shared_with_the_supervisor():
    """The session is told one path; both writers must use that exact path.

    The bridge drops late answers into the inbox and the supervisor's own
    direction goes there too. Two copies of the string would silently diverge.
    """
    from flyte_agent_factory._bridge import INBOX_SUBDIR

    assert _session_config._INBOX_DIR == INBOX_SUBDIR
    assert INBOX_SUBDIR == ".claude-flyte/inbox"


def test_a_note_written_between_drains_is_never_lost(bridge_root):
    """The old drain read the file and then truncated it, dropping anything
    appended in between -- most likely the `DONE` the session waits on."""
    bridge = _bridge.Bridge(bridge_root)
    notes = bridge_root / _bridge.NOTES

    def append(message: str) -> None:
        with notes.open("a") as fh:
            fh.write(json.dumps({"at": 0, "message": message}) + "\n")

    append("first")
    assert bridge.drain_notes() == ["first"]
    append("second")  # lands after the previous drain finished
    append("third")
    assert bridge.drain_notes() == ["second", "third"]
    assert bridge.drain_notes() == []


def test_a_half_written_note_waits_for_its_newline(bridge_root):
    bridge = _bridge.Bridge(bridge_root)
    notes = bridge_root / _bridge.NOTES
    notes.write_text('{"at": 0, "mess')
    assert bridge.drain_notes() == []
    with notes.open("a") as fh:
        fh.write('age": "whole"}\n')
    assert bridge.drain_notes() == ["whole"]


def test_a_timed_out_ask_records_that_it_gave_up(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--timeout", "1", "Anyone there?"])
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 3 and "no answer" in err
    [request] = [json.loads(p.read_text()) for p in (bridge_root / _bridge.REQUESTS).glob("*.json")]
    answer = bridge_root / _bridge.ANSWERS / f"{request['id']}.json"
    assert _bridge.gave_up_path(answer).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("gave_up", [True, False])
async def test_an_answer_in_the_grace_window_reaches_the_inbox(bridge_root, tmp_path, monkeypatch, gave_up):
    """A past demo: answered 43 s after asking, inside the 30 s grace past a short
    CLI timeout -- recorded as answered, read by nobody, and the session never acted."""
    workspace = tmp_path / "ws"
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(workspace))

    class Cond:
        name = "ask-a0-001"

        class wait:
            @staticmethod
            async def aio():
                return "publish the artifact"

    async def fake_open(*_a, **_k):
        return Cond, 1

    monkeypatch.setattr(_bridge, "open_condition", fake_open)
    bridge = _bridge.Bridge(bridge_root)
    request = {"id": "r1", "question": "Done -- anything else?", "timeout_s": 10}
    if gave_up:
        _bridge._write_atomic(_bridge.gave_up_path(bridge_root / _bridge.ANSWERS / "r1.json"), {"id": "r1"})
    await bridge._service(request, 1)
    late = list((workspace / _bridge.INBOX_SUBDIR).glob("late-*.md")) if workspace.exists() else []
    if gave_up:
        assert len(late) == 1 and "publish the artifact" in late[0].read_text()
    else:
        assert late == [], "an asker still waiting reads the answer file; no duplicate"
    assert (
        json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())["value"] == "publish the artifact"
    )


def test_an_answer_during_the_last_poll_interval_is_not_lost(bridge_root, src_path):
    """qa: the CLI polled, slept 2 s past its deadline, wrote the marker and left; an answer
    landing in that sleep found no marker yet and was read by nobody."""
    proc = _run_ask(bridge_root, src_path, ["--timeout", "1", "Last call?"])
    deadline = time.time() + 10
    while not list((bridge_root / _bridge.REQUESTS).glob("*.json")):
        assert time.time() < deadline
        time.sleep(0.02)
    time.sleep(1.4)  # past the CLI's 1 s deadline, inside its final 2 s sleep, before the marker
    _answer_next(bridge_root, {"value": "ship it"})
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode == 0 and out.strip() == "ship it"


def test_a_late_approve_reads_as_a_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path))
    bridge = _bridge.Bridge(tmp_path / "b")
    bridge._write_late("Force-push?", False)
    bridge._write_late("Force-push?", True)
    bodies = sorted(p.read_text() for p in (tmp_path / _bridge.INBOX_SUBDIR).glob("late-*.md"))
    assert len(bodies) == 2, "two late answers in one second must not overwrite each other"
    assert any("declined" in b for b in bodies) and any("approved" in b for b in bodies)


# --- closing questions that stop being live ----------------------------------------------------


class _Remote:
    """Fake `flyte.remote` + `flyte.ctx`: records what was signalled and aborted, and with what.

    The run holds two conditions of each name, a sub-agent's and ours, the sub-agent's first:
    `Condition.get` scans the whole run and returns the first match unless `action_name` narrows
    it to one parent, so a lookup without it reaches the sub-agent's (recorded in `wrong`).
    `refuse` makes closing a name (or an action) fail; `gate`, when set, holds every close until it is set.
    `invisible[name] = n` makes the first n lookups of `name` return None, as a condition just
    opened does; `gone` names are never found; `phases` sets a condition's `phase`.
    """

    OWN_ACTION = "own-action"

    def __init__(self, monkeypatch):
        import flyte
        import flyte.remote

        self.signalled: list[tuple[str, str]] = []
        self.aborted: list[tuple[str, str]] = []
        self.wrong: list[tuple[str, str]] = []
        self.refuse: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.invisible: dict[str, int] = {}
        self.gone: set[str] = set()
        self.phases: dict[str, str] = {}
        self.lookups: list[str] = []
        self.held = 0  # calls that reached the gate: how a test knows a close is on the wire
        remote = self

        async def held():
            if remote.gate is not None:
                remote.held += 1
                await remote.gate.wait()

        class Action:
            def __init__(self, name):
                self.name = name
                self.abort = SimpleNamespace(aio=self._abort)

            # Instance method with `reason`, as in flyte 2.10.7: calling it on the class is the old bug.
            async def _abort(self, reason="Manually aborted from the SDK."):
                await held()
                if self.name.startswith("subagent-"):
                    remote.wrong.append((self.name, reason))
                    return
                if self.name in remote.refuse:
                    raise RuntimeError(f"{self.name} already ended")
                remote.aborted.append((self.name, reason))

        class Condition:
            def __init__(self, name, parent):
                self.name = name
                self.parent = parent
                own = parent == _Remote.OWN_ACTION
                self.action_name = f"action-of-{name}" if own else f"subagent-action-of-{name}"
                self.phase = remote.phases.get(name, "ACTION_PHASE_PAUSED")
                self.signal = SimpleNamespace(aio=self._signal)

            async def _signal(self, payload):
                await held()
                if self.parent != _Remote.OWN_ACTION:
                    remote.wrong.append((self.name, payload))
                    return
                if self.name in remote.refuse:
                    # What the service answers when the condition is no longer PAUSED (seen live).
                    from connectrpc.code import Code
                    from connectrpc.errors import ConnectError

                    raise ConnectError(
                        Code.FAILED_PRECONDITION,
                        f"{self.name}: condition is ACTION_PHASE_SUCCEEDED, expected PAUSED",
                    )
                remote.signalled.append((self.name, payload))

        async def get_condition(name, run_name, action_name=None):
            assert run_name == "run1"
            remote.lookups.append(name)
            if remote.invisible.get(name, 0) > 0:
                remote.invisible[name] -= 1
                return None
            if not name.startswith("ask-") or name in remote.gone:
                return None
            for parent in ("subagent-1", _Remote.OWN_ACTION):
                if action_name in (None, parent):
                    return Condition(name, parent)
            return None

        async def get_action(run_name, name):
            assert run_name == "run1"
            return Action(name)

        monkeypatch.setattr(
            flyte.remote, "Condition", SimpleNamespace(get=SimpleNamespace(aio=get_condition))
        )
        monkeypatch.setattr(flyte.remote, "Action", SimpleNamespace(get=SimpleNamespace(aio=get_action)))
        monkeypatch.setattr(
            flyte,
            "ctx",
            lambda: SimpleNamespace(
                action=SimpleNamespace(run_name="run1", name=_Remote.OWN_ACTION), attempt_number=0
            ),
        )


class _Opener:
    """Fake `open_condition`: hands out conditions whose `wait` blocks until `release(name, value)`.

    `hold`, when set, keeps `open_condition` itself in flight until it is set.
    """

    def __init__(self, monkeypatch):
        self.opened: list[dict[str, Any]] = []
        self.waiting: dict[str, asyncio.Future[Any]] = {}
        self.hold: asyncio.Event | None = None
        opener = self

        async def fake_open(base, seq, **kwargs):
            if opener.hold is not None:
                await opener.hold.wait()
            name = f"{base}-a0-{seq:03d}"
            opener.opened.append({"name": name, **kwargs})
            future = asyncio.get_running_loop().create_future()
            opener.waiting[name] = future

            class Cond:
                class wait:
                    @staticmethod
                    async def aio():
                        return await future

            Cond.name = name  # type: ignore[attr-defined]  # `Cond` is a local stand-in class
            return Cond, seq

        monkeypatch.setattr(_bridge, "open_condition", fake_open)

    def release(self, name, value=None, error=None):
        future = self.waiting[name]
        future.set_exception(error) if error else future.set_result(value)

    def listening(self, name):
        return not self.waiting[name].done() and not self.waiting[name].cancelled()


def _fast_deadline(monkeypatch):
    """Run the supervisor's wait to its deadline in ~0.1 s, whatever the request's timeout."""
    real_wait_for = asyncio.wait_for

    async def quick(coro, timeout):
        return await real_wait_for(coro, 0.1)

    monkeypatch.setattr(asyncio, "wait_for", quick)


def _events(bridge):
    rows = []
    bridge._on_event = lambda icon, text: rows.append((icon, text))
    return rows


@pytest.mark.asyncio
async def test_the_condition_is_created_with_the_session_timeout_plus_the_grace(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    task = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "timeout_s": 600}, 1))
    await wait_for(lambda: opener.opened)
    assert opener.opened[0]["timeout"] == 600 + _bridge.ASK_GRACE_S == 630
    opener.release("ask-a0-001", "80")
    await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "reason"),
    [
        ("Which port? Default if no answer: use 8080.", "closed: timed out, default taken: use 8080"),
        ("Which port?", "closed: timed out, no default stated"),
    ],
)
async def test_at_the_deadline_a_text_question_is_signalled_closed(
    bridge_root, monkeypatch, question, reason
):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    await bridge._service({"id": "r1", "question": question, "timeout_s": 1}, 1)

    assert remote.signalled == [("ask-a0-001", reason)] and remote.aborted == []
    assert ("🗑️", f"closed ask-a0-001: {reason.removeprefix('closed: ')}") in rows
    assert bridge.questions == {}, "the dashboard's open list drops it"
    assert not opener.listening("ask-a0-001"), "nothing listens after the close"
    answer = json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())
    assert answer["error"] == "nobody answered in time"


@pytest.mark.asyncio
async def test_at_the_deadline_an_approve_question_is_aborted_with_the_reason(bridge_root, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    request = {"id": "r1", "kind": "approve", "question": "Push? Default: no.", "timeout_s": 1}
    await bridge._service(request, 1)

    assert remote.aborted == [("action-of-ask-a0-001", "closed: timed out, default taken: no")]
    assert remote.signalled == [] and bridge.questions == {}
    assert not opener.listening("ask-a0-001")


@pytest.mark.asyncio
async def test_a_failure_to_close_is_a_warning_row_naming_the_condition(bridge_root, monkeypatch):
    _Opener(monkeypatch)  # no remote fake: the real `flyte.ctx()` has no task context here
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    await bridge._service({"id": "r1", "question": "Anyone?", "timeout_s": 1}, 1)
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows)


@pytest.mark.asyncio
async def test_a_flyte_timeout_is_not_an_ask_failure(bridge_root, monkeypatch):
    import flyte.errors

    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    task = asyncio.create_task(bridge._service({"id": "r1", "question": "Anyone?", "timeout_s": 600}, 1))
    await wait_for(lambda: opener.opened)
    opener.release("ask-a0-001", error=flyte.errors.ConditionTimedoutError("timed out"))
    await task
    assert remote.signalled == [] and remote.aborted == [] and bridge.questions == {}


@pytest.mark.asyncio
async def test_an_answer_inside_the_grace_window_still_reaches_the_inbox_and_nothing_is_closed(
    bridge_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _bridge._write_atomic(_bridge.gave_up_path(bridge_root / _bridge.ANSWERS / "r1.json"), {"id": "r1"})
    task = asyncio.create_task(bridge._service({"id": "r1", "question": "Host?", "timeout_s": 1}, 1))
    await wait_for(lambda: opener.opened)
    opener.release("ask-a0-001", "db.internal")
    await task
    [late] = list((tmp_path / "ws" / _bridge.INBOX_SUBDIR).glob("late-*.md"))
    assert "db.internal" in late.read_text()
    assert remote.signalled == [] and remote.aborted == []


@pytest.mark.asyncio
async def test_a_late_signal_after_the_close_does_nothing(bridge_root, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    _Remote(monkeypatch)
    opener = _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    await bridge._service({"id": "r1", "question": "Host?", "timeout_s": 1}, 1)
    with pytest.raises(asyncio.CancelledError):
        await opener.waiting["ask-a0-001"]  # the wait was cancelled by the close
    assert not (tmp_path / "ws").exists(), "no late delivery after the question is closed"


@pytest.mark.asyncio
async def test_abandon_aborts_through_the_instance_with_a_reason(bridge_root, monkeypatch):
    # Regression: `flyte.remote.Action.abort.aio(run_name=..., name=...)` was called as a classmethod.
    remote = _Remote(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    await bridge._abandon("ask-a0-009", "closed: withdrawn: no longer needed", approve=True)
    assert remote.aborted == [("action-of-ask-a0-009", "closed: withdrawn: no longer needed")]
    with pytest.raises(LookupError):
        await bridge._abandon("nope-1", "closed: x", approve=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "approve"])
async def test_withdraw_closes_with_the_reason_and_unblocks_the_waiting_cli(bridge_root, monkeypatch, kind):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(bridge._service({"id": "r1", "kind": kind, "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)

    reason = "closed: answered via direct-a0-012"
    await bridge._withdraw({"id": "withdraw-1", "withdraw": "r1", "reason": "answered via direct-a0-012"})
    if kind == "approve":
        assert remote.aborted == [("action-of-ask-a0-001", reason)]
        opener.release("ask-a0-001", error=RuntimeError("aborted"))
    else:
        assert remote.signalled == [("ask-a0-001", reason)]
        opener.release("ask-a0-001", reason)
    await asker

    assert bridge.questions == {}
    answers = bridge_root / _bridge.ANSWERS
    assert json.loads((answers / "withdraw-1.json").read_text()) == {"id": "withdraw-1", "closed": "r1"}
    assert json.loads((answers / "r1.json").read_text()) == {"id": "r1", "withdrawn": reason}


@pytest.mark.asyncio
async def test_withdraw_finds_a_question_by_condition_name(bridge_root, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    assert await bridge.close_question("ask-a0-001", "withdrawn: no longer needed") is None
    assert remote.signalled == [("ask-a0-001", "closed: withdrawn: no longer needed")]
    opener.release("ask-a0-001", "closed: withdrawn: no longer needed")
    await asker


@pytest.mark.asyncio
async def test_a_failed_withdraw_leaves_the_question_open(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)  # no remote fake: closing fails
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    closed = await bridge.close_question("r1", "no longer needed")
    assert closed is not None and "failed" in closed
    assert "r1" in bridge.questions and "r1" not in bridge._closed
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows)
    opener.release("ask-a0-001", "80")
    await asker
    assert json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())["value"] == "80"


def test_withdraw_cli_closes_and_reports(bridge_root, src_path):
    proc = _run_cli(bridge_root, src_path, ["--withdraw", "r1", "--reason", "answered via direct-a0-012"])
    request = _wait_for_request(bridge_root, "withdraw-")
    assert request["withdraw"] == "r1" and request["reason"] == "answered via direct-a0-012"
    _bridge._write_atomic(
        bridge_root / _bridge.ANSWERS / f"{request['id']}.json", {"id": request["id"], "closed": "r1"}
    )
    out, _ = proc.communicate(timeout=120)
    assert proc.returncode == 0 and "closed r1" in out


def test_withdraw_cli_of_an_unknown_id_is_a_clear_error(bridge_root, src_path):
    proc = _run_cli(bridge_root, src_path, ["--withdraw", "nope", "--reason", "x"])
    request = _wait_for_request(bridge_root, "withdraw-")
    _bridge._write_atomic(
        bridge_root / _bridge.ANSWERS / f"{request['id']}.json",
        {"id": request["id"], "error": "no open question 'nope'"},
    )
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 1 and "could not withdraw nope: no open question 'nope'" in err


def test_withdraw_needs_a_reason(bridge_root, src_path):
    proc = _run_cli(bridge_root, src_path, ["--withdraw", "r1"])
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 2 and "--reason" in err


def test_the_waiting_cli_returns_promptly_when_withdrawn(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--timeout", "60", "Port?"])
    request = _answer_next(bridge_root, {"withdrawn": "closed: answered via direct-a0-012"})
    _, err = proc.communicate(timeout=120)
    assert request["supersedes"] == []
    assert proc.returncode == 4 and "closed: answered via direct-a0-012" in err


def test_supersedes_is_carried_in_the_request(bridge_root, src_path):
    proc = _run_ask(
        bridge_root, src_path, ["--supersedes", "a", "--supersedes", "b", "--timeout", "120", "Q?"]
    )
    request = _answer_next(bridge_root, {"value": "ok"})
    proc.communicate(timeout=120)
    assert request["supersedes"] == ["a", "b"]


@pytest.mark.asyncio
async def test_supersedes_closes_the_old_questions_naming_the_new_one(bridge_root, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    old_text = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    old_approve = asyncio.create_task(
        bridge._service({"id": "r2", "kind": "approve", "question": "Push?"}, 2)
    )
    await wait_for(lambda: len(opener.opened) == 2)
    new = asyncio.create_task(
        bridge._service({"id": "r3", "question": "Port, again?", "supersedes": ["r1", "ask-a0-002"]}, 3)
    )
    await wait_for(lambda: remote.signalled and remote.aborted)

    assert remote.signalled == [("ask-a0-001", "closed: superseded by ask-a0-003")]
    assert remote.aborted == [("action-of-ask-a0-002", "closed: superseded by ask-a0-003")]
    assert list(bridge.questions) == ["r3"]
    opener.release("ask-a0-001", "closed: superseded by ask-a0-003")
    opener.release("ask-a0-002", error=RuntimeError("aborted"))
    opener.release("ask-a0-003", "8080")
    await asyncio.gather(old_text, old_approve, new)
    assert json.loads((bridge_root / _bridge.ANSWERS / "r3.json").read_text())["value"] == "8080"


def _run_cli(root, src_path, args):
    return _run_ask(root, src_path, args)


def _wait_for_request(root, prefix, timeout=60):
    def found():
        return sorted((root / _bridge.REQUESTS).glob(f"{prefix}*.json"))

    wait_for_sync(found, timeout=timeout, what=f"a {prefix} request")
    return json.loads(found()[0].read_text())


# --- a question answered in the session itself (Remote Control, the Claude app) ----------------


@pytest.mark.asyncio
async def test_a_human_message_in_the_session_closes_an_open_text_ask_and_the_cli_returns(
    bridge_root, src_path, monkeypatch
):
    """ask-a0-011 was answered in Remote Control, but stayed PAUSED in Union as if unanswered."""
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    proc = _run_ask(bridge_root, src_path, ["--timeout", "120", "Which port? Default if no answer: 8080."])
    runner = asyncio.create_task(bridge.run())
    try:
        await wait_for(lambda: opener.opened, timeout=60)
        message = "Use 9090, the 8080 range is taken by the metrics sidecar. " + "x" * 600
        await bridge.close_answered_in_session(message, "Claude app", time.time())

        [(name, value)] = remote.signalled
        assert name == "ask-a0-001" and remote.aborted == []
        assert value == f"answered in session (Claude app): {message[:500]}"
        assert ("🗑️", "closed ask-a0-001: answered in session") in rows
        assert bridge.questions == {}
        # The CLI returns on the answer file, without waiting for Flyte to wake the wait.
        out, err = await asyncio.to_thread(proc.communicate, timeout=120)
        assert proc.returncode == 0
        assert "Use 9090" in out and message in out, "the CLI gets the whole message"
        assert "answered in the session (Claude app)" in out + err
        opener.release(name, value)  # Flyte delivers the signal back to the waiting `_service`
        await wait_for(lambda: not bridge._closed and not bridge._pending)
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        if proc.poll() is None:
            proc.kill()


@pytest.mark.asyncio
async def test_a_human_message_in_the_session_aborts_an_open_approve_ask_with_the_reason(
    bridge_root, monkeypatch
):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(
        bridge._service({"id": "r1", "kind": "approve", "question": "Push?", "created_at": 100.0}, 1)
    )
    await wait_for(lambda: opener.opened)
    await bridge.close_answered_in_session("yes, push it", "Claude app", 200.0)

    assert remote.aborted == [("action-of-ask-a0-001", "closed: answered in session (Claude app)")]
    assert remote.signalled == []
    assert ("🗑️", "closed ask-a0-001: answered in session") in rows
    opener.release("ask-a0-001", error=RuntimeError("aborted"))
    await asker
    answer = json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())
    assert answer == {"id": "r1", "value": "yes, push it", "in_session": "Claude app"}
    assert not any(icon == "⚠️" for icon, _ in rows), "the abort is the close, not an ask failure"


def test_an_approve_answered_in_the_session_is_neither_approved_nor_declined(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--approve", "--timeout", "60", "Push?"])
    _answer_next(bridge_root, {"value": "yes, push it", "in_session": "Claude app"})
    out, err = proc.communicate(timeout=120)
    # Exit 0/1 would be read as approved/declined; the decision is in the message.
    assert proc.returncode == _bridge._EXIT_WITHDRAWN
    assert "yes, push it" in out and "approved" not in out and "declined" not in out


@pytest.mark.asyncio
async def test_every_open_ask_is_closed_and_one_opened_after_the_message_is_not(bridge_root, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    older = [
        asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "created_at": 100.0}, 1)),
        asyncio.create_task(
            bridge._service({"id": "r2", "kind": "approve", "question": "Push?", "created_at": 110.0}, 2)
        ),
    ]
    newer = asyncio.create_task(bridge._service({"id": "r3", "question": "Host?", "created_at": 300.0}, 3))
    await wait_for(lambda: len(opener.opened) == 3)
    await bridge.close_answered_in_session("9090, and push it", "Claude app", 200.0)

    assert remote.signalled == [("ask-a0-001", "answered in session (Claude app): 9090, and push it")]
    assert remote.aborted == [("action-of-ask-a0-002", "closed: answered in session (Claude app)")]
    assert list(bridge.questions) == ["r3"], "a question asked after the message was not answered by it"
    opener.release("ask-a0-001", remote.signalled[0][1])
    opener.release("ask-a0-002", error=RuntimeError("aborted"))
    opener.release("ask-a0-003", "db.internal")
    await asyncio.gather(*older, newer)


@pytest.mark.asyncio
async def test_with_no_open_asks_a_session_message_touches_no_condition(bridge_root, monkeypatch):
    remote = _Remote(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    await bridge.close_answered_in_session("Also tidy the README please", "Claude app", time.time())
    assert remote.signalled == [] and remote.aborted == [] and rows == []


@pytest.mark.asyncio
async def test_a_failed_close_in_session_is_a_warning_and_the_question_stays_open(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)  # no remote fake: closing fails
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "created_at": 1.0}, 1))
    await wait_for(lambda: opener.opened)
    await bridge.close_answered_in_session("9090 please", "Claude app", 2.0)
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows)
    assert "r1" in bridge.questions and "r1" not in bridge._closed
    assert not (bridge_root / _bridge.ANSWERS / "r1.json").exists()
    opener.release("ask-a0-001", "80")
    await asker


@pytest.mark.asyncio
async def test_no_late_answer_file_after_a_close_in_session(bridge_root, tmp_path, monkeypatch):
    """The asker had already given up (grace window): the close must not come back as a late answer."""
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _bridge._write_atomic(_bridge.gave_up_path(bridge_root / _bridge.ANSWERS / "r1.json"), {"id": "r1"})
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Host?", "created_at": 1.0}, 1))
    await wait_for(lambda: opener.opened)
    await bridge.close_answered_in_session("db.internal, use the replica", "Claude app", 2.0)
    opener.release("ask-a0-001", remote.signalled[0][1])  # the signal comes back from Flyte
    await asker
    assert not (tmp_path / "ws").exists(), "no late-answer inbox file"


# --- the supervisor's side: which human messages close questions -------------------------------


def _session_transcript(config_dir, workspace, entries):
    import re

    folder = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "s.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def _supervisor(monkeypatch, tmp_path):
    from flyte_agent_factory import agent

    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_inbox, "_seen_human_entries", set())
    monkeypatch.setattr(_inbox, "_directions", [])
    (tmp_path / "ws" / _session_config._INBOX_DIR).mkdir(parents=True)
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Use 9090 for the port", "yes"])
async def test_a_message_typed_in_the_claude_app_closes_the_open_asks(
    bridge_root, tmp_path, monkeypatch, text
):
    """Short replies too: "yes" is the commonest answer to a question, and too short to be direction."""
    from datetime import datetime, timedelta, timezone

    _supervisor(monkeypatch, tmp_path)
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(
        bridge._service({"id": "r1", "question": "Port?", "created_at": time.time()}, 1)
    )
    await wait_for(lambda: opener.opened)
    # Stamped ahead of now: the watcher only takes messages typed after it started.
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    entry = {
        "type": "user",
        "uuid": "u1",
        "timestamp": stamp,
        "origin": {"kind": "human"},
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }
    _session_transcript(tmp_path / "claude", tmp_path / "ws", [entry])
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(cast(Any, SimpleNamespace(sent_texts=[])), timeline, stop)
    )
    await wait_for(lambda: remote.signalled)
    stop.set()
    await watcher

    assert remote.signalled == [("ask-a0-001", f"answered in session (Claude app): {text}")]
    recorded = [d for d in _inbox._directions if text in d]
    assert len(recorded) == (0 if text == "yes" else 1), "a short reply is still not direction"
    opener.release("ask-a0-001", remote.signalled[0][1])
    await asker


@pytest.mark.asyncio
async def test_a_union_direct_message_does_not_close_asks(bridge_root, tmp_path, monkeypatch):
    """`direct-` is Union direction: the asker may well want it *and* an answer, so the ask stays open."""
    _supervisor(monkeypatch, tmp_path)
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "created_at": 1.0}, 1))
    await wait_for(lambda: opener.opened)
    stop = asyncio.Event()
    messages = iter(["Use 9090 for the port, please", "/stop"])

    async def direct(base, seq, **kwargs):
        assert base == "direct"
        return SimpleNamespace(wait=SimpleNamespace(aio=lambda: asyncio.sleep(0, next(messages)))), seq

    monkeypatch.setattr(_inbox, "open_condition", direct)
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    proc = SimpleNamespace(submitted=[])

    async def submit(text, **kwargs):
        proc.submitted.append(text)

    proc.submit = submit
    await _inbox._inbox_loop(cast(Any, proc), timeline, stop, _supervise_support._grant_round)

    assert proc.submitted and any(d.startswith("Union: Use 9090") for d in _inbox._directions)
    assert remote.signalled == [] and remote.aborted == []
    assert list(bridge.questions) == ["r1"]
    opener.release("ask-a0-001", "9090")
    await asker


# --- QA round: closes reach our own condition, and never eat a real answer ---------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "approve"])
async def test_a_close_reaches_our_condition_not_a_sub_agents_of_the_same_name(
    bridge_root, monkeypatch, kind
):
    """Sub-agents run their own Bridge with the same ask-a0-NNN names in the same run.

    `Condition.get` without `action_name` returns the first match in the run, so a close could
    signal a sub-agent's live question -- which reads it as the human's answer -- and leave ours open.
    """
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(bridge._service({"id": "r1", "kind": kind, "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    assert await bridge.close_question("r1", "no longer needed") is None
    assert remote.wrong == [], "the sub-agent's condition was touched"
    assert (remote.signalled or remote.aborted)[0][1] == "closed: no longer needed"
    opener.release("ask-a0-001", "closed: no longer needed" if kind == "text" else None)
    await asker


def _fetch_answer(bridge_root, request_id="r1"):
    path = bridge_root / _bridge.ANSWERS / f"{request_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "real"), [("text", "8080"), ("approve", True)])
async def test_a_union_answer_that_lands_while_a_withdraw_is_in_flight_is_delivered(
    bridge_root, monkeypatch, kind, real
):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "kind": kind, "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    remote.gate = asyncio.Event()
    withdraw = asyncio.create_task(bridge.close_question("r1", "no longer needed"))
    await wait_for(lambda: remote.held)  # the close is on the wire...
    opener.release("ask-a0-001", real)  # ...when the human's answer lands
    await asker
    remote.refuse.add("ask-a0-001" if kind == "text" else "action-of-ask-a0-001")
    remote.gate.set()  # and then the close fails: the condition is already answered

    assert "failed" in (await withdraw or "")
    assert _fetch_answer(bridge_root) == {"id": "r1", "value": real}, "the real answer reaches the asker"
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows)
    assert bridge.questions == {} and bridge._closed == {}


@pytest.mark.asyncio
async def test_a_union_answer_racing_a_close_that_succeeds_anyway_still_wins(bridge_root, monkeypatch):
    """If the backend also accepts the close, the asker must still get the answer, not the close."""
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    remote.gate = asyncio.Event()
    close = asyncio.create_task(bridge.close_answered_in_session("use 9090", "Claude app", time.time()))
    await wait_for(lambda: remote.held)
    opener.release("ask-a0-001", "8080")
    await asker
    remote.gate.set()
    await close
    assert _fetch_answer(bridge_root) == {"id": "r1", "value": "8080"}
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows), "the close is reported failed"
    assert not any(icon == "🗑️" for icon, _ in rows), "nothing claims it was closed"


@pytest.mark.asyncio
async def test_a_racing_union_answer_after_the_asker_gave_up_goes_to_the_inbox(
    bridge_root, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _bridge._write_atomic(_bridge.gave_up_path(bridge_root / _bridge.ANSWERS / "r1.json"), {"id": "r1"})
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Host?"}, 1))
    await wait_for(lambda: opener.opened)
    remote.gate = asyncio.Event()
    withdraw = asyncio.create_task(bridge.close_question("r1", "no longer needed"))
    await wait_for(lambda: remote.held)
    opener.release("ask-a0-001", "db.internal")
    await asker
    remote.refuse.add("ask-a0-001")
    remote.gate.set()
    await withdraw
    [late] = list((tmp_path / "ws" / _bridge.INBOX_SUBDIR).glob("late-*.md"))
    assert "db.internal" in late.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "real", "shown"), [("text", "db.internal", "db.internal"), ("approve", True, "approved")]
)
async def test_an_answer_landing_between_the_deadline_and_the_close_reaches_the_inbox(
    bridge_root, tmp_path, monkeypatch, kind, real, shown
):
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    # The human answered after the wait gave up, so the close is refused...
    remote.refuse.update({"ask-a0-001", "action-of-ask-a0-001"})
    asker = asyncio.create_task(
        bridge._service({"id": "r1", "kind": kind, "question": "Host?", "timeout_s": 1}, 1)
    )
    await wait_for(lambda: any(icon == "⚠️" for icon, _ in rows))
    assert list(bridge.questions) == ["r1"], "still open in Union, so still on the dashboard"
    await asker
    opener.release("ask-a0-001", real)  # ...and their answer is the wait's value
    inbox = tmp_path / "ws" / _bridge.INBOX_SUBDIR
    await wait_for(lambda: list(inbox.glob("late-*.md")))
    [late] = list(inbox.glob("late-*.md"))
    assert shown in late.read_text()
    assert bridge.questions == {}


@pytest.mark.asyncio
async def test_a_failed_deadline_close_with_no_answer_ends_at_flytes_timeout(
    bridge_root, tmp_path, monkeypatch
):
    import flyte.errors

    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(tmp_path / "ws"))
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    remote.refuse.add("ask-a0-001")
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Host?", "timeout_s": 1}, 1))
    await wait_for(lambda: any(icon == "⚠️" for icon, _ in rows))
    await asker
    opener.release("ask-a0-001", error=flyte.errors.ConditionTimedoutError("timed out"))
    await wait_for(lambda: bridge.questions == {})
    assert any(icon == "⚠️" and "ask-a0-001" in text for icon, text in rows)
    assert not (tmp_path / "ws").exists() and bridge.questions == {}


# --- the open questions survive a restart ------------------------------------------------------


@pytest.mark.asyncio
async def test_open_questions_survive_a_restart_and_can_still_be_closed(bridge_root, tmp_path, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    registry = tmp_path / "state" / "bridge" / "questions.json"
    before = _bridge.Bridge(bridge_root, registry=registry)
    asker = asyncio.create_task(
        before._service({"id": "r1", "kind": "approve", "question": "Push?", "created_at": time.time()}, 1)
    )
    await wait_for(lambda: opener.opened)
    [saved] = json.loads(registry.read_text())
    assert saved["id"] == "r1" and saved["condition"] == "ask-a0-001" and saved["kind"] == "approve"
    assert saved["deadline_at"] > time.time()
    asker.cancel()  # the pod goes away

    # A new pod: a fresh bridge directory, the registry restored from the checkpoint.
    after = _bridge.Bridge(tmp_path / "bridge2", registry=registry)
    after.restore()
    assert list(after.questions) == ["r1"]
    assert await after.close_question("ask-a0-001", "superseded by ask-a1-001") is None
    assert remote.aborted == [("action-of-ask-a0-001", "closed: superseded by ask-a1-001")]
    assert json.loads(registry.read_text()) == [], "a closed question leaves the registry"


@pytest.mark.asyncio
async def test_an_answered_question_leaves_the_registry(bridge_root, tmp_path, monkeypatch):
    _Remote(monkeypatch)
    opener = _Opener(monkeypatch)
    registry = tmp_path / "questions.json"
    bridge = _bridge.Bridge(bridge_root, registry=registry)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    opener.release("ask-a0-001", "80")
    await asker
    assert json.loads(registry.read_text()) == []


@pytest.mark.asyncio
async def test_a_restored_question_past_its_deadline_closes_with_its_default(tmp_path, monkeypatch):
    remote = _Remote(monkeypatch)
    registry = tmp_path / "questions.json"
    entry = {
        "id": "r1",
        "kind": "ask",
        "text": "Port? Default if no answer: use 8080.",
        "opened_at": time.time() - 100,
        "applies_at": time.time() - 70,
        "deadline_at": time.time() - 40,
        "default": "Default if no answer: use 8080",
        "condition": "ask-a0-004",
        "timed_out": False,
    }
    registry.write_text(json.dumps([entry]))
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    await wait_for(lambda: remote.signalled)
    assert remote.signalled == [("ask-a0-004", "closed: timed out, default taken: use 8080")]
    assert ("🗑️", "closed ask-a0-004: timed out, default taken: use 8080") in rows
    assert bridge.questions == {}


@pytest.mark.asyncio
async def test_a_restored_question_still_open_closes_at_its_deadline(tmp_path, monkeypatch):
    remote = _Remote(monkeypatch)
    registry = tmp_path / "questions.json"
    entry = {
        "id": "r1",
        "kind": "approve",
        "text": "Push?",
        "opened_at": time.time(),
        "applies_at": time.time(),
        "deadline_at": time.time() + _bridge._CLOSE_MARGIN_S + 0.2,
        "default": "",
        "condition": "ask-a0-002",
        "timed_out": False,
    }
    registry.write_text(json.dumps([entry]))
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    bridge.restore()
    assert remote.aborted == [] and list(bridge.questions) == ["r1"]
    await wait_for(lambda: remote.aborted)
    assert remote.aborted == [("action-of-ask-a0-002", "closed: timed out, no default stated")]


def test_the_supervisor_keeps_its_question_registry_in_checkpointed_state():

    assert _inbox._question_registry_path().is_relative_to(_session_config.STATE_ROOT)


# --- `--timeout 0` means do not wait, not the 6 h default ---------------------------------------


@pytest.mark.parametrize("timeout", ["0", "-5"])
def test_a_zero_or_negative_timeout_asks_nothing_and_returns_at_once(bridge_root, src_path, timeout):
    proc = _run_ask(bridge_root, src_path, ["--timeout", timeout, "Port?"])
    _, err = proc.communicate(timeout=120)
    # "At once" is that nothing was put on the bridge to wait for, not a time limit.
    assert not list((bridge_root / _bridge.REQUESTS).glob("*.json"))
    assert proc.returncode == _bridge._EXIT_NO_ANSWER
    assert "nothing was asked" in err and "will be closed in Union" not in err
    assert list((bridge_root / _bridge.REQUESTS).glob("*.json")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -5])
async def test_a_request_with_no_wait_opens_no_condition(bridge_root, monkeypatch, timeout):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    await bridge._service({"id": "r1", "question": "Port?", "timeout_s": timeout}, 1)
    assert opener.opened == [] and bridge.questions == {}
    assert "no wait requested" in _fetch_answer(bridge_root)["error"]


# --- a session reply while the question is still opening in Union -------------------------------


@pytest.mark.asyncio
async def test_a_session_reply_before_the_condition_opens_closes_it_once_open(bridge_root, monkeypatch):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    opener.hold = asyncio.Event()
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "created_at": 1.0}, 1))
    await wait_for(lambda: "r1" in bridge.questions)
    await bridge.close_answered_in_session("use 9090", "Claude app", 2.0)
    assert remote.signalled == [] and not any(icon == "⚠️" for icon, _ in rows)
    opener.hold.set()
    await wait_for(lambda: remote.signalled)
    assert remote.signalled == [("ask-a0-001", "answered in session (Claude app): use 9090")]
    opener.release("ask-a0-001", remote.signalled[0][1])
    await asker
    assert _fetch_answer(bridge_root) == {"id": "r1", "value": "use 9090", "in_session": "Claude app"}


# --- a restored question past its deadline is forgotten whatever the close does ----------------


def _restored(tmp_path, condition="ask-a0-004", kind="ask", deadline_in=-40.0):
    registry = tmp_path / "questions.json"
    now = time.time()
    entry = {
        "id": "r1",
        "kind": kind,
        "text": "Port?",
        "opened_at": now - 100,
        "applies_at": now - 70,
        "deadline_at": now + deadline_in,
        "default": "",
        "condition": condition,
        "timed_out": False,
    }
    registry.write_text(json.dumps([entry]))
    return registry


@pytest.fixture
def quick_lookup(monkeypatch):
    """A lookup retry window of 0.2 s instead of 5 s."""
    monkeypatch.setattr(_bridge, "_LOOKUP_RETRY_S", 0.02)
    monkeypatch.setattr(_bridge, "_LOOKUP_WINDOW_S", 0.2)


@pytest.mark.asyncio
async def test_a_restored_question_whose_condition_is_gone_is_forgotten(tmp_path, monkeypatch, quick_lookup):
    """A pod down past the deadline: Flyte has already ended the condition, or it is not found."""
    remote = _Remote(monkeypatch)
    remote.gone.add("ask-a0-004")
    registry = _restored(tmp_path)
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    await wait_for(lambda: bridge.questions == {})
    assert len(remote.lookups) > 1, "not found is retried before it counts as gone"
    # Bounded by the lookup window (0.2 s of 0.02 s retries), counted rather than timed: a starved
    # process makes fewer lookups in the window, never more.
    assert len(remote.lookups) <= 12, "bounded by the lookup window"
    assert json.loads(registry.read_text()) == []
    assert any(icon == "🗑️" and "ask-a0-004" in text and "gone" in text for icon, text in rows)
    assert not any(icon == "⚠️" for icon, _ in rows)


@pytest.mark.asyncio
async def test_a_restored_question_flyte_already_resolved_is_forgotten(tmp_path, monkeypatch):
    remote = _Remote(monkeypatch)
    remote.refuse.add("ask-a0-004")  # the fake's signal raises "... already signalled"
    registry = _restored(tmp_path)
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    await wait_for(lambda: bridge.questions == {})
    assert json.loads(registry.read_text()) == []
    assert ("🗑️", "closed ask-a0-004: closed by Flyte's timeout") in rows
    assert not any(icon == "⚠️" for icon, _ in rows)


@pytest.mark.asyncio
async def test_a_restored_question_whose_close_fails_otherwise_is_forgotten_with_a_warning(
    tmp_path, monkeypatch
):
    _Opener(monkeypatch)  # no remote fake: no task context, so the close fails for another reason
    registry = _restored(tmp_path)
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    await wait_for(lambda: bridge.questions == {})
    assert json.loads(registry.read_text()) == []
    assert any(icon == "⚠️" and "ask-a0-004" in text for icon, text in rows)


@pytest.mark.asyncio
async def test_a_failed_close_before_the_deadline_keeps_the_restored_question(
    tmp_path, monkeypatch, quick_lookup
):
    remote = _Remote(monkeypatch)
    remote.gone.add("ask-a0-004")
    registry = _restored(tmp_path, deadline_in=3600)
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    bridge.restore()
    closed = await bridge.close_question("r1", "no longer needed")
    assert closed is not None and "failed" in closed
    assert list(bridge.questions) == ["r1"]


# --- a condition just opened may not be visible to `Condition.get` yet --------------------------


@pytest.mark.asyncio
async def test_a_queued_session_close_waits_for_the_new_condition_to_be_visible(
    bridge_root, monkeypatch, quick_lookup
):
    # The question becomes visible after three lookups; the window must outlast them however slow the machine.
    monkeypatch.setattr(_bridge, "_LOOKUP_WINDOW_S", 30.0)
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    remote.invisible["ask-a0-001"] = 3  # None for the first three lookups, as seen live
    opener.hold = asyncio.Event()
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?", "created_at": 1.0}, 1))
    await wait_for(lambda: "r1" in bridge.questions)
    await bridge.close_answered_in_session("use 9090", "Claude app", 2.0)
    opener.hold.set()
    await wait_for(lambda: remote.signalled)
    assert remote.signalled == [("ask-a0-001", "answered in session (Claude app): use 9090")]
    assert not any(icon == "⚠️" for icon, _ in rows)
    opener.release("ask-a0-001", remote.signalled[0][1])
    await asker
    assert _fetch_answer(bridge_root)["in_session"] == "Claude app"


@pytest.mark.asyncio
async def test_a_withdraw_right_after_opening_waits_for_the_condition_to_be_visible(
    bridge_root, monkeypatch, quick_lookup
):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    remote.invisible["ask-a0-001"] = 3
    bridge = _bridge.Bridge(bridge_root)
    asker = asyncio.create_task(bridge._service({"id": "r1", "question": "Port?"}, 1))
    await wait_for(lambda: opener.opened)
    assert await bridge.close_question("r1", "no longer needed") is None
    assert remote.signalled == [("ask-a0-001", "closed: no longer needed")]
    opener.release("ask-a0-001", "closed: no longer needed")
    await asker


@pytest.mark.asyncio
async def test_a_lookup_that_never_finds_the_condition_gives_up_after_the_window(
    monkeypatch, quick_lookup, bridge_root
):
    remote = _Remote(monkeypatch)
    remote.gone.add("ask-a0-009")
    bridge = _bridge.Bridge(bridge_root)
    started = time.monotonic()
    with pytest.raises(LookupError):
        await bridge._abandon("ask-a0-009", "closed: x", approve=False)
    assert time.monotonic() - started > 0.15, "it kept looking for the whole window"
    assert len(remote.lookups) <= 12, "and no longer: bounded by the window"


@pytest.mark.asyncio
async def test_a_restored_question_flyte_already_timed_out_is_forgotten_without_a_signal(
    tmp_path, monkeypatch
):
    remote = _Remote(monkeypatch)
    remote.phases["ask-a0-004"] = "ACTION_PHASE_TIMED_OUT"
    registry = _restored(tmp_path)
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    await wait_for(lambda: bridge.questions == {})
    assert remote.signalled == [] and json.loads(registry.read_text()) == []
    assert ("🗑️", "closed ask-a0-004: closed by Flyte's timeout") in rows


def test_a_refused_signal_on_an_ended_condition_is_recognised():
    # Live (QA round 3): signalling a condition that already ended raises ConnectError FAILED_PRECONDITION
    # "condition is ACTION_PHASE_TIMED_OUT, expected PAUSED" -- never the word "already".
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from flyte_agent_factory import _bridge

    ended = ConnectError(Code.FAILED_PRECONDITION, "condition is ACTION_PHASE_TIMED_OUT, expected PAUSED")
    assert _bridge._ended_on_server(ended)
    assert not _bridge._ended_on_server(ConnectError(Code.UNAVAILABLE, "connection reset"))
    assert not _bridge._ended_on_server(RuntimeError("boom"))


class _AskCondition:
    """A condition whose wait is resolved by the test, standing in for Union."""

    def __init__(self, name):
        self.name = name
        self.value = asyncio.get_running_loop().create_future()
        outer = self

        class wait:
            @staticmethod
            async def aio():
                return await outer.value

        self.wait = wait


@pytest.fixture
def ask_conditions(monkeypatch):
    opened: dict[str, Any] = {}

    async def open_condition(*args, **kwargs):
        name = f"ask-a0-{len(opened) + 1:03d}"
        opened[name] = _AskCondition(name)
        return opened[name], len(opened)

    async def abandon(self, name, reason, *, approve):
        if not opened[name].value.done():
            opened[name].value.set_result(reason)

    monkeypatch.setattr(_bridge, "open_condition", open_condition)
    monkeypatch.setattr(_bridge.Bridge, "_abandon", abandon)
    return opened


@pytest.mark.asyncio
async def test_an_answer_is_recorded_no_later_than_the_close(tmp_path, ask_conditions):
    bridge = _bridge.Bridge(tmp_path / "bridge")
    asker = asyncio.create_task(bridge._service({"id": "q1", "question": "Port?", "timeout_s": 600}, 1))
    await wait_for(lambda: ask_conditions)
    ask_conditions["ask-a0-001"].value.set_result("8080")
    await asker
    [ask] = bridge.ask_history
    assert ask["answered_at"] <= ask["closed_at"]


@pytest.mark.asyncio
async def test_an_ask_answered_in_the_session_records_when(tmp_path, ask_conditions):
    bridge = _bridge.Bridge(tmp_path / "bridge")
    asker = asyncio.create_task(bridge._service({"id": "q1", "question": "Size?", "timeout_s": 600}, 1))
    await wait_for(lambda: ask_conditions)
    await bridge.close_answered_in_session("large", "Claude app", time.time())
    await asker
    [ask] = bridge.ask_history
    assert ask["opened_at"] <= ask["answered_at"] <= ask["closed_at"]


@pytest.mark.asyncio
async def test_an_ask_nobody_answered_has_no_answer_time(tmp_path, ask_conditions, monkeypatch):
    monkeypatch.setattr(_bridge, "ASK_GRACE_S", 0)
    monkeypatch.setattr(_bridge, "_CLOSE_MARGIN_S", 0)
    bridge = _bridge.Bridge(tmp_path / "bridge")
    await bridge._service({"id": "q1", "question": "Name?", "timeout_s": 0.05}, 1)
    [ask] = bridge.ask_history
    assert ask.get("answered_at") is None
    assert ask["closed_at"] is not None


@pytest.mark.asyncio
async def test_ask_history_keeps_the_newest_200(tmp_path, ask_conditions, monkeypatch):
    monkeypatch.setattr(_bridge, "ASK_GRACE_S", 0)
    monkeypatch.setattr(_bridge, "_CLOSE_MARGIN_S", 0)
    bridge = _bridge.Bridge(tmp_path / "bridge")
    for i in range(230):
        await bridge._service({"id": f"q{i}", "question": "q", "timeout_s": 0.0001}, i + 1)
    assert len(bridge.ask_history) == 200
    assert [ask["id"] for ask in bridge.ask_history][:2] == ["q30", "q31"]


@pytest.mark.asyncio
async def test_closed_asks_survive_a_restart_once(tmp_path, ask_conditions):
    registry = tmp_path / "state" / "bridge" / "questions.json"
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    asker = asyncio.create_task(bridge._service({"id": "q1", "question": "Port?", "timeout_s": 600}, 1))
    await wait_for(lambda: ask_conditions)
    ask_conditions["ask-a0-001"].value.set_result("8080")
    await asker
    before = [dict(ask) for ask in bridge.ask_history]
    restarted = _bridge.Bridge(tmp_path / "bridge2", registry=registry)
    restarted.restore()
    restarted.restore()
    assert restarted.ask_history == before


@pytest.mark.asyncio
async def test_the_supervisor_can_open_a_question_of_its_own(bridge_root):
    """For what a parked or out-of-usage session cannot say: the owner gets an ask in Union."""
    bridge = _bridge.Bridge(bridge_root)
    request_id = bridge.notify_owner("Parked; reply to keep it.", 600)
    [request] = [json.loads(p.read_text()) for p in (bridge_root / _bridge.REQUESTS).glob("*.json")]
    assert request["id"] == request_id
    assert (request["question"], request["timeout_s"]) == ("Parked; reply to keep it.", 600)
    assert not bridge.answered_since(0)
    bridge.ask_history.append({"id": "x", "answered_at": 100.0})
    assert bridge.answered_since(50) and not bridge.answered_since(150)


@pytest.mark.asyncio
async def test_an_owners_answer_to_a_supervisor_question_reaches_the_session_inbox(
    bridge_root, tmp_path, monkeypatch
):
    """Nothing waits on the park notice, so its answer would be written to a file nobody reads."""
    workspace = tmp_path / "ws"
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", str(workspace))

    class Cond:
        name = "ask-a0-001"

        class wait:
            @staticmethod
            async def aio():
                return "keep going, I am back"

    async def fake_open(*_a, **_k):
        return Cond, 1

    monkeypatch.setattr(_bridge, "open_condition", fake_open)
    bridge = _bridge.Bridge(bridge_root)
    request_id = bridge.notify_owner("Parked; reply to keep it.", 600)
    [request] = [json.loads(p.read_text()) for p in (bridge_root / _bridge.REQUESTS).glob("*.json")]
    await bridge._service(request, 1)

    [late] = list((workspace / _bridge.INBOX_SUBDIR).glob("late-*.md"))
    assert request_id == request["id"] and "keep going, I am back" in late.read_text()


# --- approvals for a browser identity (`ask-human --approve --browser`) -------------------------


def _browser_bridge(bridge_root):
    bridge = _bridge.Bridge(bridge_root)
    bridge.browser_gate = _browser_gate.Gate(["github"], environ={})  # approvals need no listener
    return bridge


def _browser_request(identity="github", **extra):
    return {"id": "r1", "kind": "approve", "question": "Merge?", "browser": identity, **extra}


@pytest.mark.asyncio
async def test_a_yes_to_a_browser_approval_opens_the_gates_window(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _browser_bridge(bridge_root)
    task = asyncio.create_task(bridge._service(_browser_request(), 1))
    await wait_for(lambda: opener.opened)
    assert not bridge.browser_gate.approved("github")  # asking is not approving
    assert "github" in opener.opened[0]["prompt"], "the owner is told which identity"
    opener.release("ask-a0-001", True)
    await task
    assert bridge.browser_gate.approved("github")
    assert json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())["value"] is True


@pytest.mark.asyncio
async def test_a_no_to_a_browser_approval_opens_nothing(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _browser_bridge(bridge_root)
    task = asyncio.create_task(bridge._service(_browser_request(), 1))
    await wait_for(lambda: opener.opened)
    opener.release("ask-a0-001", False)
    await task
    assert not bridge.browser_gate.approved("github")


@pytest.mark.asyncio
async def test_a_timed_out_browser_approval_opens_nothing(bridge_root, monkeypatch):
    _Remote(monkeypatch), _Opener(monkeypatch)
    _fast_deadline(monkeypatch)
    bridge = _browser_bridge(bridge_root)
    await bridge._service(_browser_request(timeout_s=1), 1)
    assert not bridge.browser_gate.approved("github")


@pytest.mark.asyncio
async def test_a_withdrawn_browser_approval_opens_nothing(bridge_root, monkeypatch):
    _Remote(monkeypatch)
    opener = _Opener(monkeypatch)
    bridge = _browser_bridge(bridge_root)
    asker = asyncio.create_task(bridge._service(_browser_request(), 1))
    await wait_for(lambda: opener.opened)
    await bridge._withdraw({"id": "withdraw-1", "withdraw": "r1", "reason": "not needed"})
    opener.release("ask-a0-001", error=RuntimeError("aborted"))
    await asker
    assert not bridge.browser_gate.approved("github")


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [False, True])
async def test_a_browser_approval_for_an_identity_that_is_not_granted_is_an_error_answer(
    bridge_root, monkeypatch, granted
):
    opener = _Opener(monkeypatch)
    bridge = _browser_bridge(bridge_root) if granted else _bridge.Bridge(bridge_root)  # no gate at all
    await bridge._service(_browser_request("linkedin"), 1)
    answer = json.loads((bridge_root / _bridge.ANSWERS / "r1.json").read_text())
    assert "no grant for browser identity 'linkedin'" in answer["error"]
    assert opener.opened == [], "the owner is not asked about something the project cannot use"


def test_the_cli_writes_the_identity_into_an_approve_request(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--browser", "github", "--timeout", "120", "Merge PR 3?"])
    request = _answer_next(bridge_root, {"value": True})
    out, _ = proc.communicate(timeout=120)
    assert request["browser"] == "github" and request["kind"] == "approve"
    assert out.strip() == "approved"


def test_the_cli_leaves_browser_out_of_an_ordinary_request(bridge_root, src_path):
    proc = _run_ask(bridge_root, src_path, ["--approve", "--timeout", "120", "Push?"])
    request = _answer_next(bridge_root, {"value": True})
    proc.communicate(timeout=120)
    assert "browser" not in request


@pytest.mark.parametrize("name", ["Github", "a b", "x" * 21, "../x", ""])
def test_the_cli_rejects_a_bad_identity_name_and_asks_nothing(bridge_root, src_path, name):
    proc = _run_ask(bridge_root, src_path, ["--browser", name, "--timeout", "120", "Merge?"])
    _, err = proc.communicate(timeout=120)
    assert proc.returncode == 2 and "is not an identity name" in err
    assert not list((bridge_root / _bridge.REQUESTS).glob("*.json"))


# --- triage: engineering judgement is decided in the factory, the rest goes to the owner --------


def _ask(request_id="r1", question="Split _tools.py?", **extra):
    return {"id": request_id, "question": question, "timeout_s": 600, "created_at": time.time(), **extra}


def _engineering(decision="Put new tools in sibling modules.", reasoning="Leaves upstream alone.", **kw):
    return _judgement.Judgement("engineering", decision, reasoning, **kw)


def _owner(why="spends money", recommendation="Decline: too dear."):
    return _judgement.Judgement("owner", why_owner=why, recommendation=recommendation)


def _judge_with(bridge, *results):
    """Install a fake judge that returns (or raises) each result in turn and records the requests."""
    queue = list(results)
    bridge.judge = judge = _FakeJudge(queue)
    return judge


class _FakeJudge:
    def __init__(self, queue):
        self.queue = queue
        self.requests: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.cancelled = False
        self.finished = False

    async def __call__(self, request):
        self.requests.append(request)
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.finished = True
        result = self.queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _answer_file(root, request_id="r1"):
    return json.loads((root / _bridge.ANSWERS / f"{request_id}.json").read_text())


def _pending(root, request_id="r1"):
    return _bridge.pending_path(root / _bridge.ANSWERS / f"{request_id}.json")


async def test_an_engineering_call_is_decided_without_opening_a_condition(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    judge = _judge_with(bridge, _engineering())
    await bridge._serve(_ask(), 1)
    assert opener.opened == [] and bridge.questions == {}
    [decision] = bridge.decisions
    assert decision["id"] == "decision-001" and decision["request"] == "r1"
    assert decision["decision"] == "Put new tools in sibling modules." and decision["kind"] == "text"
    assert _answer_file(bridge_root)["decided"] == decision
    assert not _pending(bridge_root).exists()
    assert judge.requests[0]["question"] == "Split _tools.py?"
    assert [icon for icon, _ in rows] == ["🧭", "⚖️"]
    assert rows[1][1].startswith("decided decision-001 (engineering judgement): Put new tools")
    assert "why: Leaves upstream alone." in rows[1][1]


async def test_a_decided_ask_is_not_an_owner_ask_in_the_metrics(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _engineering())
    await bridge._serve(_ask(), 1)
    [ask] = bridge.ask_history
    assert ask["decided"] == "decision-001" and ask["closed_at"]
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    snapshot = timeline.snapshot()
    assert snapshot["metrics"]["asks"] == [] and snapshot["needs_you"] == []
    assert [d["id"] for d in snapshot["decisions"]] == ["decision-001"]


def test_the_cli_prints_a_text_decision(bridge_root, capsys):
    path = bridge_root / _bridge.ANSWERS / "r1.json"
    decided = {"id": "decision-007", "decision": "Use sibling modules.", "reasoning": "It is simplest."}
    path.write_text(json.dumps({"id": "r1", "decided": decided}))
    assert _bridge._read_answer(path, approve=False) == 0
    out, err = capsys.readouterr()
    assert out.splitlines() == [
        "Decided in the factory (decision-007, engineering judgement — not escalated to the owner): "
        "Use sibling modules.",
        "Why: It is simplest.",
    ]
    assert "Act on this decision" in err and "owner direction that overrules it wins" in err


@pytest.mark.parametrize(
    ("approve", "code", "stdout"), [(True, 0, "approved"), (False, 1, "declined"), (None, 4, "Decided in")]
)
def test_the_cli_reads_a_yes_no_decision_in_the_exit_code(bridge_root, capsys, approve, code, stdout):
    path = bridge_root / _bridge.ANSWERS / "r1.json"
    decided = {"id": "decision-001", "decision": "Neither.", "reasoning": "Because.", "approve": approve}
    path.write_text(json.dumps({"id": "r1", "decided": decided}))
    assert _bridge._read_answer(path, approve=True) == code
    out, err = capsys.readouterr()
    assert out.startswith(stdout) and "Why: Because." in out and "Act on this decision" in err


async def test_an_owner_call_opens_a_condition_with_the_triage_block(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, _owner())
    task = asyncio.create_task(bridge._serve(_ask(question="Buy a licence?"), 1))
    await wait_for(lambda: opener.opened)
    prompt = opener.opened[0]["prompt"]
    assert prompt.startswith("Buy a licence?")
    assert (
        "**Factory triage:** owner-reserved — spends money. **Recommendation:** Decline: too dear." in prompt
    )
    assert bridge.questions["r1"]["triage"] == "spends money"
    assert bridge.questions["r1"]["text"] == "Buy a licence?"
    assert ("🙋", "for the owner (spends money)") in rows
    assert not _pending(bridge_root).exists(), "the owner's clock runs once the condition is open"
    opener.release("ask-a0-001", "no")
    await task
    assert _answer_file(bridge_root)["value"] == "no" and bridge.decisions == []


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (RuntimeError("boom"), "judge could not decide: RuntimeError: boom"),
        (_judgement.Judgement("owner", why_owner="unsure: maybe a credential"), "unsure: maybe a credential"),
        (
            _judgement.Judgement("owner", why_owner="judge could not decide: its answer could not be read"),
            None,
        ),
    ],
    ids=["raises", "unsure", "unparseable"],
)
async def test_a_judge_that_fails_or_is_unsure_sends_the_question_to_the_owner(
    bridge_root, monkeypatch, result, why
):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, result)
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: opener.opened)
    assert any(icon == "🙋" and text.startswith("for the owner (") for icon, text in rows)
    if why:
        assert ("🙋", f"for the owner ({why})") in rows
    opener.release("ask-a0-001", "ok")
    await task


async def test_a_judge_that_takes_too_long_sends_the_question_to_the_owner(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    monkeypatch.setattr(_bridge, "JUDGE_CAP_S", 0.05)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _engineering())
    judge.gate = asyncio.Event()  # never set
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: opener.opened)
    assert bridge.questions["r1"]["triage"].startswith("judge could not decide: TimeoutError")
    opener.release("ask-a0-001", "ok")
    await task


async def test_an_engineering_call_without_reasoning_is_never_delivered(bridge_root, monkeypatch):
    """Whatever judge is plugged in, a decision the owner cannot read the reasons for goes to the owner."""
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _engineering(reasoning="  "))
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: opener.opened)
    assert bridge.questions["r1"]["triage"].startswith("judge could not decide: ")
    assert "no reasoning" in bridge.questions["r1"]["triage"]
    assert bridge.decisions == []
    opener.release("ask-a0-001", "ok")
    await task
    assert "decided" not in _answer_file(bridge_root) and _answer_file(bridge_root)["value"] == "ok"


async def test_stopping_the_bridge_cancels_and_awaits_a_judge_still_deciding(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _engineering())
    judge.gate = asyncio.Event()  # never set: the judge is still deciding when the bridge stops
    runner = asyncio.create_task(bridge.run())
    bridge._spawn(bridge._serve(_ask(), 1))
    await wait_for(lambda: judge.requests)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    # Once run() has returned, nothing it started is still running: no yield happens in between.
    assert judge.cancelled and judge.finished
    assert bridge._pending == set() and bridge.decisions == []


async def test_a_browser_approval_is_never_judged(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _browser_bridge(bridge_root)
    judge = _judge_with(bridge, _engineering())
    task = asyncio.create_task(bridge._serve(_browser_request(), 1))
    await wait_for(lambda: opener.opened)
    assert judge.requests == [] and "Factory triage" not in opener.opened[0]["prompt"]
    opener.release("ask-a0-001", False)
    await task


async def test_without_a_judge_the_scan_asks_the_owner_as_before(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    assert bridge.judge is None
    _bridge._write_atomic(bridge_root / _bridge.REQUESTS / "r1.json", _ask())
    bridge._scan()
    await wait_for(lambda: opener.opened)
    assert bridge.decisions == [] and "triage" not in bridge.questions["r1"]
    opener.release("ask-a0-001", "x")
    await asyncio.gather(*bridge._pending)


async def test_the_marker_is_there_while_judging_and_gone_when_decided(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _engineering())
    judge.gate = asyncio.Event()
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: _pending(bridge_root).exists())
    judge.gate.set()
    await task
    assert not _pending(bridge_root).exists() and "decided" in _answer_file(bridge_root)


async def test_the_cli_does_not_count_down_while_the_marker_exists(bridge_root, monkeypatch):
    monkeypatch.setattr(_bridge, "_POLL_INTERVAL_S", 0.05)
    cli = asyncio.create_task(asyncio.to_thread(_bridge.ask_human_cli, ["Which?", "--timeout", "1"]))
    await wait_for(lambda: list((bridge_root / _bridge.REQUESTS).glob("*.json")))
    [request] = (bridge_root / _bridge.REQUESTS).glob("*.json")
    answer = bridge_root / _bridge.ANSWERS / request.name
    _bridge._write_atomic(_bridge.pending_path(answer), {})
    await asyncio.sleep(2.5)  # more than two timeouts of judging
    assert not cli.done()
    _bridge._write_atomic(answer, {"id": request.stem, "value": "A"})
    assert await cli == 0


async def test_the_cli_counts_down_again_once_the_marker_is_gone(bridge_root, monkeypatch):
    monkeypatch.setattr(_bridge, "_POLL_INTERVAL_S", 0.05)
    cli = asyncio.create_task(asyncio.to_thread(_bridge.ask_human_cli, ["Which?", "--timeout", "1"]))
    await wait_for(lambda: list((bridge_root / _bridge.REQUESTS).glob("*.json")))
    [request] = (bridge_root / _bridge.REQUESTS).glob("*.json")
    marker = _bridge.pending_path(bridge_root / _bridge.ANSWERS / request.name)
    _bridge._write_atomic(marker, {})
    await asyncio.sleep(1.5)
    started = time.monotonic()
    marker.unlink()
    assert await cli == 3
    assert time.monotonic() - started >= 0.5, "the timeout restarts when the question opens"


async def test_the_owner_timeout_counts_from_the_moment_the_condition_opens(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _owner())
    judge.gate = asyncio.Event()
    asked = time.time() - 1000  # the CLI wrote the request long ago: judging and queueing took that long
    task = asyncio.create_task(bridge._serve(_ask(created_at=asked, timeout_s=600), 1))
    await wait_for(lambda: judge.requests)
    judge.gate.set()
    await wait_for(lambda: opener.opened)
    entry = bridge.questions["r1"]
    assert entry["opened_at"] > asked + 900
    assert entry["applies_at"] == pytest.approx(entry["opened_at"] + 600)
    assert entry["deadline_at"] == pytest.approx(entry["opened_at"] + 600 + _bridge.ASK_GRACE_S)
    assert opener.opened[0]["timeout"] == 600 + _bridge.ASK_GRACE_S
    opener.release("ask-a0-001", "x")
    await task


async def test_owner_questions_reach_the_owner_one_at_a_time(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _owner("spends money"), _owner("publishes"))
    first = asyncio.create_task(bridge._serve(_ask("r1", "Buy?"), 1))
    await wait_for(lambda: opener.opened)
    second = asyncio.create_task(bridge._serve(_ask("r2", "Publish?"), 2))
    await wait_for(lambda: "r2" in bridge.questions)
    await asyncio.sleep(0.1)
    assert [o["name"] for o in opener.opened] == ["ask-a0-001"]
    assert bridge.questions["r2"]["condition"] == "" and bridge.questions["r2"]["triage"] == "publishes"
    assert _pending(bridge_root, "r2").exists(), "its CLI keeps waiting, with its timeout stopped"
    assert not _pending(bridge_root, "r1").exists()
    opener.release("ask-a0-001", "no")
    await first
    await wait_for(lambda: len(opener.opened) == 2)
    assert not _pending(bridge_root, "r2").exists()
    opener.release("ask-a0-002", "no")
    await second


async def test_a_queued_question_whose_cli_gave_up_is_never_opened(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _owner(), _owner())
    first = asyncio.create_task(bridge._serve(_ask("r1"), 1))
    await wait_for(lambda: opener.opened)
    second = asyncio.create_task(bridge._serve(_ask("r2"), 2))
    await wait_for(lambda: "r2" in bridge.questions)
    _bridge._write_atomic(_bridge.gave_up_path(bridge_root / _bridge.ANSWERS / "r2.json"), {})
    opener.release("ask-a0-001", "x")
    await first
    await second
    assert len(opener.opened) == 1 and "r2" not in bridge.questions
    assert not _pending(bridge_root, "r2").exists()


async def test_a_withdraw_while_judging_opens_nothing_and_tells_the_cli(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _owner())
    judge.gate = asyncio.Event()
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: judge.requests)
    assert await bridge.close_question("r1", "answered via direct-a0-012") is None
    assert _answer_file(bridge_root) == {
        "id": "r1",
        "withdrawn": "closed: answered via direct-a0-012",
    }
    judge.gate.set()
    await task
    assert opener.opened == [] and bridge.questions == {} and not _pending(bridge_root).exists()


async def test_a_withdraw_while_judging_records_no_decision(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _engineering())
    judge.gate = asyncio.Event()
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: judge.requests)
    await bridge.close_question("r1", "no longer needed")
    judge.gate.set()
    await task
    assert bridge.decisions == [] and "withdrawn" in _answer_file(bridge_root)


async def test_a_withdraw_while_queued_closes_it_at_once(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _owner(), _owner())
    first = asyncio.create_task(bridge._serve(_ask("r1"), 1))
    await wait_for(lambda: opener.opened)
    second = asyncio.create_task(bridge._serve(_ask("r2"), 2))
    await wait_for(lambda: "r2" in bridge.questions)
    assert await bridge.close_question("r2", "not needed") is None
    assert "withdrawn" in _answer_file(bridge_root, "r2")  # before r1 is settled
    opener.release("ask-a0-001", "x")
    await first
    await second
    assert len(opener.opened) == 1 and "r2" not in bridge.questions


async def test_a_session_reply_while_judging_closes_the_ask(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _owner())
    judge.gate = asyncio.Event()
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: judge.requests)
    await bridge.close_answered_in_session("go ahead", "Claude app", time.time())
    assert _answer_file(bridge_root)["in_session"] == "Claude app"
    judge.gate.set()
    await task
    assert opener.opened == []


async def test_a_new_ask_replaces_the_one_that_holds_the_owners_turn(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    remote = _Remote(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _owner(), _owner())
    first = asyncio.create_task(bridge._serve(_ask("r1", "Old?"), 1))
    await wait_for(lambda: opener.opened)
    second = asyncio.create_task(bridge._serve(_ask("r2", "New?", supersedes=["r1"]), 2))
    await wait_for(lambda: remote.signalled)  # r1 is closed without waiting for the turn it holds
    opener.release("ask-a0-001", remote.signalled[0][1])
    await wait_for(lambda: len(opener.opened) == 2)
    assert opener.opened[1]["name"] == "ask-a0-002"
    await first
    opener.release("ask-a0-002", "x")
    await second


async def test_decisions_are_numbered_per_project_persisted_and_restored(tmp_path, monkeypatch):
    _Opener(monkeypatch)
    registry = tmp_path / "state" / "questions.json"
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    _judge_with(bridge, _engineering("First."), _engineering("Second."))
    await bridge._serve(_ask("r1"), 1)
    assert (registry.parent / "decisions.json").is_file()

    restarted = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    _judge_with(restarted, _engineering("Third."))
    restarted.restore()
    assert [d["id"] for d in restarted.decisions] == ["decision-001"]
    await restarted._serve(_ask("r2"), 2)
    assert [d["id"] for d in restarted.decisions] == ["decision-001", "decision-002"]


async def test_restore_drops_a_pending_marker_left_by_the_old_pod(tmp_path):
    registry = tmp_path / "state" / "questions.json"
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    stale = _pending(bridge.root, "old")
    _bridge._write_atomic(stale, {})
    bridge.restore()
    assert not stale.exists()


async def test_decisions_keep_the_newest_and_bound_their_text(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    monkeypatch.setattr(_bridge, "DECISIONS_MAX", 3)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, *[_engineering(f"d{n}", "r" * 10_000) for n in range(5)])
    for n in range(5):
        await bridge._serve(_ask(f"r{n}"), n)
    assert [d["id"] for d in bridge.decisions] == ["decision-003", "decision-004", "decision-005"]
    assert len(bridge.decisions[0]["reasoning"]) <= _bridge_triage._DECISION_TEXT_MAX


async def test_owner_direction_naming_a_decision_overrules_it(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, _engineering(), _engineering("Other."))
    await bridge._serve(_ask("r1"), 1)
    await bridge._serve(_ask("r2"), 2)
    assert bridge.overrule("decision-001 is wrong, and decision-009 never happened", "Union") == [
        "decision-001"
    ]
    first, second = bridge.decisions
    assert first["overruled_by"].startswith("Union: decision-001 is wrong") and first["overruled_at"]
    assert "overruled_at" not in second
    assert ("↩️", "decision-001 overruled by owner direction (Union)") in rows
    assert bridge.overrule("carry on", "Union") == []


async def test_naming_a_decision_without_overruling_it_is_only_a_reference(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, _engineering())
    await bridge._serve(_ask("r1"), 1)
    assert bridge.overrule("decision-001 was right, keep it. Thanks.", "Union") == []
    assert "overruled_at" not in bridge.decisions[0]
    assert ("📎", "decision-001 referenced by owner direction (Union)") in rows
    # The overruling word must be in the sentence that names the decision.
    assert bridge.overrule("Keep decision-001. Stop the other project.", "Union") == []
    assert bridge.overrule("see decision-0011 instead", "Union") == [], "no partial id match"
    assert bridge.overrule("Overrule Decision-001: split it", "Union") == ["decision-001"]


@pytest.mark.parametrize(
    "text",
    [
        "decision-001 is right, do not change it.",
        "Good call on decision-001, no need to undo it.",
        "I agree with decision-001, stop second-guessing it.",
        "decision-001 was not wrong.",
        "Keep decision-001, do not revisit it.",
        "decision-001 is right, don't change it.",
    ],
)
async def test_direction_that_endorses_a_decision_does_not_overrule_it(bridge_root, monkeypatch, text):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, _engineering())
    await bridge._serve(_ask("r1"), 1)
    assert bridge.overrule(text, "Union") == []
    assert "overruled_at" not in bridge.decisions[0]
    assert ("📎", "decision-001 referenced by owner direction (Union)") in rows


@pytest.mark.parametrize(
    "text",
    [
        "Overrule decision-001.",
        "decision-001 was wrong.",
        "Don't follow decision-001.",
        "Reverse decision-001, split the module instead.",
        "Never apply decision-001 again, it is not what I asked.",
    ],
)
async def test_direction_that_overrules_a_decision_marks_it(bridge_root, monkeypatch, text):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _engineering())
    await bridge._serve(_ask("r1"), 1)
    assert bridge.overrule(text, "Union") == ["decision-001"]


async def test_a_short_claude_app_message_can_overrule_a_decision(bridge_root, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _supervisor(monkeypatch, tmp_path)
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, _engineering())
    await bridge._serve(_ask("r1"), 1)
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    entry = {
        "type": "user",
        "uuid": "u1",
        "timestamp": stamp,
        "origin": {"kind": "human"},
        "entrypoint": "cli",
        "message": {"role": "user", "content": "Overrule decision-001."},
    }
    _session_transcript(tmp_path / "claude", tmp_path / "ws", [entry])
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(cast(Any, SimpleNamespace(sent_texts=[])), timeline, stop)
    )
    await wait_for(lambda: "overruled_at" in bridge.decisions[0])
    stop.set()
    await watcher
    assert ("↩️", "decision-001 overruled by owner direction (Claude app)") in rows
    assert _inbox._directions == [], "a short message is still not direction"


async def test_a_claude_app_reply_while_judging_closes_the_ask_through_the_watcher(
    bridge_root, tmp_path, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    _supervisor(monkeypatch, tmp_path)
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _owner())
    judge.gate = asyncio.Event()
    task = asyncio.create_task(bridge._serve(_ask("r1", created_at=time.time()), 1))
    await wait_for(lambda: judge.requests)
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    entry = {
        "type": "user",
        "uuid": "u1",
        "timestamp": stamp,
        "origin": {"kind": "human"},
        "entrypoint": "cli",
        "message": {"role": "user", "content": "yes, go ahead"},
    }
    _session_transcript(tmp_path / "claude", tmp_path / "ws", [entry])
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(cast(Any, SimpleNamespace(sent_texts=[])), timeline, stop)
    )
    await wait_for(lambda: (bridge_root / _bridge.ANSWERS / "r1.json").exists(), timeout=3)
    stop.set()
    await watcher
    judge.gate.set()
    await task
    assert opener.opened == []
    assert _answer_file(bridge_root)["in_session"] == "Claude app"


def test_a_four_digit_decision_id_can_be_overruled(bridge_root):
    bridge = _bridge.Bridge(bridge_root)
    bridge.decisions.append({"id": "decision-1000", "at": 1.0})
    assert bridge.overrule("overrule decision-1000", "Union") == ["decision-1000"]


def test_recording_a_direction_overrules_the_decisions_it_names(tmp_path, monkeypatch):
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_inbox, "_directions_path", lambda: tmp_path / "directions.json")
    bridge = _bridge.Bridge(tmp_path / "bridge")
    bridge.decisions.append({"id": "decision-002", "at": 1.0})
    rows = []
    timeline = SimpleNamespace(row=lambda icon, text: rows.append((icon, text)), bridge=bridge)
    _inbox._record_direction(
        "Do not follow decision-002; use a waiver-free fix", "Union", cast(Any, timeline)
    )
    assert bridge.decisions[0]["overruled_by"].startswith("Union: Do not follow decision-002")
    assert _inbox._directions[-1].startswith("Union: Do not follow decision-002"), "the session still gets it"


# --- triage review follow-ups -------------------------------------------------------------------


class _CustomJudgeError(Exception):
    pass


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (_CustomJudgeError("custom"), "judge could not decide: _CustomJudgeError: custom"),
        (AttributeError("no attr"), "judge could not decide: AttributeError: no attr"),
        (ImportError("lazy import"), "judge could not decide: ImportError: lazy import"),
        (None, "judge could not decide: it returned NoneType"),
    ],
    ids=["custom", "attribute", "import", "not-a-judgement"],
)
async def test_whatever_a_judge_raises_or_returns_the_question_goes_to_the_owner(
    bridge_root, monkeypatch, result, why
):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    rows = _events(bridge)
    _judge_with(bridge, result)
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: opener.opened)
    assert bridge.questions["r1"]["triage"].startswith(why)
    assert any(icon == "🙋" and text.startswith(f"for the owner ({why}") for icon, text in rows)
    assert not _pending(bridge_root).exists()
    opener.release("ask-a0-001", "ok")
    await task
    assert _answer_file(bridge_root)["value"] == "ok"


async def test_a_question_closed_while_queued_is_marked_never_opened_and_is_no_ask(bridge_root, monkeypatch):
    opener = _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    _judge_with(bridge, _owner(), _owner())
    first = asyncio.create_task(bridge._serve(_ask("r1"), 1))
    await wait_for(lambda: opener.opened)
    second = asyncio.create_task(bridge._serve(_ask("r2"), 2))
    await wait_for(lambda: "r2" in bridge.questions)
    assert await bridge.close_question("r2", "not needed") is None
    opener.release("ask-a0-001", "x")
    await first
    await second
    history = {entry["id"]: entry for entry in bridge.ask_history}
    assert history["r2"]["never_opened"] is True and "never_opened" not in history["r1"]
    timeline = _timeline._Timeline()
    timeline.bridge = bridge
    assert [ask["name"] for ask in timeline.snapshot()["metrics"]["asks"]] == ["ask-a0-001"]


@pytest.mark.parametrize("judged", [True, False], ids=["triaged", "untriaged"])
async def test_a_question_closed_while_its_condition_is_opening_is_closed_when_it_opens(
    bridge_root, monkeypatch, judged
):
    remote, opener = _Remote(monkeypatch), _Opener(monkeypatch)
    opener.hold = asyncio.Event()
    bridge = _bridge.Bridge(bridge_root)
    if judged:
        _judge_with(bridge, _owner())
    task = asyncio.create_task(bridge._serve(_ask(), 1))
    await wait_for(lambda: "r1" in bridge.questions and "r1" not in bridge._unopened)
    assert bridge.questions["r1"]["condition"] == ""
    assert await bridge.close_question("r1", "no longer needed") is None
    opener.hold.set()
    await wait_for(lambda: remote.signalled)
    opener.release("ask-a0-001", remote.signalled[0][1])
    await task
    assert "r1" not in bridge.questions
    assert _answer_file(bridge_root) == {"id": "r1", "withdrawn": "closed: no longer needed"}


async def test_an_unopened_ask_counts_from_when_it_was_made_not_when_it_was_scanned(bridge_root, monkeypatch):
    _Opener(monkeypatch)
    bridge = _bridge.Bridge(bridge_root)
    judge = _judge_with(bridge, _owner())
    judge.gate = asyncio.Event()
    made = time.time() - 10
    task = asyncio.create_task(bridge._serve(_ask(created_at=made), 1))
    await wait_for(lambda: judge.requests)
    assert bridge._unopened["r1"] == made
    await bridge.close_answered_in_session("go ahead", "Claude app", made + 5)
    assert _answer_file(bridge_root)["in_session"] == "Claude app"
    judge.gate.set()
    await task


def test_restore_keeps_decision_ids_ahead_of_the_history_when_decisions_json_is_lost(tmp_path):
    registry = tmp_path / "state" / "questions.json"
    registry.parent.mkdir()
    (registry.parent / "ask_history.json").write_text(
        json.dumps(
            [{"id": "r1", "decided": "decision-005"}, {"id": "r2", "decided": "decision-003"}, {"id": "r3"}]
        )
    )
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    bridge.restore()
    assert bridge._decision_seq == 5


def test_restore_reports_an_unreadable_decisions_file_and_keeps_it_aside(tmp_path):
    registry = tmp_path / "state" / "questions.json"
    registry.parent.mkdir()
    (registry.parent / "decisions.json").write_text('{"seq": 3, "decisions": [{"id": "dec')
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    rows = _events(bridge)
    bridge.restore()
    assert any(
        icon == "⚠️"
        and text.startswith("decisions.json unreadable: ")
        and text.endswith("; starting the list empty, ids continue from the ask history")
        for icon, text in rows
    )
    assert (registry.parent / "decisions.json.bad").read_text().startswith('{"seq": 3')
    assert bridge.decisions == []


def test_restore_keeps_decision_ids_ahead_of_decisions_whose_seq_is_stale(tmp_path):
    registry = tmp_path / "state" / "questions.json"
    registry.parent.mkdir()
    (registry.parent / "decisions.json").write_text(
        json.dumps({"seq": 1, "decisions": [{"id": "decision-004"}, {"id": "decision-002"}]})
    )
    bridge = _bridge.Bridge(tmp_path / "bridge", registry=registry)
    bridge.restore()
    assert bridge._decision_seq == 4


@pytest.mark.parametrize(("approve", "code", "word"), [(True, 0, "approved"), (False, 1, "declined")])
def test_the_cli_names_the_decision_of_a_yes_no_ask_on_stderr(bridge_root, capsys, approve, code, word):
    path = bridge_root / _bridge.ANSWERS / "r1.json"
    decided = {"id": "decision-012", "decision": "Yes.", "reasoning": "Because.", "approve": approve}
    path.write_text(json.dumps({"id": "r1", "decided": decided}))
    assert _bridge._read_answer(path, approve=True) == code
    out, err = capsys.readouterr()
    assert out.splitlines()[0] == word and "decision-012" not in out.splitlines()[0]
    assert "decision-012" in err


def test_redact_tokens_removes_registered_source_values_and_aws_shapes() -> None:
    secret = "a-live-value-" + "z" * 20
    source = lambda: [secret, "short"]  # noqa: E731 -- values under 8 characters are ignored
    _bridge.register_redaction_source(source)
    try:
        assert secret not in _bridge.redact_tokens(f"use {secret} and short")
        assert "short" in _bridge.redact_tokens("short")
    finally:
        _bridge.unregister_redaction_source(source)
    assert secret in _bridge.redact_tokens(secret)
    assert _bridge.redact_tokens("ASIAABCDEFGH12345678") == "<redacted>"
    assert _bridge.redact_tokens("aws_session_token = abc123") == "aws_session_token = <redacted>"
