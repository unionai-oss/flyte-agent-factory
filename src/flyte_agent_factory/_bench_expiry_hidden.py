"""The hidden check of the feature-expiry task: the source of its `test_hidden_expiry.py`."""

HIDDEN = '''\
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
