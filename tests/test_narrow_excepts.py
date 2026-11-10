"""Best-effort handlers catch what their call raises, and the suppressions carry a reason (P0 PR-6a)."""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import re
import subprocess
import tokenize
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte.errors
import flyte.remote
import flyte.report
import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from obstore.exceptions import GenericError

from flyte_agent_factory import (
    _bridge,
    _checkpoint,
    _contract,
    _demo_checks,
    _errors,
    _factory,
    _factory_base,
    _factory_meters,
    _inbox,
    _session_config,
    _state,
    _supervise_support,
    _timeline,
    agent,
)
from flyte_agent_factory._demo_checks import _check_demo_package
from tests.test_demo import _package
from tests.test_personal_browser import runtime as personal

ROOT = Path(__file__).resolve().parent.parent


def _project_name(run: Any) -> str:
    """`_factory._project_name` for the fakes below, which are not `flyte.remote.Run`s."""
    return _factory._project_name(run)


# --- the shared tuple ------------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "error",
    [
        ConnectError(Code.UNAVAILABLE, "down"),
        flyte.errors.InitializationError("x", "user", "not initialized"),
        flyte.errors.RuntimeUserError("x", "bad"),
        OSError("socket"),
        TimeoutError(),
        KeyError("missing"),
        json.JSONDecodeError("bad", "", 0),
        # What flyte.storage.put, Dir.from_local and fsspec raise against an unreachable bucket.
        GenericError("Generic S3 error: Error performing PUT"),
    ],
)
def test_remote_errors_cover_what_flyte_calls_raise(error):
    assert isinstance(error, _errors.REMOTE_ERRORS)


def test_remote_errors_do_not_hide_a_bug():
    assert not isinstance(ZeroDivisionError(), _errors.REMOTE_ERRORS)
    assert not isinstance(AttributeError(), _errors.REMOTE_ERRORS)


# --- narrowed sites keep handling their expected failures ------------------------------------- #


def test_a_run_whose_inputs_cannot_be_read_has_no_label():
    class Run:
        def __init__(self, error):
            self.error = error

        def inputs(self):
            raise self.error

    assert _project_name(Run(OSError("gone"))) == ""
    assert _project_name(Run(KeyError("name"))) == ""
    assert _project_name(Run(ConnectError(Code.UNAVAILABLE, "down"))) == ""
    with pytest.raises(ZeroDivisionError):  # not an expected failure: it must be seen
        _project_name(Run(ZeroDivisionError()))


def test_a_run_with_no_inputs_has_no_label():
    run = SimpleNamespace(inputs=lambda: None)  # `Action.inputs()` when the response carried none
    assert _project_name(run) == ""


def test_slot_headroom_falls_back_when_the_listing_fails(monkeypatch):
    def listall(**_):
        raise OSError("listing failed")

    monkeypatch.setattr(flyte.remote.Run, "listall", listall)
    assert _factory_meters.slot_headroom(["a", "b"]) == {}


async def test_a_lost_late_answer_does_not_break_the_run():
    bridge = _bridge.Bridge.__new__(_bridge.Bridge)
    bridge.questions = {"q1": {}, "q2": {}}

    def failing(error):
        async def aio():
            raise error

        return SimpleNamespace(wait=SimpleNamespace(aio=aio))

    # `_late_answer` replaced `_deliver_late` (close-stale-asks): it takes the wait still in flight.
    await bridge._late_answer(failing(flyte.errors.RuntimeUserError("x", "gone")).wait.aio(), "?", "q1")
    await bridge._late_answer(failing(ConnectError(Code.UNAVAILABLE, "down")).wait.aio(), "?", "q2")
    assert bridge.questions == {}


async def test_a_late_answer_of_the_wrong_type_is_dropped_not_raised():
    bridge = _bridge.Bridge.__new__(_bridge.Bridge)
    bridge.questions = {"q1": {}}

    async def aio():
        raise TypeError("expected a bool payload, got str")

    await bridge._late_answer(SimpleNamespace(wait=SimpleNamespace(aio=aio)).wait.aio(), "?", "q1")
    assert bridge.questions == {}


@pytest.mark.real_planner
async def test_a_planner_that_cannot_snapshot_to_object_storage_leaves_the_findings_alone(
    tmp_path, monkeypatch
):
    async def upload(_workspace):
        raise GenericError("Generic S3 error: Error performing PUT")

    monkeypatch.setattr(_checkpoint, "_upload_workspace", upload)
    timeline = _timeline._Timeline()
    assert await _contract._plan_next_round(tmp_path, "b", "", "- f", 1, timeline) == ("", None, "")
    assert "plan could not snapshot: GenericError" in timeline.render()


