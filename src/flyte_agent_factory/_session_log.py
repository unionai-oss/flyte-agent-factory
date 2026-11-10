"""What the task log says about the session, in place of the terminal screen.

The supervisor reads Claude Code through a pty, so the raw stream is a TUI: redraw
fragments, spinner frames, cursor moves that glue words together. Written to the
task log it was thousands of one-character lines nobody could grep. Two replacements:

* `RawCapture`: the cleaned terminal text, in a size-bounded rotating file in the
  state dir, for the day the transcript is not enough and the screen is the evidence.
* `TranscriptTail`: a readable `[session]` feed built from Claude Code's own JSONL
  transcript, one line per message, tool call or tool error.

The `tail` deque and the prompt/limit detection in `ClaudeProcess` still consume the
PTY stream unchanged; this module only decides what reaches the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import re
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict, cast

from ._bridge import redact_tokens

RAW_MAX_BYTES = 5 * 1024 * 1024
#: One backup, so the capture is bounded at twice RAW_MAX_BYTES.
RAW_BACKUPS = 1
#: A redraw repeats lines already shown; remember this many recent ones to drop the repeats.
_RECENT_LINES = 400
#: Per file, per poll; a transcript that jumps further is caught up on the next poll.
_READ_CHUNK = 1024 * 1024
_TEXT_CHARS = 200
_ARG_CHARS = 120
#: Bytes from the start of a file remembered to tell a replaced file from the same one grown. Enough
#: for a whole first record: Claude Code's begin with ~70 bytes common to every transcript in a
#: folder, and only the ids after them tell two files apart.
_HEAD_BYTES = 4096


class _Block(TypedDict, total=False):
    type: object
    text: str
    id: object
    name: object
    input: object
    tool_use_id: object
    is_error: object
    content: object


class RawCapture:
    """Callable for `ClaudeProcess(on_line=...)` that files the terminal text away.

    Uses the stdlib rotating handler rather than a hand-rolled one: it already does
    the size check and the rename, and is safe to call from the event loop thread.
    """

    def __init__(self, path: Path, *, max_bytes: int = RAW_MAX_BYTES, backups: int = RAW_BACKUPS) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", errors="replace"
        )
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._in_private_key = False
        self._closed = False

    def __call__(self, line: str) -> None:
        if self._closed:
            return
        text = line.strip()
        if self._in_private_key:
            if re.search(r"-----END [A-Z ]*PRIVATE KEY-----", text):
                self._in_private_key = False
            return
        if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text):
            self._in_private_key = not bool(re.search(r"-----END [A-Z ]*PRIVATE KEY-----", text))
        # Spinner frames and box edges have no letters or digits to keep.
        if not any(c.isalnum() for c in text) or text in self._recent:
            return
        self._recent[text] = None
        if len(self._recent) > _RECENT_LINES:
            self._recent.popitem(last=False)
        self._handler.emit(logging.makeLogRecord({"msg": redact_tokens(text)}))

    def close(self) -> None:
        self._closed = True
        self._handler.close()


def _oneline(text: object, limit: int) -> str:
    flat = " ".join(redact_tokens(str(text)).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _tool_summary(name: str, args: object) -> str:
    """`Bash: git push origin HEAD`, `Edit: src/x.py`: the call, never its payload."""
    if not isinstance(args, dict):
        return name
    for key in ("command", "file_path", "path", "notebook_path", "pattern", "url", "query", "description"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {_oneline(value, _ARG_CHARS)}"
    return name


def _blocks(content: object) -> list[_Block]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    # Claude transcript text blocks carry strings; the reader checks the container shape.
    return [cast(_Block, b) for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def session_lines(entry: Mapping[str, object], tool_names: dict[str, str]) -> list[str]:
    """Return the log lines for one transcript record, redacted. `tool_names` maps tool_use ids to names."""
    if entry.get("isMeta"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    out: list[str] = []
    kind = entry.get("type")
    for block in _blocks(message.get("content")):
        btype = block.get("type")
        if kind == "user" and btype == "text":
            out.append(f"user: {_oneline(block.get('text', ''), _TEXT_CHARS)}")
        elif kind == "assistant" and btype == "text":
            out.append(f"assistant: {_oneline(block.get('text', ''), _TEXT_CHARS)}")
        elif kind == "assistant" and btype == "tool_use":
            name = str(block.get("name") or "tool")
            if block.get("id"):
                tool_names[str(block["id"])] = name
                while len(tool_names) > 500:
                    tool_names.pop(next(iter(tool_names)))
            out.append(f"tool: {_tool_summary(name, block.get('input'))}")
        elif kind == "user" and btype == "tool_result" and block.get("is_error"):
            name = tool_names.get(str(block.get("tool_use_id")), "tool")
            detail = "".join(b.get("text", "") for b in _blocks(block.get("content")))
            out.append(f"tool error ({name}): {_oneline(detail, _TEXT_CHARS)}")
    return [redact_tokens(f"[session] {line}") for line in out if line.split(": ", 1)[-1].strip()]


class TranscriptTail:
    """Follows every `*.jsonl` under a Claude Code project folder, yielding new log lines.

    Handles what the files actually do: a record still being written (no newline yet:
    left for the next poll), a file replaced (read from the start; a new inode, or the same
    inode handed out again with different leading bytes, which filesystems do after an unlink), a
    file rewritten shorter by compaction (jump to the end, because the content is a
    summary of what was already logged), and a record seen twice (by `uuid`).
    """

    def __init__(self, folder: Path, *, skip_existing: bool = False) -> None:
        self.folder = folder
        #: A resumed session's folder already holds its whole history, which was logged before.
        self._skip_existing = skip_existing
        self._first_poll = True
        self._files: dict[Path, tuple[int, int, bytes]] = {}  # path -> (inode, offset, head)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tool_names: dict[str, str] = {}
        self._skipping: dict[Path, int] = {}

    def poll(self) -> list[str]:
        out: list[str] = []
        self._skip_notices: list[str] = []
        if not self.folder.is_dir():
            return out
        for path in sorted(self.folder.rglob("*.jsonl")):
            try:
                stat = path.stat()
            except OSError:
                continue
            known = self._files.get(path)
            replaced = False
            if known is None:
                offset = stat.st_size if self._first_poll and self._skip_existing else 0
            elif known[0] != stat.st_ino or (stat.st_size >= known[1] and self._head(path, known[2])):
                replaced = True
                offset = 0
            elif stat.st_size < known[1]:
                offset = stat.st_size
            else:
                offset = known[1]
            if known is None or replaced or stat.st_size < known[1]:
                self._skipping.pop(path, None)
            offset, lines = self._read(path, offset, stat.st_size)
            self._files[path] = (stat.st_ino, offset, self._read_head(path, offset))
            for raw in lines:
                out += self._entry_lines(raw)
        self._first_poll = False
        return self._skip_notices + out

    def _read_head(self, path: Path, offset: int) -> bytes:
        try:
            with path.open("rb") as fh:
                return fh.read(min(_HEAD_BYTES, offset))
        except OSError:
            return b""

    def _head(self, path: Path, remembered: bytes) -> bool:
        """Return True when the file no longer starts with the bytes already read from it."""
        return bool(remembered) and self._read_head(path, len(remembered)) != remembered

    def _read(self, path: Path, offset: int, size: int) -> tuple[int, list[bytes]]:
        if offset >= size:
            return offset, []
        try:
            with path.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(_READ_CHUNK)
        except OSError:
            return offset, []
        end = chunk.rfind(b"\n")
        skipped = self._skipping.get(path, 0)
        if skipped or (end < 0 and len(chunk) == _READ_CHUNK):
            end = chunk.find(b"\n")
            if end < 0:
                self._skipping[path] = skipped + len(chunk)
                return offset + len(chunk), []
            self._skipping.pop(path, None)
            notice = f"[session] (record of {skipped + end + 1} bytes skipped)"
            self._skip_notices.append(notice)
            return offset + end + 1, []
        if end < 0:
            return offset, []
        return offset + end + 1, chunk[:end].split(b"\n")

    def _entry_lines(self, raw: bytes) -> list[str]:
        try:
            entry = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(entry, dict):
            return []
        uuid = entry.get("uuid")
        if uuid:
            if uuid in self._seen:
                return []
            self._seen[uuid] = None
            while len(self._seen) > 5000:
                self._seen.popitem(last=False)
        return session_lines(entry, self._tool_names)


async def feed_loop(
    tail: TranscriptTail, emit: Callable[[str], None], stop: asyncio.Event, every: float = 2.0
) -> None:
    """Poll the transcript until `stop`. Off the loop: it reads files, and the loop is shared."""
    delay = every
    while not stop.is_set():
        try:
            for line in await asyncio.to_thread(tail.poll):
                emit(line)
            delay = every
        except Exception as e:  # noqa: BLE001 -- a transient read error must not kill the feed
            traceback.print_exc()
            emit(f"[session] transcript poll failed: {redact_tokens(str(e))}")
            delay = min(max(every, delay * 2), 30.0)
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
