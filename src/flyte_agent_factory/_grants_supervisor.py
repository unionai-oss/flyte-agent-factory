"""The project supervisor's side of credential grants (`docs/credential-grants.md`).

`Credentials` owns the grant store and its loopback server for the life of one project's main supervisor
and reads what the owner sends: the sealed text, and `/revoke-credential`. `GrantRequests` answers
`credential-grant request`: it asks the owner through an `ask-` condition and hands the sealed answer to
the store. No value passes through here: the owner's answer is sealed text, and every row, answer file
and message written below is metadata.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import flyte.errors

from flyte_agent_factory import _session_config, _supervise_support, _timeline
from flyte_agent_factory._bridge import (
    _CLOSE_MARGIN_S,
    ANSWERS,
    ASK_GRACE_S,
    REQUESTS,
    Bridge,
    _write_atomic,
    find_credential_shape,
    redact_tokens,
    register_redaction_source,
    unregister_redaction_source,
)
from flyte_agent_factory._conditions import open_condition
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._grant_links import UNAVAILABLE_BASE, read_page_base_url
from flyte_agent_factory._grant_seal import NAME_RE, GrantError, find_sealed
from flyte_agent_factory._grants import MAX_HOURS, GrantServer, GrantStore, format_utc

PREFIX = "grant-"
REVOKE_COMMAND = "/revoke-credential"
_NOTICE_SUFFIX = ".notice"
_INBOX = "the inbox"
#: What `_inbox._watch_app_messages` passes as `source` to `Credentials.handle`.
_APP = "the Claude app"


#: A run of base64 characters as long as a secret access key or a session token, holding digits and both
#: cases: what an owner pastes when the values are copied without their names. Prose, paths, URLs and hex
#: digests (one case only) do not match.
_BARE_SECRET = re.compile(
    r"(?<![A-Za-z0-9/+=])(?=[A-Za-z0-9/+=]*\d)(?=[A-Za-z0-9/+=]*[a-z])(?=[A-Za-z0-9/+=]*[A-Z])[A-Za-z0-9/+=]{30,}"
)
_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def _holds_raw_credential(text: str, *, answering: bool) -> bool:
    """Say whether `text` carries a credential value that was not sealed.

    A named shape always does (`find_credential_shape`). A bare value does too: in the answer to a
    credential question (`answering`) on its own, and elsewhere next to a key id, so that direction which
    happens to quote a long identifier, or only a key id, still reaches the session.
    """
    if find_credential_shape(text):
        return True
    return bool(_BARE_SECRET.search(text)) and (answering or bool(_KEY_ID.search(text)))


class _Refused(Exception):
    """A request that gets a refusal for the session; the text is safe to show."""


def _validate_request(request: Any, stem: str) -> tuple[str, float, str, float]:
    if not isinstance(request, dict) or request.get("id") != stem:
        raise _Refused("request id must match its filename")
    name, hours = request.get("name"), request.get("hours")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise _Refused(f"{name!r} is not a valid credential name (lower case letters, digits, '-')")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours <= MAX_HOURS:
        raise _Refused(f"--hours must be more than 0 and at most {MAX_HOURS:g}")
    timeout = request.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise _Refused("--timeout must be a number of seconds")
    if timeout <= 0:
        raise _Refused("--timeout must be more than zero: a question nobody waits for is not asked")
    return name, float(hours), str(request.get("why") or ""), float(timeout)


class GrantRequests:
    """Answers `grant-*.json` requests by asking the owner for a sealed credential."""

    def __init__(
        self,
        root: Path,
        store: GrantStore,
        bridge: Bridge,
        emit: Callable[[str, str], None],
        base_url: Callable[[], Awaitable[str]],
    ) -> None:
        self.root = root
        self._store = store
        self._bridge = bridge
        self._emit = emit
        self._base_url = base_url
        self._seen: set[str] = set()
        self._pending: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        try:
            while True:
                self.scan()
                await asyncio.sleep(1)
        finally:
            for task in self._pending:
                task.cancel()
            await asyncio.gather(*self._pending, return_exceptions=True)

    def scan(self) -> None:
        for path in sorted((self.root / REQUESTS).glob(f"{PREFIX}*.json")):
            if path.name in self._seen or (self.root / ANSWERS / path.name).exists():
                continue
            self._seen.add(path.name)
            task = asyncio.create_task(self.service(path))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def _answer(self, path: Path, granted: bool, message: str) -> None:
        _write_atomic(
            self.root / ANSWERS / path.name, {"id": path.stem, "granted": granted, "message": message}
        )

    async def service(self, path: Path) -> None:
        try:
            name, hours, why, timeout = _validate_request(json.loads(path.read_text()), path.stem)
        except _Refused as exc:
            self._answer(path, False, f"refused: {exc}")
            return
        except (OSError, ValueError):
            self._answer(path, False, "refused: the request file is not valid")
            return
        why = redact_tokens(why[:500])
        base = await self._base_url()
        link = self._store.link(base or UNAVAILABLE_BASE, name, hours)
        self._emit("🙋", f"credential {name} requested for {hours:g}h: {why or '(no reason given)'}")
        # Slightly past the CLI's own deadline, as the bridge does: open while the asker listens.
        deadline = timeout + ASK_GRACE_S
        condition_name = ""
        waiter: asyncio.Future[str] | None = None
        try:
            condition, used = await open_condition(
                "ask",
                self._bridge.next_seq(),
                prompt=_write_prompt(self._store.project, name, hours, why, link, bool(base)),
                prompt_type="markdown",
                data_type=str,
                timeout=deadline,  # resolves the condition even if this supervisor is gone first
            )
            self._bridge.keep_seq(used)
            condition_name = getattr(condition, "name", "")
            waiter = asyncio.ensure_future(condition.wait.aio())
            answer = await asyncio.wait_for(asyncio.shield(waiter), timeout=deadline - _CLOSE_MARGIN_S)
        except (asyncio.TimeoutError, flyte.errors.ConditionTimedoutError):
            self._answer(path, False, "nobody answered in time; no credential")
            if (
                condition_name
                and waiter is not None
                and await self._bridge._close(
                    condition_name, False, "closed: timed out, no credential granted"
                )
            ):
                waiter.cancel()
            return
        except REMOTE_ERRORS as exc:  # told to the session, not swallowed
            self._answer(path, False, f"could not ask the owner: {type(exc).__name__}")
            self._emit("⚠️", f"credential {name}: could not ask: {type(exc).__name__}")
            return
        granted, message = self._take(name, answer or "")
        self._answer(path, granted, message)

    def _take(self, name: str, answer: str) -> tuple[bool, str]:
        """Hand a sealed answer to the store; (granted, what to tell the session). Never echoes a value."""
        text = answer.strip()
        sealed = find_sealed(text)
        if sealed:
            try:
                info = self._store.accept(sealed, expected_name=name)
            except GrantError as exc:
                self._emit("⚠️", f"credential {name}: sealed answer refused: {exc}")
                return False, f"refused: {exc}"
            self._emit("✅", f"credential {name}: sealed answer received")
            return True, f"granted until {format_utc(info.expires_at)}"
        if _holds_raw_credential(text, answering=True):
            self._emit("⚠️", f"credential {name}: the answer was a raw credential; it was not used")
            return False, (
                "refused: the answer was a raw credential, not sealed text, and was not used. It was typed "
                "where others can read it: end it in AWS. Ask again and seal it with the link."
            )
        self._emit("🚫", f"credential {name}: refused by the owner")
        # No free text: any heuristic can miss a raw secret of a shape nobody guessed, and the owner's
        # refusal needs no reason to be clear.
        return False, "refused by the owner"


def _write_prompt(project: str, name: str, hours: float, why: str, link: str, page: bool) -> str:
    how = (
        f"1. Open this link (signed in to Union): {link}\n"
        "2. Paste a short-lived, scoped AWS credential, choose when access ends, and press **Seal**.\n"
        "3. Copy the sealed text (it starts with `fgc1.`) and paste it back here as the answer.\n"
        if page
        else "The seal page is unavailable (the factory-mcp app has no known URL), so use the command "
        "below, then paste the sealed text back here as the answer.\n"
    )
    return (
        f"### Credential request: `{name}` for project {project}\n\n"
        f"Project **{project}** asks for credential **{name}** (AWS) for **{hours:g} hours**.\n\n"
        f"Why: {why or '(no reason given)'}\n\n"
        f"{how}\n"
        "Answer `no` to refuse. Never paste a raw credential here: only the sealed text.\n\n"
        "No credential is given unless you answer with sealed text before this question times out.\n\n"
        f"Without a browser: `python -m flyte_agent_factory._grants seal --link '{link}'`"
    )


class Credentials:
    """One project's grants: store, loopback server, requests, notices to the session, the owner's inbox."""

    def __init__(
        self, project: str, timeline: _timeline._Timeline, bridge: Bridge, *, state_root: Path | None = None
    ) -> None:
        state_root = state_root or _session_config.STATE_ROOT
        self.timeline = timeline
        self._inbox = _session_config.WORKSPACE / _session_config._INBOX_DIR
        self._notices: asyncio.Queue[Path] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._ending = False
        self.store = GrantStore(
            project,
            state_path=state_root / "bridge" / "grants.json",
            on_event=timeline.row,
            notify=self._notify,
        )
        self.server = GrantServer(self.store)
        self.requests = GrantRequests(bridge.root, self.store, bridge, timeline.row, self._page_base)
        timeline.credentials = self.store

    def start(self) -> None:
        # Notices a previous attempt wrote and never delivered come first.
        for pending in sorted(self._inbox.glob(f"credential-*{_NOTICE_SUFFIX}")):
            self._notices.put_nowait(pending)
        self.store.restore()
        self.server.start()
        register_redaction_source(self._secrets)

    def _secrets(self) -> tuple[str, ...]:
        """Return what published text must never carry: the live values and the session's loopback token."""
        return (*self.store.live_values(), self.server.token)

    def session_env(self) -> dict[str, str]:
        """For the session's own environment only: never `os.environ`, which every child inherits."""
        return self.server.env()

    def coroutines(self, session: asyncio.Future[Any], stop: asyncio.Event) -> dict[str, Awaitable[None]]:
        return {
            "credential-grants": self.store.run(stop),
            "credential-requests": self.requests.run(),
            "credential-notices": self._deliver(session),
        }

    def handle(self, message: str, source: str = _INBOX) -> bool:
        """Act on a message meant for the grants and return True; False for anything else.

        The sealed text and the revoke command are consumed here: never written to the inbox, recorded as
        direction, forwarded to the session or echoed to the timeline. `source` says where the text arrived,
        for the row about a raw credential.
        """
        text = message.strip()
        sealed = find_sealed(text)
        if sealed:
            # Anywhere in the message: an owner who adds "here it is" must not send it on as direction.
            try:
                self.store.accept(sealed)  # the store's own row says what was granted
            except GrantError as exc:
                self.timeline.row("⚠️", f"sealed credential refused: {exc}")
            return True
        if _holds_raw_credential(text, answering=False):
            # A raw credential where sealed text belongs: never forwarded to the session or kept as direction.
            # Typed in the app, the session has already read it: only the inbox can stop it short of that.
            seen = "; the session has already seen it" if source == _APP else ""
            self.timeline.row(
                "⚠️",
                f"a raw credential was sent to {source}; it was not used or forwarded{seen}. It was typed "
                "where others can read it: end it in AWS, and give credentials only as sealed text",
            )
            return True
        command, _, rest = text.partition(" ")
        if command.lower() != REVOKE_COMMAND:
            return False
        words = rest.split()
        name = words[0] if words else None  # the first word: "/revoke-credential aws-x please" revokes aws-x
        if not self.store.revoke(name, reason="revoked by the owner"):
            self.timeline.row("⚠️", f"{REVOKE_COMMAND}: no active credential{f' {name}' if name else ''}")
        return True

    def close(self) -> None:
        """End every grant and stop serving: the project is over. Before the final checkpoint."""
        self._ending = True  # the session is going too; a notice written now would only outlive it
        self.store.close("the project ended")
        self.server.stop()
        unregister_redaction_source(self._secrets)

    async def _page_base(self) -> str:
        profile = _session_config._PROFILE
        return await asyncio.to_thread(
            read_page_base_url,
            profile.mcp.public_url if profile and profile.mcp else "",
            os.environ.get("FLYTE_INTERNAL_PROJECT", ""),
            os.environ.get("FLYTE_INTERNAL_DOMAIN", ""),
        )

    def _notify(self, text: str) -> None:
        """Queue a metadata-only message for the session; safe from the server's threads."""
        if self._ending:
            return
        self._inbox.mkdir(parents=True, exist_ok=True)
        notice = self._inbox / f"credential-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}{_NOTICE_SUFFIX}"
        notice.write_text(text + "\n")
        self._loop.call_soon_threadsafe(self._notices.put_nowait, notice)

    async def _deliver(self, session: asyncio.Future[Any]) -> None:
        """Submit each notice once the session exists; the file goes once it is submitted."""
        while True:
            notice = await self._notices.get()
            try:
                text = notice.read_text().strip()
            except OSError:
                continue
            # Shielded: cancelling this task must not cancel the caller's future for the session.
            proc = await asyncio.shield(session)
            await proc.submit(f"Credential notice: {text}", label="Credential notice")
            notice.unlink(missing_ok=True)


def start_credentials(
    project: str, timeline: _timeline._Timeline, bridge: Bridge
) -> tuple[Credentials, dict[str, str]]:
    """Start a project's grants and return them with the session's grant environment (never `os.environ`)."""
    credentials = Credentials(project, timeline, bridge)
    credentials.start()
    return credentials, credentials.session_env()


def start_supervised(
    credentials: Credentials,
    session: asyncio.Future[Any],
    stop: asyncio.Event,
) -> list[asyncio.Task[None]]:
    """Start the grants' background loops as supervised tasks, for `project` to cancel with its others.

    None is fatal: a project without grants still works, and the loops say so if they die.
    """
    return [
        asyncio.create_task(
            _supervise_support._supervised(coro, name, credentials.timeline, stop, fatal=None), name=name
        )
        for name, coro in credentials.coroutines(session, stop).items()
    ]
