"""The credential grant store and its loopback server: what a project's supervisor holds for its session.

`GrantStore` opens sealed credentials (`_grant_seal`), keeps them in memory only until their expiry, and
tells the session when one ends. `GrantServer` hands them to the session's own commands on `127.0.0.1`
with the AWS container-credentials protocol, so the AWS CLI, boto3, Terraform and the Go SDK use them and
refresh them without the value ever being in an environment variable or a file. The design is in
`docs/credential-grants.md`.

Nothing here prints, logs or persists a value: `GrantError` messages and the state file carry metadata only.
"""

from __future__ import annotations

import asyncio
import hmac
import http.server
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flyte_agent_factory import _grant_seal
from flyte_agent_factory._grant_seal import GrantError

URL_ENV = "AGENT_FACTORY_GRANTS_URL"
TOKEN_ENV = "AGENT_FACTORY_GRANTS_TOKEN"
MAX_GRANT_S = 7 * 24 * 3600
MAX_HOURS = MAX_GRANT_S / 3600
WARN_BEFORE_S = 15 * 60
#: A sealed text older than this, or further in the future than `_MAX_SKEW_S`, was not sealed just now.
_MAX_SEAL_AGE_S = 24 * 3600
_MAX_SKEW_S = 10 * 60

_LONG_LIVED = (
    "that is a long-lived access key, which is not accepted. Give a temporary credential instead: copy the "
    "'Access keys' block from the IAM Identity Center portal, or run `aws sts assume-role` and paste its "
    "JSON, or as a last resort `aws sts get-session-token`. See docs/credential-grants.md."
)
_ACCESS_KEY_ID = re.compile(r"ASIA[A-Z0-9]{16}")
_SECRET = re.compile(r"[A-Za-z0-9/+=]{40}")
#: Shell lines (`export X=`, `SET X=`, `$Env:X=`) and credentials-file lines (`x = `), by lower-cased name.
_LINE = re.compile(r"^(?:export\s+|set\s+|\$env:)?([A-Za-z_]+)\s*=\s*(.*?)\s*;?\s*$", re.IGNORECASE)
_LINE_FIELDS = {
    "aws_access_key_id": "access_key_id",
    "aws_secret_access_key": "secret_access_key",
    "aws_session_token": "session_token",
    "aws_security_token": "session_token",
    "aws_credential_expiration": "expiration",
}
_JSON_FIELDS = {
    "AccessKeyId": "access_key_id",
    "SecretAccessKey": "secret_access_key",
    "SessionToken": "session_token",
    "Expiration": "expiration",
}


class GrantEnded(GrantError):
    """A grant that existed and has ended; the server answers it differently from one never given."""


def format_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class GrantInfo:
    """What is known about a grant: metadata only, never a value."""

    name: str
    kind: str
    granted_at: float
    expires_at: float
    state: str  # "active" | "expired" | "revoked" | "ended"
    reason: str = ""
    region: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "granted": format_utc(self.granted_at),
            "expires": format_utc(self.expires_at),
        }


@dataclass(frozen=True)
class AwsCredential:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expiration: float | None = None


def _parse_expiration(raw: str) -> float | None:
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GrantError("the credential's Expiration is not an ISO-8601 time") from exc
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _read_json_fields(text: str) -> dict[str, str]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GrantError("the credential looks like JSON but is not valid JSON") from exc
    if isinstance(data, dict) and isinstance(data.get("Credentials"), dict):
        data = data["Credentials"]
    if not isinstance(data, dict):
        raise GrantError("no AWS access key id or secret access key found in the credential")
    return {found: str(data[name]) for name, found in _JSON_FIELDS.items() if data.get(name) is not None}


