"""Let the Claude Code session ask a human, through a Flyte condition.

`flyte.new_condition` is the durable way to pause for a human: the run suspends
(it does not busy-wait), survives a restart, and can be answered from the Union
UI, from `flyte signal`, or programmatically with
`flyte.remote.Condition.signal` -- which is what makes it answerable by the chat
agent that started the project, not just by a person.

The catch is that conditions can only be created from inside a task context, and
the Claude Code session is a *subprocess* of that task. So the session doesn't
create conditions itself; it calls a tiny CLI that drops a request file, and the
supervisor -- which does hold the task context -- turns each request into a
condition and writes the answer back.

The transport is a directory, not a socket, because it has to survive the
session subprocess being killed and restarted underneath a pending question.

    session:    ask-human --approve "Force-push to main?"
                  -> requests/<id>.json, then polls answers/<id>.json
    supervisor: sees the request -> flyte.new_condition(...) -> waits
    human:      answers in the Union UI (or an agent signals it)
    supervisor: -> answers/<id>.json
    session:    reads the answer, prints it, exits

A question is closed as soon as it stops being live, and the condition says why
("closed: timed out, default taken: ...", "closed: superseded by ask-a0-004"):
the supervisor does it at the session's deadline, and for `ask-human --withdraw`
and `--supersedes`, and when the human answers in the session itself instead of in Union.
The condition is also created with a Flyte-level timeout, so it resolves by itself even if the
supervisor is gone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

import flyte.errors

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from . import _names

    # `X as X`: re-exported, other modules and tests import these from here.
    from ._bridge_common import _EXIT_NO_ANSWER as _EXIT_NO_ANSWER
    from ._bridge_common import _EXIT_WITHDRAWN as _EXIT_WITHDRAWN
    from ._bridge_common import ANSWERS as ANSWERS
    from ._bridge_common import ASK_GRACE_S as ASK_GRACE_S
    from ._bridge_common import _Close, _withdrawn_close
    from ._bridge_common import _write_atomic as _write_atomic
    from ._bridge_common import gave_up_path as gave_up_path
    from ._bridge_triage import TriageMixin, _print_decided
    from ._bridge_triage import pending_path as pending_path
    from ._browser_gate import Gate
    from ._conditions import open_condition
    from ._errors import REMOTE_ERRORS
    from ._judgement import Judgement
else:
    from flyte_agent_factory import _names
    from flyte_agent_factory._bridge_common import (
        _EXIT_NO_ANSWER,
        _EXIT_WITHDRAWN,
        ANSWERS,
        ASK_GRACE_S,
        _Close,
        _withdrawn_close,
        _write_atomic,
        gave_up_path,
    )
    from flyte_agent_factory._bridge_triage import (
        TriageMixin,
        _print_decided,
        pending_path,
    )
    from flyte_agent_factory._conditions import open_condition
    from flyte_agent_factory._errors import REMOTE_ERRORS
    from flyte_agent_factory._judgement import Judgement

# Under HOME, not /var/run: tasks run as an unprivileged user that cannot
# write anywhere outside its own home.
BRIDGE_DIR = Path(
    _names.getenv("AGENT_FACTORY_BRIDGE_DIR")
    or Path(os.environ.get("HOME") or "/tmp") / ".claude-flyte" / "bridge"
)
REQUESTS = "requests"
#: A browser identity name, as `[browser.identities.<name>]` allows.
_IDENTITY = re.compile(r"[a-z][a-z0-9-]{0,19}")
#: What a delegate worker that ran out of turns hands back instead of `factory.patch`.
PARTIAL_PATCH = "factory.partial.patch"
PLAN = "plan.md"

#: Where the supervisor keeps direction for the session, relative to the
#: workspace. A late answer is delivered here rather than discarded. Defined
#: here and imported by the supervisor so the two cannot drift -- the session
#: is told to read this exact path.
INBOX_SUBDIR = ".claude-flyte/inbox"
NOTES = "notes.jsonl"
STOP = "stop"

# A question nobody answers shouldn't wedge the session forever: the CLI gives up
# at its timeout so the model can carry on with its default, and the condition is
# closed just after, so a dead question stops looking answerable in the UI.
DEFAULT_ASK_TIMEOUT_S = 6 * 60 * 60

# The supervisor closes this long before the Flyte timeout fires, so the condition can
# say why it closed instead of just showing a timeout.
_CLOSE_MARGIN_S = 5
#: Asks kept for the dashboard's metrics, newest last; a long project asks a few dozen at most.
ASK_HISTORY_MAX = 200
#: Decisions the factory made in place of the owner, kept like the ask history.
DECISIONS_MAX = 200
#: The longest the judge may take before the question goes to the owner as it is.
JUDGE_CAP_S = 20 * 60

_WITHDRAW_WAIT_S = 60

# A condition just opened is often not yet visible to `Condition.get` (None in 2 of 3 live
# tries, visible after ~0.25 s), so a lookup that finds nothing is retried this long before
# the condition counts as not found.
_LOOKUP_RETRY_S = 0.25
_LOOKUP_WINDOW_S = 5.0

_POLL_INTERVAL_S = 2.0


# Substrings that mark a value as credential-shaped. Used to keep secrets out
# of anything that is written somewhere durable -- run logs, the task report,
# a transcript. Learned the hard way, twice.
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def redact_env(names_only: bool = True) -> dict[str, str]:
    """Return the environment with credential-shaped values removed.

    Printing a variable to confirm it is set does not require printing its
    value, and anything printed from a task lands in run logs that outlive the
    run and are readable by everyone with access to the project.
    """
    import os

    out = {}
    for key, value in os.environ.items():
        if any(hint in key.upper() for hint in _SECRET_HINTS):
            out[key] = f"<redacted, {len(value)} chars>" if names_only else value
        else:
            out[key] = value
    return out


def redact(text: str, *secrets: str, keep: int = 0) -> str:
    """Remove known secret values from text that is about to be printed.

    `redact_env` covers the case of dumping the environment. This covers the
    other one, which has caused more trouble: text that *came back* from
    somewhere and happens to contain a credential. An MCP server that echoes
    the code it ran, a tool that quotes its own arguments, a stack trace over a
    request -- none of those are the environment, and all of them end up in run
    logs that outlive the run.

    Args:
        text: The text to scrub.
        secrets: Values to remove. Empty and very short values are ignored, so
            passing an unset variable cannot blank out the whole string.
        keep: How many leading characters of each secret to leave visible, for
            when you need to tell two credentials apart. Zero by default.

    """
    for secret in secrets:
        if not secret or len(secret) < 8:
            continue
        shown = secret[:keep] if keep else ""
        text = text.replace(secret, f"{shown}<redacted, {len(secret)} chars>")
    return text


#: Credential formats distinctive enough to redact from free text without eating prose.
#: `redact` above removes *known* values; this catches ones we were never handed, such as a
#: token the session pasted into a command line.
_TOKEN_FORMATS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"
    r"|sk-ant-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|AIza[A-Za-z0-9_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"
    r"|\brt\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+)*"
    # A key body line that lost its header (a model quoting part of a key, or a log cut mid-key): an
    # OpenSSH body line is 70 base64 characters, PEM's 64. Mixed case and a digit or +/ are required, so
    # a 64-hex digest, a long lower-case word or a path (which has `.`, `-` or `_`) is left alone. A
    # JSON-escaped key puts a literal `\n` before each line, and that `n` must not count as a word.
    r"|(?:(?<=\\n)|(?<=\\)|(?<![\w+/=.-]))"
    r"(?=[A-Za-z0-9+/]{0,69}[A-Z])(?=[A-Za-z0-9+/]{0,69}[a-z])(?=[A-Za-z0-9+/]{0,69}[0-9+/])"
    r"[A-Za-z0-9+/]{60,70}={0,2}(?![\w+/=.-])"
    r"|tskey-[A-Za-z0-9-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}",
    re.DOTALL,
)


#: The value of a Codex `auth.json` token field. The key stays, so a reader sees the field. A quoted value
#: is masked whatever its length, as JSON has it; an unquoted one only when it is 16 or more token
#: characters not followed by `(`, so `access_token = get_token()` and `id_token: 12 tokens` stay
#: readable. Either stops at `&`, so a URL keeps its other parameters.
_AUTH_JSON_TOKENS = re.compile(
    r"""(["']?(?:access|id|refresh)_token["']?\s*[:=]\s*(["'])?)"""
    r"""(?(2)[^"'\s,}&]+|[\w.~+/=-]{16,}(?![\w.~+/=(-]))"""
)


#: The value after an AWS credential name in a shell line, a credentials file or YAML. A quoted value is
#: masked whatever it holds; an unquoted one runs to whitespace, `,`, `}` or `&`.
_AWS_NAMED_VALUES = re.compile(
    r"""(\b(?:aws_secret_access_key|aws_session_token|aws_security_token)["']?\s*[:=]\s*(["'])?)"""
    r"""(?(2)[^"'\s,}&]+|[^\s,}&"']+)""",
    re.IGNORECASE,
)
#: The same in the JSON AWS prints (`aws sts assume-role`, `credential_process`), also JSON-escaped inside a
#: string. `Token` is the container-credentials field name, so only a long token-like value is masked.
_AWS_JSON_FIELDS = re.compile(
    r"""(\\?["'](?:SecretAccessKey|SessionToken)\\?["']\s*:\s*\\?["'])[^"'\\]+"""
    r"""|(\\?["']Token\\?["']\s*:\s*\\?["'])[A-Za-z0-9/+=_.~-]{40,}"""
)

#: Where live secrets that only their owner knows (the credential grant store's) come from, so that
#: `redact_tokens` removes them even where no format would match.
_redaction_sources: list[Callable[[], Iterable[str]]] = []


def register_redaction_source(fn: Callable[[], Iterable[str]]) -> None:
    """Have `redact_tokens` remove the values `fn()` returns now, each time it runs."""
    if fn not in _redaction_sources:
        _redaction_sources.append(fn)


def unregister_redaction_source(fn: Callable[[], Iterable[str]]) -> None:
    if fn in _redaction_sources:
        _redaction_sources.remove(fn)


def redact_tokens(text: str) -> str:
    """Replace anything shaped like a well-known credential, for text that goes to the task log.

    The one redaction for every path that publishes text a session wrote: the run report, the
    published transcript and log, delegate summaries, `flyte-note` and `ask-human`.
    """
    for source in tuple(_redaction_sources):
        try:
            text = redact(text, *source())
        except (OSError, ValueError, RuntimeError, TypeError) as exc:  # must not stop the patterns below
            # Said, not swallowed: a source that fails means its values are no longer masked by value.
            print(f"redaction source failed: {type(exc).__name__}", file=sys.stderr)
            continue
    text = _TOKEN_FORMATS.sub("<redacted>", text)
    text = _AWS_NAMED_VALUES.sub(r"\1<redacted>", text)
    text = _AWS_JSON_FIELDS.sub(lambda m: (m.group(1) or m.group(2)) + "<redacted>", text)
    return _AUTH_JSON_TOKENS.sub(r"\1<redacted>", text)


#: The shortest value `find_credential_shape` calls a secret. A real secret access key is 40 characters and a
#: session token far longer; a key id (`ASIA...`) is an identifier, and this repo's own tests commit
#: `aws_session_token = abc123` as a fixture, so a shorter value is not refused.
_SECRET_MIN_LEN = 20


def find_credential_shape(text: str) -> str:
    """Name the first secret-bearing AWS shape in `text`, or return "" if there is none.

    The question "would this line be refused", where `redact_tokens` is "mask it": the same two compiled
    patterns, so the two cannot disagree about a shape. The value must be at least 20 characters. A key id
    alone is an identifier, not a secret, and a scan that refused it (or a placeholder like `abc123`) would
    block every project that commits an example credential file from pushing.
    """
    for match in _AWS_NAMED_VALUES.finditer(text):
        if len(match.group(0)) - len(match.group(1)) >= _SECRET_MIN_LEN:
            name = match.group(1).split("=")[0].split(":")[0].strip(" \"'").lower()
            return "aws secret access key" if name == "aws_secret_access_key" else "aws session token"
    for match in _AWS_JSON_FIELDS.finditer(text):
        prefix = match.group(1) or match.group(2)
        if len(match.group(0)) - len(prefix) >= _SECRET_MIN_LEN:
            field = re.sub(r"[^A-Za-z]", "", prefix)
            return f"{field} field"
    return ""


def ensure_dirs(root: Path | None = None) -> Path:
    """Create the request/answer directories and return the root.

    `root=None` resolves `BRIDGE_DIR` *now* rather than at definition time: a
    default argument is bound once at import, which made the bridge path
    impossible to redirect and silently ignored any later override.
    """
    root = Path(root) if root is not None else BRIDGE_DIR
    (root / REQUESTS).mkdir(parents=True, exist_ok=True)
    (root / ANSWERS).mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# Session side: the CLI the model calls.
# --------------------------------------------------------------------------- #


def _read_answer(answer_path: Path, approve: bool) -> int | None:
    """Print the answer and return the exit code, or None if there is none (yet)."""
    if not answer_path.exists():
        return None
    try:
        answer = json.loads(answer_path.read_text())
    except json.JSONDecodeError:
        return None
    if "error" in answer:
        print(f"could not ask: {answer['error']}", file=sys.stderr)
        return 2
    if "withdrawn" in answer:
        print(
            f"this question is no longer open ({answer['withdrawn']}). Carry on; do not wait.",
            file=sys.stderr,
        )
        return _EXIT_WITHDRAWN
    if "in_session" in answer:
        print(f"answered in the session ({answer['in_session']}): {answer.get('value', '')}")
        print(
            "The human replied in this conversation rather than in Union; the question is closed. "
            "Act on that reply.",
            file=sys.stderr,
        )
        # Not 0/1 for a yes/no question: those read as approved/declined, and the decision is in the words.
        return _EXIT_WITHDRAWN if approve else 0
    if "decided" in answer:
        return _print_decided(answer["decided"], approve)
    value = answer.get("value")
    if approve:
        print("approved" if value else "declined")
        return 0 if value else 1
    print(value if isinstance(value, str) else json.dumps(value))
    return 0


def ask_human_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ask-human",
        description="Ask the human who owns this project a question, and wait for the answer.",
    )
    parser.add_argument("question", nargs="?", help="What to ask. Markdown is rendered in the Union UI.")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Ask for a yes/no decision instead of free text. Prints 'approved' or 'declined'.",
    )
    parser.add_argument(
        "--browser",
        metavar="IDENTITY",
        help="Ask the owner to approve acting in their signed-in browser IDENTITY (implies --approve). "
        "On yes the browser gate lets the session act for ten minutes.",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_ASK_TIMEOUT_S, help="Seconds to wait.")
    parser.add_argument(
        "--withdraw",
        metavar="ID",
        help="Close an open question (its request id or condition name) instead of asking one.",
    )
    parser.add_argument("--reason", help="With --withdraw: why, e.g. 'answered via direct-a0-012'.")
    parser.add_argument(
        "--supersedes",
        action="append",
        default=[],
        metavar="ID",
        help="Close this open question (request id or condition name) once the new one is open. Repeatable.",
    )
    args = parser.parse_args(argv)
    if args.withdraw:
        if args.question or args.supersedes or not args.reason:
            parser.error("--withdraw takes --reason and nothing else")
        return _withdraw(args.withdraw, args.reason)
    if not args.question or args.reason:
        parser.error("give a question to ask (--reason only goes with --withdraw)")
    if args.browser is not None:
        if not _IDENTITY.fullmatch(args.browser):
            parser.error(f"--browser {args.browser!r} is not an identity name ({_IDENTITY.pattern})")
        args.approve = True
    if args.timeout <= 0:
        # Not the default timeout: a question nobody waits for has nobody to read its answer.
        print(
            f"--timeout {args.timeout:g} means do not wait, so nothing was asked. Do the default "
            "you would have stated, record the decision and why in a `flyte-note`, and carry on.",
            file=sys.stderr,
        )
        return _EXIT_NO_ANSWER

    root = ensure_dirs()
    request_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    _write_atomic(
        root / REQUESTS / f"{request_id}.json",
        {
            "id": request_id,
            "question": args.question,
            "kind": "approve" if args.approve else "text",
            **({"browser": args.browser} if args.browser else {}),
            "supersedes": args.supersedes,
            # The supervisor closes the condition just after this expires, so a
            # question nobody answered stops looking answerable.
            "timeout_s": args.timeout,
            "created_at": time.time(),
        },
    )
    # Printed so the session can `--withdraw` this exact question from another shell.
    print(f"asking {request_id}", file=sys.stderr)

    answer_path = root / ANSWERS / f"{request_id}.json"
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        rc = _read_answer(answer_path, args.approve)
        if rc is not None:
            return rc
        if pending_path(answer_path).exists():
            deadline = time.monotonic() + args.timeout
        time.sleep(_POLL_INTERVAL_S)

    # Say so before leaving: the supervisor keeps the question open a little past this
    # deadline (ASK_GRACE_S), and an answer landing in that window was recorded as answered and then
    # read by nobody (a past run, answered 43 s after asking).
    _write_atomic(gave_up_path(answer_path), {"id": request_id, "gave_up_at": time.time()})
    # One more look *after* the marker: the bridge writes the answer and then checks for
    # the marker, so with this order at least one side sees the other and an answer that
    # landed during the last poll interval is never lost (at worst it is also in the inbox).
    rc = _read_answer(answer_path, args.approve)
    if rc is not None:
        return rc
    print(
        f"no answer after {args.timeout:.0f}s. The question will be closed in Union within "
        f"{ASK_GRACE_S}s (an answer arriving before then reaches your inbox). "
        "Do the default you stated in the question, record the decision and why in a `flyte-note`, "
        "and carry on -- do not wait.",
        file=sys.stderr,
    )
    return _EXIT_NO_ANSWER


