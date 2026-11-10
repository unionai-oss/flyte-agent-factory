"""The helper that lets the session submit child actions of its own run."""

import pytest

from flyte_agent_factory import _instructions, child


@pytest.fixture(autouse=True)
def _no_process_sink(monkeypatch):
    """`attach` installs a process-wide sink; one test's must not outlive it."""
    monkeypatch.setattr(child, "_process_sink", None)


def test_context_path_is_the_well_known_location(tmp_path):
    assert child.context_path(tmp_path) == tmp_path / ".flyte" / "run_context.json"


def test_write_context_refuses_outside_a_task(monkeypatch, tmp_path):
    """It assembles the context from the live task, so there must be one."""
    import flyte

    monkeypatch.setattr(flyte, "ctx", lambda: None)
    with pytest.raises(RuntimeError, match="inside a task"):
        child.write_context(tmp_path)


def test_write_context_refuses_the_sdks_null_context(tmp_path):
    """Outside a task `flyte.ctx()` is a falsy null context, not None; it must not pass for a task."""
    import flyte

    assert not flyte.ctx()
    with pytest.raises(RuntimeError, match="inside a task"):
        child.write_context(tmp_path)


def test_attach_creates_a_controller_when_there_is_none(monkeypatch, tmp_path):
    """`load_interactive_ctx` installs a context but no controller.

    It was written for debuggers, which only read. Submitting a child action
    goes through a controller, so `attach` has to create one.
    """
    import flyte
    from flyte._internal import controllers

    created = []
    monkeypatch.setattr(flyte, "load_interactive_ctx", lambda path=None: "ctx")

    def no_controller():
        raise RuntimeError("Controller is not initialized")

    monkeypatch.setattr(controllers, "get_controller", no_controller)
    monkeypatch.setattr(controllers, "create_controller", lambda ct, **kw: created.append(ct))

    assert child.attach(tmp_path) == "ctx"  # type: ignore[comparison-overlap]  # the fake context is a str
    assert created == ["rust"], "submission needs the remote controller"


def test_attach_reuses_an_existing_controller(monkeypatch, tmp_path):
    import flyte
    from flyte._internal import controllers

    created = []
    monkeypatch.setattr(flyte, "load_interactive_ctx", lambda path=None: "ctx")
    monkeypatch.setattr(controllers, "get_controller", lambda: "existing")
    monkeypatch.setattr(controllers, "create_controller", lambda ct, **kw: created.append(ct))

    child.attach(tmp_path)
    assert created == [], "a second attach must not build another controller"


def test_module_documents_the_image_constraint():
    """The one thing that actually bites: a bare image name has no registry
    inside a pod, so the child submits fine and then cannot pull."""
    assert "already exist" in (child.__doc__ or "")
    assert "sandbox.create" in (child.__doc__ or "")


# --- ownership record and the abort helper ------------------------------------------------------


@pytest.fixture
def caller(monkeypatch, tmp_path):
    """Run a session in root action a0 that has recorded two children, with the abort call captured."""
    from types import SimpleNamespace

    import flyte
    import flyte.remote as remote

    monkeypatch.setattr(
        flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(name="a0", run_name="run-1"))
    )
    aborted = []

    def get(run_name, name):
        def abort(reason):
            aborted.append((run_name, name, reason))

        return SimpleNamespace(abort=abort)

    monkeypatch.setattr(remote.Action, "get", get)
    child.record_child(tmp_path, "a0", "ab12")
    child.record_child(tmp_path, "a0", "cd34")
    return SimpleNamespace(base=tmp_path, aborted=aborted)


def test_abort_children_aborts_only_the_exact_recorded_names(caller):
    """Only the exact recorded child names are aborted, under the run that recorded them."""
    child.abort_children(["ab12"], "orphaned", base_dir=caller.base)
    assert caller.aborted == [("run-1", "ab12", "orphaned")]


@pytest.mark.parametrize(
    "bad",
    [
        "a0",  # the root: aborting it kills the whole project
        "zz99",  # not started by this caller
        "ab*",
        "a?12",
        "ab12,cd34",
        "ab 12",
        "",
        "../a0",
    ],
)
def test_abort_children_refuses_and_aborts_nothing(caller, bad):
    """One bad name refuses the whole batch before any abort call."""
    with pytest.raises(child.AbortRefused):
        child.abort_children(["ab12", bad], "nope", base_dir=caller.base)
    assert caller.aborted == [], "one bad name must stop the whole batch"


def test_abort_children_refuses_the_callers_own_action(monkeypatch, tmp_path):
    """A sub-agent is a child of a0 and may record names, but never aborts the action it runs in."""
    from types import SimpleNamespace

    import flyte

    monkeypatch.setattr(
        flyte, "ctx", lambda: SimpleNamespace(action=SimpleNamespace(name="sub1", run_name="run-1"))
    )
    child.record_child(tmp_path, "sub1", "sub1")
    with pytest.raises(child.AbortRefused, match="own action"):
        child.abort_children(["sub1"], "x", base_dir=tmp_path)


def test_abort_children_refuses_another_agents_children(caller, tmp_path):
    """The record is keyed by caller: a0's children are not a sub-agent's to abort."""
    child.record_child(caller.base, "other", "ef56")
    with pytest.raises(child.AbortRefused, match="not started by this action"):
        child.abort_children(["ef56"], "x", base_dir=caller.base)
    assert caller.aborted == []


