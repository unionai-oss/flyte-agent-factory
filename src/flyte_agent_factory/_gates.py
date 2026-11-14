"""Deterministic project checks before a review round is spent."""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime
import io
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tokenize
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from . import (
    _delivery,
    _evidence,
    _gate_base,
    _gate_rules,
    _gate_scope,
    _gate_start,
    _gate_tests,
    _module_size,
    _secret_env,
    _standards,
    _target_ci,
    _target_run,
    _waivers,
)

_SOURCE_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".sh",
    ".bash",
    ".sql",
    ".vue",
    ".svelte",
    ".html",
    ".css",
    ".scss",
    ".tf",
    ".hcl",
}


def is_git_repo(workspace: Path) -> bool:
    """Return True when `workspace` is itself a repository, not a directory inside some other one."""
    code, top = _gate_base._run(["git", "rev-parse", "--show-toplevel"], workspace)
    return not code and Path(top.strip()).resolve() == workspace.resolve()


def capture_base(workspace: Path, name: str) -> None:
    """Persist the starting commit once, at project creation, in the checkpointed workspace."""
    path = workspace / ".claude-flyte" / name / "base-commit"
    if path.exists():
        return
    code, head = _gate_base._run(["git", "rev-parse", "HEAD"], workspace)
    if not code:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head.strip())
        _CAPTURED_BASES[workspace.resolve(), name] = head.strip()


#: The base `capture_base` wrote in this process, by (workspace, project). The file is the session's to
#: write, so waivers are read from this copy when it exists (see `_trusted_base`).
_CAPTURED_BASES: dict[tuple[Path, str], str] = {}


def _trusted_base(workspace: Path, name: str) -> str:
    """Return the base commit waivers are read from when there is no repository URL."""
    return _CAPTURED_BASES.get((workspace.resolve(), name)) or resolve_base(workspace, name)


def resolve_base(workspace: Path, name: str) -> str:
    """Keep the creation base; a pushed project's merge-base can be HEAD itself."""
    stored = workspace / ".claude-flyte" / name / "base-commit"
    if stored.exists():
        return stored.read_text().strip()
    code, roots = _gate_base._run(["git", "rev-list", "--max-parents=0", "HEAD"], workspace)
    return roots.splitlines()[0] if not code and roots.strip() else ""


#: New commits use this domain; attribution accepts historical identities under the reserved .localhost TLD.
COMMIT_DOMAIN = "agent-factory.localhost"


def commit_identity(name: str) -> str:
    """Return the author email a project's commits carry."""
    return f"{name}@{COMMIT_DOMAIN}"


def _basic_regex_escape(text: str) -> str:
    """Escape `text` for git's default (POSIX basic) regular expressions, so it matches only itself."""
    return re.sub(r"([\\.\[\]*^$])", r"\\\1", text)


def _project_commits(workspace: Path, name: str, base: str) -> tuple[int, str]:
    # Old shared-identity commits are not attributed (no migration): they include other projects' work.
    return _gate_base._run(
        [
            "git",
            "rev-list",
            "--no-merges",
            "--topo-order",
            f"--author=<{_basic_regex_escape(name)}@[^<>@]*\\.localhost>",
            f"{base}..HEAD",
        ],
        workspace,
    )


_changed_paths = _gate_start.list_changed_paths  # the commit-path lister lives with the start tree


#: Files that decide what a pytest run reports. The session can write them, so a junit `checks.sh` left is
#: evidence only when the round changed none of them; otherwise the gate runs the tests itself.
#: `_slow_lane.py` is what checks.sh runs to make slow.xml; the package's `__init__.py`, `child.py` and
#: `_names.py` load before it writes it. Python imports `sitecustomize`, `usercustomize` and `*.pth` files at
#: start-up, a `pytest`, `_pytest` or `py` module anywhere on the path shadows the real one, and `uv.lock`
#: picks what is installed. `tests/test_gate_speed.py` checks the package modules against the launcher.
_TEST_HARNESS = re.compile(
    r"(^|/)(checks\.sh|conftest\.py|pyproject\.toml|pytest\.ini|setup\.cfg|tox\.ini|uv\.lock"
    r"|sitecustomize\.py|usercustomize\.py|pytest\.py|py\.py|_slow_lane\.py)$"
    r"|(^|/)_pytest/|(^|/)(pytest|py)/__init__\.py$|\.pth$"
    r"|(^|/)src/flyte_agent_factory/(__init__|child|_names)\.py$"
    r"|(^|/)ops/package_skill\.py$"  # checks.sh runs its --check on the skill zips
)


def changed_test_harness(workspace: Path, name: str, base: str) -> list[str]:
    """Return the test-harness files the project changed since `base`, committed or not ("?" when unknown)."""
    code, commits = _project_commits(workspace, name, base)
    if code:
        return ["?"]
    code, paths = _changed_paths(workspace, commits.split())
    status_code, status = _gate_base._run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"], workspace
    )
    if code or status_code:
        return ["?"]
    paths = [*paths, *(entry[3:] for entry in status.split("\0") if len(entry) > 3)]
    return sorted({path for path in paths if _TEST_HARNESS.search(path)})


#: What the harness writes at a project's workspace root, as anchored patterns for the clone's local
#: `.git/info/exclude`; every gate and the exclusion share it. `DESIGN.md` is deliberately absent: it is the
#: project's house rules, edited and committed.
HARNESS_PATHS = (
    "/.claude-flyte/",
    "/.claude/",
    "/.flyte/",
    "/.mcp.json",
    "/.voice-model/",
    "/CLAUDE.md",
    "/BRIEF.md",
)
MEMORY_BEGIN = "<!-- agent-factory: begin -->"
MEMORY_END = "<!-- agent-factory: end -->"
#: A snapshot from before this change holds its block between these; resume replaces it, nothing writes them.
PREVIOUS_MEMORY_BEGIN = "<!-- claude-on-flyte: begin -->"
PREVIOUS_MEMORY_END = "<!-- claude-on-flyte: end -->"
#: The prefix the supervisor records Union direction under; the only direction that grants scope.
UNION_DIRECTION = "Union: "
#: Root files the harness only adds a managed block to; the rest is the project's own text.
_MANAGED_BLOCK_FILES = {"CLAUDE.md"}


def apply_managed_block(existing: str, body: str) -> str:
    """`existing` with the harness's managed block set to `body`, exactly as the harness writes it."""
    block = f"{MEMORY_BEGIN}\n{body.strip()}\n{MEMORY_END}"
    for begin, end in ((MEMORY_BEGIN, MEMORY_END), (PREVIOUS_MEMORY_BEGIN, PREVIOUS_MEMORY_END)):
        if begin in existing and end in existing:
            before, _, rest = existing.partition(begin)
            _, _, after = rest.partition(end)
            return f"{before}{block}{after}"
    sep = "\n\n" if existing.strip() else ""
    return f"{existing}{sep}{block}\n"


