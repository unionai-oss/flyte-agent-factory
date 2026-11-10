import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte
import pytest
from flyte.ai.mcp import FlyteMCPAppEnvironment
from flyte.ai.mcp._flyte_mcp_app import ALL_MCP_TOOLS, TOOL_REGISTRY
from flyte.models import ActionPhase
from mcp.server.fastmcp.exceptions import ToolError

from flyte_agent_factory import _factory, _factory_base
from flyte_agent_factory import _factory_mcp as factory

ROOT = Path(factory.__file__).resolve().parents[2]


def test_is_the_stock_flyte_mcp_app_behind_union_auth():
    # Union's ingress authenticates each request and Flyte runs each call as the caller, so a
    # sign-in token and a Union API key both work as the bearer; nothing here checks tokens.
    assert type(factory.app) is FlyteMCPAppEnvironment
    assert factory.app.requires_auth is True
    assert not factory.app.secrets


def test_chat_can_read_a_tasks_result():
    assert "get_run_io" in factory.TOOLS and "get_run_io" in factory.app.enabled_tools


def test_the_sweep_runs_every_five_minutes_on_spot():
    [trigger] = _factory.sweep.triggers
    assert trigger.name == "queue_sweep"
    assert isinstance(trigger.automation, flyte.FixedRate)
    assert trigger.automation.interval_minutes == 5
    assert _factory.sweep.interruptible is True


def test_scope_defaults_to_the_factory_project():
    assert factory.app.env_vars is not None
    assert factory.app.env_vars["FLYTE_MCP_PROJECT"] == _factory.PROJECT
    assert factory.app.env_vars["FLYTE_MCP_DOMAIN"] == _factory.DOMAIN


def test_instructions_and_skill_describe_launching_through_the_queue():
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "factory.launch" in text and "factory.queue_list" in text and "factory.queue_cancel" in text
        assert "get_run_io" in text
        assert "factory.status" in text
        assert "signal_condition" in text
        assert "launch_project" not in text
    for url in factory.REPOS.values():
        assert url in factory.INSTRUCTIONS


def test_instructions_have_operator_chats_decide_engineering_asks():
    text = " ".join(factory.INSTRUCTIONS.split())
    assert "Decide an engineering-judgement ask- yourself" in text
    assert "what is the right thing to do, never a waiver" in text
    assert 'starts "Operator decision (engineering judgement): "' in text
    assert "tell the user what you decided" in text
    assert "Relay only owner-reserved asks" in text
    assert "one at a time" in text and "signal_condition their answer verbatim" in text
    assert "decisions, with reasoning" in text
    assert "overrule decision-003" in text


def test_operator_relay_list_carries_the_whole_floor_and_profile_extras(monkeypatch):
    """The relay sentence had silently dropped two floor categories; it renders the one list now."""
    from flyte_agent_factory import _profile, _reserved

    text = " ".join(factory.INSTRUCTIONS.split())
    for entry in _reserved.OWNER_RESERVED_FLOOR:
        assert " ".join(entry.split()) in text
    extra = "changes to the public API of the billing service"
    assert extra not in text
    source = (Path(__file__).resolve().parents[1] / "profiles" / "example.toml").read_text()
    profile = _profile.parse(source + f'\n[judgement]\nowner_reserved = ["{extra}"]\n')
    monkeypatch.setattr(factory, "PROFILE", profile)
    assert extra in factory._relay_list()


def test_chat_is_not_told_to_pick_a_slot():
    # Replaces test_instructions_count_every_live_phase_as_a_busy_slot, which pinned the instruction to
    # check each slot with list_runs before launching; a launch now queues when the slots are full.
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "run_task on a free slot" not in text
        for slot in _factory_base.SLOTS:
            assert slot not in text
        assert "never fail" in text
        assert "same name" in text or "same `name`" in text


