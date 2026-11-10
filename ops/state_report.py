"""What the retired `[state]` outboxes still hold, for the record before state step 1 deploys. Read-only.

Step 1 stops reading and writing `board/` and `github/outbox*` under `[state] uri`: board fields and
issue milestones are recomputed from Flyte runs by `factory-reconcile`. Nothing here is migrated.

- `board/outbox` records are field writes and bootstrap requests. Every one is superseded: the
  reconciler bootstraps a board missing fields and writes each item's current values. They are
  counted by operation and key, so hundreds of duplicate pending bootstrap records read as one line.
- `github/outbox` records are issue comments. Started, QA verdict, PR opened and ended comments are
  re-derived from the run's report (an old "Released/ended" one as the run's `ended` comment) while
  the run is inside the reconciler's one-day window, which a comment queued in the last day stands in
  for here. The rest (escalations, closed-PR reports, older comments) are listed in full, so a human
  can post one by hand if it matters.

Everything is left in place. The prefix is emptied by the owner after state step 4.

    PYTHONPATH=src uv run python ops/state_report.py [--profile NAME]
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from typing import Any

import flyte

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory, _factory_base, _queue  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first

#: Comment bodies the reconciler re-derives from a run's report (`_issues.derive`), by their first words.
DERIVABLE = ("Started: ", "QA round ", "PR opened: ", "Released/ended: ")


def _read(path: str) -> dict[str, Any] | None:
    try:
        record = json.loads(_queue._fs(path).cat_file(path))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def report(uri: str, now: float) -> list[str]:
    """Return report lines counting the dead, done and pending records under the state URI `uri`."""
    root = uri.rstrip("/")
    lines = []
    for name in ("board/dead", "board/done", "github/outbox-dead"):
        lines.append(f"{name}: {len(_queue.listing(f'{root}/{name}'))} record(s)")
    # Every pending record repeats its key, so a duplicate is the same op and key.
    board = [_read(path) or {} for path in _queue.listing(f"{root}/board/outbox")]
    ops = Counter((record.get("op", "unreadable"), record.get("key", "")) for record in board)
    lines.append(f"board/outbox: {len(board)} pending record(s), {len(ops)} distinct")
    for (op, key), count in ops.most_common():
        lines.append(f"  {count} x {op} {key[:12]}")
    pending = _queue.listing(f"{root}/github/outbox")
    lines.append(f"github/outbox: {len(pending)} pending comment(s)")
    for path in pending:
        item = _read(path)
        if item is None:
            lines.append(f"  unreadable: {path}")
            continue
        body = str(item.get("body", ""))
        age = (now - float(item.get("created", now))) / 3600
        heals = body.startswith(DERIVABLE) and age < 24
        lines.append(
            f"  {item.get('issue', '?')} {age:.0f}h old, {'re-derived' if heals else 'NOT re-derived'}: "
            + (body.splitlines()[0][:200] if body else "(empty)")
        )
        if not heals:
            lines.append("    " + body.replace("\n", "\n    "))
    return lines


def main() -> int:
    """Print the state report for the profile's `[state] uri`, if it has one."""
    uri = _factory_base.PROFILE.state.uri
    if not uri:
        print("this profile has no [state] uri: there is nothing to report")
        return 0
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"], project=_factory.PROJECT, domain=_factory.DOMAIN
    )
    print("\n".join(report(uri, time.time())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
