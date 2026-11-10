"""Workspace and session preparation: runtime env, team seed, git checkout, browser and MCP config."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import flyte
import flyte.errors
import flyte.remote
import flyte.report
import flyte.sandbox
from flyte.io import Dir
from flyte.models import ActionPhase

from flyte_agent_factory import (
    _browser_exit_node,
    _browser_gate,
    _checkpoint,
    _gates,
    _git,
    _github,
    _local_browser,
    _policy,
    _pull_requests,
    _push_guard,
    _reviewer,
    _roles,
    _secret_env,
    _session_config,
    _session_log,
    _timeline,
    _usage,
    _workspace,
)
from flyte_agent_factory._bridge import (
    BRIDGE_DIR,
)

# Imported here, not where used: the deploy bundles only modules loaded at import time, and
# a lazy import left `_delegation` out of the bundle (every project died at startup).
from flyte_agent_factory._delegation import (
    Reattach,
    Worker,
)
from flyte_agent_factory._errors import REMOTE_ERRORS
from flyte_agent_factory._images import AGENT_ROLES_DIR
from flyte_agent_factory.browser import (
    McpServer,
    browser_endpoint,
    codex_mcp_config,
    mcp_config,
)

_ASSETS = Path(__file__).parent / "_assets"

#: What the harness writes at a project's workspace root is listed in `_gates.HARNESS_PATHS`, for
#: the clone's local `.git/info/exclude` and for the gates alike. Anchored: an unanchored
#: `CLAUDE.md` hid every `*/CLAUDE.md` in the repo.
_HARNESS_PATHS = _gates.HARNESS_PATHS
_MEMORY_BEGIN = _gates.MEMORY_BEGIN
_MEMORY_END = _gates.MEMORY_END


def _write_managed_block(path: Path, body: str) -> None:
    """Idempotently maintain our section of a file we don't own."""
    path.write_text(_gates.apply_managed_block(path.read_text() if path.exists() else "", body))


# Re-exported from `_git`, which owns these so the voice factory can clone
# without importing this module. The names are kept because every call site
# and several tests refer to them.
GIT_DEPLOY_KEY_ENV_VAR = _git.GIT_DEPLOY_KEY_ENV_VAR
_install_deploy_key = _git.install_deploy_key
_clone = _git.clone


# Re-exported from `_secret_env`, which owns the scrubbing so the gates, the checkpoint, delegation and
# the browser can use it without importing this module. The names are kept for call sites and tests;
# the reasons for each list are written there.
_MODEL_BILLING_VARS = _secret_env.MODEL_BILLING_VARS


def _session_env(env: dict[str, str]) -> dict[str, str]:
    """`_secret_env.session_env` under this pod's profile: no raw credential, for the session and children."""
    return _secret_env.session_env(env, _session_config._PROFILE)