def _universal_newlines(text: str) -> str:
    """Normalise newlines as `Path.read_text` hands them to the harness writer: CRLF and CR read as LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _harness_only(path: str, current: bytes | None, original: bytes | None) -> bool:
    """Return True when the difference between `original` and `current` is only the harness's.

    Neither the scope gate nor the commit gate may ask for or reject such a change: the harness
    rewrites the file after the project commits, so one gate demanding a commit and the other
    rejecting it would never both pass. For a managed-block file the harness's write is replayed on
    `original`: only when that reproduces `current` exactly is the change the harness's, so any edit
    the project made -- even whitespace -- still counts.
    """
    if path in _gate_scope._HARNESS_FILES or PurePosixPath(path).parts[0] in {".claude", ".claude-flyte"}:
        return True
    if path not in _MANAGED_BLOCK_FILES or current is None:
        return False
    now = current.decode(errors="replace")
    before = _universal_newlines(original.decode(errors="replace")) if original is not None else ""
    begins = [i for i in range(len(now)) if now.startswith(MEMORY_BEGIN, i)]
    ends = [j for j in range(len(now)) if now.startswith(MEMORY_END, j)]
    return any(
        apply_managed_block(before, now[i + len(MEMORY_BEGIN) + 1 : j - 1]) == now
        for i in begins
        for j in ends
        if j > i + len(MEMORY_BEGIN)
    )


def _owner_direction(directions: Sequence[str]) -> str:
    """Direction the owner sent after the brief, so a `May edit:` line in it grants scope like the brief's.

    Only Union direction ("Union: <text>", signalled through a condition the supervisor itself waits
    on) counts. The workspace inbox and the session transcript, where Claude-app direction is read
    from, are both writable by the session, and a grant it could write itself would be no gate.
    """
    return "".join(
        "\n" + d.removeprefix(UNION_DIRECTION) for d in directions if d.startswith(UNION_DIRECTION)
    )


def _net_changes(workspace: Path, start: Path, paths: list[str]) -> list[str]:
    """Paths whose content differs between the start tree and HEAD; a change that was undone is none."""

    def content(root: Path, path: str) -> bytes | None:
        return (root / path).read_bytes() if (root / path).is_file() else None

    return [
        p
        for p in paths
        if content(start, p) != content(workspace, p)
        and not _harness_only(p, content(workspace, p), content(start, p))
    ]


def untracked_checks(workspace: Path, name: str, layout: str) -> bool:
    """Return a root-layout `checks.sh` that exists but was never `git add -f`ed.

    The harness directory is git-ignored, so `git status` cannot see it; it is the one file in
    there that is the project's work rather than harness state.
    """
    script = _gate_base.checks_rel(name, layout)
    if layout != "root" or not (workspace / script).is_file():
        return False
    code, _ = _gate_base._run(["git", "ls-files", "--error-unmatch", "--", script], workspace)
    return bool(code)


def _uncommitted(workspace: Path, name: str, layout: str = "sandbox") -> list[str]:
    """Tracked changes anywhere, plus untracked files in the project: work that is in no commit."""
    root = layout == "root"
    paths: list[str] = []
    # `standards/` and `waivers.toml` are watched in the sandbox layout too: a new file there is a violation.
    scopes = (
        ["--untracked-files=no"],
        [
            "--untracked-files=all",
            "--",
            *(["."] if root else [_gate_base.project_rel(name, layout), "standards", _waivers.FILE]),
        ],
    )
    for scope in scopes:
        code, output = _gate_base._run(
            ["git", "status", "--porcelain", "--no-renames", "-z", *scope], workspace
        )
        if not code:
            # Entries are "XY path"; the harness's own state is not the project's work.
            paths += [e[3:] for e in output.split("\0") if len(e) > 3]
    script = _gate_base.checks_rel(name, layout)
    if untracked_checks(workspace, name, layout):
        paths.append(script)
    return [
        p
        for p in dict.fromkeys(paths)
        if (p == script and root) or not _harness_only(p, *_head_and_worktree(workspace, p))
    ]


def _head_and_worktree(workspace: Path, path: str) -> tuple[bytes | None, bytes | None]:
    """Return the file as it is on disk, then as HEAD has it (None when absent)."""
    full = workspace / path
    current = full.read_bytes() if full.is_file() else None
    code, _ = _gate_base._run(["git", "cat-file", "-e", f"HEAD:{path}"], workspace)
    original = (
        _gate_start._git_bytes(["show", *_secret_env.GIT_NO_DRIVERS, f"HEAD:{path}"], workspace)
        if not code
        else None
    )
    return current, original


_UI_SUFFIXES = {".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"}


def _has_ui(project: Path) -> bool:
    return any(Path(p).suffix in _UI_SUFFIXES for p in _gate_base._project_files(project))


def _judge_project(
    workspace: Path,
    name: str,
    base: str,
    brief: str,
    demo: str,
    note: str,
    uncommitted: list[str],
    record: Callable[[str, _gate_base.Status, str], None],
    layout: str = "sandbox",
    work: _gate_tests.GateWork | None = None,
    fail_to_pass_rules: Sequence[str] = (),
) -> None:
    """Record attribution, scope and fail-to-pass for the project's net change since `base`.

    `factory:fail-to-pass` is recorded only when `fail_to_pass_rules`, the IDs of the rules that map it, is
    not empty; it names them in its output.
    """
    tested = f"[{', '.join(fail_to_pass_rules)}] "

    def record_fault(reason: str) -> None:
        record("scope", _gate_base.Status.HARNESS_FAULT, _gate_start.describe_harness_fault(reason))
        if fail_to_pass_rules:
            record(
                _standards.FAIL_TO_PASS,
                _gate_base.Status.HARNESS_FAULT,
                tested + _gate_start.describe_harness_fault(reason),
            )

    code, output = _project_commits(workspace, name, base)
    if code:
        record_fault(f"git rev-list failed: {output}")
        return
    commits = output.splitlines()
    identity = commit_identity(name)
    if commits:
        record("attribution", _gate_base.Status.PASS, f"{len(commits)} commit(s) attributed to {name}")
    else:
        code, changed = _gate_base._run(
            [
                "git",
                "diff",
                *_secret_env.GIT_NO_DRIVERS,
                "--name-only",
                base,
                "HEAD",
                "--",
                _gate_base.project_rel(name, layout),
            ],
            workspace,
        )
        if not (workspace / ".claude-flyte" / name / "base-commit").exists():
            # The base is a guess and legacy commits share an identity: "nothing attributed" proves nothing.
            reason = f"no commits carry {identity}, so the project's changes cannot be told apart"
            for rule in ("attribution", "scope", *([_standards.FAIL_TO_PASS] if fail_to_pass_rules else [])):
                record(
                    rule,
                    _gate_base.Status.UNKNOWN,
                    (tested if rule == _standards.FAIL_TO_PASS else "") + reason,
                )
            return
        if code or changed.strip() or uncommitted:
            record(
                "attribution",
                _gate_base.Status.FAIL,
                f"no commits attributed to project {name}: commit as {identity} "
                "(GIT_AUTHOR_EMAIL is set for you; do not override --author)",
            )
            return
        record("attribution", _gate_base.Status.NOT_APPLICABLE, "no project changes yet")
    try:
        with _gate_start._start_tree(workspace, base, commits) as built:
            start = built.path
            code, touched = _changed_paths(workspace, commits)
            if code:
                raise _gate_start._GateError(f"git diff-tree failed: {''.join(touched)}")
            touched = _gate_start.add_merge_paths(workspace, built, touched)
            changes = _net_changes(workspace, start, touched)
            changes += _gate_start.find_authored_paths(
                workspace, built, base, commits, sorted(set(touched) - set(changes))
            )
            code, directories = _gate_base._run(
                ["git", "ls-tree", "-d", "--name-only", "-z", base], workspace
            )
            if code:
                raise _gate_start._GateError(f"git ls-tree failed: {directories}")
            owned = set(directories.split("\0"))
            code, history = _gate_base._run(["git", "rev-list", "--no-merges", f"{base}..HEAD"], workspace)
            if code:
                raise _gate_start._GateError(f"git rev-list failed: {history}")
            code, upstream_paths = _changed_paths(workspace, sorted(set(history.splitlines()) - set(commits)))
            if code:
                raise _gate_start._GateError(f"git diff-tree failed: {''.join(upstream_paths)}")
            owned.update(PurePosixPath(p).parts[0] for p in upstream_paths if "/" in p)
            state = workspace / ".claude-flyte"
            if state.is_dir():
                owned.update(p.name for p in state.iterdir() if p.is_dir())
            violations = _gate_scope.scope_violations(changes, name, brief, owned, layout)
            flagged = _gate_scope.scope_flagged(changes, name, brief, owned, layout)
            outside = (
                "outside the brief's May edit: paths"
                if layout in {"root", "workspace"}
                else "outside project/brief"
            )
            record(
                "scope",
                _gate_base.Status.FAIL if violations else _gate_base.Status.PASS,
                "; ".join(
                    ([f"{outside}: {', '.join(violations)}"] if violations else [])
                    + (
                        [_gate_scope.STANDARDS_MESSAGE]
                        if any(PurePosixPath(p).parts[0] == _gate_scope.STANDARDS_DIR for p in violations)
                        else []
                    )
                    + ([f"flagged, not blocking: {', '.join(flagged)}"] if flagged else [])
                )
                or "within the project",
            )
            if not fail_to_pass_rules:
                return
            result, output = _gate_tests.fail_to_pass(
                workspace, name, base, changes, brief, demo, note, commits, start, layout, work
            )
            record(
                _standards.FAIL_TO_PASS,
                result if isinstance(result, _gate_base.Status) else _gate_base.Status.from_exit(result),
                tested + output,
            )
    except _gate_start._GateError as exc:
        record_fault(str(exc))


@dataclasses.dataclass(frozen=True)
class Result:
    """One rule's outcome, before `run` records it."""

    rule: str
    status: _gate_base.Status
    output: str