async def test_the_checkpoint_loop_survives_a_failed_commit(monkeypatch):
    saves = []

    async def save(_state):
        saves.append(True)
        if len(saves) == 1:
            raise ConnectError(Code.UNAVAILABLE, "down")
        stop.set()
        return True

    monkeypatch.setattr(_state, "save_checkpoint", save)
    timeline = _timeline._Timeline()
    stop = asyncio.Event()
    await asyncio.wait_for(
        _checkpoint._checkpoint_loop(state=cast(Any, None), every=0.01, timeline=timeline, stop=stop),
        timeout=5,
    )
    page = timeline.render()
    assert "commit failed" in page and "checkpointed" in page


async def test_the_periodic_flush_reports_instead_of_swallowing(monkeypatch, capsys):
    timeline = _timeline._Timeline()
    stop = asyncio.Event()

    async def flush(**_):
        stop.set()
        raise ValueError("report broke")

    monkeypatch.setattr(timeline, "flush", flush)
    await asyncio.wait_for(_supervise_support._flush_loop(timeline, stop, every=0.01), timeout=30)
    assert "periodic flush failed: ValueError: report broke" in capsys.readouterr().err


def test_a_dashboard_bug_falls_back_to_the_rows_but_a_name_error_is_seen(monkeypatch, capsys):
    timeline = _timeline._Timeline()
    timeline.row("🔀", "kept")

    def broken(_now):
        raise ZeroDivisionError("rate")

    monkeypatch.setattr(timeline, "_render_dashboard", broken)
    assert "kept" in timeline.render()
    assert "dashboard failed: ZeroDivisionError" in capsys.readouterr().err

    def bug(_now):
        raise NameError("typo")

    monkeypatch.setattr(timeline, "_render_dashboard", bug)
    with pytest.raises(NameError):
        timeline.render()


@pytest.mark.parametrize("error", [ConnectError(Code.UNAVAILABLE, "down"), OSError("gone"), TimeoutError()])
async def test_a_failed_report_flush_stays_dirty_and_is_not_fatal(monkeypatch, error):
    async def replace(*_a, **_k):
        raise error

    monkeypatch.setattr(flyte.report, "replace", SimpleNamespace(aio=replace))
    timeline = _timeline._Timeline()
    await timeline.flush(force=True)
    assert timeline._dirty


async def test_a_report_flush_bug_is_not_swallowed(monkeypatch):
    async def replace(*_a, **_k):
        raise ZeroDivisionError

    monkeypatch.setattr(flyte.report, "replace", SimpleNamespace(aio=replace))
    with pytest.raises(ZeroDivisionError):
        await _timeline._Timeline().flush(force=True)


async def test_the_daemon_thread_hands_a_base_exception_to_the_awaiter():
    class Stop(BaseException):
        pass

    def fn():
        raise Stop

    with pytest.raises(Stop):
        await _inbox._in_daemon_thread(fn)


async def test_a_failed_queue_read_shows_the_queue_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(_session_config, "_PROFILE", SimpleNamespace(state=SimpleNamespace(uri="s3://x")))
    monkeypatch.setattr(_supervise_support, "_queue_read", None)
    monkeypatch.setattr(_factory_base, "queue_root", lambda: tmp_path)

    def broken(*_a):
        raise GenericError("bucket unreachable")

    monkeypatch.setattr(_factory, "waiting_rows", broken)
    timeline = _timeline._Timeline()
    timeline.queue = [{"position": 1}]
    await _supervise_support._refresh_queue(timeline)
    assert timeline.queue == []


async def test_the_board_loop_reports_a_crash_with_a_traceback(monkeypatch, capsys):
    stop = asyncio.Event()

    class Worker:
        def tick(self):
            stop.set()
            raise ZeroDivisionError("board bug")

    monkeypatch.setattr(_supervise_support, "_board_worker", Worker())
    timeline = _timeline._Timeline()
    await asyncio.wait_for(_supervise_support._board_loop(timeline, stop), timeout=30)
    assert "board tick crashed: ZeroDivisionError" in timeline.render()
    assert "Traceback" in capsys.readouterr().err


