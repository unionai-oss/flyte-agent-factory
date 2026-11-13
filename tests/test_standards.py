import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _contract,
    _gate_base,
    _instructions,
    _profile,
    _review,
    _session_config,
    _standards,
)
from tests._standards import (
    DOCSTRINGS,
    EXCEPTIONS,
    FAIL_TO_PASS,
    FILES,
    MYPY,
    PATTERN_DEMO,
    PROMOTION,
    RUFF_AND_FORMAT,
    RULES,
    SECURITY,
    TEMPLATE,
    rule_text,
)

#: `[standards]` for the template set, as a workspace without its own would point at it.
TEMPLATE_CONFIG = _standards.settings({"dir": str(TEMPLATE)})


def search(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    """`re.search` for a pattern the shipped standards must match."""
    match = re.search(pattern, text, flags)
    assert match, pattern
    return match


def test_shipped_content():
    """Each rule of the template set parses: a unique ID, Why, Check and one valid Enforced line."""
    assert RULES
    assert len({r.ident for r in RULES}) == len(RULES)
    for rule in RULES:
        assert rule.level in {"MUST", "SHOULD"}
        assert not rule.error, (rule.ident, rule.error)
        assert "Why: " in rule.body
        assert any(line.startswith("Check: ") and line[7:].strip() for line in rule.body.splitlines())
        timeouts = [line for line in rule.body.splitlines() if line.lower().lstrip().startswith("timeout")]
        assert len(timeouts) <= 1, rule.ident
        for line in timeouts:
            assert re.fullmatch(r"Timeout: [1-9][0-9]*", line), (rule.ident, line)
            assert int(line[9:]) <= _gate_base._MAX_TOOL_TIMEOUT, rule.ident
            assert rule.kind in {"command", "lint", "script"}, rule.ident
    for name in FILES:
        if name == "README.md" or "/configs/" in name or name == "brand.md":
            continue
        assert any(r.file == name for r in RULES), name


@pytest.mark.parametrize(
    "table",
    [
        {"languages": "python"},
        {"include": "engineering"},
        {"brand": False},
        {"dir": []},
        {"other": "x"},
        {"languages": ["../python"]},
    ],
)
def test_invalid_settings(table):
    with pytest.raises(ValueError, match=r"\[standards\]"):
        _standards.settings(table)


def test_custom_transport_and_late_detection(tmp_path):
    custom = tmp_path / "private"
    (custom / "languages").mkdir(parents=True)
    (custom / "engineering.md").write_text(FILES["engineering.md"])
    (custom / "security.md").write_text(FILES["security.md"])
    (custom / "languages/python.md").write_text(FILES["languages/python.md"])
    brand = tmp_path / "identity.md"
    brand.write_text(FILES["brand.md"])
    config = _standards.settings({"dir": "private", "brand": "identity.md"})
    bundle = _standards.decode(_standards.encode(_standards.capture(config, tmp_path)))
    project = tmp_path / "workspace/app"
    project.mkdir(parents=True)
    first = _standards.materialize(project.parent, project, config, bundle)
    assert "languages/python.md" not in first
    assert first["brand.md"] == brand.read_text()
    (project / "main.py").touch()
    second = _standards.materialize(project.parent, project, config, bundle)
    assert "languages/python.md" in second
    assert (project.parent / ".claude-flyte/standards/languages/python.md").is_file()
    explicit = _standards.Standards(languages=())
    assert "languages/python.md" not in _standards.select(bundle, explicit, project)


def test_default_brand_and_budget():
    files = _standards.capture(_standards.Standards(), Path("."))
    assert files == {}  # nothing configured: no rules, not the engine's own
    text = {"rules.md": "## X-01 SHOULD\noptional\nCheck: inspect\n\n## X-02 MUST\nrequired\nCheck: run\n"}
    bounded = _standards.review_text(text, budget=55)
    assert "X-02 MUST" in bounded
    assert "X-01" not in bounded
    assert "brief >" in _standards.GUIDANCE
    assert "DESIGN.md" in _standards.GUIDANCE
    assert "SHOULD" in _standards.GUIDANCE


def test_profile_roundtrip(tmp_path):
    source = Path(__file__).resolve().parents[1] / "profiles/example.toml"
    text = source.read_text().replace('# dir = "../private-standards"', f'dir = "{TEMPLATE}"')
    profile = _profile.parse(text, str(tmp_path / "profile.toml"))
    deployed = _profile.parse(profile.deployment_text(), _profile.TOML_ENV)
    assert deployed.standards == profile.standards
    assert _standards.decode(deployed.standards_payload)["engineering.md"] == FILES["engineering.md"]
    assert deployed.deployment_text() == profile.deployment_text()


def test_bundle_rejects_traversal():
    with pytest.raises(ValueError, match="filenames"):
        _standards.decode(_standards.encode({"../outside": "bad"}))


def test_review_and_contract_guidance(tmp_path, monkeypatch):
    shutil.copytree(TEMPLATE, tmp_path / "standards")
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(_session_config, "_project_name", "app")
    craft = _review._qa_prompt("craft", "inspect", "brief", "", 1)
    assert f"{PATTERN_DEMO} MUST" in craft
    assert f"{PROMOTION} MUST" in craft  # promotion to a permanent repo needs judgement
    assert f"{FAIL_TO_PASS} MUST" not in craft
    assert f"{SECURITY} MUST" in craft
    assert "Cite rule IDs" in craft
    assert "Craft is advisory by default" in craft
    contract = _contract._contract_prompt("brief", "")
    assert f"{FAIL_TO_PASS} MUST" in contract
    assert "Include applicable MUST rule IDs as assertions" in contract
    assert "60-line" in contract
    correctness = _review._qa_prompt("correctness", "inspect", "brief", "", 1)
    assert "Do not grade style" in correctness
    assert f"{PROMOTION} MUST" not in correctness
    assert not re.search(r"[A-Za-z][A-Za-z0-9._-]* MUST", correctness)


@pytest.mark.parametrize(
    ("filename", "language"),
    [
        ("pyproject.toml", "python"),
        ("setup.py", "python"),
        ("src/main.py", "python"),
        ("main.tf", "terraform"),
        ("package.json", "typescript"),
        ("web/package.json", "typescript"),
        ("src/app.ts", "typescript"),
        ("web/src/App.tsx", "typescript"),
        ("scripts/run.sh", "shell"),
    ],
)
def test_auto_language_detection(tmp_path, filename, language):
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    config = TEMPLATE_CONFIG
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path)
    assert f"languages/{language}.md" in selected


