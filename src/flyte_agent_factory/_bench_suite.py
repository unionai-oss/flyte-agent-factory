"""The factory benchmark's task suite: what each task is, its start, and the hidden check that scores it.

A data module, shipped in the code bundle. The hidden checks live here and never in the bench repo,
so the agents under test cannot read them. Running and scoring a task is `_bench` and `_bench_flow`.

Each case gets an orphan branch containing only the task's pinned start directory, which the agent
copies to `{name}/`. The harness checks that directory's git tree id before launching. The case
branch is scored and deleted afterward; a shared repository key can still fetch other branches,
which is disclosed as contamination_risk.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._bench_expiry_hidden import HIDDEN as _EXPIRY_HIDDEN
    from ._bench_logstats_files import FILES as _LOGSTATS_FILES
    from ._bench_mlpipe_files import HIDDEN as _MLPIPE_HIDDEN
    from ._bench_mlpipe_files import HOLDOUT as _MLPIPE_HOLDOUT
else:
    from flyte_agent_factory._bench_expiry_hidden import HIDDEN as _EXPIRY_HIDDEN
    from flyte_agent_factory._bench_logstats_files import FILES as _LOGSTATS_FILES
    from flyte_agent_factory._bench_mlpipe_files import HIDDEN as _MLPIPE_HIDDEN
    from flyte_agent_factory._bench_mlpipe_files import HOLDOUT as _MLPIPE_HOLDOUT

#: Bumped whenever a task is added, removed or changed; recorded on every case with `digest()`.
SUITE = "v3"
KINDS = ("build", "escalate")


@dataclass(frozen=True)
class Task:
    id: str
    kind: str  # "build": accepted is a passed review; "escalate": accepted is a correct escalation
    brief: str  # `{start}` is `workdir`, `{name}` the case's own directory
    workdir: str  # the start directory in the bench repo
    start_tree: str  # `git rev-parse <commit>:<workdir>` at the pinned start; "" until seeded
    # Written outside the case's directory in the check pod, which gets their directory as `{hidden}`.
    check_files: Mapping[str, str]
    # Run in order from the case's directory with no credentials; the first non-zero exit fails the check.
    check_commands: tuple[tuple[str, ...], ...]
    cap_usd: float
    cap_hours: float
    estimate_usd: float  # the expected cost (design-benchmark.md section 9); the caps bound what is spent
    expected: str  # the outcome a correct factory gets


_SHORTLINKS_HIDDEN = '''\
"""Hidden acceptance for bugfix-shortlinks. `BENCH_PROJECT` is the case's directory."""

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

import pytest


@pytest.fixture
def port(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    server = subprocess.Popen(
        [sys.executable, "-m", "shortlinks", "serve", "--port", str(free), "--db", str(tmp_path / "x.db")],
        cwd=os.environ["BENCH_PROJECT"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while True:
        try:
            socket.create_connection(("127.0.0.1", free), timeout=1).close()
            break
        except OSError:
            if server.poll() is not None or time.monotonic() > deadline:
                server.kill()
                pytest.fail("the service did not start: python -m shortlinks serve --port N --db PATH")
            time.sleep(0.1)
    yield free
    server.terminate()
    server.wait(10)


def call(port, method, path, body=None):
    # http.client, not urllib: a 302 must not be followed off this machine.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        payload = None if body is None else json.dumps(body)
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read()
    finally:
        conn.close()
    try:
        return response.status, json.loads(data) if data else None
    except ValueError:
        return response.status, None


def test_a_deleted_slug_can_be_registered_again(port):
    assert call(port, "POST", "/links", {"url": "https://example.com/a", "slug": "team-offsite"})[0] == 201
    assert call(port, "GET", "/team-offsite")[0] == 302
    assert call(port, "DELETE", "/links/team-offsite")[0] == 204
    status, _ = call(port, "POST", "/links", {"url": "https://example.com/b", "slug": "team-offsite"})
    assert status == 201
    status, link = call(port, "GET", "/links/team-offsite")
    assert status == 200 and link["url"] == "https://example.com/b" and link["visits"] == 0


def test_fifty_concurrent_visits_are_all_counted(port):
    assert call(port, "POST", "/links", {"url": "https://example.com/c", "slug": "spring-promo"})[0] == 201
    # A default ThreadingHTTPServer has a small listen backlog. Bound connects, while keeping
    # enough overlapping requests to expose the seeded read/modify/write race, which has a sleep.
    # A race without that sleep can pass: request concurrency cannot force a thread switch there.
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: call(port, "GET", "/spring-promo")[0], range(200)))
    assert statuses == [302] * 200
    status, link = call(port, "GET", "/links/spring-promo")
    assert status == 200 and link["visits"] == 200


def test_original_tests_are_unchanged():
    project = Path(os.environ["BENCH_PROJECT"])
    originals = {
        "tests/test_api.py": "75e9fd038e5e7dbd076486df5acafbdc79518bd891ec722d06a1b7a0dfd5d207",
        "tests/test_import.py": "f069be7d3ee9def359f03ed59940285d808433783733296e304c0fef1e51e113",
    }
    for name, digest in originals.items():
        path = project / name
        assert path.is_file(), f"original test missing: {name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"original test changed: {name}"


def new_test_files(project):
    tests = project / "tests"
    originals = {"tests/test_api.py", "tests/test_import.py"}
    return sorted(
        str(path.relative_to(project))
        for path in tests.rglob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_test.py"))
        and str(path.relative_to(project)) not in originals
    )


def with_start_main(tmp_path, name, replacements):
    """A copy of the case whose server is the seeded start's, with `replacements` applied to it."""
    project = Path(os.environ["BENCH_PROJECT"])
    copy = tmp_path / name
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns("__pycache__", ".git", ".venv"))
    source = (Path(os.environ["BENCH_HIDDEN"]) / "shortlinks_start_main.txt").read_text()
    for old, new in replacements:
        assert old in source
        source = source.replace(old, new)
    (copy / "shortlinks" / "__main__.py").write_text(source)
    return copy


