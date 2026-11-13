"""Run the Claude Code CLI under a pseudo-terminal and talk to it.

Why a pty rather than plain pipes:

1. Claude Code refuses to start when stdin/stdout isn't a terminal -- it can't ask
   `Trust <directory>?` and exits with `Workspace not trusted`. A pty gives it a
   terminal to ask in, and `PromptRule` answers.
2. Interactive mode keeps a prompt we can type into. That is how the supervisor
   delivers direction that arrives over a Flyte condition into the same
   conversation.
"""

from __future__ import annotations

import asyncio
import os
import pty
import re
import signal
import termios
import textwrap
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ._auth import token_fd

# Escape sequences, OSC strings, and the control characters a TUI emits by the
# thousand. Stripped before matching or logging, so prompt patterns see plain
# text and the Flyte report isn't full of cursor moves.
_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[=>()#][A-Za-z0-9]?"  # charset / mode
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # stray control chars (keeps \t \n \r)
)

# How Claude Code reports that the credential it was given no longer works:
# a one-line answer to the first prompt, plus a permanent status-bar notice.
# There is no exit code and no stderr -- the TUI just sits there, which from
# the supervisor's side is indistinguishable from a session thinking quietly.
# Matched against whitespace-stripped text because the TUI positions the
# status bar with cursor-forward escapes rather than spaces.
_LOGIN_EXPIRED = re.compile(r"Loginexpired|Notloggedin|Pleaserun/login")
_WHITESPACE = re.compile(r"\s+")
# The subscription's usage cap: "You've hit your session limit · resets 7pm (UTC)".
# Like an expired login it has no exit code -- the turn just ends and the TUI
# waits -- so it read as "idle" for seven hours on two projects at once.
# Whitespace-stripped; the reset text is kept for the report.
#: "You've hit your … limit", whitespace-stripped (the TUI positions text with cursor
#: moves, not spaces). Anchored on the `⎿` Claude Code draws before it, so prose,
#: diffs and docs quoting it do not match; tolerant of letters a redraw drops.
_LIMIT_HIT = re.compile(
    r"⎿You'?v?e?hi?t?your(?:session|usage|weekly|daily|opus|sonnet|5-hour)?limit", re.IGNORECASE
)
#: "… limit reached · resets …" on raw text: must start a line (or follow `⎿`) and
#: carry a reset clause, so code and prose mentioning it do not match.
_LIMIT_REACHED = re.compile(
    r"(?:^|[\r\n⎿])[ \t]*(?:Claude\s*(?:AI\s*)?usage|5-hour|session|weekly|opus|sonnet)\s*limit\s*reached"
    r"\s*[·∙.|,:\-]*\s*(?:resets|Your\s*limit\s*will\s*reset\s*at)",
    re.IGNORECASE,
)
#: The reset clause, read from raw text so it keeps its spaces.
_LIMIT_RESET = re.compile(
    r"(?:re?se?ts|will\s*reset\s*at)\s*[^\r\n⎿]{1,40}?(?:\)|(?=[\r\n])|$)", re.IGNORECASE
)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


# Box-drawing characters (and the `❯` cursor) the TUI draws around a menu. Stripped from
# both ends of a line so "│ ❯ 1. Yes   │" is read as "1. Yes".
_MENU_EDGE = re.compile(r"^[\s\u2500-\u257f❯>|]+|[\s\u2500-\u257f|]+$")
#: How much of the end of the screen a menu must fit in to count as "the menu on screen
#: now" rather than the session talking about one.
_MENU_LINES = 15
_MENU_CHARS = 800


def _loose(text: str) -> str:
    """Return a pattern for `text` that tolerates the TUI dropping or adding spaces."""
    return r"\s*".join(re.escape(char) for char in text if char != " ")


# What the input box draws beneath itself. A confirmation menu replaces it, so seeing it
# again means the menu is gone -- whoever closed it, and however much other output
# scrolled by in the meantime.
_INPUT_BOX_FOOTER = re.compile(_loose("(shift+tab to cycle)") + "|" + _loose("? for shortcuts"))


