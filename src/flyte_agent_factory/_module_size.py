"""The module-size ratchet behind the `factory:module-size` gate check."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from . import _target_run

#: The most lines a source module may have; one already over it may not grow.
MODULE_LINE_LIMIT = 1500

# Dependency, harness and VCS trees, plus `migrations`, which the ratchet exempts from the limit. This is
# `_gate_base._SKIP_DIRS` with `migrations` added; the gate-wide list must not skip migrations, whose files
# other checks still read.
_SKIP_DIRS = {".git", ".claude-flyte", "node_modules", ".venv", "migrations"}


def _sizes(
    root: Path, is_test: Callable[[str], bool], nested: list[_target_run.Nested] | None = None
) -> dict[str, int]:
    """Physical line counts of the Python source modules under `root`, without tests and vendored trees.

    A module in a `nested` checkout counts only if the project changed it: upstream's own files are not
    the project's to shrink.
    """
    sizes: dict[str, int] = {}
    for directory, children, names in os.walk(root):
        children[:] = [n for n in children if n not in _SKIP_DIRS]
        for name in names:
            relative = str((Path(directory) / name).relative_to(root))
            if name.endswith(".py") and not is_test(relative) and not _untouched(root, relative, nested):
                with (root / relative).open("rb") as handle:
                    sizes[relative] = sum(1 for _ in handle)
    return sizes


def _in_nested(
    root: Path, relative: str, nested: list[_target_run.Nested] | None
) -> tuple[_target_run.Nested, str] | None:
    """Return the nested checkout holding `relative` (a path under `root`) and the file's path inside it."""
    for checkout in nested or []:
        if (root / relative).is_relative_to(checkout.root):
            return checkout, str((root / relative).relative_to(checkout.root))
    return None


def _untouched(root: Path, relative: str, nested: list[_target_run.Nested] | None) -> bool:
    found = _in_nested(root, relative, nested)
    return found is not None and found[0].untouched(found[1])


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


def check(project: Path, start: Path | None, is_test: Callable[[str], bool]) -> tuple[int, str]:
    """Return an exit code and report: fail a module over the limit that is new or longer than at `start`.

    `start` is the project's starting tree. A module over the limit with no more lines than it had there
    passes, listed so the debt stays visible. Without a start tree every module over the limit fails.
    """
    before = _sizes(start, is_test) if start is not None else {}
    nested = _target_run.nested_checkouts(project, _SKIP_DIRS)
    problems: list[str] = []
    debt: list[str] = []
    for relative, lines in sorted(_sizes(project, is_test, nested).items()):
        if lines <= MODULE_LINE_LIMIT:
            continue
        was = before.get(relative)
        if was is None and (found := _in_nested(project, relative, nested)):
            # The start tree has no nested checkout (it is the target's, not the project's): a file the
            # project changed there is judged against upstream's version of it.
            was = found[0].lines_at_base(found[1])
        if was is None:
            moved = _moved_from(relative, before)
            was = before[moved] if moved is not None else None
        if was is None or lines > was:
            since = f", was {was}" if was is not None else "" if start is None else ", new file"
            problems.append(f"{relative}: {lines} lines{since}; keep it under {MODULE_LINE_LIMIT}")
        else:
            debt.append(f"over the limit but not grown: {relative} ({lines} lines, was {was})")
    return (1, "\n".join(problems)) if problems else (0, "\n".join(debt) or "no module over the size limit")