def test_abort_children_needs_at_least_one_name(caller):
    """An empty name list is refused rather than aborting nothing silently."""
    with pytest.raises(child.AbortRefused):
        child.abort_children([], "x", base_dir=caller.base)


def test_cli_abort_reports_refusal_and_exits_nonzero(caller, capsys, monkeypatch):
    """The CLI prints a refusal on stderr and exits 1."""
    monkeypatch.setattr(child, "attach", lambda base_dir=None: None)
    child.context_path(caller.base).write_text("{}")  # where the supervisor writes it; attach is faked
    assert child.main(["abort", "a0"], base_dir=caller.base) == 1
    assert "a0" in capsys.readouterr().err
    assert caller.aborted == []
    assert child.main(["abort", "--reason", "mine", "ab12"], base_dir=caller.base) == 0
    assert caller.aborted == [("run-1", "ab12", "mine")]


def test_cli_abort_outside_the_workspace_refuses_cleanly(tmp_path, capsys, monkeypatch):
    """No context file where it runs: a plain refusal that says where to run it, not a traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_WORKSPACE", "/work/space")
    assert child.main(["abort", "ab12"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("refused, nothing aborted: no run context here")
    assert "run from /work/space" in err
    assert child.main(["abort", "ab12"], base_dir=tmp_path / "elsewhere") == 1


def test_record_child_is_idempotent_and_survives_a_second_process(tmp_path):
    """Recording a child twice keeps one entry, and a fresh reader sees it."""
    child.record_child(tmp_path, "a0", "ab12")
    child.record_child(tmp_path, "a0", "ab12")
    assert child.recorded_children(tmp_path, "a0") == ["ab12"]
    assert child.recorded_children(tmp_path, "nobody") == []


def test_the_sdk_hook_records_every_action_name_it_generates(monkeypatch, tmp_path):
    """The hook is on the function every controller calls to name a child, so nothing submitted escapes it."""
    from types import SimpleNamespace

    from flyte._internal.runtime import convert

    monkeypatch.setattr(
        convert,
        "generate_sub_action_id_and_output_path",
        lambda tctx, task_identity, inputs_hash, invoke_seq: (SimpleNamespace(name="kid1"), "s3://out"),
    )
    seen: list[str] = []
    with child.track_submissions(seen.append):
        result = convert.generate_sub_action_id_and_output_path(None, "t", "h", 1)  # type: ignore[arg-type]  # patched; task context unused
    assert result[1] == "s3://out", "the hook must hand the SDK's own answer back untouched"
    assert seen == ["kid1"]
    convert.generate_sub_action_id_and_output_path(None, "t", "h", 2)  # type: ignore[arg-type]  # patched; task context unused
    assert seen == ["kid1"], "outside the scope nothing is recorded"


def test_attach_records_what_is_submitted_afterwards(monkeypatch, tmp_path):
    """`attach` records each action submitted after it as a child of the root action."""
    from types import SimpleNamespace

    import flyte
    from flyte._internal import controllers

    monkeypatch.setattr(
        flyte,
        "load_interactive_ctx",
        lambda path=None: SimpleNamespace(action=SimpleNamespace(name="a0", run_name="run-1")),
    )
    monkeypatch.setattr(controllers, "get_controller", lambda: "existing")
    child.attach(tmp_path)
    child.notify_submitted("kid9")
    assert child.recorded_children(tmp_path, "a0") == ["kid9"]


def test_the_shell_cannot_abort_by_listing_or_name_a0():
    """Best effort (the Python client is reachable some other way), but the known shapes are denied."""
    from flyte_agent_factory import _auth

    for rule in (
        "Bash(*Action.abort*)",
        "Bash(*abort*listall*)",
        "Bash(*abort* a0 *)",
        "Bash(flyte abort:*)",
    ):
        assert rule in _auth.DENIED_TOOLS


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("python -m flyte_agent_factory.child abort a0", True),
        ("python -m flyte_agent_factory.child abort a0 --reason x", True),
        ("python -m flyte_agent_factory.child abort 'a0'", True),
        ('python -m flyte_agent_factory.child abort "a0"', True),
        # Child action names are random; one beginning with a0 is the session's own to stop.
        ("python -m flyte_agent_factory.child abort a0f3c9", False),
        ("python -m flyte_agent_factory.child abort a0f3c9 --reason done", False),
    ],
)
def test_the_a0_rules_match_a0_only_as_a_whole_word(command, denied):
    """Match like Claude Code: `*` is any run of characters, as in `fnmatchcase`.

    These rules use no other wildcard, so it stands in for the real matcher here.
    """
    from fnmatch import fnmatchcase

    from flyte_agent_factory import _auth

    a0_rules = [r[len("Bash(") : -1] for r in _auth.DENIED_TOOLS if "a0" in r]
    assert any(fnmatchcase(command, rule) for rule in a0_rules) is denied


def test_instructions_make_listing_read_only_and_point_at_the_helper():
    """The project memory says listing is read-only and names the abort helper."""

    memory = _instructions._project_memory(browser_enabled=False, name="proj")
    assert "**read-only**" in memory
    assert "python -m flyte_agent_factory.child abort" in memory
    assert "never choose what to abort by matching names" in memory
    assert "Aborting `a0` ends the whole project" in memory


def test_instructions_say_images_can_be_built_in_place_with_podman():
    """The project memory says images can be built in place with rootless podman."""

    memory = _instructions._project_memory(browser_enabled=False, name="proj")
    assert "podman build" in memory and "vfs" in memory and "chroot" in memory
