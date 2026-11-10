"""Permission-menu detection (`permission_prompt`, `ClaudeProcess.menu`) and how the pump uses it."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _session_config, _supervise, _supervise_support, _timeline
from flyte_agent_factory._process import ClaudeProcess, permission_prompt
from tests._wait import wait_for
from tests.test_nudge import _PERMISSION, _Bridge, _fake_clock, _Proc, _pump, _Queue

QUESTION = "Dangerous rm operation on statically-unresolvable target"
# Claude Code's other shape: "Yes, and don't ask again" in the middle, "No" last.
_THREE_OPTIONS = """ Bash command
   npm install left-pad
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for npm install commands in /work
   3. No, and tell Claude what to do differently (esc)
 Esc to cancel
"""
_IDLE_BOX = "────────────────\n❯ \n────────────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"


def _process(tmp_path) -> ClaudeProcess:
    return ClaudeProcess(["true"], cwd=tmp_path, env={})


def _menu_of(proc: ClaudeProcess) -> str | None:
    """`proc.menu` through a call, so mypy does not keep it narrowed by an earlier assert."""
    return proc.menu


def _summary(text: str) -> str:
    summary = permission_prompt(text)
    assert summary is not None
    return summary


# -- the matcher ------------------------------------------------------------ #


@pytest.mark.parametrize("text", [_PERMISSION, _PERMISSION.replace(" ", ""), _THREE_OPTIONS])
def test_menu_shapes_are_recognised(text):
    assert permission_prompt(text) is not None


def test_summary_names_what_was_asked():
    assert QUESTION in _summary(_PERMISSION)
    assert "npm install left-pad" in _summary(_THREE_OPTIONS)


def test_summary_drops_lines_made_only_of_box_drawing():
    text = "╭──────╮\n╌╌╌╌╌╌╌╌\n │ rm -rf build │\n ╌╌╌╌╌╌╌╌╌╌╌ │\n │\n" + _PERMISSION.split("\n", 3)[3]
    summary = _summary(text)
    assert "rm -rf" in summary or QUESTION in summary
    assert not any(char in summary for char in "╌─│╭╮╰╯")


def test_one_sentence_mentioning_the_menu_is_not_a_menu():
    line = "The TUI showed 'Do you want to proceed?' with options 1. Yes and 2. No, plus Esc to cancel."
    assert permission_prompt(line) is None
    assert permission_prompt("● " + line + "\n") is None


def test_a_menu_followed_by_other_output_is_not_the_menu_on_screen():
    text = _PERMISSION + "".join(f"ordinary output line {i}\n" for i in range(80)) + "> \n"
    assert len(text) < 4000
    assert permission_prompt(text) is None


def test_a_menu_that_is_not_in_the_last_lines_is_not_matched():
    padding = "".join(f"line {i}\n" for i in range(20))
    text = " Do you want to proceed?\n 1. Yes\n" + padding + " 2. No\n Esc to cancel\n"
    assert permission_prompt(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # parts out of order
        " Esc to cancel\n 2. No\n 1. Yes\n Do you want to proceed?\n",
        " 1. Yes\n Do you want to proceed?\n 2. No\n Esc to cancel\n",
        # a part missing
        " Do you want to proceed?\n 1. Yes\n Esc to cancel\n",
        " Do you want to proceed?\n 2. No\n Esc to cancel\n",
        " Do you want to proceed?\n 1. Yes\n 2. No\n",
        # the three-option form without its No
        " Do you want to proceed?\n 1. Yes\n 2. Yes, and don't ask again\n Esc to cancel\n",
        # parts mid-line rather than at line starts
        "Do you want to proceed? 1. Yes 2. No Esc to cancel\n",
        "I said: Do you want to proceed?\n and then 1. Yes\n and 2. No\n and Esc to cancel\n",
        # a different dialog with the same footer
        " ❯ No, exit\n Enter to confirm · Esc to cancel\n",
    ],
)
def test_incomplete_or_misordered_menus_do_not_match(text):
    assert permission_prompt(text) is None


def test_a_cat_of_the_brief_followed_by_an_idle_box_is_not_a_menu(tmp_path):
    brief = (
        "Live terminal tail:\n```\n" + _PERMISSION + "```\nNote the TUI output is mangled (spaces dropped).\n"
    )
    proc = _process(tmp_path)
    proc._consume("$ cat BRIEF.md\n" + brief + "● Done reading the brief.\n> \n")
    assert _menu_of(proc) is None
    proc._consume("$ cat tests/test_nudge.py\n" + _PERMISSION + "Note\n" + _IDLE_BOX)
    assert _menu_of(proc) is None


# -- the sticky state on the process ----------------------------------------- #


def test_menu_is_known_as_soon_as_it_is_drawn_even_in_pieces(tmp_path):
    proc = _process(tmp_path)
    for start in range(0, len(_PERMISSION), 7):
        proc._consume(_PERMISSION[start : start + 7])
    assert QUESTION in (_menu_of(proc) or "")


def test_menu_stays_known_after_other_output_scrolls_it_away(tmp_path):
    proc = _process(tmp_path)
    proc._consume(_PERMISSION)
    for i in range(300):
        proc._consume(f"background agent output line {i}\n")
    assert permission_prompt(proc._menu_scan) is None
    assert QUESTION in (_menu_of(proc) or "")


def test_menu_is_cleared_when_the_input_box_is_drawn_again(tmp_path):
    proc = _process(tmp_path)
    proc._consume(_PERMISSION)
    proc._consume("later output\n")
    assert _menu_of(proc)
    proc._consume(_IDLE_BOX)
    assert _menu_of(proc) is None
    proc._consume("more output\n")
    assert _menu_of(proc) is None, "the dismissed menu's text must not be found again"


@pytest.mark.parametrize("footer", ["  ⏵⏵ bypass permissions on (shift+tab to cycle)", "  ? for shortcuts"])
def test_menu_is_cleared_by_either_input_box_footer_even_with_dropped_spaces(tmp_path, footer):
    proc = _process(tmp_path)
    proc._consume(_PERMISSION)
    proc._consume(footer.replace(" ", "") + "\n")
    assert _menu_of(proc) is None


def test_a_menu_drawn_again_after_the_input_box_is_known_again(tmp_path):
    proc = _process(tmp_path)
    proc._consume(_PERMISSION + _IDLE_BOX)
    assert _menu_of(proc) is None
    proc._consume(_PERMISSION)
    assert QUESTION in (_menu_of(proc) or "")


def test_a_submitted_line_echoed_into_an_open_menu_does_not_dismiss_it(tmp_path):
    # tg9: three nudges typed into the open menu echo as "❯ Keep going", the menu stays.
    proc = _process(tmp_path)
    proc._consume(_PERMISSION)
    proc._consume("❯ Keep going until DONE\n")
    assert _menu_of(proc)


FIXTURE = Path(__file__).parent / "fixtures" / "tg9_permission_menu.log"


def test_replay_of_the_real_session_knows_the_menu_when_the_session_went_idle(tmp_path):
    """tg9: the menu scrolled out of the 4000-char buffer while a background agent kept writing."""
    proc = _process(tmp_path)
    states = []
    for line in FIXTURE.read_text().splitlines():
        if line.lstrip().startswith(("[timeline]", "[pump]", "[flyte]")):
            # The supervisor's own output, not the pty's. The 22:41:49 row is the last note
            # before the session went idle (the status line read "idle for 3m" after it).
            if "22:41:49" in line:
                break
            continue
        proc._consume(line + "\r\n")
        states.append(proc.menu)
    else:
        pytest.fail("fixture lost its 22:41:49 marker")
    assert states.index(next(s for s in states if s)) > 100, "no false positive before the menu"
    assert QUESTION in (_menu_of(proc) or "")
    # And it really had scrolled out of the buffer the old detection looked at.
    assert permission_prompt(proc._scan) is None


# -- the pump ---------------------------------------------------------------- #


class _Notices:
    def __init__(self, items):
        self.pending = list(items)
        self.requeued = False

    def drain_notices(self):
        result, self.pending = self.pending, []
        return result

    def requeue_notices(self):
        self.requeued = True


async def _start(monkeypatch, *, bridge=None, delegations=None, menu=QUESTION):
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc: Any = _Proc()  # given a `keys` list and a `type` below
    proc.menu = menu
    proc.last_output = now["t"] - 21
    proc.keys = []
    proc.type = proc.keys.append  # Escape does not close the menu: the worst case
    stop, timeline = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(bridge or _Bridge(), proc, timeline, stop, delegations=delegations))
    return now, proc, timeline, stop, pump


def _rows(timeline):
    return [row for _, _, row in timeline._rows]


async def test_a_menu_that_stays_open_is_declined_again_after_a_back_off(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch)
    try:
        await wait_for(lambda: proc.keys == ["\x1b"])
        now["t"] += _session_config.PROMPT_REDECLINE_S - 10
        await asyncio.sleep(0.05)
        assert proc.keys == ["\x1b"], "too soon to decline the same prompt again"
        now["t"] += 11
        proc.last_output = now["t"] - 21
        await wait_for(lambda: len(proc.keys) == 2)
        assert sum("declined a permission prompt" in row for row in _rows(timeline)) == 2
        now["t"] += 2
        await asyncio.sleep(0.05)
        # Typing into a menu that may still be open could approve it: Enter picks "1. Yes".
        assert proc.submitted == [], "a follow-up was typed into an open menu"
        proc.menu = None
        now["t"] += 2
        await wait_for(lambda: sum("was declined automatically" in text for text in proc.submitted) == 1)
    finally:
        stop.set()
        await pump


async def test_a_menu_that_keeps_coming_back_is_escalated_after_the_cap(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch)
    try:
        for declined in range(1, _session_config.PROMPT_DECLINE_CAP + 1):
            await wait_for(lambda: len(proc.keys) == declined)  # noqa: B023 -- awaited before the loop variable moves on
            now["t"] += _session_config.PROMPT_REDECLINE_S + 1
            proc.last_output = now["t"] - 21
        await wait_for(lambda: any("keeps coming back" in row for row in _rows(timeline)))
        await wait_for(lambda: "keeps coming back" in (timeline._activity or ""))
        # Escalating does not stop the declines: a menu left open is a session blocked for good
        # (one sat on `rm -rf s3v_cwd/*` for nine hours). The time is already past the back-off.
        await wait_for(lambda: len(proc.keys) == _session_config.PROMPT_DECLINE_CAP + 1)
        await asyncio.sleep(0.05)
        assert sum("keeps coming back" in row for row in _rows(timeline)) == 1, "one row per streak"
        assert "a human needs to look" in timeline._activity
    finally:
        stop.set()
        await pump


async def test_the_escalation_status_lasts_until_the_session_speaks(monkeypatch):
    bridge = _Queue()
    now, proc, timeline, stop, pump = await _start(monkeypatch, bridge=bridge)
    try:
        for declined in range(1, _session_config.PROMPT_DECLINE_CAP + 1):
            await wait_for(lambda: len(proc.keys) == declined)  # noqa: B023 -- awaited before the loop variable moves on
            now["t"] += _session_config.PROMPT_REDECLINE_S + 1
            proc.last_output = now["t"] - 21
        await wait_for(lambda: "keeps coming back" in (timeline._activity or ""))
        now["t"] += _session_config.IDLE_AFTER_S + 1
        await asyncio.sleep(0.05)
        assert "keeps coming back" in timeline._activity
        proc.menu = None  # the session got past it and says so
        bridge.push("working on it")
        await wait_for(lambda: "keeps coming back" not in (timeline._activity or ""))
    finally:
        stop.set()
        await pump


async def test_a_declined_menu_that_stays_open_is_escalated_and_never_typed_into(monkeypatch):
    """Reviewer probe: the same menu returning used to reset the idle clock on every pass.

    The nudges it held off are not typed into the menu either -- Enter there could approve the
    command -- so a menu Esc does not close is escalated, and the nudge waits for it to close.
    """
    now, proc, timeline, stop, pump = await _start(monkeypatch)
    try:
        await wait_for(lambda: bool(proc.keys))
        for _ in range(20):
            now["t"] += _session_config.NUDGE_AFTER_S / 4
            proc.last_output = now["t"] - 25  # the menu's quiet terminal
            await asyncio.sleep(0.02)
        assert proc.submitted == [], "typed into an open permission menu"
        assert any("keeps coming back" in row for row in _rows(timeline)), "a human is told"
        proc.menu = None
        now["t"] += 2
        await wait_for(
            lambda: _supervise_support._nudge_text(1, "") in proc.submitted, what="the idle clock was held"
        )
    finally:
        stop.set()
        await pump


async def test_notices_wait_for_a_declined_menu_to_close_and_are_then_delivered(monkeypatch):
    notice = "delegate-x (codex, ok: 3 files) — `delegate wait delegate-x`"
    notices = _Notices([])
    now, proc, _timeline, stop, pump = await _start(monkeypatch, delegations=notices)
    try:
        await wait_for(lambda: bool(proc.keys))
        notices.pending = [notice]
        for _ in range(5):
            now["t"] += _session_config.IDLE_AFTER_S + 1
            proc.last_output = now["t"] - _session_config.IDLE_AFTER_S - 1
            await asyncio.sleep(0.02)
        assert proc.submitted == [], "typed into an open permission menu"
        assert notices.pending == [notice], "a held notice must not be lost"
        proc.menu = None
        # Notices go out to an idle session. Under load the pump may decline the menu once more just before it
        # closes, which counts as activity; let fake time pass in small steps (at most one idle period).
        delivered = False
        for _ in range(int(_session_config.IDLE_AFTER_S // 10) + 2):
            now["t"] += 10
            proc.last_output = now["t"] - _session_config.IDLE_AFTER_S - 1
            # Short on purpose: a miss means the fake clock has to move on, not that the pump is stuck.
            try:
                await wait_for(lambda: any(notice in text for text in proc.submitted), timeout=1.0)
            except AssertionError:
                continue
            delivered = True
            break
        assert delivered
    finally:
        stop.set()
        await pump


async def test_a_session_message_is_held_while_a_menu_is_open_and_typed_once_it_closes(tmp_path, monkeypatch):
    proc = _process(tmp_path)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    proc._consume(_PERMISSION)
    assert _menu_of(proc)
    await proc.submit("Review round 2 rejected your work.", label="Review")
    await proc.submit("Second message.")
    assert typed == [], "typed into an open permission menu"
    proc._consume(_IDLE_BOX)
    assert _menu_of(proc) is None
    await proc.deliver_held()
    assert typed == ["Review round 2 rejected your work.", "\r", "Second message.", "\r"]
    await proc.deliver_held()
    assert len(typed) == 4, "delivered once"


async def test_an_open_menu_is_not_idleness_before_it_is_declined(monkeypatch):
    # Branch behaviour kept: while we have not yet acted, menu time spends no nudge allowance.
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc = _Proc()
    proc.menu = QUESTION
    proc.last_output = now["t"]  # busy: too soon to decline
    stop, timeline = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(_Bridge(), proc, timeline, stop))
    try:
        now["t"] += _session_config.NUDGE_AFTER_S + 1
        proc.last_output = now["t"]
        await asyncio.sleep(0.05)
        assert proc.submitted == []
    finally:
        stop.set()
        await pump


async def test_notice_delivered_after_busy_period(monkeypatch):
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc = _Proc()
    proc.last_output = now["t"]
    notices = _Notices([])
    stop, tl = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(_Bridge(), proc, tl, stop, delegations=notices))
    try:
        for step in range(10):  # busy: output every 60s for 10 minutes
            now["t"] += 60
            proc.last_output = now["t"]
            if step == 3:
                notices.pending = ["j1 (codex, ok: 1 files)"]
            if step == 6:
                notices.pending.append("j2 (claude, failed: x)")
            await asyncio.sleep(0.01)
        assert proc.submitted == []
        now["t"] += _session_config.IDLE_AFTER_S + 1
        await wait_for(lambda: bool(proc.submitted))
        assert len(proc.submitted) == 1 and "j1" in proc.submitted[0] and "j2" in proc.submitted[0]
    finally:
        stop.set()
        await pump


async def test_notices_are_requeued_when_submit_raises(monkeypatch):
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc: Any = _Proc()  # `submit` is replaced below
    proc.last_output = now["t"] - _session_config.IDLE_AFTER_S - 1

    async def broken_submit(text, **kwargs):
        raise OSError("pty closed")

    proc.submit = broken_submit
    notices = _Notices(["j1 (codex, ok: 1 files)"])
    stop, tl = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(_Bridge(), proc, tl, stop, delegations=notices))
    await asyncio.sleep(0.02)
    now["t"] += _session_config.IDLE_AFTER_S + 1
    with pytest.raises(OSError, match="pty closed"):
        await asyncio.wait_for(pump, timeout=30)
    assert notices.requeued


async def test_an_undeclined_menu_is_declined_even_while_the_terminal_keeps_writing(monkeypatch):
    """qa: a background agent writing every minute held a blocked main session open forever."""
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc: Any = _Proc()  # given a `type` below
    proc.menu = "Dangerous rm X"
    proc.last_output = now["t"]
    keys: list[str] = []
    proc.type = keys.append
    stop, timeline = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(_Bridge(), proc, timeline, stop))
    try:
        for _ in range(int(_session_config.PROMPT_BUSY_DECLINE_S / 60) + 2):
            now["t"] += 60
            proc.last_output = now["t"]
            await asyncio.sleep(0.005)
        await wait_for(lambda: bool(keys))
        assert keys == ["\x1b"]
    finally:
        stop.set()
        await pump


async def test_the_cap_holds_when_redraws_mangle_the_menu_differently(monkeypatch):
    """qa: the renderer drops different letters per redraw; per-text keys never reached the cap."""
    monkeypatch.setattr(_session_config, "POLL_EVERY_S", 0.0)
    now = _fake_clock(monkeypatch)
    proc: Any = _Proc()  # given a `type` below
    proc.menu = "python3 -m taskgraph run Dangerous rm X"
    proc.last_output = now["t"] - 21
    keys: list[str] = []
    proc.type = keys.append
    stop, timeline = asyncio.Event(), _timeline._Timeline()
    pump = asyncio.create_task(_pump(_Bridge(), proc, timeline, stop))
    variants = [
        "python3 -m taskgaph run Dangerous rm X",
        "pyhon3 -m taskgraph run Dangerous rm X",
        "python3 -m taskgrph run Dangerous rm X",
        "python3 -m tskgraph run Dangerous rm X",
        "python3 -m taskgraph rn Dangerous rm X",
    ]

    async def declined(count: int) -> None:
        # Wait for the pump to answer this redraw, not a fixed sleep: under a loaded parallel run a
        # 20ms sleep let the next redraw replace the menu before the pump had seen it.
        await wait_for(lambda: len(keys) >= count)

    try:
        for shown, variant in enumerate(variants, start=1):
            await declined(min(shown, _session_config.PROMPT_DECLINE_CAP))
            now["t"] += _session_config.PROMPT_REDECLINE_S + 1
            proc.menu = variant
            proc.last_output = now["t"] - 21
        await declined(_session_config.PROMPT_DECLINE_CAP)
        await asyncio.sleep(0.05)  # and nothing past the cap
        assert len(keys) == _session_config.PROMPT_DECLINE_CAP
        assert any("keeps coming back" in row for _, _, row in timeline._rows)
    finally:
        stop.set()
        await pump


_RM = "Bash command rm -rf s3v_cwd/* Dangerous rm operation on statically-unresolvable target: s3v_cwd/*"
# Near-identical retries of it: the command varies, the rejection reason does not.
_RM_RETRIES = [
    _RM,
    "Bash command rm -rf ./s3v_cwd/* Dangerous rm operation on statically-unresolvable target: ./s3v_cwd/*",
    "Bash command rm -rf s3v_cwd/out/* "
    "Dangerous rm operation on statically-unresolvable target: s3v_cwd/out/*",
]
_CHANGE = "do not retry this command or a variant; use a different approach"


def _change_nudges(proc):
    return [text for text in proc.submitted if _CHANGE in text.lower()]


async def _decline(now, proc, menu, declined):
    """Show `menu` until the pump declines it (`declined` Esc presses in all), then let the follow-up go."""
    proc.menu = menu
    proc.last_output = now["t"] - 21
    await wait_for(lambda: len(proc.keys) == declined, what=str(f"decline {declined} of {menu!r}"))
    proc.menu = None  # Escape closed it and the input box came back
    now["t"] += 2
    await wait_for(lambda: proc.last_submission is not None and proc.last_submission[1] == now["t"])
    await asyncio.sleep(0.02)
    now["t"] += 30  # the session retries a little later


async def test_a_prompt_declined_three_times_gets_one_change_approach_nudge_and_no_human(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch, menu=None)
    try:
        for declined, menu in enumerate(_RM_RETRIES, start=1):
            await _decline(now, proc, menu, declined)
        nudges = _change_nudges(proc)
        assert len(nudges) == 1, proc.submitted
        nudge = nudges[0]
        assert "rm -rf" in nudge and "s3v_cwd/out/*" in nudge, "names the declined command"
        assert "Dangerous rm operation on statically-unresolvable target" in nudge, "names the reason"
        assert "Do not retry this command or a variant; use a different approach" in nudge
        assert "mktemp -d" in nudge and "shutil.rmtree" in nudge and "absolute path" in nudge
        # One row for the streak, not one per repeat; and nothing for a human.
        assert sum("change approach" in row for row in _rows(timeline)) == 1
        assert sum("declined a permission prompt" in row for row in _rows(timeline)) == 2
        assert not any("human" in row or "keeps coming back" in row for row in _rows(timeline))
        assert not _timeline._STUCK_STATUS.search(timeline._activity or "")
        # The session keeps running, and the next retry is still declined rather than left open.
        assert not pump.done() and not stop.is_set()
        await _decline(now, proc, _RM, 4)
        assert sum("change approach" in row for row in _rows(timeline)) == 1
        assert not any("keeps coming back" in row for row in _rows(timeline))
    finally:
        stop.set()
        await pump


async def test_a_different_prompt_resets_the_streak(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch, menu=None)
    try:
        await _decline(now, proc, _RM_RETRIES[0], 1)
        await _decline(now, proc, _RM_RETRIES[1], 2)
        await _decline(now, proc, "Bash command npm install left-pad", 3)
        await _decline(now, proc, _RM_RETRIES[2], 4)
        await _decline(now, proc, _RM, 5)
        assert _change_nudges(proc) == []
        assert sum("declined a permission prompt" in row for row in _rows(timeline)) == 5
        await _decline(now, proc, _RM, 6)  # the third rm in a row since the reset
        assert len(_change_nudges(proc)) == 1
    finally:
        stop.set()
        await pump


async def test_a_streak_is_escalated_only_after_several_nudges_and_keeps_being_declined(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch, menu=None)
    try:
        for declined in range(1, _session_config.PROMPT_DECLINE_CAP):
            await _decline(now, proc, _RM, declined)
        assert len(_change_nudges(proc)) == _session_config.PROMPT_REPEAT_NUDGES
        assert not any("keeps coming back" in row for row in _rows(timeline))
        await _decline(now, proc, _RM, _session_config.PROMPT_DECLINE_CAP)
        assert sum("keeps coming back" in row for row in _rows(timeline)) == 1
        await _decline(now, proc, _RM, _session_config.PROMPT_DECLINE_CAP + 1)
        assert sum("keeps coming back" in row for row in _rows(timeline)) == 1
        assert len(_change_nudges(proc)) == _session_config.PROMPT_REPEAT_NUDGES + 2
        assert not pump.done()
    finally:
        stop.set()
        await pump


def test_prompts_that_differ_only_in_paths_or_redraw_damage_are_the_same():
    assert _supervise_support._same_prompt(_RM_RETRIES[0], _RM_RETRIES[2])
    assert _supervise_support._same_prompt(
        "python3 -m taskgraph run Dangerous rm X", "pyhon3 -m taskgaph run Dangerous rm X"
    )
    assert not _supervise_support._same_prompt(_RM, "Bash command npm install left-pad")
    assert not _supervise_support._same_prompt(_RM, "Bash command git push --force origin main")


def _menu(command, description, reason="", squash=False) -> str:
    """The menu as Claude Code draws it: the command, a model-written description, then any reason."""
    lines = [" Bash command", "", f"   {command}", f"   {description}", ""]
    if reason:
        lines += [f" {reason}", ""]
    lines += [" Do you want to proceed?", " ❯ 1. Yes", "   2. No", " Esc to cancel · Tab to amend"]
    text = "\n".join(lines) + "\n"
    prompt = permission_prompt(text.replace(" ", "") if squash else text)
    assert prompt is not None
    return prompt


_REASON = "Dangerous rm operation on statically-unresolvable target: "


def test_the_description_line_does_not_make_the_same_command_a_different_prompt():
    a = _menu("rm -rf s3v_cwd/*", "Clean the output directory", _REASON + "s3v_cwd/*")
    b = _menu("rm -rf s3v_cwd/*", "Wipe stale artifacts before rerun", _REASON + "s3v_cwd/*")
    assert _supervise_support._same_prompt(a, b)
    # Nor does a shared description make different commands the same prompt.
    assert not _supervise_support._same_prompt(
        _menu("cat /etc/hosts", "Read"), _menu("rm /etc/hosts", "Read")
    )
    assert not _supervise_support._same_prompt(
        _menu("rm -rf a/*", "Clean", _REASON + "a/*"), _menu("git push -f", "Clean")
    )


def test_the_same_reason_is_the_same_prompt_for_another_target():
    a = _menu("rm -rf build/*", "Remove build outputs", _REASON + "build/*")
    b = _menu('rm -rf "$OUT"', "Clean", _REASON + '"$OUT"')
    assert _supervise_support._same_prompt(a, b)


def test_a_redraw_with_its_spaces_squashed_is_the_same_prompt():
    normal = _menu("rm -rf s3v_cwd/*", "Clean", _REASON + "s3v_cwd/*")
    squashed = _menu("rm -rf s3v_cwd/*", "Clean", _REASON + "s3v_cwd/*", squash=True)
    assert _supervise_support._same_prompt(normal, squashed)
    other = _menu("chmod -R 777 /etc/*", "Open perms", squash=True)
    assert not _supervise_support._same_prompt(squashed, other)
    assert not _supervise_support._same_prompt(
        _menu("rm -rf s3v_cwd/*", "Clean", squash=True),
        _menu("git push --force origin main", "Push", squash=True),
    )


def test_prompts_with_nothing_to_compare_never_match():
    assert not _supervise_support._same_prompt("/a/*", "~/b")
    assert not _supervise_support._same_prompt("", "")
    assert not _supervise_support._same_prompt("Bash command", "Bash command")


async def test_a_follow_up_waits_for_the_menu_to_close_and_is_dropped_if_it_never_does(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch)
    try:
        await wait_for(lambda: proc.keys == ["\x1b"])
        for _ in range(10):
            now["t"] += _session_config.PROMPT_FOLLOWUP_HOLD_S / 5
            await asyncio.sleep(0.01)
        assert proc.submitted == []
        proc.menu = None
        now["t"] += 2
        await asyncio.sleep(0.05)
        assert proc.submitted == [], "a follow-up held past its bound is dropped, not sent late"
    finally:
        stop.set()
        await pump


async def test_repeat_follow_ups_are_one_short_line_so_they_add_no_inbox_file(monkeypatch):
    long = _RM + " " + "Remove every stale artifact from the previous evaluation run " * 6
    now, proc, timeline, stop, pump = await _start(monkeypatch, menu=None)
    try:
        for declined in range(1, _session_config.PROMPT_REPEAT_AFTER + 3):
            await _decline(now, proc, long, declined)
        regular, first, *repeats = (
            proc.submitted[0],
            *[text for text in proc.submitted if _CHANGE in text.lower()],
        )
        assert "\n" not in regular and len(regular) <= 400
        assert "mktemp -d" in first, "the first change-approach nudge is the full one"
        assert len(repeats) == 2
        for text in repeats:
            assert "\n" not in text and len(text) <= 400, text
            assert "rm -rf s3v_cwd/*" in text
    finally:
        stop.set()
        await pump


async def test_declines_of_alternating_prompts_still_get_a_change_approach_nudge(monkeypatch):
    rm = _menu("rm -rf s3v_cwd/*", "Clean", _REASON + "s3v_cwd/*")
    push = "Bash command git push --force origin main"
    now, proc, timeline, stop, pump = await _start(monkeypatch, menu=None)
    try:
        for declined in range(1, _session_config.PROMPT_ANY_CAP):
            await _decline(now, proc, rm if declined % 2 else push, declined)
        assert _change_nudges(proc) == []
        await _decline(now, proc, push, _session_config.PROMPT_ANY_CAP)
        nudges = _change_nudges(proc)
        assert len(nudges) == 1 and "keep hitting" in nudges[0], proc.submitted[-1]
        assert sum("coming back" in row and "human" in row for row in _rows(timeline)) == 1
        await _decline(now, proc, rm, _session_config.PROMPT_ANY_CAP + 1)
        await _decline(now, proc, push, _session_config.PROMPT_ANY_CAP + 2)
        assert sum("coming back" in row and "human" in row for row in _rows(timeline)) == 1, "escalated once"
        assert len(_change_nudges(proc)) == 3
        assert not pump.done()
    finally:
        stop.set()
        await pump


def test_the_pump_has_no_attended_path():
    """The parametrised tests above once ran an attended arm; there is none to run."""
    import inspect

    assert "unattended" not in inspect.signature(_supervise._pump).parameters


@pytest.mark.parametrize("restored_text", ["Continue fixing the findings.", "", "human direction"])
async def test_enter_waits_for_the_menu_and_checks_the_restored_input(tmp_path, restored_text, monkeypatch):
    proc = _process(tmp_path)
    typed: list[tuple[str, str | None]] = []
    monkeypatch.setattr(proc, "type", lambda text: typed.append((text, proc.menu)))
    message = "Continue fixing the findings."
    submission = asyncio.create_task(proc.submit(message))
    await asyncio.sleep(0.1)
    proc._consume(_PERMISSION)
    await submission
    assert typed == [(message, None)], "Enter approved a menu opened during the debounce"
    await proc.deliver_held()
    assert typed == [(message, None)]

    await proc.submit("Second message.")
    restored_box = _IDLE_BOX.replace("❯ \n", f"❯ {restored_text}\n")
    for start in range(0, len(restored_box), 7):
        proc._consume(restored_box[start : start + 7])
    await proc.deliver_held()
    expected = [message]
    if restored_text == message:
        expected.append("\r")
    expected.extend(["Second message.", "\r"])
    assert [text for text, _ in typed] == expected
    assert all(menu is None for _, menu in typed)
    await proc.deliver_held()
    assert [text for text, _ in typed] == expected


@pytest.mark.parametrize(
    "description", ["Trust the directory for git (safe.directory)", "bypass permissions mode"]
)
@pytest.mark.parametrize("split_menu", [False, True])
def test_first_run_rules_never_answer_a_permission_menu(tmp_path, description, split_menu, monkeypatch):
    proc = _process(tmp_path)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    menu = _PERMISSION.replace("Bash command", description)
    if split_menu:
        proc._consume(_PERMISSION)
        proc._consume(description)
    else:
        proc._consume(menu)
    assert proc.menu is not None
    assert typed == []


@pytest.mark.parametrize("ready", [_IDLE_BOX, "❯ \n", "> \n"])
@pytest.mark.parametrize(
    "description", ["Trust the directory for git (safe.directory)", "bypass permissions mode"]
)
def test_first_run_rules_are_disarmed_when_the_session_is_ready(tmp_path, ready, description, monkeypatch):
    proc = _process(tmp_path)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    proc._consume(ready)
    proc._consume(description)
    assert typed == []


def test_environment_assignments_remain_part_of_the_prompt_command():
    command, reason = _supervise_support._prompt_keys("Bash command\nFOO=bar cmd --flag\nRun the command")
    assert command == "foobarcmdflag"
    assert reason == ""


async def test_the_escalation_status_clears_when_the_menu_closes(monkeypatch):
    now, proc, timeline, stop, pump = await _start(monkeypatch)
    try:
        for declined in range(1, _session_config.PROMPT_DECLINE_CAP + 1):
            await wait_for(lambda: len(proc.keys) == declined)  # noqa: B023 -- awaited before the loop variable moves on
            now["t"] += _session_config.PROMPT_REDECLINE_S + 1
            proc.last_output = now["t"] - 21
        await wait_for(lambda: "keeps coming back" in (timeline._activity or ""))
        proc.menu = None
        await wait_for(lambda: "keeps coming back" not in (timeline._activity or ""))
    finally:
        stop.set()
        await pump


async def test_a_footer_without_restored_input_does_not_submit_pre_menu_text(tmp_path, monkeypatch):
    proc = _process(tmp_path)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    message = "Continue fixing the findings."
    submission = asyncio.create_task(proc.submit(message))
    await asyncio.sleep(0.1)
    proc._consume(f"❯ {message}\n" + _PERMISSION.replace("❯ ", ""))
    await submission
    assert typed == [message]
    proc._consume("? for shortcuts\n")
    await proc.deliver_held()
    assert typed == [message], "an old prompt does not prove our text survived the menu"


def test_a_partial_onboarding_cursor_does_not_disarm_first_run_rules(tmp_path, monkeypatch):
    proc = _process(tmp_path)
    typed: list[str] = []
    monkeypatch.setattr(proc, "type", typed.append)
    proc._consume("❯ ")
    proc._consume("No, exit\n  Yes, I accept\nEnter to confirm · Esc to cancel\n")
    assert typed == ["\x1b[B\r"]