def test_instructions_pass_the_launch_model_explicitly():
    # The slot task's own default is the author model, not the launch model.
    from flyte_agent_factory._policy import LAUNCH_MODEL

    assert f"always pass it: {LAUNCH_MODEL}" in factory.INSTRUCTIONS
    assert "always pass it" in (ROOT / "skills/agent-factory/SKILL.md").read_text()


def test_instructions_say_where_issue_comments_are_posted():
    # Rewritten in state step 1 (the design's gating change): status comments for a linked issue are
    # posted wherever the App is configured, no longer only where factory-issues drained them.
    assert "status comments are posted on it\nwherever the factory's GitHub App is configured" in (
        factory.INSTRUCTIONS
    )


def test_instructions_and_skill_describe_benchmark_inputs_and_spend_limits():
    skill = (ROOT / "skills/agent-factory/SKILL.md").read_text()
    for text in (factory.INSTRUCTIONS, skill):
        assert "factory-bench.benchmark" in text
        for name in (
            "candidate_sha",
            "baseline_sha",
            "tasks",
            "repeats",
            "candidate_policy",
            "baseline_policy",
            "reuse_baseline_days",
            "max_usd",
        ):
            assert name in text
        assert "estimate" in text and "spends nothing" in text
        assert "ceiling_usd" in text and "[bench] max_usd" in text


@pytest.fixture
def chat_benchmark(monkeypatch):
    from flyte_agent_factory import _bench_flow
    from tests.test_bench import profile

    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    monkeypatch.setattr(_bench_flow, "_app", lambda: object())
    monkeypatch.setattr(flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(run_name="chat-bench")))

    def unexpected_spend(*args, **kwargs):
        pytest.fail("validation and estimates must not deploy, launch cases or publish results")

    monkeypatch.setattr(_bench_flow, "deploy_engine", unexpected_spend)
    monkeypatch.setattr(_bench_flow, "run_case", unexpected_spend)
    monkeypatch.setattr(_bench_flow, "_store", unexpected_spend)
    return _bench_flow.benchmark.func


@pytest.mark.parametrize(
    "inputs, error",
    [
        ({"candidate_sha": "z" * 40}, "not a 40-character commit SHA"),
        ({"baseline_sha": "z" * 40}, "not a 40-character commit SHA"),
        ({"tasks": ["unknown-task"]}, "no task 'unknown-task'"),
        ({"repeats": 4}, "repeats must be 1 to 3"),
        ({"repeats": 0}, "repeats must be 1 to 3"),
        ({"max_usd": 39}, r"can spend up to \$40.*over max_usd 39"),
        ({"baseline_sha": "f" * 40, "max_usd": 79}, r"can spend up to \$80.*over max_usd 79"),
        ({"max_usd": 101}, "over the profile's"),
        ({"max_usd": -1}, "max_usd must be 0"),
        ({"max_usd": float("inf")}, "must be finite"),
        ({"max_usd": float("nan")}, "must be finite"),
        ({"candidate_policy": "[]"}, "policy must be a JSON object"),
        ({"baseline_sha": "f" * 40, "baseline_policy": "[]"}, "policy must be a JSON object"),
    ],
)
async def test_chat_benchmark_refuses_invalid_inputs_before_spending(chat_benchmark, inputs, error):
    args = {"candidate_sha": "a" * 40, "tasks": ["bugfix-shortlinks"], "repeats": 1, "max_usd": 40}
    with pytest.raises(ValueError, match=error):
        await chat_benchmark(**(args | inputs))


@pytest.mark.parametrize("baseline_sha, cases, ceiling", [("", 1, 40), ("f" * 40, 2, 80)])
async def test_chat_benchmark_estimate_spends_nothing(chat_benchmark, baseline_sha, cases, ceiling):
    summary, jsonl = await chat_benchmark(
        candidate_sha="a" * 40,
        baseline_sha=baseline_sha,
        tasks=["bugfix-shortlinks"],
        repeats=1,
        max_usd=0,
    )
    result = json.loads(summary)
    assert result["estimate_only"] is True
    assert result["engine_sha"] == "a" * 40
    assert result["estimate"]["cases"] == cases
    assert result["estimate"]["ceiling_usd"] == ceiling
    assert result["estimate"]["max_usd"] == 0
    assert jsonl == ""