def required_tools(files: dict[str, str]) -> dict[str, list[str]]:
    """Return the tools required by the already selected standards and their rule IDs.

    A `command` or `lint` rule needs the tool each of its commands starts with; a `script` rule needs bash
    for a `.sh` script. A `.py` script runs on the factory's own Python, and `forbid` needs nothing.
    """
    tools: dict[str, list[str]] = {}
    for rule in _standards.rules(files):
        if not rule.must or rule.error:
            continue
        if rule.kind in {"command", "lint"}:
            names = []
            for command in _gate_rules._commands(rule):
                names.append(command[2] if command[:2] == ["python", "-m"] else command[0])
        elif rule.kind == "script" and _standards.split_commands(rule.arg)[0][0].endswith(".sh"):
            names = ["bash"]
        else:
            continue
        for tool in names:
            ids = tools.setdefault(tool, [])
            if rule.ident not in ids:
                ids.append(rule.ident)
    return tools


def image_defect(tool: str, rules: list[str]) -> str:
    """Use the same diagnosis at startup and in the gate report."""
    return f"image defect: {tool} missing; required by {', '.join(rules)}"


_BROAD_EXCEPTIONS = {"Exception", "BaseException", "builtins.Exception", "builtins.BaseException"}


def _suppression_codes(files: dict[str, str]) -> set[str]:
    """Return the ruff codes MUST rules select (`lint` rules, and a `--select` in a `command` rule)."""
    return {code for rule in _standards.rules(files) for code in rule.lint_codes}


def _hands_to_future(handler: ast.ExceptHandler) -> bool:
    """Whether the handler passes the exception it caught to a future's `set_exception`.

    That re-raises it wherever the future is awaited, as `concurrent.futures` does for a pool's work items:
    the exception is handed on, not swallowed. A lambda parameter defaulted to it (`lambda err=e: ...`)
    counts.
    """
    if not handler.name:
        return False
    names = {handler.name}
    for node in ast.walk(handler):
        if isinstance(node, ast.Lambda):
            positional = node.args.args[len(node.args.args) - len(node.args.defaults) :]
            names.update(
                arg.arg
                for arg, default in zip(positional, node.args.defaults, strict=True)
                if isinstance(default, ast.Name) and default.id == handler.name
            )
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "set_exception"
        and any(isinstance(arg, ast.Name) and arg.id in names for arg in node.args)
        for node in ast.walk(handler)
    )


def _reports_exception(handler: ast.ExceptHandler) -> bool:
    # Ruff recognizes reporting calls, but does not enforce our loop-boundary or reason policy.
    # Check direct statements only: a report hidden in a conditional may never execute.
    for statement in handler.body:
        if any(isinstance(n, (ast.Return, ast.Raise, ast.Break, ast.Continue)) for n in ast.walk(statement)):
            return False
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        name = ast.unparse(call.func)
        if name.endswith(".exception") or name == "traceback.print_exc":
            return True
        if name.endswith((".error", ".critical", ".log")) and any(
            k.arg == "exc_info" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in call.keywords
        ):
            return True
    return False


def changed_lines(project: Path, base: str) -> dict[str, set[int]] | None:
    """Project-relative path -> line numbers added or modified since `base`; None if git cannot say.

    Untracked files count as wholly added. Deleted lines are not listed: a removal adds no debt.
    """
    code, diff = _gate_base._run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "-U0",
            "--no-color",
            *_secret_env.GIT_NO_DRIVERS,
            "--relative",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            "--",
            ".",
        ],
        project,
    )
    if code:
        return None
    changed: dict[str, set[int]] = {}
    lines: set[int] = set()
    for row in diff.splitlines():
        if row.startswith("+++ "):
            # git ends the header of a name with a space in it with a tab.
            lines = (
                changed.setdefault(row[len("+++ b/") :].rstrip("\t"), set())
                if row != "+++ /dev/null"
                else set()
            )
        elif hunk := re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", row):
            start, count = int(hunk[1]), int(hunk[2] or 1)
            lines.update(range(start, start + count))
    code, untracked = _gate_base._run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "."], project
    )
    if code:
        return None
    for relative in filter(None, untracked.split("\0")):
        try:
            count = len((project / relative).read_bytes().splitlines())
        except OSError:
            continue
        changed[relative] = set(range(1, count + 1))
    return changed


