"""Deploying the factory's own code: shared by `ops/deploy_*.py` and the `factory-deployer` task.

Three stages deploy, each by a child process that imports the code being deployed (not this
process's copy) with the deployment's recipe already in its environment, because
TaskEnvironments read it at import:

    work slots     every profile `[[slots]]` env and its sub-agent env      (`deploy_slots`)
    the factory    factory, factory-mcp and factory-deployer, plus          (`deploy_factory`)
                   factory-webhook with `[github] webhook_secret`,
                   factory-issues with that and `maintainers`,
                   factory-reconcile with `[board]` or the GitHub App, and
                   factory-bench and factory-bench-check with `[bench]`,
                   factory-loop with `[loop] enabled` (`factory_envs`)
    apps           the personal browser, with `[browser] personal`          (`deploy_apps`)
                   (`app_envs`): replaced stop-then-start, see `_deploy_apps`

`ops/deploy_work.py` and `ops/deploy_factory.py` call the first two directly (the first install); the
apps stage has no ops path and runs only in the deployer. The `factory-deployer` runs the previous
release, so it calls none of them itself: it runs `_deploy_child` from the fetched tree
(`check_in_tree`, `deploy_in_tree`), and that tree's copy of this module does the deploy.

Refusals are `Refused`, a `SystemExit`, so a CLI exits with the message and the deployer can still
catch it by name.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from . import _profile, _standards
    from ._errors import REMOTE_ERRORS
else:
    from flyte_agent_factory import _profile, _standards
    from flyte_agent_factory._errors import REMOTE_ERRORS

if TYPE_CHECKING:
    from flyte import Environment

#: The deployer's task, which no listing of projects may ever include (`_factory.live_runs`).
DEPLOYER_TASK = "factory-deployer.deploy_if_changed"
DEPLOY_PAIR = "factory-deployer.deploy_pair"
#: The reconciler's task, live hourly and not a project either.
RECONCILE_TASK = "factory-reconcile.reconcile"
#: `(trigger, task)` of the schedules `factory-reconcile` replaced (state step 1). A deploy never
#: removes an environment, so the old task version would keep running its trigger; each deploy deletes
#: them (`delete_retired_triggers`).
RETIRED_TRIGGERS = (
    ("issue_outbox", "factory-issues.drain_outbox"),
    ("factory-board-sync", "factory-board-sync.sync_board"),
)
#: Phases in which a run is live, as the server names them. `Run.listall(in_phase=)` maps each to
#: `ACTION_PHASE_<name>` and raises ValueError for anything else (there is no "WAITING").
LIVE_PHASES = ("RUNNING", "QUEUED", "WAITING_FOR_RESOURCES", "INITIALIZING", "PAUSED")

#: What `deploy_factory` may always deploy; `factory_envs` adds what the profile enables.
#: `gardening-scan` is the factory's own dependency (the weekly gardening task's scan child).
FACTORY_ENVS = frozenset({"factory", "factory-mcp", "factory-deployer", "gardening-scan"})
#: The apps a profile may enable, deployed by their own stage (`deploy_apps`) rather than the factory's:
#: a personal browser is not a factory environment and must be replaced, not rolled over.
PERSONAL_BROWSER_APP = "claude-browser-personal"
#: The shared project browser (`browser.APP_NAME`). Its name is a prefix of every personal browser's, but it
#: is deployed by hand and no profile retires it.
SHARED_BROWSER_APP = "claude-browser"
#: Apps the apps stage always stops when the profile does not enable them. Identities with other names are
#: found by prefix among the deployed apps (`_deploy_apps.deployed_personal_apps`), so they are stopped too.
APPS = frozenset({PERSONAL_BROWSER_APP})
#: The deployer's own secret: no child process, which imports and runs deployed code, may see it.
GITHUB_KEY_ENV = "GITHUB_APP_PRIVATE_KEY"
#: The only factory-mcp settings carried from the caller into a factory deploy.
_FORWARDED = frozenset(
    {
        "FACTORY_MCP_PUBLIC_URL",
        "FACTORY_MCP_ALLOW_LOCALHOST",
        "FLYTE_MCP_ALLOWED_HOSTS",
        "FLYTE_MCP_ALLOWED_ORIGINS",
    }
)
_URL_CREDENTIALS = re.compile(r"(https?://[^:/@\s]+:)[^@\s]+@")


class Refused(SystemExit):
    """The deploy would touch something it must not."""


class DeployError(RuntimeError):
    """A deploy step failed. The text never carries a URL credential."""


class MissingSecrets(DeployError):
    """The engine tree's check found Flyte secrets that the planned environments mount but do not exist.

    `secrets` maps each missing secret's name to the environments that need it. Nothing was deployed, and
    nothing is wrong with the release: the owner has only to create them.
    """

    def __init__(self, message: str, secrets: Mapping[str, list[str]]):
        super().__init__(message)
        self.secrets = dict(secrets)


#: Exit code of `_deploy_child check` when secrets are missing (1 is every other refusal).
MISSING_SECRETS_EXIT = 3


#: A file each deploy child appends the names of the environments it deployed to, one per line, when set.
#: The auto-deployer reports those in its run outputs, rather than what it would itself have predicted.
DEPLOY_REPORT_ENV = "AGENT_FACTORY_DEPLOY_REPORT"
STANDARDS_REPORT_ENV = "AGENT_FACTORY_STANDARDS_REPORT"

# The start of each child below. Each child refuses to deploy a copy of the package from anywhere but
# `root`, with an explicit check: an assert would vanish under PYTHONOPTIMIZE.
_CHILD_START = f"""
import os, sys, flyte
from pathlib import Path
root = Path(sys.argv[1])
dry = sys.argv[2] == "1"
report = os.environ.get("{DEPLOY_REPORT_ENV}")