def _phase_text(phase: Any) -> str:
    return str(getattr(phase, "value", phase)).lower()


class LaggingRun:
    """A run whose read model reports it done only after `lag` reads, as Union's does after SUCCEEDED."""

    def __init__(
        self, lag: int, phase: Any = ActionPhase.SUCCEEDED, name: str = "r1", stale_phase: Any = None
    ) -> None:
        """Start a run that reads as done only after `lag` reads; `stale_phase` shows until then."""
        self.reads, self.lag, self.name, self.url = 0, lag, name, "http://run"
        self.final_phase, self.stale_phase = phase, stale_phase

    @property
    def phase(self):
        """The phase the read model shows: stale until the lag has passed, then the final one."""
        # The read model can still show an earlier phase for a while after the run has SUCCEEDED.
        if self.stale_phase is not None and self.reads <= self.lag:
            return self.stale_phase
        return self.final_phase

    def done(self) -> bool:
        """Whether the run reads as done: finished and past the lag."""
        return _phase_text(self.final_phase) != "running" and self.reads > self.lag

    async def _inputs(self):
        return {"name": "x"}

    @property
    def inputs(self):
        """The run's inputs, as the remote `Run.inputs` exposes them."""
        return SimpleNamespace(aio=self._inputs)

    async def _outputs(self):
        return SimpleNamespace(named_outputs={"o0": '{"status": "started"}'})

    @property
    def outputs(self):
        """The run's outputs, as the remote `Run.outputs` exposes them."""
        return SimpleNamespace(aio=self._outputs)


@pytest.fixture
def lagging(monkeypatch):
    """Make `Run.get` return a `LaggingRun`, and wait_for_run's watch return at once."""
    import flyte.remote

    def install(run):
        async def get(name):
            run.reads += 1
            return SimpleNamespace(
                name=run.name,
                url=run.url,
                phase=run.phase,
                done=run.done,
                inputs=run.inputs,
                outputs=run.outputs,
                wait=SimpleNamespace(aio=lambda quiet=True: _done()),
            )

        async def _done() -> None:
            return None

        monkeypatch.setattr(flyte.remote.Run, "get", SimpleNamespace(aio=get))
        return run

    import flyte._initialize

    # `_scoped` only needs an init config whose scope already matches; there is no cluster to reach.
    scope = SimpleNamespace(project=_factory.PROJECT, domain=_factory.DOMAIN)
    monkeypatch.setattr(flyte._initialize, "_get_init_config", lambda: scope)
    monkeypatch.setenv("FLYTE_MCP_PROJECT", _factory.PROJECT)
    monkeypatch.setenv("FLYTE_MCP_DOMAIN", _factory.DOMAIN)
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 2.0)
    monkeypatch.setattr(factory, "_POLL_FIRST_S", 0.01)
    monkeypatch.setattr(factory, "_POLL_MAX_S", 0.05)
    return install


async def call(tool: str, **arguments: Any) -> Any:
    """Call `tool` on the factory's MCP app and return its JSON result."""
    assert factory.app.mcp is not None
    [content] = await factory.app.mcp.call_tool(tool, arguments)
    return json.loads(cast(Any, content).text)  # call_tool is typed as any content block; ours are text


