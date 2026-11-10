import json
import re
import shutil
import subprocess
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from flyte_agent_factory import _grant_page
from flyte_agent_factory._grant_seal import PAGE_PATH, GrantError, KeyPair, decode_b64url, encode_b64url, seal

# Built at runtime so the file holds no AWS-key-shaped literal for a secret scanner to flag.
ASIA_ID = "ASIA" + "ABCDEFGHIJKLMNOP"
AKIA_ID = "AKIA" + "ABCDEFGHIJKLMNOP"

PAYLOAD = {
    "name": "aws-marketplace",
    "kind": "aws",
    "expires_at": 2_000_000_000,
    "sealed_at": 1_999_990_000,
    "value": f"export AWS_ACCESS_KEY_ID={ASIA_ID}\nexport AWS_SESSION_TOKEN=tok",
    "region": "us-east-1",
}


def run_node(snippet: str) -> str:
    node = shutil.which("node")
    assert node, "node is required for the page's interop tests (it is on PATH on GitHub's ubuntu runners)"
    done = subprocess.run(
        [node, "-"], input=_grant_page.GRANT_JS + snippet, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def js_seal(keys: KeyPair, project: str, payload: dict[str, Any], kid: str | None = None) -> str:
    args = json.dumps([keys.public_b64, kid or keys.kid, project, payload])
    return run_node(f"sealCredential(...{args}).then(console.log);")


def test_the_pages_seal_opens_with_the_key_pair():
    keys = KeyPair.generate()
    assert keys.open(js_seal(keys, "proj", PAYLOAD), "proj") == PAYLOAD


def test_the_pages_seal_does_not_open_for_another_project_or_key():
    keys = KeyPair.generate()
    sealed = js_seal(keys, "proj", PAYLOAD)
    with pytest.raises(GrantError):
        keys.open(sealed, "other")
    with pytest.raises(GrantError):
        KeyPair.generate().open(sealed, "proj")


def test_the_page_refuses_a_link_whose_key_does_not_match_its_kid():
    keys = KeyPair.generate()
    with pytest.raises(AssertionError, match="this link is damaged"):
        js_seal(keys, "proj", PAYLOAD, kid="0" * 16)


def test_js_and_python_envelopes_have_the_same_shape():
    keys = KeyPair.generate()

    def shape(sealed: str) -> dict[str, Any]:
        envelope = json.loads(decode_b64url(sealed.removeprefix("fgc1.")))
        assert list(envelope) == ["v", "kid", "epk", "iv", "ct"]
        assert (envelope["v"], envelope["kid"]) == (1, keys.kid)
        assert len(decode_b64url(envelope["epk"])) == 65
        assert len(decode_b64url(envelope["iv"])) == 12
        assert len(decode_b64url(envelope["ct"])) > 16  # the GCM tag plus the payload
        assert not any("=" in envelope[part] for part in ("epk", "iv", "ct"))
        return {part: len(decode_b64url(envelope[part])) for part in ("epk", "iv")}

    assert shape(js_seal(keys, "proj", PAYLOAD)) == shape(seal(keys.public_b64, "proj", PAYLOAD))


def test_the_js_base64url_matches_python():
    data = bytes(range(256))
    assert run_node(f"console.log(b64uEncode(Uint8Array.from({list(data)})));") == encode_b64url(data)


@pytest.mark.parametrize(
    "value, error, warning",
    [
        ("", True, False),
        ("   \n", True, False),
        (f"export AWS_ACCESS_KEY_ID={AKIA_ID}\nexport AWS_SECRET_ACCESS_KEY=x", True, False),
        (f"aws_access_key_id = {AKIA_ID}\naws_session_token = t", True, False),
        (f"{AKIA_ID} and {ASIA_ID}", True, False),
        (f"export AWS_ACCESS_KEY_ID={ASIA_ID}\nexport AWS_SESSION_TOKEN=t", False, False),
        (f'{{"AccessKeyId": "{ASIA_ID}", "SessionToken": "t"}}', False, False),
        ("something else entirely", False, True),
    ],
)
def test_client_side_checks(value, error, warning):
    result = json.loads(run_node(f"console.log(JSON.stringify(checkCredential({json.dumps(value)})));"))
    assert bool(result.get("error")) is error
    assert bool(result.get("warning")) is warning


@pytest.mark.parametrize(
    "hours, option", [(4, 4), (3, 2), (5, 4), (10, 8), (11, 12), (48, 24), (100, 72), (1000, 168), (0.5, 1)]
)
def test_expiry_default_is_the_nearest_option(hours, option):
    assert run_node(f"console.log(nearestHours({hours}));") == str(option)


def test_the_page_makes_no_external_requests():
    html = _grant_page.PAGE_HTML
    assert "<title>Give a credential</title>" in html
    assert not re.search(r"https?:|\burl\(|@import", html, re.IGNORECASE)
    assert not re.search(r"\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\b|<link|<img| src=", html)
    assert "style=" not in html  # style-src is hash-pinned, so an inline attribute would be blocked


def test_the_headers_pin_the_inline_script_and_style_and_forbid_connections():
    import base64
    import hashlib

    headers = _grant_page.PAGE_HEADERS
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "connect-src 'none'" in csp and "unsafe-inline" not in csp

    def digest(text: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

    scripts = re.findall(r"<script>(.*?)</script>", _grant_page.PAGE_HTML, re.DOTALL)
    styles = re.findall(r"<style>(.*?)</style>", _grant_page.PAGE_HTML, re.DOTALL)
    assert len(scripts) == len(styles) == 1
    assert f"script-src {digest(scripts[0])};" in csp
    assert f"style-src {digest(styles[0])};" in csp
    assert headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_the_handler_serves_the_page_with_its_headers():
    app = Starlette(routes=[Route(PAGE_PATH, _grant_page.serve_grant_page)])
    response = TestClient(app).get(PAGE_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == _grant_page.PAGE_HTML
    for name, value in _grant_page.PAGE_HEADERS.items():
        assert response.headers[name] == value