def _check_suppressions(
    project: Path, files: dict[str, str], changed: dict[str, set[int]] | None = None
) -> Result:
    # tokenize and ast avoid treating strings as directives or guessing handler structure from indentation.
    # No Ruff rule covers reason length, protected codes, and the loop-boundary exception together.
    # `changed` limits reports to the project's own lines: a root-layout project is a whole repository,
    # whose pinned boundary handlers and older suppression comments would fail every change to it.
    protected = _suppression_codes(files)
    problems = []
    for relative in sorted(_gate_base._project_files(project)):
        path = project / relative
        if path.suffix not in {".py", ".pyi"} or (changed is not None and relative not in changed):
            continue

        def fail(line: int, message: str, end: int | None = None, filename: str = relative) -> None:
            # A violation spanning lines (a handler) is the project's if it touched any of them.
            if changed is None or any(line <= n <= (end or line) for n in changed[filename]):
                problems.append(f"{filename}:{line}: {message}")

        try:
            with tokenize.open(path) as source:
                text = source.read()
            tree = ast.parse(text)
            comments = [
                t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.COMMENT
            ]
        except (SyntaxError, UnicodeError, LookupError, tokenize.TokenError) as exc:
            # The file is in `changed`, so the project touched it; judge it whole rather than guess.
            fail(getattr(exc, "lineno", 1) or 1, f"cannot inspect suppressions: {exc}", sys.maxsize)
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        boundaries = {}
        aliases = {"contextlib.suppress"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "contextlib":
                        aliases.add(f"{alias.asname or alias.name}.suppress")
            elif isinstance(node, ast.ImportFrom) and node.module == "contextlib":
                aliases.update(a.asname or a.name for a in node.names if a.name == "suppress")
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            broad = any(t is None or ast.unparse(t) in _BROAD_EXCEPTIONS for t in caught)
            if not broad:
                continue
            ancestor = parents.get(node)
            in_loop = False
            while ancestor is not None:
                if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    break
                if isinstance(ancestor, (ast.For, ast.AsyncFor, ast.While)):
                    in_loop = True
                    break
                ancestor = parents.get(ancestor)
            reported_boundary = in_loop and _reports_exception(node)
            handed_on = _hands_to_future(node)
            boundaries[node.lineno] = reported_boundary or handed_on
            if (
                not reported_boundary
                and not handed_on
                and not any(isinstance(s, ast.Raise) for s in node.body)
            ):
                fail(node.lineno, "broad catch must report at a loop boundary or re-raise", node.end_lineno)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) in aliases
                and any(ast.unparse(arg) in _BROAD_EXCEPTIONS for arg in node.args)
            ):
                fail(
                    node.lineno,
                    "contextlib.suppress of Exception or BaseException swallows broad failures",
                    node.end_lineno,
                )
        for comment in comments:
            if not re.match(r"#\s*(?:(?:ruff|flake8):\s*)?noqa\b", comment.string, re.IGNORECASE):
                continue
            line = comment.start[0]
            directive = re.fullmatch(
                r"#\s*noqa:\s*([A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)\s*(?:#|--)\s*(.+)",
                comment.string,
                re.IGNORECASE,
            )
            if not directive:
                fail(
                    line,
                    "noqa needs specific codes and a reason of at least 10 characters "
                    "(# noqa: CODE -- reason)",
                )
                continue
            if len(directive[2].strip()) < 10:
                fail(line, "noqa reason must contain at least 10 characters")
            for code in re.split(r"\s*,\s*", directive[1].upper()):
                if code == "BLE001" and boundaries.get(line):
                    continue
                if code == "BLE001":
                    fail(line, "BLE001 may only be suppressed at a reported loop boundary")
                elif any(code.startswith(prefix) for prefix in protected):
                    fail(line, f"MUST rule code {code} cannot be suppressed")
    status = _gate_base.Status.FAIL if problems else _gate_base.Status.PASS
    lines = problems or ["suppression policy passed"]
    if changed is None:
        lines.append("checked every line: no base commit to diff against")
    return Result("factory:suppressions", status, "\n".join(lines))


#: What a check says when it needs evidence only a person can give (a bare `tool` rule; a UI to look at).
UI_EVIDENCE_MISSING = (
    "no project command configured for this evidence check: declare it in factory-evidence.toml at the "
    'project root, [evidence] "<RULE-ID>" = "<command>"'
)


