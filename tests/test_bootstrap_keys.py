"""`--chat-key` and `--deploy-key`, against fakes: the grant, the hand-over, and that no secret is printed."""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError

from flyte_agent_factory import _bootstrap, _bootstrap_flyte
from flyte_agent_factory._bootstrap_base import (
    API_KEY_REQUIREMENT,
    AccessDenied,
    BootstrapError,
    KeyExists,
    Outcome,
    Settings,
)
from flyte_agent_factory._bootstrap_flyte import FlyteSdk
from flyte_agent_factory._bootstrap_keys import build_command
from tests import _bootstrap_fakes as fakes
from tests._bootstrap_fakes import KEY_SECRET, World, make_context
from tests.test_bootstrap import person  # noqa: F401 -- autouse fixture: a git identity and a home per test

ARGS = [
    "--endpoint", "flyte.example.test", "--project", "my-agents", "--domain", "staging",
    "--workspace", "pat/agent-workspace", "--state-uri", "s3://example-bucket/state",
]  # fmt: skip
CONNECTOR = "https://factory-mcp.example.test/flyte-mcp/mcp"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    w.mcp_url = "https://factory-mcp.example.test"
    (tmp_path / "flyte-config.yaml").write_text("admin:\n  endpoint: dns:///flyte.example.test\n")
    return w


def key_run(world, flag, capsys, *, claude=True, extra=()):
    """One key-mode run; return (exit code, printed lines). Asserts the key reached no stdout or stderr."""
    out: list[str] = []

    def make(settings):
        ctx = make_context(world, settings, out)
        if not claude:
            ctx.which = lambda name: None
        return ctx

    code = _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), flag, *extra], make_context=make)
    seen = capsys.readouterr()
    assert KEY_SECRET not in "\n".join(out) + seen.out + seen.err
    return code, out


def test_chat_key_with_claude_code_grants_the_key_and_adds_the_connector_without_printing_it(world, capsys):
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 0, out
    assert world.policies == {"factory-chat": [("contributor", "my-agents", "staging")]}
    assert world.assignments == [("client-factory-chat", "factory-chat")]
    [add] = [c for c in world.commands if c[:3] == ["claude", "mcp", "add"]]
    assert add[3:8] == ["-s", "user", "--transport", "http", "agent-factory"] and add[8] == CONNECTOR
    assert add[-1] == f"Authorization: Bearer {KEY_SECRET}"
    assert world.tty == [], "the key goes to claude, not to the terminal"
    assert [line.split(":")[0] for line in out] == ["[done] chat-key"]


def test_a_rerun_with_the_connector_present_changes_nothing(world, capsys):
    assert key_run(world, "--chat-key", capsys)[0] == 0
    before = list(world.events)
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 0 and world.events == before
    assert "already" in out[0]


def test_chat_key_without_claude_shows_the_key_once_on_the_terminal_only(world, capsys):
    code, out = key_run(world, "--chat-key", capsys, claude=False)
    assert code == 0, out
    [shown] = world.tty
    assert KEY_SECRET in shown and CONNECTOR in shown and "never into a chat" in shown
    assert not [c for c in world.commands if c[:3] == ["claude", "mcp", "add"]]
    assert world.assignments == [("client-factory-chat", "factory-chat")]


def test_an_existing_chat_key_is_never_deleted_silently(world, capsys):
    world.union_keys["factory-chat"] = "client-old"
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[0].startswith("[blocked] chat-key")
    assert "delete it" in out[0] and "factory-chat" in out[0]
    assert world.union_keys == {"factory-chat": "client-old"} and world.assignments == []


def test_deploy_key_is_written_to_the_terminal_only(world, capsys):
    code, out = key_run(world, "--deploy-key", capsys)
    assert code == 0, out
    assert world.policies == {"factory-deploy": [("contributor", "my-agents", "staging")]}
    assert world.assignments == [("client-factory-deploy", "factory-deploy")]
    [shown] = world.tty
    assert KEY_SECRET in shown and "password manager" in shown and "_UNION_EAGER_API_KEY" in shown
    assert world.commands == []


def test_a_key_that_cannot_be_shown_is_deleted_rather_than_lost(world, capsys):
    world.tty = None
    code, out = key_run(world, "--deploy-key", capsys)
    assert code == 3 and "no terminal" in out[0]
    assert world.union_keys == {}


def test_a_failed_assignment_deletes_the_unused_key(world, capsys):
    world.assign_error = True
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[0].startswith("[blocked] chat-key") and "deleted" in out[0]
    assert world.union_keys == {} and world.tty == []


