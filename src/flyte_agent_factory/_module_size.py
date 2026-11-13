"""The module-size ratchet behind the `factory:module-size` gate check."""

from __future__ import annotations

import os
from collections.abc import Callable, Container
from pathlib import Path
from typing import TYPE_CHECKING

from . import _gate_base, _vendored

if TYPE_CHECKING:
    from . import _gate_tests

#: The most lines a source module may have; one already over it may not grow.
MODULE_LINE_LIMIT = 1500

# Dependency, harness and VCS trees, plus `migrations`, which the ratchet exempts from the limit. This is
# `_gate_base._SKIP_DIRS` with `migrations` added; the gate-wide list must not skip migrations, whose files
# other checks still read.
_SKIP_DIRS = {".git", ".claude-flyte", "node_modules", ".venv", "migrations"}


def _sizes(
    root: Path,
    is_test: Callable[[str], bool],
    upstreams: _vendored.Upstreams | None = None,
    started: Container[str] = (),
) -> dict[str, int]:
    """Physical line counts of the Python source modules under `root`, without tests and vendored trees.

    A module that is identical to its upstream's (a vendored copy or a nested checkout) is skipped: upstream's
    own files are not the project's to shrink. One in `started` (the start tree's modules) is not: the project
    had it, so it is judged against what it had, whatever upstream commit it matches.
    """
    sizes: dict[str, int] = {}
    for directory, children, names in os.walk(root):
        children[:] = [n for n in children if n not in _SKIP_DIRS]
        for name in names:
            relative = str((Path(directory) / name).relative_to(root))
            if (
                name.endswith(".py")
                and not is_test(relative)
                and not (upstreams and relative not in started and upstreams.unchanged(relative))
            ):
                with (root / relative).open("rb") as handle:
                    sizes[relative] = sum(1 for _ in handle)
    return sizes


def _moved_from(relative: str, before: dict[str, int]) -> str | None:
    """Return the start-tree path `relative` was moved from: the one path differing in exactly one directory.

    A package rename (`src/old/x.py` -> `src/new/x.py`) moves every module without changing it; judging the
    moved file as new would fail every rename of a package that holds an over-limit module.
    """
    parts = Path(relative).parts
    matches = [
        old
        for old in before
        if len(Path(old).parts) == len(parts)
        and Path(old).name == parts[-1]
        and sum(a != b for a, b in zip(Path(old).parts, parts, strict=True)) == 1
    ]
    return matches[0] if len(matches) == 1 else None


def check(
    project: Path,
    start: Path | None,
    is_test: Callable[[str], bool],
    work: _gate_tests.GateWork | None = None,
) -> tuple[int, str]:
    """Return an exit code and report: fail a module over the limit that is new or longer than at `start`.

    `start` is the project's starting tree. A module over the limit with no more lines than it had there
    passes, listed so the debt stays visible. Without a start tree every module over the limit fails.
    `work` is the gate run's shared state; its `trusted_base` is where `_vendored.load` reads upstream from.
    """
    before = _sizes(start, is_test) if start is not None else {}
    upstreams = _vendored.load(project, _gate_base._SKIP_DIRS, trusted_base=work.trusted_base if work else "")
    problems: list[str] = []
    debt: list[str] = []
    for relative, lines in sorted(_sizes(project, is_test, upstreams, before).items()):
        if lines <= MODULE_LINE_LIMIT:
            continue
        was = before.get(relative)
        if was is None:
            # The start tree has no copy of an upstream tree (a nested checkout is the target's, a vendored
            # copy is another repository's): a file the project changed there is judged against upstream's.
            was = upstreams.count_upstream_lines(relative)
        if was is None:
            moved = _moved_from(relative, before)
            was = before[moved] if moved is not None else None
        if was is None or lines > was:
            since = f", was {was}" if was is not None else "" if start is None else ", new file"
            problems.append(f"{relative}: {lines} lines{since}; keep it under {MODULE_LINE_LIMIT}")
        else:
            debt.append(f"over the limit but not grown: {relative} ({lines} lines, was {was})")
    report = "\n".join(problems) if problems else "\n".join(debt) or "no module over the size limit"
    return (1 if problems else 0), "\n".join(filter(None, [report, upstreams.describe()]))
