import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests import _cpu_budget, _tini
from tests._git import git

# Pytest loads this file as `conftest`; tests that `from tests.conftest import ...` would otherwise run it a
# second time, as `tests.conftest`, and its environment scrub below would then delete the variables the
# autouse fixtures set (a Union tier key) halfway through whichever test imported it first.
sys.modules.setdefault("tests.conftest", sys.modules[__name__])


def pytest_xdist_auto_num_workers(config: pytest.Config | None) -> int | None:
    """Return the `-n auto` worker count: the CPUs the cgroup quota grants, not those the node has.

    A set `PYTEST_XDIST_AUTO_NUM_WORKERS` wins, as in xdist's own default.
    """
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    return _cpu_budget.cpus(visible=_cpu_budget.visible())


# Fixture projects (the benchmark's reference service) carry tests of their own, run from a copy.
collect_ignore = ["fixtures"]

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# Subprocesses (`sys.executable -c ...`) get it too: pytest's `pythonpath` setting only changes this
# process, and a slot pod's PYTHONPATH points at the deployed copy of the package.
os.environ["PYTHONPATH"] = os.pathsep.join([str(SRC), *filter(None, [os.environ.get("PYTHONPATH")])])

# The reaping tests and the test-lane script run a real tini, which a pod from an older image lacks (#34).
_tini.ensure_on_path()


# The deployment profile the whole suite runs against, selected before any module that reads
# it at import. Neutral placeholder values (the owner's real profile is private); the profile
# tests select others explicitly.
TEST_PROFILE = Path(__file__).resolve().parent / "profiles" / "work.toml"
os.environ["AGENT_FACTORY_PROFILE"] = str(TEST_PROFILE)
os.environ.pop("AGENT_FACTORY_PROFILE_TOML", None)
# The rest of what a deployed slot sets and the code reads at import or in a prompt (by name only; values
# are never read here). Without this the suite passes or fails depending on where it runs. FLYTE_INTERNAL_*
# stays: the tests that read it skip outside a pod and should still run inside one.
_SLOT_VARS = (
    "PROJECT_ANTHROPIC_API_KEY",  # session guidance grows a section when set
    "GIT_DEPLOY_KEY",
    "AGENT_FACTORY_SUBAGENT_ENV_NAME",
    "AGENT_FACTORY_BASE",
    "AGENT_FACTORY_WORKSPACE",
    "AGENT_FACTORY_BRIDGE_DIR",
    "AGENT_FACTORY_PROFILES_DIR",
    "AGENT_FACTORY_BROWSER_ISOLATED",
    "AGENT_FACTORY_BROWSER_EXIT_NODE",
    "BROWSER_PERSONAL_TOKEN",
    "FACTORY_MCP_PUBLIC_URL",
    "FLYTE_MCP_DOMAIN",
    "OPERATE_REPO",
    "OPERATE_CAP",
    # An operator's shell, not a slot, but the same effect on the tests that read them.
    "OPERATE_TASK",
    "FACTORY_MCP_ALLOW_LOCALHOST",
    "BROWSER_SECRETS_DIR",
)
for _var in [
    v
    for v in os.environ
    if v in _SLOT_VARS
    or v.startswith("GIT_DEPLOY_KEY")
    # Policy keys (AGENT_FACTORY_<ROLE>_MODEL/_ENGINE/...) except the profile selectors set above.
    or (
        v.startswith("AGENT_FACTORY_")
        # The profile selectors are set above; LIVE_TESTS is the documented opt-in for billed tests.
        and v not in ("AGENT_FACTORY_PROFILE", "AGENT_FACTORY_PROFILE_TOML", "AGENT_FACTORY_LIVE_TESTS")
    )
]:
    del os.environ[_var]
# The agent's state root defaults to $HOME/state, which in a project pod is the live run's own volume: its
# meter history and refusals, checkpointed metrics. A test that does not patch STATE_ROOT would read that
# changing state (order- and machine-dependent results) and could write into it. Every test gets a
# session base of its own, set before anything imports the agent.
# Once per process: this module can be imported twice (as `conftest` and as `tests.conftest`, which an xdist
# worker does), and a second base would leave `AGENT_FACTORY_BASE` and `TEST_BASE` disagreeing. Keyed by pid
# so each xdist worker, a separate process, still gets its own.
# (Its own variable: the slot-variable sweep above clears `AGENT_FACTORY_BASE` on every import.)
if os.environ.get("FACTORY_TESTS_BASE_PID") == str(os.getpid()):
    TEST_BASE = Path(os.environ["FACTORY_TESTS_BASE"])