RUN_SETTINGS = Settings(
    endpoint="flyte.example.test",
    project="my-agents",
    domain="staging",
    workspace="pat/agent-workspace",
    state_uri="s3://example-bucket/state",
)


def assert_one_grant_command(out, policy, client_id):
    """The run's line ends with the admin's one command: this run's values, the key's client ID, no secret."""
    grant = build_command(RUN_SETTINGS, f"--grant {policy} {client_id}")
    assert "\n".join(out).count(grant) == 1, out
    assert_filled_in(grant)
    assert out[-1].endswith(grant)


def test_a_refused_assignment_keeps_the_shown_key_and_prints_the_admins_grant_command(world, capsys):
    world.assign_denied = True
    code, out = key_run(world, "--deploy-key", capsys)
    assert code == 3 and out[0].startswith("[todo] deploy-key"), out
    [shown] = world.tty
    assert KEY_SECRET in shown, "the person holds the key; only its grant is left to the admin"
    assert world.union_keys == {"factory-deploy": "client-factory-deploy"} and world.assignments == []
    assert_one_grant_command(out, "factory-deploy", "client-factory-deploy")


def test_a_refused_policy_still_connects_the_key_and_prints_the_admins_grant_command(world, capsys):
    world.policy_denied = True
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[0].startswith("[todo] chat-key"), out
    [add] = [c for c in world.commands if c[:3] == ["claude", "mcp", "add"]]
    assert add[-1] == f"Authorization: Bearer {KEY_SECRET}"
    assert world.union_keys == {"factory-chat": "client-factory-chat"}
    assert world.policies == {} and world.assignments == []
    assert_one_grant_command(out, "factory-chat", "client-factory-chat")


def test_a_refused_grant_with_no_terminal_deletes_the_key_and_prints_no_grant_command(world, capsys):
    world.policy_denied, world.tty = True, None
    code, out = key_run(world, "--deploy-key", capsys)
    assert code == 3 and "no terminal" in out[0]
    assert world.union_keys == {} and "--grant" not in "\n".join(out)


def test_the_admins_grant_command_grants_that_key_and_makes_none(world, capsys):
    out: list[str] = []
    argv = [*ARGS, "--grant", "factory-deploy", "client-factory-deploy"]
    code = _bootstrap.main(argv, make_context=lambda settings: make_context(world, settings, out))
    assert code == 0, out
    assert out[0].startswith("[done] grant") and "client-factory-deploy" in out[0]
    assert world.policies == {"factory-deploy": [("contributor", "my-agents", "staging")]}
    assert world.assignments == [("client-factory-deploy", "factory-deploy")]
    assert world.union_keys == {} and world.tty == [] and world.commands == []


def test_the_grant_takes_only_the_factorys_policies_and_no_other_mode(capsys):
    for bad in (
        ["--grant", "contributor", "client-1"],
        ["--grant", "factory-chat", "client-1", "--deploy-key"],
        ["--grant", "factory-chat", "client-1", "--dry-run"],
    ):
        with pytest.raises(SystemExit):
            _bootstrap.main([*ARGS, *bad])


def test_a_claude_mcp_add_that_times_out_prints_no_key_and_deletes_it(world, capsys):
    """`TimeoutExpired`'s text is the command line, which carries the header: it must never be printed."""
    out: list[str] = []

    def make(settings):
        ctx = make_context(world, settings, out)
        listing = ctx.run_command

        def run_command(argv):
            if list(argv[:3]) == ["claude", "mcp", "add"]:
                raise subprocess.TimeoutExpired(list(argv), 60)
            return listing(argv)

        ctx.run_command = run_command
        return ctx

    code = _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), "--chat-key"], make_context=make)
    seen = capsys.readouterr()
    assert KEY_SECRET not in "\n".join(out) + seen.out + seen.err
    assert code == 3 and out[0].startswith("[blocked] chat-key") and "TimeoutExpired" in out[0]
    assert world.union_keys == {}, "the key that never reached the client is deleted"