def _export_runtime_env() -> dict[str, str]:
    """Publish the container's real paths so subprocesses agree on them.

    The `ask-human` / `flyte-note` wrappers, and any shell Claude Code spawns,
    inherit these. Written into `os.environ` rather than passed explicitly
    because the wrappers are reached through a shell the agent controls.
    """
    os.environ["CLAUDE_CONFIG_DIR"] = str(_session_config.CONFIG_DIR)
    os.environ["AGENT_FACTORY_WORKSPACE"] = str(_session_config.WORKSPACE)
    os.environ["AGENT_FACTORY_BRIDGE_DIR"] = str(BRIDGE_DIR)
    os.environ["PATH"] = f"{_session_config.BIN_DIR}:{os.environ.get('PATH', '')}"
    bundle_root = str(Path(__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    if bundle_root not in existing.split(":"):
        os.environ["PYTHONPATH"] = f"{bundle_root}:{existing}" if existing else bundle_root

    # The agent is told to commit early and often, and a container has no git
    # identity -- `git commit` fails with "Please tell me who you are" and the
    # model has no way to fix it that survives a restart. Set it via the
    # environment rather than `git config`, so it applies to every repository.
    os.environ.setdefault("GIT_AUTHOR_NAME", "Agent Factory")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "agent-factory@localhost")
    if _session_config._project_name:
        for role in ("AUTHOR", "COMMITTER"):
            os.environ[f"GIT_{role}_NAME"] = "Agent Factory"
            os.environ[f"GIT_{role}_EMAIL"] = _gates.commit_identity(_session_config._project_name)
    os.environ.setdefault("GIT_COMMITTER_NAME", os.environ["GIT_AUTHOR_NAME"])
    os.environ.setdefault("GIT_COMMITTER_EMAIL", os.environ["GIT_AUTHOR_EMAIL"])
    # Nobody is at a terminal to type a password, and git would otherwise block
    # forever on a private remote.
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    # Flyte v2 never sets FLYTE_INTERNAL_IMAGE, but the session instructions tell sessions to reuse
    # it; the pod carries the same image id in _F_IMG_ID. Sub-agents get it through the same env.
    if "_F_IMG_ID" in os.environ:
        os.environ.setdefault("FLYTE_INTERNAL_IMAGE", os.environ["_F_IMG_ID"])
    _install_deploy_key(_session_config._project_repo)

    return dict(os.environ)


# Seeded once and then left alone: this is where a human writes the house rules
# the agents are told to follow, so re-seeding would overwrite their edits.
_DESIGN_SEED = """\
# Design guidance

House rules for this project. Every agent reads this; it overrides their own
instructions where they conflict. Edit it freely -- it is not regenerated.

General engineering and security practice lives in `.claude-flyte/standards/`,
including "Don't reinvent the wheel". Read those standards alongside
this project guidance.

## Things about this environment that have already cost time

- **Containers run as an unprivileged user.** Nothing may assume it can write
  outside `$HOME` (or `/tmp`). This broke three separate things before it was
  understood: the agent's own state paths, the `claude` binary's install
  location, and the browser app's profile directory. Derive writable paths from
  `$HOME`; never hardcode `/var/lib`, `/state` or `/usr/local/bin`.
- **The code bundle carries Python modules, not package data.** Anything that
  is not a `.py` has to be put into the image (`Image.with_source_folder`), or
  it will be present locally and silently absent in the container.
- **Building an image is not running it.** An image that builds can still exit
  on its first line. Prove a component starts, not that it compiles.

## Running work on the cluster

- Attach once with `flyte_agent_factory.child.attach()`, then write ordinary Flyte.
  Anything you submit becomes a child action of this run.
- **Images must already exist.** Reuse `$FLYTE_INTERNAL_IMAGE` or a public
  image; use `flyte.sandbox.create(packages=[...])` when you genuinely need a
  new one built.
- **Always bound the queue** with `flyte.Timeout(max_queued_time=...)`, and on
  `MaxQueuedTimeExceededError` fall back -- spot first, then on-demand with the
  timeout cleared. A job with no queue bound waits forever and that is not a
  failure.
- **Keep the workspace small.** It is checkpointed whole on a timer, so cost
  scales with its size. Datasets and model artifacts belong in a child action's
  outputs, not in the working tree.

## Working as a team

Use the subagents in `.claude/agents/`, named by what they do: `scope` settles scope and
acceptance criteria, `discover` finds out what the problem really is (DISCOVERY.md),
`prototype` tries two or more shapes of anything a person will see or use, `shape` settles the
design (DESIGN-RATIONALE.md), `build` builds it, and `verify` -- several in parallel, on
different aspects -- finds out whether it actually works. Don't let one agent both build and
bless its own work. The old names `product`, `designer`, `dev` and `qa` still work as aliases
this release.
"""


def _roles_dir() -> Path:
    """Return the directory the role files live in.

    The image is the source of truth; the in-package copy is the fallback for a run with no image (a
    local run, CI), for Claude and Codex sub-agents alike.
    """
    roles_dir = Path(AGENT_ROLES_DIR)
    return roles_dir if roles_dir.is_dir() else _ASSETS / "agents"


def _alias_text(alias: str, role: str, text: str) -> str:
    """Render role file `text` as alias `alias`: its own name and a first body line naming the role."""
    header, sep, body = text.partition("\n---\n")
    header = header.replace(f"name: {role}\n", f"name: {alias}\n", 1)
    return f"{header}{sep}Alias of `{role}`; use `{role}`.\n\n{body}"


def _seed_team(workspace: Path) -> list[str]:
    """Install the role subagents, and the design guidance they are told to read.

    These are Claude Code's own subagent definitions rather than anything
    invented here: a markdown file per role in `.claude/agents/`, which the
    session can then delegate to and run several of in parallel. They live in
    the same process, so a swarm stays inside this one Flyte run.
    """
    agents_dir = workspace / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    # The image is the source of truth; the in-package copy is the fallback for
    # a local run, where there is no image.
    roles_dir = _roles_dir()

    installed = []
    for source in sorted(roles_dir.glob("*.md")):
        (agents_dir / source.name).write_text(source.read_text())
        installed.append(source.stem)
    # The aliases are generated rather than kept as a second copy of each prompt on disk.
    for alias, role in _roles.ROLE_ALIASES.items():
        if role in installed:
            (agents_dir / f"{alias}.md").write_text(
                _alias_text(alias, role, (roles_dir / f"{role}.md").read_text())
            )
            installed.append(alias)

    design = workspace / _session_config.DESIGN_FILE
    if not design.exists():
        design.write_text(_DESIGN_SEED)
    return installed


def _install_bridge_commands(
    bin_dir: Path = _session_config.BIN_DIR, *, delegation_available: bool = False, grants: bool = False
) -> None:
    """Put `ask-human` and `flyte-note` on PATH for the session to call.

    `grants` adds `credential-grant`, for the main session only: a sub-agent pod has no grant server.

    Thin wrappers around the code bundle Flyte already ships, so the image
    doesn't need this package installed into it.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "ask-human": ("flyte_agent_factory._bridge", "main", ["ask"]),
        "flyte-note": ("flyte_agent_factory._bridge", "main", ["note"]),
        "end-project": ("flyte_agent_factory._bridge", "main", ["end"]),
        "ci-status": ("flyte_agent_factory._bridge", "main", ["ci-status"]),
    }
    if grants:
        commands["credential-grant"] = ("flyte_agent_factory._grants_cli", "main", [])
    else:
        (bin_dir / "credential-grant").unlink(missing_ok=True)
    if not delegation_available:
        (bin_dir / "delegate").unlink(missing_ok=True)
    if delegation_available:
        commands["delegate"] = ("flyte_agent_factory._bridge", "main", ["delegate"])
    for name, (module, entry, leading) in commands.items():
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{_workspace.build_entry_command(module, entry, tuple(leading))}\n")
        script.chmod(0o755)


def _check_image_tools(files: dict[str, str] | None, timeline: _timeline._Timeline) -> None:
    """Report each enforced tool this image lacks. A diagnostic: it never raises."""
    try:
        required = _gates.required_tools(files or {})
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        _timeline._log_line(f"[image] could not read the required tools: {type(exc).__name__}", err=True)
        return
    for tool, rules in required.items():
        if not shutil.which(tool):
            message = _gates.image_defect(tool, rules)
            if message not in timeline._image_defects:
                timeline._image_defects.append(message)
                timeline.row("🚨", message)


async def _prepare_workspace(
    *, brief: str, repo: str, branch: str, resuming: bool, timeline: _timeline._Timeline
) -> None:
    _session_config.WORKSPACE.mkdir(parents=True, exist_ok=True)

    # Clone before anything else writes here: `git clone` refuses a non-empty
    # target, and the inbox/brief below would make it non-empty.
    if repo and not resuming and not (_session_config.WORKSPACE / ".git").exists():
        timeline.row("📥", f"cloning {repo}")
        await _clone(repo, branch, _session_config.WORKSPACE)
    if repo and (_session_config.WORKSPACE / ".git").exists():
        # Defence in depth beside the workspace-only deploy key: a push anywhere else is refused locally.
        _push_guard.install(_session_config.WORKSPACE, repo)

    (_session_config.WORKSPACE / _session_config._INBOX_DIR).mkdir(parents=True, exist_ok=True)
    _exclude_harness(_session_config.WORKSPACE)

    # The brief is written to a file and referenced by one short line, because
    # typing multi-line text into the TUI would submit it a line at a time.
    (_session_config.WORKSPACE / _session_config._BRIEF_FILE).parent.mkdir(parents=True, exist_ok=True)
    (_session_config.WORKSPACE / _session_config._BRIEF_FILE).write_text(brief.strip() + "\n")

    installed = _seed_team(_session_config.WORKSPACE)
    if installed:
        timeline.row("👥", f"team: {', '.join(installed)}")
    else:
        timeline.row("⚠️", "no role definitions found; the swarm roles are unavailable")


def _exclude_harness(workspace: Path) -> None:
    """Keep the harness's files out of `git status` in a cloned workspace.

    `.git/info/exclude` is local to the clone, so this changes nothing in the
    repository. It only hides *untracked* files: a harness path the repo already
    tracks is still the repo's (a project-owned `CLAUDE.md`, say), which is why
    the brief no longer lives at the root at all.
    """
    try:
        top, out = subprocess.run(
            _secret_env.git_argv("rev-parse", "--show-toplevel", "--git-path", "info/exclude"),
            cwd=workspace,
            env=_secret_env.child_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\n")[:2]
    except (OSError, subprocess.CalledProcessError, ValueError):
        return  # not a git workspace
    if Path(top).resolve() != workspace.resolve():
        return  # a folder inside some other repo: its root-anchored patterns would land there
    exclude = Path(out) if Path(out).is_absolute() else workspace / out
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.is_file() else ""
    missing = [p for p in _HARNESS_PATHS if p not in text.splitlines()]
    if missing:
        lead = "\n" if text and not text.endswith("\n") else ""
        with exclude.open("a") as f:
            f.write(lead + "".join(f"{p}\n" for p in ["# agent-factory harness files", *missing]))


async def _remote_default_branch() -> str:
    """Return the branch the remote's HEAD points at ("" when it cannot be read)."""
    code, out = await _checkpoint._git_out(
        _session_config.WORKSPACE, "ls-remote", "--symref", "origin", "HEAD", timeout=60
    )
    match = re.match(r"ref: refs/heads/(\S+)\tHEAD", out) if code == 0 else None
    if match:
        return match[1]
    # The remote cannot be asked: the clone recorded its HEAD when it was made.
    code, head = await _checkpoint._git_out(
        _session_config.WORKSPACE, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", timeout=10
    )
    return head.strip().removeprefix("origin/") if code == 0 and head.strip().startswith("origin/") else ""


async def _sync_with_remote_branch(branch: str, timeline: _timeline._Timeline) -> None:
    """Bring a local `branch` that is behind (or apart from) `origin/<branch>` up to date.

    A resumed pod restores the workspace from a checkpoint that can predate pushes, or a human may
    have pushed to the branch. Fast-forward when possible, merge when diverged; never rewrite
    anything. On failure the tree is left clean and the session is told in its guidance.
    """
    global _branch_sync_problem
    remote = f"origin/{branch}"
    if (await _checkpoint._git_out(_session_config.WORKSPACE, "rev-parse", "--verify", "--quiet", remote))[
        0
    ] != 0:
        return  # not on the remote (yet, or deleted after a merge): nothing to catch up with
    if (await _checkpoint._git_out(_session_config.WORKSPACE, "merge-base", "--is-ancestor", remote, "HEAD"))[
        0
    ] == 0:
        return  # already contains everything on the remote
    code, _ = await _checkpoint._git_out(_session_config.WORKSPACE, "merge", "--ff-only", remote, timeout=60)
    if code != 0:
        code, _ = await _checkpoint._git_out(
            _session_config.WORKSPACE, "merge", "--no-edit", remote, timeout=60
        )
        if code != 0:
            await _checkpoint._git_out(_session_config.WORKSPACE, "merge", "--abort", timeout=30)
    if code != 0:
        _branch_sync_problem = (
            f"could not bring {branch} up to date with {remote} (the merge failed, conflicts or "
            f"uncommitted changes); run the push command below and resolve it before you build on this"
        )
        timeline.row("⚠️", _branch_sync_problem)
    else:
        timeline.row("🌿", f"brought {branch} up to date with {remote}")


async def _checkout_project_branch(
    branch: str, timeline: _timeline._Timeline, default_branch: str = ""
) -> bool:
    """Put the workspace on `branch`: kept if it exists here or on the remote, else created.

    A new branch starts at `origin/<default branch>`, not at whatever the clone's HEAD is. The
    default branch is the caller's (the GitHub App's answer), else the remote's HEAD, else `main`.
    """
    global _default_branch, _branch_sync_problem
    _branch_sync_problem = ""
    if (await _checkpoint._git_out(_session_config.WORKSPACE, "fetch", "--prune", "origin", timeout=120))[
        0
    ] != 0:
        timeline.row("⚠️", "git fetch origin failed; the project branch is chosen from what is already here")
    _default_branch = default_branch or await _remote_default_branch() or "main"
    _, current = await _checkpoint._git_out(
        _session_config.WORKSPACE, "rev-parse", "--abbrev-ref", "HEAD", timeout=10
    )
    if current == branch:
        await _sync_with_remote_branch(branch, timeline)
        return True
    base = f"origin/{_default_branch}"
    if (
        await _checkpoint._git_out(
            _session_config.WORKSPACE, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
        )
    )[0] == 0:
        checkout = ["checkout", branch]
    elif (
        await _checkpoint._git_out(
            _session_config.WORKSPACE, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"
        )
    )[0] == 0:
        checkout = ["checkout", "-b", branch, "--track", f"origin/{branch}"]
    elif (await _checkpoint._git_out(_session_config.WORKSPACE, "rev-parse", "--verify", "--quiet", base))[
        0
    ] == 0:
        # --no-track: the upstream is set by the first guided push, never `origin/<default>`.
        checkout = ["checkout", "-b", branch, "--no-track", base]
    else:
        timeline.row("⚠️", f"{base} not found; {branch} starts from the current commit")
        checkout = ["checkout", "-b", branch]
    if (await _checkpoint._git_out(_session_config.WORKSPACE, *checkout, timeout=30))[0] != 0:
        timeline.row(
            "⚠️",
            f"could not switch to {branch}; the session is told to push only to {branch}, "
            f"never {_default_branch}",
        )
        return False
    await _sync_with_remote_branch(branch, timeline)
    timeline.row("🌿", f"working on {branch} (pull requests go to {_default_branch})")
    return True


async def _start_pull_requests(repo: str, app_key: str, timeline: _timeline._Timeline) -> None:
    """Switch to the project branch and arm pull requests, when the profile has `[github]`.

    Anything that stops it before the branch step leaves `_pr` unset and the project exactly as it
    was before the App. After that point a failed checkout does not fall back to the default
    branch: the guidance stays on the project branch and the PR is opened once it is on the remote.
    """
    global _pr
    _pr = None
    github = _session_config._PROFILE.github if _session_config._PROFILE else None
    if not (github and github.configured):
        return
    if not app_key:
        timeline.row(
            "⚠️", f"[github] is set but {_pull_requests.KEY_ENV_VAR} is not mounted; no pull requests"
        )
        return
    assert _session_config._PROFILE is not None  # `github` came from it
    try:
        slug = _github.parse_repo(repo or _session_config._PROFILE.repo)
    except _github.GithubError as exc:
        timeline.row("⚠️", f"no pull requests: {exc}")
        return
    if not (_session_config.WORKSPACE / ".git").exists():
        timeline.row("⚠️", "no pull requests: the workspace is not a git clone")
        return
    app = _github.GithubApp(github.app_id, app_key)
    try:
        default_branch = await asyncio.to_thread(app.default_branch, slug)
    except (
        _github.GithubError,
        KeyError,
    ):  # GitHub or the key failing only costs this lookup; git answers instead
        default_branch = ""
    candidate = _pull_requests.PullRequests(
        app,
        slug,
        _session_config._project_name,
        timeline.run_url,
        timeline.row,
        issue=timeline.issues.issue if timeline.issues else "",
    )
    await _checkout_project_branch(candidate.branch, timeline, default_branch)
    _pr = candidate
    timeline.branch = candidate.branch


async def _start_browser(timeline: _timeline._Timeline) -> tuple[bool, _local_browser.LocalBrowser | None]:
    """Give the session a browser, preferring one in this pod.

    In-pod is preferred because it is this project's own browser: nothing to
    contend over, a profile that rides the checkpoint, and -- the part that
    changes what the agent can do -- a shared network namespace, so a page the
    agent serves on localhost is reachable. Against the shared app it is not,
    which is why showing that app agent-authored content means a `data:` URL.

    Falls back to the shared app when this image has no Chromium, and to no
    browser at all if neither works. A browser is optional; a session that
    cannot start without one would be worse than one that reports it has none.
    """
    proxy = _browser_exit_node.settings()
    required = bool(proxy["exit_node"] or proxy["tailscale_secret"])
    if required and (_session_config.WORKSPACE / ".mcp.json").exists():
        # A checkpoint may still advertise a direct shared browser from an earlier deployment.
        _write_mcp_config(None)
    if _local_browser.available():
        browser = _local_browser.LocalBrowser(
            profile_dir=_session_config.STATE_ROOT / "browser-profile",
            output_dir=_session_config.STATE_ROOT / "browser-output",
            exit_node=proxy["exit_node"],
            tailscale_secret=proxy["tailscale_secret"],
        )
        try:
            endpoint = await browser.start()
        except (OSError, RuntimeError) as e:  # fall back rather than fail the run
            timeline.row("⚠️", f"in-pod browser failed to start: {type(e).__name__}: {e}")
            await browser.stop()
            if required:
                timeline.row("⚠️", "exit node required; continuing without browser (direct fallback blocked)")
                return False, None
        else:
            _write_mcp_config(endpoint, browser.client_headers)
            timeline.row("🌐", f"browser in this pod at {endpoint}/mcp (localhost is reachable)")
            return True, browser

    if required:
        timeline.row("⚠️", "exit node required but local browser unavailable; continuing without browser")
        return False, None

    try:
        endpoint = browser_endpoint()
    except REMOTE_ERRORS as e:  # the browser is optional
        timeline.row("⚠️", f"no browser available, continuing without one: {type(e).__name__}: {e}")
        return False, None

    _write_mcp_config(endpoint)
    timeline.row("🌐", f"shared browser app at {endpoint}/mcp")
    timeline.row("⚠️", "the shared browser is one seat for all projects; localhost is NOT reachable from it")
    return True, None


def _write_mcp_config(endpoint: str | None, headers: dict[str, str] | None = None) -> Path:
    """Merge or remove the browser servers in the workspace `.mcp.json`, and say where."""
    config_path = _session_config.WORKSPACE / ".mcp.json"
    config: dict[str, Any] = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError:
            config = {}
    servers = config.setdefault("mcpServers", {})
    # The shared entry is rewritten below; the personal ones are the gate's, and a stale one (a checkpoint
    # from a run with another grant) must not survive.
    for stale in [name for name in servers if name == "browser" or name.startswith("browser-")]:
        del servers[stale]
    personal = personal_servers()
    servers.update(personal)
    if endpoint is not None:
        servers.update(mcp_config(endpoint, headers)["mcpServers"])
    config_path.write_text(json.dumps(config, indent=2))
    if _usage._author_engine == "codex":
        _write_codex_mcp_config({k: v for k, v in servers.items() if k == "browser" or k in personal})
    return config_path


def personal_servers() -> dict[str, McpServer]:
    """Return the `.mcp.json` entries of the granted personal browsers: the gate's address, no credential.

    The gate (supervisor-side) adds the bearer token, so neither this file, which is checkpointed and
    uploaded with the workspace, nor the session ever holds it.
    """
    if _gate is None:
        return {}
    return {
        f"browser-{name}": {"type": "http", "url": f"{_gate.url}/{name}/mcp"} for name in _gate.identities
    }


def _start_browser_gate(timeline: _timeline._Timeline) -> _browser_gate.Gate | None:
    """Start the gate for this run's grant, publish its address to the session, and say what is granted."""
    global _gate
    _gate = _browser_gate.start(_session_config._PROFILE, os.environ)
    if _gate is not None:
        os.environ[_browser_gate.GATE_ENV] = _gate.url
        timeline.row("🔐", f"personal browsers granted: {', '.join(_gate.identities)}")
    return _gate


def _stop_browser_gate() -> None:
    global _gate
    if _gate is not None:
        _gate.stop()
        os.environ.pop(_browser_gate.GATE_ENV, None)
    _gate = None


def _write_codex_mcp_config(servers: dict[str, McpServer]) -> None:
    """Return the same browser servers in `$CODEX_HOME/config.toml`, which is where Codex reads them.

    Rendered from the endpoint of this start, never carried over: `project` removes the previous
    file first, so a checkpoint cannot bring back an address that no longer exists.
    """
    if _gate is not None:
        for name in _gate.identities:
            if f"browser-{name}" in servers and not _gate.exit_ready(name):
                del servers[f"browser-{name}"]
    if servers:
        config = _session_config.CODEX_HOME_DIR / "config.toml"
        config.write_text(codex_mcp_config(servers))
        config.chmod(0o600)  # private like auth.json beside it, though it holds no secret value


def _log_session_line(timeline: _timeline._Timeline, path: Path | None = None) -> _session_log.RawCapture:
    """File the session's terminal text away; send nothing to the report or the task log.

    The pty carries a TUI: it repaints on every keystroke, so the same line arrived
    dozens of times, in fragments, with words run together. In the report that buried
    the session URL and the agent's own notes; in the task log it was thousands of
    one-character lines. Reconstructing lines from a repainting TUI is lossy by
    nature, so it is kept out of both. The cleaned text goes to a bounded rotating
    file for debugging, and `_inbox._session_feed` logs what the session did, from its
    transcript.

    The report carries structured events only: notes, questions, child-action
    outcomes, checkpoints, and the header.
    """
    return _session_log.RawCapture(path or _session_config.RAW_TERMINAL_LOG)


#: Set by `project` when the profile has `[github]` and the repo is on github.com: the project works on
#: `factory/<name>` and the supervisor opens its pull request. None keeps today's push-to-default-branch.
_pr: _pull_requests.PullRequests | None = None
#: The grant gate, while this project has a browser grant (`_start_browser_gate`).
_gate: _browser_gate.Gate | None = None
#: The repo's default branch; what the project branch starts from and merges.
_default_branch = "main"
#: Set when a resumed project branch could not be brought up to date with the remote; shown in the guidance.
_branch_sync_problem = ""


def _delegate_setting(engine: str) -> str:
    """`model/effort` a delegation will run on, for the dashboard only."""
    setting = _policy.resolve_codex("delegate") if engine == "codex" else _policy.DEFAULTS["author"]
    return f"{engine} {setting.model or 'default'}/{setting.effort or 'default'}"


#: Turn budget of a delegate worker. Named here rather than inherited from `subagent`'s default, so the
#: limit a delegation can hit is a decision someone can find; a worker that hits it hands back partial work.
_DELEGATE_MAX_TURNS = 80


def _delegate_worker(timeline: _timeline._Timeline) -> Worker:
    """Return the `Delegations` worker: one delegated job as a sub-agent child action."""

    async def delegate_worker(
        target: Path,
        brief: str,
        engine: str,
        *,
        answer_only: bool = False,
        on_engine: Callable[[str, str], None] | None = None,
    ) -> str:
        await _usage._read_meters(timeline)
        upload = target / "upload"
        upload.mkdir()
        shutil.copy2(target / "repo.tar.gz", upload / "repo.tar.gz")
        # `Delegations.service` leaves the base commit beside the snapshot: a worker that runs out of
        # turns diffs against it. Absent for a worker driven without a delegation.
        base_file = target / "base"
        partial_base = base_file.read_text().strip() if base_file.is_file() and not answer_only else ""
        summary, output = await _reviewer._call_reviewer(
            timeline=timeline,
            purpose="delegate",
            # How the dashboard learns the engine that ran after a fallback.
            on_engine=on_engine,
            round_=_usage._metering_round,
            # A research question is told its final message is the answer, not to hand back files.
            role="" if answer_only else "dev",
            answer_only=answer_only,
            engine=engine,
            brief=brief,
            workdir=await Dir.from_local(str(upload)),
            max_turns=_DELEGATE_MAX_TURNS,
            partial_base=partial_base,
        )
        await output.download(str(target / "result"))
        return summary

    return delegate_worker


#: Waits between reads of a reattached action's details while the read model catches up (~90s in all).
_REATTACH_SETTLE_DELAYS_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0)


