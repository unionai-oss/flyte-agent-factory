"""The sub-agent task's helpers: its result record, the direct meters, the Codex run, the hand-back."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox

from flyte_agent_factory import (
    _auth,
    _author,
    _checkpoint,
    _codex_auth,
    _meter,
    _names,
    _policy,
    _roles,
    _router,
    _session_config,
    _session_setup,
    _timeline,
    _usage,
)
from flyte_agent_factory._bridge import (
    redact_tokens,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._codex_process import CodexAuthor, _Event
from flyte_agent_factory._delegation import (
    PARTIAL_PATCH,
    PLAN,
    work_diff,
)
from flyte_agent_factory._errors import REMOTE_ERRORS


async def _subagent_result(
    text: str,
    route: dict[str, Any] | None,
    *,
    routes: list[dict[str, Any]] | None = None,
    refusals: dict[str, float] | None = None,
    unusable: list[str] | None = None,
) -> str:
    if route is None:
        return redact_tokens(text)
    import flyte.report

    # The task interface stays (text, workspace). Reports carry telemetry without putting an
    # internal trailer in the author's tool response; the supervisor reads completed child reports.
    clean, rec = _usage._split_usage(text)
    rec["route"] = route
    if routes:
        rec["routes"] = routes
    if refusals:
        rec["refusals"] = refusals
    if unusable:
        rec["unusable"] = unusable
    await flyte.report.log.aio(f"{_usage._USAGE_MARK}{json.dumps(rec)}-->", do_flush=True)
    return redact_tokens(clean)  # what the coordinator reads, and may quote in its report


#: Plain Flyte tasks need a fresh process: the image lacks the actor bridge required for reusable
#: tasks (see subagent_env). Each direct child therefore probes each configured login at most once;
#: its result then supplies the supervisor's meters. Reviewer calls use the supervisor's cache and
#: never probe here. Within a process, cache failures too and serialize concurrent readers.
_direct_meters: tuple[float, dict[str, _meter.Meter | None]] | None = None
_direct_meters_lock = asyncio.Lock()


async def _read_direct_meters() -> dict[str, _meter.Meter | None]:
    """Return the meters a sub-agent can read from its own tokens, for a call nobody routed.

    An unreadable
    meter is None, which the router treats as unknown. Read at most once per `_meter.MIN_INTERVAL_S`
    in this process, failures included. Fresh child processes pay at most two probes: Claude is
    bounded at 20 seconds, Codex at 30 seconds.
    """
    global _direct_meters
    async with _direct_meters_lock:
        if _direct_meters and time.time() - _direct_meters[0] < _meter.MIN_INTERVAL_S:
            return dict(_direct_meters[1])
        readers: dict[str, Callable[[], Awaitable[_meter.Meter]]] = {}
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        if token:
            # Until the probe confirms the endpoint takes a setup-token, the meter stays unknown here
            # and CLI rate-limit observations are the headroom signal.
            if _meter.SETUP_TOKEN_USAGE_SUPPORTED:
                readers["claude"] = lambda: _meter.read_claude(
                    token, _session_config._claude_account(), setup_token=True
                )
        found: dict[str, _meter.Meter | None] = {}
        for name, read in readers.items():
            try:
                found[name] = await read()
            except _usage._METER_ERRORS as exc:
                _timeline._log_line(f"[meter] {name}: {type(exc).__name__}", err=True)
        _direct_meters = (time.time(), found)
        return dict(found)


async def _route_direct_subagent(
    role: str,
    engine: str,
    model: str,
    effort: str,
    *,
    hard_pin: bool = False,
    available: tuple[str, ...] | None = None,
    refusals: dict[str, float] | None = None,
    unusable: list[str] | None = None,
    routes: list[dict[str, Any]] | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    """Route a `subagent(...)` call that did not come through `_call_reviewer`.

    A given engine is a pin and is logged as one. The Claude `model` and `effort` the task defaults
    to are replaced by the role's Codex setting when the router moves the call to Codex. The last item
    is the decision as a record, for the sub-agent to hand back to its parent (`_with_route`).
    """
    available = (
        available
        if available is not None
        else tuple(
            name
            for name, variable in (
                ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
                ("codex", _session_config.CODEX_TOKEN_ENV_VAR),
            )
            if os.environ.get(variable, "").strip()
        )
    )
    purpose = role or "subagent"
    meters = await _read_direct_meters()
    for refused, until in (refusals or {}).items():
        meter = meters.get(refused) or _meter.Meter(refused, "limit-text", time.time())
        meters[refused] = replace(meter, limited_until=until)
    decision = _router.route(
        purpose,
        meters,
        available=available,
        now=time.time(),
        preferred=engine or "claude",
        pin=engine,
        hard_pin=hard_pin,
        exclude=tuple(refusals or ()),
        builders=(_names.getenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", "claude"),)
        if purpose in (*_usage._QA_ROLES, "qa", "verify") and _usage._qa_differs()
        else (),
    )
    if not decision.deferred:
        if refusals or decision.engine != (engine or "claude"):
            setting = (
                _policy.resolve_codex(purpose) if decision.engine == "codex" else _policy.DEFAULTS["author"]
            )
            model, effort = setting.model, setting.effort
        if decision.engine == "codex" and _policy.is_claude_model(model):
            model, effort = _policy.resolve_codex(purpose)
        decision = replace(decision, model=model, effort=effort)
    _usage._record_route(decision, None)
    record = {**decision.record(), "row": decision.row()}
    if decision.deferred:
        await _subagent_result(
            "", record, routes=[*(routes or []), record], refusals=refusals, unusable=unusable
        )
        raise _usage._deferred(decision)
    # Not None once it is not deferred (`Decision.deferred` is `engine is None`).
    return cast(str, decision.engine), model, effort, record


#: Longest the follow-up that asks a turn-limited worker for its remaining-work plan may take.
_PLAN_FOLLOWUP_TIMEOUT_S = 300

_PLAN_PROMPT = (
    "You ran out of turns before finishing. Do not use any tools. Reply with the plan of the work that "
    "remains, as a short numbered list: for each step the files and what to do, and how to check it. "
    "Say first what is already done."
)


async def _ask_remaining_work(
    claude: str, work: Path, env: dict[str, str], payload: dict[str, Any], model: str, effort: str
) -> str:
    """Return the worker's own plan for what remains: one short `--resume` turn on its session."""
    session = payload.get("session_id")
    if not session:
        return ""
    # The same credential path as the worker's own call: in token deployments `env` alone has no login.
    with _auth.token_fd(env, os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()) as (child_env, pass_fds):
        proc = await asyncio.create_subprocess_exec(
            claude,
            "-p",
            _PLAN_PROMPT,
            "--resume",
            str(session),
            "--output-format",
            "json",
            *_policy.argv(_policy.Setting(model, effort)),
            "--max-turns",
            "2",
            "--permission-mode",
            "bypassPermissions",
            cwd=str(work),
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=_PLAN_FOLLOWUP_TIMEOUT_S)
    except asyncio.TimeoutError:
        await _checkpoint._reap(proc)
        return ""
    return _final_text(raw.decode(errors="replace")) if proc.returncode == 0 else ""