def permission_prompt(text: str) -> str | None:
    """Summarise the confirmation menu at the end of `text`, or None if there is none.

    The summary is the (up to three) lines above the question, newline-separated.

    Requires the menu's structure in order, each part at the start of a line (Claude Code
    draws it that way even when cursor positioning drops the spaces): the question, `1. Yes`,
    `2. No` (or `2. Yes, and ...` then `3. No ...`), and the `Esc to cancel` footer as the
    last line, all within the last few lines. One sentence that merely mentions them, or a
    menu that other output has since scrolled past, is not an open menu.
    """
    lines = [
        line
        for line in (_MENU_EDGE.sub("", raw) for raw in re.split(r"\r\n|\r|\n", strip_ansi(text)))
        if line
    ]
    compact = [_WHITESPACE.sub("", line).lower() for line in lines]
    # The footer closes the dialog and nothing is drawn below it, so it must be the last
    # line: a menu followed by anything else is scrolled output -- or a `cat` of one.
    if not compact or not (compact[-1].startswith("esc") and "cancel" in compact[-1]):
        return None
    first = len(lines) - 1
    size = len(lines[-1])
    while first > 0 and len(lines) - first < _MENU_LINES and size + len(lines[first - 1]) <= _MENU_CHARS:
        first -= 1
        size += len(lines[first])
    for question in range(len(lines) - 2, first - 1, -1):
        if not (compact[question].startswith("doyouwantto") and "?" in compact[question]):
            continue
        rest = iter(compact[question + 1 : -1])
        for line in rest:
            if line.startswith("1.yes"):
                break
        else:
            continue
        # The second option is a No, or the three-option form's "Yes, and ..." then a No.
        for line in rest:
            if line.startswith("2.no"):
                three = False
                break
            if line.startswith("2.yes,"):
                three = True
                break
        else:
            continue
        if three and not any(line.startswith("3.no") for line in rest):
            continue
        context = lines[max(0, question - 3) : question]
        # One line per screen line, so a reader can tell the command and the reason from
        # Claude Code's model-written description of it (`agent._same_prompt` does).
        return "\n".join(" ".join(line.split()) for line in context)[-500:] or lines[question]
    return None


@dataclass
class PromptRule:
    """A first-run question to answer automatically."""

    pattern: re.Pattern[str]
    response: str
    once: bool = True
    _fired: bool = False

    def match(self, window: str) -> bool:
        if self.once and self._fired:
            return False
        if self.pattern.search(window):
            self._fired = True
            return True
        return False


def default_prompt_rules() -> list[PromptRule]:
    """Answers for the dialogs that block an unattended first start.

    Each of these is a one-time confirmation Claude Code persists once answered,
    so on a restored project they simply never fire.

    Deliberately absent: a generic "press enter to continue" rule. A rule that
    blindly confirms whatever is on screen eventually confirms a dialog whose
    default is the option you did not want -- which is exactly what the
    bypassPermissions screen below would have suffered.
    """
    yes = "y\r"
    # Move the selection down one, then confirm. For menus whose highlighted
    # option is the one we do *not* want.
    down_then_enter = "\x1b[B\r"
    return [
        # `Trust <directory>? [y/N]` -- shown in a directory not yet trusted.
        PromptRule(re.compile(r"trust (the )?(files|folder|directory)|trust .{0,80}\?", re.IGNORECASE), yes),
        # The bypassPermissions acceptance screen. This one is a two-option
        # menu rather than a y/n prompt, and the highlighted option is
        # "No, exit" -- so a bare Enter quits Claude Code instead of starting
        # it, parking an unattended session at startup with no session ever
        # registering. Match the accept label, which appears nowhere else.
        PromptRule(
            re.compile(r"yes,\s*i\s*accept|bypass\s*permissions\s*mode", re.IGNORECASE),
            down_then_enter,
        ),
        # Onboarding/theme picker, if the pre-seeded config didn't take.
        PromptRule(re.compile(r"(choose|select) (a )?(theme|text style)", re.IGNORECASE), "\r"),
    ]


