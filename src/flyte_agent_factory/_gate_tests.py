"""The fail-to-pass run behind `factory:fail-to-pass`: test-weakening checks, junit reuse, start cache."""

from __future__ import annotations

import ast
import concurrent.futures
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import flyte_agent_factory

from . import _gate_base, _gate_start, _secret_env, _vendored


def _is_test(path: str) -> bool:
    p = Path(path)
    return (
        "tests" in p.parts
        or "__tests__" in p.parts
        or p.name.startswith("test_")
        or ".test." in p.name
        or ".spec." in p.name
        or p.name.endswith(("_test.py", ".tftest.hcl"))
        or p.name == "conftest.py"
    ) and p.suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".sh", ".hcl"}


def _test_names(text: str) -> set[str]:
    def collect(node: ast.AST, prefix: str = "") -> set[str]:
        names: set[str] = set()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                names.update(collect(child, prefix + child.name + "."))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"):
                names.add(prefix + child.name)
        return names

    return collect(ast.parse(text))


#: Test-skipping spellings in pytest and unittest: `pytest.skip(...)`, `@pytest.mark.skipif`,
#: `@unittest.skip`, `self.skipTest(...)`.
_PY_SKIPS = {"skip", "skipif", "skipUnless", "skipTest", "xfail", "importorskip", "expectedFailure"}


_JS_SKIPS = re.compile(r"\b(?:it|test|describe)\.(?:skip|todo)\b|\bx(?:it|describe|test)\(")


def _count_skips(text: str, suffix: str) -> int:
    """Skips in the code itself; the word in a comment or a string is not one."""
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
        return len(_JS_SKIPS.findall(text))
    if suffix != ".py":
        return 0
    count = 0
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Attribute):
            count += node.attr in _PY_SKIPS
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            count += node.func.id in _PY_SKIPS
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count += sum(isinstance(d, ast.Name) and d.id in _PY_SKIPS for d in node.decorator_list)
    return count


#: The pre-review sets this for `checks.sh`, which writes `fast.xml`, `slow.xml` (pytest `--junitxml`) and
#: `head` (the commit it ran on) there, so the gate reads that run instead of repeating it.
PYTEST_JUNIT_ENV = "AGENT_FACTORY_PYTEST_JUNIT_DIR"
#: With the value `child`, `checks.sh` runs its slow lane in a child action (`_slow_lane.ENV_VAR`, the same
#: name: `_slow_lane` declares a task environment, which importing it here would register as a side effect).
SLOW_LANE_ENV = "AGENT_FACTORY_SLOW_LANE"
#: Names an executable that runs a project's pytest lane, or `-- <command>`, in a child action (the launcher
#: `write_lane_launcher` writes). Unset in CI and on a laptop: `checks.sh` then runs things itself.
TEST_LANE_ENV = "AGENT_FACTORY_TEST_LANE"


#: Not part of a tree's identity: caches and environments that running a tool in it creates.
_UNHASHED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
}


#: A project that registers this pytest marker has a slow lane, run beside the rest.
_SLOW_MARKER = re.compile(r"""["']slow:""")


def cache_root() -> Path:
    """Where results that depend only on a tree's content are kept between gate runs.

    `$XDG_CACHE_HOME` when set, else `$HOME/.cache`: the container user can write nowhere else.
    """
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "factory-gate"


def tree_key(tree: Path) -> str:
    """Identify a tree by its content (paths, modes, bytes), so equal start trees share cached results.

    Hashed from the files rather than taken from a commit: the start tree is HEAD with the project's
    commits reverse-applied, which is the same tree after the project adds another commit.
    """
    digest = hashlib.sha256()
    for root, dirs, names in os.walk(tree):
        dirs[:] = sorted(d for d in dirs if d not in _UNHASHED_DIRS)
        for name in sorted(names):
            path = Path(root, name)
            digest.update(f"{path.relative_to(tree)}\0{path.lstat().st_mode}\0".encode())
            digest.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()[:32]


