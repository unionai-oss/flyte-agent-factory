"""The run report, which is the only place the claude.ai session URL surfaces."""

from flyte_agent_factory import _instructions, _session_config, _session_setup, _timeline
from flyte_agent_factory._session_setup import _write_managed_block
from tests.conftest import reload_agent


def test_report_escapes_content_and_keeps_the_session_link():
    timeline = _timeline._Timeline()
    timeline.header("project", "oauth <flow>")
    timeline.header("Claude app", "https://claude.ai/code/abc", link=True)
    timeline.row("📨", "direction: <script>alert(1)</script> & more")

    rendered = timeline.render()
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="https://claude.ai/code/abc"' in rendered


def test_report_is_bounded():
    # A months-long project would otherwise grow an unbounded report.
    timeline = _timeline._Timeline(keep=50)
    for n in range(500):
        timeline.row("·", f"row {n}")
    assert timeline.render().count("<tr><td><code>") == 50


def test_managed_block_is_idempotent(tmp_path):
    memory = tmp_path / "CLAUDE.md"
    memory.write_text("# My project\n\nExisting house rules.\n")

    _write_managed_block(memory, _instructions._project_memory(browser_enabled=True))
    with_browser = memory.read_text()
    _write_managed_block(memory, _instructions._project_memory(browser_enabled=False))
    without_browser = memory.read_text()

    # The repo owns this file; we only own our block in it.
    assert "Existing house rules." in without_browser
    assert without_browser.count("agent-factory: begin") == 1
    assert "Chromium runs beside you" in with_browser
    assert "Chromium runs beside you" not in without_browser


def test_runtime_env_gives_the_container_what_it_cannot_assume(monkeypatch, tmp_path):
    """Paths and a git identity, both absent in a non-root container.

    Tasks run as an unprivileged user, so nothing may assume `/workspace` or
    `/usr/local/bin` is writable; and a fresh container has no git identity, so
    the `commit early and often` instruction would fail on the first commit.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))  # the agent's base follows this home
    monkeypatch.setenv("PATH", "/usr/bin")
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_TERMINAL_PROMPT",
    ):
        monkeypatch.delenv(var, raising=False)

    from flyte_agent_factory import agent as agent_module

    agent_module = reload_agent(agent_module)
    env = _session_setup._export_runtime_env()

    assert env["AGENT_FACTORY_WORKSPACE"] == str(tmp_path / "state" / "workspace")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "state" / "claude")
    assert env["PATH"].startswith(str(tmp_path / ".local" / "bin"))
    assert env["GIT_AUTHOR_EMAIL"]
    assert env["GIT_COMMITTER_NAME"] == env["GIT_AUTHOR_NAME"]
    # Otherwise a private remote blocks the agent forever on a password prompt.
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_runtime_env_sets_flyte_internal_image_from_the_pod_image_id(tmp_path, monkeypatch):
    """The runtime env takes `FLYTE_INTERNAL_IMAGE` from the pod's image id."""
    monkeypatch.setenv("AGENT_FACTORY_BASE", str(tmp_path))
    monkeypatch.setenv("_F_IMG_ID", "registry.example/img:1")
    monkeypatch.delenv("FLYTE_INTERNAL_IMAGE", raising=False)

    env = _session_setup._session_env(_session_setup._export_runtime_env())
    assert env["FLYTE_INTERNAL_IMAGE"] == "registry.example/img:1"

    # An image the platform did set is never overwritten.
    monkeypatch.setenv("FLYTE_INTERNAL_IMAGE", "registry.example/other:2")
    assert _session_setup._export_runtime_env()["FLYTE_INTERNAL_IMAGE"] == "registry.example/other:2"


def test_unattended_memory_tells_the_session_not_to_block():
    import inspect

    # There is no attended mode: every project's memory carries the unattended guidance.
    assert "unattended" not in inspect.signature(_instructions._project_memory).parameters
    memory = _instructions._project_memory(browser_enabled=False)

    # Every question must be bounded, or the run stalls forever.
    assert "--timeout" in memory
    assert "Nobody is watching" in memory
    assert "asking costs nothing" not in memory
    assert "Block on a human only for" in memory
    # The attended wording ("a long --timeout (or none) is right") is gone.
    assert "or none" not in memory


def test_permission_mode_is_chosen_at_runtime():
    """Empty default, so the runtime default applies unless the caller overrides it."""
    import inspect

    from flyte_agent_factory.agent import project

    params = inspect.signature(project.func).parameters
    assert params["permission_mode"].default == ""
    assert "unattended" not in params
    # The wrappers reject it (TypeError, argparse); the task itself ignores an undeclared input.