def test_include_and_explicit_languages(tmp_path):
    config = _standards.settings({"dir": str(TEMPLATE), "include": ["security"], "languages": ["shell"]})
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path)
    assert "engineering.md" not in selected
    assert "security.md" in selected
    assert "languages/shell.md" in selected


def test_oversized_bundle():
    with pytest.raises(ValueError, match="512 KB"):
        _standards.encode({"large.md": "x" * (_standards.MAX_FILES + 1)})


def test_brand_guidance():

    guidance = _instructions._recording_note()
    assert ".claude-flyte/standards/brand.md" in guidance
    assert "follow it; do not redecide" in guidance
    assert "BRAND.md" in guidance
    assert "inferred" in guidance


def test_each_check_is_declared_once_and_ids_are_contiguous():
    python = [r for r in RULES if r.file == "languages/python.md"]
    # The formatter check lives in one rule only, so a failing format is reported once.
    formatting = [r.ident for r in python if "ruff format --check ." in r.arg]
    assert formatting == [RUFF_AND_FORMAT]
    terraform = [int(r.ident.rsplit("-", 1)[1]) for r in RULES if r.file == "languages/terraform.md"]
    assert terraform == list(range(1, len(terraform) + 1))


def test_repo_promotion_is_judgement():
    assert next(r for r in RULES if r.ident == PROMOTION).kind == "judgement"
    assert re.search(r"^Enforced: judgement$", rule_text(PROMOTION), re.MULTILINE)


def test_python_docstring_rule_says_tests_are_exempt():
    rule = rule_text(DOCSTRINGS)
    assert "tests" in search(r"^Check: .*$", rule, re.MULTILINE)[0]
    config = tomllib.loads(FILES["languages/configs/ruff.toml"])
    assert config["lint"]["per-file-ignores"] == {
        "**/test_*.py": ["D"],
        "**/*_test.py": ["D"],
        "**/tests/**": ["D"],
        "**/conftest.py": ["D"],
    }


