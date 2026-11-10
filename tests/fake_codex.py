"""A stand-in for `codex exec --json`, for testing `CodexProcess` offline.

Invoked as `python fake_codex.py exec [resume] --json [options] [THREAD_ID] -- PROMPT`. Threads are
files under $CODEX_HOME/threads, so a second invocation can resume what a first one started, and a
fresh $CODEX_HOME knows none (as a pod that lost its home would).

Environment flags (the process under test passes its own env through):
    FAKE_CODEX_RECORD=PATH  append one JSON line per turn: argv, cwd and the environment variable names
    FAKE_CODEX_LIMIT=1    refuse with a usage-limit error and the rate_limits event _meter parses
    FAKE_CODEX_CRASH=1    print a traceback line and exit 3 with no result
    FAKE_CODEX_STDERR=TEXT  print TEXT to stderr first (Codex's own log lines, an MCP server's)
    FAKE_CODEX_SLEEP=N    stay in the turn N seconds before the answer (to be interrupted)
    FAKE_CODEX_LINES=N    print N extra non-event lines first (to overflow a bounded tail)
    FAKE_CODEX_BG_STDOUT=PATH  detach a sleep inheriting stdout and record its PID at PATH
    FAKE_CODEX_MCP=1      read $CODEX_HOME/config.toml, do one MCP tools/list and one tool call per server,
                          and answer with the result (the answer is the turn's output)
    FAKE_CODEX_AUTH=reused  fail the turn the way Codex does when the refresh token was already used
                          ("401" fails it as a bare unauthorized, "reconnect" only prints it mid-turn)
    FAKE_CODEX_BG=CMD     start CMD in the background (a tool that outlives the turn) before anything else
"""

import itertools
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def mcp_round_trip() -> str:
    """What Codex does with `[mcp_servers.*]`: initialize, list tools, call the first one.

    Headers come from `http_headers` and `bearer_token_env_var` exactly as Codex would send them.
    """
    config = tomllib.loads((Path(os.environ["CODEX_HOME"]) / "config.toml").read_text())
    answers = []
    for name, server in config.get("mcp_servers", {}).items():
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **server.get("http_headers", {}),
        }
        if server.get("bearer_token_env_var"):
            headers["Authorization"] = f"Bearer {os.environ[server['bearer_token_env_var']]}"

        ids = itertools.count(1)

        def send(method, params=None, _url=server["url"], _headers=headers, _ids=ids):
            request_id = next(_ids)
            body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            request = urllib.request.Request(_url, json.dumps(body).encode(), _headers, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)["result"]

        send("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}})
        tools = [t["name"] for t in send("tools/list")["tools"]]
        result = send("tools/call", {"name": tools[0], "arguments": {"url": "https://example.com"}})
        answers.append(f"{name}: tools={tools} result={result['content'][0]['text']}")
    return "; ".join(answers) or "no mcp servers"


def login(argv: list[str]) -> int:
    """`codex login --with-access-token`: the token on stdin, an auth.json in $CODEX_HOME (or a refusal)."""
    token = sys.stdin.read().strip()
    if os.environ.get("FAKE_CODEX_LOGIN_FAIL") or not token:
        print("Error logging in with access token: rejected", file=sys.stderr)
        return 1
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "access_token", "token_length": len(token)}))
    return 0


def main(argv: list[str]) -> int:
    if argv[:2] == ["login", "--with-access-token"]:
        return login(argv)
    if "--" not in argv:
        print("error: the prompt must follow `--`", file=sys.stderr)
        return 2
    split = argv.index("--")
    head, prompt = argv[:split], " ".join(argv[split + 1 :])
    if head[:1] != ["exec"]:
        print(f"error: unexpected command {head[:1]}", file=sys.stderr)
        return 2
    resuming = head[1:2] == ["resume"]
    if record := os.environ.get("FAKE_CODEX_RECORD"):
        with open(record, "a") as stream:
            stream.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "env": sorted(os.environ)}) + "\n")
    threads = Path(os.environ.get("CODEX_HOME", ".")) / "threads"
    threads.mkdir(parents=True, exist_ok=True)

    if os.environ.get("FAKE_CODEX_BG"):
        subprocess.Popen(
            os.environ["FAKE_CODEX_BG"],
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if os.environ.get("FAKE_CODEX_STDERR"):
        print(os.environ["FAKE_CODEX_STDERR"], file=sys.stderr, flush=True)
    for n in range(int(os.environ.get("FAKE_CODEX_LINES", "0"))):
        print(f"noise {n}", flush=True)
    if os.environ.get("FAKE_CODEX_CRASH"):
        print("Traceback (most recent call last): fake codex crashed", flush=True)
        return 3

    if resuming:
        # `resume` takes the thread id as its one positional argument before the prompt.
        positional = [a for a in head[2:] if not a.startswith("-")]
        thread = positional[-1] if positional else ""
        if not (threads / thread).is_file():
            emit({"type": "error", "message": f"no recorded session found matching '{thread}'"})
            return 1
    else:
        thread = str(uuid.uuid4())
        (threads / thread).write_text("")
        emit({"type": "thread.started", "thread_id": thread})

    turns = threads / thread
    turns.write_text(turns.read_text() + prompt + "\n")
    count = len(turns.read_text().splitlines())
    emit({"type": "turn.started"})

    auth = os.environ.get("FAKE_CODEX_AUTH")
    if auth == "reconnect":
        emit({"type": "error", "message": "Reconnecting... 1/5 (unexpected status 401 Unauthorized)"})
    elif auth:
        message = (
            "unexpected status 401 Unauthorized: Missing bearer or basic authentication"
            if auth == "401"
            else "Your access token could not be refreshed because your refresh token was already used. "
            "Please log out and sign in again."
        )
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1

    if os.environ.get("FAKE_CODEX_LIMIT"):
        emit(
            {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_in_seconds": 1800},
                    "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_in_seconds": 400000},
                },
            }
        )
        message = "You've hit your usage limit. Try again later."
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        if os.environ.get("FAKE_CODEX_BG_STDOUT"):
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            # Atomic: the test polls this path and must never read it half-written (empty).
            pidfile = Path(os.environ["FAKE_CODEX_BG_STDOUT"])
            tmp = pidfile.with_name(pidfile.name + ".tmp")
            tmp.write_text(str(child.pid))
            os.replace(tmp, pidfile)
        return 1

    time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))
    if os.environ.get("FAKE_CODEX_FAIL_AFTER_MESSAGE"):
        # A turn that speaks, then dies mid-stream: it never completes.
        emit(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "I will look first."},
            }
        )
        emit({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
        return 3
    text = os.environ.get("FAKE_CODEX_ANSWER", f"turn {count} on {thread}: {prompt}")
    if os.environ.get("FAKE_CODEX_MCP"):
        text = mcp_round_trip()
    if "-o" in head:
        Path(head[head.index("-o") + 1]).write_text(text)
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
