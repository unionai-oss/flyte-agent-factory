"""A coding-agent session (Claude Code or Codex) that lives as long as the project does.

One Flyte run == one project. That mapping is the whole design:

- A run has no time bound in Flyte (`max_runtime` unset means unlimited), so a
  run can sit there for months -- the lifetime of a feature, which is what the
  user actually wants to track.
- `flyte.new_condition` only works inside a task, and conditions are how both
  the human and the chat agent that started the project talk to it. An app
  couldn't offer them.
- The session only makes outbound calls -- it opens no inbound port -- so the
  agent needs no ingress, which removes the one thing an app would have given us.
- Run history, the report, abort, and resource accounting all come out per
  project for free.

Subagent swarms need no special handling: Claude Code's subagents, agent teams
and dynamic workflows are threads of this one process, so they stay inside this
one run. The pod is sized for several at once, and `_auth.DENIED_TOOLS` stops
the model shelling out to `flyte run` and starting runs nobody is watching.

    # kick off (from a shell, a script, or a chat agent via MCP)
    flyte run src/flyte_agent_factory/agent.py project \
        --brief "Add OAuth device-flow login to the CLI" \
        --repo git@github.com:me/myproject.git

The run's report follows the project; direction arrives as Flyte conditions.
"""

from __future__ import annotations

import asyncio
import functools
import html
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox
import httpx
from flyte.io import Dir

from flyte_agent_factory import (
    _auth,
    _author,
    _board,
    _browser_exit_node,
    _checkpoint,
    _codex_login,
    _context_guard,
    _demo_checks,
    _gates,
    _gcp,
    _git,
    _github,
    _grants_supervisor,
    _inbox,
    _instructions,
    _issue_file,
    _issues,
    _judgement,
    _local_browser,
    _meter,
    _names,
    _policy,
    _profile,
    _pull_requests,
    _review,
    _reviewer,
    _router,
    _session_config,
    _session_log,
    _session_setup,
    _standards,
    _state,
    _subagent_run,
    _supervise,
    _supervise_support,
    _timeline,
    _usage,
)
from flyte_agent_factory._auth import (
    assert_claude_available,
    mark_trusted,
    write_settings,
)
from flyte_agent_factory._author_session import AuthorSession
from flyte_agent_factory._bridge import (
    BRIDGE_DIR,
    Bridge,
    _write_atomic,
    ensure_dirs,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._ci_status import CiStatus
from flyte_agent_factory._codex_process import CodexAuthor
from flyte_agent_factory._conditions import open_condition
from flyte_agent_factory._delegation import (
    Delegations,
    delegate_limit,
    usable_repo,
)
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import VOICE_PACKAGES, agent_image
from flyte_agent_factory._process import ClaudeProcess
from flyte_agent_factory.child import write_context

# Kept for ops/model_eval, which grades prompts with the verdict pattern; the review loop owns it now.
_VERDICT = _review._VERDICT

#: How the session runs when the caller does not say. See `project` for why;
#: override per run with `--permission_mode` when a project wants prompts.
_AUTO_PERMISSION_MODE = "bypassPermissions"


def _token_secret_env() -> dict[str, str]:
    """Return the setup-token's secret name for pods, so their meters name the account."""
    name = _names.getenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "").strip()
    return {"AGENT_FACTORY_CLAUDE_TOKEN_SECRET": name} if name else {}


def _shell_policy_env() -> dict[str, str]:
    """Return the deploying shell's role-policy variables, under their current names, for the pod.

    The policy keys and the tooling variables (`_names.TOOLING_FIXED`): the other `AGENT_FACTORY_*`
    variables are this machine's paths (base dir, workspace) or are set for the pod by the lines around it.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if k.startswith(_names.PREFIX)
        and k not in _names.POD_FIXED | {_profile.PROFILE_ENV, _profile.TOML_ENV}
    }


def _codex_token_secret_env() -> dict[str, str]:
    """Return the Codex token's secret name for pods, so their meters name the account; {} without one."""
    return (
        {"AGENT_FACTORY_CODEX_TOKEN_SECRET": _session_config.CODEX_TOKEN_SECRET}
        if _session_config.CODEX_TOKEN_SECRET
        else {}
    )


def _credentials_secret() -> list[flyte.Secret]:
    """Return the Claude setup-token, and the Codex access token if deployed with one."""
    out = []
    token_secret = _names.getenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", "").strip()
    if token_secret:
        out.append(flyte.Secret(key=token_secret, as_env_var="CLAUDE_CODE_OAUTH_TOKEN"))
    if _session_config.CODEX_TOKEN_SECRET:
        out.append(
            flyte.Secret(
                key=_session_config.CODEX_TOKEN_SECRET, as_env_var=_session_config.CODEX_TOKEN_ENV_VAR
            )
        )
    return out


def _secrets() -> list[flyte.Secret]:
    """Secrets to mount, chosen at deploy time.

    A secret that doesn't exist is a hard pod-level mount failure in Flyte, not
    a soft miss, so every optional secret has to be opted into when deploying
    rather than discovered at runtime.

        AGENT_FACTORY_BROWSER=0                     # no browser in the image
        AGENT_FACTORY_SECRETS=github_token,npm_token=NPM_TOKEN
    """
    # A secret that does not exist is a hard pod mount failure rather than a
    # soft miss, so every optional secret is opted into at deploy time.
    secrets = _credentials_secret()

    for spec in filter(None, (s.strip() for s in _names.getenv("AGENT_FACTORY_SECRETS", "").split(","))):
        key, _, env_var = spec.partition("=")
        secrets.append(flyte.Secret(key=key, as_env_var=env_var or None))

    proxy = _browser_exit_node.settings()
    if proxy["tailscale_secret"]:
        secrets.append(flyte.Secret(key=proxy["tailscale_secret"], mount=Path("/etc/flyte/secrets")))
    return secrets


# Whether the project image carries Chromium and the Playwright MCP server, so
# a session browses in its own pod rather than contending for the shared app.
# A deploy-time choice because a TaskEnvironment's image is fixed at deploy,
# and because a project that never browses should not pay Chromium's cold
# start. The per-run `browser` argument still decides whether to *start* it.
_IN_POD_BROWSER = _names.getenv("AGENT_FACTORY_BROWSER", "1") not in ("0", "false", "")


#: Each profile slot is deployed under its own name (the deploy sets AGENT_FACTORY_ENV_NAME), which fixes its
#: secret set on the TaskEnvironment at deploy time. `agent-factory` is only the default for local and dev
#: use; no factory deploy path registers it.
_ENV_NAME = _names.getenv("AGENT_FACTORY_ENV_NAME") or "agent-factory"
#: A sub-agent launched from a pod is built from this module as imported there, so the name rides into
#: the pod in `env_vars` below; each slot gets its own name so the registry never holds one
#: `agent-factory-subagent` that means different slots depending on who deployed last.
_SUBAGENT_ENV_NAME = _names.getenv("AGENT_FACTORY_SUBAGENT_ENV_NAME") or "agent-factory-subagent"
_PROFILE_SLOT = (
    next((slot for slot in _session_config._PROFILE.slots if slot.env == _ENV_NAME), None)
    if _session_config._PROFILE
    else None
)
#: Every identity's token is mounted on every slot; which project may use which is decided at run time.
_browser_identities = _session_config._PROFILE.browser.identities if _session_config._PROFILE else ()