def from_root(module):
    if not Path(module.__file__).resolve().is_relative_to(root.resolve()):
        raise SystemExit(f"imported {{module.__file__}}, not the tree at {{root}}")


def record(deployments):
    if report and not dry:
        with open(report, "a") as out:
            for deployment in deployments:
                out.writelines(f"{{deployed.get_name()}}\\n" for deployed in deployment.envs.values())
"""

# Runs in a subprocess so the recipe's env vars are set before `agent` is imported
# (TaskEnvironments read them at import) and never leak into this process.
SLOTS_CHILD = (
    _CHILD_START
    + """
flyte.init_from_api_key(api_key=os.environ["_UNION_EAGER_API_KEY"], project=sys.argv[3],
                        domain=sys.argv[4], root_dir=root, image_builder="remote")
import subprocess
# The code bundle omits .git. Capture provenance from the pinned checkout before deployment.
revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=False)
os.environ["AGENT_FACTORY_ENGINE_SHA"] = revision.stdout.strip() if revision.returncode == 0 else ""
from flyte_agent_factory import agent
from_root(agent)
envs = (agent.env, agent.subagent_env, agent.setup_env)
if {e.name for e in envs} != set(sys.argv[5].split(",")):
    raise SystemExit(f"the slot recipe gave {[e.name for e in envs]}, not {sys.argv[5]}")
deployments = flyte.deploy(*envs, dry_run=dry)
for d in deployments:
    print(d.summary_repr() if hasattr(d, "summary_repr") else d)
record(deployments)
"""
)

FACTORY_CHILD = (
    _CHILD_START
    + """
flyte.init_from_api_key(api_key=os.environ["_UNION_EAGER_API_KEY"], project=sys.argv[3],
                        domain=sys.argv[4], root_dir=root, image_builder="remote")
from flyte_agent_factory._deploy import check_environments
from flyte_agent_factory import _deployer, _factory, _factory_mcp
for module in (_factory, _factory_mcp, _deployer):
    from_root(module)