def _cached_run(
    tree: Path,
    key: str,
    rule: str,
    command: list[str],
    run: Callable[[], tuple[int, str]],
    work: GateWork | None,
) -> tuple[int, str]:
    """Return `run()` for a command on `tree`, kept in `<cache>/<key>/<rule>.json` for the next gate run.

    A rule can run several commands, so the file maps command to result. The tree's own path is left
    out of the command, because a throwaway start tree has a new one each time. Timeouts and a missing
    tool say nothing about the tree and are never kept. The tool's version is not part of the key:
    delete the directory after upgrading ruff or mypy.
    """
    path = cache_root() / key / f"{rule}.json"
    wanted = shlex.join(command).replace(str(tree), "<tree>")
    started = time.monotonic()
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        saved = {}
    hit = saved.get(wanted) if isinstance(saved, dict) else None
    if isinstance(hit, list) and len(hit) == 2:
        result = (int(hit[0]), str(hit[1]))
    else:
        result = run()
        if result[0] not in {124, 127}:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                scratch = path.with_suffix(f".{os.getpid()}.tmp")
                scratch.write_text(json.dumps({**saved, wanted: list(result)}))
                scratch.replace(path)
            except OSError:
                pass  # a cache that cannot be written (read-only $HOME) must not fail the gate
        hit = None
    if work is not None:
        work.base_runs += 1
        work.base_hits += hit is not None
        work.seconds["base"] += time.monotonic() - started
    return result


@dataclasses.dataclass(frozen=True)
class JunitCase:
    """One test's outcome from a pytest junit file."""

    classname: str
    name: str
    passed: bool


@dataclasses.dataclass(frozen=True)
class JunitRun:
    """What a pytest junit file says: every test's outcome, and the slow lane's seconds."""

    cases: list[JunitCase]
    slow_seconds: float = 0.0


def parse_junit(path: Path) -> list[JunitCase]:
    """Read the tests in a pytest junit file. Raises OSError or ParseError for one that is unusable."""
    # The file is written by pytest in the workspace; expat (2.4.1+) refuses entity-expansion bombs.
    return [
        JunitCase(
            case.get("classname", ""),
            case.get("name", ""),
            case.find("failure") is None and case.find("error") is None,
        )
        for case in ElementTree.parse(path).getroot().iter("testcase")
    ]


def read_junit(directory: Path, since: float, head: str) -> JunitRun | None:
    """Return the tests `checks.sh` ran on commit `head`, or None when they cannot stand in for a run here.

    Both lanes' files must be there and newer than `since` (when the checks started), and `head` must be
    the commit they ran on: anything less is a partial or an old run.
    """
    try:
        if (directory / "head").read_text().strip() != head:
            return None
        files = [directory / "fast.xml", directory / "slow.xml"]
        if any(f.stat().st_mtime < since for f in files):
            return None
        cases = [case for f in files for case in parse_junit(f)]
        slow = ElementTree.parse(files[1]).getroot().find("testsuite")
        seconds = float(slow.get("time", 0)) if slow is not None else 0.0
    except (OSError, ValueError, ElementTree.ParseError):
        return None
    return JunitRun(cases, seconds)


def _changed_tests_pass(cases: list[JunitCase], relative: list[str]) -> bool:
    """Whether every changed test file has tests in `cases` and all of them passed.

    False covers "cannot tell" too (a file the run did not collect): the caller then runs the tests
    itself, which also gives the failure message.
    """
    for path in relative:
        if path.startswith(".."):
            return False
        module = Path(path).with_suffix("").as_posix().replace("/", ".")
        mine = [c for c in cases if c.classname == module or c.classname.startswith(module + ".")]
        if not mine or not all(c.passed for c in mine):
            return False
    return True


@dataclasses.dataclass
class HeadRun:
    """The head tree's full pytest run: exit code, output, and the tests (None when no junit came out)."""

    code: int
    output: str
    cases: list[JunitCase] | None
    slow_seconds: float = 0.0