env = flyte.TaskEnvironment(
    name=_ENV_NAME,
    # Modules reached lazily (`_factory` and its deps; the `credential-grant` command) must ship explicitly.
    include=(
        "_factory.py",
        "_deploy.py",
        "_factory_base.py",
        "_factory_grants.py",
        "_factory_live.py",
        "_factory_meters.py",
        "_factory_policy.py",
        "_factory_recreate.py",
        "_factory_status.py",
        "_grants_cli.py",
        # The gate launches this by name; importing it would also register its task environment.
        "_slow_lane.py",
    ),
    # The voice stack rides along with the browser: a session asked to narrate
    # cannot usefully install 3GB of wheels mid-recording, and a browser-less
    # project has nothing to record anyway.
    image=(
        agent_image(
            browser=True, tailscale=bool(_browser_exit_node.settings()["tailscale_secret"])
        ).with_pip_packages(*VOICE_PACKAGES)
        if _IN_POD_BROWSER
        else agent_image(browser=False)
    ),
    # Disk was 30Gi while this pod mounted a Volume: that needs the
    # `smarter-devices/fuse` device, which only some nodes advertise, and a
    # large ephemeral-storage request narrowed an already tight subset -- a
    # 100Gi request alone made it unschedulable. Volumes are gone, so that
    # constraint is gone with them, and 30Gi turned out to be genuinely tight:
    # a project that downloaded LJSpeech and VCTK had its first attempt killed
    # with no application log at all, which is what disk-pressure eviction
    # looks like from inside.
    #
    # Still modest on CPU and memory on purpose. Heavy work belongs in a child
    # action with its own resources, not in the supervisor -- this pod's job is
    # to hold a session alive, and `_warn_on_low_headroom` now says so in the
    # report when either is running out.
    resources=flyte.Resources(cpu=(1, 4), memory=("2Gi", "12Gi"), disk="120Gi"),
    # On-demand, deliberately. This pod holds the Claude Code session: a
    # preemption kills the conversation and the run restarts from the last
    # checkpoint. It is also the only thing that can fall *back* to on-demand
    # for its children, and an interruptible parent would need spot capacity
    # just to start, so it could never reach that fallback.
    interruptible=False,
    # Unpacked into a fresh list so it takes `TaskEnvironment`'s `list[str | Secret]` (lists are invariant).
    secrets=[
        *_secrets(),
        # Every slot carries every identity's token, for the supervisor only (`_secret_env.session_env`).
        *(
            flyte.Secret(key=identity.token_secret, as_env_var=identity.token_env)
            for identity in _browser_identities
        ),
    ],
    # No paths here: these are captured when `flyte deploy` runs, on a machine
    # whose HOME has nothing to do with the container's. The task exports them
    # itself at startup, via `_export_runtime_env`.
    env_vars={
        "PYTHONUNBUFFERED": "1",
        "AGENT_FACTORY_ENGINE_SHA": _names.getenv("AGENT_FACTORY_ENGINE_SHA", ""),
        "AGENT_FACTORY_BROWSER_IDENTITY_NAMES": ",".join(i.name for i in _browser_identities),
        **_browser_exit_node.deploy_env(_session_config._PROFILE),
        # The token's secret name travels into the pod, so its meters name the account.
        **_token_secret_env(),
        # A sub-agent launched from the pod must be the one deployed beside this environment.
        "AGENT_FACTORY_SUBAGENT_ENV_NAME": _SUBAGENT_ENV_NAME,
        **_codex_token_secret_env(),
        # Which mounted deploy key belongs to which repo (`_profile.Profile.deploy_env`);
        # `_git.install_deploy_key` reads it in the pod.
        **(
            {_git.REPO_KEYS_ENV_VAR: os.environ[_git.REPO_KEYS_ENV_VAR]}
            if _names.getenv(_git.REPO_KEYS_ENV_VAR)
            else {}
        ),
        # Per-role model/effort/engine choices made at deploy (e.g. every judge on Codex),
        # carried into the pod where the supervisor reads them.
        **_shell_policy_env(),
        # The profile's role policy applies over the shell's, whichever way this is deployed.
        # A slot's own policy comes with it; outside a slot (local and dev use) the global one applies.
        **(_session_config._PROFILE.policy_env_for(_PROFILE_SLOT) if _session_config._PROFILE else {}),
        # The profile itself, for what the pod reads from it (ops tools, console hosts).
        **_profile.pod_env(),
    },
)
# `include` is anchored at this file. The SDK finds the declaring file by skipping every path containing
# "/flyte/" as its own, so in a checkout under the pods' /home/flyte it picks a caller's frame or none.
env._declaring_file = os.path.abspath(__file__)


# Setup tasks (`gcp_bootstrap`) run here: they need no Claude or Codex credential, so none is mounted,
# and no volume -- they do not touch a project's state.
setup_env = flyte.TaskEnvironment(
    name="factory-setup",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
    env_vars={"PYTHONUNBUFFERED": "1", **_profile.pod_env()},
)

# Sub-agents run here, not in `env`. Secrets are declared per environment, so a
# sub-agent sharing `env` would inherit every secret the parent mounts -- the
# deploy key, any API tokens -- and a sub-agent is the least supervised thing in
# the system. It gets the Claude token and nothing else, and no volume: its
# workspace arrives as a typed input and leaves as a typed output.
subagent_env = flyte.TaskEnvironment(
    name=_SUBAGENT_ENV_NAME,
    image=agent_image(),
    resources=flyte.Resources(cpu=(1, 4), memory=("2Gi", "8Gi"), disk="50Gi"),
    secrets=[*_credentials_secret()],  # see `env` above: a copy typed as `list[str | Secret]`
    env_vars={
        "PYTHONUNBUFFERED": "1",
        "AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE": _names.getenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", "claude"),
        **_token_secret_env(),
        **_codex_token_secret_env(),
    },
    # Spot by default: a swarm is the bulk of the compute, it is discounted,
    # and spot is a separate pool from the on-demand nodes this cluster has
    # exhausted -- so it is both cheaper and likelier to be placed. A preempted
    # sub-agent is retried on the platform's own budget rather than consuming
    # `retries`; the cost is the work it had done, which for a short role
    # invocation is small. Override to False for a sub-agent doing something
    # long enough that losing it would hurt.
    interruptible=True,
    # No `reusable` policy. A warm pool was the original design, but a reusable
    # (actor) action runs the image's `unionai-actor-bridge`, which the agent
    # image does not contain: every pod died with StartError, after 25 placement
    # attempts and ~26 minutes, and reviewers -- the only users -- could never
    # run (seen in a past run). Plain tasks need no bridge. If the bridge is ever added
    # to the image this can be restored; per-invocation paths below already
    # assume a shared process, so nothing else changes.
)