def _withdraw(target: str, reason: str) -> int:
    """Ask the supervisor to close an open question, and report whether it did."""
    root = ensure_dirs()
    request_id = f"withdraw-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    _write_atomic(
        root / REQUESTS / f"{request_id}.json", {"id": request_id, "withdraw": target, "reason": reason}
    )
    answer_path = root / ANSWERS / f"{request_id}.json"
    deadline = time.monotonic() + _WITHDRAW_WAIT_S
    while time.monotonic() < deadline:
        if answer_path.exists():
            try:
                answer = json.loads(answer_path.read_text())
            except json.JSONDecodeError:
                continue
            if "error" in answer:
                print(f"could not withdraw {target}: {answer['error']}", file=sys.stderr)
                return 1
            print(f"closed {answer['closed']}: {reason}")
            return 0
        time.sleep(_POLL_INTERVAL_S / 4)
    print(
        f"could not withdraw {target}: the supervisor did not answer in {_WITHDRAW_WAIT_S}s", file=sys.stderr
    )
    return 1


class _DelegationResult(TypedDict):
    state: str
    error: NotRequired[str]
    summary: NotRequired[str]
    patch: NotRequired[str]
    partial_patch: NotRequired[str]
    plan: NotRequired[str]


def delegate_cli(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    explicit_submit = bool(argv and argv[0] == "submit")
    if explicit_submit:
        argv.pop(0)
    parser = argparse.ArgumentParser(prog="delegate")
    parser.add_argument("task", nargs="?")
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("--answer", help="Read-only question; returns the worker final message")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--timeout", type=float, default=DEFAULT_ASK_TIMEOUT_S)
    args = parser.parse_args(argv)
    root = ensure_dirs()
    jobs = Path(_names.getenv("AGENT_FACTORY_WORKSPACE", os.getcwd())) / ".claude-flyte/delegations"

    def result_for(job_id: str) -> _DelegationResult:
        answer = root / ANSWERS / f"{job_id}.json"
        try:
            result = json.loads(answer.read_text())
            if isinstance(result, dict) and "state" in result:
                # Answer files are written by the delegation service with this shape.
                return cast(_DelegationResult, result)
        except (OSError, ValueError):
            pass
        job = jobs / job_id
        if (job / "error.txt").exists():
            return {"state": "failed", "error": (job / "error.txt").read_text()}
        if (job / "answer-only").exists() and (job / "summary.md").exists():
            return {"state": "done", "summary": (job / "summary.md").read_text()}
        if (job / "summary.md").exists() and (job / "factory.patch").exists():
            return {
                "state": "done",
                "summary": (job / "summary.md").read_text(),
                "patch": str(job / "factory.patch"),
            }
        if all((job / name).exists() for name in ("summary.md", PARTIAL_PATCH, PLAN)):
            return {
                "state": "partial",
                "summary": (job / "summary.md").read_text(),
                "partial_patch": str(job / PARTIAL_PATCH),
                "plan": str(job / PLAN),
            }
        return {"state": "queued"}

    if args.task == "status" and not explicit_submit:
        ids = {path.stem for path in (root / REQUESTS).glob("delegate-*.json")}
        ids.update(path.name for path in jobs.glob("delegate-*") if path.is_dir())
        for job_id in sorted(ids):
            print(f"{job_id} {result_for(job_id)['state']}")
        return 0
    if args.task == "wait" and not explicit_submit:
        if not args.job_id or Path(args.job_id).name != args.job_id:
            parser.error("wait requires a job id")
        request = root / REQUESTS / f"{args.job_id}.json"
        if not request.exists() and not (jobs / args.job_id).is_dir():
            parser.error("unknown job id")
        deadline = time.monotonic() + args.timeout
        while True:
            result = result_for(args.job_id)
            if result["state"] != "queued":
                if "error" in result:
                    print(result["error"], file=sys.stderr)
                    return 1
                if result["state"] in ("done", "partial"):
                    print(result["summary"])
                    for key in ("patch", "partial_patch", "plan"):
                        if key in result:
                            print(result[key])
                    return 0
            if time.monotonic() >= deadline:
                print("delegation wait timed out", file=sys.stderr)
                return 3
            time.sleep(_POLL_INTERVAL_S)
    if any(arg == "--timeout" or arg.startswith("--timeout=") for arg in argv):
        parser.error("--timeout is only supported by wait")
    if sum(bool(value) for value in (args.file, args.task, args.answer)) != 1 or args.job_id:
        parser.error("provide a task brief or --file")
    brief = args.file.read_text() if args.file else args.answer or args.task
    job_id = f"delegate-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    _write_atomic(
        root / REQUESTS / f"{job_id}.json", {"id": job_id, "brief": brief, "answer_only": bool(args.answer)}
    )
    print(job_id)
    return 0