async def test_get_run_io_waits_for_outputs_the_read_model_has_not_published(lagging):
    """`get_run_io` waits for outputs the read model has not yet published."""
    run = lagging(LaggingRun(lag=6))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_get_run_io_says_so_when_the_outputs_never_become_readable(lagging, monkeypatch):
    """`get_run_io` says so when the outputs never become readable."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None
    assert "not yet readable" in result["outputs_error"] and "Retry get_run_io" in result["outputs_error"]


@pytest.mark.parametrize("phase", [ActionPhase.FAILED, ActionPhase.ABORTED, ActionPhase.TIMED_OUT])
async def test_get_run_io_does_not_wait_for_a_run_that_has_no_outputs_coming(lagging, phase):
    """`get_run_io` does not wait for a run that has no outputs coming."""
    run = lagging(LaggingRun(lag=10**9, phase=phase))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None and "outputs_error" not in result
    assert run.reads <= 4


async def test_get_run_io_waits_while_the_read_model_still_shows_a_running_phase(lagging):
    """`get_run_io` keeps waiting while the lagging read model still shows a running phase."""
    # The stock get_run reads the same lagging model, so during the lag it says "running".
    run = lagging(LaggingRun(lag=6, stale_phase=ActionPhase.RUNNING))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_get_run_io_never_returns_a_bare_null_during_the_lag(lagging, monkeypatch):
    """`get_run_io` never returns a bare null during the lag."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9, stale_phase=ActionPhase.RUNNING))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] is None
    assert "not yet readable" in result["outputs_error"] and "Retry get_run_io" in result["outputs_error"]


@pytest.mark.parametrize("phase", [ActionPhase.SUCCEEDED, "SUCCEEDED", "succeeded", ActionPhase.RECOVERED])
async def test_get_run_io_waits_whatever_the_phase_is_spelled_as(lagging, phase):
    """`get_run_io` waits whatever way the phase is spelled."""
    run = lagging(LaggingRun(lag=6, phase=phase))
    result = await call("get_run_io", name="r1")
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6


async def test_wait_for_run_returns_once_the_outputs_are_readable(lagging):
    """`wait_for_run` returns once the outputs are readable."""
    lagging(LaggingRun(lag=6))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "succeeded" and result["outputs_ready"] is True


async def test_wait_for_run_reports_outputs_not_ready_instead_of_hanging(lagging, monkeypatch):
    """`wait_for_run` reports outputs not ready instead of hanging."""
    monkeypatch.setattr(factory, "_OUTPUTS_WAIT_S", 0.2)
    lagging(LaggingRun(lag=10**9))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "succeeded" and result["outputs_ready"] is False


async def test_wait_for_run_leaves_a_failed_run_alone(lagging):
    """`wait_for_run` leaves a failed run alone."""
    run = lagging(LaggingRun(lag=10**9, phase=ActionPhase.FAILED))
    result = await call("wait_for_run", name="r1", timeout_s=5)
    assert result["phase"] == "failed" and "outputs_ready" not in result
    assert run.reads <= 3