@env.task(report=True, retries=20)
async def project(
    brief: str,
    name: str = "",
    repo: str = "",
    branch: str = "",
    model: str = "",
    effort: str = "",
    permission_mode: str = "",
    browser: bool = True,
    snapshot_every_s: int = 600,
    workspace_in: Dir | None = None,
    demo_package: str = "",
    issue: str = "",
    hard_pin: bool = False,
    engine: str = "",
) -> tuple[str, Dir]:
    """Run a Claude Code project for as long as it takes.

    Args:
        engine: `claude` or `codex` pins the engine the author session runs on. Empty uses the profile's
            `COORDINATOR_ENGINE` policy, else the router's choice among the engines the slot has a token for.
        hard_pin: Wait for pinned engines instead of moving benchmark arms to another engine.
        issue: Associated GitHub issue as owner/repo#number; board updates and milestone comments only.
        brief: What to build. Written to `.claude-flyte/BRIEF.md` and handed to the session as
            its opening instruction.
        name: Stable project id, and the key its snapshots are stored under.
            Re-running with the same name resumes that project. Defaults to the
            run name, which gives a fresh project per run.
        repo: Git remote to clone on first start. Ignored when resuming.
        branch: Branch to clone.
        model: Model alias or full name for the session.
        effort: `claude --effort` level (see `_policy.EFFORTS`); empty keeps the CLI default.
        permission_mode: Claude Code permission mode. Defaults to `bypassPermissions`,
            so a permission prompt cannot stall the session. Set explicitly
            to override.
        browser: Wire up the browser app as an MCP server.
        snapshot_every_s: How often to push the workspace to object storage.
        demo_package: The directory under `demo/` this brief is responsible
            for. Set it whenever the brief asks for a demo: the completion
            check then looks at that package and nothing else. Without it the
            check guesses from git, which is wrong as soon as two agents share
            a repository -- one rebased onto `main` and was then told to fix a
            package belonging to a live session next door.
        workspace_in: The workspace a previous run of this project returned.
            Pass it to continue that project; omit it to start fresh. Ordinary
            typed data flow -- no bucket convention to know, and nothing
            Union-specific, so this runs on OSS Flyte too.

    Returns:
        A summary of how the project ended, and the workspace as a `Dir` to
        pass back as `workspace_in` to continue it.

    """
    _usage._hard_pins = hard_pin
    # First, before any child exists: same-uid processes (the session's Bash calls) cannot then read
    # this process's credentials from /proc/<pid>/environ or /proc/<pid>/mem.
    _auth.set_non_dumpable()
    timeline = _timeline._Timeline()
    # Before anything is spawned: no child inherits the App's key, under either of its names.
    app_key = _pull_requests.take_app_key(
        _session_config._PROFILE.github.private_key_secret if _session_config._PROFILE else ""
    )
    board_token = _board.take_token(
        _session_config._PROFILE.board.pat_secret
        if _session_config._PROFILE and _session_config._PROFILE.board
        else ""
    )
    try:
        author = _policy.resolve("author")
        # Shape only: Claude Code owns the alias list (`opusplan`, `sonnet[1m]`, full ids).
        if not re.fullmatch(r"[A-Za-z0-9][\w.:\[\]-]*", author.model):
            raise ValueError(f"invalid author model {author.model!r}")
    except ValueError as exc:
        timeline.row("⚠️", f"author misconfigured: {exc}; using defaults")
        author = _policy.DEFAULTS["author"]
    explicit_model, explicit_effort = model, effort
    model, effort = model or author.model, effort or author.effort
    ctx = flyte.ctx()
    # `ActionID.__post_init__` defaults run_name to the action name, so it is never None here.
    run_name = cast(str, ctx.action.run_name) if ctx and ctx.action else "local"
    project_name = name.strip() or run_name or "claude-project"
    _supervise_support._released = False
    _session_config._project_name = project_name
    _session_config._project_repo = repo
    _session_config._project_brief = brief
    _session_config._demo_kind = _standards.demo_kind(brief, demo_package)

    timeline.header("project", project_name)
    timeline.header("run", run_name)
    timeline.header("model", f"{model} ({effort or 'default effort'})")
    # The console's run page, from the same profile the probes use; "" where there is no console.
    timeline.run_url = _board.run_url(_session_config._PROFILE, run_name) if ctx and ctx.action else ""
    github = _session_config._PROFILE.github if _session_config._PROFILE else None
    milestone_app = (
        _github.GithubApp(github.app_id, app_key) if github and github.configured and app_key else None
    )
    # Quietly: `_start_board` normalises it again below and is the one that says when it cannot.
    timeline.issues = await asyncio.to_thread(
        _issues.for_project,
        _session_config._PROFILE,
        project_name,
        run_name,
        milestone_app,
        _supervise_support._board_issue(issue, lambda *row: None),
        # Flyte keeps the original run start across attempts; the pod's start would hide old markers.
        started_at=(
            ctx.run_start_time.timestamp()  # type: ignore[union-attr]  # None is excluded by the getattr
            if ctx and getattr(ctx, "run_start_time", None)
            else None
        ),
    )
    if timeline.issues:
        timeline.issues.milestone("started", f"Started: {timeline.run_url or run_name}")
    timeline.attempt = int(getattr(ctx, "attempt_number", 0) or 0)
    timeline.repo, timeline.branch = repo, branch
    await timeline.flush(force=True)

    # --- mount ---------------------------------------------------------- #
    # The workspace and the session transcript live on a Volume, so there is
    # nothing to unpack: after this they are simply on disk.
    state = await _state.restore(root=_session_config.STATE_ROOT, workspace_in=workspace_in)
    await _checkpoint._note_checkpoint_location(timeline, run_name)
    _usage._meters = _meter.MeterStore(_session_config.STATE_ROOT / "meters")
    os.environ["AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE"] = "claude"
    _session_setup._export_runtime_env()
    if _checkpoint._ended_path().is_file():
        # An earlier attempt of this run finished and then lost its pod before returning. Another
        # session now would run on a login the next project may already hold.
        # A truncated marker (pod lost mid-write) is still "ended": never raise on every retry.
        try:
            marker = json.loads(_checkpoint._ended_path().read_text())
            ended = str(marker["outcome"])
            ended_released = marker.get("released") is True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            ended, ended_released = "marker unreadable", False
        timeline.row("🏁", f"already ended ({ended}); publishing the workspace without a session")
        published = await _state.publish(state)
        await _checkpoint._hand_over_slot(timeline, run_name, completion_key=app_key)
        timeline.phase("released" if ended_released else "done")
        await _supervise_support._flush_milestones(timeline)
        await timeline.flush(force=True)
        return f"{project_name}: {ended}", published
    timeline.row(
        "♻️" if state.resumed else "✨",
        f"resumed from {state.resumed_from}" if state.resumed else "new project",
    )

    # --- sign in -------------------------------------------------------- #
    author_engine = _usage._author_engine = await _author._choose_author_engine(
        _author._engine_input_pin(engine), _usage._author_engines(), state.resumed, timeline
    )
    os.environ["AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE"] = author_engine
    if previous_owner := _author._record_author_choice(author_engine, state.resumed, timeline):
        await _author._queue_resume_handoff(previous_owner, author_engine, brief, timeline)
    if author_engine == "codex":
        setting = _policy.resolve_codex("author", warn=timeline.row)
        model = (
            explicit_model
            if explicit_model and not _policy.is_claude_model(explicit_model)
            else setting.model
        )
        effort = explicit_effort if explicit_effort in _policy.CODEX_EFFORTS else setting.effort
        timeline.header("engine", "codex")
        timeline.header("model", f"{model or 'codex default'} ({effort or 'default effort'})")
        _author._prepare_codex_home()  # fails here, before any workspace work, when the token is missing
        if not state.resumed:
            _author._CODEX_THREAD_FILE.unlink(missing_ok=True)
    else:
        assert_claude_available()
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
            raise RuntimeError(
                "no Claude setup-token available. Run `claude setup-token`, store it as a Flyte secret "
                "(`flyte create secret <name>`) and name it in [secrets] claude_token."
            )
        _session_config._claude_account()  # fingerprint the token for the meters
        _auth.drop_stale_login(_session_config.CONFIG_DIR)
        # The same mode whether or not anyone is watching. A TUI permission dialog
        # is not a way to reach a human: it is invisible from the run report
        # and it blocks the session until someone attaches. Two runs launched to build demos sat on one having
        # done no work at all.
        #
        # Asking is not removed by this, it is moved to the channel built for it.
        # `ask-human` opens a Flyte condition: visible in the console, answerable
        # from anywhere, durable across restarts. Nobody is assumed to be watching,
        # so forwarded dialogs expire into their default rather than hold the session.
        mode = permission_mode or _AUTO_PERMISSION_MODE
        write_settings(_session_config.CONFIG_DIR, hold_dialogs=False, extra=_context_guard.settings_extra())
        mark_trusted(_session_config.CONFIG_DIR, _session_config.WORKSPACE)

    # --- workspace ------------------------------------------------------ #
    await _session_setup._prepare_workspace(
        brief=brief, repo=repo, branch=branch, resuming=state.resumed, timeline=timeline
    )
    await _session_setup._start_pull_requests(repo, app_key, timeline)
    await _supervise_support._start_board(issue, project_name, board_token, timeline)
    await _issue_file.refresh(timeline, milestone_app)
    if not state.resumed:
        # Once, at creation: a resumed project's base is the one saved in its checkpoint, or the
        # root of history for legacy projects -- never the HEAD it resumed at.
        await asyncio.to_thread(_gates.capture_base, _session_config.WORKSPACE, _session_config._project_name)
    _session_setup._check_image_tools(_session_config._applicable_standards(), timeline)
    if author_engine == "codex":
        (_session_config.CODEX_HOME_DIR / "config.toml").unlink(
            missing_ok=True
        )  # rewritten by `_start_browser` if there is one
    gate = _session_setup._start_browser_gate(timeline)
    browser, local_browser = (await _session_setup._start_browser(timeline)) if browser else (False, None)
    if gate is not None and not browser:
        _session_setup._write_mcp_config(None)  # the personal entries do not need the shared browser
    # Written after wiring, so the session is only told about a browser it
    # actually has.
    # The same console handover the demo task does. Without it a project that
    # needs the Union console is blocked by construction: one spent three hours
    # reporting "the console is signed out", because every word of console-auth
    # guidance had gone into the demo memory and none into this one.
    console_token = _session_config.BASE_DIR / ".console-token"
    if browser and _session_config._CONSOLE_HOST and _local_browser.api_key():
        console_token.write_text(_local_browser.api_key())
        console_token.chmod(0o600)
        timeline.row("🔑", f"console token at {console_token} for the session to install")
    else:
        console_token = None  # type: ignore[assignment]

    # Fetched by the task, not the session: resolving an artifact needs a
    # control-plane credential, and three separate demo runs shipped music-only
    # video because the session was told to resolve it and could not. Handing
    # over a directory removes the question.
    voice_model = (
        await _demo_checks._fetch_voice_model(_session_config.WORKSPACE, timeline) if browser else None
    )

    delegation_available = usable_repo(_session_config.WORKSPACE)
    _session_setup._write_managed_block(
        # Codex reads `$CODEX_HOME/AGENTS.md` as its own standing instructions; a managed block in the
        # workspace's AGENTS.md would show up in the project's diff.
        _session_config.WORKSPACE / "CLAUDE.md"
        if author_engine == "claude"
        else _session_config.CODEX_HOME_DIR / "AGENTS.md",
        _instructions._project_memory(
            browser,
            console_token=console_token,
            name=project_name,
            voice_model=voice_model,
            delegation_available=delegation_available,
            layout=_session_config._layout(),
            branch=_session_setup._pr.branch if _session_setup._pr else "",
            default_branch=_session_setup._default_branch,
            branch_problem=_session_setup._branch_sync_problem,
            engine=author_engine,
            personal_browsers=gate.identities if gate else (),
        ),
    )
    if author_engine == "codex":
        timeline.row(
            "ℹ️",
            "codex coordinator: no .claude/agents sub-agent roles (use delegate), no PushNotification "
            "(use ask-human), no /compact between rounds",
        )

    ensure_dirs(BRIDGE_DIR)
    _session_setup._install_bridge_commands(delegation_available=delegation_available, grants=True)
    # Lets the session submit child actions of this run; see `child.attach`.
    context = write_context(_session_config.WORKSPACE)
    timeline.row("🔗", f"run context at {context.relative_to(_session_config.WORKSPACE)}")

    # --- run ------------------------------------------------------------ #
    stop = asyncio.Event()
    workspace_out: Dir | None = None
    outcome = "interrupted"
    bridge = Bridge(BRIDGE_DIR, on_event=timeline.row, registry=_inbox._question_registry_path())
    timeline.bridge = bridge
    bridge.browser_gate = gate
    bridge.judge = functools.partial(_judgement.run_judge, timeline=timeline)

    # After a handoff this start is the new author taking over, not a fresh start that lost the work.
    taking_over = _author._takeover_row(author_engine)
    if author_engine == "codex":
        # The thread the checkpoint saved, if any. `_start_codex_author` reads the same file.
        resume = state.resumed and CodexAuthor.saved_thread(_author._CODEX_THREAD_FILE) is not None
        if state.resumed and not resume:
            timeline.row("ℹ️", taking_over or "restored state holds no Codex thread; starting fresh")
    else:
        argv = _author._author_argv(project_name, mode, model, effort)
        if browser:
            # Named on the command line, not left in `.mcp.json` for discovery. A
            # project-level config is *offered* rather than loaded: Claude Code asks
            # "Use this MCP server?" with "Continue without using this MCP server"
            # as the highlighted default, and nobody is at the terminal to say
            # otherwise. Sessions therefore ran with a browser started, running and
            # advertised in their memory, and no `browser_*` tools -- which is a
            # much more confusing failure than having no browser at all.
            #
            # `--strict-mcp-config` keeps it to this one server, so a stray config
            # elsewhere in the image cannot add others.
            argv += ["--mcp-config", str(_session_config.WORKSPACE / ".mcp.json"), "--strict-mcp-config"]
        elif gate is not None:  # personal browsers without the shared one: the same reason applies
            argv += ["--mcp-config", str(_session_config.WORKSPACE / ".mcp.json"), "--strict-mcp-config"]
        # Resume the conversation rather than starting a second one in the same
        # project -- but only when there is one, and with a plain start as fallback.
        resume = state.resumed and _author.has_conversation(_session_config.CONFIG_DIR)
        owner_file = _session_config.STATE_ROOT / "author-conversation.json"
        if owner_file.is_file() and json.loads(owner_file.read_text()).get("fresh"):
            resume = False
            planned = json.loads(owner_file.read_text()).get("thread")
            if planned and any(
                p.stem == planned
                for p in _inbox._session_transcripts(_session_config.CONFIG_DIR, _session_config.WORKSPACE)
            ):
                # Started by a run that died before it recorded that: attach, rather than ask
                # Claude to create a session id that exists.
                resume = state.resumed
            elif planned:
                # A handoff planned this conversation's id before Claude existed. Creating it under
                # that id keeps the journal and the real session in agreement; any other id would
                # make the next restart resume a conversation that is not there.
                argv += ["--session-id", planned]
        if state.resumed and (taking_over or not resume):
            timeline.row("ℹ️", taking_over or "restored state holds no conversation; starting fresh")
        candidates = _author.session_argv_candidates(argv, resume=resume)
        if resume and owner_file.is_file():
            saved_owner = json.loads(owner_file.read_text())
            if saved_owner.get("engine") == "claude" and saved_owner.get("thread"):
                candidates[0] = [*argv, "--resume", saved_owner["thread"]]

    delegations = Delegations(
        BRIDGE_DIR,
        _session_config.WORKSPACE,
        _session_setup._delegate_worker(timeline),
        _usage._delegation_engine(timeline),
        timeline.row,
        max_jobs=delegate_limit(timeline.row),
        setting=_session_setup._delegate_setting,
        reattach=_session_setup._delegate_reattach(timeline),
    )
    timeline.delegations = delegations
    ci_status = CiStatus(BRIDGE_DIR, lambda: _session_setup._pr, timeline.row)
    credentials, grant_env = _grants_supervisor.start_credentials(project_name, timeline, bridge)

    # The inbox listens before the session exists, so a `/stop` typed while the session is starting is
    # heard without an author turn. Its other messages wait for `session`, which is set below.
    fatal: list[BaseException] = []
    session: asyncio.Future[ClaudeProcess] = asyncio.get_running_loop().create_future()
    # Duck-typed: ClaudeProcess, CodexAuthor, _NoSession and AuthorSession share the surface used below.
    proc: Any
    on_engine = functools.partial(_author._direct_engine, session)
    inbox_loop = functools.partial(_inbox._inbox_loop, on_engine=on_engine, on_late_answer=bridge.late_answer)
    inbox = asyncio.create_task(
        _supervise_support._supervised(
            inbox_loop(session, timeline, stop, _supervise_support._grant_round, credentials),
            "inbox",
            timeline,
            stop,
            fatal=fatal,
        ),
        name="inbox",
    )
    # Start the session before the other background tasks: they hold a reference to
    # `proc`, and `_start_session` may discard a process and start another.
    try:
        if author_engine == "codex":
            proc = _author._start_codex_author(timeline, model=model, effort=effort, extra_env=grant_env)
        else:
            starting = asyncio.ensure_future(
                _author._start_session(
                    candidates,
                    env={
                        **_session_setup._session_env(_session_setup._export_runtime_env()),
                        **_context_guard.session_env(),
                        **grant_env,
                    },
                    timeline=timeline,
                )
            )
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({starting, stopper}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopper.cancel()
                if not starting.done():
                    # Stopped while starting, or this task was cancelled: either way the start ends, and
                    # `_start_session` stops the process it had begun on the way out.
                    starting.cancel()
                    await asyncio.gather(starting, return_exceptions=True)
            if starting.cancelled():
                proc = _author._NoSession()
            else:
                proc = starting.result()
    except BaseException:
        inbox.cancel()
        credentials.close()
        raise
    if len(_usage._author_engines()) > 1:
        if not state.resumed:
            (_session_config.STATE_ROOT / "author-pending.json").unlink(missing_ok=True)
            (_session_config.STATE_ROOT / "author-conversation.json").unlink(missing_ok=True)
        owner_file = _session_config.STATE_ROOT / "author-conversation.json"
        if not owner_file.is_file():
            _write_atomic(owner_file, {"engine": author_engine})
        proc = AuthorSession(proc, _session_config.STATE_ROOT / "author-pending.json")
        saved_owner = json.loads(owner_file.read_text())
        _author._restore_refusal(proc, saved_owner)
        proc.on_limit = _author._author_handoff(proc, state, timeline, brief=brief)
    timeline.proc = proc
    session.set_result(proc)
    waiter = asyncio.create_task(
        proc.wait_for_exit()
        if isinstance(proc, AuthorSession)
        else proc.wait()
        if author_engine == "claude"
        else _author._wait_for_codex_crash(proc),
        name=author_engine,
    )

    # Gated on whether we actually resumed, not on whether state came back. A
    # restored config dir with no resumable conversation starts a fresh
    # session, and that session has never been told the brief.
    kickoff = _session_setup._kickoff_prompt()

    # Wrapped, because these are the run's senses and controls and all four
    # fail silently otherwise: they are gathered with `return_exceptions=True`
    # at shutdown, so a crash mid-run is never surfaced. A run was observed
    # with no open direction condition, which means the inbox loop had stopped
    # and the only way to send it anything -- including `/stop` -- was gone,
    # with nothing anywhere saying so.
    tasks = [
        asyncio.create_task(
            _supervise_support._supervised(
                _supervise_support._board_loop(timeline, stop), "board", timeline, stop, fatal=None
            ),
            name="board",
        ),
        asyncio.create_task(
            _supervise_support._supervised(delegations.run(), "delegations", timeline, stop, fatal=fatal),
            name="delegations",
        ),
        asyncio.create_task(
            _supervise_support._supervised(bridge.run(), "bridge", timeline, stop, fatal=fatal), name="bridge"
        ),
        asyncio.create_task(
            _supervise_support._supervised(ci_status.run(), "ci-status", timeline, stop, fatal=None),
            name="ci-status",
        ),
        asyncio.create_task(
            _supervise_support._supervised(
                _supervise._pump(
                    bridge,
                    proc,
                    timeline,
                    stop,
                    kickoff="" if resume else kickoff,
                    workspace=_session_config.WORKSPACE,
                    brief=brief,
                    demo_package=demo_package,
                    pinned_acceptance=_session_config.BASE_DIR / ".acceptance-pinned",
                    delegations=delegations,
                ),
                "pump",
                timeline,
                stop,
                fatal=fatal,
            ),
            name="pump",
        ),
        inbox,
        asyncio.create_task(
            _supervise_support._supervised(
                _checkpoint._checkpoint_loop(
                    state=state,
                    every=max(60, snapshot_every_s),
                    timeline=timeline,
                    stop=stop,
                ),
                "checkpoint",
                timeline,
                stop,
                fatal=fatal,
            ),
            name="checkpoint",
        ),
        asyncio.create_task(
            _supervise_support._supervised(
                _usage._meter_loop(timeline, stop), "meter", timeline, stop, fatal=None
            ),
            name="meter",
        ),
        asyncio.create_task(
            _supervise_support._supervised(
                _author._codex_login_loop(timeline, stop), "codex-login", timeline, stop, fatal=None
            ),
            name="codex-login",
        ),
        asyncio.create_task(
            _supervise_support._supervised(
                _supervise_support._dashboard_loop(timeline, stop), "dashboard", timeline, stop, fatal=None
            ),
            name="dashboard",
        ),
    ]
    if author_engine == "claude" or isinstance(proc, AuthorSession):
        # A dual-engine session may later acquire a Claude conversation.
        # They read the messages typed into the session and the Claude transcript.
        tasks += [
            asyncio.create_task(
                _supervise_support._supervised(
                    _inbox._watch_app_messages(proc, timeline, stop, credentials),
                    "app-direction",
                    timeline,
                    stop,
                    fatal=None,
                ),
                name="app-direction",
            ),
            asyncio.create_task(
                _supervise_support._supervised(
                    _session_log.feed_loop(
                        _inbox._session_feed(resume), lambda line: print(line, flush=True), stop
                    ),
                    "session-feed",
                    timeline,
                    stop,
                    fatal=None,
                ),
                name="session-feed",
            ),
        ]
    tasks += _grants_supervisor.start_supervised(credentials, session, stop)

    try:
        await timeline.flush(force=True)

        # Gated on whether we actually resumed, not on whether state came back.
        # A restored config dir with no resumable conversation starts a fresh
        # session, and that session has never been told the brief -- without
        # this it would sit idle forever, looking healthy.
        if stop.is_set():
            pass  # stopped while the session was starting: it takes no turn
        elif isinstance(proc, AuthorSession) and proc._paused:
            pass  # the pump re-routes the saved refusal before delivering any pending work
        elif isinstance(proc, AuthorSession) and proc.pending:
            await _author._resume_author_pending(proc, author_engine)
        elif not resume:
            await proc.submit(kickoff, label="Project kickoff")

        stopper = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()

        if waiter in done:
            status = waiter.result()
            if status == 0:
                # A clean exit is how ending the project from the session
                # works: `/exit` there ends the session, which ends the run
                # with a final checkpoint and a published workspace. No abort.
                outcome = "session ended cleanly"
            else:
                # A crash must not look like finishing. Raising lets Flyte
                # retry, which restores the checkpoint and resumes; returning
                # normally would quietly end a months-old project on a bug.
                tail = "\n".join(list(proc.tail)[-30:])
                if author_engine == "codex" and getattr(proc, "auth_failure", False):
                    await _author._codex_login_dead(proc, timeline)
                raise RuntimeError(
                    f"{'Codex' if author_engine == 'codex' else 'Claude Code'} exited with status {status}; "
                    f"retrying from the last checkpoint.\n{tail}"
                )
        elif fatal:
            # `stop` was set by a fatal error, not by a human. Ending normally
            # here would publish and report success for a run that never did
            # any work.
            await _inbox._stop_author(proc, timeline)
            raise fatal[0]
        else:
            outcome = (
                _supervise_support._release_note if _supervise_support._released else "stopped on request"
            )
            await _inbox._stop_author(proc, timeline)
    finally:
        stop.set()
        ended_normally = sys.exc_info()[0] is None
        if not ended_normally:
            timeline.phase("failed")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        credentials.close()  # before the final checkpoint: nothing of a grant outlives the project
        # A clean exit or crash can also leave background tools writing into the workspace.
        await _inbox._stop_author(proc, timeline)
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

        if isinstance(getattr(proc, "_on_line", None), _session_log.RawCapture):
            proc._on_line.close()

        try:
            await _supervise_support._refresh_child_usage(timeline)
        except (*REMOTE_ERRORS, *_usage._USAGE_READ_ERRORS) as exc:
            _timeline._log_line(f"[usage] child reports: {type(exc).__name__}", err=True)

        # Before publishing: Chromium holds files in the profile open, and a
        # profile snapshotted mid-write comes back corrupt and refuses to load.
        if local_browser is not None:
            await local_browser.stop()
        _session_setup._stop_browser_gate()

        # Publish on the way out even if the body raised: without this the
        # run's work is only in a checkpoint, which the next run cannot read.
        if ended_normally and run_name != "local":
            # In the checkpoint, so a pod lost after the hand-over below retries into
            # "already ended" rather than a second session. Not locally: there is no retry.
            try:
                _checkpoint._ended_path().write_text(
                    json.dumps({"outcome": outcome, "released": _supervise_support._released})
                )
            except OSError as e:
                timeline.row("⚠️", f"could not mark the project ended: {type(e).__name__}: {e}")
        try:
            await _state.save_checkpoint(state)
            workspace_out = await _state.publish(state)
            timeline.row("💾", "published the workspace")
        except REMOTE_ERRORS as e:  # the checkpoint and the workspace are both remote writes
            timeline.row("⚠️", f"final publish failed: {type(e).__name__}: {e}")
        if not ended_normally:  # the normal end flushes below, after its 🏁 row
            await _supervise_support._flush_milestones(timeline)
        await timeline.flush(force=True)

    if not timeline.session_end_written:
        session_ended = timeline.session_end if outcome == "session ended cleanly" else outcome
        session_ended = session_ended or outcome
        timeline.row("🏁", session_ended)
        timeline.activity(session_ended)
        timeline.session_end_written = True
    if workspace_out is None:
        timeline.phase("failed")
        await _supervise_support._flush_milestones(timeline)
        await timeline.flush(force=True)
        raise RuntimeError(f"{outcome}, but the workspace could not be published")
    await _checkpoint._hand_over_slot(timeline, run_name, completion_key=app_key)
    # A release keeps its own phase: "done" would read as finished-and-closed to the dashboard.
    timeline.phase("released" if _supervise_support._released else "done")
    await _supervise_support._flush_milestones(timeline)
    await timeline.flush(force=True)
    return f"{project_name}: {outcome}", workspace_out


if __name__ == "__main__":
    import sys

    flyte.init_from_config()
    run = flyte.run(
        project, brief=sys.argv[1] if len(sys.argv) > 1 else "Say hello, then wait for direction."
    )
    print(f"run: {run.url}")


# --------------------------------------------------------------------------- #
# Child actions: ML jobs and nested agents.
#
# Both run inside this run. `flyte.sandbox.create(...).run()` and
# `subagent(...)` are ordinary task invocations from inside a task, which Flyte
# records as child actions of this project -- own container, own image, own GPU,
# own retries, one run to watch.
# --------------------------------------------------------------------------- #


@subagent_env.task(report=True)
async def subagent(
    brief: str,
    workdir: Dir | None = None,
    repo: str = "",
    branch: str = "",
    model: str = _policy.DEFAULTS["author"].model,
    max_turns: int = 80,
    role: str = "",
    effort: str = "",
    answer_only: bool = False,
    engine: str = "",
    routed: bool = False,
    hard_pin: bool = False,
    partial_base: str = "",
) -> tuple[str, Dir]:
    """Return a nested Claude Code or Codex session in its own container, as a child action.

    Headless: a sub-agent is work to be delegated, not a session anyone
    needs to attach to, and `claude -p` needs no pty. It still gets conditions -- this is a task, so
    `ask-human` works here as it does in the parent, opening its question
    against this child action.

    Being a child action is the point: each one has its own logs, its own
    report and its own row in the run, so a swarm is readable instead of four
    roles interleaved in one stream.

    Returns the final text, plus its workspace so the parent can inspect what
    it did. Nothing is merged automatically: the parent decides what to take.

    Args:
        brief: What to do. The sub-agent's only instruction.
        workdir: A directory to work in, usually a slice of the parent's
            workspace.
        repo: Clone this instead, when the work belongs to another repository.
        branch: Branch to clone.
        model: Model alias or full name.
        effort: `claude --effort` level (see `_policy.EFFORTS`); empty keeps the CLI default.
        max_turns: Hard cap on agentic turns, so a confused sub-agent ends.
        answer_only: The caller reads only the final message and discards the
            workspace (reviewers, arbiter, planner); the sub-agent is told so,
            instead of being told its files are handed back.
        engine: `claude` or `codex` pins the engine (logged as a pin). Empty lets the
            router pick from the live meters.
        hard_pin: Wait for the pinned engine when exhausted (benchmark arms).
        routed: The caller has already routed this call (`_call_reviewer`); set by it, not by hand.
        partial_base: A delegation's base commit in `repo/` of the workdir. When a Claude session ends at
            `max_turns`, the sub-agent still returns, with `factory.partial.patch` (a diff against this
            commit) and `plan.md` (what remains) in the workspace, instead of failing and losing the work.
        role: One of the roles in `_assets/agents` -- `scope`, `discover`, `prototype`, `shape`,
            `build`, `verify`, or an old name (`product`, `designer`, `dev`, `qa`). Passed to Claude
            Code as `--agent`, which is its own mechanism for this; empty runs a general-purpose session.

    """
    import flyte.report

    _auth.set_non_dumpable()  # as in `project`: the sub-agent's children cannot read this process's secrets
    route_blob = None
    routes: list[dict[str, Any]] = []
    refusals: dict[str, float] = {}
    unusable: list[str] = []
    # `available` is taken now: the Claude runner strips its credential from os.environ, and a refusal
    # seen after that must still be recorded against Claude when the call is routed again.
    reroute = functools.partial(
        _subagent_run._reroute_after_limit,
        hard_pin=hard_pin,
        available=_subagent_run._available_engines(),
        refusals=refusals,
        unusable=unusable,
        routes=routes,
    )
    pin = engine
    if not routed:
        engine, model, effort, route_blob = await _subagent_run._route_direct_subagent(
            role, engine, model, effort, hard_pin=hard_pin
        )
        routes.append(route_blob)
        await _subagent_run._subagent_result(
            "", route_blob, routes=routes, refusals=refusals, unusable=unusable
        )
    engine = engine or "claude"

    # Per-invocation paths. `reusable` shares the container *and* the Python
    # process between concurrent sub-agents, so a fixed path here would let two
    # of them overwrite each other's workspace and cross-wire their questions.
    slot = tempfile.mkdtemp(prefix="subagent-")
    base = Path(slot)
    work = base / "workspace"
    work.mkdir(parents=True, exist_ok=True)

    if workdir is not None:
        await _checkpoint._download_workspace(workdir, work)
    elif repo:
        await _session_setup._clone(repo, branch, work)
    # So the sub-agent can stop the child actions it starts (`python -m flyte_agent_factory.child abort`,
    # which reads the context from where it runs). `.flyte/` is a harness path, so git leaves it out.
    # A local call is in no task and has no run to join.
    if flyte.ctx():
        write_context(work)

    # Each refusal excludes an engine, so this makes at most two CLI attempts.
    while True:
        try:
            config_dir = base / "claude"
            bridge_dir = base / "bridge"

            if engine == "codex":
                # The same setup as the Claude path -- the brief written, the harness excluded, the
                # bridge up -- before Codex starts. Returning before it left Codex with no brief, or a
                # parent's stale one, and no `ask-human` (found by Codex reviewing this very change).
                _session_setup._exclude_harness(work)
                (work / _session_config._BRIEF_FILE).parent.mkdir(parents=True, exist_ok=True)
                (work / _session_config._BRIEF_FILE).write_text(brief.strip() + "\n")
                bridge = Bridge(ensure_dirs(bridge_dir))
                _session_setup._install_bridge_commands()
                bridge_task = asyncio.create_task(bridge.run(), name="subagent-bridge")
                try:
                    summary = await _subagent_run._run_codex(
                        work,
                        base,
                        brief,
                        answer_only=answer_only,
                        role=role,
                        model=model,
                        effort=effort,
                        extra_env={
                            **_session_setup._session_env(_session_setup._export_runtime_env()),
                            "AGENT_FACTORY_WORKSPACE": str(work),
                            "AGENT_FACTORY_BRIDGE_DIR": str(bridge_dir),
                        },
                    )
                finally:
                    bridge_task.cancel()
                    await asyncio.gather(bridge_task, return_exceptions=True)
                return await _subagent_run._subagent_result(
                    summary, route_blob, routes=routes, refusals=refusals, unusable=unusable
                ), await Dir.from_local(str(work))

            if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
                raise RuntimeError(
                    "no Claude setup-token available for the sub-agent ([secrets] claude_token)"
                )
            write_settings(config_dir, hold_dialogs=False)
            mark_trusted(config_dir, work)

            _session_setup._exclude_harness(work)
            (work / _session_config._BRIEF_FILE).parent.mkdir(parents=True, exist_ok=True)
            (work / _session_config._BRIEF_FILE).write_text(brief.strip() + "\n")
            _session_setup._write_managed_block(
                work / "CLAUDE.md", _subagent_run._subagent_memory(answer_only=answer_only)
            )
            roles = _session_setup._seed_team(work)

            # `ask-human` works here too, against this child action.
            bridge = Bridge(ensure_dirs(bridge_dir))
            _session_setup._install_bridge_commands()
            bridge_task = asyncio.create_task(bridge.run(), name="subagent-bridge")

            argv = [
                "claude",
                "-p",
                (
                    f"Read {_session_config._BRIEF_FILE} and carry it out. "
                    "Your final message is your entire answer: "
                    "nothing you write to files is kept, so put everything the brief asks for in it."
                    if answer_only
                    else f"Read {_session_config._BRIEF_FILE} and carry it out. "
                    "Finish with a short report of what you did."
                ),
                "--output-format",
                "json",
                *_policy.argv(_policy.Setting(model, effort)),
                "--max-turns",
                str(max_turns),
                # Nothing here can answer a permission prompt, and a stalled sub-agent
                # burns its parent's wall clock. `ask-human` is the escape hatch.
                "--permission-mode",
                "bypassPermissions",
            ]
            if role:
                if role not in roles:
                    raise ValueError(f"unknown role {role!r}; available: {', '.join(sorted(roles))}")
                argv += ["--agent", role]

            env = {
                **_session_setup._session_env(_session_setup._export_runtime_env()),
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "AGENT_FACTORY_WORKSPACE": str(work),
                "AGENT_FACTORY_BRIDGE_DIR": str(bridge_dir),
            }

            try:
                # The setup-token goes in through a pipe: the sub-agent's Bash calls can print its env.
                with _auth.token_fd(env, os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()) as (
                    child_env,
                    pass_fds,
                ):
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        cwd=str(work),
                        env=child_env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        pass_fds=pass_fds,
                    )
                raw, err = await proc.communicate()
            finally:
                bridge_task.cancel()
                await asyncio.gather(bridge_task, return_exceptions=True)

            stderr = err.decode(errors="replace")
            try:
                payload = json.loads(raw.decode(errors="replace"))
            except ValueError:
                payload = {}
            observed = _meter.from_claude_events([payload], _session_config._claude_account())
            if observed:
                if observed.limited_until:
                    observed.limited_until = _usage._keep_later_reset(
                        str(payload.get("result") or ""), observed.limited_until
                    )
                try:
                    await flyte.report.log.aio(_meter.report_comment(observed), do_flush=True)
                except (*_usage._METER_ERRORS, *REMOTE_ERRORS, RuntimeError) as exc:
                    _timeline._log_line(f"[meter] claude: {type(exc).__name__}", err=True)
            if partial_base and not answer_only and payload.get("subtype") == "error_max_turns":
                # Checked before the exit code: the CLI exits non-zero at the turn limit.
                summary = await _subagent_run._partial_handback(
                    work,
                    partial_base,
                    max_turns,
                    payload,
                    functools.partial(
                        _subagent_run._ask_remaining_work, argv[0], work, env, payload, model, effort
                    ),
                )
                return await _subagent_run._subagent_result(
                    summary + _usage._usage_trailer(raw.decode(errors="replace")),
                    route_blob,
                    routes=routes,
                    refusals=refusals,
                    unusable=unusable,
                ), await Dir.from_local(str(work))
            if proc.returncode != 0:
                # A usage-limit refusal exits 1 with the result JSON on stdout: name it as such.
                _subagent_run._raise_on_cli_error(raw.decode(errors="replace"), only_limit=True)
                # stdout too: `claude -p` puts its own errors there, not on stderr.
                raise RuntimeError(
                    f"sub-agent exited {proc.returncode}.\n"
                    f"stdout: {raw.decode(errors='replace')[-1500:] or '(empty)'}\n"
                    f"stderr: {stderr[-1500:] or '(empty)'}"
                )

            _subagent_run._raise_on_cli_error(raw.decode(errors="replace"))
            summary = _subagent_run._final_text(raw.decode(errors="replace")) or _reviewer._NO_FINAL_MESSAGE
            await flyte.report.log.aio(
                f"<h3>Sub-agent{f' · {html.escape(role)}' if role else ''}</h3>"
                f"<pre>{html.escape(brief[:1000])}</pre><hr><pre>{html.escape(summary[:4000])}</pre>",
                do_flush=True,
            )
            # Added after the report log so the log stays clean; `_call_reviewer` strips it.
            return await _subagent_run._subagent_result(
                summary + _usage._usage_trailer(raw.decode(errors="replace")),
                route_blob,
                routes=routes,
                refusals=refusals,
                unusable=unusable,
            ), await Dir.from_local(str(work))

        except RuntimeError as exc:
            if _usage.USAGE_LIMIT_MARK not in (error := str(exc)):
                raise
            # A stale reset (its time already passed) says nothing about now; the fallback does.
            until = _usage._refusal_until(error) or time.time() + _session_config.LIMIT_RETRY_S
            deferred: _router.RouterDeferred | None = None  # an unrouted call with no engine left
            if not routed:  # a routed caller re-runs the brief with a fresh worker itself
                try:
                    engine, model, effort, route_blob = await reroute(
                        role, pin, model, effort, engine, error, until
                    )
                    continue
                except _router.RouterDeferred as no_engine:
                    deferred = no_engine  # `engine` is still the refused one
            # A delegate's work so far would be lost with this worker: hand it back, as at the turn limit.
            if partial_base and not answer_only and _usage._CODEX_LOGIN_UNUSABLE not in error:
                handback = await _subagent_run._limit_handback(work, partial_base, engine, until, routed)
                if handback is not None:
                    return await _subagent_run._subagent_result(
                        handback, route_blob, routes=routes, refusals=refusals, unusable=unusable
                    ), await Dir.from_local(str(work))
            if deferred is not None:
                raise deferred from exc
            raise