def test_new_regression_tests_exist():
    assert new_test_files(Path(os.environ["BENCH_PROJECT"])), "add regression tests in new test files"


# The start's code with one bug fixed, so the other is the only reason a new test can fail. The case's
# own code passing the new tests is the visible `pytest` run after this one.
SLUG_BUG_ONLY = [("deleted=True", "deleted=False")]
VISITS_BUG_ONLY = [
    ('visits = ? WHERE slug = ?", (seen + 1, slug)', 'visits = visits + 1 WHERE slug = ?", (slug,)')
]


@pytest.mark.parametrize(
    "bug, fixed_other",
    [("deleted slugs", VISITS_BUG_ONLY), ("lost visits", SLUG_BUG_ONLY)],
)
def test_new_regression_tests_fail_on_each_seeded_bug(tmp_path, bug, fixed_other):
    files = new_test_files(Path(os.environ["BENCH_PROJECT"]))
    assert files, "add regression tests in new test files"
    copy = with_start_main(tmp_path, "mutant", fixed_other)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *files],
        cwd=copy,
        capture_output=True,
        text=True,
        timeout=120,  # Bounds a hung mutant; 25 s timed out with both lanes on a loaded 4-CPU pod.
    )
    assert run.returncode != 0, f"no new test fails with only the {bug} bug: {run.stdout[-1500:]}"
    assert "failed" in run.stdout, run.stdout[-1500:]
'''

# The seeded start's server (tests/fixtures/bench/shortlinks_start), which the mutation check runs the
# agent's new tests against. A test keeps it identical to the fixture.
_SHORTLINKS_START_MAIN = '''\
"""Command line entry point: `python -m shortlinks serve` runs the HTTP service."""

import argparse
import json
import random
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

LOCK = threading.Lock()
# slug, url, created_at, visits
Row = tuple[str, str, str, int]


def serve(port: int, path: str) -> None:
    """Serve the link API on 127.0.0.1:`port`, storing links in the SQLite file at `path`."""
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS links (slug TEXT PRIMARY KEY, url TEXT, created_at TEXT, "
        "visits INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0)"
    )
    db.commit()

    def find(slug: str, *, deleted: bool = False) -> Row | None:
        with LOCK:
            query = "SELECT slug, url, created_at, visits FROM links WHERE slug = ?"
            row: Row | None = db.execute(
                query + ("" if deleted else " AND deleted = 0"), (slug,)
            ).fetchone()
            return row

    def link(row: Row) -> dict[str, Any]:
        return {"slug": row[0], "url": row[1], "created_at": row[2], "visits": row[3]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(
            self, status: int, body: dict[str, Any] | None = None, location: str = ""
        ) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
            )
            slug = body.get("slug") or "".join(
                random.choices(string.ascii_letters + string.digits, k=7)
            )
            if find(slug, deleted=True):
                return self.reply(409, {"error": "slug taken"})
            created = datetime.now(timezone.utc).isoformat()
            with LOCK:
                db.execute("DELETE FROM links WHERE slug = ?", (slug,))
                db.execute(
                    "INSERT INTO links (slug, url, created_at) VALUES (?, ?, ?)",
                    (slug, body["url"], created),
                )
                db.commit()
            self.reply(201, {"slug": slug, "url": body["url"], "created_at": created})

        def do_GET(self) -> None:
            if self.path.startswith("/links/"):
                row = find(self.path.removeprefix("/links/"))
                return (
                    self.reply(200, link(row)) if row else self.reply(404, {"error": "not found"})
                )
            slug = self.path.removeprefix("/")
            row = find(slug)
            if not row:
                return self.reply(404, {"error": "not found"})
            seen = row[3]
            time.sleep(0.02)
            with LOCK:
                db.execute("UPDATE links SET visits = ? WHERE slug = ?", (seen + 1, slug))
                db.commit()
            self.reply(302, location=row[1])

        def do_DELETE(self) -> None:
            slug = self.path.removeprefix("/links/")
            if not find(slug):
                return self.reply(404, {"error": "not found"})
            with LOCK:
                db.execute("UPDATE links SET deleted = 1 WHERE slug = ?", (slug,))
                db.commit()
            self.reply(204)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    """Parse the command line and run the chosen command."""
    parser = argparse.ArgumentParser(prog="shortlinks")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve")
    run.add_argument("--port", type=int, default=8080)
    run.add_argument("--db", default="shortlinks.db")
    args = parser.parse_args()
    serve(args.port, args.db)


main()
'''

_PYTEST = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")

_LOGSTATS_HIDDEN = '''\
"""Hidden acceptance for refactor-logstats. `BENCH_PROJECT` is the case's directory."""

import ast
import hashlib
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(os.environ["BENCH_PROJECT"])
HIDDEN = Path(os.environ["BENCH_HIDDEN"])
CASES = ["case1", "case2", "case3", "case4", "case5", "case6"]
MODULES = ("parse", "aggregate", "render")
MAX_LINES = 200

