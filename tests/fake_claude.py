"""A scripted stand-in for the `claude` CLI, for rehearsing a session offline.

It is run under a real pty by the real `ClaudeProcess`, prints the line Remote
Control prints, reads whatever the supervisor types, and reacts per a script.
It is not a model: it does exactly what the script says, which is the point --
the supervisor's behaviour (nudges, the DONE gate, the review loop, what the
session is told) can then be exercised end to end in seconds, where the real
thing needs a cluster run, a wait, and `flyte get logs`.

Script (JSON, path in $FAKE_CLAUDE_SCRIPT): a list of
    {"when": "<regex matched against a received line>", "run": ["<sh command>", ...]}
Each step fires at most once, in any order. Commands run with the supervisor's
environment, so `flyte-note` is the real wrapper. Every received line is
appended to $FAKE_CLAUDE_LOG so tests can assert on what the session was told.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SESSION_URL = "https://claude.ai/code/session_FAKE0123456789 "


def main() -> int:
    with open(os.environ["FAKE_CLAUDE_SCRIPT"]) as fh:
        steps = json.load(fh)
    log = os.environ["FAKE_CLAUDE_LOG"]
    print(SESSION_URL, flush=True)
    fired: set[int] = set()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "/exit":
            return 0
        pointer = re.fullmatch(
            r"New supervisor message\. The complete message is in (\.claude-flyte/inbox/message-\d+\.md); "
            r"read the whole file and act on it\.",
            line,
        )
        if pointer:
            # The scripted session follows the same file instruction as the real session.
            line = Path(pointer.group(1)).read_text()
        with open(log, "a") as fh:
            fh.write(line + "\n")
        for i, step in enumerate(steps):
            if i in fired or not re.search(step["when"], line):
                continue
            fired.add(i)
            for command in step["run"]:
                subprocess.run(command, shell=True, check=False)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