async def _partial_handback(
    work: Path, base: str, max_turns: int, payload: dict[str, Any], ask_plan: Callable[[], Awaitable[str]]
) -> str:
    """Write a turn-limited delegate's work to `work` and return its summary.

    `factory.partial.patch` is everything changed since `base`, untracked files included (`add -N`
    records them as intent-to-add so `git diff` shows them), and `plan.md` is what remains. The plan
    is the final assistant text when the CLI gave one, else what `ask_plan()` returns; a worker
    that cannot produce one still hands its patch back.
    """
    repo = work / "repo"
    if not repo.is_dir():
        # The worker ran out of turns before it extracted the repo: there is no tree to diff.
        return (
            f"PARTIAL: the worker hit its {max_turns}-turn limit before it had extracted the repo, "
            "so there is no work to hand back. Run it again, with a narrower brief if it is large."
        )
    # Disarms the tree: the worker is finished, and `Dir.from_local(work)` ships this copy back.
    diff = await asyncio.to_thread(work_diff, repo, base)
    plan = str(payload.get("result") or "").strip()
    if not plan:
        try:
            plan = (await ask_plan()).strip()
        except (OSError, RuntimeError) as exc:
            _timeline._log_line(f"[delegate] no remaining-work plan: {type(exc).__name__}", err=True)
    plan = plan or "(the worker gave no plan; read the partial patch and the brief to see what is left)"
    (work / PLAN).write_text(plan + "\n")
    (work / PARTIAL_PATCH).write_bytes(diff)
    return f"PARTIAL: the worker hit its {max_turns}-turn limit before finishing.\n\nRemaining work:\n{plan}"


# "You've hit your limit" is the subscription refusal; older CLIs say "Claude AI usage limit reached".
_CLAUDE_LIMIT = _meter._CLAUDE_LIMIT