def test_a_claude_mcp_add_that_exits_nonzero_prints_no_key_and_deletes_it(world, capsys, monkeypatch):
    def failing(argv):
        if list(argv[:3]) == ["claude", "mcp", "add"]:
            return subprocess.CompletedProcess(argv, 2, f"header {KEY_SECRET}", f"bad {KEY_SECRET}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(fakes, "_run_command", lambda w, argv: failing(argv))
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[0].startswith("[blocked] chat-key") and "(2)" in out[0] and "deleted" in out[0]
    assert world.union_keys == {}, "the key that never reached the client is deleted"


def test_chat_key_with_no_public_url_asks_for_the_plain_bootstrap_first(world, capsys):
    world.mcp_url = None
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[0].startswith("[todo] chat-key") and "no public URL" in out[0]
    assert world.union_keys == {} and world.commands == []


def test_chat_key_with_neither_claude_nor_a_terminal_deletes_the_key(world, capsys):
    world.tty = None
    code, out = key_run(world, "--chat-key", capsys, claude=False)
    assert code == 3 and "no terminal" in out[0]
    assert world.union_keys == {}


def test_a_refused_grant_of_the_chat_key_shown_on_the_terminal_prints_the_admins_command(world, capsys):
    world.assign_denied = True
    code, out = key_run(world, "--chat-key", capsys, claude=False)
    assert code == 3 and out[0].startswith("[todo] chat-key"), out
    [shown] = world.tty
    assert KEY_SECRET in shown and "claude.ai" in shown
    assert world.union_keys == {"factory-chat": "client-factory-chat"} and world.assignments == []
    assert_one_grant_command(out, "factory-chat", "client-factory-chat")


def test_an_existing_deploy_key_is_never_deleted_silently(world, capsys):
    world.union_keys["factory-deploy"] = "client-old"
    code, out = key_run(world, "--deploy-key", capsys)
    assert code == 3 and out[0].startswith("[blocked] deploy-key") and "already exists" in out[0]
    assert world.union_keys == {"factory-deploy": "client-old"} and world.tty == []


def test_an_unexpected_failure_while_assigning_deletes_the_key_and_propagates(world, capsys, monkeypatch):
    def boom(self, client_id, policy):
        raise KeyError("a bug, not a Union answer")

    monkeypatch.setattr(fakes.FakeFlyte, "assign_policy", boom)
    with pytest.raises(KeyError):
        key_run(world, "--chat-key", capsys)
    assert world.union_keys == {} and world.tty == []


def test_a_key_that_cannot_be_deleted_is_named_for_deleting_by_hand(world, capsys, monkeypatch):
    def refuse(self, client_id):
        raise BootstrapError("deleting failed")

    world.assign_error = True
    monkeypatch.setattr(fakes.FakeFlyte, "delete_api_key", refuse)
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and out[-1].startswith("[blocked] chat-key")
    assert any("could not delete the unused API key 'client-factory-chat'" in line for line in out)
    assert any("delete it by hand" in line for line in out)


def test_a_step_that_raises_a_subprocess_error_prints_its_class_not_its_command_line():
    def call():
        raise subprocess.TimeoutExpired(["tool", "--header", f"Authorization: Bearer {KEY_SECRET}"], 60)

    outcome = _bootstrap._guard("step", call)
    assert isinstance(outcome, Outcome)
    assert KEY_SECRET not in outcome.detail and "TimeoutExpired" in outcome.detail


def test_a_broader_existing_policy_stops_before_a_key_is_made(world, capsys):
    world.policies["factory-chat"] = [("admin", "my-agents", "staging")]
    code, out = key_run(world, "--chat-key", capsys)
    assert code == 3 and "does not bind exactly" in out[0]
    assert world.union_keys == {}


@pytest.mark.parametrize("flag", [["--chat-key"], ["--deploy-key"], ["--grant", "factory-chat", "u"]])
def test_without_flyteplugins_union_it_says_why_and_changes_nothing(world, capsys, flag):
    world.can_manage_keys = False
    code, out = key_run(world, flag[0], capsys, extra=flag[1:])
    assert code == 3 and API_KEY_REQUIREMENT in out[0] and "Linux x86_64" in out[0]
    assert world.events == []


@pytest.mark.parametrize("flag", [["--grant", "factory-chat", "client-1"], ["--deploy-key"], ["--chat-key"]])
def test_a_key_mode_refuses_a_config_for_another_endpoint_before_any_union_call(world, capsys, flag):
    (world.root / "flyte-config.yaml").write_text("admin:\n  endpoint: dns:///other-org.example.org\n")
    code, out = key_run(world, flag[0], capsys, extra=flag[1:])
    assert code == 3 and out[0].startswith("[blocked]") and "other-org.example.org" in out[0]
    assert world.events == [] and world.policies == {} and world.union_keys == {}


@pytest.mark.parametrize("flag", [["--grant", "factory-chat", "client-1"], ["--deploy-key"], ["--chat-key"]])
def test_a_key_mode_without_a_flyte_config_signs_in_then_goes_on(world, capsys, flag):
    (world.root / "flyte-config.yaml").unlink()
    code, out = key_run(world, flag[0], capsys, extra=flag[1:])
    assert code == 0, out
    assert world.events[:2] == ["create_config", "sign_in"]
    assert "sign in first" not in "\n".join(out)


def test_the_key_modes_are_listed_in_help_and_exclude_each_other(capsys):
    with pytest.raises(SystemExit):
        _bootstrap.main(["--help"])
    help_text = capsys.readouterr().out
    assert "--chat-key" in help_text and "--deploy-key" in help_text
    for bad in (["--chat-key", "--deploy-key"], ["--chat-key", "--dry-run"], ["--deploy-key", "--status"]):
        with pytest.raises(SystemExit):
            _bootstrap.main([*ARGS, *bad])


@pytest.mark.parametrize("flag", [["--chat-key"], ["--deploy-key"], ["--grant", "factory-chat", "u"]])
def test_the_key_modes_refuse_an_oss_backend(capsys, flag):
    # Keys and policies are Union's; on OSS Flyte the MCP is authenticated another way (oss-flyte-compat.md).
    with pytest.raises(SystemExit) as exc:
        _bootstrap.main([*ARGS, "--backend", "oss", *flag])
    assert exc.value.code == 2 and "Union API keys" in capsys.readouterr().err


def command_in(lines, flag):
    # The step's line, and the `next:` line that repeats it.
    command, *repeats = re.findall(rf"d=~/.*? {flag}\b", "\n".join(lines))
    assert set(repeats) <= {command}
    return command


def assert_filled_in(command):
    for value in (
        "flyte.example.test",
        "my-agents",
        "staging",
        "pat/agent-workspace",
        "s3://example-bucket/state",
    ):
        assert value in command
    assert not re.search(r"<[^>]*>", command) and "client_id=" not in command


def test_the_printed_commands_carry_the_runs_own_values_and_no_placeholders(world):
    from tests.test_bootstrap import run as full_run

    world.api_key = False
    code, out = full_run(world, "--domain", "staging")
    assert code == 3
    assert_filled_in(command_in(out, "--deploy-key"))
    world.api_key = True
    code, out = full_run(world, "--domain", "staging")
    assert code == 0, out
    chat = command_in(out, "--chat-key")
    assert_filled_in(chat)
    assert "Bearer" not in "\n".join(out)


def test_the_command_is_quoted_for_the_shell():
    settings = Settings(endpoint="h", project="p q", workspace="o/r", state_uri="s3://b/a b")
    command = build_command(settings, "--chat-key")
    assert "--project 'p q'" in command and "--state-uri 's3://b/a b'" in command


def _binding_pb2(binding: dict[str, Any]) -> SimpleNamespace:
    """A stand-in `PolicyBinding` protobuf: the role, and a project resource (oneof `resource`) if given."""
    resource = binding.get("resource", {})
    kind = "project" if "project" in resource else None
    project = SimpleNamespace(
        name=resource.get("project", ""), domain=SimpleNamespace(name=resource.get("domain", ""))
    )
    return SimpleNamespace(
        role_id=SimpleNamespace(name=binding["role"]),
        resource=SimpleNamespace(
            WhichOneof=lambda oneof: kind if oneof == "resource" else None, project=project
        ),
    )


class _Remote:
    """Stands in for `flyteplugins.union.remote`: the calls the SDK adapter makes, recorded."""

    def __init__(self, existing: list[dict[str, Any]] | None = None) -> None:
        self.existing = existing
        self.calls: list[tuple[Any, ...]] = []
        outer = self

        class Policy:
            @staticmethod
            def get(name):
                if outer.existing is None:
                    raise ConnectError(Code.NOT_FOUND, "no such policy")
                # Like the SDK's `Policy`, the bindings are read from its protobuf.
                bindings = [_binding_pb2(b) for b in outer.existing]
                return SimpleNamespace(pb2=SimpleNamespace(bindings=bindings))

            @staticmethod
            def create(name, bindings):
                outer.calls.append(("create", name, bindings))

            @staticmethod
            def update(name, old, new):
                outer.calls.append(("update", name, old, new))

        class Assignment:
            @staticmethod
            def create(creds_subject, policy):
                outer.calls.append(("assign", creds_subject, policy))

        self.Policy, self.Assignment = Policy, Assignment


def sdk_with(monkeypatch, remote):
    sdk = FlyteSdk(Settings(endpoint="e", project="my-agents", workspace="o/r", state_uri="s3://b/a"))
    sdk._ready = True
    monkeypatch.setattr(_bootstrap_flyte, "_load_union_remote", lambda: remote)
    return sdk


BINDING: dict[str, Any] = {
    "role": "contributor",
    "resource": {"project": "my-agents", "domain": "development"},
}


def test_the_sdk_adapter_creates_a_missing_policy_scoped_to_the_project_and_domain(monkeypatch):
    remote = _Remote()
    sdk_with(monkeypatch, remote).ensure_policy("factory-chat")
    assert remote.calls == [("create", "factory-chat", [BINDING])]


def test_the_sdk_adapter_adds_the_binding_to_an_empty_policy_and_accepts_a_matching_one(monkeypatch):
    remote = _Remote(existing=[])
    sdk_with(monkeypatch, remote).ensure_policy("factory-chat")
    assert remote.calls == [("update", "factory-chat", [], [BINDING])]
    # What the SDK reads back also names the organization on the resource.
    matching = _Remote(
        existing=[{"role": "contributor", "resource": {**BINDING["resource"], "organization": "o"}}]
    )
    sdk_with(monkeypatch, matching).ensure_policy("factory-chat")
    assert matching.calls == []


def test_the_sdk_adapter_accepts_the_binding_it_made_as_the_plugin_reads_it_back(monkeypatch):
    """A rerun finds the policy an earlier run made: read back through the real protobuf, it must match."""
    # The plugin is a Linux dependency, as are the key modes this tests; CI runs on Linux.
    from flyteplugins.union.remote import _policy  # type: ignore[import-untyped]

    made = _policy._dict_to_binding(BINDING, "org")
    # `Policy.bindings` would give {"role": ..., "resource": {"project": "my-agents"}}: no domain.
    remote = _Remote(existing=[])
    monkeypatch.setattr(
        remote.Policy, "get", staticmethod(lambda name: SimpleNamespace(pb2=SimpleNamespace(bindings=[made])))
    )
    sdk_with(monkeypatch, remote).ensure_policy("factory-chat")
    assert remote.calls == []


@pytest.mark.parametrize(
    "held",
    [
        [{"role": "admin", "resource": BINDING["resource"]}],
        [{"role": "contributor", "resource": {"project": "other", "domain": "development"}}],
        [BINDING, {"role": "contributor"}],
    ],
)
def test_the_sdk_adapter_refuses_a_policy_with_other_bindings(monkeypatch, held):
    remote = _Remote(existing=held)
    with pytest.raises(BootstrapError, match="does not bind exactly"):
        sdk_with(monkeypatch, remote).ensure_policy("factory-chat")
    assert remote.calls == []


def test_the_sdk_adapter_assigns_by_the_keys_client_id(monkeypatch):
    remote = _Remote()
    sdk_with(monkeypatch, remote).assign_policy("client-1", "factory-chat")
    assert remote.calls == [("assign", "client-1", "factory-chat")]


def _denied(*args, **kwargs):
    raise ConnectError(Code.PERMISSION_DENIED, "permission denied")


@pytest.mark.parametrize("call", ["get", "create"])
def test_the_sdk_adapter_reports_a_refused_policy_as_access_denied(monkeypatch, call):
    remote = _Remote(existing=None if call == "create" else [])
    monkeypatch.setattr(remote.Policy, call, staticmethod(_denied))
    with pytest.raises(AccessDenied, match="factory-chat"):
        sdk_with(monkeypatch, remote).ensure_policy("factory-chat")


def test_the_sdk_adapter_reports_a_refused_assignment_and_accepts_an_existing_one(monkeypatch):
    remote = _Remote()
    monkeypatch.setattr(remote.Assignment, "create", staticmethod(_denied))
    with pytest.raises(AccessDenied, match="factory-chat"):
        sdk_with(monkeypatch, remote).assign_policy("client-1", "factory-chat")

    def exists(*args, **kwargs):
        raise ConnectError(Code.ALREADY_EXISTS, "assignment exists")

    monkeypatch.setattr(remote.Assignment, "create", staticmethod(exists))
    sdk_with(monkeypatch, remote).assign_policy("client-1", "factory-chat")  # an admin's rerun is done


def test_the_sdk_adapter_makes_keys_without_default_policies_and_reports_a_name_clash(monkeypatch):
    seen = {}

    class ApiKey:
        @staticmethod
        def create(name, skip_default_policy_assignments):
            seen[name] = skip_default_policy_assignments
            if name == "taken":
                raise ConnectError(Code.ALREADY_EXISTS, "client exists")
            return SimpleNamespace(client_id="c1", encoded_credentials=KEY_SECRET)

    remote = _Remote()
    remote.ApiKey = ApiKey  # type: ignore[attr-defined]
    sdk = sdk_with(monkeypatch, remote)
    key = sdk.create_api_key("factory-chat")
    assert key.client_id == "c1" and KEY_SECRET not in repr(key) and seen == {"factory-chat": True}
    with pytest.raises(KeyExists):
        sdk.create_api_key("taken")