else:
    TEST_BASE = Path(tempfile.mkdtemp(prefix="factory-tests-base-"))
    os.environ["FACTORY_TESTS_BASE_PID"] = str(os.getpid())
    os.environ["FACTORY_TESTS_BASE"] = str(TEST_BASE)
os.environ["AGENT_FACTORY_BASE"] = str(TEST_BASE)
# The tf-bucket hidden check downloads the AWS provider on every `terraform init` (several per case) unless it
# has a plugin cache; one per process, since concurrent installs into one cache race.
os.environ.setdefault("TF_PLUGIN_CACHE_DIR", str(TEST_BASE / "terraform-plugins"))
Path(os.environ["TF_PLUGIN_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
# The bridge likewise: unset it defaults to $HOME/.claude-flyte/bridge, the live supervisor's, where a
# test's `Delegations` would fail every live delegation request on construction.
os.environ["AGENT_FACTORY_BRIDGE_DIR"] = str(TEST_BASE / "bridge")

# Only now, with the environment above in place: these modules read the profile and the slot variables at
# import. Absent (the standards gate's base tree predates the package), each test module reports its own
# ModuleNotFoundError at collection instead of this file failing first.
try:
    from flyte_agent_factory import (
        _checkpoint,
        _contract,
        _gates,
        _inbox,
        _pins,
        _review,
        _reviewer,
        _session_config,
        _session_setup,
        _supervise_support,
        _usage,
    )
except ModuleNotFoundError as missing:
    if missing.name != "flyte_agent_factory":
        raise

# Every variable that changes how Claude Code authenticates or bills. Tests set these
# deliberately; everything else must run without them, and without inheriting them from the
# developer's shell.
CLAUDE_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)


@pytest.fixture(autouse=True)
def _suite_profile_survives_every_test():
    """Ops tools select a profile with `os.environ[...] = ...` (`--profile`), which monkeypatch cannot undo.

    Restored here, after monkeypatch's own teardown, so one test's `--profile` never becomes the
    next test's profile.
    """
    yield
    os.environ["AGENT_FACTORY_PROFILE"] = str(TEST_PROFILE)
    os.environ.pop("AGENT_FACTORY_PROFILE_TOML", None)


@pytest.fixture(autouse=True)
def _no_sealed_repo_outlives_its_test():
    """A sealed setup arms its repositories (`_seal_git.arm`) until `_seal_session.end()`, which a test may
    never reach. Left armed, every later test's harness git would run with hooks off."""
    yield
    from flyte_agent_factory import _seal_git

    _seal_git.disarm_all()


@pytest.fixture(autouse=True)
def _not_inside_the_run_that_runs_the_tests(monkeypatch):
    """A factory pod is itself a Flyte run: its run id would send lookups (`_gate_tests._run_image`) to the
    real cluster. Tests run as CI does, outside a run; one that needs a run sets its own id."""
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)


@pytest.fixture(autouse=True)
def _tests_run_as_the_unprivileged_user_the_factory_expects(monkeypatch):
    """Sessions refuse to start as root (`_unprivileged`); a CI container running the suite as root must still
    exercise them. A test of the refusal sets `os.geteuid` itself, after this."""
    if os.geteuid() == 0:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)


@pytest.fixture(autouse=True)
def _the_pod_is_healthy_whatever_runs_the_tests(monkeypatch):
    """Rows and timeout messages name the process table only when it is unhealthy (`_proc_health`); the pod
    that runs the suite may have the flyte `a0` process as PID 1 and zombies of its own. A test of the
    unhealthy case sets its own snapshot, after this."""
    from flyte_agent_factory import _proc_health

    monkeypatch.setattr(_proc_health, "snapshot", lambda proc=None: _proc_health.ProcHealth(100, 0, "tini"))


FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_engine_input_pin_left_by_another_test(monkeypatch):
    """`project` pins its `engine` input in a module global (`_author._engine_input_pin`); a test that runs
    it would otherwise pin the next test on that worker (seen: a COORDINATOR_ENGINE=claude test routed to
    codex)."""
    from flyte_agent_factory import _pins

    monkeypatch.setattr(_pins, "engine_input", "")


@pytest.fixture(autouse=True)
def _fixture_project_runs_from_its_root(request, monkeypatch):
    """A fixture project's own tests run from that project's root, as they do from a copy.

    They start their service with `python -m <package>`, which resolves only from there; the factory's
    gates name changed test files from the repository root. The project is the directory holding `tests/`
    (no conftest of its own: the benchmark copies these projects file for file).
    """
    path = Path(request.path).resolve()
    if FIXTURES in path.parents:
        monkeypatch.chdir(next(p.parent for p in path.parents if p.name == "tests" and FIXTURES in p.parents))


@pytest.fixture
def src_path() -> str:
    return str(SRC)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in CLAUDE_AUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    # A project pod's own Union key: `_auth.own_client_id` would hand the live client id to the tests.
    for var in ("_UNION_EAGER_API_KEY", "EAGER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # The pod's own run: with it, the checks step looks up the run's image on the live cluster.
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)


@pytest.fixture(autouse=True)
def _roles_come_from_the_package(monkeypatch, tmp_path_factory):
    """An image roles directory on the machine (`/opt/claude-roles`) must not decide the roles a test sees."""
    monkeypatch.setattr(
        _session_setup, "AGENT_ROLES_DIR", str(tmp_path_factory.getbasetemp() / "no-image-roles")
    )


@pytest.fixture(autouse=True)
def _reset_workspace_env(monkeypatch):
    """Undo AGENT_FACTORY_WORKSPACE before every test.

    `_session_setup._export_runtime_env` writes it straight into os.environ, so without this a test reads
    the workspace an earlier test on the same worker exported.
    """
    monkeypatch.delenv("AGENT_FACTORY_WORKSPACE", raising=False)
    # `child.write_context` exports the run context the same way.
    monkeypatch.delenv("AGENT_FACTORY_RUN_CONTEXT", raising=False)


@pytest.fixture(autouse=True)
def _reset_author_pin(monkeypatch):
    """Undo the run-wide pin state `agent.project` sets before every test.

    `project` records its `engine` input in `_pins.engine_input` and its `hard_pin` in `_usage._hard_pins`,
    and exports AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE, so without this a test that runs it pinned to codex
    pins the next test on the same worker to codex too.
    """
    monkeypatch.setattr(_pins, "engine_input", "")
    monkeypatch.setattr(_usage, "_hard_pins", False)
    monkeypatch.delenv("AGENT_FACTORY_CHOSEN_AUTHOR_ENGINE", raising=False)


@pytest.fixture
def bridge_root(tmp_path, monkeypatch) -> Path:
    """An isolated bridge directory, exported so subprocesses agree on it."""
    root = tmp_path / "bridge"
    monkeypatch.setenv("AGENT_FACTORY_BRIDGE_DIR", str(root))

    from flyte_agent_factory import _bridge

    # The module read the env var at import time, so point it at this root too.
    monkeypatch.setattr(_bridge, "BRIDGE_DIR", root)
    return _bridge.ensure_dirs(root)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "claude-config"


@pytest.fixture(autouse=True)
def _never_write_real_secrets(monkeypatch):
    """No test may store a real Flyte secret.

    Every project pod has a client initialised, so an unstubbed test would overwrite a production
    secret (found in review of the wordstats-03 fixes: one test call reached `Secret.create` with
    the real credential). Tests that exercise storing patch `Secret.create` themselves, after this.
    """
    from types import SimpleNamespace

    import flyte.remote

    async def refuse(**kw):
        raise RuntimeError(f"test tried to write real secret {kw.get('name')!r}")

    monkeypatch.setattr(flyte.remote.Secret, "create", SimpleNamespace(aio=refuse))