def _raise_on_cli_error(stdout: str, *, only_limit: bool = False) -> None:
    """`claude -p --output-format json` can exit 0 with `is_error`; that is a failure to run, not an answer.

    Otherwise an auth error becomes the "result", the reviewer gives no verdict, and a
    review round is spent on an infrastructure failure.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return
    if isinstance(payload, dict) and payload.get("is_error"):
        result = str(payload.get("result", ""))[:300]
        # Not `_CODEX_LIMIT`: "quota" or a bare "429" in an ordinary error is not a usage refusal.
        meter = _meter.from_claude_events([payload], _session_config._claude_account())
        if meter and meter.limited_until:
            # Carry any observed reset to the supervisor's refusal cache: a failed action has no outputs.
            meter.limited_until = _usage._reset_at(result) or meter.limited_until
            observation = f" {_meter.report_comment(meter)}"
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {result}{observation}")
        if not only_limit:
            raise RuntimeError(f"sub-agent reported an error: {result}")


def _final_text(stdout: str) -> str:
    """Pull the final message out of `claude -p --output-format json`.

    Falls back to raw stdout: a sub-agent that produced a useful answer in an
    unexpected shape should not be reported as having produced nothing.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    if isinstance(payload, dict):
        for key in ("result", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return stdout.strip()


_CODEX_LIMIT = _meter._CODEX_LIMIT


def _codex_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return token usage summed over `turn.completed` events of `codex exec --json`.

    Probed shape: input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens,
    reasoning_output_tokens.
    """
    total: dict[str, int] = {}
    for e in events:
        if e.get("type") != "turn.completed":
            continue
        for k, v in (e.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + int(v)
    return total


async def _run_codex(
    work: Path,
    base: Path,
    brief: str,
    *,
    answer_only: bool,
    role: str,
    model: str,
    effort: str = "medium",
    extra_env: dict[str, str] | None = None,
) -> str:
    """Run one Codex sub-agent in `work`; its final message, plus the usage trailer.

    Same contract as the Claude path: the brief is in `.claude-flyte/BRIEF.md`, the house rules
    in `AGENTS.md` (Codex's CLAUDE.md), a role file prepended when one is named. A usage-limit
    refusal raises with USAGE_LIMIT_MARK so the caller waits or falls back like any limit.
    """
    credential = os.environ.get(_session_config.CODEX_TOKEN_ENV_VAR, "").strip()
    if not credential:
        raise RuntimeError(
            "no Codex credential available for the sub-agent "
            "([secrets] codex_token or the slot's codex_login)"
        )
    login = _codex_auth.is_login(credential)
    if login and _codex_auth.expires_soon(credential):
        # A sub-agent gets no refresh token (below), so its copy cannot outlive the access token; a
        # short-lived one would die mid-task, so the call waits or goes to Claude now.
        raise RuntimeError(
            f"{_usage.USAGE_LIMIT_MARK} codex: the mounted access token expires within "
            f"{_codex_auth.MIN_ACCESS_LIFE_S // 60:.0f} minutes and a sub-agent cannot refresh it"
        )
    # Under $HOME, not /tmp: Codex refuses to set up its helper links under a temp dir.
    home = Path.home() / ".codex-subagents" / base.name
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    if login:
        # No refresh token: the first holder to refresh invalidates every other copy, and the author is
        # the one that keeps the chain (`_author._persist_codex_login`).
        _auth.write_private(home / "auth.json", _codex_auth.strip_refresh_token(credential))
    else:
        await asyncio.to_thread(_author._login_with_access_token, home, credential)
    _session_setup._write_managed_block(work / "AGENTS.md", _subagent_memory(answer_only=answer_only))
    role_text = ""
    if role:
        # The image holds only the new roles; an old name runs as the role it is an alias of.
        role_file = _session_setup._roles_dir() / f"{_roles.canonical_role(role)}.md"
        if not role_file.is_file():  # a roles directory from before the rename still has the old file
            role_file = _session_setup._roles_dir() / f"{role}.md"
        if not role_file.is_file():
            raise ValueError(f"unknown role {role!r}")
        text = role_file.read_text(errors="replace")
        # Drop the YAML header Claude Code's agent files carry: Codex has no use for it.
        if text.startswith("---"):
            end = text.find("\n---", 3)
            text = text[end + 4 :] if end != -1 else text
        role_text = text.strip() + "\n\n"
    prompt = role_text + (
        f"Read {_session_config._BRIEF_FILE} and carry it out. Your final message is your entire answer: "
        "nothing you write to files is kept, so put everything the brief asks for in it."
        if answer_only
        else f"Read {_session_config._BRIEF_FILE} and carry it out. Finish with a short report of what you "
        f"did."
    )
    last = base / "codex-last.txt"
    argv = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        # Its own container and a throwaway copy, like the Claude sub-agent's bypassPermissions.
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'cli_auth_credentials_store="file"',
        "-o",
        str(last),
    ]
    # Both optional: a direct `subagent(engine="codex")` call arrives with `effort=""` and the
    # Claude author default as `model`, and Codex refuses an empty effort and has no "sonnet".
    if model and _policy.is_claude_model(model):
        model = ""
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    if model:
        argv += ["-m", model]
    # After `--`: a prompt starting with "-" (a role file's `---` header did) was read as an option.
    argv += ["--", prompt]
    env = {**_session_setup._session_env({**os.environ, **(extra_env or {})}), "CODEX_HOME": str(home)}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(work),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,  # it otherwise waits for more input on stdin
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    events: list[Any] = []  # parsed JSON lines
    for line in stdout.splitlines():
        with contextlib.suppress(ValueError):
            if line.strip().startswith("{"):
                events.append(json.loads(line))
    answer = last.read_text(errors="replace").strip() if last.is_file() else ""
    observed = _meter.from_events(events, _session_config._codex_account())
    if proc.returncode != 0 or not answer:
        # Codex reports refusals (its usage limit included) as JSON events on stdout; stderr
        # then holds only "Reading additional input from stdin...", so a limit read as a crash.
        reported = [
            str(e.get("message") or (e.get("error") or {}).get("message") or "")
            for e in events
            if isinstance(e, dict) and e.get("type") in ("error", "turn.failed")
        ]
        detail = ("\n".join(m for m in reported if m) or stderr or stdout)[-600:]
        if (
            _codex_auth.is_auth_failure(stderr)
            or any(_codex_auth.is_auth_failure(m) for m in reported)
            or any(
                CodexAuthor._auth_failure(cast("_Event", e))  # parsed JSON; read defensively
                for e in events
                if isinstance(e, dict)
            )
        ):
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} {_usage._CODEX_LOGIN_UNUSABLE}: {detail[-200:]}")
        if observed:
            if _CODEX_LIMIT.search(detail):
                observed.limited_until = (
                    _usage._reset_at(detail) or time.time() + _session_config.LIMIT_RETRY_S
                )
            # A failed action has no return value; its report must retain the last backend observation.
            try:
                await flyte.report.log.aio(_meter.report_comment(observed), do_flush=True)
            except (*_usage._METER_ERRORS, *REMOTE_ERRORS, RuntimeError, ValueError) as exc:
                # Reporting the meter must never replace the real error raised below.
                _timeline._log_line(f"[meter] codex: {type(exc).__name__}", err=True)
        if _CODEX_LIMIT.search(detail):
            raise RuntimeError(f"{_usage.USAGE_LIMIT_MARK} codex: {detail[-200:]}")
        raise RuntimeError(f"codex sub-agent exited {proc.returncode}: {detail}")
    usage = _codex_usage(events)
    rec = {
        "input_tokens": int(usage.get("input_tokens") or 0) - int(usage.get("cached_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cached_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_write_input_tokens") or 0),
        "cost_usd": 0.0,
        "models": [model or "codex:default"],
        "engine": "codex",
        "effort": effort,
        "turns": 0,
        **({"rate_limits": observed.to_dict()} if observed else {}),
    }
    return answer + f"{_usage._USAGE_MARK}{json.dumps(rec)}-->"


def _subagent_memory(answer_only: bool = False) -> str:
    # A past run: the planner, told its files are handed back and to report briefly, wrote
    # PLAN.md in a copy that is discarded and replied "see PLAN.md".
    hand_back = (
        "- Your working directory is a throwaway copy: nothing you write there is kept or seen. "
        "Your final message is your entire answer -- put everything the brief asks for in it, "
        "and never refer to a file you wrote.\n"
        if answer_only
        else "- Everything in your working directory is handed back to the parent agent when you "
        "finish. Leave it in a state someone can read: commit, or leave a clear note.\n"
    )
    finish = "give your full answer as your final message" if answer_only else "finish with a short report"
    return (
        "## You are a Flyte sub-agent\n\n"
        "You were delegated a slice of a larger project and run in your own container, as a child "
        "action of that project's Flyte run.\n\n"
        f"- Your instruction is in `{_session_config._BRIEF_FILE}`. Carry it out and {finish}.\n"
        '- Ask with `ask-human "<question>"` if you are blocked on a decision; it opens a question '
        "in Union against your own action and waits.\n"
        + hand_back
        + "- You cannot spin off further jobs. Finish your slice and report back.\n"
    )
