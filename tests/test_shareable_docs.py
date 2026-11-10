"""What a stranger needs to install the factory from the docs alone: the personal example profiles,
working links, and one secrets reference that names every secret-valued profile key."""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from flyte_agent_factory import _deploy, _profile
from ops import personal_literals

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_EXAMPLE = ROOT / "profiles" / "example-claude-personal.toml"
CODEX_EXAMPLE = ROOT / "profiles" / "example-codex-personal.toml"
SECRETS_DOC = ROOT / "docs" / "secrets.md"


# --- the personal example profiles -------------------------------------------------------------


def test_the_claude_personal_example_is_minimal_and_valid():
    profile = _profile.load(str(CLAUDE_EXAMPLE))
    assert profile.name == "claude-personal"
    assert 1 <= len(profile.slots) <= 2
    assert profile.secrets.claude_token and profile.secrets.deploy_key
    # Only the two secrets a personal plan needs.
    assert not (profile.secrets.codex_token or profile.secrets.gateway_key or profile.secrets.api_key)
    assert not profile.has_codex
    assert not profile.github.configured and not profile.deploy.auto
    assert not profile.repo_keys and profile.board is None and profile.bench is None


def test_the_codex_personal_example_runs_its_coordinator_on_a_chatgpt_login():
    profile = _profile.load(str(CODEX_EXAMPLE))
    assert profile.name == "codex-personal"
    assert not profile.secrets.codex_token
    # claude_token is required by the parser (every profile has one), so the example keeps it.
    assert profile.secrets.claude_token and profile.secrets.deploy_key
    [slot] = [s for s in profile.slots if s.codex_login]
    assert slot.codex_login.startswith("codex_login")
    assert slot.policy["COORDINATOR_ENGINE"] == "codex"
    assert not profile.github.configured and not profile.deploy.auto


@pytest.mark.parametrize("path", [CLAUDE_EXAMPLE, CODEX_EXAMPLE], ids=lambda p: p.name)
def test_a_personal_example_is_trackable_and_commented(path):
    ignored = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT, timeout=10).returncode == 0
    assert not ignored, f"{path.name} is git-ignored: it must be tracked"
    keys = [ln for ln in path.read_text().splitlines() if re.match(r"^\s*[A-Za-z_]+ = ", ln)]
    assert keys
    lines = path.read_text().splitlines()
    for number, line in enumerate(lines):
        if re.match(r"^\s*[A-Za-z_]+ = ", line):
            assert "#" in line or lines[number - 1].lstrip().startswith("#"), f"uncommented: {line.strip()}"


# --- links ----------------------------------------------------------------------------------------