def test_round3_standards():
    assert "Enforced: judgement" in rule_text(PROMOTION)
    assert "[mypy-tests.*]" in FILES["languages/configs/mypy.ini"]
    assert "tests" in search(r"^Check: .*$", rule_text(MYPY), re.MULTILINE)[0]


def test_mypy_baseline_accepts_untyped_tests(tmp_path):
    import subprocess
    import sys

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_x():\n    assert True\n")
    config = tmp_path / "mypy.ini"
    config.write_text(FILES["languages/configs/mypy.ini"])
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(config), "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _tool(tmp_path, module, config_name, *args):
    config = tmp_path / config_name
    config.write_text(FILES[f"languages/configs/{config_name}"])
    flag = "--config-file" if module == "mypy" else "--config"
    return subprocess.run(
        [sys.executable, "-m", module, *args, flag, str(config), "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_test_files(tmp_path):
    for path in (
        "test_x.py",
        "x_test.py",
        "conftest.py",
        "pkg/test_y.py",
        "pkg/y_test.py",
        "pkg/tests/helper.py",
    ):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("def check():\n    assert True\n")


def test_mypy_baseline_accepts_tomllib(tmp_path):
    (tmp_path / "lib.py").write_text(
        'import tomllib\n\ndef read_port() -> int:\n    return int(tomllib.loads("port = 8080")["port"])\n'
    )
    result = _tool(tmp_path, "mypy", "mypy.ini")
    assert result.returncode == 0, result.stdout + result.stderr


def test_mypy_baseline_accepts_untyped_test_files_anywhere(tmp_path):
    _write_test_files(tmp_path)
    (tmp_path / "pkg/lib.py").write_text("def untyped():\n    return 1\n")
    result = _tool(tmp_path, "mypy", "mypy.ini")
    # Only the real module is flagged, so the baseline is not simply switched off.
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pkg/lib.py" in result.stdout
    assert result.stdout.count("error:") == 1, result.stdout


def test_ruff_baseline_exempts_test_files_anywhere_from_docstrings(tmp_path):
    _write_test_files(tmp_path)
    (tmp_path / "pkg/lib.py").write_text("def public():\n    return 1\n")
    result = _tool(tmp_path, "ruff", "ruff.toml", "check", "--select", "D", "--output-format", "concise")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pkg/lib.py" in result.stdout
    assert not [line for line in result.stdout.splitlines() if "test" in line or "conftest" in line], (
        result.stdout
    )


def test_python_rules_name_every_test_file_pattern():
    for ident in (DOCSTRINGS, MYPY):
        check = search(r"^Check: .*$", rule_text(ident), re.MULTILINE)[0]
        assert "test_*.py" in check and "conftest.py" in check, ident


def test_retired_rule_ids_are_gone_and_py_014_carries_the_type_rule():
    retired_line = search(r"^Retired IDs are never reused: .*$", FILES["README.md"], re.MULTILINE)[0]
    ids = re.findall(r"[A-Z]+-\d+", retired_line)
    folded_into = set(re.findall(r"folded into ([A-Z]+-\d+)", retired_line))
    current = {r.ident for r in RULES}
    assert ids and folded_into <= current
    assert not (set(ids) - folded_into) & current
    assert not [n for n in FILES if n.startswith("platforms/")]
    body = next(r.body for r in RULES if r.ident == MYPY)
    assert body.startswith("Type public interfaces and run the type checker (mypy or Pyright)")
    assert "inspect public parameters and return types" in body
    assert "never reused" in FILES["README.md"]


def test_eng_007_holds_the_flyte_clause():
    body = next(r.body for r in RULES if r.ident == EXCEPTIONS)
    for needed in ("flyte.errors.NonRecoverableError", "retries=", "checkpoint", "loop boundary"):
        assert needed in body


def test_the_flyte_clause_reaches_the_craft_reviewer(tmp_path):
    # qa: the Flyte clause was a judgement rule the craft reviewer saw; folded into a tool-enforced rule it
    # was filtered out of `judgement_only` review text, so nothing checked it at all.
    (tmp_path / "main.py").write_text("import flyte\n")
    config = TEMPLATE_CONFIG
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path)
    craft = _standards.review_text(selected, judgement_only=True)
    assert EXCEPTIONS in craft and "NonRecoverableError" in craft


def test_a_stray_tsconfig_does_not_select_typescript(tmp_path):
    config = TEMPLATE_CONFIG
    (tmp_path / "tsconfig.json").touch()
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path)
    assert "languages/typescript.md" not in selected
    (tmp_path / "index.ts").touch()
    selected = _standards.select(_standards.capture(config, tmp_path), config, tmp_path)
    assert "languages/typescript.md" in selected