def test_bridge_wrappers_carry_pythonpath(tmp_path):
    """A bare `python -c` subprocess does not inherit the code-bundle root.

    The supervisor can import `flyte_agent_factory` because Flyte put the bundle on
    sys.path; a shell the agent spawns cannot. Without this the wrappers die
    with ModuleNotFoundError, and the agent has to discover PYTHONPATH itself.
    """
    from flyte_agent_factory._session_setup import _install_bridge_commands

    _install_bridge_commands(tmp_path)

    for name in ("ask-human", "flyte-note"):
        script = (tmp_path / name).read_text()
        assert "PYTHONPATH=" in script, f"{name} would not find flyte_agent_factory"
        # Must not clobber an inherited PYTHONPATH.
        assert "${PYTHONPATH:+:$PYTHONPATH}" in script


def test_team_is_seeded_and_design_guidance_is_not_clobbered(tmp_path, monkeypatch):
    """The roles are Claude Code's own subagent files, not an invented mechanism.

    `DESIGN.md` is seeded once and then left alone, because it is where a human
    writes the house rules — re-seeding it every run would erase their edits.
    """
    from flyte_agent_factory._session_setup import _seed_team

    # The package's roles, not whatever image roles directory the machine happens to have.
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path / "no-image-roles"))
    installed = _seed_team(tmp_path)
    assert set(installed) == {
        "scope",
        "discover",
        "prototype",
        "shape",
        "build",
        "verify",
        "product",
        "designer",
        "dev",
        "qa",
    }

    agents = tmp_path / ".claude" / "agents"
    for role in installed:
        body = (agents / f"{role}.md").read_text()
        # Claude Code discovers subagents by this frontmatter.
        assert body.startswith("---\nname: "), role
        assert "description:" in body.split("---")[1], role

    design = tmp_path / _session_config.DESIGN_FILE
    assert "Don't reinvent the wheel" in design.read_text()

    design.write_text("# Design guidance\n\nMy own rules.\n")
    _seed_team(tmp_path)
    assert design.read_text() == "# Design guidance\n\nMy own rules.\n"
    # Role files, by contrast, are ours and stay current.
    assert (agents / "verify.md").read_text().startswith("---\nname: verify")


def test_memory_points_at_the_team_and_the_house_rules():

    memory = _instructions._project_memory(browser_enabled=False)
    assert ".claude/agents/" in memory
    assert "several `verify` at once" in memory
    assert _session_config.DESIGN_FILE in memory


def test_roles_are_read_from_the_image_when_present(tmp_path, monkeypatch):
    """Flyte's code bundle carries modules, not package data.

    Reading the role files out of the installed package worked locally and
    found nothing in the container, so the swarm silently had no roles. The
    image is the source of truth; the package copy is the local fallback.
    """

    image_roles = tmp_path / "image-roles"
    image_roles.mkdir()
    (image_roles / "scout.md").write_text("---\nname: scout\ndescription: x\n---\nbody\n")
    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(image_roles))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _session_setup._seed_team(workspace) == ["scout"]
    assert (workspace / ".claude" / "agents" / "scout.md").is_file()


def test_roles_fall_back_to_the_package_for_local_runs(tmp_path, monkeypatch):

    monkeypatch.setattr(_session_setup, "AGENT_ROLES_DIR", str(tmp_path / "does-not-exist"))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert set(_session_setup._seed_team(workspace)) == {
        "scope",
        "discover",
        "prototype",
        "shape",
        "build",
        "verify",
        "product",
        "designer",
        "dev",
        "qa",
    }


