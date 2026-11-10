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
    from ._bench_logstats_files import FILES as _LOGSTATS_FILES
else:
    from flyte_agent_factory._bench_logstats_files import FILES as _LOGSTATS_FILES

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

_EXPIRY_HIDDEN = '''\
"""Hidden acceptance for feature-expiry. `BENCH_PROJECT` is the case's directory."""

import hashlib
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path
import time
from datetime import datetime, timedelta

import pytest


# A real-time HTTP request cannot reliably land on an exact microsecond. The child-only
# clock and random hooks exercise the stdlib paths without depending on module/helper names.
BOOTSTRAP = """
import datetime
import json
import os
import random
import runpy
from pathlib import Path

control = Path(os.environ["BENCH_CONTROL"])
RealDatetime = datetime.datetime
class Clock(RealDatetime):
    @classmethod
    def now(cls, tz=None):
        instant = RealDatetime.fromisoformat(json.loads(control.read_text())["now"])
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)
datetime.datetime = Clock
choices = random.choices
def controlled_choices(*args, **kwargs):
    state = json.loads(control.read_text())
    collision = state.pop("collision", None)
    if collision is not None and kwargs.get("k", 1) == len(collision):
        state["collision_used"] = True
        control.write_text(json.dumps(state))
        return list(collision)
    return choices(*args, **kwargs)
random.choices = controlled_choices
runpy.run_module("shortlinks", run_name="__main__")
"""


@pytest.fixture
def start_service(tmp_path):
    servers = []

    def start(*, controlled=False):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        db = tmp_path / f"{free}.db"
        control = tmp_path / f"{free}.json"
        control.write_text(json.dumps({"now": "2030-01-01T00:00:00+00:00"}))
        env = dict(os.environ, BENCH_CONTROL=str(control))
        command = ["-c", BOOTSTRAP] if controlled else ["-m", "shortlinks"]
        server = subprocess.Popen(
            [sys.executable, *command, "serve", "--port", str(free), "--db", str(db)],
            cwd=os.environ["BENCH_PROJECT"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        servers.append(server)
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", free), timeout=1).close()
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("the service did not start: python -m shortlinks serve --port N --db PATH")
                time.sleep(0.1)
        return free, db, control

    try:
        yield start
    finally:
        for server in servers:
            server.terminate()
            server.wait(10)


@pytest.fixture
def service(start_service):
    port, db, _ = start_service()
    return port, db


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


def test_invalid_expires_in_is_rejected(service):
    port, _ = service
    # The raw JSON bodies, because `true`, `60.0` and `"60"` must reach the server as themselves.
    bad = [0, -1, 31536001, True, False, 60.0, 1.5, "60", "", [60], {"s": 60}]
    for index, value in enumerate(bad):
        request = {"url": "https://example.com/a", "slug": f"bad-{index}", "expires_in": value}
        status, body = call(port, "POST", "/links", request)
        assert status == 400, f"expires_in={value!r} gave {status}"
        assert isinstance(body, dict) and "error" in body
        # A refused request creates nothing.
        assert call(port, "GET", f"/links/bad-{index}")[0] == 404


def test_valid_expires_in_sets_expires_at(service):
    port, _ = service
    for slug, seconds in (("one", 1), ("year", 31536000), ("hour", 3600)):
        request = {"url": "https://example.com/a", "slug": slug, "expires_in": seconds}
        status, link = call(port, "POST", "/links", request)
        assert status == 201, f"expires_in={seconds} gave {status}"
        created = datetime.fromisoformat(link["created_at"])
        assert datetime.fromisoformat(link["expires_at"]) == created + timedelta(seconds=seconds)
        # Same format as created_at, including the UTC offset suffix.
        assert link["expires_at"][-6:] == link["created_at"][-6:]
        if seconds > 1:
            status, again = call(port, "GET", f"/links/{slug}")
            assert status == 200 and again["expires_at"] == link["expires_at"]


def test_no_expiry_means_expires_at_null(service):
    port, _ = service
    status, link = call(port, "POST", "/links", {"url": "https://example.com/a", "slug": "plain"})
    assert status == 201 and link["expires_at"] is None
    request = {"url": "https://example.com/a", "slug": "nulled", "expires_in": None}
    status, link = call(port, "POST", "/links", request)
    assert status == 201 and link["expires_at"] is None
    status, link = call(port, "GET", "/links/nulled")
    assert status == 200 and link["expires_at"] is None
    assert call(port, "GET", "/nulled")[0] == 302


def test_an_expired_link_is_gone_and_its_slug_stays_taken(service):
    port, _ = service
    request = {"url": "https://example.com/a", "slug": "soon", "expires_in": 1}
    assert call(port, "POST", "/links", request)[0] == 201
    assert call(port, "GET", "/soon")[0] == 302
    time.sleep(1.3)
    for path in ("/soon", "/links/soon"):
        status, body = call(port, "GET", path)
        assert status == 410 and isinstance(body, dict) and "error" in body, path
    assert call(port, "POST", "/links", {"url": "https://example.com/b", "slug": "soon"})[0] == 409
    assert call(port, "DELETE", "/links/soon")[0] == 204
    assert call(port, "GET", "/soon")[0] == 404
    assert call(port, "GET", "/links/soon")[0] == 404
    assert call(port, "POST", "/links", {"url": "https://example.com/b", "slug": "soon"})[0] == 201


def test_a_410_is_not_a_visit(service):
    port, db = service
    request = {"url": "https://example.com/a", "slug": "count", "expires_in": 1}
    assert call(port, "POST", "/links", request)[0] == 201
    assert call(port, "GET", "/count")[0] == 302
    status, before = call(port, "GET", "/links/count")
    assert status == 200 and before["visits"] == 1
    time.sleep(1.3)
    assert [call(port, "GET", "/count")[0] for _ in range(3)] == [410] * 3
    assert call(port, "GET", "/links/count")[0] == 410
    # Storage is private: discover visit columns, then fall back to the public count.
    # SQLite's own quoting rules handle identifiers; parameters only quote values.
    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    with sqlite3.connect(db) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({quote(table)})")]
            if "slug" not in columns:
                continue
            visits = next((name for name in columns if "visit" in name.lower()), None)
            if visits is not None:
                row = conn.execute(
                    f"SELECT {quote(visits)} FROM {quote(table)} WHERE slug = ?", ("count",)
                ).fetchone()
                if row is not None:
                    assert row == (1,)
                    return
    # Metadata was observed before expiry above. After expiry the API may expose only
    # an error, so a private counter cannot always be checked without imposing a schema.
    warnings.warn("410 visit count not observable in storage or API; skipped only the stored-count assertion")


def test_expiry_includes_the_exact_boundary(start_service):
    port, _, control = start_service(controlled=True)
    status, link = call(port, "POST", "/links", {
        "url": "https://example.com/a", "slug": "boundary", "expires_in": 1,
    })
    assert status == 201
    assert call(port, "GET", "/boundary")[0] == 302
    state = json.loads(control.read_text())
    if datetime.fromisoformat(link["created_at"]) != datetime.fromisoformat(state["now"]):
        # Other clocks need a real-time probe; exact equality is not deterministic over HTTP.
        warnings.warn("service does not use datetime.now; exact-equality assertion unavailable")
        deadline = datetime.fromisoformat(link["expires_at"]).timestamp()
        while time.time() < deadline:
            time.sleep(0.001)
    else:
        state["now"] = link["expires_at"]
        control.write_text(json.dumps(state))
    for path in ("/boundary", "/links/boundary"):
        status, body = call(port, "GET", path)
        assert status == 410 and isinstance(body, dict) and "error" in body


def test_random_generation_does_not_return_an_expired_slug(start_service):
    port, _, control = start_service(controlled=True)
    status, link = call(port, "POST", "/links", {
        "url": "https://example.com/a", "expires_in": 1,
    })
    assert status == 201
    slug = link["slug"]
    state = json.loads(control.read_text())
    state.update(now=(datetime.fromisoformat(link["expires_at"]) + timedelta(seconds=1)).isoformat())
    control.write_text(json.dumps(state))
    if datetime.fromisoformat(link["created_at"]) != datetime.fromisoformat("2030-01-01T00:00:00+00:00"):
        deadline = datetime.fromisoformat(link["expires_at"]).timestamp()
        while time.time() < deadline:
            time.sleep(0.001)
    assert call(port, "GET", f"/{slug}")[0] == 410
    state["collision"] = slug
    control.write_text(json.dumps(state))
    status, generated = call(port, "POST", "/links", {"url": "https://example.com/b"})
    assert status == 201
    assert generated["slug"] != slug
    assert call(port, "GET", f"/{slug}")[0] == 410
    if not json.loads(control.read_text()).get("collision_used"):
        warnings.warn("generator does not use random.choices; forced-collision assertion unavailable")


def test_original_tests_are_unchanged():
    project = Path(os.environ["BENCH_PROJECT"])
    originals = {
        "tests/test_import.py": "8a459a9cb69059d9b8d606e4f7a3e0d30df5a1b00ef75d7e18f82ef3d08785a3",
    }
    for name, digest in originals.items():
        path = project / name
        assert path.is_file(), f"original test missing: {name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"original test changed: {name}"
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


_MLPIPE_HOLDOUT = """\
tenure_months,monthly_charge,support_calls,churn
68.77,28.59,4,0
8.75,23.58,0,0
21.18,106.53,5,1
64.8,78.59,5,0
36.91,59.69,1,0
11.49,66.83,6,1
11.49,43.72,0,0
7.3,50.78,4,1
65.44,43.58,4,0
2.1,86.38,3,1
18.09,74.28,2,1
27.32,110.73,4,1
69.89,117.69,3,0
67.58,95.52,1,0
67.22,32.14,6,1
69.0,51.87,1,0
70.81,87.24,0,0
42.82,37.52,5,1
1.45,29.56,6,1
16.81,88.78,5,1
69.29,114.44,6,1
10.15,90.8,5,1
22.15,93.09,4,1
43.56,81.02,6,1
36.15,20.31,1,0
39.94,48.32,0,0
31.44,29.93,1,0
51.94,90.81,1,0
58.96,78.57,1,0
49.79,60.54,5,1
28.42,85.57,2,1
21.22,95.82,0,0
28.47,56.59,5,1
51.66,64.99,5,1
28.73,94.23,3,1
63.24,95.69,5,1
64.38,62.19,2,0
58.46,69.61,5,0
58.73,102.27,5,1
65.13,96.41,1,0
71.43,74.87,1,0
19.45,77.19,1,1
12.89,33.88,6,1
62.83,81.33,4,0
53.95,111.32,3,0
1.95,57.87,1,1
3.68,42.48,6,1
67.85,26.05,4,0
43.62,34.21,1,0
9.86,55.65,4,1
49.35,52.76,1,0
7.34,66.9,2,1
35.45,24.72,1,0
63.07,85.38,4,0
57.67,106.13,2,0
23.44,42.39,5,1
70.37,28.04,4,0
25.74,61.24,5,1
3.5,87.76,2,1
44.38,109.29,3,1
6.53,111.29,5,1
30.97,56.25,4,1
13.33,68.21,2,1
21.66,34.82,2,1
43.68,52.27,1,0
35.62,78.57,3,1
64.78,39.44,1,0
24.62,29.83,1,0
17.32,114.99,4,1
54.29,85.09,1,0
62.41,50.35,5,0
22.56,79.3,1,0
11.94,78.25,0,0
30.85,112.23,2,1
45.05,43.33,6,1
23.5,34.34,1,0
46.03,65.51,1,0
18.12,26.39,6,1
24.23,44.53,5,1
11.24,27.69,1,0
61.79,79.68,5,0
27.42,77.38,1,0
68.94,24.67,6,0
3.49,63.15,6,1
11.52,32.66,1,0
7.64,83.18,5,1
64.32,102.05,6,1
44.19,29.03,1,0
56.43,57.08,5,0
38.96,29.77,5,1
13.22,36.91,3,1
22.04,60.84,0,0
11.47,116.05,5,1
57.43,96.33,1,0
64.76,47.45,4,0
8.07,83.42,4,1
47.19,102.24,6,1
18.95,29.43,2,0
39.98,57.21,0,0
23.51,35.56,3,1
67.22,58.49,4,0
19.29,108.06,0,1
69.28,28.62,1,0
5.82,72.01,4,1
9.31,110.61,5,1
52.85,25.49,4,0
52.44,110.12,2,0
2.74,31.51,5,1
48.37,50.67,0,0
29.06,23.69,5,1
27.18,93.63,5,1
16.37,59.63,4,1
34.19,58.11,2,0
42.18,57.05,6,1
49.8,59.52,6,1
39.22,57.4,1,0
28.12,43.91,4,1
3.17,60.33,2,1
34.79,68.86,4,1
30.52,74.08,3,1
41.94,49.23,1,0
46.26,51.41,3,0
52.74,84.28,3,1
55.45,102.17,4,1
13.31,64.69,6,1
28.35,42.87,5,1
69.19,84.6,6,1
44.94,49.12,0,0
22.65,51.69,1,0
17.78,101.19,6,1
67.09,97.08,3,0
39.58,95.48,3,1
71.78,30.88,2,0
37.4,87.55,6,1
3.51,26.9,3,1
20.23,23.21,6,1
57.0,33.8,0,0
21.21,60.49,0,0
55.51,70.07,1,0
62.36,23.53,2,0
15.44,29.85,1,0
31.1,54.64,1,0
7.98,80.02,3,1
53.32,83.8,5,1
43.74,62.39,0,0
30.99,65.39,3,1
19.1,109.85,3,1
15.22,95.38,4,1
16.55,51.54,6,1
64.89,39.04,0,0
54.54,36.2,1,0
67.96,39.99,1,0
18.11,20.05,6,1
33.54,41.55,4,1
47.31,86.12,2,0
60.21,26.09,5,0
20.58,80.89,5,1
1.52,30.7,4,1
52.86,105.72,6,1
49.15,85.58,4,1
11.1,57.62,6,1
25.03,64.65,6,1
56.56,70.58,6,1
16.02,30.61,0,0
45.62,68.44,6,1
33.07,108.52,6,1
52.19,39.76,5,1
57.52,37.05,0,0
33.86,46.19,1,0
19.56,35.91,0,0
33.02,45.12,0,0
29.38,83.8,0,0
14.32,107.41,6,1
69.63,73.31,4,0
2.09,83.65,0,1
63.66,42.48,3,0
27.8,105.2,4,1
35.6,52.07,1,0
26.71,79.37,6,1
34.03,51.18,6,1
65.22,74.03,1,0
44.09,46.48,5,1
30.73,75.1,3,1
58.61,67.77,1,0
42.24,92.88,3,1
28.62,62.35,3,1
32.92,85.39,3,1
66.88,87.25,6,1
58.63,83.37,4,0
70.98,52.31,0,0
50.35,57.17,6,1
35.06,60.57,6,1
68.9,107.4,2,0
19.89,25.35,2,0
34.46,58.36,4,1
60.45,57.98,2,0
31.32,117.75,1,1
44.87,49.85,4,1
51.42,97.87,3,1
52.29,64.65,0,0
15.34,96.23,6,1
36.15,45.87,5,1
66.83,111.21,6,1
15.69,38.41,0,0
27.0,74.37,0,0
3.02,87.64,6,1
68.51,23.52,1,0
61.48,32.3,0,0
3.86,37.13,3,1
44.98,76.52,4,1
7.39,28.04,2,1
32.53,70.23,0,0
59.83,62.85,4,0
48.41,103.72,5,1
62.79,114.43,1,0
57.53,48.23,0,0
41.54,90.28,4,1
58.54,89.45,5,1
38.89,110.73,4,1
14.68,87.79,2,1
30.42,110.12,1,1
12.17,85.72,3,1
37.21,30.92,4,0
35.19,23.08,2,0
4.46,54.43,4,1
38.22,105.49,4,1
44.63,57.79,6,1
1.56,52.33,4,1
59.97,54.23,2,0
14.87,58.79,3,1
67.31,44.53,6,0
48.1,44.68,0,0
28.16,88.89,2,1
4.42,93.81,5,1
17.43,63.68,0,0
59.35,104.24,0,0
61.37,45.04,2,0
13.1,54.86,1,0
4.29,40.56,5,1
40.57,58.58,3,0
67.6,33.38,5,0
12.53,57.82,2,1
43.5,49.57,4,0
2.23,116.29,3,1
52.15,61.64,2,0
59.48,110.48,5,1
42.37,64.4,0,0
67.76,44.11,0,0
50.84,111.7,5,1
12.94,59.8,3,1
56.81,111.87,1,0
60.05,98.36,6,1
10.51,83.22,0,1
19.35,22.73,6,1
35.38,116.17,1,0
65.64,62.58,1,0
11.05,114.35,1,1
61.31,90.26,2,0
4.44,80.6,3,1
63.08,36.81,5,0
39.3,98.84,1,0
3.09,77.8,3,1
38.12,39.86,1,0
59.24,56.76,6,1
55.25,20.98,5,0
10.3,23.26,3,1
10.36,86.61,4,1
40.6,104.22,6,1
6.24,89.08,0,1
70.33,80.87,3,0
63.09,38.18,1,0
36.37,89.17,4,1
10.09,90.37,1,1
46.26,32.35,2,0
6.96,110.71,3,1
7.0,58.9,4,1
56.04,112.56,5,1
24.12,62.01,4,1
20.38,22.59,5,1
6.68,95.2,6,1
43.02,79.51,3,1
33.63,51.21,0,0
29.98,42.49,0,0
12.94,58.64,3,1
65.69,69.88,0,0
58.02,65.36,2,0
16.06,56.28,1,1
7.97,106.49,6,1
66.61,25.43,3,0
45.73,56.4,5,1
7.85,81.7,6,1
43.82,111.16,6,1
65.46,105.91,2,0
33.96,114.69,5,1
51.18,26.0,1,0
25.9,75.23,4,1
67.7,89.61,4,0
57.03,32.76,1,0
49.89,91.05,6,1
37.76,73.15,2,0
19.19,32.89,0,0
34.41,60.5,4,1
64.42,106.72,4,1
32.76,85.2,0,0
31.36,64.13,6,1
38.64,80.22,4,1
42.72,53.04,1,0
15.28,25.9,6,1
66.89,106.19,3,0
8.84,79.89,2,1
3.56,115.79,4,1
20.03,46.54,3,1
63.35,79.52,6,1
62.79,86.52,5,1
56.27,52.76,5,1
66.19,68.88,3,0
38.46,116.92,4,1
64.22,44.35,1,0
50.97,63.38,1,0
12.28,98.8,5,1
29.7,20.26,2,0
58.14,84.46,2,0
50.28,113.04,0,0
69.96,90.68,4,0
19.42,41.62,2,0
69.11,73.48,2,0
30.16,38.24,2,0
10.08,74.75,1,1
15.52,56.36,5,1
37.72,118.52,1,0
3.13,91.63,6,1
66.42,83.84,1,0
13.0,31.55,4,1
42.85,98.45,6,1
49.7,60.39,6,1
61.15,92.53,4,1
37.62,94.31,3,1
27.18,57.22,1,0
13.05,56.75,2,1
71.45,58.2,0,0
8.18,99.42,4,1
63.88,47.44,5,0
45.77,24.72,0,0
63.79,52.42,4,0
67.07,25.72,2,0
20.05,76.91,0,0
2.49,107.45,0,1
14.21,89.35,0,1
34.86,40.81,1,0
30.58,69.79,3,1
52.85,59.38,2,0
64.19,34.07,4,0
49.77,35.28,6,1
58.17,91.4,2,0
59.99,30.14,6,0
35.56,65.82,5,1
13.12,84.32,6,1
23.52,100.96,1,1
48.95,23.28,1,0
6.22,105.89,0,1
63.97,64.59,6,1
33.98,60.52,5,1
5.95,109.72,4,1
12.47,51.18,0,0
15.02,30.05,4,1
10.14,22.65,4,1
59.52,48.48,3,0
60.12,92.19,6,1
40.64,60.49,2,0
41.73,55.48,1,0
18.26,54.36,3,1
70.16,50.67,2,0
1.24,51.96,4,1
20.81,44.67,0,0
56.46,107.76,0,0
25.73,112.5,1,1
43.61,52.77,0,0
46.65,92.63,4,1
32.41,66.27,1,0
22.7,51.15,3,1
52.96,95.11,4,1
59.43,24.62,4,0
13.63,36.7,2,0
68.47,91.88,4,0
6.96,99.72,0,1
22.96,104.92,3,1
25.18,42.72,2,0
44.63,21.02,0,0
65.92,116.92,1,0
6.94,71.02,4,1
42.39,111.07,1,0
29.35,49.79,5,1
51.76,79.24,5,1
71.13,71.86,5,0
17.47,72.93,1,1
42.55,77.35,3,1
12.46,92.6,3,1
49.36,98.26,1,0
2.17,75.17,5,1
36.95,31.56,5,1
38.24,110.5,6,1
34.03,117.78,1,1
18.48,77.87,1,0
57.04,79.74,6,1
11.21,91.89,3,1
53.33,43.9,3,0
52.78,67.15,0,0
59.44,109.41,2,0
23.52,44.23,4,1
63.95,32.76,5,0
50.9,77.46,4,1
71.3,84.55,2,0
19.9,24.64,1,0
65.01,24.16,6,0
47.59,24.17,4,0
29.32,106.52,4,1
70.77,46.64,6,0
23.11,104.19,5,1
37.11,91.14,0,0
27.09,40.54,0,0
52.39,93.71,6,1
19.25,72.64,0,0
66.29,51.05,3,0
71.78,90.64,2,0
71.49,107.69,1,0
6.43,79.79,3,1
28.0,90.69,6,1
51.56,89.98,3,0
65.81,28.56,2,0
9.24,78.72,4,1
62.02,68.81,5,1
9.59,65.92,3,1
17.19,57.85,0,0
66.19,32.0,4,0
71.98,106.25,3,0
67.8,27.26,2,0
26.57,96.81,3,1
36.95,75.42,2,0
3.74,87.89,4,1
7.44,103.13,5,1
36.04,52.61,5,1
32.0,82.13,3,1
6.52,110.96,2,1
5.42,90.38,2,1
56.22,50.54,6,1
56.24,23.51,6,0
58.96,103.47,1,0
20.8,103.83,2,1
43.81,109.66,3,1
21.77,81.66,5,1
31.69,67.6,3,1
17.23,30.3,3,1
58.59,62.27,2,0
1.18,77.65,2,1
33.62,105.8,1,1
4.49,85.15,6,1
58.32,59.25,3,0
1.28,66.28,1,1
6.32,33.2,6,1
70.29,93.67,5,1
12.72,106.87,3,1
63.75,21.72,2,0
64.87,26.22,4,0
5.98,100.42,4,1
51.05,80.6,0,0
13.12,82.7,2,1
8.41,111.85,0,1
7.87,83.25,4,1
23.51,118.36,4,1
11.66,62.04,5,1
44.95,89.94,5,1
61.51,100.79,5,1
9.74,85.38,0,1
12.9,72.91,3,1
17.35,28.48,6,1
44.45,22.14,1,0
53.2,105.47,5,1
23.87,97.0,0,0
46.52,58.11,4,0
53.76,54.14,3,0
47.68,82.41,5,1
63.63,73.51,4,1
23.54,28.67,4,1
40.95,98.77,3,1
53.68,114.23,4,1
23.89,113.94,0,1
27.98,20.81,6,1
1.73,105.03,4,1
2.45,79.36,3,1
68.96,54.44,5,0
1.01,114.25,3,1
6.05,37.49,2,1
66.79,52.84,4,0
27.66,58.02,6,1
44.64,35.56,2,0
5.1,76.97,3,1
11.34,73.36,6,1
5.48,41.78,0,0
8.19,64.3,5,1
15.7,25.62,4,1
15.77,103.53,4,1
60.96,112.97,6,1
42.48,57.04,0,0
63.02,104.17,4,1
6.91,84.31,6,1
21.39,53.86,4,1
55.35,58.29,3,0
23.51,113.28,6,1
23.5,60.36,3,1
41.02,49.06,1,0
12.81,88.54,5,1
21.83,53.86,2,0
"""

_MLPIPE_HIDDEN = '''\
"""Hidden acceptance for ml-pipeline, run without cluster credentials."""