envs = (_factory.env, _factory_mcp.app, _deployer.env)
if _deployer.PROFILE.board or _deployer.PROFILE.github.configured:
    from flyte_agent_factory import _reconcile
    from_root(_reconcile)
    envs += (_reconcile.env,)
if _deployer.PROFILE.github.webhook_secret:
    from flyte_agent_factory import _webhook
    from_root(_webhook)
    envs += (_webhook.env,)
if _deployer.PROFILE.bench:
    from flyte_agent_factory import _bench_flow
    from_root(_bench_flow)
    envs += (_bench_flow.env,)
if _deployer.PROFILE.loop.enabled:
    from flyte_agent_factory import _loop_flow
    from_root(_loop_flow)
    envs += (_loop_flow.env,)
check_environments(*envs)
# flyte.deploy handles AppEnvironments too, including a real dry-run path.
# In SDK 2.10.7 serve(dry_run=True) still calls _deploy_app without dryrun.
from flyte._deploy import plan_deploy
for plan in plan_deploy(*envs):
    check_environments(*plan.envs.values())
deployments = flyte.deploy(*envs, dry_run=dry)
for deployment in deployments:
    print(deployment.summary_repr())
    for deployed in deployment.envs.values():
        if deployed.get_name() == "factory-mcp" and not dry:
            app = deployed.deployed_app.watch(wait_for="activated")
            print(f"factory-mcp public URL: {app.endpoint}")
            print(f"Connector URL: {app.endpoint.rstrip('/')}/flyte-mcp/mcp")
            print("Connect with header Authorization: Bearer <Union API key or access token>.")
        # Not watched like factory-mcp: an app whose secret does not exist yet never activates, and the
        # deploy must not hang or fail on it. The create response may not carry the URL yet.
        if deployed.get_name() == "factory-webhook" and not dry:
            endpoint = deployed.deployed_app.endpoint
            if endpoint:
                print(f"factory-webhook public URL: {endpoint}")
                print(f"GitHub webhook URL: {endpoint.rstrip('/')}/github")
            else:
                print("factory-webhook has no public URL yet; see the console's Apps page.")
record(deployments)
if not dry:
    from flyte_agent_factory._deploy import delete_retired_triggers
    delete_retired_triggers(_deployer.PROFILE)
"""
)

# The deployer image carries `kubernetes` from the release that adds this stage; an older deployer's
# does not, and fails here with a message instead of a traceback. It runs after slots and factory, so the
# new deployer is already deployed when it does, and it retries.
APPS_CHILD = (
    _CHILD_START
    + """
from flyte_agent_factory import _profile
from flyte_agent_factory._deploy import APPS, app_envs, check_environments
profile = _profile.require()
enabled = app_envs(profile)
if enabled:
    try:
        import kubernetes  # noqa: F401  (Flyte imports it to serialize the pod template)
    except ImportError:
        raise SystemExit(
            "this deployer's image lacks the kubernetes package, which serializing the personal browser "
            "needs; the deployer deployed by this release carries it and will retry"
        )
flyte.init_from_api_key(api_key=os.environ["_UNION_EAGER_API_KEY"], project=sys.argv[3],
                        domain=sys.argv[4], root_dir=root, image_builder="remote")
from flyte_agent_factory import _deploy_apps
from_root(_deploy_apps)
# A known app the profile does not enable is stopped, not deleted: its archive stays for a re-enable. Before
# the deploys, so a failed deploy does not leave a dropped app running.
_deploy_apps.retire_apps(APPS - enabled, dry_run=dry)
_deploy_apps.retire_apps(_deploy_apps.deployed_personal_apps() - APPS - enabled, dry_run=dry)
envs = ()
if profile.browser.personal:
    from flyte_agent_factory import browser
    from_root(browser)
    missing = [i.name for i in profile.browser.identities if i.name not in browser.identity_apps]
    if missing:
        raise SystemExit(f"the profile has browser identities {missing}, but browser.py does not define them")
    envs += tuple(browser.identity_apps[i.name] for i in profile.browser.identities)
