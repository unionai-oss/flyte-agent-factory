"""The grant gate: the only way a session reaches a signed-in (personal) browser.

A personal browser acts as its owner, so a session must not hold its bearer token. The supervisor runs
this gate on 127.0.0.1 inside its own (non-dumpable) process, next to the bridge; the session's MCP config
points at `http://127.0.0.1:<port>/<identity>/mcp`, and the gate adds the token from the supervisor's
environment as it proxies to the identity's app. Nothing here writes the token anywhere.

What the gate enforces, per run:

- Only the identities in the run's grant (`AGENT_FACTORY_BROWSER_IDENTITIES`) are routed; any other path or
  identity is answered 403 and never reaches an app.
- Reading tools pass. Anything that can act as the owner (a click on something that is not a link, typing,
  `browser_evaluate`, any tool this module does not know) is answered by the gate itself with an error result
  telling the session to ask the owner (`ask-human --approve --browser <identity> ...`). The bridge calls
  `Gate.approve` when the owner says yes: an in-process call, with no HTTP route that opens an approval.
- Gated calls need no approval while the page is inside the sandbox repo on GitHub.

The gate learns what the page is from the tool results it relays (`Page URL:` and snapshot lines), kept per
MCP session. Only results of tools that really snapshot the page are believed: a page can print anything to
the console, and a forged snapshot there must not turn a button into a "link".

Considered and rejected: aiohttp or httpx (transitive dependencies, not ours, and neither is simpler for a
byte-for-byte relay of a stream); a tool allow-list taken from the app at run time (the gate has to decide
before the app is asked, and an unknown tool must be refused, not discovered).
"""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

from flyte_agent_factory import _github, _profile, browser

#: The run env var that holds the grant (comma-separated identity names; absent or empty is none).
GRANT_ENV = "AGENT_FACTORY_BROWSER_IDENTITIES"
#: Where the session finds the gate; set by the supervisor once the gate listens.
GATE_ENV = "AGENT_FACTORY_BROWSER_GATE"
#: How long an approval lasts.
APPROVAL_S = 600
#: Idle MCP sessions are normal, and a tool call (a page that loads slowly) can take long.
_UPSTREAM_TIMEOUT_S = 3600
#: Bounds what is buffered to read a response; a larger one is relayed but not parsed.
_MAX_PARSED = 16 * 1024 * 1024
_MAX_BODY = 64 * 1024 * 1024

#: Tools that only read, or that are the session's own recording and debugging aids.
ALLOWED_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_wait_for",
        "browser_tabs",
        "browser_console_messages",
        "browser_network_requests",
        "browser_network_request",
        "browser_hover",
        "browser_resize",
        "browser_pdf_save",
        "browser_close",
        "browser_find",
        "browser_highlight",
        "browser_hide_highlight",
        "browser_emulate_media",
        "browser_start_video",
        "browser_stop_video",
        "browser_video_chapter",
        "browser_video_show_actions",
        "browser_video_hide_actions",
        "browser_start_recording",
        "browser_stop_recording",
        "browser_start_tracing",
        "browser_stop_tracing",
    }
)
#: Tools that can act as the owner: named so that the list of Playwright MCP 0.0.83 is fully classified
#: (`tests/test_browser_gate.py`). A tool in neither set is gated too.
GATED_TOOLS = frozenset(
    {
        "browser_type",
        "browser_fill_form",
        "browser_press_key",
        "browser_select_option",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_handle_dialog",
        "browser_evaluate",
        "browser_run_code_unsafe",
        "browser_resume",  # steps the page's scripts under a debugger
        "browser_annotate",  # puts an interactive annotation layer in front of the owner's page
    }
)
CLICK_TOOL = "browser_click"
#: Tools that run arbitrary code in the browser, which can take it anywhere (github.com/settings): the
#: sandbox exemption never covers them. `browser_file_upload` and `browser_drop` hand data to an element of
#: the current page and cannot leave it, so they stay under the exemption.
ALWAYS_APPROVED_TOOLS = frozenset({"browser_evaluate", "browser_run_code_unsafe"})
#: Reading tools that can open a URL. Anything but http(s) (and `about:blank` for navigate) is gated:
#: `javascript:` in the address bar runs script in the owner's page.
_URL_TOOLS = frozenset({"browser_navigate", "browser_tabs"})
#: Tools whose result carries the page header and snapshot of the page itself.
_PAGE_TOOLS = frozenset(
    {
        CLICK_TOOL,
        "browser_navigate",
        "browser_navigate_back",
        "browser_snapshot",
        "browser_tabs",
        "browser_wait_for",
        "browser_hover",
        "browser_type",
        "browser_fill_form",
        "browser_press_key",
        "browser_select_option",
        "browser_drag",
        "browser_drop",
        "browser_file_upload",
        "browser_handle_dialog",
    }
)