def standards_results(
    project: Path,
    files: dict[str, str],
    *,
    configs: Path,
    shell_scripts: tuple[str, ...] = (),
    changed: dict[str, set[int]] | None = None,
    start: Path | None = None,
    work: _gate_tests.GateWork | None = None,
    size_reference: Path | None = None,
    standards_dir: Path | None = None,
) -> list[Result]:
    """Run the mechanically checked MUST rules in `files` against `project`.

    The built-in `factory:` checks run when a rule maps them (`Enforced: check <name>`; `fail-to-pass` is
    judged in the scope flow, see `_judge_project`) and are recorded under their own name with the mapped
    rule IDs in the output. `size_reference` is the tree `factory:module-size` measures growth against when
    there is no `start` tree. With no rules at all the result says no standards are configured.

    `configs` holds the baseline tool configs for a project without its own; `standards_dir` (default: two
    levels above `configs`, where the standards are materialized) is what a `script` rule's path is
    relative to; `shell_scripts` are
    extra project-relative scripts for shellcheck. A rule's `Timeout: <seconds>` line bounds each of
    its commands (default `_gate_base._TOOL_TIMEOUT`). `changed` (see `changed_lines`) limits the suppression
    policy to the lines a project added; None checks every line. `start` is the project's starting tree
    (root layout): a language-pack rule that fails on `project` and has no finding the start
    lacks passes as pre-existing, so inherited debt does not block (see `_judge_against_start`; tsc output is
    not parsed, so TS rules keep failing). The start's results are kept by the start tree's content (see
    `_cached_run`), and mypy keeps its incremental cache between runs, head and start apart. `work`
    shares the head tree's pytest run with fail-to-pass and reports where the time went. Shared by the
    DONE gate and the CI entry point (`python -m flyte_agent_factory._gates standards <dir>`), so both judge
    a tree the same way.
    """
    results: list[Result] = []
    all_rules = _standards.rules(files)
    standards_dir = standards_dir or configs.parent.parent

    # Rules judged against the start tree; without one, their failure is the factory's
    # (`fault_failure_without_start`)
    language_idents = {r.ident for r in all_rules if r.language_pack} | {_standards.MODULE_SIZE}

    def record(rule: str, status: _gate_base.Status, output: str) -> None:
        if rule in language_idents and work:
            status, output = _gate_start.fault_failure_without_start(status, output, work.start_fault)
        results.append(Result(rule, status, output))

    if not all_rules:
        record("factory:standards", _gate_base.Status.NOT_APPLICABLE, _standards.NO_STANDARDS)
        return results

    start_key = ""

    def with_mypy_cache(command: list[str], kind: str) -> list[str]:
        # Head and start have different sources; one cache for both would be rebuilt by each run.
        if Path(command[0]).name != "mypy":
            return command
        return [command[0], "--cache-dir", str(_gate_tests.cache_root() / "mypy" / kind), *command[1:]]

    def execute(
        rule: _standards.Rule, command: list[str], timeout: int, head: tuple[int, str] | None = None
    ) -> tuple[int, str]:
        nonlocal start_key
        # Compare on the full output: the recorded tail can hide findings.
        code, output = head or _gate_base._run(
            with_mypy_cache(command, "head"), project, timeout=timeout, tail=None
        )
        if code and start is not None and rule.language_pack:
            start_key = start_key or _gate_tests.tree_key(start)
            on_start = with_mypy_cache(command, "start")
            return _gate_start._judge_against_start(
                code,
                output,
                *_gate_tests._cached_run(
                    start,
                    start_key,
                    rule.ident,
                    on_start,
                    lambda: _gate_base._run(on_start, start, timeout=timeout, tail=None),
                    work,
                ),
            )
        return code, output[-_gate_base._OUTPUT_TAIL :]

    def run_commands(rule: _standards.Rule, timeout: int) -> None:
        for command in _gate_rules._commands(rule):
            shared = work.head_pytest(project) if work and command == ["python", "-m", "pytest"] else None
            # pytest's entry point uses its isolated interpreter without replacing the
            # factory's Python on PATH. Project-local tools still take precedence below.
            if command[:3] == ["python", "-m", "pytest"] and shutil.which("pytest"):
                command = ["pytest", *command[3:]]
            pyproject = project / "pyproject.toml"
            if command[0] == "mypy" and (
                (project / "pyrightconfig.json").exists()
                or (pyproject.exists() and "[tool.pyright" in pyproject.read_text())
            ):
                command = ["pyright"]
            tool = command[2] if command[:2] == ["python", "-m"] else command[0]
            binary = project / "node_modules/.bin" / command[0]
            local = project / ".venv/bin" / command[0]
            if local.is_file():
                command[0] = str(local)
            elif binary.is_file():
                command[0] = str(binary)
            if not shared and not shutil.which(command[0]):
                record(rule.ident, _gate_base.Status.TOOL_UNAVAILABLE, image_defect(tool, [rule.ident]))
                continue
            command = _gate_rules.with_baseline_config(command, project, configs, shell_scripts)
            eslint_baseline = (
                Path(command[0]).name == "eslint"
                and not any(project.glob("eslint.config.*"))
                and not any(project.glob(".eslintrc*"))
            )
            if Path(command[0]).name == "tsc" and not (project / "tsconfig.json").exists():
                config = json.loads((configs / "tsconfig.json").read_text())
                config["files"] = [str(p) for p in project.rglob("*.ts") if "node_modules" not in p.parts]
                config.pop("include", None)
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json", dir=project) as baseline:
                    json.dump(config, baseline)
                    baseline.flush()
                    command += ["--project", baseline.name]
                    code, output = execute(rule, command, timeout)
            elif eslint_baseline:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".mjs", dir=project) as baseline:
                    baseline.write((configs / "eslint.config.mjs").read_text())
                    baseline.flush()
                    command += ["--config", baseline.name]
                    code, output = execute(rule, command, timeout)
                if "Cannot find package" in output:
                    code = 127
                    output = "ESLint baseline dependency missing\n" + output
            else:
                code, output = execute(
                    rule, command, timeout, (shared.code, shared.output) if shared else None
                )
            if not shared and "No module named pytest" in output:  # a shared run quotes child-lane launchers
                code = 127
                output = image_defect("pytest", [rule.ident])
            record(rule.ident, _gate_base.Status.from_exit(code), shlex.join(command) + "\n" + output)

    def labelled(ids: list[str], output: str) -> str:
        return f"[{', '.join(ids)}] {output}"

    must = [rule for rule in all_rules if rule.must]
    # An ID that two rules share names neither: waivers, reports and reviewers could not tell them apart.
    duplicated = {r.ident for r in must if sum(o.ident == r.ident for o in all_rules) > 1}
    mapped = _standards.check_rules(files)
    if project.is_dir():
        for ident in sorted(duplicated):
            where = ", ".join(r.file for r in all_rules if r.ident == ident)
            record(
                ident, _gate_base.Status.FAIL, f"rule ID defined more than once ({where}); IDs must be unique"
            )
        if any(rule.lint_codes for rule in must):
            results.append(_check_suppressions(project, files, changed))
        if ids := mapped.get(_standards.MODULE_SIZE):
            code, output = _module_size.check(project, start or size_reference, _gate_tests._is_test, work)
            record(_standards.MODULE_SIZE, _gate_base.Status.from_exit(code), labelled(ids, output))
        # An evidence rule names no command of its own: the project may declare the one that satisfies it.
        declared, misdeclared = _evidence.declared(project, _standards.evidence_rules(must))
        if misdeclared:
            record("factory:evidence", _gate_base.Status.FAIL, "\n".join(misdeclared))
        for rule in must:
            if rule.kind == "check" and rule.arg == _standards.UI_EVIDENCE and rule.ident not in duplicated:
                ids = mapped[_standards.UI_EVIDENCE]
                if rule.ident != ids[0]:
                    continue
                commands = [i for i in ids if i in declared]
                if commands and _has_ui(project):
                    first = len(results)
                    for ident in commands:
                        run_commands(
                            dataclasses.replace(rule, kind="command", arg=declared[ident]),
                            _gate_rules._timeout(rule)[0] or _gate_base._TOOL_TIMEOUT,
                        )
                    # One entry under the check's name, as when nothing is declared.
                    results[first:] = [
                        Result(_standards.UI_EVIDENCE, r.status, labelled(ids, r.output))
                        for r in results[first:]
                    ]
                    continue
                if _has_ui(project):
                    status, output = _gate_base.Status.TOOL_UNAVAILABLE, UI_EVIDENCE_MISSING
                else:
                    status, output = _gate_base.Status.NOT_APPLICABLE, "no UI in the project"
                record(_standards.UI_EVIDENCE, status, labelled(ids, output))
            elif rule.ident in duplicated or rule.kind in {"", "judgement", "check"}:
                if rule.error:
                    record(rule.ident, _gate_base.Status.FAIL, f"invalid Enforced line: {rule.error}")
                continue
            if rule.kind == "tool" and rule.ident not in declared:
                output = UI_EVIDENCE_MISSING.replace("<RULE-ID>", rule.ident)
                record(rule.ident, _gate_base.Status.TOOL_UNAVAILABLE, output)
                continue
            timeout, problem = _gate_rules._timeout(rule)
            if problem:
                record(rule.ident, _gate_base.Status.FAIL, problem)
            elif rule.kind in {"command", "lint"}:
                run_commands(rule, timeout)
            elif rule.kind == "tool":
                run_commands(dataclasses.replace(rule, kind="command", arg=declared[rule.ident]), timeout)
            elif rule.kind == "forbid":
                code, output = _gate_rules._forbidden(project, rule)
                record(rule.ident, _gate_base.Status.from_exit(code), output)
            elif rule.kind == "script":
                command = _gate_rules._script_command(rule, standards_dir)
                if isinstance(command, str):
                    record(rule.ident, _gate_base.Status.FAIL, command)
                else:
                    code, output = _gate_base._run(command, project, timeout=timeout)
                    record(rule.ident, _gate_base.Status.from_exit(code), shlex.join(command) + "\n" + output)
    if ids := mapped.get(_standards.LANGUAGE_SUMMARY):
        language = {r.ident for r in must if r.language_pack and r.kind not in {"judgement", "check"}}
        language_results = [r for r in results if r.rule in language]
        statuses = {r.status for r in language_results}
        record(
            _standards.LANGUAGE_SUMMARY,
            _gate_base.Status.FAIL
            if _gate_base.Status.FAIL in statuses
            else _gate_base.Status.TOOL_UNAVAILABLE
            if _gate_base.Status.TOOL_UNAVAILABLE in statuses
            else _gate_base.Status.HARNESS_FAULT
            if _gate_base.Status.HARNESS_FAULT in statuses
            else _gate_base.Status.PASS,
            labelled(
                ids,
                "language formatter/linter gate: "
                + ", ".join(f"{r.rule} {r.status.value}" for r in language_results),
            ),
        )
    return results