check_environments(*envs, allowed=enabled)
for env in envs:
    # Recorded only once the app is active (or unchanged): a deploy that did not start it is not a release.
    record(_deploy_apps.deploy_app(env, dry_run=dry))
"""
)


def redact(text: str) -> str:
    """`text` with the password of any URL in it hidden (git echoes the URL it failed on)."""
    return _URL_CREDENTIALS.sub(r"\1***@", text)


def slot_table(profile: _profile.Profile) -> dict[str, str]:
    """Slot env name -> its sub-agent env name."""
    return {s.env: s.subagent_env for s in profile.slots}


#: The environment of the setup tasks (`codex_bootstrap_login`, `gcp_bootstrap`), deployed with every slot so
#: they can be run from chat. It mounts no credential.
SETUP_ENV = "factory-setup"


def slot_envs(profile: _profile.Profile) -> set[str]:
    """Every environment name a slot deploy may touch."""
    return set(slot_table(profile)) | set(slot_table(profile).values()) | {SETUP_ENV}


def recipe_env(profile: _profile.Profile, slot: str, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the child's environment: `base` (default: ours), with the recipe for `slot` forced over it."""
    env = dict(os.environ if base is None else base)
    env.update(profile.deploy_env(profile.slot(slot)))
    return env


def check_recipe(profile: _profile.Profile, env: Mapping[str, str]) -> None:
    """Refuse anything that is not a work slot on the profile's one token."""
    slots = slot_table(profile)
    name = env.get("AGENT_FACTORY_ENV_NAME")
    if name not in slots:
        raise Refused(f"refusing: env name {name!r} is not one of {sorted(slots)}")
    sub = slots[name]
    secret = profile.secrets.claude_token
    if env.get("AGENT_FACTORY_CLAUDE_TOKEN_SECRET") != secret:
        raise Refused(f"refusing: {name} must run on {secret}, not another credential")
    if env.get("AGENT_FACTORY_SUBAGENT_ENV_NAME") != sub:
        raise Refused(f"refusing: {name}'s sub-agents must be {sub}, on the same token")


def check_slots(profile: _profile.Profile, slots: list[str]) -> None:
    """Refuse before anything is deployed: any env that is not a slot, then every slot's recipe."""
    for slot in slots:
        if slot not in slot_table(profile):
            raise Refused(f"refusing: env name {slot!r} is not one of {sorted(slot_table(profile))}")
        check_recipe(profile, recipe_env(profile, slot))


#: Minutes between safety-net ticks; deployer/queue polling without a webhook is faster.
RECONCILE_MINUTES = 60


def reconcile_minutes(profile: _profile.Profile) -> int:
    """How often the deployer runs on a schedule.

    With the webhook a push starts the deployer, so its schedule is only a backstop for missed
    deliveries and pending CI. Without it the deployer polls, every 5 minutes. The webhook only counts
    when `[deploy] webhook` says pushes to the deploy repo reach it: a repository without the App never
    delivers, and an hourly poll would leave its merges an hour late. The queue sweep does not follow
    this: see `QUEUE_SWEEP_MINUTES`.
    """
    return RECONCILE_MINUTES if profile.github.webhook_secret and profile.deploy.webhook else 5


#: Minutes between queue sweeps, webhook or not. The queue is dispatched on every launch and every
#: normal project ending, but an aborted, crashed or failed run hands nothing over, so the sweep is the
#: only thing that starts the launch waiting for its slot. It rode `reconcile_minutes` (60 with the
#: webhook) and a freed slot sat unused for 25+ minutes. An idle sweep costs one queue listing.
QUEUE_SWEEP_MINUTES = 5