# The sha256 of every file under tests/ in the start, which the refactor must leave alone.
TESTS_SHA256 = {
    "conftest.py": "11f09a37298a8ffd09dd3f721565e4b07a443af3ecd523a8cc9ec8aed14f32a5",
    "samples.py": "e70450051f831078fb3ad67735f766901c237135019bb525f0515a4157a38a34",
    "test_cli.py": "eb694786ac01186287eff020efc77126b011cec6050f61842746205987b15463",
    "test_parse.py": "d66085bbca571677ee838b93679d0cfde14d909c3c03fb95c4a81ac3e06ad308",
    "test_summary.py": "7909982696c2e152f5d65c924a03466fcfd3f081dc1ecfbc71007be067b17845",
}


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "logstats", *args], cwd=PROJECT, capture_output=True, check=False
    )


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_output_is_byte_identical_to_the_start(case, flags):
    golden = (HIDDEN / f"{case}.{'json' if flags else 'txt'}").read_bytes()
    done = run(str(HIDDEN / f"{case}.log"), "--top", "4", *flags)
    assert done.returncode == (1 if case == "case5" else 0)
    assert done.stderr == b""
    assert done.stdout == golden


@pytest.mark.parametrize("top", [1, 7])
@pytest.mark.parametrize("flags", [(), ("--json",)], ids=["text", "json"])
def test_other_top_values(top, flags):
    done = run(str(HIDDEN / "case6.log"), "--top", str(top), *flags)
    expected = json.loads((HIDDEN / f"case6.top{top}.result").read_text())
    assert (done.returncode, done.stdout.decode(), done.stderr.decode()) == tuple(
        expected["json" if flags else "txt"]
    )


@pytest.mark.parametrize("case", ["missing", "bad_flag", "empty", "top_zero"])
def test_error_and_empty_behavior(case):
    args = {
        "missing": ["missing.log"],
        "bad_flag": [str(HIDDEN / "case1.log"), "--bad-flag"],
        "empty": [str(HIDDEN / "empty.log")],
        "top_zero": [str(HIDDEN / "case1.log"), "--top", "0"],
    }[case]
    expected = json.loads((HIDDEN / f"{case}.result").read_text())
    done = run(*args)
    assert (done.returncode, done.stdout.decode(), done.stderr.decode()) == tuple(expected)


def public_callables(module, *args):
    found = []
    for name, value in vars(module).items():
        own = getattr(value, "__module__", None) == module.__name__
        if name.startswith("_") or not callable(value) or not own:
            continue
        try:
            inspect.signature(value).bind(*args)
        except (TypeError, ValueError):
            continue
        found.append(value)
    return found


def test_modules_perform_their_responsibilities():
    parse, aggregate, render = [importlib.import_module(f"logstats.{name}") for name in MODULES]
    lines = (HIDDEN / "case1.log").read_text().splitlines()
    expected = json.loads((HIDDEN / "case1.json").read_text())
    for module in (parse, aggregate, render):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(alias.name.split(".")[-1] == "cli" for alias in node.names), module.__name__
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "cli", module.__name__
                assert not any(alias.name == "cli" for alias in node.names), module.__name__
    failures = []
    parsers = public_callables(parse, lines[0])
    factories = public_callables(aggregate)
    if not parsers:
        failures.append("parse: no public parser accepting a log line")
    if not factories:
        failures.append("aggregate: no public summary factory accepting no arguments")
    # Invoke public functions by signature: helper renames and record representations are free to change.
    # Each candidate can be an unrelated helper, so a failure must not hide later candidates.
    for parser in parsers:
        try:
            records = [parser(line) for line in lines]
            assert all(record is not None for record in records), "parser returned None for valid input"
        except Exception as exc:
            failures.append(f"{parser.__name__}: {exc!r}")
            continue
        for factory in factories:
            try:
                summary = factory()
                # These are the running-summary operations retained by the original public API.
                for record in records:
                    summary.add_record(record)
                assert summary.parsed == expected["parsed"], "parsed count differs from golden report"
                assert summary.total_bytes == expected["bytes"], "byte total differs from golden report"
                summary = factory()
                for line in lines:
                    summary.add_line(line)
            except Exception as exc:
                failures.append(f"{factory.__name__}: {exc!r}")
                continue
            builders = public_callables(render, summary, 4)
            if not builders:
                failures.append("render: no public report builder accepting a summary and limit")
            for build in builders:
                try:
                    report = build(summary, 4)
                except Exception as exc:
                    failures.append(f"{build.__name__}: {exc!r}")
                    continue
                outputs = []
                renderers = public_callables(render, report)
                if not renderers:
                    failures.append(f"{build.__name__}: no public renderer accepting the report")
                for fn in renderers:
                    try:
                        output = fn(report)
                        if isinstance(output, str):
                            outputs.append(output)
                        else:
                            failures.append(f"{fn.__name__}: returned {type(output).__name__}, expected str")
                    except Exception as exc:
                        failures.append(f"{fn.__name__}: {exc!r}")
                mismatches = []
                for suffix, label in (("txt", "text"), ("json", "JSON")):
                    golden = (HIDDEN / f"case1.{suffix}").read_text()
                    # The CLI may supply the final newline; its byte-exact output is checked separately.
                    if not any(output in (golden, golden.removesuffix("\\n")) for output in outputs):
                        mismatches.append(f"{build.__name__}: {label} output differs from golden report")
                if not mismatches:
                    return
                failures.extend(mismatches)
    pytest.fail(f"parse/aggregate/render do not implement the golden report: {failures}")


def test_the_input_is_split_into_parse_aggregate_and_render_modules():
    missing = [name for name in MODULES if not (PROJECT / "logstats" / f"{name}.py").is_file()]
    assert not missing, f"no logstats/{missing[0]}.py"


def test_every_module_is_at_most_200_lines():
    too_long = {}
    for path in sorted((PROJECT / "logstats").rglob("*.py")):
        count = len(path.read_text().splitlines())
        if count > MAX_LINES:
            too_long[str(path.relative_to(PROJECT))] = count
    assert not too_long, too_long


def test_the_tests_are_unchanged():
    root = PROJECT / "tests"
    found = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not {"__pycache__", ".pytest_cache"} & set(path.parts)
    }
    assert found == TESTS_SHA256
