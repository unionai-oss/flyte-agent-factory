"""The factory's MCP server: Flyte's stock MCP app, every tool, limited to the factory's project and domain.

Union authenticates every request at the app ingress (`requires_auth=True`) and Flyte runs each
call as the caller, so a Union user's sign-in token or a Union API key both work as the bearer.
What the caller may do inside the factory's project and domain is bounded by their Union auth (RBAC);
this module only keeps every tool from reaching outside it (`_scope_tools_to_the_factory`).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlparse

import flyte
from flyte.ai.mcp import FlyteMCPAppEnvironment
from flyte.ai.mcp._flyte_mcp_app import ALL_MCP_TOOLS, TOOL_REGISTRY
from flyte.ai.mcp._tools import build_tool_functions
from mcp.server.fastmcp import (
    Context as MCPContext,
)  # annotations below are strings; FastMCP resolves them here
from mcp.server.fastmcp.exceptions import ToolError

from . import _factory, _grant_page, _profile
from ._factory import DOMAIN, PROJECT
from ._factory_base import PROFILE, SLOTS, cap
from ._images import agent_image
from ._policy import LAUNCH_MODEL
from ._reserved import list_owner_reserved

#: Every tool the installed flyte ships (the same 32 through 2.13.0); `_scope_tools_to_the_factory` is what
#: limits them, not leaving some out.
TOOLS: list[str] = list(ALL_MCP_TOOLS)

_mcp = PROFILE.mcp
if _mcp is None:
    raise _profile.ProfileError(f"profile {PROFILE.name!r}: missing table [mcp], needed by factory-mcp")

#: Union's read model lags the watch stream: for 10-60s after a run SUCCEEDED, `Run.get` can still say it
#: is not done, and the stock `get_run_io` then returns `outputs: null` with no error. These bound how long
#: the tools below wait for it to catch up, and how fast they poll (doubling up to the cap).
_OUTPUTS_WAIT_S = 90.0
_POLL_FIRST_S = 1.0
_POLL_MAX_S = 10.0
#: Compared lowercase: the stock tools report `ActionPhase.value` ('succeeded'), other callers use the name.
_SUCCESS_PHASES = ("succeeded", "recovered")
#: Phases after which a run has no outputs coming, so a null `outputs` is the right answer straight away.
_FAILURE_PHASES = ("failed", "aborted", "timed_out")


def _phase_name(phase: Any) -> str:
    """Lowercase name of a phase given as an enum, its value, or a string of either case."""
    return str(getattr(phase, "value", phase) or "").lower()


REPOS = _factory.allowed_repos()
CAPACITY = (
    f"one shared environment, up to {cap()} live projects"
    if PROFILE.shared
    else f"{len(SLOTS)} slots, one live project each"
)
#: The tasks the instructions steer chat to. Any task in the factory project can be run, but the slot tasks
#: are not among these: started directly they skip validation and the queue, and two launches could race
#: for one slot.
#: Chat may also start a Codex sign-in: it only ever writes a `codex_login*` secret (see the task).
CODEX_LOGIN_TASK = "factory-setup.codex_bootstrap_login"
CHAT_TASKS = (
    "launch",
    "queue_list",
    "queue_cancel",
    "status",
    "set_reserve",
    "get_reserve",
    "rotate_token",
    "browser_link",
    "credential_link",
    "credentials",
)


def _relay_list() -> str:
    """Name the owner-reserved categories, floor and profile extras, that a chat relays to the user."""
    return ", ".join(list_owner_reserved(PROFILE.judgement))


INSTRUCTIONS = f"""Use Flyte project {PROJECT} and domain {DOMAIN}.
For "how is X doing", run_task(name="factory.status", inputs={{"name": "X"}}), then get_run_io.
It reads the live run's report (or the newest ended run); a run id also works. Optional rows defaults
to 40. It returns the current activity, dashboard phase, newest timeline rows, open questions, review,
PR URL, meters and snapshot age. Check snapshot_age_s for freshness; an error includes the run URL
when a report is unavailable. SUCCEEDED alone does not mean review passed.
The software factory builds projects in long-lived Claude sessions with Codex workers and
independent review. It has {CAPACITY}; launches never fail for
capacity: when none is free the launch waits in a queue and starts by itself.
Launch with run_task(name="factory.launch", inputs=...), then wait_for_run(timeout_s=300), then
get_run_io for the JSON result. After a run SUCCEEDED, wait_for_run and get_run_io wait up to
{_OUTPUTS_WAIT_S:g}s for its outputs to become readable; if get_run_io still returns outputs null with an
outputs_error saying they are not yet readable, call get_run_io again. Inputs: brief (full markdown),
name (lowercase slug, 2-41 letters, digits or hyphens), model (always pass it: {LAUNCH_MODEL} unless the
user asks otherwise), repo (one of:
{", ".join(REPOS.values())}; the first is the default), demo_package (optional), issue (optional
owner/repo#number linking the project to a GitHub work item; status comments are posted on it
wherever the factory's GitHub App is configured), priority (optional: normal, the default; low, which
starts only when a slot is free and no other launch waits; or expedite, which goes first). The result's
status is
started (give the user its url), queued (tell the user the position) or updated (that name was already
waiting; its brief was replaced and it kept its place). A launch that is refused says why: a bad name, a
brief that is too long, a repo that is not allowed, or a name that is running (send direction instead).
When a launch run FAILS, get_run_io has no outputs: read the reason with get_logs (small max_lines) or
get_action's error_info.
run_task(name="factory.queue_list") shows the slots, the waiting launches in order and the recent
ones (and any usage reserve holding a launch back); run_task(name="factory.queue_cancel",
inputs={{"name": ...}}) removes a waiting launch (and aborts its run, if a dispatcher had just created
one). Read both results with get_run_io.
Pass browser (optional) to factory.launch only when the user asked for a signed-in browser identity for that
project: identity names, comma-separated, or "none" to revoke the grant a resume would inherit. Leave it out
otherwise; a resume keeps the project's grant.
run_task(name="factory.browser_link", inputs={{"identity": ...}})
returns JSON {{identity, url}}: a one-time sign-in/view link for the user (ten minutes, single use).
Credentials: a project asks for an AWS credential with an ask- question that holds a link. The user opens
the link, pastes a short-lived scoped credential, presses Seal and copies the sealed text (it starts with
fgc1.); relay exactly that text with signal_condition as the answer, or "no" to refuse. Never ask the user
to paste a raw credential into chat, and never repeat one. To give or refresh one the project did not ask
for, run_task(name="factory.credential_link", inputs={{"name": "<project>", "credential": "<name>",
"hours": 4}}) returns JSON {{project, credential, hours, url, paste_to}}: give the user the url, then
signal the sealed text they bring to the condition named paste_to (the project's direct- inbox). To
revoke, signal "/revoke-credential <name>" (or "/revoke-credential" for all) to that direct- condition.
run_task(name="factory.credentials") lists every live project's grants (names, expiry, state; never a
value); factory.status returns one project's as credentials.
Each account (claude, codex) can hold back k percent of every usage window per hour left until that
window resets, so a launch waits when its account has less than k x hours-to-reset left and reopens by
itself as the reset nears. run_task(name="factory.set_reserve", inputs={{"account": "claude", "k": 0.5}})
sets it (0 removes it) and the next admission uses it; run_task(name="factory.get_reserve") reads every k.
A project whose run has ended is resumed by launching the same name again with
the new work as the brief: it continues from the last published workspace.
Use factory.status for progress and review state (rounds, blocking findings, each judge's latest
verdict); get_logs reads diagnostic logs from the beginning.
list_conditions finds ask- questions and direct- inboxes (send direction; /stop saves and ends the
project). Decide an engineering-judgement ask- yourself (design, structure, how to satisfy a check, tests,
tooling): do what is the right thing to do, never a waiver, lowered threshold, removed rule or skipped test.
Signal a value that starts "Operator decision (engineering judgement): " with the reasoning, and tell the
user what you decided. Relay only owner-reserved asks ({_relay_list()}) to the user, one at a time, stated
plainly with your recommendation, and signal_condition their answer verbatim. The factory's own judge already
decides engineering asks before they open: factory.status returns those as decisions, with reasoning. The user
overrules one by sending direction that names its id, such as "overrule decision-003: ...".
Prefer /stop over abort_run; abort loses uncheckpointed work.
run_task(name="{CODEX_LOGIN_TASK}", inputs={{"secret_name": "codex_login_..."}}) signs Codex in to a
ChatGPT account: list_conditions on its run shows a codex-login prompt with a link and a code for the user to
open and enter; once they do, the login is stored as that secret.
run_task(name="factory.rotate_token") (no inputs) makes a replaced token secret reach running projects: the
owner updates the secret first; this then sends /stop to every live project and queues one resume of each
with the reason "token rotated", which starts on a new pod from the checkpoint once the old run has
ended. Calling it again while the resumes wait queues nothing more. Read the result with get_run_io;
factory.queue_list and factory.status show the waiting resumes and their reason.
When a project fails because its token was refused (its run says the Claude setup-token or the Codex
access token expired or was revoked), the owner replaces the token in the Flyte secret named by
[secrets] claude_token or codex_token; then launch the project again by name.
Launch benchmarks with run_task(name="factory-bench.benchmark", inputs=...), then get_run_io for
the summary JSON and cases JSONL. Inputs: candidate_sha (required full 40-character commit SHA or
factory repo ref), baseline_sha (optional SHA or ref; empty means candidate only), tasks (suite task
ids; empty means all), repeats (1-3, default 2), candidate_policy and baseline_policy (JSON object
strings, default "{{}}"), reuse_baseline_days (non-negative, default 7), max_usd (default 0).
First pass max_usd=0: it returns an estimate and spends nothing. Read estimate.ceiling_usd, including
both arms and stop margins; a paid launch requires the user's spend authorization and an explicit
max_usd covering that ceiling, within the profile's [bench] max_usd. Reuse the same inputs for that
launch. The task refuses bad SHAs or refs, unknown task ids, invalid policies, repeats outside 1-3,
and estimates above max_usd before deployment or case launches. A refused run has no outputs: use
get_logs (small max_lines) or get_action's error_info. Monitor with
list_runs(task_name="factory-bench.benchmark"), get_run_io and the run report.
Every tool is limited to this project and domain; a call naming another is refused, and list_projects shows
only this project. What you may do here is bounded by the caller's Union permissions.
Running a slot task directly is unsupported (it skips validation and the queue), though it is not blocked.
After a failed deploy, start run_task(name="factory-deployer.deploy_if_changed") (no inputs) instead of
waiting for the next tick; read its result with get_run_io.
list_secrets shows names only and no tool returns a secret value; create_secret and delete_secret exist for
the token secrets the owner would otherwise set. list_apps, get_app, activate_app and deactivate_app, and
list_triggers, get_trigger, activate_trigger and deactivate_trigger, manage this project's apps and triggers.
"""

_public_url = os.environ.get("FACTORY_MCP_PUBLIC_URL", _mcp.public_url).rstrip("/")

#: The docs the search tools read. The stock defaults are under /root, which the factory's `agent_image()`
#: does not have and its unprivileged user could not read; the image build puts them here instead.
DOCS_DIR = "/opt/flyte-mcp-docs"
FLYTE_SDK_TAG = "v2.10.7"  # the installed flyte; its examples match the SDK the tools run on
_FETCH_DOCS = (
    # Shallow clones keep only the directory the search reads; the rest of each repo is dropped.
    f"mkdir -p {DOCS_DIR} "
    f"&& git clone --depth 1 --branch {FLYTE_SDK_TAG} https://github.com/flyteorg/flyte-sdk /tmp/flyte-sdk "
    f"&& mkdir -p {DOCS_DIR}/flyte-sdk && mv /tmp/flyte-sdk/examples {DOCS_DIR}/flyte-sdk/examples "
    "&& git clone --depth 1 https://github.com/unionai/unionai-examples /tmp/unionai-examples "
    f"&& mkdir -p {DOCS_DIR}/unionai-examples && mv /tmp/unionai-examples/v2 {DOCS_DIR}/unionai-examples/v2 "
    f"&& curl -fsSL -o {DOCS_DIR}/llms.txt https://www.union.ai/docs/v2/union/llms.txt "
    f"&& rm -rf /tmp/flyte-sdk /tmp/unionai-examples && chmod -R a+rX {DOCS_DIR}"
)

app = FlyteMCPAppEnvironment(
    name="factory-mcp",
    image=agent_image()
    .with_pip_packages("flyte[mcp]>=2.10.7,<2.11.0", "mcp>=1.30.0,<2")
    .with_commands([_FETCH_DOCS]),
    requires_auth=True,
    tools=TOOLS,
    task_allowlist=None,  # any task in the factory project; `_scope_tools_to_the_factory` bounds it
    instructions=INSTRUCTIONS,
    allowed_hosts=[urlparse(_public_url).netloc] if _public_url else None,
    allowed_origins=[_public_url] if _public_url else None,
    env_vars={"FLYTE_MCP_PROJECT": PROJECT, "FLYTE_MCP_DOMAIN": DOMAIN, **_profile.pod_env()},
    scaling=flyte.app.Scaling(replicas=1),
    sdk_examples_path=f"{DOCS_DIR}/flyte-sdk/examples",
    docs_examples_path=f"{DOCS_DIR}/unionai-examples/v2",
    full_docs_path=f"{DOCS_DIR}/llms.txt",
)


ToolFn = Callable[..., Awaitable[dict[str, Any]]]


def _wrap_tools_to_wait_for_outputs(stock: dict[str, Any]) -> dict[str, ToolFn]:
    """Return `wait_for_run` and `get_run_io` that wait out the read model's lag after SUCCEEDED.

    Wrappers over the stock tools rather than a replacement: the SDK owns how outputs are decoded, and
    this only adds the wait. Registered over the stock ones in `app.mcp`, because the chat flow is
    always `run_task`, `wait_for_run`, `get_run_io`, and the watch stream behind `wait_for_run` is
    ahead of the `Run.get` behind `get_run_io`.
    """
    stock_get_run: ToolFn = stock["get_run"]
    stock_wait: ToolFn = stock["wait_for_run"]
    stock_io: ToolFn = stock["get_run_io"]

    async def outputs_when_readable(
        name: str,
        project: str | None,
        domain: str | None,
        # Unparametrised on purpose: FastMCP finds its Context argument by `issubclass`, which a
        # `Context[...]` alias fails. The tools' `-> dict` is bare for the same reason: `dict[str, Any]`
        # makes FastMCP add structured output, a second content block that clients do not expect.
        ctx: MCPContext | None,  # type: ignore[type-arg]  # bare on purpose, see above
    ) -> dict[str, Any]:
        """Poll `get_run_io` until the outputs are there, `outputs_error` is set or the wait is spent."""
        deadline = time.monotonic() + _OUTPUTS_WAIT_S
        delay = _POLL_FIRST_S
        while True:
            io = await stock_io(name, project, domain, ctx)
            if io.get("outputs") is not None or io.get("outputs_error"):
                return io
            if time.monotonic() + delay > deadline:
                io["outputs_error"] = (
                    f"outputs not yet readable: Flyte had not published the outputs of run {name!r} "
                    f"after {_OUTPUTS_WAIT_S:g}s. Retry get_run_io; if the run may still be going, "
                    "wait_for_run tells the two apart."
                )
                return io
            await asyncio.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_S)

    async def wait_for_run(
        name: str,
        timeout_s: float | None = None,
        project: str | None = None,
        domain: str | None = None,
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # see above
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
        """Block until a run reaches a terminal phase, then report it.

        Prefer this over polling `get_run` in a loop. Always pass `timeout_s` for runs that
        might be long. On timeout this does *not* error: it returns the run's current,
        non-terminal status with `timed_out: true`. A run that SUCCEEDED is reported only once
        its outputs are readable by `get_run_io` (`outputs_ready: true`), up to a bounded wait;
        if they are not, `outputs_ready` is false and `get_run_io` should be retried.
        """
        result = await stock_wait(name, timeout_s, project, domain, ctx)
        if _phase_name(result.get("phase")) in _SUCCESS_PHASES:
            io = await outputs_when_readable(name, project, domain, ctx)
            result["outputs_ready"] = io.get("outputs") is not None
        return result

    async def get_run_io(
        name: str,
        project: str | None = None,
        domain: str | None = None,
        ctx: MCPContext | None = None,  # type: ignore[type-arg]  # see above
    ) -> dict:  # type: ignore[type-arg]  # bare on purpose, see above
        """Get a run's inputs, and its outputs once it has finished.

        `outputs` is null if the run failed. Otherwise this waits (bounded) for the outputs to become
        readable, since a run that has just SUCCEEDED can still look as if it is going; if they are
        not, `outputs_error` says so and the call can be repeated. If the outputs exist but cannot be
        decoded, `outputs_error` explains why instead of the outputs silently coming back null.
        """
        io = await stock_io(name, project, domain, ctx)
        if io.get("outputs") is not None or io.get("outputs_error"):
            return io
        # Null outputs are right for a run that failed. Any other phase may be stale: the read model that
        # `get_run` reads lags the one that SUCCEEDED, so only the failure phases are trusted as they are.
        if _phase_name((await stock_get_run(name, project, domain, ctx)).get("phase")) in _FAILURE_PHASES:
            return io
        return await outputs_when_readable(name, project, domain, ctx)

    return {"wait_for_run": wait_for_run, "get_run_io": get_run_io}


def _use_patient_tools(environment: FlyteMCPAppEnvironment) -> None:
    """Swap the stock `wait_for_run` and `get_run_io` in `environment.mcp` for patient ones.

    The replacements come from `_wrap_tools_to_wait_for_outputs`.
    """
    server = environment.mcp
    assert server is not None  # built in the environment's __post_init__
    for tool_name, fn in _wrap_tools_to_wait_for_outputs(build_tool_functions(environment)).items():
        server.remove_tool(tool_name)
        info = TOOL_REGISTRY[tool_name]  # type: ignore[index]  # keys are the registered tool names
        server.add_tool(fn, name=tool_name, annotations=info.annotations())


_use_patient_tools(app)


def _scoped_to_the_factory(name: str, fn: ToolFn) -> ToolFn:
    """Wrap `fn` so it refuses a project or domain other than the factory's, else calls through.

    The stock tools take any `project` and `domain` and run the call there as the caller, so without this
    chat could reach every project the caller's Union RBAC allows. Empty means "the server default", which
    is the factory's (`FLYTE_MCP_PROJECT`/`FLYTE_MCP_DOMAIN`).
    """
    signature = inspect.signature(fn)

    # functools.wraps copies the signature, annotations and docstring, so FastMCP builds the same schema.
    @functools.wraps(fn)
    async def scoped(*args: Any, **kwargs: Any) -> Any:
        given = signature.bind(*args, **kwargs).arguments
        for argument, factory_value in (("project", PROJECT), ("domain", DOMAIN)):
            value = given.get(argument)
            if value and value != factory_value:
                raise ToolError(
                    f"{name}: {argument} {value!r} is not allowed; this server is limited to "
                    f"{argument} {factory_value!r}."
                )
        return await fn(*args, **kwargs)

    return scoped


def _list_only_the_factory_project(stock: ToolFn) -> ToolFn:
    """`list_projects` that shows only the factory's project."""

    @functools.wraps(stock)
    async def list_projects(limit: int = 100, ctx: MCPContext | None = None) -> Any:  # type: ignore[type-arg]
        # Ask for everything: the stock limit counts projects before this filter drops all but one.
        everything = cast(list[dict[str, Any]], await stock(10**6, ctx))
        return [p for p in everything if p["id"] == PROJECT][:limit]

    return list_projects