def test_memory_teaches_hardware_discovery_not_a_hardcoded_size():
    """The agent should learn its limits from the scheduler, not from me.

    There is no API that reports free capacity, so an estimate baked in here
    would be wrong on another cluster and stale on this one. What generalises
    is the loop: probe at the shape you want, read the scheduler's verdict,
    resize, resubmit.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    # A sample of a real verdict, so it recognises one and reads it rather than
    # reasoning about why placement failed.
    assert "fits no nodepool" in memory, "should show what the verdict looks like"
    assert "probe with a trivial job" in memory.lower()
    assert "No API reports free capacity" in memory.replace("\n", " ")
    # Distinguishes rejected-at-admission from queued-but-unsatisfiable: they
    # look the same to an agent and need opposite responses.
    assert "neither failure nor progress" in memory
    assert "MaxQueuedTimeExceededError" in memory
    assert "interruptible=True" in memory

    # No baked-in sizes: they would be wrong elsewhere and stale here.
    for guess in ("2 CPU", "4-8Gi", "8Gi unless", "20Gi"):
        assert guess not in memory, f"hardcoded capacity guess leaked in: {guess}"


def test_browser_guidance_covers_recording_and_retrieval():
    """The recording tools are useless if the agent doesn't know they exist.

    `browser_start_video` only appears when the server runs with
    `--caps=devtools`, and anything it writes lives in the browser container,
    so it has to be fetched into the workspace to survive.
    """

    with_browser = _instructions._project_memory(browser_enabled=True)
    assert "browser_start_video" in with_browser
    assert "/output/" in with_browser
    assert "published with the run" in with_browser
    # It must not try to log in to things itself.
    assert "never try to log in" in with_browser

    without = _instructions._project_memory(browser_enabled=False)
    assert "browser_start_video" not in without, "no browser, no browser guidance"


def test_raw_session_output_never_reaches_the_report():
    """A repainting TUI redraws one line dozens of times.

    The old "notable line" filter matched error/abort, so the report became a
    wall of the same space-stripped text and buried the session URL and the
    agent's own notes. Container logs keep the raw stream.
    """
    from flyte_agent_factory._session_setup import _log_session_line

    timeline = _timeline._Timeline()
    log = _log_session_line(timeline)
    for _ in range(30):
        log("Monitor event: ActionAbortedError: Action abc was aborted, aborting current Action a0")

    rendered = timeline.render()
    assert "ActionAborted" not in rendered
    assert rendered.count("<tr><td><code>") == 0


def test_status_line_replaces_rather_than_accumulates():
    """A project that wakes and idles repeatedly must not bury its history."""

    timeline = _timeline._Timeline()
    timeline.row("📝", "built the dataset")
    for state in ("working", "idle for 3m", "working", "idle for 40m"):
        timeline.activity(state)

    rendered = timeline.render()
    assert rendered.count("<b>Status:</b>") == 1
    assert "idle for 40m" in rendered
    assert "idle for 3m" not in rendered
    assert "built the dataset" in rendered


def test_memory_requires_an_explicit_completion_note():

    memory = _instructions._project_memory(browser_enabled=False)
    assert "DONE:" in memory
    assert "RUNNING` whether" in memory


def test_spot_is_the_default_for_children_but_not_the_supervisor():
    """Spot is cheaper and a separate pool, so children should prefer it.

    The supervisor must not: it holds the Claude Code session, a preemption
    kills the conversation, and an interruptible parent would need spot
    capacity just to start — so it could never reach the on-demand fallback it
    exists to provide.
    """
    from flyte_agent_factory.agent import env, setup_env, subagent_env

    assert subagent_env.interruptible is True, "the swarm is the bulk of the compute"
    assert env.interruptible is False, "the session must not be preemptible"
    assert setup_env.interruptible is False, "a setup task waiting on a human should not be interrupted"


def test_memory_makes_spot_the_default_for_cpu_work():

    memory = _instructions._project_memory(browser_enabled=False)
    assert "spot first for cpu work" in memory.lower()
    # And explains why it is safe, not just cheap.
    assert "platform's budget" in memory
    assert "MaxQueuedTimeExceededError" in memory


def test_memory_says_spot_and_gpu_are_mutually_exclusive():
    """The previous wording caused a real failure.

    It said spot was "often the only way to get a GPU here". There are no GPUs
    in any spot nodepool on this cluster, so a run that followed that advice
    had its GPU probe rejected at admission in about a second.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "interruptible=False" in memory, "a GPU requires on-demand"
    assert "gpu=0" in memory, "should name what the spot nodepools actually report"
    assert "only way to get a GPU" not in memory, "the claim that caused the failure"


