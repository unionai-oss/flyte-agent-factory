"""What `_bridge` and `_bridge_triage` both need: the answer files and the constants around them.

Its own module so the triage code can sit beside `Bridge` without importing it back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypedDict

ANSWERS = "answers"

# How long past the CLI's deadline the question stays open, so an answer that lands
# just after the asker gave up still reaches the inbox. The Flyte-level timeout is
# set to the same instant.
ASK_GRACE_S = 30

# Exit codes of `ask-human`: 3 = nobody answered, 4 = the question was withdrawn or superseded,
# or a yes/no question was answered in the session in words, 5 = a yes/no question was answered
# in Union in words that are neither a yes nor a no.
_EXIT_NO_ANSWER = 3
_EXIT_WITHDRAWN = 4
_EXIT_ANSWERED_IN_WORDS = 5

_APPROVE_WORDS = frozenset({"yes", "y", "true", "approve", "approved", "ok", "lgtm"})
_DECLINE_WORDS = frozenset({"no", "n", "false", "decline", "declined", "reject"})

#: The default a question states for itself ("... Default: use sqlite"), for the dashboard.
_STATED_DEFAULT = re.compile(r"\bdefault(?:ing)?\b[^.\n]{0,160}", re.IGNORECASE)


def _write_atomic(path: Path, payload: Any) -> None:
    # Rename is atomic within a directory, so a reader never sees half a file.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(path)


def gave_up_path(answer_path: Path) -> Path:
    """Where the CLI records that it stopped waiting for `answer_path`."""
    return answer_path.with_suffix(".gaveup")


class _Close(TypedDict):
    reason: str
    outcome: dict[str, Any]
    abort_reason: str
    row: str
    answered: bool


def _withdrawn_close(why: str) -> _Close:
    """Build the close of a question that is withdrawn or superseded: its CLI is told it is no longer open."""
    reason = f"closed: {why}"
    return {
        "reason": reason,
        "outcome": {"withdrawn": reason},
        "abort_reason": "",
        "row": "",
        "answered": False,
    }


def parse_approval(value: object) -> bool | None:
    """Read an owner's answer to a yes/no question: True, False, or None when it is neither.

    Approve asks carry text, because the owner answers from chat as often as from Union and a sentence
    cannot be a bool. A leading verdict word decides ("yes, go ahead", "no -- wait"); anything that does
    not start with one ("hold off until Monday", "not yet") is words for the session to act on. "0" and
    "1" count only on their own: "1 more thing" is not a yes.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower() if value is not None else ""
    if text.strip(" .!?") in ("0", "1"):
        return text.strip(" .!?") == "1"
    found = re.match(r"\W*([a-z]+)\b", text)
    word = found.group(1) if found else ""
    if word in _APPROVE_WORDS:
        return True
    if word in _DECLINE_WORDS:
        return False
    return None


def open_replacement(
    entry: dict[str, Any] | None, questions: Iterable[dict[str, Any]], history: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the open question that replaced the ask `entry`, following replacements of replacements.

    `superseded` names the replacement by condition or by request id (`superseded by ask-a0-002`,
    `superseded by r7`); a decision (`superseded by decision-003`) is never an open question, and an
    answered or closed replacement is followed only through its own `superseded`. None when nothing open
    replaced it.
    """
    opened = [q for q in questions if q["condition"]]
    seen: set[str] = set()
    while entry is not None and entry.get("superseded"):
        successor = str(entry["superseded"]).removeprefix("superseded by ").strip()
        if not successor or successor in seen:
            return None
        seen.add(successor)
        live = next((q for q in opened if successor in (q["id"], q["condition"])), None)
        if live is not None:
            return live
        entry = next((e for e in reversed(history) if successor in (e.get("id"), e.get("condition"))), None)
    return None