_target_base = _target_run.target_base


def run_target_command(
    item: _target_ci.Command, checkout: Path, job_state: Path | None = None, repo: str = ""
) -> str:
    """Run one script of the target's CI as its runner would; return a failure report, or "" if it passes."""
    return _target_run.report(item, _target_run.execute(item, checkout, job_state, repo))


def _target_changed_files(checkout: Path) -> set[str] | None:
    """Return the files the target checkout changes since its base, or None if that is unknown."""
    base = _target_base(checkout)
    if not base:
        return None
    code, output = _gate_base._run(["git", "diff", "--name-only", "-z", f"{base}...HEAD"], checkout)
    return None if code else {name for name in output.split("\0") if name}


def _target_verdict(rule: str, judged: _target_run.Judged, ran: str, extra: str = "") -> Result:
    """Return judged runs' result: FAIL for the project's failures, NOT RUN if only some could not run."""
    notes = "".join(
        f"\n{line}" for line in (*judged.not_run, *(f"pre-existing: {p}" for p in judged.pre_existing))
    )
    if judged.failures:
        return Result(
            rule,
            _gate_base.Status.FAIL,
            "the target repo's own CI fails locally\n" + "\n".join(judged.failures) + notes + extra,
        )
    if judged.not_run:
        return Result(
            rule,
            _gate_base.Status.NOT_RUN,
            "NOT RUN, which is neither a pass nor the project's failure; the rest ran clean" + notes + extra,
        )
    return Result(rule, _gate_base.Status.PASS, ran + notes + extra)


def target_ci_result(checkout: Path, repo: str = "", *, against_base: bool = True) -> Result:
    """Run what the target's CI runs. A command that cannot run offline fails too: it is not skipped.

    `against_base`: a failure the unpatched base's CI has identically is pre-existing, not the project's.
    Promotion turns it off: what it pushes must pass outright.
    """
    rule = "factory:target-ci"
    changed = _target_changed_files(checkout)
    commands = _target_ci.discover(checkout, changed, repo)
    # Steps that cannot fail CI are not run, but the report says so rather than leaving them out silently.
    skipped = "".join(
        f"\nskipped `{c.command}` ({c.origin}): {c.skipped}"
        for c in _target_ci.skipped(checkout, changed, repo)
    )
    if not commands:
        return Result(
            rule,
            _gate_base.Status.NOT_APPLICABLE,
            "no lint, type, docstring or test commands found in CI" + skipped,
        )
    judged = _target_run.Judged()
    with tempfile.TemporaryDirectory(prefix="factory-target-jobs-", ignore_cleanup_errors=True) as scratch:
        try:
            jobs, held = _target_run.run_jobs(checkout, commands, repo, Path(scratch))
        except _target_run.WorktreeUnavailable as exc:
            return Result(
                rule, _gate_base.Status.FAIL, f"cannot copy the checkout to run the target's CI in:\n{exc}"
            )
        for steps in jobs:
            one = _target_run.judge(
                checkout, steps, repo, against_base=against_base, artifacts=Path(scratch) / "artifacts"
            )
            judged.failures += one.failures
            judged.pre_existing += one.pre_existing
            judged.not_run += one.not_run
    ran = [f"`{item.command}`" for steps in jobs for item, run in steps if not run.skipped]
    return _target_verdict(rule, judged, "ran " + ", ".join(ran), skipped + "".join(f"\n{h}" for h in held))


def _is_test_file(relative: str) -> bool:
    """Whether a project-relative path is a test module: coverage configs commonly omit those."""
    path = PurePosixPath(relative)
    return (
        path.name == "conftest.py"
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
        or bool({"test", "tests"} & set(path.parts[:-1]))
    )


def uncovered_changed_lines(report: dict[str, Any], checkout: Path, changed: dict[str, set[int]]) -> set[str]:
    """`path:line` of changed lines coverage.py's JSON report says did not run or left a branch untaken.

    A changed Python file that is not a test and is not in the report at all was never imported, so
    nothing ran in it: all its changed lines are uncovered.
    """
    uncovered: set[str] = set()
    # A file outside the checkout (imported from a PYTHONPATH directory or site-packages) is not the change.
    files = {
        str(path.relative_to(checkout) if path.is_absolute() else path): data
        for name, data in report.get("files", {}).items()
        if not (path := Path(name)).is_absolute() or path.is_relative_to(checkout)
    }
    reported = set(files)
    for relative, lines in changed.items():
        if relative.endswith(".py") and relative not in reported and not _is_test_file(relative):
            uncovered.update(f"{relative}:{n}" for n in lines)
    for relative, data in files.items():
        lines = changed.get(relative, set())
        missing = set(data.get("missing_lines", []))
        uncovered.update(f"{relative}:{n}" for n in missing & lines)
        # A branch into a line that never ran is already reported as that line.
        uncovered.update(
            f"{relative}:{src}"
            for src, dest in data.get("missing_branches", [])
            if src in lines and dest not in missing
        )
    return uncovered


def _target_coverage_result(project: Path, checkout: Path, repo: str = "", new: bool = False) -> Result:
    """Require every changed line of the target's checkout covered, or listed in SUMMARY.md with a reason.

    `new`: the target has no commits yet, so all its Python is the project's change (the empty tree).
    """
    rule = "factory:target-coverage"
    base = _target_base(checkout) or (_delivery.EMPTY_TREE if new else "")
    changed = changed_lines(checkout, base) if base else None
    if changed is None:
        return Result(
            rule,
            _gate_base.Status.FAIL,
            "cannot tell what changed: the checkout has no merge-base with its default branch",
        )
    command = _target_ci.coverage_command(checkout)
    (checkout / ".coverage").unlink(missing_ok=True)  # a stale data file would be reported as this run's
    run = _target_run.execute(command, checkout, None, repo)
    # Upstream tests that fail on the unpatched base too are not the project's; its changed lines still count.
    judged = _target_run.judge(checkout, [(command, run)], repo, from_ci=False)
    if judged.failures or judged.not_run:
        return _target_verdict(rule, judged, "")
    old = "".join(f"\npre-existing: {p}" for p in judged.pre_existing)
    with tempfile.TemporaryDirectory(prefix="factory-coverage-", ignore_cleanup_errors=True) as scratch:
        report_path = Path(scratch) / "coverage.json"
        if reader := _target_ci.coverage_report_command(command, report_path):
            read = _target_run.execute(reader, checkout, None, repo)
            code, output = (read.code or 1, read.not_run) if read.not_run else (read.code, read.output)
        else:
            code, output = _gate_base._run(
                ["python", "-m", *_target_ci.COVERAGE_JSON, "-o", str(report_path)], checkout
            )
        if "No module named coverage" in output:
            return Result(rule, _gate_base.Status.TOOL_UNAVAILABLE, image_defect("coverage", [rule]))
        if code:
            return Result(rule, _gate_base.Status.FAIL, f"`coverage json` exited {code}:\n{output}")
        report = json.loads(report_path.read_text())
    summary = project / "SUMMARY.md"
    listed = _target_ci.parse_listed_lines(summary.read_text(errors="replace") if summary.is_file() else "")
    unlisted = sorted(
        uncovered_changed_lines(report, checkout, changed) - listed,
        key=lambda entry: (entry.rpartition(":")[0], int(entry.rpartition(":")[2])),
    )
    if unlisted:
        return Result(
            rule,
            _gate_base.Status.FAIL,
            f"`{command.command}`: changed lines not covered: {', '.join(unlisted)}\n"
            "Cover them, or list each in SUMMARY.md under `## Uncovered changed lines`: `path:line -- why`",
        )
    return Result(
        rule, _gate_base.Status.PASS, f"every changed line is covered or listed (`{command.command}`){old}"
    )