def _get_only_the_factory_project(stock: ToolFn) -> ToolFn:
    """`get_project` that refuses every project but the factory's."""

    @functools.wraps(stock)
    async def get_project(name: str, ctx: MCPContext | None = None) -> Any:  # type: ignore[type-arg]
        if name != PROJECT:
            raise ToolError(
                f"get_project: project {name!r} is not allowed; this server is limited to {PROJECT!r}."
            )
        return await stock(name, ctx)

    return get_project


def _scope_tools_to_the_factory(environment: FlyteMCPAppEnvironment) -> None:
    """Swap every registered tool that takes a `project` or `domain` for one that refuses the wrong ones.

    Run after `_use_patient_tools`, so the patient `wait_for_run` and `get_run_io` are the ones wrapped.
    Tools with neither (`whoami`, the searches) are left alone.
    """
    server = environment.mcp
    assert server is not None  # built in the environment's __post_init__
    for tool in server._tool_manager.list_tools():  # FastMCP has no public way to read a tool's function
        name, fn = tool.name, cast(ToolFn, tool.fn)
        if name == "list_projects":
            replacement = _list_only_the_factory_project(fn)
        elif name == "get_project":
            replacement = _get_only_the_factory_project(fn)
        elif {"project", "domain"} & set(inspect.signature(fn).parameters):
            replacement = _scoped_to_the_factory(name, fn)
        else:
            continue
        server.remove_tool(name)
        server.add_tool(replacement, name=name, annotations=TOOL_REGISTRY[name].annotations())  # type: ignore[index]


_scope_tools_to_the_factory(app)


# The environment built its Starlette app (which snapshots FastMCP's routes) when `app` was created, so a
# route added now only shows up once that app is rebuilt. FastMCP serves it under the mount: `PAGE_PATH`.
_grant_page.register(app.mcp)
app._starlette_app = app._create_starlette_app()