def test_the_exit_gate_closes_and_names_only_the_class_when_status_fails(capsys):
    import importlib.util

    assets = ROOT / "src" / "flyte_agent_factory" / "_assets" / "browser"
    spec = importlib.util.spec_from_file_location("personal_runtime_narrow", assets / "personal.py")
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)

    def status():
        raise ValueError("secret-token-in-message")

    assert not runtime.ExitGate("home", status).ready()
    out = capsys.readouterr().out
    assert "ValueError" in out and "secret-token-in-message" not in out


# --- the demo manifest (TRY004) --------------------------------------------------------------- #


def test_a_section_that_is_not_an_object_is_a_type_error(tmp_path):
    package = _package(
        tmp_path, manifest={"audience": "a platform lead", "subject": "a thing", "sections": ["nope"]}
    )
    with pytest.raises(TypeError, match="section 1 is not an object"):
        _check_demo_package(package)
    assert (package / _session_config.DEMO_ASSETS_SUBDIR / _session_config.DEMO_MANIFEST).is_file()


async def test_a_malformed_section_is_still_reported_not_raised(tmp_path):
    _package(tmp_path, manifest={"audience": "a platform lead", "subject": "a thing", "sections": ["nope"]})
    problem = await _demo_checks._demo_package_problem(tmp_path, "polynomial")
    assert f"`{_session_config.DEMO_SUBDIR}/polynomial`" in problem and "is not an object" in problem


def test_a_manifest_that_is_not_an_object_is_a_type_error(tmp_path):
    package = _package(tmp_path, manifest=["audience", "subject", "sections"])
    with pytest.raises(TypeError, match="is a JSON list, not an object"):
        _check_demo_package(package)


async def test_a_demo_report_that_storage_refuses_is_reported_not_raised(tmp_path, monkeypatch):
    (tmp_path / _session_config.DEMO_SUBDIR / "gw").mkdir(parents=True)
    video = tmp_path / _session_config.DEMO_SUBDIR / "gw" / "gw.mp4"
    video.write_bytes(b"x")

    async def head(_workspace):
        return "abc1234"

    async def flush():
        raise GenericError("Generic S3 error: Error performing PUT")

    monkeypatch.setattr(_demo_checks, "_check_demo_package", lambda package: (video, [video]))
    monkeypatch.setattr(_checkpoint, "_git_head", head)
    monkeypatch.setattr(flyte.report, "get_tab", lambda name: SimpleNamespace(replace=lambda html: None))
    monkeypatch.setattr(flyte.report.flush, "aio", flush, raising=False)
    timeline = _timeline._Timeline()
    await _demo_checks._publish_demo_reports(tmp_path, "s", timeline, "gw")
    assert "could not report gw: GenericError" in timeline.render()


async def test_a_bug_rendering_the_demo_report_is_loud(tmp_path, monkeypatch):
    (tmp_path / _session_config.DEMO_SUBDIR / "gw").mkdir(parents=True)
    video = tmp_path / _session_config.DEMO_SUBDIR / "gw" / "gw.mp4"
    video.write_bytes(b"x")

    async def head(_workspace):
        return "abc1234"

    def broken(**_):
        raise ZeroDivisionError

    monkeypatch.setattr(_demo_checks, "_check_demo_package", lambda package: (video, [video]))
    monkeypatch.setattr(_checkpoint, "_git_head", head)
    monkeypatch.setattr(_demo_checks, "_demo_report", broken)
    monkeypatch.setattr(flyte.report, "get_tab", lambda name: SimpleNamespace(replace=lambda html: None))
    with pytest.raises(ZeroDivisionError):
        await _demo_checks._publish_demo_reports(tmp_path, "s", _timeline._Timeline(), "gw")


async def test_a_manifest_that_is_a_list_is_reported_not_raised(tmp_path):
    _package(tmp_path, manifest=["audience", "subject", "sections"])
    problem = await _demo_checks._demo_package_problem(tmp_path, "polynomial")
    assert (
        f"`{_session_config.DEMO_SUBDIR}/polynomial`" in problem
        and "is a JSON list, not an object" in problem
    )


async def test_a_manifest_that_is_not_utf8_is_reported_not_raised(tmp_path):
    package = _package(tmp_path)
    (package / _session_config.DEMO_ASSETS_SUBDIR / _session_config.DEMO_MANIFEST).write_bytes(
        b'{"audience": "\xff\xfe"}'
    )
    problem = await _demo_checks._demo_package_problem(tmp_path, "polynomial")
    assert f"`{_session_config.DEMO_SUBDIR}/polynomial`" in problem and "is not UTF-8" in problem