def factory_envs(profile: _profile.Profile) -> frozenset[str]:
    """Return the factory environments this profile enables.

    Webhook intake, issue intake, reconcile, the benchmark and the loop are each optional.

    `factory-issues` is the webhook app's dependency when issue intake or the loop is on.
    `factory-reconcile` heals what the board and the issue milestones show, so it is deployed where
    either exists: with `[board]`, or with the GitHub App the milestones are posted as.
    `factory-bench-check` is `factory-bench`'s: the pod without credentials its hidden checks run in.
    """
    webhook = {"factory-webhook"} if profile.github.webhook_secret else set()
    issues = (
        {"factory-issues"}
        if profile.github.intake or profile.loop.enabled or (profile.board and webhook)
        else set()
    )
    reconcile = {"factory-reconcile"} if profile.board or profile.github.configured else set()
    bench = {"factory-bench", "factory-bench-check"} if profile.bench else set()
    loop = {"factory-loop"} if profile.loop.enabled else set()
    return FACTORY_ENVS | webhook | issues | reconcile | bench | loop


def delete_retired_triggers(profile: _profile.Profile) -> None:
    """Delete both retired schedules regardless of current settings; one already gone is fine.

    Needs an initialised Flyte client (the factory deploy child has one). Authentication failures
    raise; other failures warn so cleanup cannot invalidate a recorded deployment.
    """
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError
    from flyte import remote

    for name, task in RETIRED_TRIGGERS:
        try:
            remote.Trigger.delete(name=name, task_name=task)
        except ConnectError as exc:
            if exc.code in (Code.UNAUTHENTICATED, Code.PERMISSION_DENIED):
                raise
            if exc.code != Code.NOT_FOUND:
                logging.getLogger(__name__).warning(
                    "Could not delete retired trigger %s (%s)", name, exc.code
                )
                continue
        except REMOTE_ERRORS as exc:
            logging.getLogger(__name__).warning(
                "Could not delete retired trigger %s (%s)", name, type(exc).__name__
            )
            continue
        print(f"retired trigger {name} ({task}) is deleted", flush=True)


def app_envs(profile: _profile.Profile) -> frozenset[str]:
    """Return the apps this profile enables, which `deploy_apps` deploys: one per browser identity."""
    return frozenset(identity.app for identity in profile.browser.identities)


def planned_envs(profile: _profile.Profile) -> list[str]:
    """Every environment a deploy of `profile` deploys: its slots and sub-agents, the factory's, the apps."""
    return sorted(slot_envs(profile)) + sorted(factory_envs(profile)) + sorted(app_envs(profile))


def check_environments(*envs: Environment, allowed: frozenset[str] | None = None) -> None:
    """Refuse a deploy outside the environments enabled by the selected profile (`allowed`: the factory's)."""
    allowed = factory_envs(_profile.require()) if allowed is None else allowed
    seen = set()
    pending = list(envs)
    while pending:
        env = pending.pop()
        if env.name not in allowed:
            raise Refused(f"refusing to deploy {env.name!r}; only {sorted(allowed)} are allowed")
        if id(env) not in seen:
            seen.add(id(env))
            pending.extend(env.depends_on)


def factory_env(base: Mapping[str, str], project: str, domain: str) -> dict[str, str]:
    """Return `base` for a factory deploy: only the factory-mcp settings in `_FORWARDED` are carried over."""
    env = {
        k: v
        for k, v in base.items()
        if not k.startswith(("FACTORY_MCP_", "FLYTE_MCP_ALLOWED_")) or k in _FORWARDED
    }
    env.update({"FLYTE_MCP_PROJECT": project, "FLYTE_MCP_DOMAIN": domain})
    return env


#: Lines of a deploy child's stderr kept for the error raised when it fails.
STDERR_TAIL_LINES = 20