#: What `_slow_lane.main` exits with under `--no-local` when the lane did not run in a child action
#: (`_slow_lane.NOT_RUN`, copied for the reason `SLOW_LANE_ENV` gives).
_LANE_NOT_RUN = 75


#: Prints `image=<the image this run's root action runs on>`. In the supervisor `FLYTE_INTERNAL_IMAGE` names
#: the plain Flyte image (no git, Node or Playwright), where most of a suite errors; lanes need the run's own.
#: Run by the deployed interpreter in the project directory, where the run context file is.
_RUN_IMAGE_LOOKUP = """
import os, re
import flyte.remote as remote
from flyte_agent_factory.child import attach
attach()
d = remote.ActionDetails.get(run_name=os.environ["FLYTE_INTERNAL_EXECUTION_ID"], name="a0")
print("image=" + re.findall(r'image: "([^"]+)"', str(d.pb2))[0])
"""


#: Run id -> its root action's image. A run's image does not change, and every review asks for it.
_RUN_IMAGES: dict[str, str] = {}


def _run_image(project: Path, env: dict[str, str]) -> str:
    """Return the image this run's root action runs on, or "" when it cannot be looked up.

    Outside a run (no `FLYTE_INTERNAL_EXECUTION_ID`) there is nothing to ask, so no lookup is started.
    """
    run = os.environ.get("FLYTE_INTERNAL_EXECUTION_ID", "")
    if not run:
        return ""
    if run not in _RUN_IMAGES:
        code, output = _gate_base._run(
            [sys.executable, "-P", "-c", _RUN_IMAGE_LOOKUP], project, env=env, timeout=120
        )
        found = [line.removeprefix("image=") for line in output.splitlines() if line.startswith("image=")]
        if code != 0 or not found:
            return ""
        _RUN_IMAGES[run] = found[-1]
    return _RUN_IMAGES[run]