async def test_the_patient_tools_replace_the_stock_ones_without_changing_the_tool_set():
    """The patient tools replace the stock ones without changing the tool set."""
    assert factory.app.mcp is not None
    tools = {tool.name: tool for tool in await factory.app.mcp.list_tools()}
    assert set(tools) == set(factory.TOOLS)
    for name in ("get_run_io", "wait_for_run"):
        description = tools[name].description or ""
        assert "bounded" in description or "outputs_ready" in description
    annotations = tools["get_run_io"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True


def test_instructions_tell_chat_what_to_do_when_outputs_are_not_yet_readable():
    """The instructions tell chat what to do when outputs are not yet readable."""
    assert "not yet readable" in factory.INSTRUCTIONS and "call get_run_io again" in factory.INSTRUCTIONS


# ---- every stock tool, limited to the factory's project and domain ----

OTHER = {"project": "other-project", "domain": "other-domain"}


def _registered_tools() -> dict[str, Any]:
    assert factory.app.mcp is not None
    return {tool.name: tool for tool in factory.app.mcp._tool_manager.list_tools()}


def _scoped_tool_names() -> list[str]:
    """Every registered tool that takes a project or a domain, found from the tools themselves."""
    return sorted(
        name
        for name, tool in _registered_tools().items()
        if {"project", "domain"} & set(inspect.signature(tool.fn).parameters)
    )


def _required_arguments(tool_name: str) -> dict[str, Any]:
    """Valid values for the arguments a tool cannot do without, apart from project and domain."""
    parameters = inspect.signature(_registered_tools()[tool_name].fn).parameters
    required = {
        name: p
        for name, p in parameters.items()
        if p.default is inspect.Parameter.empty and name not in ("ctx", "project", "domain")
    }
    return {name: {} if name == "inputs" else "x" for name in required}


async def call_list(tool: str, **arguments: Any) -> list[dict[str, Any]]:
    """Call a tool that returns a list; FastMCP gives those as (content blocks, {"result": the list})."""
    assert factory.app.mcp is not None
    _content, structured = cast(Any, await factory.app.mcp.call_tool(tool, arguments))
    return cast(list[dict[str, Any]], structured["result"])


def test_exposes_launching_and_following_projects_only():
    """The tools for launching and following projects are there, limited to the factory's project.

    This test also pinned the absence of the secret and trigger tools; the brief asks for every stock tool,
    so that is now `test_enables_every_stock_tool`, and "only" is the project/domain scope check.
    """
    assert factory.app.enabled_tools == set(factory.TOOLS)
    assert "run_task" in factory.app.enabled_tools
    assert factory.app.env_vars is not None and factory.app.env_vars["FLYTE_MCP_PROJECT"] == _factory.PROJECT
    following = {"run_task", "get_run", "get_run_io", "wait_for_run", "list_conditions"}
    assert set(_scoped_tool_names()) >= following


def test_enables_every_stock_tool():
    assert factory.app.enabled_tools == set(ALL_MCP_TOOLS)
    assert set(factory.TOOLS) == set(ALL_MCP_TOOLS)
    assert set(_registered_tools()) == set(ALL_MCP_TOOLS)
    assert len(ALL_MCP_TOOLS) == len(TOOL_REGISTRY)


def test_the_allowlist_is_exactly_the_factory_chat_tasks_and_benchmark():
    """No task allowlist any more; the chat tasks are the ones the instructions steer to, and no slot task."""
    # The brief drops the per-task allowlist: any task registered in the factory project and domain can be
    # run, and the scope check below keeps chat inside it. What this test pinned as the allowlist is now
    # `CHAT_TASKS`, the set the instructions steer chat to, which still names no slot task.
    assert factory.app.task_allowlist is None
    assert factory.CHAT_TASKS == (
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
    assert factory.CODEX_LOGIN_TASK == "factory-setup.codex_bootstrap_login"
    assert not any(slot in task for task in factory.CHAT_TASKS for slot in _factory_base.SLOTS)


@pytest.fixture
def remote(lagging, monkeypatch):
    """Fake the `flyte.remote` calls the tools make, and record each one in `calls`."""
    import flyte.remote

    calls: list[tuple[str, Any]] = []

    def task_get(**kwargs):
        calls.append(("Task.get", kwargs))
        return SimpleNamespace(name=kwargs["name"])

    async def run(task, **inputs):
        calls.append(("run", (task.name, inputs)))
        return SimpleNamespace(url="http://run", name="r1")

    async def project_listall(*args, **kwargs):
        calls.append(("Project.listall", None))
        for project in (_factory.PROJECT, "other-project"):
            yield SimpleNamespace(pb2=SimpleNamespace(id=project, name=project, description="", state=0))

    async def project_get(name):
        calls.append(("Project.get", name))
        raise AssertionError("get_project must refuse before asking Union")

    async def secret_listall(*args, **kwargs):
        # A secret object that carries its value, as a careless SDK change might make it.
        yield SimpleNamespace(name="claude_token", type="regular", value="hunter2-secret-value")

    async def secret_create(name, value, type):
        calls.append(("Secret.create", name))

    monkeypatch.setattr(flyte.remote.Task, "get", task_get)
    monkeypatch.setattr(flyte.run, "aio", run)
    monkeypatch.setattr(flyte.remote.Project, "listall", SimpleNamespace(aio=project_listall))
    monkeypatch.setattr(flyte.remote.Project, "get", SimpleNamespace(aio=project_get))
    monkeypatch.setattr(flyte.remote.Secret, "listall", SimpleNamespace(aio=secret_listall))
    monkeypatch.setattr(flyte.remote.Secret, "create", SimpleNamespace(aio=secret_create))
    lagging(LaggingRun(lag=0))
    return calls


@pytest.mark.parametrize(
    "project, name, allowed",
    [
        (_factory.PROJECT, "factory.launch", True),
        (_factory.PROJECT, "factory.status", True),
        (_factory.PROJECT, "factory.queue_list", True),
        (_factory.PROJECT, "factory.queue_cancel", True),
        (_factory.PROJECT, "factory-bench.benchmark", True),
        # The cases below were refused by the old allowlist. The brief has chat run any task in the factory
        # project and domain (slot tasks are unsupported, not blocked), so they now start.
        (_factory.PROJECT, "factory-deployer.deploy_if_changed", True),
        (_factory.PROJECT, "factory-setup.gcp_bootstrap", True),
        (_factory.PROJECT, "factory-bench.deploy_engine", True),
        (_factory.PROJECT, "factory-bench.run_case", True),
        (_factory.PROJECT, "factory-bench.hidden_check", True),
        (_factory.PROJECT, _factory_base.SLOTS[0], True),
        (_factory.PROJECT, _factory_base.SLOTS[-1], True),
        (_factory.PROJECT, "factory.sweep", True),
        (_factory.PROJECT, "claude-agent.project", True),
        (_factory.PROJECT, "agent-factory.project", True),
        (_factory.PROJECT, "factory.read_login_meter", True),
        # Still refused, now by the server's project/domain check rather than the allowlist.
        ("other-project", "factory.launch", False),
        ("other-project", "factory-bench.benchmark", False),
    ],
)
async def test_run_task_reaches_the_queue_tasks_in_the_factory_project_only(remote, project, name, allowed):
    """Any task in the factory project starts through run_task; nothing in another project does."""
    arguments = {"project": project, "domain": _factory.DOMAIN, "name": name, "inputs": {"a": 1}}
    if not allowed:
        with pytest.raises(ToolError, match=project):
            await call("run_task", **arguments)
        assert not remote
        return
    assert await call("run_task", **arguments) == {"url": "http://run", "name": "r1"}
    assert ("run", (name, {"a": 1})) in remote


def _scoped_tool_arguments() -> list[tuple[str, str]]:
    """(tool, argument) for every registered tool and each of `project`/`domain` it takes."""
    return [
        (name, argument)
        for name in _scoped_tool_names()
        for argument in ("project", "domain")
        if argument in inspect.signature(_registered_tools()[name].fn).parameters
    ]


@pytest.mark.parametrize("tool, argument", _scoped_tool_arguments())
async def test_a_tool_refuses_another_project_or_domain_before_any_remote_call(
    remote, monkeypatch, tool, argument
):
    reached: list[tuple[Any, ...]] = []

    def scoped(*args: Any, **kwargs: Any) -> None:
        reached.append(args)
        pytest.fail(f"{tool} reached the remote scope")

    monkeypatch.setattr(factory.app, "_scoped", scoped)
    arguments = _required_arguments(tool) | {argument: OTHER[argument]}
    with pytest.raises(ToolError, match=OTHER[argument]):
        assert factory.app.mcp is not None
        await factory.app.mcp.call_tool(tool, arguments)
    assert not reached and not remote


def test_every_tool_that_takes_a_project_or_domain_is_covered_by_the_scope_tests():
    assert {"run_task", "get_task", "get_run", "wait_for_run", "get_run_io", "list_secrets"} <= set(
        _scoped_tool_names()
    )
    assert not {"whoami", "search_full_docs", "list_projects", "get_project"} & set(_scoped_tool_names())


async def test_the_factorys_own_project_and_domain_are_accepted(remote):
    result = await call_list("list_secrets", project=_factory.PROJECT, domain=_factory.DOMAIN)
    assert result == [{"name": "claude_token", "type": "regular"}]


async def test_scoping_leaves_each_tools_input_schema_unchanged():
    stock = FlyteMCPAppEnvironment(name="stock", tools=list(ALL_MCP_TOOLS))
    assert stock.mcp is not None
    stock_tools = {t.name: t for t in stock.mcp._tool_manager.list_tools()}
    ours = _registered_tools()
    assert set(ours) == set(stock_tools)
    for name, tool in ours.items():
        assert tool.parameters == stock_tools[name].parameters, name
        assert tool.annotations == stock_tools[name].annotations, name


async def test_list_projects_shows_only_the_factory_project(remote):
    result = await call_list("list_projects")
    assert [p["id"] for p in result] == [_factory.PROJECT]


async def test_get_project_refuses_another_project(remote):
    with pytest.raises(ToolError, match="other-project"):
        assert factory.app.mcp is not None
        await factory.app.mcp.call_tool("get_project", {"name": "other-project"})
    assert ("Project.get", "other-project") not in remote


async def test_list_secrets_returns_names_and_types_only(remote):
    result = await call_list("list_secrets")
    assert result == [{"name": "claude_token", "type": "regular"}]
    assert "hunter2-secret-value" not in json.dumps(result)


async def test_create_secret_does_not_echo_the_value(remote):
    result = await call("create_secret", name="claude_token", value="hunter2-secret-value")
    assert result == {"name": "claude_token", "created": True}
    assert "hunter2-secret-value" not in json.dumps(result)
    assert ("Secret.create", "claude_token") in remote


async def test_the_patient_tools_are_still_patient_once_scoped(lagging):
    # The scope wrappers sit over the patient ones; the existing lag tests above go through both. This checks
    # the wrapped function itself is the patient one and that it still passes the scope through.
    run = lagging(LaggingRun(lag=6))
    result = await call("get_run_io", name="r1", project=_factory.PROJECT, domain=_factory.DOMAIN)
    assert result["outputs"] == {"o0": '{"status": "started"}'}
    assert run.reads > 6
    assert "bounded" in (_registered_tools()["get_run_io"].description or "")


def test_instructions_describe_the_wider_surface_and_its_limits():
    text = factory.INSTRUCTIONS
    assert "factory-deployer.deploy_if_changed" in text
    assert "names only" in text and "list_secrets" in text
    assert "list_apps" in text and "list_triggers" in text
    assert "unsupported" in text
    assert "limited to this project and domain" in text


def test_search_tools_read_a_corpus_the_image_carries_outside_root():
    for path in (factory.app.sdk_examples_path, factory.app.docs_examples_path, factory.app.full_docs_path):
        assert path is not None and path.startswith("/opt/flyte-mcp-docs/")
    commands = " ".join(
        str(layer)
        for layer in factory.app.image._layers  # type: ignore[union-attr]
    )
    assert "v2.10.7" in commands and "llms.txt" in commands and "/opt/flyte-mcp-docs" in commands


def test_the_grant_page_is_served_by_the_factory_app_at_its_public_path():
    from starlette.testclient import TestClient

    from flyte_agent_factory import _grant_page
    from flyte_agent_factory._grant_seal import PAGE_PATH

    assert factory.app._starlette_app is not None
    client = TestClient(factory.app._starlette_app)
    # The stock auth middleware wants the browser's Union cookie (or a bearer); with one it serves the page.
    assert client.get(PAGE_PATH).status_code == 401
    response = client.get(PAGE_PATH, headers={"Cookie": "union=signed-in"})
    assert response.status_code == 200
    assert response.text == _grant_page.PAGE_HTML
    assert response.headers["Cache-Control"] == "no-store"
    assert "connect-src 'none'" in response.headers["Content-Security-Policy"]


def test_the_grant_page_adds_no_mcp_tool():
    assert factory.app.enabled_tools == set(factory.TOOLS)
    assert factory.app.mcp is not None
    assert {tool.name for tool in factory.app.mcp._tool_manager.list_tools()} == set(factory.TOOLS)