def test_memory_does_not_claim_a_device_class_is_unavailable():
    """Both verified on the cluster: no spot GPUs, but on-demand T4 is placeable.

    An intermediate version of this guidance said the cluster's GPUs were L40S
    only and that naming `nvidia-tesla-t4` "can never be placed" -- inferred
    from a set of tainted L40S nodes. A run then had exactly that request
    succeed. Asserting the claim stays out, because telling an agent a working
    configuration is impossible is worse than telling it nothing.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "can never be placed" not in memory
    assert "confirmed placeable" in memory
    assert "without probing" in memory, "it should probe rather than trust a list"


def test_end_project_sentinel_stops_the_project(tmp_path, monkeypatch):
    """ "We're done" in the Claude app must complete the run, not need an abort.

    An abort discards everything since the last checkpoint, so ending has to
    go through the supervisor's clean shutdown path.
    """
    from flyte_agent_factory import _bridge

    # BRIDGE_DIR is read at import time, so the env var alone is not enough.
    monkeypatch.setattr(_bridge, "BRIDGE_DIR", tmp_path / "bridge")
    root = _bridge.ensure_dirs(tmp_path / "bridge")
    bridge = _bridge.Bridge(root)
    assert bridge.stop_requested() is None

    _bridge.end_cli(["delivered the polynomial study"])
    assert bridge.stop_requested() == "delivered the polynomial study"


def test_a_crash_is_not_a_clean_finish():
    """A non-zero exit must raise so Flyte retries from the checkpoint.

    Returning normally would quietly end a months-old project on a bug, and the
    run would look successful.
    """
    import inspect

    from flyte_agent_factory.agent import project

    source = inspect.getsource(project.func)
    assert "session ended cleanly" in source
    assert "retrying from the last" in source
    # The clean path is gated on status 0, not on the process merely ending.
    assert "if status == 0:" in source


def test_memory_points_at_the_artifact_service_for_large_data():
    """Large data has a proper home, and the agent should look there first.

    A run asked for audio to be staged for it, and the answer it got was a git
    branch -- because nobody, including me, checked the artifact service. The
    upload path there goes through the control plane and needs no cloud
    credentials, which is the non-obvious part.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "Artifact.listall" in memory, "it should look before asking for a stage"
    assert "Artifact.get" in memory
    assert "upload_dir" in memory
    assert "attrs" in memory, "caveats travel with the data"
    # The traps that cost real time, including that syncify is inconsistent:
    # upload_dir returns a value, to_python returns a coroutine.
    assert "syncified" in memory
    assert "to_python` *is* a" in memory
    assert "No transformers could reverse" in memory
    assert "255" in memory


def test_memory_tells_it_to_push_not_just_commit():
    """A local commit is not durable, and the memory used to claim it was.

    The wording was "a commit is the only thing that reliably survives", which
    is false: a commit lives in the pod's workspace and depends on the same
    periodic snapshot as any untracked file. A run finished its work, reported
    it "committed", and nothing had left the cluster.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "git push" in memory
    assert "does not leave this pod" in memory
    assert "a commit is the only thing that reliably survives" not in memory


def test_memory_warns_that_a_restart_loses_the_venv_and_not_the_children():
    """Both halves were learned from one real restart.

    A run's pod was killed with no log at all; on resume the session reported
    "local venv lost, launcher processes died" and relaunched. The venv really
    is gone -- but the child actions were not: they keep running on the
    cluster, so relaunching pays for the same GPU work twice. That time they
    had all finished first, so nothing was wasted.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "Action.listall" in memory
    assert "do not resubmit" in memory
    assert "duplicates work you are already paying for" in memory
    # And the nuance about where a venv belongs, rather than "put it in the workspace".
    assert "multi-gigabyte ML stack" in memory


def test_memory_warns_that_a_stale_clone_blocks_the_push():
    """Concurrent runs share one repo, so every clone goes stale.

    A run cloned at 14:56 and finished at 20:10; four other commits landed on
    main in between, so its push was rejected as a non-fast-forward. It
    reported the work "committed on main", which was true, while nothing had
    left the pod -- and the entrypoints a downstream factory needed were
    invisible.
    """

    memory = _instructions._project_memory(browser_enabled=False)

    assert "git rebase origin/main" in memory
    assert "non-fast-forward" in memory
    # And a way to tell a successful push from a successful commit.
    assert "@{u}..HEAD" in memory
    # Conflicts will be in another project's directory; do not clobber it.
    assert "do not resolve by overwriting someone else's work" in memory


def test_project_memory_includes_the_console_handover_when_a_token_exists():
    """A project that needs the console was blocked by construction.

    One spent three hours reporting "the console is signed out" and refusing to
    invent a gateway host, because every word of console-auth guidance had been
    written into the demo memory and none into this one. Same mechanism for
    both: a 0600 file whose path is safe to echo.
    """
    import pathlib

    token = pathlib.Path("/home/flyte/.console-token")
    memory = _instructions._project_memory(browser_enabled=True, console_token=token)

    assert str(token) in memory
    assert "readFileSync" in memory
    assert "page.context().route(" in memory
    assert "never paste it into the snippet" in memory