'''

_DOCSKIT_HIDDEN = '''\
"""Hidden acceptance for docs-standards. `BENCH_PROJECT` is the case's directory."""

import ast
import doctest
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(os.environ["BENCH_PROJECT"])
# The start's modules without their docstrings, as ast.unparse prints them: the fix changes no behaviour.
START = {
    "__init__": """""",
    "__main__": """import argparse
import sys
from docskit import stats, text

def main(argv: list[str] | None=None) -> int:
    parser = argparse.ArgumentParser(prog='docskit')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('slug').add_argument('text')
    commands.add_parser('summary').add_argument('numbers', nargs='+', type=float)
    args = parser.parse_args(argv)
    if args.command == 'slug':
        print(text.slugify(args.text))
    else:
        print(f'mean={stats.mean(args.numbers):g} median={stats.median(args.numbers):g}')
    return 0
if __name__ == '__main__':
    sys.exit(main())""",
    "stats": """def mean(values: list[float]) -> float:
    return sum(values) / len(values)

def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2""",
    "text": """import re

def slugify(text: str) -> str:
    return re.sub('[^a-z0-9]+', '-', text.lower()).strip('-')

def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit - 3] + '...'""",
}
DOCSTRING_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def without_docstrings(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, DOCSTRING_OWNERS) and ast.get_docstring(node, clean=False) is not None:
            node.body = node.body[1:]
    return ast.dump(tree)


def test_every_readme_command_runs():
    readme = (PROJECT / "README.md").read_text()
    fences = r"^ {0,3}(`{3,}|~{3,})([^\\n]*)\\n(.*?)^ {0,3}\\1[ \\t]*$"
    blocks = [
        body for _, language, body in re.findall(fences, readme, re.S | re.M)
        if language.strip() in {"", "sh", "bash", "shell", "console"}
    ]
    prose = re.sub(fences, "", readme, flags=re.S | re.M)
    inline = re.findall(r"`([^`\\n]+)`", prose)
    commands = blocks + [s for s in inline if re.match(r"python[0-9.]*\\s+-m\\s+docskit\\s+", s)]
    assert commands, "the README has no fenced shell command or inline CLI example"
    with tempfile.TemporaryDirectory(prefix="docskit-commands-") as tmp:
        directory = Path(tmp)
        calls = directory / "calls.jsonl"
        # Record expanded argv at execution time: shell variables and continuations defeat token scanning.
        shim = (
            f"#!{sys.executable}\\n"
            "import json, os, sys\\n"
            "if sys.argv[1:3] == ['-m', 'docskit']:\\n"
            f"    with open({str(calls)!r}, 'a') as log:\\n"
            "        log.write(json.dumps(sys.argv[3:]) + '\\\\n')\\n"
            f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\\n"
        )
        for name in {"python", "python3", f"python{sys.version_info.major}.{sys.version_info.minor}"}:
            executable = directory / name
            executable.write_text(shim)
            executable.chmod(0o755)
        env = {**os.environ, "PATH": str(directory) + os.pathsep + os.environ.get("PATH", os.defpath)}
        for command in commands:
            script = re.sub(r"^\\$ ", "", command, flags=re.M)
            done = subprocess.run(
                ["bash", "-e", "-c", script], cwd=PROJECT, env=env,
                capture_output=True, text=True, timeout=30
            )
            assert done.returncode == 0, f"{command} exited {done.returncode}: {done.stderr[-500:]}"
        seen = set()
        if calls.exists():
            for line in calls.read_text().splitlines():
                args = json.loads(line)
                if args:
                    seen.add(args[0])
    assert {"slug", "summary"} <= seen, "each CLI subcommand needs a runnable example"
    # Semantic README truthfulness is not machine-verified; only the specific seeded lies are rejected.
    description = prose.lower()
    assert not re.search(r"preserves uppercase|uses underscores|prints only the sum", description)