def _read_line_fields(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        match = _LINE.match(line)
        if match and match.group(1).lower() in _LINE_FIELDS:
            found[_LINE_FIELDS[match.group(1).lower()]] = _unquote(match.group(2))
    return found


def parse_aws(text: str, now: float | None = None) -> AwsCredential:
    """Parse the AWS credential formats an owner is likely to paste; refuse anything but a temporary one.

    Messages never quote any part of the input.
    """
    found = _read_json_fields(text) if text.strip().startswith("{") else _read_line_fields(text)
    key_id, secret, token = (
        found.get(n, "") for n in ("access_key_id", "secret_access_key", "session_token")
    )
    if not key_id or not secret:
        raise GrantError(
            "no AWS access key id and secret access key found in the credential; paste the 'export "
            "AWS_...' lines, a credentials-file block or the JSON of `aws sts assume-role`"
        )
    if key_id.startswith("AKIA") or not token:
        raise GrantError(_LONG_LIVED)
    if not _ACCESS_KEY_ID.fullmatch(key_id):
        raise GrantError("the access key id is not a temporary one (it must look like ASIA + 16 characters)")
    if not _SECRET.fullmatch(secret):
        raise GrantError("the secret access key is not 40 characters of letters, digits, / + =")
    expiration = _parse_expiration(found.get("expiration", ""))
    if now is not None and expiration is not None and expiration <= now:
        raise GrantError(f"the credential expired at {format_utc(expiration)}; give a current one")
    return AwsCredential(key_id, secret, token, expiration)


@dataclass
class _Grant:
    """One grant. The secret and token are bytearrays so removal can overwrite them."""

    name: str
    kind: str
    granted_at: float
    expires_at: float
    region: str = ""
    state: str = "active"
    reason: str = ""
    access_key_id: str = ""
    secret: bytearray = field(default_factory=bytearray, repr=False)
    token: bytearray = field(default_factory=bytearray, repr=False)
    warned: bool = False

    def info(self) -> GrantInfo:
        return GrantInfo(
            self.name, self.kind, self.granted_at, self.expires_at, self.state, self.reason, self.region
        )

    def wipe(self) -> None:
        for buf in (self.secret, self.token):
            buf[:] = bytes(len(buf))
            buf.clear()


#: (icon, timeline text, message for the session), sent after the lock is released.
_Event = tuple[str, str, str]


def _build_ended_event(name: str, reason: str) -> _Event:
    return (
        "🔒",
        f"credential {name} ended: {reason}",
        f"Access to credential {name} has ended: {reason}. Do not retry it; if you still need it, ask again "
        f"with `credential-grant request {name}`.",
    )


class GrantStore:
    """The grants one project's supervisor holds. Safe to use from the server's threads and the event loop."""

    def __init__(
        self,
        project: str,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        on_event: Callable[[str, str], None] = lambda icon, text: None,
        notify: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.project = project
        self.key = _grant_seal.KeyPair.generate()
        self._state_path = state_path
        self._clock = clock
        self._on_event = on_event
        self._notify = notify
        self._lock = threading.Lock()
        self._grants: dict[str, _Grant] = {}
        #: Set once by `close`; an Event because `accept` reads it both before and under the lock.
        self._closed = threading.Event()
        #: Digests of the sealed texts this store has granted from (never the texts themselves).
        self._opened: set[str] = set()

    # -- giving ---------------------------------------------------------------------------------------

    def accept(self, sealed: str, *, expected_name: str | None = None) -> GrantInfo:
        if self._closed.is_set():
            raise GrantError("this project is ending; nothing can be granted")
        payload = self.key.open(sealed, self.project)
        # Each sealed text opens once: pasting it again must not undo a revoke or an expiry.
        digest = _grant_seal.compute_seal_id(sealed)
        if digest in self._opened:
            raise GrantError("this sealed credential has already been used; seal it again from a new link")
        name = payload["name"]
        if expected_name is not None and name != expected_name:
            raise GrantError(
                f"this sealed credential is for {name!r}, but credential {expected_name!r} was asked for"
            )
        now = self._clock()
        sealed_at = payload["sealed_at"]
        if now - sealed_at > _MAX_SEAL_AGE_S:
            raise GrantError("this sealed credential is more than a day old; seal it again from a new link")
        if sealed_at - now > _MAX_SKEW_S:
            raise GrantError(
                "this sealed credential claims to be from the future; check the clock and seal it again"
            )
        credential = parse_aws(payload["value"], now)
        expires_at = min(
            payload["expires_at"],
            credential.expiration or math.inf,
            sealed_at + MAX_GRANT_S,
            now + MAX_GRANT_S,
        )
        if expires_at <= now:
            raise GrantError("this credential's expiry is not in the future; seal it again with a later time")
        grant = _Grant(
            name,
            payload["kind"],
            now,
            float(expires_at),
            payload["region"],
            access_key_id=credential.access_key_id,
            secret=bytearray(credential.secret_access_key.encode()),
            token=bytearray(credential.session_token.encode()),
        )
        with self._lock:
            if self._closed.is_set():  # closed while this one was being opened
                grant.wipe()
                raise GrantError("this project is ending; nothing can be granted")
            old = self._grants.get(name)
            if old:
                old.wipe()
            self._grants[name] = grant
            self._opened.add(digest)
            self._persist()
        until = format_utc(grant.expires_at)
        self._emit(
            [
                (
                    "🔑",
                    f"credential {name} (aws) granted until {until}",
                    f"Credential {name} (aws) is granted until {until}. Use it with "
                    f"`credential-grant exec {name} -- aws sts get-caller-identity`.",
                )
            ]
        )
        return grant.info()

    # -- ending ---------------------------------------------------------------------------------------

    def revoke(self, name: str | None = None, *, reason: str = "revoked by the owner") -> list[str]:
        with self._lock:
            ending = [g for g in self._grants.values() if g.state == "active" and name in (None, g.name)]
            events = self._end(ending, "revoked", reason)
        self._emit(events)
        return [g.name for g in ending]

    def tick(self) -> None:
        now = self._clock()
        events: list[_Event] = []
        with self._lock:
            active = [g for g in self._grants.values() if g.state == "active"]
            for grant in active:
                if grant.expires_at <= now:
                    events += self._end([grant], "expired", f"expired at {format_utc(grant.expires_at)}")
                elif not grant.warned and grant.expires_at - now <= WARN_BEFORE_S:
                    grant.warned = True
                    minutes = max(1, math.ceil((grant.expires_at - now) / 60))
                    left = f"{minutes} minute{'s' if minutes != 1 else ''}"
                    until = format_utc(grant.expires_at)
                    events.append(
                        (
                            "⏳",
                            f"credential {grant.name} expires at {until} (in {left})",
                            f"Credential {grant.name} expires at {until} (in {left}). Finish what "
                            f"needs it, or ask for a new one with `credential-grant request {grant.name}`.",
                        )
                    )
        self._emit(events)

    async def run(self, stop: asyncio.Event, interval_s: float = 5.0) -> None:
        while not stop.is_set():
            self.tick()
            try:
                await asyncio.wait_for(stop.wait(), interval_s)
            except asyncio.TimeoutError:
                pass

    def close(self, reason: str = "the project ended") -> None:
        with self._lock:
            self._closed.set()
            events = self._end([g for g in self._grants.values() if g.state == "active"], "ended", reason)
            self.key.wipe()
        self._emit(events)

    def restore(self) -> None:
        """Mark what the state file records as active as ended: a restart loses the key and every value."""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            recorded = list(json.loads(self._state_path.read_text())["grants"])
        except (OSError, ValueError, KeyError, TypeError):
            return  # an unreadable file holds no value either; there is nothing to say about it
        known: dict[str, _Grant] = {}
        unreadable = 0
        for item in recorded:
            try:
                known[item["name"]] = _Grant(**{k: item[k] for k in _META_FIELDS if k in item})
            except (KeyError, TypeError):
                unreadable += 1  # one bad entry must not hide the others, which may be active
        events: list[_Event] = []
        if unreadable:
            events.append(
                (
                    "⚠️",
                    "grant state partly unreadable",
                    f"{unreadable} recorded credential grant(s) could not be read after the project "
                    "restart; ask again with `credential-grant request <name>` if one is missing.",
                )
            )
        with self._lock:
            for name, grant in known.items():
                if name in self._grants:
                    continue
                self._grants[name] = grant
                if grant.state == "active":
                    grant.state, grant.reason = "ended", "the project restarted"
                    events.append(
                        (
                            "🔒",
                            f"credential {name} ended: the project restarted",
                            f"Credential {name} did not survive the project restart: grants are kept in "
                            "memory "
                            f"only. If you still need it, ask again with `credential-grant request {name}`.",
                        )
                    )
            self._persist()
        self._emit(events)

    def _end(self, grants: list[_Grant], state: str, reason: str) -> list[_Event]:
        """End `grants` (caller holds the lock): wipe the values, record why, and say so."""
        for grant in grants:
            grant.wipe()
            grant.state, grant.reason = state, reason
        if grants:
            self._persist()
        return [_build_ended_event(g.name, reason) for g in grants]

    # -- reading --------------------------------------------------------------------------------------

    def grants(self) -> list[GrantInfo]:
        self.tick()  # as in `aws_response`: past its expiry is never listed active, to exec, scan or report
        with self._lock:
            return sorted((g.info() for g in self._grants.values()), key=lambda g: g.granted_at)

    def aws_response(self, name: str) -> dict[str, str]:
        self.tick()  # an expired grant is never served, even if `run` is late
        with self._lock:
            grant = self._grants.get(name)
            if grant is None:
                raise GrantError(f"no credential named {name} has been granted to this project")
            if grant.state != "active":
                raise GrantEnded(f"credential {name} ended: {grant.reason}")
            return {
                "AccessKeyId": grant.access_key_id,
                "SecretAccessKey": grant.secret.decode(),
                "Token": grant.token.decode(),
                "Expiration": format_utc(grant.expires_at),
            }

    def live_values(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                value.decode()
                for g in self._grants.values()
                if g.state == "active"
                for value in (g.secret, g.token)
            )

    def report(self) -> dict[str, Any]:
        return {
            "key": self.key.public_b64,
            "kid": self.key.kid,
            "grants": [g.to_dict() for g in self.grants()],
        }

    def link(self, base_url: str, name: str, hours: float) -> str:
        if not _grant_seal.NAME_RE.fullmatch(name):
            raise GrantError(f"{name!r} is not a valid credential name (lower case letters, digits, '-')")
        link = _grant_seal.Link(self.project, name, self.key.public_b64, self.key.kid, hours)
        return _grant_seal.make_link(base_url, link)

    # -- internals ------------------------------------------------------------------------------------

    def _persist(self) -> None:
        """Write the metadata atomically, 0600 (caller holds the lock). It never holds a value."""
        if self._state_path is None:
            return
        data = {"grants": [g.info().to_dict() for g in self._grants.values()]}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp, self._state_path)

    def _emit(self, events: list[_Event]) -> None:
        """Tell the timeline and the session; a failing callback must not undo or block an ending."""
        for icon, text, message in events:
            for send, arg in ((self._on_event, (icon, text)), (self._notify, (message,))):
                try:
                    send(*arg)
                except (OSError, ValueError, RuntimeError, TypeError) as exc:  # a callback's own failure
                    print(f"credential grant notice failed: {type(exc).__name__}", file=sys.stderr)


_META_FIELDS = ("name", "kind", "granted_at", "expires_at", "state", "reason", "region")


class GrantServer:
    """Serves a store's grants to the session on 127.0.0.1, behind a per-start bearer token."""

    def __init__(self, store: GrantStore) -> None:
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.url = ""
        self._httpd: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
        httpd.grant_server = self  # type: ignore[attr-defined]  # read by `_Handler`
        self._httpd = httpd
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, name="grant-server", daemon=True).start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def env(self) -> dict[str, str]:
        return {URL_ENV: self.url, TOKEN_ENV: self.token}