# --- the personal browser lifecycle ----------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, "tailscale"),
        subprocess.TimeoutExpired("tailscale", 10),
        FileNotFoundError("tailscale"),
        json.JSONDecodeError("bad", "", 0),
        KeyError("BackendState"),
        AttributeError("'list' object has no attribute 'values'"),
    ],
)
def test_the_exit_gate_fails_closed_on_what_tailscale_status_raises(error):
    def status():
        raise error

    assert not personal.ExitGate("home", status).ready()


def test_a_bug_in_the_exit_gate_status_is_loud():
    def status():
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        personal.ExitGate("home", status).ready()


@pytest.mark.parametrize("error", [GenericError("Generic S3 error"), ConnectionResetError(), ValueError()])
def test_a_failed_snapshot_before_a_browser_reset_does_not_stop_the_reset(tmp_path, monkeypatch, error):
    monkeypatch.setattr(personal, "MCP_RESTART_FILE", tmp_path / "restart")
    monkeypatch.setattr(personal, "stop_browser", lambda: None)

    def save():
        raise error

    # With the marker still present the wait for the browser times out: save was survived.
    with pytest.raises(RuntimeError, match="did not come back"):
        personal.restart_browser(tmp_path, save, timeout=0)


def test_a_bug_in_the_snapshot_before_a_browser_reset_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(personal, "MCP_RESTART_FILE", tmp_path / "restart")
    monkeypatch.setattr(personal, "stop_browser", lambda: None)

    def save():
        raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        personal.restart_browser(tmp_path, save, timeout=0)


def test_storage_retries_any_client_error_then_reports_unavailable(monkeypatch):
    monkeypatch.setattr(personal.time, "sleep", lambda s: None)
    calls = []

    def call():
        calls.append(True)
        raise ZeroDivisionError  # stands in for a client's own exception type

    with pytest.raises(personal.StorageUnavailable, match="ZeroDivisionError"):
        personal.storage(call, personal.time.monotonic() + 3)
    assert len(calls) > 1


# --- mutable defaults (B006) ------------------------------------------------------------------ #


def test_gcp_bootstrap_inputs_are_not_mutable_defaults():
    params = inspect.signature(agent.gcp_bootstrap.func).parameters
    for name in ("roles", "billing_roles", "apis"):
        assert params[name].default is None
        assert params[name].annotation in ("list[str] | None", list[str] | None)


def test_flyte_supports_the_gcp_bootstrap_input_type():
    """A tuple input would be pickled by Flyte; the list-or-None form is a real Flyte type."""
    from flyte.types import TypeEngine

    literal_type = TypeEngine.to_literal_type(cast(Any, list[str] | None))
    assert literal_type.HasField("union_type")


# --- the broad handlers that remain are pinned ----------------------------------------------- #

#: `except Exception` sites left in src/ and ops/, each a boundary with `noqa: BLE001 -- <reason>`:
#: loop and task boundaries, report/redaction boundaries that print only the class, the probes that
#: record a failure as a finding, and three the coordinator kept broad on purpose -- the personal
#: browser's ExitGate (security boundary, fails closed), its restart_browser snapshot (arbitrary
#: fsspec backend, no common exception type) and model_eval's budget_guard (re-raises as BudgetStop,
#: fails closed), and the board loop's tick (a board bug is reported once and the next tick runs,
#: rather than ending board updates for the run), and the pump's child-listing harvest (a status
#: heuristic: whatever the listing raises counts as a failed listing, not the end of the supervisor
#: loop). A change to this number needs a justification in
#: review: a new site must be a boundary, and a removed one should be narrowed, not hidden.
#: +2 (board #17, deployer on labelled runs): the deployer tick and the new deploy_pair task are each a
#: task boundary that redacts every error before re-raising it, so no exception text carries a token.
#: +1 (board #27, author handoff): once the author is stopped for a handoff, whatever preparation raises
#: must still end in the restart; a leaked error would leave a stopped author with `_switching` set.
#: +1 (board #49 follow-up, dispatch wake): the daemon-thread runner hands any exception to the awaiting
#: coroutine, so a stalled or failing planner wake ends on a bounded timeout instead of holding the deploy.
BROAD_HANDLER_SITES = 11