def test_the_modules_are_the_starts_apart_from_docstrings():
    found = {path.stem for path in (PROJECT / "docskit").glob("*.py")}
    assert found == set(START)
    tests = PROJECT / "tests"
    hashes = {
        str(path.relative_to(tests)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tests.rglob("*")
        if path.is_file() and not {"__pycache__", ".pytest_cache"} & set(path.parts)
    }
    expected = {"test_docskit.py": "9c0d6437e908d1f062eff9ee6cb38d5f5b138cc0f35864c1c100ba76130e4de6"}
    assert hashes == expected, "existing tests changed"
    for name, start in START.items():
        source = (PROJECT / "docskit" / f"{name}.py").read_text()
        assert without_docstrings(source) == without_docstrings(start), f"docskit/{name}.py changed behaviour"


@pytest.mark.parametrize("name", sorted(START))
def test_every_public_function_has_a_docstring(name):
    tree = ast.parse((PROJECT / "docskit" / f"{name}.py").read_text())
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    missing = [f.name for f in functions if not f.name.startswith("_") and not ast.get_docstring(f)]
    assert not missing, f"no docstring: {', '.join(missing)}"
    lies = ("output as a string", "without dividing", "without sorting", "uppercase letters preserved",
            "nothing added", "always returns none")
    for function in functions:
        if function.name.startswith("_"):
            continue
        doc = ast.get_docstring(function).lower()
        assert not any(lie in doc for lie in lies), f"{function.name}: stale claim"
        examples = doctest.DocTestParser().get_examples(ast.get_docstring(function))
        assert examples, f"{function.name}: needs at least one doctest example"
    # Import by package name (pytest's doctest collection skips __main__.py, and doctest's CLI imports
    # its own __main__), then run every example; the `if __name__ == "__main__"` guard does not fire.
    runner = (
        "import doctest, importlib, sys\\n"
        f"result = doctest.testmod(importlib.import_module('docskit.{name}'))\\n"
        "sys.exit(1 if result.failed else 0)\\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", runner], cwd=PROJECT, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stdout + done.stderr
'''

_TFBUCKET_HIDDEN = '''\
"""Hidden Terraform checks share a 150 s budget (BENCH_TF_BUDGET_S widens it only in the engine's tests)."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

project = Path(os.environ["BENCH_PROJECT"])
hidden = Path(os.environ["BENCH_HIDDEN"])
# `terraform test` requires its test directory inside the module. Check formatting before adding fixtures.
tests = ".bench-hidden-tests"
deadline = time.monotonic() + float(os.environ.get("BENCH_TF_BUDGET_S") or 150)
terraform_env = os.environ.copy()
cache = Path(terraform_env.get("TF_PLUGIN_CACHE_DIR", str(Path.home() / ".terraform.d/plugin-cache")))
cache.mkdir(parents=True, exist_ok=True)
terraform_env["TF_PLUGIN_CACHE_DIR"] = str(cache)
# The image's filesystem mirror avoids registry discovery as well as repeated provider downloads.
cli_config = Path("/opt/bench-terraform/terraform.rc")
if cli_config.is_file():
    terraform_env["TF_CLI_CONFIG_FILE"] = str(cli_config)
# Two overrides: the provider pin goes in before `init`; the provider configuration only after the mocked
# `terraform test`, which a real provider block would bypass (its runs then call AWS). Its endpoints point at
# an unroutable address, so nothing in this check can reach AWS even by mistake.
versions = """terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "5.100.0"
    }
  }
}
"""
override = """provider "aws" {
  region                      = "us-east-1"
  access_key                  = "x"
  secret_key                  = "x"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  endpoints {
    s3  = "http://127.0.0.1:9"
    sts = "http://127.0.0.1:9"
  }
}
"""


def terraform(*args, cwd=project, capture=False):
    print("$ terraform " + " ".join(args), flush=True)
    try:
        done = subprocess.run(
            ["terraform", *args], cwd=cwd, env=terraform_env,
            timeout=max(0.1, deadline - time.monotonic()), capture_output=capture, text=True,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit("Terraform hidden check exceeded its time budget") from None
    if done.returncode:
        if capture:
            print(done.stdout, done.stderr, flush=True)
        print(f"terraform {args[0]} failed with exit code {done.returncode}", flush=True)
        raise SystemExit(done.returncode)
    return done.stdout


def iter_resources(module):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from iter_resources(child)


def check_plan(directory, days, outputs, *variables, from_bucket=()):
    with tempfile.TemporaryDirectory(prefix="bench-tf-plan-") as tmp:
        plan = str(Path(tmp) / "bucket.tfplan")
        # All resources are new: disabling provider account/credential probes keeps planning offline.
        terraform("plan", "-input=false", "-no-color", "-refresh=false", f"-out={plan}",
                  *variables, cwd=directory, capture=True)
        data = json.loads(terraform("show", "-json", plan, cwd=directory, capture=True))
    configured_outputs = data["configuration"]["root_module"].get("outputs", {})
    assert outputs <= configured_outputs.keys(), f"{directory}: missing required outputs"
    for name in from_bucket:
        # A constant output has no references; Terraform lists `aws_s3_bucket.this.id` and the resource.
        references = configured_outputs[name].get("expression", {}).get("references", [])
        assert any(ref.split(".")[:2] == ["aws_s3_bucket", "this"] for ref in references), (
            f"output {name} must come from aws_s3_bucket.this"
        )
    lifecycles = [resource for resource in iter_resources(data["planned_values"]["root_module"])
                  if resource["type"] == "aws_s3_bucket_lifecycle_configuration"]
    assert lifecycles, f"{directory}: missing lifecycle configuration"
    for lifecycle in lifecycles:
        rules = lifecycle["values"]["rule"]
        assert len(rules) == 1, "Exactly one lifecycle rule is required"
        rule = rules[0]
        assert rule["status"] == "Enabled", "The lifecycle rule must be enabled"
        assert rule["expiration"][0]["days"] == days, f"{directory}: expected {days}-day expiration"
        assert rule.get("prefix") in (None, ""), "The lifecycle rule must cover every object"
        for item in rule.get("filter") or []:
            assert item.get("prefix") in (None, ""), "The lifecycle rule must cover every object"
            assert not item.get("tags") and not item.get("tag") and not item.get("and"), (
                "The lifecycle rule must not filter by tags or an and block"
            )
            sizes = ("object_size_greater_than", "object_size_less_than")
            assert all(item.get(key) is None for key in sizes), "The rule must not filter by object size"


terraform("fmt", "-check", "-recursive")
example = project / "examples" / "basic"
names = ("zz_bench_versions_override.tf", "zz_bench_override.tf")
reserved = [directory / name for directory in (project, example) for name in names]
assert not any(path.exists() for path in reserved), "Reserved benchmark override already exists"
(project / tests).mkdir()
try:
    (project / "zz_bench_versions_override.tf").write_text(versions)
    for test in hidden.glob("*.tftest.hcl"):
        shutil.copy(test, project / tests / test.name)
    # validate reads tests/ even when the hidden runs use a different test directory.
    # Install the solution's test-run modules before switching init to the hidden tests.
    terraform("init", "-backend=false", "-input=false", "-no-color")
    terraform("validate", "-no-color")
    terraform("init", "-backend=false", "-input=false", "-no-color", f"-test-directory={tests}")
    terraform("test", "-no-color", f"-test-directory={tests}")
    try:
        (project / "zz_bench_override.tf").write_text(override)
        check_plan(project, 30, {"bucket_id", "bucket_arn"}, "-var=bucket_name=bench-bucket",
                   from_bucket=("bucket_id", "bucket_arn"))
        (example / "zz_bench_versions_override.tf").write_text(versions)
        (example / "zz_bench_override.tf").write_text(override)
        terraform("init", "-backend=false", "-input=false", "-no-color", cwd=example)
        check_plan(example, 7, {"bucket_id"})
    except AssertionError as failed:
        # The summary keeps only its tail, and Terraform's warnings are long: name the stage reached.
        raise SystemExit(f"plan check failed after terraform test passed: {failed}") from None
finally:
    for path in reserved:
        path.unlink(missing_ok=True)
    shutil.rmtree(project / tests)
'''

_TFBUCKET_TEST = """\
mock_provider "aws" {}

