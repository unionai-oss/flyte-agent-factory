"""What `_bridge` and `_bridge_triage` both need: the answer files and the constants around them.

Its own module so the triage code can sit beside `Bridge` without importing it back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, TypedDict

ANSWERS = "answers"

# How long past the CLI's deadline the question stays open, so an answer that lands
# just after the asker gave up still reaches the inbox. The Flyte-level timeout is
# set to the same instant.
ASK_GRACE_S = 30

# Exit codes of `ask-human`: 3 = nobody answered, 4 = the question was withdrawn or superseded,
# or a yes/no question was answered in the session in words.
_EXIT_NO_ANSWER = 3
_EXIT_WITHDRAWN = 4

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