import contextlib
import csv
import functools
import hashlib
import importlib
import io
import json
import os
import pkgutil
import re
import runpy
import subprocess
import sys
import time
import types
from pathlib import Path

import flyte
from flyte._cache.cache import cache_from_request
from flyte._task import TaskTemplate

PROJECT = Path(os.environ["BENCH_PROJECT"])
HIDDEN = Path(os.environ["BENCH_HIDDEN"])
ORIGINAL_HASHES = {
    "mlpipe/train.csv": "e5877512ed689d0c3618b107722ebe339a08351f4da226ac4e8807d1c9fe28ac",
    "tests/test_data.py": "c4ddf6763aac3b14558c614aab10687c23f5df29cb522edc87aef9cf5c9f5fd9",
}
# One budget for every `python -m mlpipe` run together, measured from when this module is collected.
BUDGET_S = 60
STARTED = time.monotonic()
# Loaded by `python -m mlpipe` through PYTHONPATH. flyte's `forward` is what a direct task call runs and
# `execute` is what a local run uses; both are patched on the one class every `env.task` function becomes.
# The record is a file because the pipeline runs in a child process.
SITECUSTOMIZE = """\\
import json
import os


def _record(kind, name):
    with open(os.environ["BENCH_TRACE"], "a") as stream:
        stream.write(json.dumps({"kind": kind, "name": name}) + chr(10))


def _wrap(owner, attribute, kind, label):
    original = getattr(owner, attribute)

    def wrapper(self, *args, **kwargs):
        _record(kind, label(self))
        return original(self, *args, **kwargs)

    setattr(owner, attribute, wrapper)


try:
    from flyte._task import AsyncFunctionTaskTemplate
    from sklearn.linear_model import LogisticRegression
except ImportError:
    pass
else:
    for _attribute in ("forward", "execute"):
        _wrap(AsyncFunctionTaskTemplate, _attribute, "task", lambda task: task.func.__name__)
    _wrap(LogisticRegression, "fit", "fit", lambda model: "LogisticRegression")
"""