def _delegate_reattach(timeline: _timeline._Timeline) -> Reattach:
    """Return the `Delegations` reattach hook: collect a worker action submitted before a restart."""

    async def reattach(target: Path, run_name: str, name: str) -> str:
        action = await flyte.remote.Action.get.aio(run_name=run_name, name=name)
        # `Action.wait` is a plain coroutine in the SDK, not syncified: there is no `.aio`.
        await action.wait(quiet=True)
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # `Action.wait` swallows a cancellation and returns with the action still running. Say it
            # was cancelled, so `Delegations` leaves the job `running` for the next reattach.
            raise asyncio.CancelledError
        # Fetched again, and until it agrees: `wait` follows the watch stream, and the read model
        # `ActionDetails.get` serves can still say RUNNING for a while after the action ended.
        for delay in _REATTACH_SETTLE_DELAYS_S:
            details = await flyte.remote.ActionDetails.get.aio(run_name=run_name, name=name)
            if details.done():
                break
            await asyncio.sleep(delay)
        else:
            raise RuntimeError(
                f"worker action {name} ended on the watch stream but still shows {details.phase.value} "
                f"after {sum(_REATTACH_SETTLE_DELAYS_S):.0f}s; collect its outputs from the console"
            )
        if details.phase != ActionPhase.SUCCEEDED:
            error = details.error_message or "no error recorded"
            raise RuntimeError(f"worker action {name} ended {details.phase.value}: {error}")
        summary, output = await details.outputs()
        await output.download(str(target / "result"))
        return _usage._ingest_subagent_usage(summary, "delegate", _usage._metering_round, timeline)

    return reattach