def test_ruff_baseline_line_length_is_110():
    # qa: a 110 formatter rejoins lines in code formatted at 100 and fails `ruff format --check` on projects
    # that did not change; so the formatter stays at 100 and only the E501 limit is 110.
    config = tomllib.loads(FILES["languages/configs/ruff.toml"])
    assert config["lint"]["pycodestyle"]["max-line-length"] == 110
    assert config["line-length"] == 100


def _private_standards(tmp_path):
    custom = tmp_path / "private"
    (custom / "languages/configs").mkdir(parents=True)
    (custom / "engineering.md").write_text(FILES["engineering.md"])
    (custom / "security.md").write_text(FILES["security.md"])
    (custom / "languages/configs/.tflint.hcl").write_text(FILES["languages/configs/.tflint.hcl"])
    return custom


def test_capture_skips_tool_caches_and_binary_files(tmp_path):
    # qa: `ruff check .` on the factory repo wrote standards/languages/configs/.ruff_cache/, and
    # capture crashed reading it as text, which broke every import of _profile.
    custom = _private_standards(tmp_path)
    for cache in (".ruff_cache/0.17.1", ".git/objects", "languages/__pycache__"):
        (custom / cache).mkdir(parents=True)
        (custom / cache / "entry").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
        (custom / cache / "notes.md").write_text("text in a hidden or cache directory is not a standard\n")
    for name, content in (
        ("languages/configs/logo.png", b"\x89PNG\r\n\x1a\n\0\0"),
        ("languages/configs/zeros.bin", b"\0" * 8),
        ("brand-logo.png", b"\x89PNG\r\n\x1a\n\0\0"),
    ):
        (custom / name).write_bytes(content)
    files = _standards.capture(_standards.settings({"dir": "private"}), tmp_path)
    assert sorted(files) == ["engineering.md", "languages/configs/.tflint.hcl", "security.md"]


def test_capture_names_a_required_file_that_is_not_text(tmp_path):
    custom = _private_standards(tmp_path)
    (custom / "security.md").write_bytes(b"\x89PNG\r\n\x1a\n\0\0")
    with pytest.raises(ValueError, match=r"\[standards\] security\.md"):
        _standards.capture(_standards.settings({"dir": "private"}), tmp_path)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        # auto mode: the pack used to vanish without a word
        ("languages/python.md", FILES["languages/python.md"].encode() + "caf\xe9\n".encode("latin-1")),
        ("demo.md", FILES["demo.md"].encode() + b"\xff"),
        ("README.md", FILES["README.md"].encode() + b"\xff"),
        ("languages/configs/ruff.toml", b"# caf\xe9\n"),
        ("languages/configs/zeros.txt", b"\0" * 8),
    ],
)
def test_a_shipped_file_that_is_not_text_fails_naming_it(tmp_path, name, content):
    custom = _private_standards(tmp_path)
    (custom / name).parent.mkdir(parents=True, exist_ok=True)
    (custom / name).write_bytes(content)
    with pytest.raises(ValueError, match=rf"\[standards\] {re.escape(name)} "):
        _standards.capture(_standards.settings({"dir": "private"}), tmp_path)


def test_a_broken_symlink_fails_naming_it(tmp_path):
    custom = _private_standards(tmp_path)
    (custom / "languages/broken.md").symlink_to(tmp_path / "nonexistent")
    with pytest.raises(ValueError, match=r"\[standards\] languages/broken\.md "):
        _standards.capture(_standards.settings({"dir": "private"}), tmp_path)


