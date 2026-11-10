"""Guidance must agree with the code it describes.

The memory text, the README and the code have disagreed repeatedly, and each
time a session found out by hitting it: a snippet using `require` where the
runtime is ESM, a package path derived from the run id, a check scanning
directories the brief never mentioned. These tests make the disagreement a
failing test instead of a lost afternoon.
"""

from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from flyte_agent_factory import _instructions, _session_config, _session_setup, _supervise_support, agent

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "flyte_agent_factory"


def _memories() -> dict[str, str]:
    """The project memory in every shape it is generated in."""
    token = Path("/home/x/.console-token")
    voice = Path("/home/x/.voice-model")
    return {
        "plain": _instructions._project_memory(False),
        "browser": _instructions._project_memory(True, console_token=token, voice_model=voice),
    }


@pytest.fixture(params=sorted(_memories()))
def memory(request) -> str:
    return _memories()[request.param]


def test_no_javascript_snippet_uses_require(memory):
    """`browser_run_code_unsafe` runs as an ES module, where `require` is undefined.

    A session lost a turn reporting its sandbox had no filesystem access when
    the snippet it was handed was simply wrong. Prose may *name* require to warn
    against it; only code fences are checked.
    """
    for block in re.findall(r"```(?:js|javascript)\n(.*?)```", memory, re.DOTALL):
        assert not re.search(r"\brequire\s*\(", block), block


def test_the_console_snippet_is_not_pasted_with_a_token(memory):
    """The MCP server echoes code back into the run log; a literal token leaks."""
    for block in re.findall(r"```(?:js|javascript)\n(.*?)```", memory, re.DOTALL):
        assert not re.search(r"Bearer ['\"][A-Za-z0-9_\-.]{20,}", block)


def test_every_command_the_memory_names_is_installed(memory, tmp_path):
    _session_setup._install_bridge_commands(tmp_path)
    installed = {p.name for p in tmp_path.iterdir()}
    named = set(re.findall(r"`(ask-human|flyte-note|end-project|ci-status)\b", memory))
    assert named, "the memory stopped mentioning the bridge commands at all"
    assert named <= installed, f"memory names commands that are not on PATH: {named - installed}"
    # And the reverse: a command we install but never explain is one nobody uses.
    assert installed <= set(re.findall(r"(ask-human|flyte-note|end-project|ci-status)", memory)), (
        f"installed but undocumented: "
        f"{installed - set(re.findall(r'(ask-human|flyte-note|end-project|ci-status)', memory))}"
    )


def test_the_round_limit_in_the_memory_is_the_one_enforced(memory):
    stated = {int(n) for n in re.findall(r"You get (\d+) rounds", memory)}
    assert stated == {_session_config._QA_MAX_ROUNDS}
    assert f"After {_session_config._QA_MAX_ROUNDS} failed rounds" in memory


def test_the_memory_tells_the_session_what_the_supervisor_will_actually_check(memory):
    """The DONE gate rejects on these; the session must be told before it trips them."""
    assert _session_config._ACCEPTANCE_FILE in memory
    assert "push" in memory.lower()
    assert "git rev-list --count @{u}..HEAD" in memory


def test_every_package_path_the_memory_names_exists(memory):
    for rel in set(re.findall(r"`(flyte_agent_factory/[\w/]+\.py)`", memory)):
        assert (PACKAGE.parent / rel).is_file(), f"memory points at {rel}, which does not exist"


def test_the_role_agents_the_memory_lists_are_shipped(memory):
    shipped = {p.stem for p in (PACKAGE / "_assets" / "agents").glob("*.md")}
    named = set(re.findall(r"`(scope|discover|prototype|shape|build|verify)`", memory))
    assert named <= shipped, named - shipped


def test_readme_documents_direct_commands():
    readme = (ROOT / "README.md").read_text()
    assert "python -m pytest tests -q" in readme
    assert "ruff check src tests ops" in readme
    assert not (ROOT / "Makefile").exists()
    assert not re.search(r"\bmake (?:test|lint|deploy|run)\b", readme)
    # The documented executables must resolve from a clean checkout: `uv run` only finds what the
    # project declares.
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    assert {"ruff", "pytest", "pytest-asyncio"} <= {re.split(r"[<>=!~ ]", d)[0] for d in dev}
    assert all(re.search(r"[<>=]", d) for d in dev), f"unpinned dev dependency in {dev}"


def test_readme_deploy_commands_carry_what_the_makefile_exported():
    readme = (ROOT / "README.md").read_text()
    assert (
        'export AGENT_FACTORY_SECRETS="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile '
        'agent_secrets)"' in readme
    )
    assert "self_login" not in readme and "ops/deploy_self.py" not in readme


def test_nothing_still_points_at_the_deleted_makefile():
    stale = re.compile(
        r"Makefile|`make[ `]|\bmake (?:target|test|lint|deploy|run)\b|make <target>", re.IGNORECASE
    )
    for name in ("README.md",):
        hits = stale.findall((ROOT / name).read_text())
        assert not hits, (name, hits)
    for path in (ROOT / "tests").glob("test_*.py"):
        if path.name == Path(__file__).name:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                text = node.name.replace("_", " ") + " " + (ast.get_docstring(node) or "")
                assert not stale.search(text), (path.name, node.name)


def test_readme_does_not_hardcode_a_test_count():
    """`# 64 tests` sat in the README long after the suite passed 200."""
    assert not re.search(r"\b\d+ tests\b", (ROOT / "README.md").read_text())