def test_the_broad_handlers_are_pinned_to_the_boundary_sites():
    import importlib.util
    import subprocess
    import sys

    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff is not installed in this environment (it is in the dev dependency group)")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "BLE001", "--ignore-noqa", "--no-cache"]
        + ["--output-format", "json", "--exit-zero", "src", "ops"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    hits = [
        f"{Path(h['filename']).relative_to(ROOT)}:{h['location']['row']}" for h in json.loads(result.stdout)
    ]
    assert len(hits) == BROAD_HANDLER_SITES, "\n".join(hits)


# --- suppressions carry a reason -------------------------------------------------------------- #

NOQA = re.compile(r"noqa(?::\s*(?P<codes>[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*))?(?P<rest>.*)")
REASON = re.compile(r"^\s+--\s+\S.{9,}")


def _python_files():
    for top in ("src", "ops"):
        yield from (ROOT / top).rglob("*.py")


def test_every_noqa_names_its_codes_and_gives_a_reason():
    bad = []
    for path in _python_files():
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type != tokenize.COMMENT or "noqa" not in token.string:
                    continue
                match = NOQA.search(token.string)
                if not (match and match["codes"] and REASON.match(match["rest"])):
                    bad.append(f"{path.relative_to(ROOT)}:{token.start[0]}: {token.string.strip()}")
    assert not bad, "\n".join(bad)


def test_nothing_suppresses_every_exception():
    bad = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in _python_files()
        for n, line in enumerate(io.StringIO(path.read_text()), 1)
        if re.search(r"suppress\(\s*(Base)?Exception\b", line)
    ]
    assert not bad, "\n".join(bad)


# --- exception-handling paydown: narrowed sites keep handling their expected failures (ops and engine) ---- #


async def test_a_failed_budget_check_stops_the_launch_and_a_bug_is_seen():
    from ops.model_eval.experiment import BudgetStop, budget_guard

    def backend(error):
        async def meters(_engine):
            raise error

        return SimpleNamespace(live=lambda: [], meters=meters)

    with pytest.raises(BudgetStop, match="OSError"):
        await budget_guard(backend(OSError("meter down")), "codex", {})
    with pytest.raises(ZeroDivisionError):
        await budget_guard(backend(ZeroDivisionError()), "codex", {})


def test_one_login_failing_is_a_line_and_a_bug_is_seen():
    from ops import meter_logins

    def failing(error):
        def submit(_slot, *, interruptible):
            raise error

        return submit

    assert "unavailable (OSError" in meter_logins._line("acct", failing(OSError("gone")))
    with pytest.raises(ZeroDivisionError):
        meter_logins._line("acct", failing(ZeroDivisionError()))


def test_a_github_failure_becomes_a_row_naming_only_the_class():
    from flyte_agent_factory._pull_requests import PullRequests

    rows: list[tuple[str, str]] = []
    prs = PullRequests(
        cast(Any, SimpleNamespace()), "o/r", "p", "", lambda icon, text: rows.append((icon, text))
    )

    def failing(error):
        def call():
            raise error

        return call

    assert prs._guard("lookup", failing(KeyError("number"))) is None
    assert rows == [("⚠️", "lookup failed for o/r: KeyError")]
    with pytest.raises(ZeroDivisionError):
        prs._guard("lookup", failing(ZeroDivisionError()))


def test_an_unreadable_issue_link_is_logged_and_a_bug_is_seen(monkeypatch, caplog):
    from flyte_agent_factory import _issues, _queue

    profile = SimpleNamespace(state=SimpleNamespace(uri="memory://state"))
    monkeypatch.setattr(_queue, "root", lambda uri: uri)

    def entries(*_):
        raise GenericError("Generic S3 error: listing")

    monkeypatch.setattr(_queue, "entries", entries)
    assert _issues.linked_issue(profile, "p") == ""  # type: ignore[arg-type]
    assert "Could not read project's issue link" in caplog.text
    monkeypatch.setattr(_queue, "entries", lambda *_: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        _issues.linked_issue(profile, "p")  # type: ignore[arg-type]


async def test_a_wake_that_fails_reaches_the_awaiting_coroutine():
    from flyte_agent_factory import _deployer

    def failing():
        raise OSError("offline")

    with pytest.raises(OSError, match="offline"):
        await _deployer._on_daemon_thread(failing)


async def test_the_session_feed_reports_a_failed_poll_with_its_traceback(capsys):
    from flyte_agent_factory import _session_log

    stop = asyncio.Event()
    lines: list[str] = []

    class Tail:
        def poll(self):
            stop.set()
            raise ValueError("torn line")

    await _session_log.feed_loop(Tail(), lines.append, stop, every=0.01)  # type: ignore[arg-type]
    assert lines == ["[session] transcript poll failed: torn line"]
    assert "ValueError: torn line" in capsys.readouterr().err