def _ci_text(data: dict[str, Any]) -> str:
    """Return a human-readable form of a `ci-status` answer."""
    if "log" in data:
        note = " (truncated: the end of the log)" if data["truncated"] else ""
        return f"job {data['job']} of {data['repo']}{note}\n{data['log']}"
    lines = [f"{data['repo']} {data['branch']} @ {data['sha'][:12]}"]
    for title, key, state, name in (
        ("workflow runs", "runs", "conclusion", "name"),
        ("check-runs (id is the job id for --logs)", "checks", "conclusion", "name"),
        ("statuses", "statuses", "state", "context"),
    ):
        lines.append(f"{title}:")
        for item in data[key]:
            result = item[state] or item.get("status") or "?"
            ident = f"{item['id']} " if "id" in item else ""
            lines.append(f"  {ident}{item[name]}: {result} {item['url']}".rstrip())
        if not data[key]:
            lines.append("  (none)")
    if data["missing"]:
        lines.append(f"not readable, so missing above: the GitHub App lacks {', '.join(data['missing'])}")
    return "\n".join(lines)


def ci_status_cli(argv: list[str] | None = None) -> int:
    """Ask the supervisor for CI results of this session's branch; it holds the GitHub credentials."""
    parser = argparse.ArgumentParser(
        prog="ci-status", description="Read-only CI results (workflow runs, check-runs, job logs)."
    )
    parser.add_argument("--sha", help="a commit of the branch instead of its head")
    parser.add_argument("--logs", type=int, metavar="JOB_ID", help="tail of one job's log")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    if args.sha and args.logs:
        parser.error("--sha and --logs do not combine")

    root = ensure_dirs()
    request_id = f"ci-status-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    answer = root / ANSWERS / f"{request_id}.json"
    _write_atomic(
        root / REQUESTS / f"{request_id}.json", {"id": request_id, "sha": args.sha, "job": args.logs}
    )
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            reply = json.loads(answer.read_text())
            break
        except (OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            print("ci-status timed out waiting for the supervisor", file=sys.stderr)
            return _EXIT_NO_ANSWER
        time.sleep(_POLL_INTERVAL_S)
    if "error" in reply:
        print(reply["error"], file=sys.stderr)
        return 1
    print(json.dumps(reply["result"], indent=2) if args.json else _ci_text(reply["result"]))
    return 0


def end_cli(argv: list[str] | None = None) -> int:
    """Finish the project: the supervisor shuts the session down cleanly.

    Exists so that "we're done, wrap it up" said to the session leads to a
    completed run rather than someone having to remember `/exit` or reach for
    `flyte abort`, which would discard everything since the last checkpoint.
    """
    parser = argparse.ArgumentParser(
        prog="end-project", description="Finish this project and complete the Flyte run."
    )
    parser.add_argument("summary", nargs="?", default="", help="One line on what was delivered.")
    args = parser.parse_args(argv)

    root = ensure_dirs()
    _write_atomic(root / f"{STOP}.json", {"at": time.time(), "summary": args.summary})
    print("ending the project: final checkpoint, publishing the workspace, completing the run")
    return 0


def note_cli(argv: list[str] | None = None) -> int:
    """Append a line to the Flyte task report, so progress is visible in Union."""
    parser = argparse.ArgumentParser(prog="flyte-note", description="Write a line into the Flyte run report.")
    parser.add_argument("message")
    args = parser.parse_args(argv)

    root = ensure_dirs()
    with (root / NOTES).open("a") as fh:
        fh.write(json.dumps({"at": time.time(), "message": args.message}) + "\n")
    return 0


# --------------------------------------------------------------------------- #
# Supervisor side: turn requests into conditions.
# --------------------------------------------------------------------------- #


def _default_text(stated: str) -> str:
    """Strip a "Default if no answer:" lead-in: "Default if no answer: use sqlite" -> "use sqlite"."""
    return re.sub(r"^default(?:ing)?\b[^:]*:\s*", "", stated, flags=re.IGNORECASE).strip()


class _ConditionGone(LookupError):
    """No condition of that name under this action, even after the lookup window."""


def _ended_on_server(exc: BaseException) -> bool:
    """Return whether a signal was refused because the condition had already ended.

    Live, that is FAILED_PRECONDITION, "condition is ACTION_PHASE_TIMED_OUT, expected PAUSED".
    """
    code = getattr(exc, "code", None)
    return getattr(code, "name", str(code)).upper() == "FAILED_PRECONDITION" or "expected PAUSED" in str(exc)


class _ConditionEnded(RuntimeError):
    """The condition has already ended in Flyte (timed out, answered or aborted)."""


#: Phases of a condition action that has ended (`Condition.phase` reads `ACTION_PHASE_<one of these>`).
#: Listed rather than the live ones, so an unfamiliar phase is still closed rather than skipped.
_ENDED_PHASES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED_OUT"}


def _timeout_reason(stated_default: str) -> str:
    """Why a question nobody answered was closed: with the default it stated, if it stated one."""
    if stated_default:
        return f"closed: timed out, default taken: {_default_text(stated_default)}"
    return "closed: timed out, no default stated"


class Bridge(TriageMixin):
    """Watches the request directory and services each ask as a Flyte condition.

    Run it as a background asyncio task for the lifetime of the session. Each
    question gets its own `asyncio.Task` so a pending one never blocks the next.

    A reply the human types into the session itself closes the open questions
    (`close_answered_in_session`). A Union `direct-` message does not, by decision:
    it may answer one question, several or none, and nothing here can tell which, so
    closing those stays with the session (`ask-human --withdraw`).
    """

    #: Where the open questions are kept across a restart; None keeps them in memory only.
    _registry: Path | None = None

    def __init__(
        self,
        root: Path | None = None,
        *,
        on_event: Callable[[str, str], None] | None = None,
        registry: Path | None = None,
    ) -> None:
        # Same reason as `ensure_dirs`: resolved per instance, not at import.
        self.root = ensure_dirs(root)
        self._seen: set[str] = set()
        self._pending: set[asyncio.Task[None]] = set()
        self._seq = 0
        self._on_event = on_event
        self._notes_offset = 0
        self._registry = registry
        #: Closes on the wire or done, by request id, until the question's `_service` has seen it
        #: end: what its CLI is told (`outcome`) and the text we signalled (`signal`; None for an
        #: abort), so a value that is not ours is recognised as the human's real answer.
        self._closed: dict[str, dict[str, Any]] = {}
        #: Request ids a human answered in Union while we were closing them: the close lost.
        self._answered_first: set[str] = set()
        #: Closes asked for before the question has a condition (judged, waiting its turn, or opening in
        #: Union); `_service` honours them before it opens one, or once it is open.
        self._queued: dict[str, _Close] = {}
        #: Questions a human has not answered yet, by request id, for the dashboard. A question
        #: leaves as soon as it is answered or closed.
        self.questions: dict[str, dict[str, Any]] = {}
        #: The grant gate, when the project has a browser grant: a "yes" to an approval asked with
        #: `--browser` opens its window here. Set by the supervisor; the gate has no route that does this.
        self.browser_gate: Gate | None = None
        self.ask_history: list[dict[str, Any]] = []
        #: Triage of each ask before the owner sees it (`_judgement.run_judge`); None asks the owner
        #: everything, as sub-agent bridges do.
        self.judge: Callable[[dict[str, Any]], Awaitable[Judgement]] | None = None
        #: What the factory decided in the owner's place, oldest first (`decision-NNN`).
        self.decisions: list[dict[str, Any]] = []
        self._decision_seq = 0
        #: Asks being judged or waiting their turn, by request id -> when they were asked: nothing is
        #: open in Union for them, so a close is recorded here and `_service` honours it.
        self._unopened: dict[str, float] = {}
        #: Owner-reserved questions reach the owner one at a time, so each is answered with the
        #: previous one settled.
        self._owner_turn = asyncio.Lock()

    # Read here, not in the mixin, so a test or caller that sets this module's value still takes effect.
    def _judge_cap_s(self) -> float:
        return JUDGE_CAP_S

    def _decisions_max(self) -> int:
        return DECISIONS_MAX

    def _emit(self, icon: str, text: str) -> None:
        if self._on_event:
            self._on_event(icon, text)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _history_path(self) -> Path:
        # Both callers check that a registry is configured before requesting its sibling.
        return cast(Path, self._registry).with_name("ask_history.json")

    def _remember(self, entry: dict[str, Any]) -> None:
        self.ask_history.append(entry)
        del self.ask_history[:-ASK_HISTORY_MAX]

    def _save(self) -> None:
        """Write the open questions to the registry, so a restarted supervisor can still close them.

        The history of every ask (answered and closed too) goes beside it: the benchmark's metrics
        count them, and a restart must not make them disappear.
        """
        if self._registry is None:
            return
        try:
            self._registry.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(self._registry, list(self.questions.values()))
            _write_atomic(self._history_path(), self.ask_history)
            _write_atomic(self._decisions_path(), {"seq": self._decision_seq, "decisions": self.decisions})
        except OSError as e:
            self._emit("⚠️", f"could not save the open questions: {type(e).__name__}: {e}")

    def _forget(self, request_id: str) -> None:
        entry = self.questions.pop(request_id, None)
        if entry is not None:
            entry["closed_at"] = time.time()
            self._save()

    def restore(self) -> None:
        """Take back the questions that were open before a restart; each still closes at its deadline.

        Their `ask-human` went with the old pod, so nothing waits for an answer here; they are kept
        so that `--withdraw`, `--supersedes` and a reply in the session can still close them, and so
        that one nobody answers still closes saying which default was taken.
        """
        if self._registry is None:
            return
        try:
            history = json.loads(self._history_path().read_text())
        except (OSError, ValueError):
            history = []
        try:
            saved_decisions = json.loads(self._decisions_path().read_text())
            self._decision_seq = int(saved_decisions["seq"])
            self.decisions[:] = [d for d in saved_decisions["decisions"] if isinstance(d, dict)]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._emit(
                "⚠️",
                f"decisions.json unreadable: {type(e).__name__}: {e}; "
                "starting the list empty, ids continue from the ask history",
            )
            # Kept for inspection: the next `_save` would overwrite it.
            try:
                self._decisions_path().replace(self._decisions_path().with_suffix(".json.bad"))
            except OSError:
                pass  # read-only state dir: the warning above is all there is to say
        # Never behind an id already handed out: `decisions.json` may be lost, or older than the history.
        ids = [d.get("id") for d in self.decisions]
        ids += [e.get("decided") for e in history if isinstance(history, list) and isinstance(e, dict)]
        for decision_id in ids:
            if isinstance(decision_id, str) and (found := re.fullmatch(r"decision-(\d+)", decision_id)):
                self._decision_seq = max(self._decision_seq, int(found.group(1)))
        # Their `ask-human` went with the old pod: a marker left behind would only hold a new CLI's clock.
        for stale in (self.root / ANSWERS).glob("*.pending"):
            stale.unlink(missing_ok=True)
        known = {entry["id"] for entry in self.ask_history}
        for entry in history if isinstance(history, list) else []:
            if isinstance(entry, dict) and entry.get("id") not in known:
                self._remember(entry)
        try:
            saved = json.loads(self._registry.read_text())
        except (OSError, ValueError):
            return
        for entry in saved if isinstance(saved, list) else []:
            # One that never opened in Union has nothing to close.
            if not isinstance(entry, dict) or not entry.get("condition") or entry.get("id") in self.questions:
                continue
            entry["restored"] = True
            self.questions[entry["id"]] = entry
            # The same ask as its history entry, now open again: one record, not two.
            self.ask_history[:] = [old for old in self.ask_history if old.get("id") != entry["id"]]
            self._remember(entry)
            self._spawn(self._expire(entry))
        for entry in self.ask_history:
            # Open when the old pod died, yet not in the registry: it never reached Union.
            if entry.get("id") not in self.questions and not entry.get("closed_at"):
                entry["closed_at"] = time.time()
        self._save()

    async def _expire(self, entry: dict[str, Any]) -> None:
        """Close a restored question at its deadline, as `_service` does for one it is waiting on.

        Forgotten whatever the close does: past its deadline it is answerable by nobody, and kept
        it would be restored and fail again on every restart, show on the dashboard as waiting and
        hold the release. The usual case is a pod that was down past the deadline, which finds the
        condition already ended by Flyte's timeout, or gone.
        """
        await asyncio.sleep(max(0.0, float(entry.get("deadline_at") or 0) - _CLOSE_MARGIN_S - time.time()))
        if entry["id"] not in self.questions:
            return
        name = entry["condition"]
        reason = _timeout_reason(entry.get("default") or "")
        try:
            await self._abandon(name, reason, approve=entry["kind"] == "approve")
        except _ConditionGone:
            self._emit("🗑️", f"closed {name}: gone (not found in Union)")
        # TypeError: a payload the condition rejects.
        except (*REMOTE_ERRORS, TypeError) as e:  # reported as a row; the question is forgotten anyway
            if isinstance(e, _ConditionEnded) or _ended_on_server(e):
                self._emit("🗑️", f"closed {name}: closed by Flyte's timeout")
            else:
                self._emit(
                    "⚠️", f"could not close {name}: {type(e).__name__}: {e} (forgotten: past its deadline)"
                )
        else:
            self._emit("🗑️", f"closed {name}: {reason.removeprefix('closed: ')}")
        self._forget(entry["id"])

    async def run(self) -> None:
        self.restore()
        try:
            while True:
                self._scan()
                await asyncio.sleep(_POLL_INTERVAL_S)
        except asyncio.CancelledError:
            pending = set(self._pending)
            for task in pending:
                task.cancel()
            # Awaited, so nothing this bridge started (a judge still deciding, say) outlives it.
            await asyncio.gather(*pending, return_exceptions=True)
            raise

    def _scan(self) -> None:
        for path in sorted((self.root / REQUESTS).glob("*.json")):
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            try:
                request = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if request.get("id", "").startswith(("delegate-", "ci-status-", "grant-")):
                continue
            if "withdraw" in request:
                self._spawn(self._withdraw(request))
                continue
            if request.get("id") in self.questions:
                continue  # restored from before a restart: already open in Union
            self._seq += 1
            self._spawn((self._serve if self.judge else self._service)(request, self._seq))

    def _prepare(self, request: dict[str, Any]) -> tuple[str, str, float, str] | None:
        """Return (question, kind, timeout, browser identity) of a request, or None after refusing it."""
        request_id = request["id"]
        question = redact_tokens(request.get("question", "(no question)"))
        kind = request.get("kind", "text")
        answer_path = self.root / ANSWERS / f"{request_id}.json"

        given = request.get("timeout_s")
        timeout = DEFAULT_ASK_TIMEOUT_S if given is None else float(given)
        if given is not None and timeout <= 0:
            # Asked not to wait (`ask-human` itself sends none of these): not the default timeout,
            # because a question nobody waits for has nobody to read its answer.
            _write_atomic(
                answer_path,
                {"id": request_id, "error": "no wait requested (timeout <= 0); nothing was asked"},
            )
            return None
        identity = str(request.get("browser") or "")
        if identity:
            if self.browser_gate is None or identity not in self.browser_gate.identities:
                _write_atomic(
                    answer_path,
                    {
                        "id": request_id,
                        "error": f"this project has no grant for browser identity {identity!r}",
                    },
                )
                return None
            question += self.browser_gate.prompt_note(identity)
        return question, kind, timeout, identity

    async def _service(
        self,
        request: dict[str, Any],
        seq: int,
        *,
        entry: dict[str, Any] | None = None,
        prompt: str = "",
        prepared: tuple[str, str, float, str] | None = None,
    ) -> None:
        """Open a question in Union and carry the answer (or its timeout) to the CLI.

        `entry`, `prompt` and `prepared` come from `_serve` for a triaged question that is already
        recorded and prepared.
        """
        prepared = prepared or self._prepare(request)
        if prepared is None:
            return
        request_id = request["id"]
        answer_path = self.root / ANSWERS / f"{request_id}.json"
        question, kind, timeout, identity = prepared
        deadline = timeout + ASK_GRACE_S
        if entry is None:
            self._emit("🙋", f"asked: {question}")
            entry = self._new_entry(
                request_id, kind, question, float(request.get("created_at") or time.time()), timeout
            )

        name = ""
        waiter: asyncio.Future[str | bool] | None = None
        try:
            # A constrained TypeVar cannot take "bool or str", though either is a valid answer type.
            condition, used = await open_condition(  # type: ignore[type-var]
                "ask",
                seq,
                prompt=prompt or question,
                prompt_type="markdown",
                data_type=bool if kind == "approve" else str,
                # Resolves the condition even if this supervisor is gone before it closes it.
                timeout=deadline,
            )
            # A collision advanced the counter; keep the shared one ahead of it.
            self._seq = max(self._seq, used)
            name = entry["condition"] = getattr(condition, "name", "")
            self._save()
            # The turn is ours and the condition is open: the CLI's own clock may run from here.
            pending_path(answer_path).unlink(missing_ok=True)
            queued = self._queued.pop(request_id, None)
            if queued is not None:
                await self._close_entry(entry, **queued)
            if not prompt:  # a triaged question replaced its predecessors before it waited for its turn
                await self._supersede(request, f"superseded by {name}")
            # Shielded: if the close at the deadline fails, an answer may still be on its way,
            # and a condition can be waited on only once.
            # Cast: the condition answers with `data_type`, a bool or a str (see the ignore above).
            waiter = asyncio.ensure_future(cast("Awaitable[str | bool]", condition.wait.aio()))
            value = await asyncio.wait_for(asyncio.shield(waiter), timeout=deadline - _CLOSE_MARGIN_S)
        except (asyncio.TimeoutError, flyte.errors.ConditionTimedoutError) as e:
            # The asker has given up and so has the question: close it, saying why, so it
            # stops looking answerable. An answer in the grace window was delivered above.
            _write_atomic(answer_path, {"id": request_id, "error": "nobody answered in time"})
            if not name or waiter is None or isinstance(e, flyte.errors.ConditionTimedoutError):
                self._forget(request_id)
                if name:
                    self._emit("🗑️", f"closed {name}: timed out (closed by Flyte)")
                return
            if await self._close(name, kind == "approve", _timeout_reason(entry["default"])):
                waiter.cancel()
                self._forget(request_id)
                return
            # The close failed, and the likeliest reason is a human answering after the wait gave
            # up and before the close: that answer is the wait's value. Flyte's own timeout, a few
            # seconds on, ends the wait if nobody did.
            self._spawn(self._late_answer(waiter, question, request_id))
            return
        # TypeError: a payload the condition rejects. LookupError: `_ConditionGone`.
        except (*REMOTE_ERRORS, TypeError, LookupError) as e:  # surfaced to the model, not swallowed
            self._forget(request_id)
            self._queued.pop(request_id, None)
            closing = self._closed.pop(request_id, None)
            if closing is not None:
                # Aborting an approve condition ends its wait with an error: that is the close.
                _write_atomic(answer_path, {"id": request_id, **closing["outcome"]})
                return
            _write_atomic(answer_path, {"id": request_id, "error": f"{type(e).__name__}: {e}"})
            self._emit("⚠️", f"ask failed: {type(e).__name__}: {e}")
            return

        closing = self._closed.pop(request_id, None)
        if closing is not None and kind != "approve" and value == closing["signal"]:
            # Our own close, come back as the condition's value: neither an answer to hand on
            # nor a late one for the inbox.
            self._forget(request_id)
            _write_atomic(answer_path, {"id": request_id, **closing["outcome"]})
            return
        if closing is not None:
            # A human answered in Union while our close was on the wire (a bool value is never
            # ours: we abort those). Theirs wins, and the close is reported as failed.
            self._answered_first.add(request_id)
        # Before the forget, which stamps `closed_at`: an answer never comes after the close.
        entry["answered_at"] = time.time()
        self._forget(request_id)
        self._emit("✅", f"answered: {value!r}")
        if identity and value is True and self.browser_gate is not None:
            # Before the answer is written: the session retries the moment it reads "approved".
            self.browser_gate.approve(identity)
            self._emit("🔓", f"browser {identity}: approved by the owner")
        told_closed = closing is not None and answer_path.exists()
        if not told_closed:
            _write_atomic(answer_path, {"id": request_id, "value": value})
        if told_closed or gave_up_path(answer_path).exists():
            # The asker stopped waiting (the grace window) or was already told the question
            # closed: nobody will read the answer file, so it goes to the inbox as a late answer.
            self._write_late(question, value)

    async def _late_answer(self, waiting: Awaitable[str | bool], question: str, request_id: str = "") -> None:
        """Hand an answer that arrives after the asker moved on to the session's inbox.

        Only after a close failed, so the question really is still open; it stays on the
        dashboard until the wait ends, which Flyte's timeout on the condition bounds.
        """
        try:
            value = await waiting
            for entry in self.ask_history:
                if entry["id"] == request_id:
                    # Before the `finally` forgets it, which stamps `closed_at`.
                    entry["answered_at"] = time.time()
                    break
        # TypeError: the condition rejects a payload of the wrong type. Anything escaping here
        # would end the question's task with an error nobody reads.
        except (*REMOTE_ERRORS, TypeError):  # a lost late answer must not break the run
            return
        finally:
            self._forget(request_id)
        self._write_late(question, value)

    async def _withdraw(self, request: dict[str, Any]) -> None:
        """Service `ask-human --withdraw`: close the question and tell the CLI how it went."""
        error = await self.close_question(request["withdraw"], request.get("reason") or "withdrawn")
        answer = {"id": request["id"]}
        if error:
            answer["error"] = error
        else:
            answer["closed"] = request["withdraw"]
        _write_atomic(self.root / ANSWERS / f"{request['id']}.json", answer)

    async def close_question(self, target: str, why: str) -> str | None:
        """Close an open question, found by request id or condition name. Returns an error, or None."""
        if target in self._unopened:
            self._close_unopened(target, _withdrawn_close(why))
            return None
        entry = next((q for q in self.questions.values() if target in (q["id"], q["condition"])), None)
        if entry is not None and not entry["condition"]:
            # Past the queue, its condition still opening: `_service` closes it the moment it is open.
            self._queued[entry["id"]] = _withdrawn_close(why)
            return None
        if entry is None:
            error = f"no open question {target!r}"
            self._emit("⚠️", f"could not close {target}: {error}")
            return error
        reason = f"closed: {why}"
        if not await self._close_entry(entry, reason, {"withdrawn": reason}):
            return f"closing {entry['condition']} failed (see the timeline)"
        return None

    async def close_answered_in_session(self, message: str, source: str, at: float) -> None:
        """Close the asks: the human replied in the session itself (`source`, e.g. "Claude app") at `at`.

        Every question open when the message was typed is closed with it. Which one a reply
        answers is not knowable from here, and closing too many is cheap -- the session reads
        the reply in its own conversation and re-asks whatever is still open -- while leaving
        one open is the bug: Union shows a question as unanswered that its owner answered.
        A question asked after the message cannot have been answered by it, so it stays.
        """
        for request_id, asked in list(self._unopened.items()):
            if asked <= at:
                self._close_unopened(
                    request_id,
                    {
                        "reason": f"answered in session ({source}): {message[:500]}",
                        "outcome": {"value": message, "in_session": source},
                        "abort_reason": "",
                        "row": "",
                        "answered": True,
                    },
                )
        for entry in [
            q for q in self.questions.values() if q["opened_at"] <= at and q["id"] not in self._unopened
        ]:
            close: _Close = {
                # A text condition carries the reply itself, so Union shows what was said.
                "reason": f"answered in session ({source}): {message[:500]}",
                "outcome": {"value": message, "in_session": source},
                "abort_reason": f"closed: answered in session ({source})",
                "row": "answered in session",
                "answered": True,
            }
            if not entry["condition"]:
                # Still opening in Union: `_service` closes it the moment it is open.
                self._queued[entry["id"]] = close
                continue
            await self._close_entry(entry, **close)

    async def _close_entry(
        self,
        entry: dict[str, Any],
        reason: str,
        outcome: dict[str, Any],
        *,
        abort_reason: str = "",
        row: str = "",
        answered: bool = False,
    ) -> bool:
        """Close an open question and tell its waiting CLI `outcome`. False (and a ⚠️ row) if it failed.

        `reason` is a text condition's signalled value, and an approve condition's abort reason
        unless `abort_reason` is given.
        """
        approve = entry["kind"] == "approve"
        said = (abort_reason or reason) if approve else reason
        # Before the close: the waiting `_service` wakes up as soon as the condition ends.
        self._closed[entry["id"]] = {"outcome": outcome, "signal": None if approve else said}
        closed = await self._close(entry["condition"], approve, said, row, request_id=entry["id"])
        if not closed or entry.get("restored"):
            # No `_service` is waiting on a restored question to collect this.
            self._closed.pop(entry["id"], None)
        if not closed:
            return False
        if answered:
            entry["answered_at"] = time.time()
        self._forget(entry["id"])
        # Told now rather than when `_service` wakes: an aborted action's wait can take a
        # while to notice, and the CLI should not sit there that long. `_service` writes the
        # same thing again when it does.
        _write_atomic(self.root / ANSWERS / f"{entry['id']}.json", {"id": entry["id"], **outcome})
        return True

    async def _close(
        self, condition_name: str, approve: bool, reason: str, row: str = "", *, request_id: str = ""
    ) -> bool:
        """`_abandon` with the outcome on the timeline (`row`, or the reason); never raises."""
        try:
            await self._abandon(condition_name, reason, approve=approve)
            error = ""
        # TypeError: a payload the condition rejects; LookupError: no such condition here.
        except (*REMOTE_ERRORS, LookupError, TypeError) as e:  # reported as a row; the caller carries on
            error = f"{type(e).__name__}: {e}"
        answered_first = request_id in self._answered_first
        self._answered_first.discard(request_id)
        if answered_first:
            self._emit("⚠️", f"could not close {condition_name}: a human answered it in Union first")
            return False
        if error:
            self._emit("⚠️", f"could not close {condition_name}: {error}")
            return False
        self._emit("🗑️", f"closed {condition_name}: {row or reason.removeprefix('closed: ')}")
        return True

    def _write_late(self, question: str, value: Any) -> None:
        if value is None or str(value).strip() == "":
            return

        workspace = _names.getenv("AGENT_FACTORY_WORKSPACE")
        if not workspace:
            self._emit("⏳", f"late answer, nowhere to put it: {str(value)[:160]}")
            return

        inbox = Path(workspace) / INBOX_SUBDIR
        inbox.mkdir(parents=True, exist_ok=True)
        # Unique: two late answers in the same second used to overwrite each other.
        path = inbox / f"late-{int(time.time())}-{uuid.uuid4().hex[:6]}.md"
        path.write_text(
            "# A late answer to a question you already moved past\n\n"
            f"You asked:\n\n> {question}\n\n"
            "Nobody answered before your timeout, so you carried on -- correctly. "
            "The answer arrived afterwards:\n\n"
            f"{('approved' if value else 'declined') if isinstance(value, bool) else value}\n\n"
            "Decide whether it changes anything. If you already chose a different route, "
            "say so rather than silently redoing the work.\n"
        )
        self._emit("📬", f"late answer delivered to {path.name}")

    async def _abandon(self, condition_name: str, reason: str, *, approve: bool) -> None:
        """End a question on the Flyte side, with `reason` visible to whoever looks at it.

        A bool (approve) condition cannot carry text, so it is closed by aborting its action
        with the reason; a text condition is signalled with the reason as its value.
        Raises if it cannot: the caller decides how to report that.
        """
        import flyte.remote

        ctx = flyte.ctx()
        if ctx is None or ctx.action is None:
            raise RuntimeError("no task context")
        run_name = ctx.action.run_name
        # Narrowed to this action's own conditions: sub-agents run their own Bridge in the same
        # run with the same ask-a0-NNN names, and without `action_name` the SDK returns the first
        # match in the run -- a close then answered a sub-agent's live question and left ours open.
        # A retry is another attempt of the same action, so this also finds a question asked before it.
        give_up = time.monotonic() + _LOOKUP_WINDOW_S
        while True:
            condition = await flyte.remote.Condition.get.aio(
                condition_name, run_name=run_name, action_name=ctx.action.name
            )
            if condition is not None:
                break
            # Not yet visible, if it was opened a moment ago: see _LOOKUP_WINDOW_S.
            if time.monotonic() >= give_up:
                raise _ConditionGone(f"condition {condition_name} not found")
            await asyncio.sleep(_LOOKUP_RETRY_S)
        phase = str(getattr(condition, "phase", "") or "").removeprefix("ACTION_PHASE_")
        if phase in _ENDED_PHASES:
            raise _ConditionEnded(f"condition {condition_name} already ended ({phase})")
        if approve:
            # `abort` is an instance method: it needs the action fetched first.
            action = await flyte.remote.Action.get.aio(run_name=run_name, name=condition.action_name)
            await action.abort.aio(reason=reason)
        else:
            await condition.signal.aio(reason)

    def next_seq(self) -> int:
        """Return the next condition number, for a service that opens `ask-` conditions beside this one."""
        self._seq += 1
        return self._seq

    def keep_seq(self, used: int) -> None:
        """Keep the shared counter ahead of `used`, which `open_condition` advanced past a collision."""
        self._seq = max(self._seq, used)

    def notify_owner(self, text: str, timeout_s: float) -> str:
        """Open a question in Union on the supervisor's behalf; return its request id.

        For what the session cannot say itself because it is parked or out of usage. It goes through
        the same request file as `ask-human`, so it is serviced, timed out and closed like any ask.
        """
        request_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        # Nothing waits on this ask, as `ask-human` would. The marker makes `_service` hand an answer to
        # the session's inbox as a late one instead of leaving it in a file nobody reads.
        _write_atomic(
            gave_up_path(self.root / ANSWERS / f"{request_id}.json"),
            {"id": request_id, "gave_up_at": time.time()},
        )
        _write_atomic(
            self.root / REQUESTS / f"{request_id}.json",
            {
                "id": request_id,
                "question": text,
                "kind": "text",
                "supersedes": [],
                "timeout_s": timeout_s,
                "created_at": time.time(),
            },
        )
        return request_id

    def answered_since(self, since: float) -> bool:
        """Whether the human answered any question (epoch seconds) at or after `since`."""
        return any(e.get("answered_at", 0) >= since for e in self.ask_history)

    def stop_requested(self) -> str | None:
        """Return the summary from `end-project`, if it has been run."""
        path = self.root / f"{STOP}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text()).get("summary") or "ended by request"
        except (OSError, json.JSONDecodeError):
            return "ended by request"

    def drain_notes(self) -> list[str]:
        """Return any `flyte-note` lines written since the last call.

        Reads forward from an offset instead of reading and then truncating.
        The truncating version lost any note that `flyte-note` appended in the
        gap between the read and the truncate -- and the note most likely to
        land there is the `DONE` the session posts and then waits on. Appends
        only ever add bytes, so an offset cannot lose one. A trailing line with
        no newline yet is a writer mid-write and is left for the next call.
        """
        path = self.root / NOTES
        try:
            with path.open("rb") as fh:
                fh.seek(self._notes_offset)
                chunk = fh.read()
        except OSError:
            return []
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self._notes_offset += end + 1
        out = []
        for line in chunk[: end + 1].decode("utf-8", "replace").splitlines():
            try:
                out.append(redact_tokens(json.loads(line)["message"]))
            except (json.JSONDecodeError, KeyError):
                continue
        return out


def main(argv: list[str] | None = None) -> int:
    """Run `python -m flyte_agent_factory._bridge {ask,note} ...`.

    The agent writes thin shell wrappers onto PATH that call this, rather than
    relying on console scripts, so the code bundle Flyte already ships is the
    only copy of this logic that has to exist in the image.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m flyte_agent_factory._bridge {ask,note} ...", file=sys.stderr)
        return 64
    command, rest = argv[0], argv[1:]
    if command == "delegate":
        return delegate_cli(rest)
    if command == "ci-status":
        return ci_status_cli(rest)
    if command == "ask":
        return ask_human_cli(rest)
    if command == "note":
        return note_cli(rest)
    if command == "end":
        return end_cli(rest)
    print(f"unknown command {command!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
