import http.client
import json
import socket
import subprocess
import sys
import time
from typing import Any

import pytest


@pytest.fixture
def port(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "shortlinks",
            "serve",
            "--port",
            str(port),
            "--db",
            str(tmp_path / "test.db"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("server did not start")
                time.sleep(0.05)
        yield port
    finally:
        server.terminate()
        server.wait(10)


def call(port: int, method: str, path: str, body: dict[str, str] | None = None) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=json.dumps(body) if body else None)
        response = connection.getresponse()
        data = response.read()
        return response.status, json.loads(data) if data else None
    finally:
        connection.close()


def test_create_inspect_redirect_and_delete(port):
    assert call(port, "POST", "/links", {"slug": "hello", "url": "https://example.com"})[0] == 201
    assert call(port, "POST", "/links", {"slug": "hello", "url": "https://example.com"})[0] == 409
    assert call(port, "GET", "/links/hello")[1]["visits"] == 0
    assert call(port, "GET", "/hello")[0] == 302
    assert call(port, "GET", "/links/hello")[1]["visits"] == 1
    assert call(port, "DELETE", "/links/hello")[0] == 204
    assert call(port, "GET", "/links/hello")[0] == 404
    assert call(port, "GET", "/hello")[0] == 404
