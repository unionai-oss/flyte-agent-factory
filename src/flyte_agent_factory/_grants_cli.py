"""`credential-grant`: the session's command for the credentials its project was granted.

It talks only to the loopback server in `_grants` (`AGENT_FACTORY_GRANTS_URL` / `_TOKEN`) and never prints a
credential value: `exec` points one command at the server instead of putting a value in its environment.
`run_seal_cli` is the no-browser way for an owner to seal one (`python -m flyte_agent_factory._grants seal`).
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from flyte_agent_factory import _grant_seal
from flyte_agent_factory._bridge import _POLL_INTERVAL_S, ANSWERS, REQUESTS, _write_atomic, ensure_dirs
from flyte_agent_factory._grant_seal import GrantError
from flyte_agent_factory._grants import MAX_HOURS, TOKEN_ENV, URL_ENV, parse_aws

#: Every other way an AWS tool could find credentials, removed so the command uses the grant or nothing.
_DROPPED = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CREDENTIAL_FILE",  # botocore's legacy EC2-tools credential file
)
_UNAVAILABLE = "credential grants are not available in this session"


class _CliError(Exception):
    """A failure to report in one plain line, with the exit code to use."""

    def __init__(self, message: str, code: int = 2, reason: str = "") -> None:
        super().__init__(message)
        self.code = code
        #: The grant server's own code for a refusal (`GrantEnded`, `NoSuchGrant`, ...), "" otherwise.
        self.reason = reason


def build_exec_env(
    environ: Mapping[str, str], url: str, token: str, name: str, region: str | None
) -> dict[str, str]:
    """Return the environment for a command using grant `name`: the loopback endpoint, no other source."""
    env = {k: v for k, v in environ.items() if k not in _DROPPED}
    env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] = f"{url.rstrip('/')}/v1/aws/{name}"
    env["AWS_CONTAINER_AUTHORIZATION_TOKEN"] = token
    # Without these an ended grant would fall through to a profile, or to the node's instance role.
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["BOTO_CONFIG"] = "/dev/null"  # else botocore reads ~/.boto or /etc/boto.cfg before the endpoint
    if region:
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"] = region
    return env


def _read_server() -> tuple[str, str]:
    url, token = os.environ.get(URL_ENV), os.environ.get(TOKEN_ENV)
    if not url or not token:
        raise _CliError(_UNAVAILABLE)
    return url.rstrip("/"), token


def _call(method: str, path: str) -> Any:
    url, token = _read_server()
    req = urllib.request.Request(url + path, method=method, headers={"Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # loopback URL from our own env
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except ValueError:
            body = {}
        message, reason = body.get("message", ""), body.get("code", "")
        raise _CliError(
            f"the grant server refused the request ({exc.code}): {message}", reason=reason
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _CliError(f"{_UNAVAILABLE}: the grant server did not answer") from exc


def _list(_args: argparse.Namespace) -> int:
    grants = _call("GET", "/v1/grants")["grants"]
    if not grants:
        print("no credentials granted")
    for g in grants:
        tail = f"expires {g['expires']}" if g["state"] == "active" else g["reason"]
        print(f"{g['name']}  {g['kind']}  {g['state']}  {tail}")
    return 0


def _exec(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise _CliError("usage: credential-grant exec <name> -- <command...>")
    url, token = _read_server()
    grant = next((g for g in _call("GET", "/v1/grants")["grants"] if g["name"] == args.name), None)
    if grant is None:
        raise _CliError(
            f"no credential named {args.name} has been granted; "
            f"ask with `credential-grant request {args.name}`"
        )
    if grant["state"] != "active":
        raise _CliError(
            f"credential {args.name} ended: {grant['reason']}; "
            f"ask again with `credential-grant request {args.name}`"
        )
    env = build_exec_env(os.environ, url, token, args.name, grant.get("region"))
    # Typed as returning: it replaces this process, so the `return 0` below is reached only when it is faked.
    execute: Callable[..., object] = os.execvpe
    try:
        execute(command[0], command, env)
    except OSError as exc:
        raise _CliError(f"cannot run {command[0]}: {exc.strerror}", 127) from exc
    return 0


def _revoke(args: argparse.Namespace) -> int:
    ended = _call("POST", f"/v1/revoke/{args.name}")["revoked"]
    if not ended:
        print(f"no active credential named {args.name}")
        return 1
    print(f"ended: {', '.join(ended)}")
    return 0


def request(args: argparse.Namespace) -> int:
    """Ask the owner for a credential through the supervisor and wait for the outcome.

    The supervisor opens the question and checks the answer (`_grants_supervisor.GrantRequests`); like
    `ci-status`, this only drops a request file and polls for the answer file.
    """
    if args.timeout <= 0:
        raise _CliError("--timeout must be more than zero: a question nobody waits for is not asked")
    root = ensure_dirs()
    request_id = f"grant-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    answer = root / ANSWERS / f"{request_id}.json"
    _write_atomic(
        root / REQUESTS / f"{request_id}.json",
        {
            "id": request_id,
            "name": args.name,
            "hours": args.hours,
            "why": args.why,
            "timeout_s": args.timeout,
            "created_at": time.time(),
        },
    )
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            reply = json.loads(answer.read_text())
            break
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            print("nobody answered in time; no credential", file=sys.stderr)
            return 3
        time.sleep(_POLL_INTERVAL_S)
    granted = bool(reply.get("granted"))
    print(reply.get("message", ""), file=sys.stdout if granted else sys.stderr)
    return 0 if granted else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="credential-grant", description="Use the credentials this project was granted."
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("list", help="show what is granted and until when (never values)").set_defaults(run=_list)
    run = sub.add_parser("exec", help="run a command with a granted credential: exec NAME -- CMD...")
    run.add_argument("name")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(run=_exec)
    rev = sub.add_parser("revoke", help="give a credential back now")
    rev.add_argument("name")
    rev.set_defaults(run=_revoke)
    ask = sub.add_parser("request", help="ask the owner for a credential")
    ask.add_argument("name")
    ask.add_argument("--hours", type=float, default=4)
    ask.add_argument("--why", default="")
    ask.add_argument("--timeout", type=float, default=3600)
    ask.set_defaults(run=request)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return int(args.run(args))
    except _CliError as exc:
        print(f"credential-grant: {exc}", file=sys.stderr)
        return exc.code


def _read_credential() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.read()
    lines: list[str] = []
    while True:
        line = getpass.getpass("" if lines else "Paste the credential, then an empty line (not echoed): ")
        if not line.strip():
            return "\n".join(lines)
        lines.append(line)


def run_seal_cli(argv: list[str]) -> int:
    """`seal --link <link>`: seal a credential for a project without a browser; print only the sealed text."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._grants")
    parser.add_argument("verb", choices=["seal"])
    parser.add_argument("--link", required=True)
    parser.add_argument("--hours", type=float, help="how long the project may use it (default: the link's)")
    parser.add_argument("--region", default="")
    args = parser.parse_args(argv)
    try:
        link = _grant_seal.parse_link(args.link)
        hours = args.hours if args.hours is not None else link.hours
        if not math.isfinite(hours) or not 0 < hours <= MAX_HOURS:
            raise GrantError(f"--hours must be more than zero and at most {MAX_HOURS:g}")
        text = _read_credential()
        now = time.time()
        parse_aws(text, now)  # the same refusals as the supervisor, before anything is sealed
        payload = {
            "name": link.name,
            "kind": "aws",
            "expires_at": int(now + hours * 3600),
            "sealed_at": int(now),
            "value": text,
            "region": args.region,
        }
        print(_grant_seal.seal(link.public_b64, link.project, payload))
    except GrantError as exc:
        print(f"seal: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
