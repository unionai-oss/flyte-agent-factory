"""The start tree a project is judged against, and the finding comparison against its results."""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from . import _gate_base, _secret_env


class _GateError(Exception):
    """The start tree could not be built; the message says why."""


def _git_bytes(args: list[str], cwd: Path) -> bytes:
    result = subprocess.run(
        _secret_env.git_argv(*args),
        cwd=cwd,
        env=_secret_env.child_env(),
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        raise _GateError(f"git {args[0]} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


@contextlib.contextmanager
def _start_tree(workspace: Path, base: str, commits: list[str] | None) -> Iterator[Path]:
    """Where the project started: HEAD with the project's own commits reverse-applied.

    The project is judged by its net change. Judging each commit against its parent blocked forever
    on a skip that was added and later removed, and pushed history cannot be rewritten. Without
    `commits` (no attribution available) the start is simply `base`.
    """
    archive = _git_bytes(["archive", "HEAD" if commits is not None else base], workspace)
    with tempfile.TemporaryDirectory(prefix="factory-start-") as directory:
        checkout = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(checkout, filter="data")
        # Newest first, so each patch meets the lines it produced. Upstream code stays identical
        # on both sides, even after the mandated rebase and push.
        for commit in commits or []:
            patch = _git_bytes(
                ["show", *_secret_env.GIT_NO_DRIVERS, "--format=", "--binary", "--no-renames", commit],
                workspace,
            )
            if not patch:
                continue
            applied = subprocess.run(
                _secret_env.git_argv("apply", "--reverse", "--binary"),
                cwd=checkout,
                env=_secret_env.child_env(),
                input=patch,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if applied.returncode:
                raise _GateError("cannot reverse project changes: " + applied.stderr.decode(errors="replace"))
        yield checkout


#: ruff's concise format: `path:line:col: CODE message`.
_RUFF_CONCISE = re.compile(r"^(\S+?):\d+:\d+: ([A-Z]+\d+) (?:\[\*\] )?(.*)$")


#: ruff's full format: a `CODE message` header, then ` --> path:line:col` on the next line.
_RUFF_HEADER = re.compile(r"^([A-Z]+\d+) (?:\[\*\] )?(.*)$")


_RUFF_ARROW = re.compile(r"^\s*--> (\S+?):\d+:\d+$")


#: mypy: `path:line[:col]: error: message  [code]`; its `note:` lines do not match.
_MYPY = re.compile(r"^(\S+?):\d+(?::\d+)?: error: (.*?)(?:  \[([\w-]+)\])?$")


#: A pytest node id. It holds no whitespace outside its parameter brackets, and the bracket part, which may
#: hold spaces and " - " (`test_sign[utf-8 - bom]`), runs to the first `]` that " - " or the end of the line
#: follows. The one grammar for every reader of pytest's summary (`_target_run` too).
PYTEST_ID = r"[^\s\[]+(?:\[.*?\])?"
#: A row of pytest's short test summary, which names every failing node id: the kind, then the id and its
#: message. A log line (`ERROR    asyncio:...`) pads its level with spaces and is not one.
_PYTEST_ROW = re.compile(r"^(FAILED|ERROR) (\S.*)$")
_PYTEST_SPLIT = re.compile(rf"({PYTEST_ID})((?: - .*)?)")
#: Another place a bracketed id could end: a `]` that " - " or the end of the row follows.
_ID_END = re.compile(r"\](?: - |$)")


def parse_pytest_row(line: str) -> tuple[str, str] | None:
    """Return a pytest summary row's kind and key, or None when the line is not such a row.

    The key is the node id when the row shows where it ends: no whitespace outside its brackets, a test file
    or a test in one (`.py`, `::`), and no later `]` the message could start after instead. Otherwise it is
    the whole row after the kind (`test_x[a] - b]`, a path with a space), which equals only the same row: a
    test that cannot be told apart from another is never taken for it.
    """
    if not (row := _PYTEST_ROW.match(line)):
        return None
    kind, rest = row[1], row[2]
    if split := _PYTEST_SPLIT.fullmatch(rest):
        test, message = split[1], split[2]
        name = test.split("[", 1)[0]
        if ("::" in name or name.endswith(".py")) and not ("[" in test and _ID_END.search(message)):
            return kind, test
    return kind, rest


#: A tool that did not get to judge the tree: pytest's usage, collection and "nothing ran" outcomes.
_NOT_A_VERDICT = ("Interrupted:", "error during collection", "no tests ran")


#: The exit code ruff, mypy and pytest use for "ran fine and found problems".
_FINDINGS_EXIT = 1


def finding_keys(output: str) -> Counter[tuple[str, str, str]]:
    """Return the findings in a tool's output as a multiset of (path, code, message) keys.

    Line numbers are not part of a key, because they shift when unrelated code moves. tsc's
    `file(1,7): error` format is not parsed, so an output with no recognised finding gives an empty
    multiset, which the caller treats as "cannot tell".
    """
    keys: Counter[tuple[str, str, str]] = Counter()
    header = None
    for line in output.splitlines():
        if match := _RUFF_CONCISE.match(line):
            keys[match[1], match[2], match[3]] += 1
        elif (arrow := _RUFF_ARROW.match(line)) and header:
            keys[arrow[1], header[1], header[2]] += 1
        elif match := _MYPY.match(line):
            keys[match[1], match[3] or "", match[2]] += 1
        elif found := parse_pytest_row(line):
            keys[found[1], found[0], ""] += 1
        header = _RUFF_HEADER.match(line)
    return keys


def _judge_against_start(code: int, output: str, start_code: int, start_output: str) -> tuple[int, str]:
    """Judge a failed tool run against the same run on the project's starting tree.

    Debt the start already had is not the project's to fix, but it must not grow: the run fails if any
    finding key occurs more often than it did on the start. It also fails when the run is not an ordinary
    findings run (a timeout, a crash, pytest's exit 2-5), or when no finding is recognised in the run
    (nothing to compare), so a timeout on both sides is never "pre-existing". Pass the tool's full
    output, which a truncated tail would make look flat; the verdict line is kept in front of the
    recorded tail.
    """
    tail = output[-_gate_base._OUTPUT_TAIL :]
    if code != _FINDINGS_EXIT or any(marker in output for marker in _NOT_A_VERDICT):
        return code, tail
    if start_code == 0:
        return code, f"new failure: the starting tree passes\n{tail}"
    before, now = finding_keys(start_output), finding_keys(output)
    if not now:
        return code, tail
    new = now - before
    if new:
        shown = "\n".join(f"  {path} {kind} {message}".rstrip() for path, kind, message in list(new)[:20])
        return code, f"regression: {new.total()} finding(s) not on the starting tree:\n{shown}\n{tail}"
    return (
        0,
        f"pre-existing (the starting tree fails too: {before.total()} findings, now {now.total()})\n{tail}",
    )