variables {
  bucket_name = "bench-bucket"
  tags = {
    Environment = "benchmark"
  }
}

run "default_expiration" {
  command = apply

  assert {
    condition     = aws_s3_bucket_versioning.this.versioning_configuration[0].status == "Enabled"
    error_message = "Bucket versioning must be enabled."
  }

  assert {
    condition = (
      aws_s3_bucket_versioning.this.bucket == aws_s3_bucket.this.bucket &&
      aws_s3_bucket_lifecycle_configuration.this.bucket == aws_s3_bucket.this.bucket
    )
    error_message = "Versioning and lifecycle must belong to this bucket."
  }

  assert {
    condition = (
      length(aws_s3_bucket_lifecycle_configuration.this.rule) == 1 &&
      one(aws_s3_bucket_lifecycle_configuration.this.rule).status == "Enabled" &&
      one(aws_s3_bucket_lifecycle_configuration.this.rule).expiration[0].days == 30 &&
      # Not the filter's prefix: it is computed, so the mock provider fills it with a random string.
      try(length(one(aws_s3_bucket_lifecycle_configuration.this.rule).filter[0].tags), 0) == 0 &&
      try(length(one(aws_s3_bucket_lifecycle_configuration.this.rule).filter[0].and), 0) == 0
    )
    error_message = "An enabled rule must expire all current objects after 30 days by default."
  }

  assert {
    condition = (
      aws_s3_bucket.this.bucket == var.bucket_name &&
      aws_s3_bucket.this.tags == var.tags
    )
    error_message = "Keep the bucket inputs working."
  }
}

run "custom_expiration" {
  command = apply

  variables {
    expiration_days = 91
  }

  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.this.rule).expiration[0].days == 91
    error_message = "The lifecycle expiration must follow expiration_days."
  }
}

run "example" {
  command = apply

  module {
    source = "./examples/basic"
  }
}

run "zero_days" {
  command = plan

  variables {
    expiration_days = 0
  }

  expect_failures = [var.expiration_days]
}

run "negative_days" {
  command = plan

  variables {
    expiration_days = -1
  }

  expect_failures = [var.expiration_days]
}

run "fractional_days" {
  command = plan

  variables {
    expiration_days = 1.5
  }

  expect_failures = [var.expiration_days]
}
"""


TASKS: dict[str, Task] = {
    task.id: task
    for task in (
        Task(
            id="bugfix-shortlinks",
            kind="build",
            brief="""\
# Fix two bugs users reported in the link shortener

`{start}/` in this repo is a link-shortener service: stdlib Python, SQLite, an HTTP API run with
`python -m shortlinks serve --port N --db PATH` from inside its directory, and a pytest suite that
starts the real server. Read its README and tests first.

Start by copying it, as a copy rather than a move: `cp -r {start} {name}`. Leave `{start}/` exactly as
it is, and do all your work in `{name}/`. Everything else in the repo is not yours.

## The reports

1. "I deleted my link `team-offsite` and tried to create it again with the same slug. The service
   says 409, slug taken, and nobody else has it." A deleted slug must be free again: `POST /links`
   with it returns 201, and the new link is the one `GET /links/<slug>` shows.
2. "Our campaign link shows fewer visits than our analytics. We sent 50 parallel requests to the short
   URL and the count went up by less than 50." 50 concurrent `GET /<slug>` must leave `visits == 50`.

## The change

Fix both. Add a regression test for each, in new test files, that fails on the code as you found it
and passes after the fix. Keep the existing tests, unmodified. Change no other behaviour.

## Done means

`{name}/ACCEPTANCE.md`, written before you fix anything, gives a verdict per line and the command you ran
for each. `python -m pytest -q` from inside `{name}/` passes. Commit and push it all, then declare
`DONE`. Anything this brief does not say, decide yourself and note it in the README.
""",
            workdir="bench/shortlinks",
            start_tree="4c3549ded73b9768f08e8d4822c4bc924ea1246e",
            check_files={
                "test_hidden_shortlinks.py": _SHORTLINKS_HIDDEN,
                "shortlinks_start_main.txt": _SHORTLINKS_START_MAIN,
            },
            check_commands=((*_PYTEST, "{hidden}"), _PYTEST),
            cap_usd=40.0,
            cap_hours=3.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
        Task(
            id="feature-expiry",
            kind="build",
            brief="""\
# Add link expiry to the link shortener

`{start}/` in this repo is a working link-shortener service: stdlib Python, SQLite, an HTTP API run with
`python -m shortlinks serve --port N --db PATH` from inside its directory, and a pytest suite that
starts the real server. Read its code and tests first.

Start by copying it, as a copy rather than a move: `cp -r {start} {name}`. Leave `{start}/` exactly as
it is, and do all your work in `{name}/`. Everything else in the repo is not yours.

## The change

`POST /links` accepts an optional `"expires_in"`, a JSON integer from 1 to 31536000 inclusive.
Omitting it or sending `null` means no expiry. Anything else returns 400, including `0`, negatives,
`31536001`, booleans (`true` is not 1), floats (including `60.0`) and strings (including `"60"`).

