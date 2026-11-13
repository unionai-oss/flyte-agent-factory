"""The tools step names one install command per missing tool, and later steps wait on it."""

from __future__ import annotations

import dataclasses

import pytest

from flyte_agent_factory import _bootstrap
from flyte_agent_factory._bootstrap_base import Context
from tests._bootstrap_fakes import CLAUDE_TOKEN, World, make_context

ARGS = [
    "--endpoint", "flyte.example.test", "--project", "my-agents",
    "--workspace", "pat/agent-workspace", "--state-uri", "s3://example-bucket/state",
]  # fmt: skip


@pytest.fixture(autouse=True)
def person(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def run(world, *extra, missing=(), platform="linux", signed_in=True):
    out: list[str] = []

    def make(settings):
        ctx = make_context(world, settings, out, CLAUDE_TOKEN)
        ctx.which = lambda name: None if name in missing else f"/usr/bin/{name}"
        ctx.platform = platform
        if not signed_in:
            ctx.github.authenticated = lambda: False  # type: ignore[method-assign]
        return ctx

    code = _bootstrap.main([*ARGS, "--dir", str(world.root / "clone"), *extra], make_context=make)
    return code, out


def line(out, step="tools"):
    return next(entry for entry in out if entry.startswith("[") and f"] {step}:" in entry)


def test_only_the_missing_tool_gets_an_install_line(world):
    code, out = run(world, missing=("gh",))
    text = line(out)
    assert code == 3 and text.startswith("[blocked] tools:")
    assert "gh:" in text and "`sudo apt-get install -y gh`" in text and "install_linux.md" in text
    for other in ("git:", "uv:", "ssh-keygen:", "astral.sh"):
        assert other not in text


def test_status_with_a_missing_tool_prints_its_install_line(world):
    _, out = run(world, "--status", missing=("gh",))
    _, real = run(world, missing=("gh",))
    assert "`sudo apt-get install -y gh`" in line(out)
    assert "install_linux.md" in line(out)
    assert line(out).split("tools: ", 1)[1].startswith("not on PATH: gh")
    assert "`sudo apt-get install -y gh`" in line(real)


def test_a_missing_ssh_keygen_is_named(world):
    _, out = run(world, missing=("ssh-keygen",))
    assert "ssh-keygen: `sudo apt-get install -y openssh-client`" in line(out)


def test_each_platform_gets_its_own_command(world):
    assert "`brew install gh`" in line(run(world, missing=("gh",), platform="darwin")[1])
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in line(run(world, missing=("uv",))[1])
    assert "https://docs.astral.sh/uv" in line(run(world, missing=("uv",), platform="freebsd")[1])


def test_several_missing_tools_each_get_one_line(world):
    _, out = run(world, missing=("git", "uv"))
    text = line(out)
    assert "git: `sudo apt-get install -y git`" in text and "uv:" in text and "gh:" not in text


def test_dry_run_says_what_the_step_would_ask(world):
    _, out = run(world, "--dry-run", missing=("gh",))
    assert "would ask you to install gh:" in line(out)


def test_gh_not_signed_in_is_planned_as_gh_auth_login(world):
    _, out = run(world, "--dry-run", signed_in=False)
    text = line(out)
    assert "would ask you to run `gh auth login`" in text and "install" not in text


def test_gh_not_signed_in_blocks_a_real_run_with_the_same_word_as_status(world):
    _, real = run(world, signed_in=False)
    _, status = run(world, "--status", signed_in=False)
    assert line(real).startswith("[blocked] tools:") and line(status).startswith("[blocked] tools:")


@pytest.mark.parametrize("extra", [(), ("--status",), ("--dry-run",)])
def test_later_steps_wait_instead_of_failing_on_a_missing_tool(world, extra):
    code, out = run(world, *extra, missing=("gh",))
    text = "\n".join(out)
    assert code == 3 and "FileNotFoundError" not in text
    assert "wait on it" in text
    assert "] workspace:" not in text


def test_the_context_default_platform_is_the_running_one():
    import sys

    field = {f.name: f for f in dataclasses.fields(Context)}["platform"]
    assert field.default == sys.platform


def test_status_with_gh_not_signed_in_prints_the_sign_in_command(world):
    _, out = run(world, "--status", signed_in=False)
    assert "gh auth login" in line(out) and "gh auth login" in out[-1]