@pytest.fixture(autouse=True)
def _no_host_attempt_number(monkeypatch):
    # A test run inside a retried Flyte task inherits FLYTE_ATTEMPT_NUMBER and would label attempts a1.
    monkeypatch.delenv("FLYTE_ATTEMPT_NUMBER", raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_routes(monkeypatch):
    # `_session_config._routes` is module state that route decisions append to; a test that routes without
    # patching it left 30 KB behind, which a later snapshot size test then failed on.

    monkeypatch.setattr(_session_config, "_routes", [])


@pytest.fixture(autouse=True)
def _no_leaked_token_account(monkeypatch):
    # `_session_config._token_meter_account` remembers the token's account after the variable is dropped.

    monkeypatch.setattr(_session_config, "_token_meter_account", "")


@pytest.fixture(autouse=True)
def _no_cached_token_fingerprint(monkeypatch):
    # `_factory_meters.mounted_token_fingerprint` reads its mount once per process.
    from flyte_agent_factory import _factory_meters

    monkeypatch.setattr(_factory_meters, "_token_fingerprint", None)
    monkeypatch.delenv(_factory_meters.TOKEN_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_project_layout(monkeypatch):
    # project.func assigns these globals; a later pump test must not inherit its checks path.

    monkeypatch.setattr(_session_config, "_project_name", "")
    monkeypatch.setattr(_session_config, "_project_repo", "")
    # And its meter store: a limit one test notes there must not defer a later test's routed call.
    monkeypatch.setattr(_usage, "_meters", None)


@pytest.fixture(autouse=True)
def _no_leaked_child_listing_error(monkeypatch):
    """`_in_flight_error` is module state; one test's failure must not become another's warning row."""

    monkeypatch.setattr(_supervise_support, "_in_flight_error", "")


@pytest.fixture(autouse=True)
def _fresh_prior_findings(monkeypatch):
    """Reviewer findings carry between rounds in module state; never between tests."""

    monkeypatch.setattr(_review, "_prior_findings", {})
    monkeypatch.setattr(_reviewer, "_review_ledger", [])
    monkeypatch.setattr(_session_config, "_rulings", [])
    monkeypatch.setattr(_review, "_ruled", {})


@pytest.fixture(autouse=True)
def _no_network_push_fact(request, monkeypatch):
    """Review tests run against this repo; `_push_fact` would `git ls-remote` over the network.

    Tests of `_push_fact` itself opt out with `@pytest.mark.real_push_fact`.
    """
    if request.node.get_closest_marker("real_push_fact"):
        return

    async def offline(_workspace, _head):
        return "- Pushed: (stubbed in tests)"

    monkeypatch.setattr(_review, "_push_fact", offline)


@pytest.fixture(autouse=True)
def _no_granted_rounds(monkeypatch):
    """Granted review rounds are module state; never carried between tests."""

    monkeypatch.setattr(_session_config, "_granted_rounds", 0)
    monkeypatch.setattr(_supervise_support, "_escalated", False)
    monkeypatch.setattr(_supervise_support, "_preflight_escalated", False)
    monkeypatch.setattr(_supervise_support, "_preflight_resumes", 0)


@pytest.fixture(autouse=True)
def _no_planner(monkeypatch, request):
    """The planner is a sub-agent; pump tests get "no plan, convergence unknown" unless they say otherwise."""
    if request.node.get_closest_marker("real_planner"):
        return

    async def none(*_a, **_k):
        return "", None, ""

    monkeypatch.setattr(_contract, "_plan_next_round", none)


@pytest.fixture(autouse=True)
def _no_reset_clock(monkeypatch, request):
    """Limit tests use a fake monotonic clock; a real "resets 7pm (UTC)" would gate them on wall time."""
    if request.node.get_closest_marker("real_reset"):
        return

    monkeypatch.setattr(_usage, "_reset_at", lambda text, now=None: None)


@pytest.fixture(autouse=True)
def _no_contract(monkeypatch, request, tmp_path):
    """The contract is module state, and freezing it calls a sub-agent: off unless a test opts in."""

    monkeypatch.setattr(_contract, "_contract", "")
    monkeypatch.setattr(_contract, "_contract_path", None)
    monkeypatch.setattr(_session_config, "_round_scores", [])
    monkeypatch.setattr(_contract, "_contract_failed", False)
    # _pump points the contract at STATE_ROOT; a test must never touch the live run's file (qa).
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    if request.node.get_closest_marker("real_contract"):
        return

    async def none(*_a, **_k):
        return None

    monkeypatch.setattr(_contract, "_load_or_freeze_contract", none)


@pytest.fixture(autouse=True)
def _fresh_usage(monkeypatch):

    monkeypatch.setattr(_session_config, "_usage", {})
    monkeypatch.setattr(_usage, "_metering_round", 0)
    monkeypatch.setattr(_session_config, "_usage_loaded", False)


@pytest.fixture(autouse=True)
def _fresh_directions(monkeypatch):
    """Human direction is module state; never shared between tests."""

    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_inbox, "_seen_human_entries", set())
    monkeypatch.setattr(_inbox, "_transcript_offsets", {})
    monkeypatch.setattr(_inbox, "_transcript_heads", {})


@pytest.fixture(autouse=True)
def _raw_terminal_log_in_tmp(tmp_path, monkeypatch):
    """The raw terminal capture is a file under $HOME; no test may write there."""

    monkeypatch.setattr(_session_config, "RAW_TERMINAL_LOG", tmp_path / "logs" / "terminal.log")


@pytest.fixture(autouse=True)
def _gate_cache_in_tmp(tmp_path_factory, monkeypatch):
    """Gate results are cached under the cache root: no test may write the real one or read a stale hit."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("gate-cache")))


@pytest.fixture(autouse=True)
def _isolated_factory_gates(request, monkeypatch):
    """Loop tests isolate the new subprocess gate; gate tests explicitly exercise it."""
    if request.node.get_closest_marker("real_factory_gates"):
        return
    from flyte_agent_factory import _gates

    monkeypatch.setattr(_gates, "run", lambda *_args: "")


@pytest.fixture(autouse=True)
def _no_queue_read_in_flight(monkeypatch):
    """A queue read a hang test left running must not make the next test's refresh skip."""

    monkeypatch.setattr(_supervise_support, "_queue_read", None)


@pytest.fixture(autouse=True)
def state_store(tmp_path_factory):
    """The factory's state store over a real local bare git repository, for every test.

    Without it a test that reaches the store would build one from the profile and push to its real
    repository. `ttl` is the production one; a test that needs another writer builds a second
    `StateStore` (own cache) on `Path(state_store.remote)` with `tests.state_git.make_store(remote=...)`.
    """
    from flyte_agent_factory import _state_store
    from tests.state_git import make_store

    store = make_store(tmp_path_factory.mktemp("state"), name="suite", ttl=1.0)
    _state_store.configure(store)
    yield store
    _state_store.configure(None)


@pytest.fixture
def queue_state(state_store) -> str:
    """The launch queue's root, on the state store `state_store` made."""
    from flyte_agent_factory import _factory_base

    return _factory_base.queue_root()


@pytest.fixture
def flyte_world(queue_state, monkeypatch):
    """`_factory` against a fake Flyte remote and a real queue on a local git store."""
    from flyte_agent_factory import _factory
    from tests.fake_flyte import FakeFlyte

    world = FakeFlyte(monkeypatch, _factory)
    monkeypatch.setattr(world, "queue", queue_state, raising=False)  # the fake declares no `queue`
    return world


ENGINE_NAMES = ("claude", "codex")
TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _no_test_reaches_a_real_engine(monkeypatch, tmp_path_factory):
    """Run `claude`/`codex` only from a test's own fakes, never the pod's real, quota-spending CLI.

    A bare name resolves to the first fake on the child's PATH under this session's temp root or tests/,
    skipping the pod's tool directories that `_export_runtime_env` puts first. A real CLI raises. The
    documented live-test opt-in (`AGENT_FACTORY_LIVE_TESTS`) is exempt.
    """
    if os.environ.get("AGENT_FACTORY_LIVE_TESTS"):
        return
    import asyncio

    roots = (tmp_path_factory.getbasetemp().resolve(), TESTS_DIR)
    real_exec = asyncio.create_subprocess_exec

    def ours(path: Path) -> bool:
        path = path.resolve()
        return any(path.is_relative_to(root) for root in roots)

    async def guarded_exec(program, *args, **kwargs):
        name = os.fspath(program)
        if os.path.basename(name) in ENGINE_NAMES:
            if os.sep in name:
                if not ours(Path(name)):
                    raise RuntimeError(f"test tried to run a real engine CLI: {name}")
            else:
                path = (kwargs.get("env") or os.environ).get("PATH", "")
                fakes = [Path(d) / name for d in path.split(os.pathsep) if d]
                fake = next((f for f in fakes if f.is_file() and ours(f)), None)
                if fake is None:
                    raise RuntimeError(f"test tried to run a real `{name}`: no fake on its PATH")
                program = str(fake)
        return await real_exec(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_exec)


@pytest.fixture(autouse=True)
def _no_test_packs_this_checkouts_ignored_files(monkeypatch):
    """Snapshot only the repo's own files when a test hands the checkout itself to `_upload_workspace`.

    Review tests pass `Path(".")`, which is this checkout, and the snapshot tars it whole: with a local
    `.venv` (~15k files) that made each review test take 20s alone and minutes under a parallel run. A
    test workspace that is not the checkout is packed as it is, `.venv` included.
    """
    import shutil
    import subprocess

    real = _checkpoint._upload_workspace
    root = TESTS_DIR.parent

    async def upload(workspace):
        if Path(workspace).resolve() != root:
            return await real(workspace)
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        ).stdout.decode()
        with tempfile.TemporaryDirectory(prefix="checkout-") as copy:
            for name in filter(None, listed.split("\0")):
                source, target = root / name, Path(copy) / name
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            return await real(Path(copy))

    monkeypatch.setattr(_checkpoint, "_upload_workspace", upload)