_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^(```|~~~)")


def _markdown_files() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")), ROOT / "profiles" / "README.md"]


def _links(path: Path):
    in_fence = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if _FENCE.match(line.lstrip()):
            in_fence = not in_fence
        if in_fence:
            continue
        line = re.sub(r"`[^`]*`", "", line)  # inline code is not a link
        for match in _LINK.finditer(line):
            yield number, match.group(1)


def test_every_relative_markdown_link_resolves():
    broken = []
    for path in _markdown_files():
        for number, target in _links(path):
            if re.match(r"(https?://|mailto:|#)", target):
                continue
            file = target.split("#", 1)[0]
            if not (path.parent / file).exists():
                broken.append(f"{path.relative_to(ROOT)}:{number}: {target}")
    assert not broken, "\n".join(broken)


def test_the_new_pages_are_linked_from_the_entry_points():
    assert "docs/quickstart.md" in (ROOT / "README.md").read_text()
    index = (ROOT / "docs" / "README.md").read_text()
    for page in ("quickstart.md", "secrets.md", "install-audit.md"):
        assert f"]({page})" in index, f"docs/README.md does not list {page}"


# --- secrets ----------------------------------------------------------------------------------------

#: Profile field names that hold the name of a Flyte secret, outside `Secrets` (whose every field does).
_SECRET_FIELD = re.compile(r"(_secret|secret_name|repo_key|codex_login|pat_secret|claude_token)$")


def _secret_keys() -> set[str]:
    """Every profile key that names a Flyte secret, read from the profile's own dataclasses."""
    keys = {f.name for f in dataclasses.fields(_profile.Secrets)}
    for obj in vars(_profile).values():
        if dataclasses.is_dataclass(obj) and getattr(obj, "__module__", "") == _profile.__name__:
            keys |= {f.name for f in dataclasses.fields(obj) if _SECRET_FIELD.search(f.name)}
    return keys


def test_the_secret_keys_are_found():
    # Guards the derivation itself: if the dataclasses are renamed this must not silently find nothing.
    assert {"claude_token", "deploy_key", "codex_login", "pat_secret", "private_key_secret"} <= _secret_keys()


def _first_cells(text: str) -> list[str]:
    return [ln.split("|")[1] for ln in text.splitlines() if ln.startswith("|") and ln.count("|") > 2]


def test_every_secret_valued_profile_key_is_in_the_secrets_reference():
    cells = " ".join(_first_cells(SECRETS_DOC.read_text()))
    missing = sorted(key for key in _secret_keys() | {"repo_keys"} if key not in cells)
    assert not missing, f"docs/secrets.md has no row naming: {', '.join(missing)}"


def test_the_secrets_reference_has_the_columns_a_reader_needs():
    header = next(ln for ln in SECRETS_DOC.read_text().splitlines() if ln.startswith("| Profile key"))
    for column in ("required", "unlocks", "obtain", "store", "mounted"):
        assert column in header.lower(), f"secrets table lacks a {column} column"


#: The `[table]` each secret-holding profile dataclass is read from, so a row is matched by its qualified key.
_TABLES = {
    "Secrets": "[secrets]",
    "Slot": "[[slots]]",
    "Gcp": "[gcp]",
    "Github": "[github]",
    "Board": "[board]",
    "Browser": "[browser]",
    "Bench": "[bench]",
}


def _qualified_secret_keys() -> set[str]:
    keys = {f"[secrets] {f.name}" for f in dataclasses.fields(_profile.Secrets)}
    for name, obj in vars(_profile).items():
        is_profile_class = isinstance(obj, type) and obj.__module__ == _profile.__name__
        if not (is_profile_class and dataclasses.is_dataclass(obj)):
            continue
        fields = [f.name for f in dataclasses.fields(obj) if _SECRET_FIELD.search(f.name)]
        if fields:
            assert name in _TABLES, f"{name} holds secret names; add its table to _TABLES"
            keys |= {f"{_TABLES[name]} {field}" for field in fields}
    return keys


def test_every_secret_key_has_a_row_under_its_own_table():
    # A bare-name match lets `[bench] claude_token` stand in for a deleted `[secrets] claude_token` row.
    cells = " ".join(c.replace("`", "") for c in _first_cells(SECRETS_DOC.read_text()))
    missing = sorted(key for key in _qualified_secret_keys() if key not in cells)
    assert not missing, f"docs/secrets.md has no row for: {', '.join(missing)}"


# --- the quickstart runs as written ---------------------------------------------------------------

QUICKSTART = ROOT / "docs" / "quickstart.md"


def test_the_quickstart_python_version_is_the_one_the_engine_runs_on():
    stated = re.search(r"Python (\d+\.\d+)", QUICKSTART.read_text())
    assert stated, "the quickstart names no Python version"
    target = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["mypy"]["python_version"]
    assert stated.group(1) == target


def test_no_doc_runs_a_tracked_script_that_is_not_executable():
    bad = []
    for path in _markdown_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for script in re.findall(r"(?:^|[\s`(])\./(\S+?\.sh)\b", line):
                mode = subprocess.run(
                    ["git", "ls-files", "-s", script],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                ).stdout
                if mode and not mode.startswith("100755"):
                    bad.append(f"{path.relative_to(ROOT)}:{number}: ./{script} is not executable")
    assert not bad, "\n".join(bad)


def test_the_quickstart_config_command_writes_the_file_the_example_profiles_name():
    found = re.search(r"flyte create config [^`]*--output ([^\s`]+)", QUICKSTART.read_text())
    assert found, "the quickstart has no `flyte create config --output` command"
    for example in (CLAUDE_EXAMPLE, CODEX_EXAMPLE):
        assert found.group(1) == _profile.load(str(example)).backend.config


def _block_under(heading: str) -> str:
    text = QUICKSTART.read_text()
    section = text[text.index(heading) :]
    block = re.search(r"```bash\n(.*?)```", section, re.DOTALL)
    assert block, f"no bash block under {heading!r}"
    return block.group(1)


def test_quickstart_step_3_runs_against_a_local_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, timeout=10)
    section = QUICKSTART.read_text().split("## 3.", 1)[1].split("The generator initializes", 1)[0]
    block = (
        "\n".join(re.findall(r"```bash\n(.*?)```", section, re.DOTALL))
        .replace("https://github.com/you/my-workspace.git", str(remote))
        .replace("../my-workspace", str(tmp_path / "ws"))
        .replace("../flyte-agent-factory", str(ROOT))
        .replace("uv run", "uv run --offline")
    )
    # A drifted URL or path would otherwise push to the real GitHub or run outside the temp directory.
    assert str(remote) in block and str(ROOT) in block and str(tmp_path / "ws") in block
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    ran = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block],
        cwd=ROOT,
        env={**os.environ, **identity},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    owners = (tmp_path / "ws" / ".github" / "CODEOWNERS").read_text()
    assert "@OWNER" not in owners
    pushed = subprocess.run(
        ["git", "--git-dir", str(remote), "log", "--oneline", "main"], capture_output=True, timeout=10
    )
    assert pushed.returncode == 0 and pushed.stdout