#: The headers a client sends that the app needs; nothing else is forwarded (above all not Authorization).
_REQUEST_HEADERS = ("Mcp-Session-Id", "Mcp-Protocol-Version", "Accept", "Content-Type", "Last-Event-ID")
_RESPONSE_HEADERS = ("Content-Type", "Mcp-Session-Id", "Mcp-Protocol-Version", "Cache-Control")

_PAGE_SECTION = re.compile(r"^### Page[^\n]*\n- Page URL: (\S+)", re.MULTILINE)
_SNAPSHOT_HEADER = re.compile(r"^### Snapshot\n", re.MULTILINE)
_LINK_REF = re.compile(r"^\s*- link\b.*\[ref=([\w-]+)\]", re.MULTILINE)
_NAME = re.compile(r"[a-z][a-z0-9-]{0,19}")


def sandbox_slug(repo_url: str) -> str:
    """Return `owner/repo` (lower case) of the sandbox repo URL, or "" when it is not a GitHub one."""
    try:
        return _github.parse_repo(repo_url).lower()
    except _github.GithubError:
        return ""


def in_sandbox(page_url: str, slug: str) -> bool:
    """Whether `page_url` is the sandbox repo on GitHub: exactly it, or below it by `/`, `?` or `#`."""
    if not slug or not page_url:
        return False
    parts = urllib.parse.urlsplit(page_url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com" or parts.port:
        return False
    if parts.username or parts.password:
        return False
    segments = parts.path.lower().split("/")  # "/o/r/x" -> ["", "o", "r", "x"]
    return "/".join(segments[1:3]) == slug


def _url_is_unsafe(call: _Call) -> bool:
    """Whether a reading tool is asked to open something that is not a plain web page."""
    if call.name not in _URL_TOOLS:
        return False
    url = call.arguments.get("url")
    if url is None and call.name == "browser_tabs":
        return False  # list, select and close name no URL
    if not isinstance(url, str):
        return True
    # No stripping or guessing: " javascript:x" is refused here and the browser would trim it.
    return urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https") and url != "about:blank"


def granted_identities(environ: Mapping[str, str], profile: _profile.Profile | None) -> tuple[str, ...]:
    """Return the identity names the run is granted, in the profile's order; names it lacks are dropped."""
    wanted = {n for n in environ.get(GRANT_ENV, "").replace(",", " ").split() if _NAME.fullmatch(n)}
    return tuple(i.name for i in profile.browser.identities if i.name in wanted) if profile else ()


@dataclass
class _Session:
    """What the gate has seen of one MCP session: the page URL and the refs the last snapshot gave links."""

    page_url: str = ""
    link_refs: frozenset[str] = frozenset()


@dataclass
class _Call:
    """A JSON-RPC `tools/call` found in a request."""

    id: Any
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


def _call_of(message: Any) -> _Call | None:
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    params = params if isinstance(params, dict) else {}
    arguments = params.get("arguments")
    return _Call(
        message.get("id"), str(params.get("name", "")), arguments if isinstance(arguments, dict) else {}
    )


class Gate:
    """The gate: an HTTP proxy for the granted identities, and the approvals that loosen it."""

    def __init__(
        self,
        identities: Iterable[str],
        *,
        sandbox_repo: str = "",
        environ: Mapping[str, str],
        clock: Callable[[], float] = time.monotonic,
        endpoint: Callable[[str], str] | None = None,
        public_endpoint: Callable[[str], str] | None = None,
    ) -> None:
        self.identities = tuple(identities)
        self._slug = sandbox_slug(sandbox_repo)
        self._environ = environ
        self._clock = clock
        # Looked up at call time, not bound as defaults: the app's address is resolved per request.
        self._endpoint = endpoint or (lambda identity: browser.identity_endpoint(identity))
        self._public_endpoint = public_endpoint or (
            lambda identity: browser.identity_public_endpoint(identity)
        )
        self._lock = threading.Lock()
        self._approved: dict[str, float] = {}
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._last_url: dict[str, str] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The gate's base URL; valid after `start`."""
        assert self._server is not None, "the gate is not running"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> str:
        """Listen on an ephemeral loopback port, and return `url`."""
        gate = self

        class Handler(_Handler):
            owner = gate

        server = self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        # A short poll: `stop` waits for it, and a project's end should not wait for half a second.
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), name="browser-gate", daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        """Stop listening. Open relays end when their connection does."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- approvals ------------------------------------------------------------------------------------

    def approve(self, identity: str, seconds: float = APPROVAL_S) -> None:
        """Let gated calls through for `identity` for `seconds`. In-process only: no HTTP route calls it."""
        if identity not in self.identities:
            raise ValueError(f"browser identity {identity!r} is not granted to this project")
        with self._lock:
            self._approved[identity] = self._clock() + seconds

    def approved(self, identity: str) -> bool:
        with self._lock:
            return self._approved.get(identity, 0.0) > self._clock()

    def last_page_url(self, identity: str) -> str:
        """Return the page URL the app last reported for `identity` in any session, for the owner's prompt."""
        with self._lock:
            return self._last_url.get(identity, "")

    # -- decisions ------------------------------------------------------------------------------------

    def _session(self, identity: str, session_id: str) -> _Session:
        with self._lock:
            return self._sessions.setdefault((identity, session_id), _Session())

    def needs_approval(self, identity: str, session_id: str, call: _Call) -> bool:
        """Whether `call` must wait for the owner: it can act as them, and nothing excuses it."""
        if self.approved(identity):
            return False
        if call.name in ALLOWED_TOOLS:
            return _url_is_unsafe(call)
        if call.name in ALWAYS_APPROVED_TOOLS:
            return True
        session = self._session(identity, session_id)
        if call.name == CLICK_TOOL and self._is_link(session, call):
            return False
        return not in_sandbox(session.page_url, self._slug)

    @staticmethod
    def _is_link(session: _Session, call: _Call) -> bool:
        # 0.0.83 calls the argument `target` (a ref or a selector); earlier releases called it `ref`.
        # Every one given must be a link: the gate cannot know which the app reads.
        targets = [call.arguments[k] for k in ("ref", "target") if k in call.arguments]
        return bool(targets) and all(isinstance(t, str) and t in session.link_refs for t in targets)

    def refusal(self, identity: str, call: _Call) -> dict[str, Any]:
        """Return the JSON-RPC answer the gate gives a call it holds back."""
        where = self.last_page_url(identity) or "no page yet"
        text = (
            f"Not done: {call.name} acts as the owner of the browser identity '{identity}' (page: {where}), "
            "so the owner has to approve it first. Run "
            f'`ask-human --approve --browser {identity} "<what you will do, and where>"`, '
            "and when it prints `approved`, retry this call. "
            "Reading pages needs no approval, nor does clicking a link the latest `browser_snapshot` showed "
            "(take one first: navigate and click results do not list the links)."
        )
        result = {"content": [{"type": "text", "text": text}], "isError": True}
        return {"jsonrpc": "2.0", "id": call.id, "result": result}

    # -- what the app said ----------------------------------------------------------------------------

    def observe(self, identity: str, session_id: str, tool: str, message: Any) -> None:
        """Learn the page from a response message to a `tools/call` of `tool`."""
        if (
            tool not in _PAGE_TOOLS
            or not isinstance(message, dict)
            or not isinstance(message.get("result"), dict)
        ):
            return
        content = message["result"].get("content")
        texts = (
            [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        )
        text = "\n".join(t for t in texts if isinstance(t, str))
        # The last "### Page" before the snapshot: the sections before it ("### Result", console output) can
        # carry anything a page printed, and the app writes its own after them.
        snapshot = list(_SNAPSHOT_HEADER.finditer(text))
        head = text[: snapshot[-1].start()] if snapshot else text
        pages = _PAGE_SECTION.findall(head)
        session = self._session(identity, session_id)
        with self._lock:
            if pages:
                session.page_url = pages[-1]
                self._last_url[identity] = pages[-1]
            # A page header without a snapshot means the refs of the old snapshot name nothing now.
            if snapshot:
                session.link_refs = frozenset(_LINK_REF.findall(text[snapshot[-1].end() :]))
            elif pages:
                session.link_refs = frozenset()

    def ticket_link(self, identity: str) -> str:
        """Return a one-time view link for `identity`, asking its app with the bearer token."""
        token = self._environ.get(self._token_env(identity), "")
        return browser.view_link_from(self._endpoint(identity), self._public_endpoint(identity), token)

    def prompt_note(self, identity: str) -> str:
        """Return what the owner's approval prompt adds: the identity and the page the gate last saw."""
        page = self.last_page_url(identity) or "none yet"
        return f"\n\n_Browser identity: **{identity}**. Page the gate last saw: {page}._"

    def exit_ready(self, identity: str) -> bool:
        """Whether `identity`'s exit node is up (always true for one without an exit node)."""
        return browser.identity_exit_ready(identity, self.bearer(identity))

    def _token_env(self, identity: str) -> str:
        return _profile.BrowserIdentity(identity, "").token_env

    def bearer(self, identity: str) -> str:
        return self._environ.get(self._token_env(identity), "")


def _batch_refusal(gate: Gate, identity: str, messages: list[Any]) -> list[dict[str, Any]]:
    """Answer a whole JSON-RPC batch one of whose calls is held back: none of it runs.

    Splitting a batch and merging the app's answers back would make the gate a JSON-RPC peer; the clients
    that matter send one message per request, so a batch with a held-back call is refused whole.
    """
    answers: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or "id" not in message:
            continue  # a notification has no answer
        call = _call_of(message)
        if call is not None:
            answer = gate.refusal(identity, call)
            answer["result"]["content"][0]["text"] = (
                "Not run: another call in this batch needs the owner's approval, so the batch was refused. "
                + answer["result"]["content"][0]["text"]
            )
            answers.append(answer)
        else:
            error = {"code": -32600, "message": "batch refused: a call in it needs the owner's approval"}
            answers.append({"jsonrpc": "2.0", "id": message["id"], "error": error})
    return answers


class _Handler(BaseHTTPRequestHandler):
    """One request: route, decide, relay. HTTP/1.0 on purpose: a streamed body ends with the connection."""

    owner: Gate

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the supervisor's log is not a place for request lines

    # -- helpers --------------------------------------------------------------------------------------

    def _send_json(self, status: int, payload: Any, extra: Mapping[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_message(self, accept: str, payload: Any) -> None:
        """Answer as the app would: an event stream if the client accepts one, else plain JSON."""
        if "text/event-stream" not in accept:
            self._send_json(200, payload)
            return
        body = f"event: message\ndata: {json.dumps(payload)}\n\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forbid(self, reason: str) -> None:
        self._send_json(403, {"error": reason})

    def _route(self) -> tuple[str, str] | None:
        """Return (identity, endpoint) for a path the gate serves, else answer 403 and return None."""
        # A web page in a browser on this pod could post to a loopback port. MCP clients send no Origin, and
        # a rebinding page arrives with a Host of its own.
        port = cast(tuple[str, int], self.server.server_address)[1]
        if self.headers.get("Origin") or self.headers.get("Host") not in (
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        ):
            self._forbid("requests from web pages are refused")
            return None
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if len(parts) != 2 or parts[0] not in self.owner.identities or parts[1] not in ("mcp", "view-link"):
            self._forbid("this path or browser identity is not available to this project")
            return None
        return parts[0], parts[1]

    # -- methods --------------------------------------------------------------------------------------

    def do_GET(self) -> None:
        route = self._route()
        if route is None:
            return
        identity, endpoint = route
        if endpoint == "view-link":
            try:
                self._send_json(200, {"url": self.owner.ticket_link(identity)})
            except (OSError, RuntimeError, ValueError) as e:  # the app is down, or has no such identity
                self._send_json(502, {"error": f"could not get a view link: {type(e).__name__}"})
            return
        self._relay(identity, b"", {})

    def do_DELETE(self) -> None:
        route = self._route()
        if route is not None and route[1] == "mcp":
            self._relay(route[0], b"", {})
        elif route is not None:
            self._forbid("this path is read with GET")

    def do_POST(self) -> None:
        route = self._route()
        if route is None:
            return
        identity, endpoint = route
        if endpoint != "mcp":
            self._forbid("this path is read with GET")
            return
        length = (
            int(self.headers.get("Content-Length") or 0)
            if self.headers.get("Content-Length", "0").isdigit()
            else -1
        )
        if not 0 <= length <= _MAX_BODY:
            self._send_json(413, {"error": "request body missing its length, or too large"})
            return
        body = self.rfile.read(length)
        session_id = self.headers.get("Mcp-Session-Id", "")
        accept = self.headers.get("Accept", "")
        calls: dict[Any, str] = {}
        try:
            parsed = json.loads(body)
        except ValueError:  # UnicodeDecodeError too: Node would read lossily what the gate could not judge
            self._send_json(400, {"error": "the request body is not JSON-RPC the gate can read"})
            return
        messages = parsed if isinstance(parsed, list) else [parsed]
        held: list[_Call] = []
        for message in messages:
            call = _call_of(message)
            if call is not None:
                calls[call.id] = call.name
                if self.owner.needs_approval(identity, session_id, call):
                    held.append(call)
        if held:
            if isinstance(parsed, list):
                self._send_message(accept, _batch_refusal(self.owner, identity, messages))
            else:
                self._send_message(accept, self.owner.refusal(identity, held[0]))
            return
        # The app gets the gate's own serialisation of what it judged, not the client's bytes: a duplicate key
        # or an odd escape cannot then mean one thing here and another to the app's parser.
        self._relay(identity, json.dumps(parsed).encode(), calls)

    # -- the relay ------------------------------------------------------------------------------------

    def _relay(self, identity: str, body: bytes, calls: Mapping[Any, str]) -> None:
        owner = self.owner
        token = owner.bearer(identity)
        if not token:
            self._send_json(502, {"error": f"no token for browser identity '{identity}' in this pod"})
            return
        try:
            target = urllib.parse.urlsplit(owner._endpoint(identity).rstrip("/") + "/mcp")
        except RuntimeError as e:
            self._send_json(502, {"error": str(e)})
            return
        headers = {k: v for k in _REQUEST_HEADERS if (v := self.headers.get(k))}
        headers["Authorization"] = f"Bearer {token}"
        connection_class = (
            http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        )
        upstream = connection_class(target.netloc, timeout=_UPSTREAM_TIMEOUT_S)
        try:
            upstream.request(self.command, target.path, body=body or None, headers=headers)
            response = upstream.getresponse()
        except OSError as e:
            upstream.close()
            self._send_json(502, {"error": f"the browser app did not answer: {type(e).__name__}"})
            return
        session_id = self.headers.get("Mcp-Session-Id") or response.getheader("Mcp-Session-Id") or ""
        try:
            self.send_response(response.status)
            for name in _RESPONSE_HEADERS:
                if value := response.getheader(name):
                    self.send_header(name, value)
            if length := response.getheader("Content-Length"):
                self.send_header("Content-Length", length)
            self.end_headers()
            content_type = response.getheader("Content-Type", "")
            watcher = _Watcher(owner, identity, session_id, calls, stream="text/event-stream" in content_type)
            while chunk := response.read1(65536):
                for ready in watcher.feed(chunk):
                    self.wfile.write(ready)
                    self.wfile.flush()
            self.wfile.write(watcher.finish())
        except (OSError, http.client.HTTPException):
            pass  # the client or the app went away mid-stream; nothing to tell either of them
        finally:
            upstream.close()


class _Watcher:
    """Passes a response through while reading the JSON-RPC messages in it.

    An event stream is cut into events and each is read *before* it is handed on, so the gate knows the page
    by the time the client can send its next call. A JSON body is read whole, for the same reason.
    """

    def __init__(self, gate: Gate, identity: str, session_id: str, calls: Mapping[Any, str], *, stream: bool):
        self._gate, self._identity, self._session_id = gate, identity, session_id
        self._calls, self._stream = calls, stream
        self._buffer = b""

    def _read_event(self, raw: bytes) -> None:
        lines = raw.decode(errors="replace").splitlines()
        data = [line[5:].lstrip() for line in lines if line.startswith("data:")]
        if data:
            self._read("\n".join(data))

    def _read(self, text: str) -> None:
        try:
            value = json.loads(text)
        except ValueError:
            return
        for message in value if isinstance(value, list) else [value]:
            if isinstance(message, dict) and message.get("id") in self._calls:
                self._gate.observe(self._identity, self._session_id, self._calls[message["id"]], message)

    def feed(self, chunk: bytes) -> list[bytes]:
        if not self._calls:
            return [chunk]
        self._buffer += chunk
        if not self._stream:
            if len(self._buffer) > _MAX_PARSED:
                out, self._buffer, self._calls = self._buffer, b"", {}
                return [out]
            return []
        ready = []
        while (event := re.search(rb"\r?\n\r?\n", self._buffer)) is not None:
            raw, self._buffer = self._buffer[: event.end()], self._buffer[event.end() :]
            self._read_event(raw)
            ready.append(raw)
        return ready

    def finish(self) -> bytes:
        rest, self._buffer = self._buffer, b""
        if rest and self._calls:
            self._read_event(rest) if self._stream else self._read(rest.decode(errors="replace"))
        return rest


def start(profile: _profile.Profile | None, environ: Mapping[str, str]) -> Gate | None:
    """Start the gate for this run's grant, or return None when it grants nothing."""
    names = granted_identities(environ, profile)
    if not names:
        return None
    gate = Gate(names, sandbox_repo=profile.repo if profile else "", environ=environ)
    gate.start()
    return gate