def patch_supervisor_clock(monkeypatch, clock):
    """Replace `time` in every module that `agent` was before it was split, so one clock drives them all."""
    from flyte_agent_factory import _author, _pre_review, _subagent_run, _supervise, _supervise_support, agent

    for module in (agent, _author, _pre_review, _subagent_run, _supervise, _supervise_support):
        monkeypatch.setattr(module, "time", clock)


def reload_agent(module):
    """Re-import `agent` as a pod would, after the environment was changed.

    Paths and the selected profile are read at import by `_session_config`, which `agent` only looks
    up, so it has to be re-imported first.
    """
    import importlib

    importlib.reload(_session_config)
    return importlib.reload(module)


@pytest.fixture
def project(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("def answer():\n    return 1\n")
    (app / "test_main.py").write_text(
        "from main import answer\n\ndef test_answer():\n    assert answer() == 1\n"
    )
    git(tmp_path, "add", "app")
    git(tmp_path, "commit", "-m", "base")
    _gates.capture_base(tmp_path, "app")
    git(tmp_path, "config", "user.email", "app@agent-factory.localhost")
    return tmp_path, (tmp_path / ".claude-flyte/app/base-commit").read_text()


@pytest.fixture(autouse=True)
def _a_pod_with_its_tier_key(monkeypatch):
    # Every entry task establishes its Union identity first (`_union_identity.establish`). Tests call those
    # tasks directly, so give them a pod that holds a key; the probe would need a backend.
    import base64

    from flyte_agent_factory import _union_identity

    key = base64.b64encode(b"union.example.org:test-client:test-secret:").decode()
    monkeypatch.setenv(_union_identity.KEY_ENV, key)
    monkeypatch.setenv(_union_identity.TIER_ENV, "project")
    monkeypatch.setenv(_union_identity.SECRET_ENV, "test-project-key")
    monkeypatch.setattr(_union_identity, "_probe", lambda: None)
    _union_identity.current.cache_clear()
    yield
    _union_identity.current.cache_clear()


@pytest.fixture
def seal_namespaces():
    """The seal tests launch through user namespaces, as a sealed run does: required, never skipped."""
    from flyte_agent_factory import _seal_net

    assert _seal_net.available(), (
        f"these tests need unprivileged user, net and mount namespaces ({_seal_net.unavailable_reason()}); "
        "on Ubuntu 24.04: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0"
    )


@pytest.fixture
def git_http_backend():
    """The upstream and push tests serve git through `git http-backend` (git ships it): never skipped."""
    from tests.test_seal_proxy import GIT_BACKEND

    assert GIT_BACKEND.exists(), f"git http-backend is missing at {GIT_BACKEND}"