def target_results(project: Path, brief: str) -> list[Result]:
    """Return the target repo's CI and changed-line coverage for a project whose brief says `Delivers to:`.

    The target's tree is the project's imported files (`promote.toml`), checked in a scratch repo made from
    the workspace history. A project from before imports works in a nested checkout of the target instead.
    """
    target = _target_ci.parse_target(brief)
    if target is None:
        return []
    try:
        delivery = _delivery.load(project)
        if delivery is not None:
            _delivery.require_repo(delivery, target.repo)
            with _delivery.target_checkout(project, delivery) as scratch:
                return _target_results_in(project, scratch, target.repo, new=not delivery.base)
    except _delivery.DeliveryError as exc:
        return [Result("factory:target-ci", _gate_base.Status.FAIL, str(exc))]
    checkout = _target_ci.find_checkout(project, target)
    if checkout is None:
        where = f"at `{target.path}`" if target.path else "(the one nested git checkout)"
        return [
            Result(
                "factory:target-ci",
                _gate_base.Status.FAIL,
                f"no {_delivery.FILE} and no checkout of {target.repo} {where} in the project: "
                f"the operator imports the target's files with `python ops/promote.py import {target.repo} "
                "<this project's directory>`",
            )
        ]
    return _target_results_in(project, checkout, target.repo)


def _target_results_in(project: Path, checkout: Path, repo: str, new: bool = False) -> list[Result]:
    return [target_ci_result(checkout, repo), _target_coverage_result(project, checkout, repo, new)]


_NAME_MAX = 255  # NAME_MAX on Linux: longer components make `stat` raise ENAMETOOLONG


