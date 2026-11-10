"""Keep the author's paused inbox durable across usage limits and pod restarts."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Protocol

from ._bridge import _write_atomic


class AuthorProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...
    async def submit(self, text: str, *, label: str = "") -> None: ...
    async def stop(self, *, grace: float = 20.0) -> None: ...


class AuthorSession:
    # Background tasks share the process reference and paused inbox; engine policy stays in agent.
    # Set by the supervisor, not here: declared so the assignments type-check, without defining them
    # (an attribute defined here would hide the engine process's own `retry_at` from `__getattr__`).
    retry_at: float | None
    _checkpointed_limit: tuple[str | None, float | None]

    def __init__(self, current: AuthorProcess, pending_file: Path) -> None:
        self.current = current
        self._pending_file = pending_file
        self._paused = False
        self._switching = False
        self._resumed_hit: float | None = None
        self._drained: list[
            str
        ] = []  # what the last resume took out of `pending`, until it is known to be accepted
        self._lock = asyncio.Lock()
        try:
            self.pending: list[str] = json.loads(pending_file.read_text())
        except FileNotFoundError:
            self.pending = []

    # Engine-specific attributes are deliberately forwarded without enumerating them here.
    def __getattr__(self, name: str) -> Any:
        return getattr(self.current, name)

    def _save(self) -> None:
        self._pending_file.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self._pending_file, self.pending)

    def pause(self) -> None:
        self._paused = True

    def unpause(self) -> None:
        # Treat the refusal now on screen as handled, so a redraw of it is not a new pause.
        self._resumed_hit = getattr(self.current, "limit_hit_at", None)
        self._paused = False
        self._drained = []

    def restore_drained(self) -> None:
        # A resume the engine refused never delivered what it drained; put it back ahead of anything
        # queued since. Clearing `_drained` makes a second call a no-op.
        if self._drained:
            self.pending[:0] = self._drained
            self._drained = []
            self._save()

    async def submit(self, text: str, *, label: str = "") -> None:
        async with self._lock:
            hit = getattr(self.current, "limit_hit_at", None)
            if self._paused or (hit is not None and hit != self._resumed_hit):
                self.pause()
                self.pending.append(text)
                self._save()
            else:
                await self.current.submit(text, label=label)

    async def replace(self, current: AuthorProcess, handoff: str) -> None:
        async with self._lock:
            self._switching = True
            try:
                await self.current.stop()
                self.current = current
                self._resumed_hit = None
                self._drained = []
                messages = self._join_pending()
                await current.submit("\n\n".join([handoff, messages]))
                self.pending.clear()
                self._save()
                self._paused = False
            finally:
                self._switching = False

    def _join_pending(self) -> str:
        messages = "\n\n".join(self.pending)
        if len(messages) > 16000:
            # Keep the first turn bounded without dropping a queued direction or finding.
            with tempfile.NamedTemporaryFile(
                mode="w",
                prefix="handoff-messages-",
                suffix=".md",
                dir=self._pending_file.parent,
                delete=False,
            ) as artifact:
                artifact.write(messages)
            messages = f"Queued messages (read the full file): {artifact.name}\n\n{messages[:16000]}"
        return messages

    async def resume(self, text: str, *, label: str = "") -> None:
        async with self._lock:
            self._resumed_hit = getattr(self.current, "limit_hit_at", None)
            await self.current.submit("\n\n".join([text, self._join_pending()]), label=label)
            self._drained = list(self.pending)
            self.pending.clear()
            self._save()
            self._paused = False

    async def stop(self, **kwargs: float) -> None:
        await self.current.stop(**kwargs)

    async def wait_for_exit(self) -> int:
        # Codex is normally idle between turns. Both process types expose a returncode only
        # when the supervisor should end the project, so polling also survives a replacement.
        while True:
            if not self._switching and self.current.returncode is not None:
                return self.current.returncode
            await asyncio.sleep(0.1)