def _run_child(
    profile: _profile.Profile,
    script: str,
    src: Path,
    env: dict[str, str],
    *args: str,
    dry_run: bool,
    stderr_tail: list[str] | None = None,
) -> int:
    """Run a deploy child script; its exit code. Its stdout goes to ours.

    Its stderr is passed through to ours as it is written, and its last `STDERR_TAIL_LINES` lines are
    added to `stderr_tail`: the exit code alone says "failed", the tail says why.
    """
    # The tree has no `profiles/` of its own (real profiles are untracked), so the child gets the text.
    env = _profile.child_env(profile, env)
    env["PYTHONPATH"] = str(src)
    tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    read_fd, write_fd = os.pipe()

    def pump() -> None:
        with os.fdopen(read_fd, errors="replace") as lines:
            for line in lines:
                sys.stderr.write(line)
                sys.stderr.flush()
                tail.append(line.rstrip("\n"))

    pump_thread = threading.Thread(target=pump, name="deploy-child-stderr", daemon=True)
    pump_thread.start()
    try:
        code = subprocess.run(
            [sys.executable, "-c", script, str(src), "1" if dry_run else "0", *args],
            check=False,
            env=env,
            cwd=src,
            stderr=write_fd,
        ).returncode
    finally:
        # The pump reads to end of file once every holder of the pipe has closed it: a grandchild that
        # inherited stderr extends the wait, which DEPLOY_TIMEOUT bounds.
        os.close(write_fd)
        pump_thread.join()
    if stderr_tail is not None:
        stderr_tail.extend(tail)
    return code


def make_child_failure(what: str, stderr_tail: list[str]) -> DeployError:
    """Return the error for a failed deploy child: `what`, then what the child said last."""
    lines = [line for line in stderr_tail if line.strip()][-STDERR_TAIL_LINES:]
    return DeployError(redact(f"{what}: {' | '.join(lines)}" if lines else what))


#: Seconds for the engine tree to read and check a profile: a parse, so anything longer is a hang.
CHECK_TIMEOUT = 2 * 60
#: Seconds for the engine tree's whole deploy. Image builds can take most of the deployer's hour
#: (`_deployer.MAX_RUNTIME`); this stops short of it so a hung deploy is killed and recorded as a
#: failure, rather than the task being killed with nothing recorded.
DEPLOY_TIMEOUT = 50 * 60