def test_documented_operation_commands_exist():
    for script in re.findall(r"python (ops/[\w_]+\.py)", (ROOT / "README.md").read_text()):
        assert (ROOT / script).is_file()


def test_the_nudge_names_the_command_that_ends_the_gate():
    assert "flyte-note" in _supervise_support._NUDGE and "DONE" in _supervise_support._NUDGE


def test_asking_guidance_defaults_to_resolving_not_handing_back():
    """Run after run came back to a human for direction it could have found itself.

    The attended text used to read "asking costs nothing. Prefer it over
    guessing", which is an instruction to hand back, and sessions followed it.
    There is no attended text any more: the one guidance is the unattended one.
    """
    import inspect

    assert not inspect.signature(_instructions._asking_guidance).parameters
    text = _instructions._asking_guidance()
    lowered = text.lower()
    assert "prefer it over guessing" not in lowered
    assert "asking costs nothing" not in lowered
    assert "answer it yourself" in lowered, "must try to resolve before asking"
    # The timeout-and-default pattern, and nobody watching.
    assert "Nobody is watching" in text
    assert "or none" not in text
    assert "ask-human --timeout <seconds>" in text
    assert "Default if no answer:" in text
    assert "flyte-note" in text and "carry on" in text
    # When blocking on a human is allowed at all.
    assert "irreversible" in lowered and "outside your remit" in lowered and "contradiction" in lowered


def test_profile_owner_reserved_extras_reach_the_session_guidance(monkeypatch):
    """The session classifies first, so a profile extra that only the judge saw did almost nothing."""
    from flyte_agent_factory import _profile

    text = (ROOT / "profiles" / "example.toml").read_text()
    text += '\n[judgement]\nowner_reserved = ["changes to the public API of the billing service"]\n'
    monkeypatch.setattr(_session_config, "_PROFILE", _profile.parse(text))
    assert "billing" in _instructions._asking_guidance()


def test_asking_guidance_has_the_session_decide_engineering_judgement():
    """Factory issue #118: engineering calls were escalated with bad options and a default for silence."""
    text = _instructions._asking_guidance()
    lowered = text.lower()
    assert "classify the question before you ask it" in lowered
    assert "owner-reserved" in lowered and "engineering judgement" in lowered
    assert "what is the right thing to do" in lowered
    assert 'flyte-note "DECISION:' in text
    assert "never ask for, or choose, a waiver" in lowered
    assert "fix the code or the check" in lowered
    assert "the factory's judge decides it, not the owner" in lowered
    assert "overrules a factory decision wins" in lowered
    assert "timeouts and defaults are for these questions only" in lowered
    assert "--withdraw" in text and "--supersedes" in text


def test_the_ask_cli_timeout_message_says_to_do_the_default():
    from flyte_agent_factory import _bridge

    source = Path(_bridge.__file__).read_text()
    assert "Do the default you stated" in source
    assert "proceed without it or ask again" not in source


def test_every_memory_mode_carries_the_asking_guidance(memory):
    assert "Default if no answer:" in memory


def test_no_shipped_guidance_points_at_the_old_root_brief():
    """qa: the role files still said `BRIEF.md` after the brief moved to `.claude-flyte/BRIEF.md`.

    In a shared repo the root `BRIEF.md` can be another project's brief.
    """
    import re

    assets = Path(agent.__file__).parent / "_assets"
    offenders = []
    for path in assets.rglob("*.md"):
        if path.is_relative_to(assets / "workspace_template" / "standards"):
            continue  # a verbatim copy of standards/, not guidance to a session
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(?<![/\w.-])BRIEF\.md", line):
                offenders.append(f"{path.relative_to(assets)}:{n}: {line.strip()}")
    assert not offenders, offenders


def test_narration_guidance_states_the_measured_cost():
    """A past demo: memory said 'a few seconds per line'; it took 5-35 minutes, and the
    session sat for an hour with the pod at 98% memory, planning around the wrong number."""
    note = _instructions._narration_note(Path("/tmp/voice"))
    assert "few seconds per line" not in note
    assert "5-8 minutes per line" in note and "child action" in note and "at most two" in note


def test_tracked_root_markdown_is_only_readme_and_agents():
    """One README; every other document lives under docs/."""
    tracked = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    root_docs = {p for p in tracked if "/" not in p}
    # AGENTS.md is allowed, not required: it is not tracked in this repo yet. CONTRIBUTING.md and SECURITY.md
    # are the community files GitHub shows a public repository's visitors (#110 asks for them at the root);
    # each is a short pointer to its page under docs/.
    assert "README.md" in root_docs
    assert root_docs <= {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md"}


_FLOOR_KEYWORDS = (
    "spending money",
    "credentials",
    "irreversible",
    "publishing",
    "merging outside",
    "product direction",
    "finished work",
    "kept for themself",
)


def test_the_docs_name_every_reserved_category():
    """The docs restate the floor by hand; one keyword per entry keeps them whole."""
    from flyte_agent_factory import _reserved

    assert len(_FLOOR_KEYWORDS) == len(_reserved.OWNER_RESERVED_FLOOR)
    for keyword, entry in zip(_FLOOR_KEYWORDS, _reserved.OWNER_RESERVED_FLOOR, strict=True):
        assert keyword in entry, f"{keyword!r} no longer matches the floor entry {entry!r}"
    text = " ".join((ROOT / "docs" / "judgement.md").read_text().lower().split())
    missing = [k for k in _FLOOR_KEYWORDS if k.lower() not in text]
    assert not missing, f"judgement.md does not name: {missing}"