class _Handler(http.server.BaseHTTPRequestHandler):
    server: http.server.ThreadingHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        """Log nothing: a request line names the credential, and headers and bodies hold the token."""

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self, grant_server: GrantServer) -> bool:
        header = self.headers.get("Authorization")
        if header is None:
            self._send(401, {"code": "Unauthorized", "message": "an Authorization header is required"})
            return False
        supplied = header.strip().removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), grant_server.token.encode()):
            self._send(403, {"code": "Forbidden", "message": "the token is not valid for this project"})
            return False
        return True

    def _handle(self, method: str) -> None:
        grant_server: GrantServer = self.server.grant_server  # type: ignore[attr-defined]
        if not self._authorised(grant_server):
            return
        store = grant_server.store
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if method == "GET" and parts == ["v1", "grants"]:
            self._send(200, {"grants": [g.to_dict() for g in store.grants()]})
        elif method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "aws"]:
            try:
                self._send(200, store.aws_response(parts[2]))
            except GrantError as exc:
                code = "GrantEnded" if isinstance(exc, GrantEnded) else "NoSuchGrant"
                self._send(404, {"code": code, "message": str(exc)})
        elif method == "POST" and len(parts) == 3 and parts[:2] == ["v1", "revoke"]:
            self._send(200, {"revoked": store.revoke(parts[2], reason="released by the project")})
        else:
            self._send(404, {"code": "NotFound", "message": "no such route"})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


if __name__ == "__main__":
    from flyte_agent_factory._grants_cli import run_seal_cli

    raise SystemExit(run_seal_cli(sys.argv[1:]))