def test_the_shareable_docs_name_no_owner_identifier():
    pages = [
        ROOT / "README.md",
        ROOT / "profiles" / "README.md",
        *sorted((ROOT / "profiles").glob("example-*.toml")),
    ]
    pages += [ROOT / "docs" / page for page in ("quickstart.md", "secrets.md", "install-audit.md")]
    hits = [
        f"{page.relative_to(ROOT)}:{number}"
        for page in pages
        for number, line in enumerate(page.read_text().splitlines(), 1)
        if personal_literals.pattern().search(line)
    ]
    assert not hits, "\n".join(hits)


@pytest.mark.parametrize("path", [CLAUDE_EXAMPLE, CODEX_EXAMPLE], ids=lambda p: p.name)
def test_a_personal_example_passes_the_factory_mcp_profile_check(path):
    # Quickstart step 5 deploys factory-mcp, which refuses a profile without an [mcp] table at import.
    assert _profile.load(str(path)).mcp is not None, f"{path.name} has no [mcp] table"
    env = {**os.environ, "AGENT_FACTORY_PROFILE": str(path), "PYTHONPATH": str(ROOT / "src")}
    env.pop("AGENT_FACTORY_PROFILE_TOML", None)
    ran = subprocess.run(
        [sys.executable, "-c", "import flyte_agent_factory._factory_mcp"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert ran.returncode == 0, ran.stderr[-2000:]


def test_the_quickstart_mcp_snippet_is_valid_toml():
    text = QUICKSTART.read_text()
    snippets = [s for s in re.findall(r"```toml\n(.*?)```", text, re.DOTALL) if "[mcp]" in s]
    assert snippets, "the quickstart shows no [mcp] toml block"
    for snippet in snippets:
        assert tomllib.loads(snippet)["mcp"]["public_url"].startswith("https://")
    assert '`[mcp] public_url = "' not in text, "an inline `[mcp] public_url = ...` is not valid TOML"


def test_the_quickstart_names_the_url_line_the_deploy_prints():
    # Step 6 tells the reader to copy the printed URL; the line must be the one the deploy child prints.
    assert "factory-mcp public URL" in _deploy.FACTORY_CHILD
    assert "factory-mcp public URL" in (ROOT / "docs" / "quickstart.md").read_text()


def test_the_personal_examples_are_not_deployment_profiles_by_default():
    # `_profile.require()` lists profiles/*.toml and `-p <name>` loads profiles/<name>.toml: examples are
    # reachable only by an explicit name, and none is the default.
    assert not (ROOT / "profiles" / _profile.DEFAULT_FILE).is_file() or (
        (ROOT / "profiles" / _profile.DEFAULT_FILE).read_text().strip()
        not in {"example", "example-claude-personal"}
    )


def test_export_local_credentials_is_tracked_executable():
    mode = subprocess.run(
        ["git", "ls-files", "-s", "scripts/export_local_credentials.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    assert mode.startswith("100755"), mode