def run_cli(*flags, trace=None):
    """Run `python -m mlpipe` inside what is left of the budget; `trace` is where to record what ran."""
    left = BUDGET_S - (time.monotonic() - STARTED)
    assert left > 0, f"the {BUDGET_S} s budget for the whole check is spent"
    env = dict(os.environ)
    if trace:
        shim = trace.parent / "shim"
        shim.mkdir(exist_ok=True)
        (shim / "sitecustomize.py").write_text(SITECUSTOMIZE)
        env.update(BENCH_TRACE=str(trace), PYTHONPATH=os.pathsep.join([str(shim), str(PROJECT)]))
    return subprocess.run(
        [sys.executable, "-m", "mlpipe", *flags], cwd=PROJECT, env=env,
        capture_output=True, text=True, timeout=left,
    )


def printed_accuracy(done):
    assert done.returncode == 0, done.stderr[-1500:]
    score = re.search(r"^accuracy=([0-9.]+)$", done.stdout, re.M)
    assert score, f"expected accuracy=NUMBER, got {done.stdout[-500:]!r}"
    return float(score[1])


@functools.cache
def discover():
    """`(has_env, {"load"|"train"|"eval": task object or None})` across the package and its modules."""
    package = importlib.import_module("mlpipe")
    modules = [package]
    mains = []
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if info.name.rsplit(".", 1)[-1] == "__main__":
            mains.append(info.name)
        else:
            modules.append(importlib.import_module(info.name))

    def found():
        tasks = {}
        for name in ("load", "train", "eval"):
            candidates = [getattr(module, name, None) for module in modules]
            tasks[name] = next((task for task in candidates if isinstance(task, TaskTemplate)), None)
        return tasks

    if mains and not all(found().values()):
        # Run `__main__` under another name so its `if __name__ == "__main__":` guard does not fire.
        # Only done when needed: an unguarded `main()` there would train in this process.
        argv, sys.argv = sys.argv, ["mlpipe"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                namespace = runpy.run_module(mains[0], run_name="mlpipe.discovery")
            modules.append(types.SimpleNamespace(**namespace))
        except (Exception, SystemExit):
            pass
        finally:
            sys.argv = argv
    has_env = any(isinstance(getattr(module, "env", None), flyte.TaskEnvironment) for module in modules)
    return has_env, found()


def test_training_data_and_existing_tests_are_unchanged():
    for name, expected in ORIGINAL_HASHES.items():
        path = PROJECT / name
        assert path.is_file(), f"missing original file: {name}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, f"changed original file: {name}"


def test_train_and_eval_are_cached_flyte_tasks():
    has_env, tasks = discover()
    assert has_env
    for name, task in tasks.items():
        assert task is not None and cache_from_request(task.cache).behavior == "auto", (
            f"missing real cached Flyte task: {name}"
        )


def test_cli_scores_the_hidden_holdout(tmp_path):
    assert 0.85 <= printed_accuracy(run_cli()) <= 1.0
    trace = tmp_path / "trace.jsonl"
    holdout_path = str(HIDDEN / "holdout.csv")
    printed = printed_accuracy(run_cli("--eval-csv", holdout_path, trace=trace))
    assert 0.85 <= printed <= 1.0, printed
    events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    ran = {event["name"] for event in events if event["kind"] == "task"}
    _, tasks = discover()
    for name in ("train", "eval"):
        assert tasks[name].func.__name__ in ran, f"the {name} task never ran; tasks that ran: {sorted(ran)}"
    assert any(event["kind"] == "fit" for event in events), "LogisticRegression.fit was never called"

    with (HIDDEN / "holdout.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    features = [name for name in rows[0] if name != "churn"]
    # Reversing row order exposes CLIs that silently reuse their default evaluation set.
    for index, holdout in enumerate((rows, rows[::-1])):
        evaluation = tmp_path / f"evaluation-{index}.csv"
        predictions = tmp_path / f"predictions-{index}.txt"
        with evaluation.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=features)
            writer.writeheader()
            writer.writerows({name: row[name] for name in features} for row in holdout)
        done = run_cli("--eval-csv", str(evaluation), "--predictions", str(predictions))
        assert done.returncode == 0, done.stderr[-1500:]
        assert predictions.is_file(), "CLI did not write predictions"
        labels = predictions.read_text().splitlines()
        assert len(labels) == len(holdout), "expected one predicted label per evaluation row"
        assert set(labels) <= {"0", "1"}, "predictions must be binary labels, without a header"
        accuracy = sum(label == row["churn"] for label, row in zip(labels, holdout)) / len(holdout)
        assert accuracy >= 0.85, f"independently measured accuracy={accuracy:.6f}"
        if index == 0:
            assert abs(printed - accuracy) <= 0.02, f"printed accuracy={printed}, predictions earn {accuracy}"
'''

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