def test_project_memory_omits_console_guidance_without_a_token():
    """No credential in the pod means the section would only mislead."""

    memory = _instructions._project_memory(browser_enabled=True, console_token=None)
    assert "readFileSync" not in memory


def test_every_row_also_reaches_the_task_log(capsys):
    """The report cannot be read from the CLI; the task log can."""
    timeline = _timeline._Timeline()
    timeline.row("📝", "tests are green")
    timeline.row("⚠️", "reviewer could not run")
    timeline.row("💥", "the inbox loop stopped")
    out, err = capsys.readouterr()
    assert "[timeline]" in out and "📝 tests are green" in out
    assert "reviewer could not run" in err and "[WARN]" in err
    assert "the inbox loop stopped" in err and "[ERROR]" in err
    assert "tests are green" not in err, "routine rows must not look like failures"


def test_status_changes_are_logged_once_not_every_pass(capsys):
    timeline = _timeline._Timeline()
    for _ in range(5):
        timeline.activity("working")
    timeline.activity("idle for 3m")
    out = capsys.readouterr().out
    assert out.count("status: working") == 1
    assert out.count("status: idle for 3m") == 1


def test_a_long_row_is_bounded_in_the_log(capsys):
    _timeline._Timeline().row("📝", "x" * 10_000)
    assert len(capsys.readouterr().out) < 2_200


def test_project_git_identity_overrides_inherited_generic_email(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(_session_config, "_project_name", "app")
    monkeypatch.setattr(_session_setup, "_install_deploy_key", lambda repo="": None)
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "agent-factory@localhost")
    _session_setup._export_runtime_env()
    assert os.environ["GIT_AUTHOR_EMAIL"] == "app@agent-factory.localhost"
    assert os.environ["GIT_COMMITTER_EMAIL"] == "app@agent-factory.localhost"


def _risks(timeline):
    return timeline.snapshot()["risks"]


def test_an_ordinary_router_decision_is_not_a_fallback():
    timeline = _timeline._Timeline()
    timeline.row("🔀", "router: planner -> codex (claude 5h 97%, codex 5h 12%; most headroom)")
    timeline.row(
        "🔀", "router: correctness -> codex (claude 5h 97%, codex 5h 12%; QA differs from builder claude)"
    )
    timeline.row("🔀", "router: delegate -> claude (pinned to claude)")
    timeline.row("🔀", "router: planner -> deferred (claude exhausted; retry at 01:50 UTC)")
    data = timeline.snapshot()
    assert data["cost"]["fallbacks"] == [] and data["cost"]["fallback_counts"] == {}
    assert not any("fallback" in risk for risk in _risks(timeline))


def test_a_refusal_retry_counts_once_and_groups_across_changing_readings():
    timeline = _timeline._Timeline()
    for pct in (12, 15, 18):
        timeline.row("🔀", f"router: planner -> codex (claude 5h 10%, codex 5h {pct}%; claude refused)")
    timeline.row("🔀", "router: planner -> codex (claude 5h 10%; pinned to codex)")
    data = timeline.snapshot()
    assert len(data["cost"]["fallbacks"]) == 3
    assert data["cost"]["fallback_counts"] == {"router: planner -> codex (claude refused)": 3}
    assert any("3 fallback(s)" in risk for risk in _risks(timeline))


def test_legacy_fallback_rows_still_count():
    timeline = _timeline._Timeline()
    timeline.row("🔀", "delegate: codex is at its limit (codex: 5h 96%); using claude")
    timeline.row("⚠️", "judge failed; retrying on opus")
    assert timeline.snapshot()["cost"]["fallback_counts"] == {
        "delegate: codex is at its limit; using claude": 1,
        "judge failed; retrying on opus": 1,
    }


def test_legacy_colon_fallback_rows_still_count():
    timeline = _timeline._Timeline()
    timeline.row("🔀", "delegate: using claude")
    assert timeline.snapshot()["cost"]["fallback_counts"] == {"delegate: using claude": 1}


def test_pr_and_refusal_deferral_rows_are_not_fallbacks():
    timeline = _timeline._Timeline()
    timeline.row("🔀", "pull request https://github.com/example/repo/pull/1")
    timeline.row("🔀", "router: planner -> deferred (claude refused; codex exhausted)")
    assert timeline.snapshot()["cost"]["fallbacks"] == []