_reviewer.subagent_task = subagent
_author.project_task = project
_gcp_setting = _gcp.setting


@setup_env.task(report=True)
async def codex_bootstrap_login(secret_name: str = "", attempts: int = 3) -> str:
    """Sign Codex in to a ChatGPT account (personal Plus or Pro works) and store it as a slot's `codex_login`.

    A link and a one-time code are shown as a HITL prompt (`codex-login-...`) and in the report: open the
    link, sign in and enter the code; the login is stored as the Flyte secret `secret_name`. With no
    argument, the profile's only slot `codex_login`. Name the same secret on one slot as `codex_login`.
    """
    if not secret_name:
        logins = _codex_login.profile_logins()
        if len(logins) != 1:
            raise ValueError(
                "pass secret_name: the profile names "
                + (", ".join(logins) if logins else "no slot codex_login")
            )
        secret_name = logins[0]
    if not secret_name.startswith("codex_login"):
        # Chat can start this task, so it must not be able to overwrite any other secret.
        raise ValueError(f"secret_name must start with codex_login, not {secret_name!r}")
    return await _codex_login.bootstrap(
        secret_name, store=_author._replace_secret, timeline=_timeline._Timeline(), attempts=attempts
    )


@setup_env.task(report=True)
async def gcp_bootstrap(
    project_id: str,
    billing_account: str = "",
    create_project: bool = False,
    region: str = "us-west1",
    secret_name: str = "",
    # Flyte has no tuple input type (it would pickle), so these are `list | None`; None means the
    # profile's value, else the default.
    roles: list[str] | None = None,
    billing_roles: list[str] | None = None,
    apis: list[str] | None = None,
    max_wait_s: int = 900,
    attempts: int = 3,
) -> str:
    """Sign in to Google by link and leave a project with a least-privilege service-account key.

    A link is shown as a HITL prompt; open it, sign in, and signal the code the page displays.
    With that short-lived token the task prepares `project_id` (creating it with
    `create_project`), enables the APIs, creates the `factory-infra` service account with
    `roles` on the project and `billing_roles` on `billing_account`, and stores one key for it
    as the Flyte secret `secret_name`. Your token is kept in memory and revoked at the end.

    An empty `secret_name`, `roles` or `apis` takes the profile's `[gcp]` value, then the
    default (`roles/owner`; the list of APIs the infrastructure brief needs). Delete the key and
    the service account when the project is finished.
    """
    gcp = _session_config._PROFILE.gcp if _session_config._PROFILE else None
    timeline = _timeline._Timeline()
    timeline.header("what", "bootstrapping GCP access for a project")
    timeline.header("project", project_id or "(none given)")
    timeline.header("service account", f"{_gcp.SA_NAME}@{project_id}.iam.gserviceaccount.com")
    await timeline.flush(force=True)

    async def ask(prompt: str, attempt: int) -> str:
        timeline.row("🔐", f"sign-in link shown (attempt {attempt}); waiting for the code")
        await timeline.flush(force=True)
        condition, _ = await open_condition(
            "gcp-login", attempt, prompt=prompt, prompt_type="markdown", data_type=str
        )
        try:
            return await asyncio.wait_for(condition.wait.aio(), timeout=max_wait_s) or ""
        except asyncio.TimeoutError:
            timeline.row("⏳", f"no code within {max_wait_s}s")
            return ""

    async def store(name: str, value: str) -> None:
        await flyte.remote.Secret.create.aio(name=name, value=value)

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            summary = await _gcp.bootstrap(
                project_id=project_id,
                billing_account=billing_account,
                create_project=create_project,
                region=region,
                secret_name=secret_name or (gcp.secret_name if gcp else ""),
                roles=_gcp_setting(roles, gcp.roles if gcp else (), _gcp.DEFAULT_ROLES),
                billing_roles=_gcp_setting(billing_roles, (), _gcp.DEFAULT_BILLING_ROLES),
                apis=_gcp_setting(apis, gcp.apis if gcp else (), _gcp.DEFAULT_APIS),
                ask=ask,
                store_secret=store,
                log=timeline.row,
                http=http,
                attempts=attempts,
            )
    finally:
        await timeline.flush(force=True)
    return summary