class ClaudeProcess:
    """A Claude Code CLI process on a pty, with line callbacks and typed input."""

    #: Summary of a confirmation menu believed to be open, else None. Sticky: a background
    #: agent can keep writing until the menu has scrolled out of any buffer (a real run sat
    #: on one for 16 minutes), so it is cleared only when the input box is drawn again.
    #: Class-level defaults because tests build bare instances with `__new__`.
    menu: str | None = None
    _menu_scan = ""
    #: Messages submitted while `menu` was set, oldest first, and the lock that keeps two
    #: deliveries from interleaving their keystrokes. Created on first use, for the same reason.
    _held: list[tuple[str, str]] | None = None
    _delivering: asyncio.Lock | None = None
    _pending_enter: str | None = None
    _input_text: str | None = None

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_line: Callable[[str], None] | None = None,
        prompt_rules: Iterable[PromptRule] | None = None,
        tail_lines: int = 400,
        inbox_messages: bool = False,
        token: str = "",
    ) -> None:
        self.inbox_messages = inbox_messages
        #: The setup-token, handed to each start through a pipe (`_auth.token_fd`), never `env`.
        self._token = token
        self._argv = list(argv)
        self._cwd = cwd
        self._env = env
        self._on_line = on_line
        self._rules = list(prompt_rules) if prompt_rules is not None else default_prompt_rules()

        self._master: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._pending = ""
        # Prompt text can straddle reads, so rules match against a sliding
        # window rather than a single chunk.
        self._window = ""
        # Like `_window`, but never cleared: used to find the logged-out notice, which can
        # straddle reads.
        self._scan = ""
        self.tail: deque[str] = deque(maxlen=tail_lines)
        self.login_expired = False
        #: `time.monotonic()` of the last terminal output, or None. While a turn runs the
        #: spinner writes several times a second (in-process subagents included); an idle
        #: prompt writes nothing. Measured live: ~350 B/s busy, 0 B idle for 100 s.
        self.last_output: float | None = None
        #: Total characters of terminal output, only ever growing.
        self.output_count = 0
        #: `time.monotonic()` when a usage-limit notice last appeared, and its reset text.
        self.limit_hit_at: float | None = None
        #: Wall-clock time of the same refusal: a clock-only reset in `limit_text` is read against it.
        self.limit_seen_wall: float | None = None
        self.limit_text = ""
        self.last_submission: tuple[str, float, int] | None = None
        self.sent_texts: deque[str] = deque(maxlen=500)
        self._limit_scan = ""
        self._limit_raw = ""
        #: Whether anything was submitted since the last counted notice. A redraw of a
        #: notice already on screen comes without new input; a genuine refusal follows one.
        self._submitted_since_limit = True
        self._exited = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        master, slave = pty.openpty()
        _size_pty(slave)
        self._master = master

        with token_fd(self._env, self._token) as (env, pass_fds):
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=str(self._cwd),
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        # The child owns the slave end now; holding it open here would keep the
        # master from ever reporting EOF.
        os.close(slave)

        asyncio.get_running_loop().add_reader(master, self._drain)

    def _drain(self) -> None:
        assert self._master is not None
        try:
            chunk = os.read(self._master, 65536)
        except OSError:
            # EIO on a pty master is how the child's exit arrives.
            chunk = b""
        if not chunk:
            self._close_reader()
            self._exited.set()
            return
        self._consume(chunk.decode("utf-8", "replace"))

    def _close_reader(self) -> None:
        if self._master is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self._master)
        except (ValueError, RuntimeError):
            pass
        try:
            os.close(self._master)
        except OSError:
            pass
        self._master = None

    def _consume(self, text: str) -> None:
        clean = strip_ansi(text)

        self._window = (self._window + clean)[-4000:]
        self._track_menu(clean)
        if _INPUT_BOX_FOOTER.search(self._window) or re.search(
            r"(?:^|[\r\n])[ \t]*[❯>][ \t]*[\r\n]", self._window
        ):
            # Restored sessions skip onboarding; their unused rules must not answer
            # matching prose or permission requests later in the conversation.
            self._rules.clear()
        if self.menu is None:
            for rule in self._rules:
                if rule.match(self._window):
                    # Clear the window so one prompt can't re-trigger a `once=False`
                    # rule on every subsequent read.
                    self._window = ""
                    self.type(rule.response)

        # Scanned against a sliding buffer, not the current chunk: a pty delivers whatever
        # happens to be ready, so the notice can arrive split across reads. Deliberately
        # separate from `_window`, which a firing prompt rule clears.
        self._scan = (self._scan + clean)[-4000:]

        if not self.login_expired and _LOGIN_EXPIRED.search(_WHITESPACE.sub("", self._scan)):
            self.login_expired = True

        if clean:
            self.output_count += len(clean)
            self.last_output = time.monotonic()
            self._scan_for_limit(clean)

        self._pending += clean
        # A TUI repaints with \r; treat it as a line break for logging only.
        *lines, self._pending = re.split(r"\r\n|\r|\n", self._pending)
        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            self.tail.append(line)
            if self._on_line:
                self._on_line(line)

    # -- input -------------------------------------------------------------- #

    def type(self, text: str) -> None:
        """Write raw keystrokes to the terminal."""
        if self._master is None:
            return
        try:
            os.write(self._master, text.encode())
        except OSError:
            pass

    def _track_menu(self, clean: str) -> None:
        self._menu_scan = (self._menu_scan + clean)[-4000:]
        footers = list(_INPUT_BOX_FOOTER.finditer(self._menu_scan))
        if footers:
            self.menu = None
            # Only the restored input box can tell us whether a held Enter still
            # belongs to our message. Missing or edited text must not be submitted.
            inputs = re.findall(
                r"(?:^|[\r\n])[ \t]*❯[ \t]*([^\r\n]*)", self._menu_scan[: footers[-1].start()]
            )
            self._input_text = inputs[-1].strip() if inputs else ""
            # Forget the dismissed menu's text, or it would be detected again.
            self._menu_scan = self._menu_scan[footers[-1].end() :]
        found = permission_prompt(self._menu_scan)
        if found is not None:
            self.menu = found
            self._input_text = None
            # A prompt drawn before this menu is not evidence of restored input.
            self._menu_scan = ""

    def _scan_for_limit(self, clean: str) -> None:
        """Record a usage-limit notice once per appearance.

        Searches the carried-over tail *plus the whole chunk*: a screen redraw puts
        the notice in the middle of one large read, followed by kilobytes of prompt
        box, and trimming before searching lost it (qa, on the real redraw block).
        """
        stripped = self._limit_scan + _WHITESPACE.sub("", clean)
        raw = self._limit_raw + clean
        found = _LIMIT_HIT.search(stripped) or _LIMIT_REACHED.search(raw)
        if found:
            # The reset clause at the notice itself, not at the first "limit" in the chunk.
            anchor = re.search(r"⎿[^\r\n]*?limit|(?:^|[\r\n])[^\r\n]*limit\s*reached", raw, re.IGNORECASE)
            reset = _LIMIT_RESET.search(raw[anchor.start() :] if anchor else raw)
            text = " ".join(reset.group(0).split()) if reset else ""
            # New when something was submitted since the last one (a genuine refusal),
            # when it is the first, or when its reset time differs (a new episode the
            # session walked into on its own, with no submit in between).
            if self._submitted_since_limit or self.limit_hit_at is None or (text and text != self.limit_text):
                self.limit_hit_at = time.monotonic()
                self.limit_seen_wall = time.time()
                self.limit_text = text
                self._submitted_since_limit = False
            self._limit_scan, self._limit_raw = "", ""
        else:
            self._limit_scan, self._limit_raw = stripped[-160:], raw[-400:]

    async def submit(self, text: str, *, label: str = "") -> None:
        """Submit a message, or hold it while a permission menu is open (see `deliver_held`).

        Nothing is ever typed into an open menu: text or Enter landing there can approve the
        command the supervisor is declining. Held messages are kept however long the menu
        stays, and go out in order once it closes.
        """
        if self._held is None:
            self._held = []
        self._held.append((text, label))
        await self.deliver_held()

    async def deliver_held(self) -> None:
        """Type the held messages, oldest first, while no menu is open. The pump calls it each pass."""
        if not self._held and self._pending_enter is None:
            return
        if self._delivering is None:
            self._delivering = asyncio.Lock()
        async with self._delivering:
            if self._pending_enter is not None and not self._finish_enter():
                return
            while self._held and self.menu is None:
                text, label = self._held.pop(0)
                await self._type_message(text, label)
                if self._pending_enter is not None:
                    return

    def _finish_enter(self) -> bool:
        if self.menu is not None or self._input_text is None:
            return False
        if self._pending_enter == self._input_text:
            self.type("\r")
        self._pending_enter = None
        return True

    async def _type_message(self, text: str, label: str) -> None:
        """Type one message, keeping large payloads out of the terminal paste path."""
        one_line = " ".join(text.split()) if self.inbox_messages else text
        if self.inbox_messages and (len(text) > 400 or "\n" in text or "\r" in text):
            inbox = self._cwd / ".claude-flyte/inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            # Exclusive creation survives restarts and cannot overwrite direction files.
            seq = 1
            while True:
                path = inbox / f"message-{seq:04d}.md"
                try:
                    with path.open("x") as message:
                        # Read truncates long lines; textwrap handles whitespace and unbroken tokens.
                        message.write(
                            "\n".join(
                                "\n".join(
                                    textwrap.wrap(
                                        line,
                                        width=1500,
                                        replace_whitespace=False,
                                        expand_tabs=False,
                                        drop_whitespace=False,
                                        break_on_hyphens=False,
                                    )
                                )
                                if len(line) > 1500
                                else line
                                for line in text.split("\n")
                            )
                        )
                    break
                except FileExistsError:
                    seq += 1
            rel = path.relative_to(self._cwd)
            one_line = (
                f"{label or 'New supervisor message'}. The complete message is in {rel}; "
                "read the whole file and act on it."
            )
        if not one_line:
            return
        # Remembered so the supervisor can tell its own messages from a human's in the
        # transcript: anything else typed into this session came from a person at the session.
        self.last_submission = (text, time.monotonic(), self.output_count)
        self.sent_texts.append(one_line)
        self._submitted_since_limit = True
        self._pending_enter = self._input_text = one_line
        self.type(one_line)
        # The prompt debounces input; submitting in the same write as the text
        # occasionally lands before the box has the full string.
        await asyncio.sleep(0.4)
        self._finish_enter()

    # -- teardown ----------------------------------------------------------- #

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    async def wait(self) -> int:
        assert self._proc is not None
        code = await self._proc.wait()
        # EOF on the master can lag the child's exit slightly.
        try:
            await asyncio.wait_for(self._exited.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._close_reader()
        return code

    async def stop(self, *, grace: float = 20.0) -> None:
        """Ask the session to exit cleanly, then insist."""
        if self._proc is None or self._proc.returncode is not None:
            return
        # `/exit` lets Claude Code flush its transcript, which a signal would skip.
        # Bounded: a submit stuck behind a wedged delivery must not keep the signals below from firing.
        try:
            await asyncio.wait_for(self.submit("/exit"), timeout=min(grace, 5.0))
        except asyncio.TimeoutError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=grace)
            return
        except asyncio.TimeoutError:
            pass

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(self._proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
                return
            except asyncio.TimeoutError:
                continue

    def __del__(self) -> None:  # pragma: no cover - best-effort fd hygiene
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:
                pass


def _size_pty(fd: int, rows: int = 50, cols: int = 200) -> None:
    """Give the TUI a generous window.

    Claude Code hides parts of its own output when the terminal is narrow --
    including the `/rc active` indicator that carries the session URL this
    module scrapes.
    """
    try:
        import fcntl
        import struct

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (ImportError, OSError):
        pass