- `expires_at` is `created_at` plus `expires_in` seconds, in exactly the same format as `created_at`,
  or `null`. Both the 201 from `POST /links` and `GET /links/<slug>` include it.
- A link is expired when the current time is at or after `expires_at`. Then `GET /<slug>` and
  `GET /links/<slug>` return 410 Gone with `{"error": ...}`. A request answered with 410 is not a visit.
- Expiry does not free the slug. `POST /links` with an expired link's slug returns 409, as for any
  taken slug, and random generation never returns one. `DELETE /links/<slug>` on an expired link
  returns 204. After that both GETs return 404, and the slug can be registered again.

Add tests for each rule, in new test files. Keep the existing tests, unmodified. Change no other
behaviour. A test must not sleep more than 3 seconds in total.

## Done means

`{name}/ACCEPTANCE.md`, written before you build, gives a verdict per line and the command you ran
for each. `python -m pytest -q` from inside `{name}/` passes. Commit and push it all, then declare
`DONE`. Anything this brief does not say, decide yourself and note it in the README.
""",
            workdir="bench/expiry",
            start_tree="f55524c6a7fe535b64a75c0aa7b2576dd566a892",
            check_files={"test_hidden_expiry.py": _EXPIRY_HIDDEN},
            check_commands=(_PYTEST, (*_PYTEST, "{hidden}")),
            cap_usd=40.0,
            cap_hours=3.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
        Task(
            id="refactor-logstats",
            kind="build",
            brief="""\
# Split the log summariser into modules, without changing what it does

`{start}/` in this repo is `logstats`, a command-line tool that summarises Common Log Format access
logs (`python -m logstats FILE [FILE ...] [--top N] [--json]`, run from inside its directory). All
its code is in one 400-line module, `logstats/cli.py`, and it has a pytest suite. Read the README
and the tests first.

Start by copying it, as a copy rather than a move: `cp -r {start} {name}`. Leave `{start}/` exactly as
it is, and do all your work in `{name}/`. Everything else in the repo is not yours.

## The change

Split `logstats/cli.py` into three new modules and what remains of it:

- `logstats/parse.py`: turning a log line into a record.
- `logstats/aggregate.py`: the running totals and the top lists.
- `logstats/render.py`: the text and JSON output.
- `logstats/cli.py`: reading the input, the arguments and `main`.

Keep the running summary's public `add_line(line)` and `add_record(record)` operations and its
`parsed` and `total_bytes` counters. Parser and rendering helper names are free to change; the
checker discovers public callables by signature, calls the parser with one line, builds a summary
with no arguments, builds a report with `(summary, limit)`, and renders that report with one argument.
These operations must be defined in their responsible modules, rather than imported from `cli.py`.

This is a refactor. The tool's behaviour must not change at all: its stdout, with and without
`--json`, must stay byte-identical, and so must its exit codes and error messages. Every module,
including `cli.py`, must be at most 200 lines. Leave the files under `tests/` exactly as they are:
do not edit, add or delete any; they must pass as they stand.

## Done means

`{name}/ACCEPTANCE.md`, written before you change anything, gives a verdict per line and the command
you ran for each. `python -m pytest -q` from inside `{name}/` passes. Commit and push it all, then
declare `DONE`. Anything this brief does not say, decide yourself and note it in the README.
""",
            workdir="bench/logstats",
            start_tree="b8d30e519f738fe3793efb21394d0fb8b988817e",
            check_files={"test_hidden_logstats.py": _LOGSTATS_HIDDEN, **_LOGSTATS_FILES},
            check_commands=(_PYTEST, (*_PYTEST, "{hidden}")),
            cap_usd=40.0,
            cap_hours=3.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
        Task(
            id="docs-standards",
            kind="build",
            brief="""\
# Bring a small package's documentation up to the team's standards

`{start}/` in this repo is `docskit`: a small stdlib Python package (three modules and a command line)
with a README and a pytest suite. Its documentation has drifted from the code. Read the README and the
modules first.

Start by copying it, as a copy rather than a move: `cp -r {start} {name}`. Leave `{start}/` exactly as
it is, and do all your work in `{name}/`. Everything else in the repo is not yours.

## The change

Bring the README and the docstrings in line with the documentation rules in `standards/`: the README
must be true of the code as it is now, every public function has a docstring, and no docstring says
something the code does not do. Every public function's docstring must include at least one doctest
example; all doctests must run and pass with `python -m pytest --doctest-modules docskit`.
Include a runnable example for each CLI subcommand, fenced or inline. Shell fences may use backticks
or tildes. Examples must invoke `python -m docskit` (or `python3` / the current Python version);
the check records these invocations with a shim on PATH, after shell variables are expanded, to
confirm both subcommands actually run. Keep Python on PATH when running examples.
Change documentation only: no code, no behaviour and no tests.

## Done means

`{name}/ACCEPTANCE.md`, written before you change anything, gives a verdict per line and the command you
ran for each. Every shell command in the README runs and exits 0 from inside `{name}/`, and
`python -m pytest -q` passes there. Commit and push it all, then declare `DONE`.
""",
            workdir="bench/docskit",
            start_tree="17480f62bc055f6984af08855dc2584263645a2b",
            check_files={"test_hidden_docskit.py": _DOCSKIT_HIDDEN},
            check_commands=(_PYTEST, (*_PYTEST, "{hidden}")),
            cap_usd=25.0,
            cap_hours=2.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
        Task(
            id="vague",
            kind="escalate",
            brief="""\
Customers are unhappy with the link shortener. Fix what they're complaining about.