def _launcher_command(
    junit: Path, lane_args: list[str], result: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """Return the deployed engine's launcher command for one lane, and the environment to run it in.

    The launcher is the `flyte_agent_factory` this process is running, never the workspace's copy: the
    workspace's harness files are what the gate does not trust. `-P` keeps the project (the cwd) off
    `sys.path`, and `PYTHONPATH` names only the package's own directory.
    """
    package_root = Path(flyte_agent_factory.__file__).resolve().parent.parent
    command = [
        sys.executable,
        "-P",
        "-m",
        "flyte_agent_factory._slow_lane",
        "--no-local",
        "--junit",
        str(junit),
        *(["--result", str(result)] if result is not None else []),
        *lane_args,
    ]
    # The scrubbed environment is what `checks.sh` gets (`_session_setup._session_env`, same function), and
    # it keeps what the launcher needs to attach to the run.
    return command, _secret_env.child_env(PYTHONPATH=str(package_root), **{SLOW_LANE_ENV: "child"})


def write_lane_launcher(directory: Path, image: str = "") -> Path:
    """Write the executable `TEST_LANE_ENV` names into `directory`; return its path.

    It is the launcher of `_launcher_command` made callable by a script, so any project's `checks.sh` can use
    it: the supervisor's interpreter and engine, never the project's (`-P` keeps the cwd off `sys.path`, and
    the insert, unlike `PYTHONPATH`, does not leak into the project's own pytest when the lane falls back to
    running locally). It needs nothing from the project's environment.

    `image` is the run's own image (`_run_image`): the child must use it, as `FLYTE_INTERNAL_IMAGE` here is
    the plain Flyte image without git. Without it the child would fail setup, so the launcher does not offer
    the child and runs the lane here. Only the image name is written: no credential or other env value.
    """
    package_root = Path(flyte_agent_factory.__file__).resolve().parent.parent
    code = (
        f"import sys; sys.path.insert(0, {str(package_root)!r}); "
        "from flyte_agent_factory._slow_lane import main; sys.exit(main())"
    )
    directory.mkdir(parents=True, exist_ok=True)
    launcher = directory / "test-lane"
    if image:
        mode = f"AGENT_FACTORY_SLOW_LANE=child FLYTE_INTERNAL_IMAGE={shlex.quote(image)}"
        setup = ""
    else:
        mode, setup = "", "unset AGENT_FACTORY_SLOW_LANE\n"
    launcher.write_text(
        f'#!/bin/sh\n{setup}{mode} exec {shlex.quote(sys.executable)} -P -c {shlex.quote(code)} "$@"\n'
    )
    launcher.chmod(0o755)
    return launcher


def _run_head_pytest(project: Path, child_lanes: bool = False) -> HeadRun | None:
    """Run the head tree's whole suite once, as the project's interpreter would, and read its junit.

    A project with a slow lane runs it beside the rest in a second process: the two share the CPUs but
    not a queue, so the Terraform and benchmark tests no longer run after everything else. With
    `child_lanes`, a project that has a slow lane, a `uv.lock` and `tests/` sends each lane to a child
    action first (the deployed launcher), and runs here only a lane the launcher could not place.
    Returns None when pytest is not installed, which the rules report as an image defect themselves.
    """
    env = _secret_env.child_env(PYTHONPATH=str(project / "src") + os.pathsep + str(project))
    python = project / ".venv/bin/python"
    interpreter = str(python) if python.is_file() else sys.executable
    pyproject = project / "pyproject.toml"
    slow_lane = pyproject.is_file() and _SLOW_MARKER.search(pyproject.read_text())
    lanes = [["-m", "not slow"], ["-m", "slow"]] if slow_lane else [[]]
    in_children = (
        child_lanes and bool(slow_lane) and (project / "uv.lock").is_file() and (project / "tests").is_dir()
    )
    image = ""
    skipped_children = ""
    if in_children:
        image = _run_image(project, _launcher_command(Path(), [])[1])
        if not image:
            in_children = False
            skipped_children = "child lanes not used (the run's image could not be looked up): ran here\n"
    with tempfile.TemporaryDirectory(prefix="factory-junit-") as scratch:

        def lane(number: int) -> tuple[int, str, float, bool]:
            started = time.monotonic()
            junit = Path(scratch) / f"{number}.xml"
            note = skipped_children
            if in_children:
                result = Path(scratch) / f"{number}.json"
                launcher, launcher_env = _launcher_command(junit, lanes[number], result)
                launcher_env["FLYTE_INTERNAL_IMAGE"] = image
                try:
                    code, output = _gate_base._run(
                        launcher, project, env=launcher_env, timeout=_gate_base.TEST_TIMEOUT, tail=None
                    )
                except OSError as exc:
                    code, output = 127, f"launcher could not start: {exc}"
                # Completion survives report/download failures and a later launcher crash. Junit alone
                # cannot distinguish a pytest failure from an import failure in the launcher.
                try:
                    completion = json.loads(result.read_text())
                    if (
                        not isinstance(completion, dict)
                        or completion.get("completed") is not True
                        or type(completion.get("code")) is not int
                        or completion["code"] not in range(6)
                    ):
                        raise ValueError("invalid pytest completion")
                except FileNotFoundError:
                    evidence = "missing pytest completion"
                except (OSError, ValueError):
                    evidence = "invalid pytest completion"
                else:
                    return completion["code"], output, time.monotonic() - started, False
                # The launcher's own first `slow lane:` line is the reason (cannot attach, no capacity, setup
                # failed); the Flyte controller logs ahead of it.
                said = [line for line in output.splitlines() if line.startswith("slow lane:")]
                reason = said[0] if said else (output.strip().splitlines() or ["no output"])[-1]
                if code != _LANE_NOT_RUN:
                    reason = f"exit {code}: {reason}"
                    reason += f"; {evidence}"
                note = f"child lane not run ({reason}): ran here\n"
                # A failed launcher can leave a partial report; only the local run may supply evidence now.
                junit.unlink(missing_ok=True)
            command = [interpreter, "-m", "pytest", "-q", f"--junitxml={junit}", *lanes[number]]
            code, output = _gate_base._run(
                command, project, env=env, timeout=_gate_base.TEST_TIMEOUT, tail=None
            )
            return code, note + output, time.monotonic() - started, "No module named pytest" in output

        with concurrent.futures.ThreadPoolExecutor(len(lanes)) as pool:
            runs = list(pool.map(lane, range(len(lanes))))
        try:
            cases = [
                case for number in range(len(lanes)) for case in parse_junit(Path(scratch) / f"{number}.xml")
            ]
        except (OSError, ElementTree.ParseError):
            cases = None
    output = "\n".join(output for _, output, _, _ in runs)
    if cases is None and any(missing for _, _, _, missing in runs):
        return None
    # pytest exits 5 when a lane selects nothing, which is not a failure while the other lane ran.
    codes = [code for code, _, _, _ in runs if code != 5]
    return HeadRun(max(codes, default=5), output, cases, runs[1][2] if slow_lane else 0.0)


@dataclasses.dataclass
class GateWork:
    """What one gate run shares between its rules, and reports back to the pre-review's timeline.

    `junit_dir` and `checks_started` describe the pytest run `checks.sh` already made. Without a usable
    one, `share_pytest` (the gate will run plain pytest anyway, for a rule that maps it) makes the first
    rule that needs the head tree's tests run them once for all. Without either, each rule runs what it needs.
    """

    junit_dir: Path | None = None
    checks_started: float = 0.0
    #: The test-harness files the round changed: set, the junit `checks.sh` left is not trusted.
    reuse_refused: str = ""
    share_pytest: bool = False
    #: The base the supervisor recorded for the project: the one commit vendored trees are read from locally.
    trusted_base: str = ""
    #: The pre-review sets this: the head run sends its lanes to child actions (see `_run_head_pytest`).
    child_lanes: bool = False
    head_sha: str = ""
    seconds: dict[str, float] = dataclasses.field(default_factory=lambda: {"gate": 0.0, "base": 0.0})
    slow_seconds: float = 0.0
    pytest_reused: bool = False
    base_runs: int = 0
    base_hits: int = 0
    _head: HeadRun | None = None
    _head_done: bool = False

    def head_pytest(self, project: Path) -> HeadRun | None:
        """Return the head tree's tests, from `checks.sh` or one run made here; None when neither exists."""
        if self._head_done:
            return self._head
        self._head_done = True
        if self.junit_dir is not None and self.head_sha and not self.reuse_refused:
            run = read_junit(self.junit_dir, self.checks_started, self.head_sha)
            # A failing run is run again here: its output is what the rules judge and print.
            if run and run.cases and all(case.passed for case in run.cases):
                self._head = HeadRun(
                    0, f"pytest results reused from {self.junit_dir}: {len(run.cases)} passed", run.cases
                )
                self.pytest_reused = True
                self.slow_seconds = run.slow_seconds
                return self._head
        if self.share_pytest:
            self._head = _run_head_pytest(project, self.child_lanes)
            self.slow_seconds = self._head.slow_seconds if self._head else 0.0
        return self._head


def scan_work(project: Path, pytest_junit: Path | None) -> GateWork:
    """Return the shared work of a standards scan: plain pytest runs once, or is read from `pytest_junit`.

    No `checks_started`: CI's artifact downloads reset mtimes, so the `head` file's commit is what ties the
    junit to the tree. When `project` has no commit to name, `head_sha` stays empty and nothing is reused.
    """
    work = GateWork(share_pytest=True)
    if pytest_junit is not None:
        code, head = _gate_base._run(["git", "rev-parse", "HEAD"], project)
        work.junit_dir = pytest_junit
        work.head_sha = "" if code else head.strip()
    return work


#: Permission is a line of its own in the brief. A sentence that merely mentions tests and
#: "skip" or "remove" ("Never skip or weaken tests.") is the opposite of permission.
_REMOVAL_PERMISSION = re.compile(
    r"^\s*(?:[-*]\s*)?(?:Tests may be (?:removed|deleted):|(?:Remove|Delete) tests?:\s*\S)",
    re.IGNORECASE | re.MULTILINE,
)


def fail_to_pass(
    workspace: Path,
    name: str,
    base: str,
    changes: list[str],
    brief: str,
    demo: str,
    note: str,
    commits: list[str] | None = None,
    start: Path | None = None,
    layout: str = "sandbox",
    work: GateWork | None = None,
) -> tuple[int | _gate_base.Status, str]:
    """Run HEAD tests against the start tree's code, after checking tests were not weakened.

    `changes` are the project's net changed paths. `start` is the tree the project started from;
    it is built here from `base` or `commits` when the caller has not already done so.

    Returns an exit code, or `_gate_base.Status.NOT_APPLICABLE` when there is nothing to test.
    """
    if start is None:
        try:
            with _gate_start._start_tree(workspace, base, commits) as built:
                return fail_to_pass(
                    workspace, name, base, changes, brief, demo, note, commits, built, layout, work
                )
        except _gate_start._GateError as exc:
            return 1, str(exc)
    project = _gate_base.project_dir(workspace, name, layout)
    upstreams = _vendored.load(project, _gate_base._SKIP_DIRS, trusted_base=base)
    code, output = _judge_tests(
        workspace, name, layout, project, upstreams, changes, brief, demo, note, start, work
    )
    # Every outcome says which trees were taken as upstream's, and why any could not be: a passing rule too.
    return code, "\n".join(filter(None, [output, upstreams.describe()]))


def _judge_tests(
    workspace: Path,
    name: str,
    layout: str,
    project: Path,
    upstreams: _vendored.Upstreams,
    changes: list[str],
    brief: str,
    demo: str,
    note: str,
    start: Path,
    work: GateWork | None,
) -> tuple[int | _gate_base.Status, str]:
    """Return `fail_to_pass`'s code and output for a start tree that exists."""

    def project_relative(path: str) -> str:
        return os.path.relpath(workspace / path, project)

    # A vendored file identical to upstream is upstream's, not a change of the project's: it is neither
    # weakened nor a test the project wrote. One the start tree has is the project's, judged against start.
    changes = [p for p in changes if (start / p).exists() or not upstreams.unchanged(project_relative(p))]
    tests = [p for p in changes if _is_test(p)]
    permits_removal = bool(_REMOVAL_PERMISSION.search(brief))
    if not permits_removal:
        # A test file upstream has, missing from a directory the copy has: no diff shows a file that was never
        # committed, so it is found here. One the start tree had is a change, judged below.
        for omitted in upstreams.omitted_tests:
            path = os.path.relpath(project / omitted, workspace)
            if not (start / path).exists():
                return 1, f"tests removed, deleted or skipped without brief authorization: {path}"
    for path in tests:
        before = (start / path).read_text(errors="replace") if (start / path).is_file() else None
        if before is None and (upstream := upstreams.read_upstream(project_relative(path))) is not None:
            # No start copy of a vendored test: what it was is what upstream has.
            before = upstream.decode(errors="replace")
        after = (workspace / path).read_text(errors="replace") if (workspace / path).is_file() else None
        if permits_removal or before is None and after is None:
            continue
        suffix = Path(path).suffix
        try:
            removed = (
                after is None
                or before is not None
                and suffix == ".py"
                and bool(_test_names(before) - _test_names(after))
            )
            added_skips = _count_skips(after or "", suffix) > (
                0 if before is None else _count_skips(before, suffix)
            )
        except SyntaxError as exc:
            return 1, f"invalid test syntax: {path}: {exc}"
        if removed or added_skips:
            return 1, f"tests removed, deleted or skipped without brief authorization: {path}"
    if demo:
        return 0, "demo: production fail-to-pass gate exempt; the demo rules apply instead"
    # A refactor preserves behaviour that tests describe; a project that started with none has
    # nothing to preserve, so it owes tests like any other new code.
    started_with_tests = any(
        _is_test(p) for p in _gate_base._project_files(_gate_base.project_dir(start, name, layout))
    )
    if started_with_tests and re.search(r"refactor:\s*no behaviour change", note, re.IGNORECASE):
        return 0, "EXEMPT: refactor: no behaviour change"
    tests = [p for p in tests if (workspace / p).is_file() and Path(p).name != "conftest.py"]
    if not tests:
        # New Python projects owe behaviour tests too; lack of a framework is not an exemption.
        # Terraform and docs changes have no Python behaviour to test.
        if not any(Path(p).suffix == ".py" and not _is_test(p) for p in changes):
            return _gate_base.Status.NOT_APPLICABLE, "no Python source changes"
        return (
            1,
            "no tests for new behaviour; no added or changed behaviour tests; "
            "declare refactor: no behaviour change only for a refactor",
        )
    relative = [os.path.relpath(workspace / p, project) for p in tests]
    # The session wrote these tests: they run with what the session has, never the supervisor's secrets.
    env = _secret_env.child_env(PYTHONPATH=str(project / "src") + os.pathsep + str(project))
    python = project / ".venv/bin/python"
    interpreter = str(python) if python.is_file() else sys.executable
    command = [interpreter, "-m", "pytest", "-q", *relative]
    python_tests = all(Path(p).suffix == ".py" for p in tests)
    if not python_tests:
        script = workspace / _gate_base.checks_rel(name, layout)
        if not script.is_file():
            # The sandbox script is `<name>/checks.sh`; its message predates the root layout and stays.
            missing = _gate_base.checks_rel(name, layout) if layout == "root" else "checks.sh"
            return 127, f"no project {missing} to run non-Python tests"
        command = ["bash", str(script)]
    # The head tree's tests may already have run (checks.sh, or the one run a pytest rule shares): the changed
    # tests' outcomes are read from it rather than run again.
    shared = work.head_pytest(project) if work is not None and python_tests else None
    if shared is None or shared.cases is None or not _changed_tests_pass(shared.cases, relative):
        code, output = _gate_base._run(
            command, project if python_tests else workspace, env=env, timeout=_gate_base.TEST_TIMEOUT
        )
        if "No module named pytest" in output:
            return 127, "pytest is not installed"
        if code == 127:
            return 127, "project test command: " + output
        if code:
            return 1, f"head tests fail: {shlex.join(command)}\n{output}"
    for path in [p for p in changes if _is_test(p) and (workspace / p).is_file()]:
        target = start / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(workspace / path, target)
    base_project = _gate_base.project_dir(start, name, layout)
    env = _secret_env.child_env(PYTHONPATH=str(base_project / "src") + os.pathsep + str(base_project))
    base_command = command
    if not python_tests:
        # The head runner is test infrastructure; its relative paths must resolve inside base.
        runner = start / _gate_base.checks_rel(name, layout)
        runner.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, runner)
        base_command = ["bash", str(runner)]
    code, output = _cached_run(
        start,
        tree_key(start),
        "factory:fail-to-pass",
        base_command,
        lambda: _gate_base._run(
            base_command, base_project if python_tests else start, env=env, timeout=_gate_base.TEST_TIMEOUT
        ),
        work,
    )
    missing_base_code = code == 2 and ("ModuleNotFoundError:" in output or "ImportError:" in output)
    if code != 1 and not missing_base_code:
        return 1, f"base must have a failing test (exit 1), observed exit {code}\n{output}"
    return 0, f"head tests pass; base tests fail\n{output}"