def _run_in_tree(
    src: Path, args: list[str], *, text: str, base_env: Mapping[str, str], timeout: int, capture: bool
) -> tuple[int, str, str]:
    """Run `python -m flyte_agent_factory._deploy_child <args>` from `src`: exit code, stdout, stderr.

    Unless `capture`, its output goes to this process's (the run's log), and both strings are empty.

    The child is the tree's code, so the profile selection of this process (and the App key) is not
    passed on: the profile is `text`, on stdin. It runs in its own session, so a timeout kills the
    deploy children under it too, not just the one this process started.
    """
    env = {
        k: v
        for k, v in base_env.items()
        if k not in (_profile.PROFILE_ENV, _profile.TOML_ENV, GITHUB_KEY_ENV)
    }
    env["PYTHONPATH"] = str(src)  # only the tree: never this process's copy
    with subprocess.Popen(
        [sys.executable, "-m", "flyte_agent_factory._deploy_child", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
        env=env,
        cwd=src,
        start_new_session=True,
    ) as proc:
        try:
            out, err = proc.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise DeployError(
                f"{args[0]} in the engine tree did not finish in {timeout} s; killed it"
            ) from None
    return proc.returncode, out or "", err or ""


def _tree_args(step: str, src: Path, where: str, tree: Path | None, error: Path) -> list[str]:
    args = [step, "--src", str(src), "--where", where, "--error", str(error)]
    return args + (["--tree", str(tree)] if tree else [])


#: Lines of a child's stderr reported when it wrote no message of its own (a crash, a missing module).
_STDERR_LINES = 10


def _child_error(error: Path, stderr: str, code: int) -> str:
    """Why a tree child failed: the message it wrote, else the end of its stderr, else its exit code."""
    if error.is_file() and error.read_text().strip():
        return error.read_text().strip()
    tail = stderr.strip().splitlines()[-_STDERR_LINES:]  # a traceback ends with its exception
    return " | ".join(tail) if tail else f"exit code {code}; its output is in this run's log"


def _env_names(text: str) -> list[str]:
    """`text` as the JSON list of environment names the check prints; DeployError if it is not one."""
    try:
        names = json.loads(text)
    except ValueError:
        names = None
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise DeployError(
            redact(f"the engine tree's check printed {text[:200]!r}, not a list of environment names")
        )
    return names


def _parse_missing_secrets(output: str) -> dict[str, list[str]]:
    """Return the `{"missing_secrets": {...}}` line of the check's stdout; empty if it printed none."""
    for line in output.splitlines():
        if line.startswith('{"missing_secrets"'):
            try:
                found = json.loads(line)["missing_secrets"]
            except (ValueError, KeyError):
                break
            if isinstance(found, dict):
                return {str(name): [str(env) for env in envs] for name, envs in found.items()}
    return {}


def check_in_tree(
    src: Path,
    text: str,
    where: str,
    *,
    tree: Path | None,
    base_env: Mapping[str, str],
    diagnostics: dict[str, bool | str | None] | None = None,
) -> list[str]:
    """Return the environments `src` would deploy for the profile `text`; DeployError if it refuses it.

    The deployer runs the previous release, so its own `parse` cannot know a key added in the release
    it deploys; the release's own code reads the profile, strictly. `tree` is the engine checkout the
    profile's standards come from, or None for text that already carries them (the pod's own).
    `diagnostics`, when supplied, receives the captured standards state; old engines report it as unknown.
    """
    with tempfile.TemporaryDirectory(prefix="deploy-check-") as tmp:
        error = Path(tmp) / "error"
        standards_report = Path(tmp) / "standards.json"
        code, output, stderr = _run_in_tree(
            src,
            _tree_args("check", src, where, tree, error),
            text=text,
            # An environment sidecar leaves older fetched engines' CLI and stdout protocol intact.
            base_env={**base_env, STANDARDS_REPORT_ENV: str(standards_report)},
            timeout=CHECK_TIMEOUT,
            capture=True,
        )
        if code == MISSING_SECRETS_EXIT:
            raise MissingSecrets(redact(_child_error(error, stderr, code)), _parse_missing_secrets(output))
        if code:
            reason = _child_error(error, stderr, code)
            # A ProfileError already starts with `where`.
            reason = reason if reason.startswith(f"{where}: ") else f"{where}: {reason}"
            raise DeployError(redact(f"the engine being deployed refuses {reason}"))
        if diagnostics is not None:
            try:
                metadata = json.loads(standards_report.read_text())
            except (OSError, ValueError):
                metadata = None
            configured = metadata.get("configured") if isinstance(metadata, dict) else None
            configured = configured if isinstance(configured, bool) else None
            diagnostics.clear()
            diagnostics.update(configured=configured)
            if configured is None:
                diagnostics["warning"] = "standards metadata unavailable (older or incompatible engine)"
            elif not configured:
                diagnostics["warning"] = _standards.NO_STANDARDS
    lines = output.strip().splitlines()
    return _env_names(lines[-1] if lines else "")


def deploy_in_tree(
    src: Path,
    text: str,
    where: str,
    *,
    tree: Path | None,
    project: str,
    domain: str,
    ref: str,
    base_env: Mapping[str, str],
) -> list[str]:
    """Deploy the profile `text` with the code in `src`: slots, factory, apps; what it deployed.

    Which environments exist, each slot's recipe and the child scripts are all the tree's own (see
    `_deploy_child`). Its output goes to this process's log; the names come from `DEPLOY_REPORT_ENV`.
    """
    with tempfile.TemporaryDirectory(prefix="deploy-run-") as tmp:
        error, report = Path(tmp) / "error", Path(tmp) / "deployed"
        args = [
            *_tree_args("deploy", src, where, tree, error),
            "--project",
            project,
            "--domain",
            domain,
            "--ref",
            ref,
        ]
        env = {**base_env, DEPLOY_REPORT_ENV: str(report)}
        code, _, _ = _run_in_tree(src, args, text=text, base_env=env, timeout=DEPLOY_TIMEOUT, capture=False)
        if code:
            raise DeployError(redact(f"deploy of {ref} failed: {_child_error(error, '', code)}"))
        names = sorted(set(report.read_text().split())) if report.is_file() else []
    if not names:  # it exited 0, so a child that does not report is not the tree's own (or is broken)
        raise DeployError(f"deploy of {ref} reported no environment deployed")
    return names


def deploy_slots(
    profile: _profile.Profile,
    src: Path,
    slots: list[str],
    *,
    project: str,
    domain: str,
    ref: str,
    dry_run: bool = False,
    base_env: Mapping[str, str] | None = None,
    stderr_tail: list[str] | None = None,
) -> int:
    """Deploy each slot and its sub-agent env from `src`; the first non-zero exit code, else 0.

    Every slot is attempted: one failing must not leave the others stale. `stderr_tail` collects the
    children's last stderr lines (`_run_child`).
    """
    check_slots(profile, slots)
    rc = 0
    for slot in slots:
        env = recipe_env(profile, slot, base_env)
        check_recipe(profile, env)
        names = ",".join(sorted({slot, slot_table(profile)[slot], SETUP_ENV}))
        print(f"deploying {slot} from {ref}{' [dry run]' if dry_run else ''}", flush=True)
        code = _run_child(
            profile, SLOTS_CHILD, src, env, project, domain, names, dry_run=dry_run, stderr_tail=stderr_tail
        )
        rc = rc or code
    return rc


def deploy_factory(
    profile: _profile.Profile,
    src: Path,
    *,
    project: str,
    domain: str,
    env: dict[str, str],
    dry_run: bool = False,
    stderr_tail: list[str] | None = None,
) -> int:
    """Deploy the profile's `factory_envs` from `src`; the child's exit code."""
    try:
        _profile.planner_ready(profile)
    except _profile.ProfileError as exc:
        raise Refused(str(exc)) from exc
    return _run_child(
        profile, FACTORY_CHILD, src, env, project, domain, dry_run=dry_run, stderr_tail=stderr_tail
    )


def deploy_apps(
    profile: _profile.Profile,
    src: Path,
    *,
    project: str,
    domain: str,
    env: dict[str, str],
    dry_run: bool = False,
    stderr_tail: list[str] | None = None,
) -> int:
    """Deploy the apps the profile enables and stop the known ones it does not; the child's exit code.

    Each enabled app is replaced stop-then-start (`_deploy_apps`). The child always runs: an app deployed
    for an earlier profile is stopped there.
    """
    return _run_child(
        profile, APPS_CHILD, src, env, project, domain, dry_run=dry_run, stderr_tail=stderr_tail
    )


@contextlib.contextmanager
def worktree(repo: Path, sha: str, prefix: str) -> Iterator[Path]:
    """Return a detached worktree of `repo` at `sha`, removed afterwards; local edits never ship."""
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree), sha], check=True
        )
        try:
            yield tree
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)], check=False)


def public_head(repo: str) -> str:
    """Return the head SHA of `repo`'s default branch, read anonymously by git; "" when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}.git", "HEAD"],
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    sha = done.stdout.split("\t")[0]
    return sha if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""


@contextlib.contextmanager
def fetched_tree(url: str, sha: str) -> Iterator[Path]:
    """Return a shallow checkout of exactly `sha` from `url`, in a temp dir removed afterwards.

    `url` may carry a credential (an installation token); it appears on git's command line and
    nowhere in the error raised, which is redacted.
    """
    with tempfile.TemporaryDirectory(prefix="deploy-fetch-") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        for argv in (
            ["git", "init", "-q"],
            ["git", "fetch", "-q", "--depth", "1", url, sha],
            ["git", "checkout", "-q", "FETCH_HEAD"],
        ):
            done = subprocess.run(argv, cwd=tree, env=env, capture_output=True, text=True, check=False)
            if done.returncode:
                raise DeployError(redact(f"git {argv[1]} of {sha} failed: {done.stderr.strip()}"))
        yield tree