def test_a_symlinked_pack_directory_is_skipped(tmp_path):
    custom = _private_standards(tmp_path)
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "python.md").write_text(FILES["languages/python.md"])
    (custom / "languages/packs").symlink_to(packs)
    files = _standards.capture(_standards.settings({"dir": "private"}), tmp_path)
    assert sorted(files) == ["engineering.md", "languages/configs/.tflint.hcl", "security.md"]


def test_a_directory_symlink_cycle_is_skipped(tmp_path):
    custom = _private_standards(tmp_path)
    (custom / "languages/cycle").symlink_to(custom, target_is_directory=True)
    files = _standards.capture(_standards.settings({"dir": "private"}), tmp_path)
    assert sorted(files) == ["engineering.md", "languages/configs/.tflint.hcl", "security.md"]


def test_a_symlinked_pack_file_is_read(tmp_path):
    custom = _private_standards(tmp_path)
    pack = tmp_path / "python.md"
    pack.write_text(FILES["languages/python.md"])
    (custom / "languages/python.md").symlink_to(pack)
    files = _standards.capture(_standards.settings({"dir": "private"}), tmp_path)
    assert files["languages/python.md"] == FILES["languages/python.md"]


def test_crlf_standards_keep_their_rules(tmp_path):
    # qa: CRLF files kept their \r, so RULE matched nothing and the pack enforced no rule at all.
    custom = _private_standards(tmp_path)
    (custom / "languages/python.md").write_bytes(FILES["languages/python.md"].replace("\n", "\r\n").encode())
    files = _standards.capture(_standards.settings({"dir": "private"}), tmp_path)
    assert files["languages/python.md"] == FILES["languages/python.md"]


@pytest.mark.parametrize("value", ["standards", "../standards"])
def test_path_origin_never_uses_engine_or_cwd(tmp_path, monkeypatch, value):
    root = tmp_path / "direction"
    base = root / "profiles"
    base.mkdir(parents=True)
    engine = tmp_path / "engine"
    (engine / "standards").mkdir(parents=True)
    (engine / "profiles").mkdir()
    cwd = tmp_path / "cwd"
    (cwd / "standards").mkdir(parents=True)
    monkeypatch.setattr(_standards, "ROOT", engine / "profiles" if value.startswith("..") else engine)
    monkeypatch.chdir(cwd)
    with pytest.raises(ValueError, match="dir does not exist") as exc:
        _standards.capture(_standards.Standards(dir=value), base, root=root)
    assert str(base / value) in str(exc.value)


def test_path_origin_candidate_order(tmp_path):
    root = tmp_path / "tree"
    base = root / "profiles"
    base.mkdir(parents=True)
    assert _standards._resolve("standards", base, root) == base / "standards"
    (root / "standards").mkdir()
    assert _standards._resolve("standards", base, root) == root / "standards"
    (base / "standards").mkdir()
    assert _standards._resolve("standards", base, root) == base / "standards"
    assert _standards._resolve("../standards", base, root) == base / "../standards"
    absolute = tmp_path / "absolute"
    assert _standards._resolve(str(absolute), base, root) == absolute


def test_path_origin_brand_uses_repository_compatibility_candidate(tmp_path):
    base = tmp_path / "profiles"
    base.mkdir()
    (tmp_path / "brand.md").write_text("repository brand")
    files = _standards.capture(_standards.Standards(brand="brand.md"), base, root=tmp_path)
    assert files == {"brand.md": "repository brand"}


def test_a_file_two_language_packs_detect_is_checked_against_upstream_once(tmp_path, monkeypatch):
    from flyte_agent_factory import _vendored

    loads = []

    class Upstreams:
        def unchanged(self, path):
            return False

    def load(*args, **kwargs):
        loads.append(args)
        return Upstreams()

    monkeypatch.setattr(_vendored, "load", load)
    project = tmp_path / "app"
    project.mkdir()
    (project / "main.py").touch()
    files = {
        "languages/python.md": "detect: **/*.py\n",
        "languages/scripting.md": "detect: *.py\n",
        "languages/rust.md": "detect: **/*.rs\n",
    }
    selected = _standards.select(files, _standards.Standards(), project, fetch=False)
    assert {"languages/python.md", "languages/scripting.md"} <= selected.keys()
    assert "languages/rust.md" not in selected
    assert len(loads) == 1