def _label_paths(workspace: Path, project: Path, output: str) -> list[tuple[str, str]]:
    """Return the (path, line) pairs in gate output worth recording as labels.

    URL fragments, versions, ratios and dates (`3/4.5`, `2026/10.25`) look like filenames and are not.
    A token counts if it names a file that exists, or ends in a source-like suffix. With a directory part,
    a short alphabetic suffix also counts: a file the fix has since deleted still names where the finding was.
    """
    text = re.sub(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+", "", output)
    found = []
    for path, line in re.findall(r"([\w./-]+\.[\w]+)(?::(\d+))?", text):
        if any(len(part) > _NAME_MAX for part in path.split("/")):
            continue
        suffix = Path(path).suffix.lower()
        if (
            suffix in _SOURCE_SUFFIXES
            or (workspace / path).is_file()
            or (project / path).is_file()
            or ("/" in path and re.fullmatch(r"\.[a-z]{1,3}", suffix))
        ):
            found.append((path, line))
    return found


def _load_waivers(
    workspace: Path, name: str, repo_url: str
) -> tuple[list[_waivers.Waiver], _gate_base.Status, str]:
    """Return the owner's waivers, and the status and note for `factory:waivers` when there are none to use.

    A malformed file is a FAIL; a copy that could not be read is UNKNOWN, which is visible but does not block.

    Nothing from the workspace's `.git` decides what is read (see `_waivers`). Any failure to read the
    owner's copy means no waivers: a waiver must not outlive its removal because the network was down, and
    a session-controlled ref is never the fallback.
    """
    try:
        if repo_url:
            return _waivers.load_owner(repo_url), _gate_base.Status.PASS, ""
        return _waivers.load(workspace, _trusted_base(workspace, name)), _gate_base.Status.PASS, ""
    except _waivers.OwnerCopyUnavailable as exc:
        note = f"the owner's copy of waivers.toml could not be read, so no waiver applies: {exc}"
        return [], _gate_base.Status.UNKNOWN, note
    except ValueError as exc:
        return [], _gate_base.Status.FAIL, str(exc)


def run(
    workspace: Path,
    name: str,
    brief: str,
    demo: str,
    note: str,
    files: dict[str, str],
    timeline: Any,
    layout: str = "sandbox",
    directions: Sequence[str] = (),
    work: _gate_tests.GateWork | None = None,
    repo_url: str = "",
) -> str:
    """Return blocking failures and record every per-rule result for operators.

    `repo_url` is the repository the supervisor works on; owner waivers are read from its default branch.

    `directions` is the owner's later direction; a `May edit:` line in it grants scope. `work` carries
    in the pytest run `checks.sh` made, and carries out how long each part took.
    """
    started = time.monotonic()
    work = work or _gate_tests.GateWork()
    project = _gate_base.project_dir(workspace, name, layout)
    brief = brief + _owner_direction(directions)
    results = []
    today = datetime.datetime.now(datetime.timezone.utc).date()
    in_repo = is_git_repo(workspace)
    mapped = _standards.check_rules(files)
    waivers, waiver_status, waiver_note = (
        _load_waivers(workspace, name, repo_url) if in_repo else ([], _gate_base.Status.PASS, "")
    )

    def record(rule: str, status: _gate_base.Status, output: str) -> None:
        if not isinstance(status, _gate_base.Status):
            raise TypeError(
                f"record() takes a Status, not {status!r}; convert an exit code with Status.from_exit"
            )
        # Messages are written "RULE: ..." in places; the label is added once, here.
        output = output.removeprefix(f"{rule}: ")
        # The scope gate is what keeps the factory from writing waivers.toml; it is not the owner's to waive.
        if status is _gate_base.Status.FAIL and rule not in {"scope", "factory:waivers"}:
            ids = mapped.get(rule, ())
            if waiver := _waivers.applicable(waivers, rule, name, today, ids):
                status = _gate_base.Status.WAIVED
                output = f"waived by owner until {waiver.expires}: {waiver.link}\n{output}"
            elif expired := _waivers.matching(waivers, rule, name, ids):
                output = f"waiver expired {max(w.expires for w in expired)}\n{output}"
        results.append({"rule": rule, "status": status.value, "output": output})
        if hasattr(timeline, "metric_label"):
            # Labels are training data on the side: whatever goes wrong here must not reach the gate result.
            # OSError: a filesystem refusing a name; ValueError/TypeError: a token or label it cannot take.
            try:
                for path, line in _label_paths(workspace, project, output) or [("", "")]:
                    timeline.metric_label(
                        kind="gate",
                        round=timeline.review_round + 1,
                        name=rule,
                        verdict=status.value,
                        file=path,
                        line=int(line) if line else None,
                        title=output[:500],
                    )
            except (OSError, ValueError, TypeError):
                pass
        if status.holds:
            work.harness_faults.append(f"{rule}: {output}")
        icon = "🚧" if status.blocks else "⚠️" if status is _gate_base.Status.UNKNOWN or status.holds else "ℹ️"
        timeline.row(icon, f"{rule}: {status.value}: {output}")

    if waiver_note:
        record("factory:waivers", waiver_status, waiver_note)
    if in_repo:
        code, work.head_sha = _gate_base._run(["git", "rev-parse", "HEAD"], workspace)
        work.head_sha = work.head_sha.strip() if not code else ""
    work.share_pytest = any(
        rule.must and rule.kind == "command" and rule.arg == "python -m pytest"
        for rule in _standards.rules(files)
    )
    tested = mapped.get(_standards.FAIL_TO_PASS, [])
    base = work.trusted_base = resolve_base(workspace, name) if in_repo else ""
    uncommitted = _uncommitted(workspace, name, layout) if in_repo else []
    if base:
        _judge_project(workspace, name, base, brief, demo, note, uncommitted, record, layout, work, tested)
    else:
        # Without a repository, or a commit to measure from, nothing can be proved either way.
        reason = "no git" if not in_repo else "starting commit unavailable"
        record("scope", _gate_base.Status.NOT_APPLICABLE, reason)
        if tested:
            record(
                _standards.FAIL_TO_PASS, _gate_base.Status.NOT_APPLICABLE, f"[{', '.join(tested)}] {reason}"
            )
    if uncommitted:
        record(
            "uncommitted", _gate_base.Status.FAIL, "commit and push before DONE: " + ", ".join(uncommitted)
        )
    configs = workspace / ".claude-flyte/standards/languages/configs"
    # The root layout keeps the project's script in the harness directory, which the shellcheck walk skips.
    scripts = (
        (_gate_base.checks_rel(name, layout),)
        if layout == "root" and (workspace / _gate_base.checks_rel(name, layout)).is_file()
        else ()
    )
    changed = changed_lines(project, base) if base else None
    with contextlib.ExitStack() as stack:
        # A root-layout project is a whole repository: judge its language tools on regressions from
        # the starting tree, as the suppressions gate does on `changed`, not on debt it inherited.
        start = None
        if layout == "root" and base:
            code, output = _project_commits(workspace, name, base)
            try:
                start = stack.enter_context(
                    _gate_start._start_tree(workspace, base, None if code else output.splitlines())
                ).path
            except _gate_start._GateError as exc:
                # `scope` reports this once, in `_judge_project`. Without a start tree a language rule
                # cannot tell inherited debt from the project's: its failure is the factory's fault.
                work.start_fault = str(exc)
        for result in standards_results(
            project, files, configs=configs, shell_scripts=scripts, changed=changed, start=start, work=work
        ):
            record(result.rule, result.status, result.output)
    for result in target_results(project, brief):
        record(result.rule, result.status, result.output)
    target = workspace / ".claude-flyte" / name / "standards-results.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(results, indent=2))
    work.seconds["gate"] = time.monotonic() - started
    return "\n".join(f"{r['rule']}: {r['output']}" for r in results if _gate_base.Status(r["status"]).blocks)


def standards_scan(
    project: Path, standards_dir: Path | None = None, pytest_junit: Path | None = None
) -> list[Result]:
    """Run the mechanically checked MUST rules of `standards_dir` on `project`, as the DONE gate would.

    Without a standards directory there are no rules: the one result says so. Every base file of the
    directory applies (`[standards] include` is a profile's setting, and a scan has no profile).

    `pytest_junit` (see `main`) lets the pytest rule be judged from an earlier run; every rule still runs.
    """
    if standards_dir is None:
        return [Result("factory:standards", _gate_base.Status.NOT_APPLICABLE, _standards.NO_STANDARDS)]
    base_files = sorted(
        p.stem
        for p in standards_dir.glob("*.md")
        if p.stem not in {"README", "brand", "demo"} and re.fullmatch(r"[a-z][a-z0-9_-]*", p.stem)
    )
    config = _standards.Standards(dir=str(standards_dir), include=tuple(base_files))
    files = _standards.capture(config, project)
    # The baseline configs go to a scratch directory, not `<dir>/.claude-flyte/standards`: in a factory
    # workspace that is the running project's own materialized copy, which this must not overwrite.
    with tempfile.TemporaryDirectory(prefix="factory-standards-") as scratch:
        selected = _standards.materialize(Path(scratch), project, config, files)
        configs = Path(scratch) / ".claude-flyte/standards/languages/configs"
        # Plain pytest runs as the gate runs it (slow lane beside the rest), so both judge a tree alike.
        return standards_results(
            project,
            selected,
            configs=configs,
            work=_gate_tests.scan_work(project, pytest_junit),
            size_reference=_parent_tree(project, Path(scratch) / "parent"),
        )


def _parent_tree(project: Path, into: Path) -> Path | None:
    """Export the commit before `project`'s HEAD to `into`; None outside git or on a root commit.

    A scan has no round start tree, so the module-size check judges growth against the previous commit: a
    module that was already over the limit and did not grow is debt, not a failure, on main as on a pull
    request's merge.
    """
    code, prefix = _gate_base._run(["git", "rev-parse", "--show-prefix"], project)
    if code or _gate_base._run(["git", "rev-parse", "--verify", "--quiet", "HEAD~1"], project)[0]:
        return None
    into.mkdir()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD~1", "--", "."], cwd=project, capture_output=True, check=False
    )
    if archive.returncode:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    return into / prefix.strip() if prefix.strip() else into


def finding_counts(results: Sequence[Result]) -> dict[str, int]:
    """Return findings per failing rule: its non-blank output lines. A missing tool is not a finding."""
    return {
        r.rule: sum(bool(line.strip()) for line in r.output.splitlines())
        for r in results
        if r.status is _gate_base.Status.FAIL
    }


def main(argv: list[str] | None = None) -> int:
    """`standards <dir>`: run a standards directory's mechanically checked MUST rules on `dir`.

    `--pytest-junit DIR` reuses CI's pytest run; an unusable, failing or other-commit run is run again.
    """
    ap = argparse.ArgumentParser(prog="python -m flyte_agent_factory._gates")
    ap.add_argument("step", choices=("standards",))
    ap.add_argument("dir", type=Path, help="the project tree to check")
    ap.add_argument(
        "--standards",
        type=Path,
        help="the standards directory (default: <dir>/standards if it exists, else no rules)",
    )
    ap.add_argument(
        "--pytest-junit",
        type=Path,
        help="a directory holding fast.xml, slow.xml (pytest --junitxml of the `not slow` and `slow` lanes) "
        "and head (the commit they ran on); when they are for <dir>'s HEAD and every test passed, "
        "the test rule is judged from them instead of running pytest again",
    )
    args = ap.parse_args(argv)
    project = args.dir.resolve()
    if not project.is_dir():
        ap.error(f"not a directory: {project}")
    standards = args.standards or (project / "standards" if (project / "standards").is_dir() else None)
    if standards is not None and not standards.is_dir():
        ap.error(f"not a directory: {standards}")
    junit = args.pytest_junit
    if junit is not None and not junit.is_dir():
        ap.error(f"not a directory: {junit}")
    results = standards_scan(project, standards.resolve() if standards else None, junit)
    for result in results:
        print(f"{result.rule}: {result.status.value}")
        for line in result.output.splitlines():
            print("    " + line)
    return 1 if any(r.status.blocks for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