The service is in `{start}/`. Start by copying it: `cp -r {start} {name}`. Leave `{start}/`
exactly as it is, and do all your work in `{name}/`. Everything else in the repo is not yours.
""",
            workdir="bench/shortlinks",
            start_tree="4c3549ded73b9768f08e8d4822c4bc924ea1246e",
            check_files={},
            check_commands=(),
            cap_usd=15.0,
            cap_hours=1.5,
            estimate_usd=5.0,
            expected="escalated",
        ),
        Task(
            id="too-big",
            kind="escalate",
            brief="""\
Implement the Union dataplane Terraform brief in `{start}/BRIEF.md`, and a hosted console
and a migration of existing customers.

Start by copying it: `cp -r {start} {name}`. Leave `{start}/` exactly as it is, and do all your
work in `{name}/`. Everything else in the repo is not yours.
""",
            workdir="bench/too-big",
            start_tree="e95eaee1e79dc2989b51aa98ed78a93418fd49f3",
            check_files={},
            check_commands=(),
            cap_usd=20.0,
            cap_hours=1.5,
            estimate_usd=20.0,
            expected="escalated",
        ),
        Task(
            id="tf-bucket",
            kind="build",
            brief="""\
# Add versioning and lifecycle expiration to the S3 bucket module

`{start}/` contains `tfbucket`, a small Terraform AWS S3 module. Read its README first.
Start by copying it: `cp -r {start} {name}`. Leave `{start}/` exactly as it is, and do all
work in `{name}/`. Everything else in the repo is not yours.

Terraform is not installed in the agent image. Install Terraform 1.9.x as part of this task.

Enable bucket versioning with `aws_s3_bucket_versioning.this`. Add one enabled lifecycle rule
in `aws_s3_bucket_lifecycle_configuration.this` that expires current objects across the whole
bucket. Its expiration days must come from a new `expiration_days` number variable, default
30, accepting positive integers only. Preserve the existing bucket inputs and outputs.
Use `aws_s3_bucket.this.bucket` as the bucket argument for both resources, so their plans can
be checked without computed IDs. Keep the module free of backend
configuration. Add a working `examples/basic` caller with AWS provider configuration, a
seven-day expiration, and a `bucket_id` output. Document the inputs and versioned expiration
semantics in the README. No AWS account or apply is needed: use Terraform's mocked provider
tests to check the plans.

`{name}/ACCEPTANCE.md`, written before you build, gives a verdict per line and the command
you ran for each. `terraform fmt -check -recursive`, `terraform init -backend=false`,
`terraform validate` and your mocked `terraform test` must pass from `{name}/`.
Commit and push it all, then declare `DONE`. Anything this brief does not say, decide yourself
and note it in the README.
""",
            workdir="bench/tfbucket",
            start_tree="d3395fc60d1a2c8fd8cd637f6405b3126d14b1f2",
            check_files={"check_tfbucket.py": _TFBUCKET_HIDDEN, "bucket.tftest.hcl": _TFBUCKET_TEST},
            check_commands=(("python", "{hidden}/check_tfbucket.py"),),
            cap_usd=40.0,
            cap_hours=3.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
        Task(
            id="ml-pipeline",
            kind="build",
            brief="""\
# Turn the churn training script into a local Flyte 2 pipeline

`{start}/` contains a plain Python script, `mlpipe/train.py`, and 2,000 synthetic customer rows.
Read its README and tests first. Start by copying it: `cp -r {start} {name}`. Leave `{start}/`
exactly as it is, and do all your work in `{name}/`. Everything else in the repo is not yours.

Make it a Flyte 2 pipeline: a `flyte.TaskEnvironment` named `env`, with `load`, `train` and `eval`
tasks cached with `env.task(cache="auto")` (equivalent constants or shared decorators are fine).
Expose these task objects as `load`, `train` and `eval` in the package or its modules.
Load the CSV, train a scikit-learn logistic regression,
and evaluate accuracy on separate rows. Keep the CSV and existing tests unchanged and add tests.

Run locally with `python -m mlpipe`, without cluster access. Preserve the script's `--train-csv` and
`--eval-csv` options and its `accuracy=NUMBER` output. Accuracy must be at least 0.85 on a separate
holdout CSV drawn from the same distribution. Also accept `--eval-csv PATH --predictions OUT`
when PATH has only the feature columns (no `churn` label). Write one predicted label (`0` or `1`)
per input row to OUT, in input order, with no header. In this mode no accuracy output is required;
the checker scores those predictions against hidden labels. Use Flyte 2.10.7,
scikit-learn 1.6.1 and pandas 2.2.3, which are available in the check image.

`{name}/ACCEPTANCE.md`, written before you build, gives a verdict per line and the command you ran
for each. `python -m pytest -q` passes. Commit and push it all, then declare `DONE`.
""",
            workdir="bench/mlpipe",
            start_tree="50bfa314c8cc78983f002808a3e88c05125709fd",
            check_files={"test_hidden_mlpipe.py": _MLPIPE_HIDDEN, "holdout.csv": _MLPIPE_HOLDOUT},
            check_commands=(_PYTEST, (*_PYTEST, "{hidden}")),
            cap_usd=40.0,
            cap_hours=3.0,
            estimate_usd=15.0,
            expected="accepted",
        ),
    )
}


def get(task_id: str) -> Task:
    """Return the task `task_id`; ValueError naming the suite's tasks if there is none."""
    try:
        return TASKS[task_id]
    except KeyError:
        raise ValueError(f"no task {task_id!r} in suite {SUITE}; the tasks are {', '.join(TASKS)}") from None


def brief(task: Task) -> str:
    """Return the brief as launched, with `{name}` still in it: the launch substitutes the case's name."""
    return task.brief.replace("{start}", task.workdir)


def digest() -> str:
    """Return a short hash of every task's full spec.

    Two cases are comparable only when their digests match.
    """
    blob = json.dumps({"suite": SUITE, "tasks": [asdict(task) for task in TASKS.values()]}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
