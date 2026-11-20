"""The standards gate's per-file tools and `forbid` rules judge only what the project changed upstream."""

import hashlib
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _gate_base, _gate_rules, _gates, _vendored
from tests import _standards as template

pytestmark = pytest.mark.real_factory_gates

BAD_SH = "#!/bin/sh\ngit tag $1\n"  # SC2086
CONFIGS = template.TEMPLATE / "languages/configs"
SHELL = {"languages/shell.md": template.FILES["languages/shell.md"]}
TREE = "copy"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)


def _checkout(root: Path, files: dict[str, str], branch: str = "main") -> None:
    """A git checkout at its base (committed on `branch`; any other name leaves no default branch)."""
    _write(root, files)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


def _run(project: Path, rule: str, name: str = "engineering.md") -> _gates.Result:
    results = _gates.standards_results(project, {name: rule}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "X-1"]
    return result


def _command(rule_line: str, ident: str = "X-1") -> str:
    return f"## {ident} MUST\n{rule_line}\n"


def _owned_sh(project: Path) -> None:
    _write(project, {"checks.sh": "#!/bin/sh\necho ok\n", "evidence/run.sh": "#!/bin/sh\necho ok\n"})


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    _owned_sh(root)
    return root


# -- shellcheck's file list --


def test_an_unchanged_nested_checkout_script_is_not_linted(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert "repo/utils/bump.sh" not in result.output.splitlines()[0]
    assert "checks.sh" in result.output.splitlines()[0]


def test_a_modified_nested_checkout_script_is_linted_and_fails(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    (project / "repo/utils/bump.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "repo/utils/bump.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "SC2086" in result.output


def test_a_new_script_in_a_nested_checkout_is_linted(project):
    _checkout(project / "repo", {"utils/ok.sh": "#!/bin/sh\necho ok\n"})
    _write(project / "repo", {"utils/new.sh": BAD_SH})
    assert _run(project, _command("Enforced: command shellcheck")).status is _gate_base.Status.FAIL


def test_a_project_owned_script_is_always_linted(project):
    _checkout(project / "repo", {"utils/ok.sh": "#!/bin/sh\necho ok\n"})
    _write(project, {"scripts/mine.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "scripts/mine.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output


def test_a_nested_checkout_with_no_known_base_is_linted_as_the_projects(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH}, branch="feature")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "repo/utils/bump.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo: base unknown, so its files are judged as the project's" in result.output


def test_the_output_names_the_nested_checkout_judged_against_its_base(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    base = _git(project / "repo", "rev-parse", "HEAD")
    result = _run(project, _command("Enforced: command shellcheck"))
    assert f"Nested git checkouts judged against their base: repo (base {base[:12]})." in result.output


# -- a vendored.toml tree (a copy without .git) --


@pytest.fixture
def copy(tmp_path, monkeypatch):
    """A project holding a copy of a local upstream repository, declared in `vendored.toml`."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(_vendored, "_ALLOWED_SCHEMES", ("https", "file"))
    monkeypatch.setattr(_vendored, "_UNAVAILABLE", {})
    upstream = tmp_path / "upstream"
    _checkout(upstream, {"a.sh": BAD_SH, "b.sh": BAD_SH, "app.js": "var a = 1\n", "lib/x.py": "import os\n"})
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    project = tmp_path / "proj"
    shutil.copytree(upstream, project / TREE, ignore=shutil.ignore_patterns(".git"))
    _owned_sh(project)
    commit = _git(upstream, "rev-parse", "HEAD")
    (project / "vendored.toml").write_text(
        f'[[tree]]\npath = "{TREE}"\nrepo = "{upstream.as_uri()}"\ncommit = "{commit}"\n'
    )
    return project


def test_an_identical_vendored_script_is_skipped_and_a_changed_one_is_linted(copy):
    (copy / TREE / "b.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(copy, _command("Enforced: command shellcheck"))
    first = result.output.splitlines()[0]
    assert f"{TREE}/b.sh" in first and f"{TREE}/a.sh" not in first
    assert result.status is _gate_base.Status.FAIL
    assert f"{TREE}/a.sh" not in result.output.split("\n", 1)[1].split("SC2086")[0]


def test_an_all_identical_vendored_tree_passes_and_is_named(copy):
    result = _run(copy, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert f"Files identical to upstream are not the project's (vendored.toml): {TREE} (" in result.output


# -- the benchmark layout, end to end --


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_an_untouched_checkout_does_not_fail_the_shell_pack(tmp_path):
    project = tmp_path / "proj"
    _write(project, {"checks.sh": "#!/bin/sh\necho ok\n", "setup.sh": "#!/bin/sh\necho ok\n"})
    _write(project, {"evidence/run.sh": "#!/bin/sh\necho ok\n"})
    _checkout(project / "repo", {"utils/bump_docker.sh": BAD_SH})
    before = _digest(project)
    results = {r.rule: r for r in _gates.standards_results(project, SHELL, configs=CONFIGS)}
    sh002 = results[template.find(kind="command", arg="shellcheck", file="languages/shell.md")]
    assert sh002.status is _gate_base.Status.PASS, sh002.output
    assert "Nested git checkouts judged against their base: repo (base " in sh002.output
    assert _digest(project) == before


# -- forbid rules --

FORBID = _command("Enforced: forbid TODO in **/*.txt")


def test_a_pattern_only_in_an_unchanged_checkout_file_passes(project):
    _checkout(project / "repo", {"notes.txt": "TODO upstream\n"})
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "Nested git checkouts judged against their base: repo (base " in result.output


def test_the_pattern_in_a_changed_checkout_file_or_a_project_file_fails(project):
    _checkout(project / "repo", {"notes.txt": "TODO upstream\n"})
    (project / "repo/notes.txt").write_text("TODO upstream\nTODO mine\n")
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.FAIL
    assert "repo/notes.txt:1" in result.output
    (project / "repo/notes.txt").write_text("TODO upstream\n")
    _git(project / "repo", "checkout", "-q", "--", ".")
    assert _run(project, FORBID).status is _gate_base.Status.PASS
    _write(project, {"mine.txt": "TODO\n"})
    result = _run(project, FORBID)
    assert result.status is _gate_base.Status.FAIL and "mine.txt:1" in result.output
    assert "repo/notes.txt" not in result.output.split("Nested git")[0]


# -- ruff, eslint, prettier --

RUFF_PY = "import os\n"  # F401


@pytest.fixture
def temporary(monkeypatch):
    """The text of each file a command names after `--config` or `--ignore-path`, as the tool read it."""
    texts: dict[str, str] = {}
    run = _gate_base._run

    def read_then_run(args, *rest, **kwargs):
        for flag, value in itertools.pairwise(args):
            # `os.path.isfile` is False, where `Path.is_file` raises, for an inline value too long for a name.
            if flag in ("--config", "--ignore-path") and os.path.isfile(value):
                texts[value] = Path(value).read_text()
        return run(args, *rest, **kwargs)

    monkeypatch.setattr(_gate_base, "_run", read_then_run)
    return texts


def _named_file(result: _gates.Result, texts: dict[str, str], flag: str = "--config") -> str:
    """The text of the temporary file the recorded command names last after `flag`, which is gone now."""
    command = shlex.split(result.output.splitlines()[0])
    path = command[len(command) - command[::-1].index(flag)]
    assert not Path(path).exists(), path
    return texts[path]


def _ruff_config(result: _gates.Result, texts: dict[str, str]) -> dict[str, Any]:
    return tomllib.loads(_named_file(result, texts))


def _inline_excludes(result: _gates.Result) -> list[str]:
    """The excludes the recorded ruff command gives inline, as the one `--config` override holding them."""
    command = shlex.split(result.output.splitlines()[0])
    [override] = [v for f, v in itertools.pairwise(command) if f == "--config" and v.startswith("extend-")]
    excludes: list[str] = tomllib.loads(override)["extend-exclude"]
    return excludes


def _eslint_ignores(result: _gates.Result, texts: dict[str, str]) -> list[str]:
    ignores = re.search(r"\{ ignores: (\[.*\]) \}\];\n$", _named_file(result, texts), re.DOTALL)
    assert ignores, _named_file(result, texts)
    patterns: list[str] = json.loads(ignores[1])
    return patterns


def _ruff_rules() -> list[tuple[str, str]]:
    return [
        ("check", _command("Enforced: command ruff check --select F401 .")),
        ("lint", _command("Enforced: lint ruff F401")),
        ("format", _command("Enforced: command ruff format --check .")),
    ]


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_ruff_leaves_an_unchanged_checkout_alone_and_judges_a_changed_file(project, kind):
    bad = "x=1\n" if kind == "format" else RUFF_PY
    rule = dict(_ruff_rules())[kind]
    _write(project, {"mine.py": "x = 1\n"})
    _checkout(project / "repo", {"pkg/a.py": bad})
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output
    command = result.output.splitlines()[0]
    after_harness = command.split("--extend-exclude .claude-flyte")[1]
    assert """--config 'extend-exclude = ["./repo"]'""" in after_harness
    # A `lint` rule still reads no configuration; the others keep the baseline they had.
    assert ("--isolated" in command) == (kind == "lint")
    assert (str(CONFIGS / "ruff.toml") in after_harness) == (kind != "lint")
    _write(project / "repo", {"pkg/a.py": bad + "# changed\n"})
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pkg/a.py" in result.output


def test_ruff_excludes_file_by_file_when_the_tree_holds_a_changed_one(project):
    _checkout(project / "repo", {"a.py": RUFF_PY, "b.py": "x = 1\n"})
    (project / "repo/b.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo/a.py"]


def test_a_vendored_tree_is_excluded_from_ruff_too(copy):
    result = _run(copy, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    (copy / TREE / "lib/x.py").write_text("import os\nimport sys\n")
    assert _run(copy, _ruff_rules()[0][1]).status is _gate_base.Status.FAIL


ESLINT = _command("Enforced: command eslint .")


def test_eslint_leaves_an_unchanged_checkout_alone(project, temporary):
    # The project's own config: the baseline one needs packages this image does not carry.
    _write(
        project,
        {"own.js": "var b = 2;\n", "eslint.config.mjs": 'export default [{ rules: { semi: "error" } }];\n'},
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    *ignores, wrapper = _eslint_ignores(result, temporary)
    assert ignores == ["repo"] and wrapper.endswith(".mjs")
    (project / "repo/lib/x.js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/lib/x.js" in result.output.split("\n", 1)[1]


PRETTIER = _command("Enforced: command prettier --check .")


def test_prettier_leaves_an_unchanged_checkout_alone(project):
    _write(project, {"own.js": "var b = 2;\n"})
    _checkout(project / "repo", {"lib/x.js": "var   a = 1\n"})
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "[warn] repo/lib/x.js" not in result.output
    (project / "repo/lib/x.js").write_text("var   a = 2\n")
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/lib/x.js" in result.output


# -- the helpers' contract --


def test_without_upstreams_every_script_is_listed(project):
    _checkout(project / "repo", {"utils/bump.sh": BAD_SH})
    command = _gate_rules.with_baseline_config(["shellcheck"], project, CONFIGS, ("extra.sh",))
    assert command == ["shellcheck", "checks.sh", "evidence/run.sh", "repo/utils/bump.sh", "extra.sh"]


def test_prettier_judges_a_changed_mdx_beside_unchanged_javascript(project):
    _checkout(project / "repo", {"a.js": "const a = 1;\n", "page.mdx": "# ok\n"})
    (project / "repo/page.mdx").write_text("#   changed\n")
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/page.mdx" in result.output


def test_ruff_excludes_an_explicit_unchanged_file(project):
    _checkout(project / "repo", {"bad.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 repo/bad.py"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_shellcheck_with_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_prettier_excludes_explicit_unchanged_file(project):
    _checkout(project / "repo", {"bad.js": "var   a = 1\n"})
    result = _run(project, _command("Enforced: command prettier --check repo/bad.js"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_shellcheck_with_options_and_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --severity warning"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_prettier_checks_explicit_changed_file_alongside_unchanged_file(project):
    _checkout(project / "repo", {"a.js": "var   a = 1\n", "b.js": "var b = 1;\n"})
    (project / "repo/b.js").write_text("var   b = 2\n")
    result = _run(project, _command("Enforced: command prettier --check repo/a.js repo/b.js"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/b.js" in result.output.split("Checking formatting...")[1]
    assert "repo/a.js" not in result.output.split("Checking formatting...")[1]


def test_shellcheck_invalid_options_still_fail_when_no_scripts(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --not-an-option"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_shellcheck_optional_color_with_no_scripts_passes(tmp_path):
    result = _run(tmp_path, _command("Enforced: command shellcheck --color"))
    assert result.status is _gate_base.Status.PASS, result.output


def test_shellcheck_filters_explicit_upstream_operands_and_keeps_option_values(project):
    _checkout(project / "repo", {"bad.sh": BAD_SH, "changed.sh": BAD_SH})
    (project / "repo/changed.sh").write_text(BAD_SH + "git tag $2\n")
    result = _run(
        project, _command("Enforced: command shellcheck --severity info repo/bad.sh -- repo/changed.sh")
    )
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/bad.sh" not in result.output.splitlines()[0]
    assert "repo/changed.sh" in result.output
    assert "checks.sh" in result.output


@pytest.mark.parametrize("option", ["--severity nonsense", "--color=bogus"])
def test_shellcheck_invalid_option_values_fail_without_scripts(tmp_path, option):
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {option}"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_prettier_separator_keeps_generated_options_before_operands(project):
    _checkout(project / "repo", {"bad.js": "var   a = 1\n"})
    _write(project, {"own.js": "var b = 1;\n"})
    result = _run(project, _command("Enforced: command prettier --check -- repo/bad.js own.js"))
    assert result.status is _gate_base.Status.PASS, result.output


@pytest.mark.parametrize("arguments", ["repo/bad.sh", "--severity warning repo/bad.sh", "-- repo/bad.sh"])
def test_shellcheck_only_explicit_unchanged_script_passes(tmp_path, arguments):
    _checkout(tmp_path / "repo", {"bad.sh": BAD_SH})
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.PASS, result.output


@pytest.mark.parametrize("arguments", ["--not-an-option repo/bad.sh", "--severity nonsense repo/bad.sh"])
def test_shellcheck_invalid_options_fail_with_only_unchanged_script(tmp_path, arguments):
    _checkout(tmp_path / "repo", {"bad.sh": BAD_SH})
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.FAIL, result.output


def test_shellcheck_generated_scripts_before_separator_are_checked(tmp_path):
    _checkout(tmp_path / "repo", {"bad.sh": BAD_SH})
    _write(tmp_path, {"own.sh": BAD_SH})
    result = _run(tmp_path, _command("Enforced: command shellcheck -- repo/bad.sh"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "own.sh" in result.output
    assert "SC2086" in result.output


@pytest.mark.parametrize("arguments", ["", "--"])
def test_shellcheck_checks_generated_script_starting_with_dash(tmp_path, arguments):
    _write(tmp_path, {"-bad.sh": BAD_SH})
    result = _run(tmp_path, _command(f"Enforced: command shellcheck {arguments}"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "SC2086" in result.output


# -- what counts as the project's in a nested checkout --


def test_a_gitignored_new_script_in_a_nested_checkout_is_the_projects(project):
    _checkout(project / "repo", {"utils/bad.sh": BAD_SH, ".gitignore": "build/\n"})
    _write(project / "repo", {"build/new.sh": BAD_SH})
    result = _run(project, _command("Enforced: command shellcheck"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/build/new.sh" in result.output.splitlines()[0]
    assert "repo/utils/bad.sh" not in result.output.splitlines()[0]


def test_a_gitignored_new_python_file_in_a_nested_checkout_is_not_excluded_from_ruff(project):
    _checkout(project / "repo", {"lib/ok.py": "x = 1\n", ".gitignore": "build/\n"})
    _write(project / "repo", {"build/x.py": RUFF_PY})
    # Walking a directory, ruff itself skips git-ignored files, the project's own as well: only the gate's
    # excludes are the upstream's question.
    result = _run(project, _ruff_rules()[0][1])
    assert _inline_excludes(result) == ["./repo/lib"], result.output
    result = _run(project, _command("Enforced: command ruff check --select F401 repo/build/x.py"))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/build/x.py" in result.output.split("\n", 1)[1]


def test_a_script_edited_inside_a_submodule_of_a_nested_checkout_is_linted(tmp_path):
    project = tmp_path / "proj"
    _owned_sh(project)
    _checkout(tmp_path / "subsrc", {"s.sh": "#!/bin/sh\necho ok\n"})
    _checkout(project / "repo", {"a.txt": "a\n"})
    _git(
        project / "repo",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(tmp_path / "subsrc"),
        "sub",
    )
    _git(project / "repo", "commit", "-q", "-m", "sub")
    (project / "repo/sub/s.sh").write_text(BAD_SH)
    result = _run(project, _command("Enforced: command shellcheck"))
    assert "repo/sub/s.sh" in result.output.splitlines()[0]
    assert result.status is _gate_base.Status.FAIL, result.output


# -- excludes: anchored, collapsed, escaped --


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_ruff_still_judges_a_project_directory_named_like_a_checkout(project, kind):
    _checkout(project / "repo", {"lib/ok.py": "x = 1\n"})
    _write(project, {"tools/repo/mine.py": "x=1\n" if kind == "format" else RUFF_PY})
    result = _run(project, dict(_ruff_rules())[kind])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "tools/repo/mine.py" in result.output.split("\n", 1)[1]


ESLINT_CONFIG = {"eslint.config.mjs": 'export default [{ rules: { semi: "error" } }];\n'}


def test_eslint_still_judges_a_project_directory_named_like_a_checkout(project):
    _checkout(project / "repo", {"lib/ok.js": "var a = 1;\n"})
    _write(project, {**ESLINT_CONFIG, "tools/repo/mine.js": "var b = 2\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "tools/repo/mine.js" in result.output.split("\n", 1)[1]


def test_unchanged_directories_are_excluded_whole_so_a_large_checkout_fits_the_command_line(project):
    files = {f"pkgs/p{i}/src/m{j}.py": RUFF_PY for i in range(40) for j in range(75)}
    _checkout(project / "repo", {**files, "pkgs/p0/changed.py": "x = 1\n"})
    (project / "repo/pkgs/p0/changed.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    others = sorted(f"./repo/pkgs/p{i}" for i in range(1, 40))
    assert _inline_excludes(result) == ["./repo/pkgs/p0/src", *others]


def test_a_changed_file_beside_unchanged_ones_and_directories_is_linted(project):
    _checkout(project / "repo", {"pkg/a.py": RUFF_PY, "pkg/b.py": "x = 1\n", "pkg/sub/c.py": RUFF_PY})
    (project / "repo/pkg/b.py").write_text(RUFF_PY)
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _inline_excludes(result) == ["./repo/pkg/a.py", "./repo/pkg/sub"]
    found = result.output.split("\n", 1)[1]
    assert "repo/pkg/b.py" in found and "repo/pkg/a.py" not in found and "repo/pkg/sub/c.py" not in found


@pytest.mark.parametrize("kind", ["check", "format"])
def test_gitignored_bytecode_does_not_keep_a_large_checkout_from_being_excluded_whole(project, kind):
    # Python leaves a git-ignored `__pycache__` in every package; such a file is the project's, but not one
    # ruff reads, so each package is excluded whole rather than file by file.
    files = {f"pkg{i}/module_{j:02}_with_a_longer_name.py": "x = 1\n" for i in range(150) for j in range(20)}
    _checkout(project / "repo", {**files, ".gitignore": "__pycache__/\n"})
    _write(project / "repo", {f"pkg{i}/__pycache__/x.pyc": "" for i in range(150)})
    rule = dict(_ruff_rules())[kind]
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert _inline_excludes(result) == ["./repo"]
    (project / "repo/pkg7/module_03_with_a_longer_name.py").write_text(
        "x=1\n" if kind == "format" else RUFF_PY
    )
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert "repo/pkg7/module_03_with_a_longer_name.py" in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("kind", ["check", "lint", "format"])
def test_a_flat_directory_of_thousands_of_unchanged_files_keeps_the_command_short(project, kind):
    # No directory can be excluded whole beside a changed file, so each file would be: 4,000 paths in one
    # argument pass the kernel's 128 KiB limit on a single argument. Ruff is given the project's files
    # instead, leaving out a git-ignored one as walking `.` does.
    names = [f"repo/flat/module_{i:04}_with_a_longer_name.py" for i in range(4000)]
    files = {name.removeprefix("repo/"): "x = 1\n" for name in names}
    _checkout(project / "repo", {**files, ".gitignore": "flat/generated.py\n"})
    _write(project, {names[17]: "y = 2\n", "repo/flat/generated.py": RUFF_PY, "tools/mine.py": "x = 1\n"})
    rule = dict(_ruff_rules())[kind]
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = result.output.splitlines()[0]
    assert len(command) < 2000, command[:500]
    assert "extend-exclude =" not in command and "Ruff read no configuration" not in result.output
    words = shlex.split(command)
    assert "." not in words and words[words.index(names[17]) + 1] == "tools", command
    assert ("--isolated" in command) == (kind == "lint")
    (project / names[17]).write_text("x=1\n" if kind == "format" else RUFF_PY)
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[17] in result.output.split("\n", 1)[1]


def test_ruff_with_excludes_still_reads_the_projects_own_configuration(project):
    _write(
        project,
        {
            "pyproject.toml": '[tool.ruff]\nextend-exclude = ["gen"]\n[tool.ruff.lint]\nselect = ["F401"]\n',
            "gen/g.py": RUFF_PY,
            "mine.py": "x = 1\n",
        },
    )
    _checkout(project / "repo", {"a.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo"]
    _write(project, {"mine.py": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "mine.py" in result.output.split("\n", 1)[1] and "gen/g.py" not in result.output


STRICT = {
    "pyproject.toml": '[tool.ruff.lint]\nselect = ["F401"]\n',
    # A nested configuration stricter than the project root's, for the files below it.
    "strict/ruff.toml": 'line-length = 20\n[lint]\nselect = ["F401", "E501"]\n',
    "strict/long.py": 'x = "a line longer than twenty"\n',
}


@pytest.mark.parametrize("unchanged", [1, 4000])
def test_ruff_with_excludes_still_applies_a_nested_stricter_configuration(project, unchanged):
    # Inline excludes for one unchanged file; the project's files named instead for 4,000 of them.
    names = [f"flat/module_{i:04}_with_a_longer_name.py" for i in range(unchanged)]
    _checkout(project / "repo", {**dict.fromkeys(names, "x = 1\n"), "flat/changed.py": "x = 1\n"})
    _write(project, {"repo/flat/changed.py": "y = 2\n", **STRICT})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1) and len(command) < 2000, command[:500]
    assert "E501" in found and "strict/long.py" in found and "repo/" not in found, found[:2000]
    _write(project, {"strict/long.py": "x = 1\n"})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output[:2000]


def test_too_many_project_files_to_name_fall_back_to_a_configuration_file_and_say_so(project, temporary):
    # Half of 3,200 files changed: neither the 1,600 unchanged ones nor the 1,600 changed ones fit the
    # command line. Excluded through a file, ruff reads no nested configuration, and the output says so.
    names = [f"repo/flat/module_{i:04}_with_a_longer_name.py" for i in range(3200)]
    _checkout(project / "repo", {name.removeprefix("repo/"): "x = 1\n" for name in names})
    _write(project, dict.fromkeys(names[1::2], "y = 2\n"))
    _write(project, STRICT)
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    assert "Ruff read no configuration nested below the project root" in result.output
    assert _ruff_config(result, temporary) == {
        "extend": str(project / "pyproject.toml"),
        "extend-exclude": [f"./{name}" for name in names[::2]],
    }
    _write(project, {names[1]: RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[1] in result.output.split("\n", 1)[1]


MARKDOWN = "# T\n\n```python\nx=[1,2,\n3]\n```\n"  # ruff format reads Python blocks in Markdown


def test_ruff_format_leaves_an_unchanged_upstream_markdown_file_alone(project):
    _checkout(project / "repo", {"README.md": MARKDOWN, "docs/guide.md": MARKDOWN, "pkg/a.py": "x = 1\n"})
    _write(project, {"repo/pkg/a.py": "x = 2\n"})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.PASS, result.output
    assert _inline_excludes(result) == ["./repo/README.md", "./repo/docs"]
    _write(project, {"repo/README.md": MARKDOWN + "\n"})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/README.md" in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("unchanged", [1, 4000])
def test_ruff_still_reads_the_projects_markdown_and_extend_include_files(project, unchanged):
    # Inline excludes for one unchanged file; the project's files named instead for 4,000 of them. Both a
    # Markdown file (ruff's default `include`) and a `.pyw` the project's `extend-include` adds are read,
    # and a new `.pyw` beside an unchanged `.py` keeps its directory from being excluded whole.
    names = [f"flat/module_{i:04}_with_a_longer_name.py" for i in range(unchanged)]
    files = {**dict.fromkeys(names, "x = 1\n"), "flat/changed.py": "x = 1\n", "other/a.py": "x = 1\n"}
    _checkout(project / "repo", files)
    _write(project, {"repo/flat/changed.py": "y = 2\n", "ruff.toml": 'extend-include = ["*.pyw"]\n'})
    _write(project, {"README.md": MARKDOWN})
    result = _run(project, dict(_ruff_rules())["format"])
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1) and len(command) < 2000, command[:500]
    assert "README.md" in found and "repo/" not in found, found[:2000]
    _write(project, {"README.md": "# T\n", "repo/other/new.pyw": RUFF_PY})
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    found = result.output.split("\n", 1)[1]
    assert "repo/other/new.pyw" in found and "flat/module" not in found, found[:2000]


def test_a_lint_rule_with_excludes_still_reads_no_project_configuration(project):
    _write(
        project,
        {
            "pyproject.toml": (
                '[tool.ruff]\nextend-exclude = ["gen"]\n'
                '[tool.ruff.lint.per-file-ignores]\n"mine.py" = ["F401"]\n'
            ),
            "gen/g.py": RUFF_PY,
            "mine.py": RUFF_PY,
        },
    )
    _checkout(project / "repo", {"a.py": RUFF_PY})
    configured = _run(project, _ruff_rules()[0][1])
    assert configured.status is _gate_base.Status.PASS, configured.output
    result = _run(project, dict(_ruff_rules())["lint"])
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "gen/g.py" in found and "mine.py" in found and "repo/a.py" not in found


def _thousands_of_js_files(project: Path) -> list[str]:
    """A checkout holding 3,000 files in one directory, one of them changed (and still well formatted)."""
    names = [f"repo/lib/module_{i:04}_with_a_longer_name.js" for i in range(3000)]
    _checkout(project / "repo", {name.removeprefix("repo/"): "var a = 1;\n" for name in names})
    (project / names[5]).write_text("var a = 2;\n")
    return names


def test_eslint_with_thousands_of_unchanged_files_is_given_the_projects_files(project):
    # Each ignore pattern is matched against every file ESLint walks: thousands of them took minutes over a
    # large checkout. ESLint is given the project's files instead, and still applies the project's config.
    names = _thousands_of_js_files(project)
    config = 'export default [{ rules: { semi: "error" } }, { ignores: ["repo/lib/generated.js"] }];\n'
    # A file of the project's that its config ignores, and one with no config for its extension.
    _write(
        project, {"eslint.config.mjs": config, "repo/lib/generated.js": "var g = 1\n", "tools/t.ts": "x\n"}
    )
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = shlex.split(result.output.splitlines()[0])
    assert len(shlex.join(command)) < 2000 and "--config" not in command, command[:20]
    assert {names[5], "repo/lib/generated.js", "tools", "eslint.config.mjs"} <= set(
        command
    ) and "." not in command
    assert "warning" not in result.output
    (project / names[5]).write_text("var a = 3\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


def test_eslint_with_thousands_of_excludes_and_no_dot_reads_them_from_a_config(project, temporary):
    # A command naming its own paths cannot have them swapped for the project's files.
    names = _thousands_of_js_files(project)
    _write(project, {**ESLINT_CONFIG, "own.js": "var b = 2;\n"})
    rule = _command("Enforced: command eslint repo own.js")
    result = _run(project, rule)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    assert len(result.output.splitlines()[0]) < 2000, result.output.splitlines()[0][:500]
    assert _eslint_ignores(result, temporary)[:-1] == [name for name in names if name != names[5]]
    (project / names[5]).write_text("var a = 3\n")
    result = _run(project, rule)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


def test_prettier_with_thousands_of_unchanged_files_is_given_the_projects_files(project):
    names = _thousands_of_js_files(project)
    unformatted = "var   g = 1\n"
    _write(
        project,
        {
            # Prettier's own ignore files still apply to the files it is given, and a file it has no
            # parser for is skipped as walking `.` skips it.
            ".prettierignore": "repo/lib/skipped.js\n",
            ".gitignore": "repo/lib/built.js\n",
            "repo/lib/skipped.js": unformatted,
            "repo/lib/built.js": unformatted,
            "repo/lib/data.unknown": unformatted,
        },
    )
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output[:2000]
    command = shlex.split(result.output.splitlines()[0])
    assert len(shlex.join(command)) < 2000 and "--ignore-path" not in command, command[:20]
    assert {names[5], "repo/lib/skipped.js", "repo/lib/data.unknown", "evidence"} <= set(command)
    assert "." not in command
    (project / names[5]).write_text(unformatted)
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert names[5] in result.output.split("\n", 1)[1]


@pytest.mark.parametrize("option", ["-c strict.mjs", "--config strict.mjs", "--config=strict.mjs"])
def test_eslint_with_excludes_still_uses_the_config_the_command_names(project, option, temporary):
    _write(
        project,
        {
            "eslint.config.mjs": "export default [];\n",
            "strict.mjs": 'export default [{ rules: { semi: "error" } }];\n',
            "tools/x.js": "var x = 1\n",
        },
    )
    _checkout(project / "repo", {"lib/up.js": "var u = 1\n"})
    result = _run(project, _command(f"Enforced: command eslint {option} ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    found = result.output.split("\n", 1)[1]
    assert "tools/x.js" in found and "semi" in found and "repo/lib/up.js" not in found
    assert "strict.mjs" not in result.output.splitlines()[0]
    assert 'import base from "./strict.mjs";' in _named_file(result, temporary)


def test_eslint_with_a_legacy_config_keeps_the_ignore_file_the_command_names(project, temporary, monkeypatch):
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")
    _write(
        project,
        {
            ".eslintrc.json": '{"root": true, "rules": {"semi": "error"}}\n',
            ".eslintignore": "own.js\n",
            ".myignore": "gen\n",
            "gen/g.js": "var g = 1\n",
            "own.js": "var b = 2\n",
        },
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n"})
    result = _run(project, _command("Enforced: command eslint --ignore-path .myignore ."))
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo\n"
    assert ".myignore" not in result.output.splitlines()[0]
    found = result.output.split("\n", 1)[1]
    assert "own.js" in found and "gen/g.js" not in found and "repo/lib/x.js" not in found


def test_eslint_with_a_legacy_config_reads_excludes_from_an_ignore_file(project, temporary, monkeypatch):
    monkeypatch.setenv("ESLINT_USE_FLAT_CONFIG", "false")  # ESLint 9 reads `.eslintrc` only when told to
    _write(
        project,
        {
            ".eslintrc.json": '{"root": true, "rules": {"semi": "error"}}\n',
            ".eslintignore": "gen\n",
            "gen/g.js": "var g = 1\n",
            "own.js": "var b = 2;\n",
        },
    )
    _checkout(project / "repo", {"lib/x.js": "var a = 1\n", "lib/[id].js": "var c = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo\n"
    (project / "repo/lib/x.js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert _named_file(result, temporary, "--ignore-path") == "gen\n\n/repo/lib/\\[id\\].js\n"
    found = result.output.split("\n", 1)[1]
    assert "repo/lib/x.js" in found and "[id].js" not in found and "gen/g.js" not in found


def test_ruff_excludes_an_unchanged_file_whose_name_holds_glob_characters(project):
    _checkout(project / "repo", {"pkg/a[1].py": RUFF_PY, "pkg/st*r,{x}.py": RUFF_PY, "pkg/ok.py": "x = 1\n"})
    _write(project, {"pkg/a1.py": "x = 1\n", "pkg/stXr,x.py": "x = 1\n"})
    (project / "repo/pkg/ok.py").write_text("x = 2\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.PASS, result.output
    (project / "repo/pkg/a[1].py").write_text("import sys\n")
    result = _run(project, _ruff_rules()[0][1])
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pkg/a[1].py" in result.output.split("\n", 1)[1]


def test_eslint_excludes_an_unchanged_file_whose_name_holds_glob_characters(project):
    _checkout(project / "repo", {"pages/[id].js": "var a = 1\n", "pages/ok.js": "var c = 3;\n"})
    _write(project, ESLINT_CONFIG)
    (project / "repo/pages/ok.js").write_text("var c = 4;\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.PASS, result.output
    (project / "repo/pages/[id].js").write_text("var a = 2\n")
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pages/[id].js" in result.output.split("\n", 1)[1]


def test_eslint_does_not_exclude_a_project_file_a_glob_character_would_match(project):
    _checkout(project / "repo", {"pages/[id].js": "var a = 1;\n", "pages/ok.js": "var c = 3;\n"})
    _write(project, ESLINT_CONFIG)
    (project / "repo/pages/ok.js").write_text("var c = 4;\n")
    _write(project / "repo", {"pages/i.js": "var d = 1\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "repo/pages/i.js" in result.output.split("\n", 1)[1]


def test_the_recorded_prettier_command_is_the_one_that_ran(project):
    _write(project, {"own.js": "var b = 2;\n"})
    _checkout(project / "repo", {"lib/x.js": "var   a = 1\n"})
    result = _run(project, PRETTIER)
    assert result.status is _gate_base.Status.PASS, result.output
    first = result.output.splitlines()[0]
    assert "--ignore-path .gitignore --ignore-path .prettierignore --ignore-path " + str(project) in first


@pytest.mark.parametrize("unchanged", [5, 60])
def test_eslint_lints_the_projects_files_of_any_extension_its_config_reads(project, unchanged):
    # Few unchanged directories (a wrapper config ignores them) or more than `_MAX_IGNORES` (the project's
    # files named instead): either way the config's `files`, not an extension, decide what ESLint reads, so
    # a project file only `files` matches is linted and an unchanged upstream one is not.
    upstream = {f"d{i:02}/u{i}.js": "var a = 1;\n" for i in range(unchanged)}
    _checkout(project / "repo", {**upstream, "d01/x.es6": "var u = 1\n"})
    config = 'export default [{ files: ["**/*.es6"], rules: { semi: "error" } }];\n'
    _write(
        project, {"repo/d02/u2.js": "var a = 2;\n", "eslint.config.mjs": config, "lib/x.es6": "var b = 2\n"}
    )
    _write(project, {"repo/d01/mine.es6": "var m = 2\n"})
    result = _run(project, ESLINT)
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("--config" in command) == (unchanged == 5), command[:500]
    assert "lib/x.es6" in found and "repo/d01/mine.es6" in found and "repo/d01/x.es6" not in found, found
    assert "2 problems (2 errors, 0 warnings)" in found, found


def test_eslint_with_no_config_lookup_imports_no_config_beside_excludes(project):
    _write(project, {**ESLINT_CONFIG, "tools/x.js": "var x = 1\n"})
    _checkout(project / "repo", {"lib/up.js": "var u = 1\n"})
    assert (
        _run(project, _command("Enforced: command eslint --no-config-lookup .")).status
        is _gate_base.Status.PASS
    )
    assert _run(project, ESLINT).status is _gate_base.Status.FAIL


@pytest.mark.parametrize("unchanged", [1, 3000])
def test_ruff_reads_the_files_the_projects_include_names(project, unchanged):
    # An `include` with no `extend-include` (ruff shows the empty one as `[]`), beside a checkout whose own
    # `pyproject.toml` includes something else: the project root's configuration decides, inline and with
    # the project's files named in place of `.`.
    files = {f"d02/many_{i:05}.py": "x = 1\n" for i in range(unchanged)}
    files["pyproject.toml"] = '[tool.ruff]\ninclude = ["pkg/**/*.py"]\n'
    _checkout(project / "repo", {**files, "d02/u2.py": "x = 1\n"})
    _write(
        project,
        {"repo/d02/u2.py": "x = 2\n", "ruff.toml": 'include = ["*.py", "*.pyw"]\n', "lib/x.pyw": RUFF_PY},
    )
    assert _gate_rules._list_ruff_includes(["ruff", "check", "."], project) == ("*.py", "*.pyw")
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    command, found = result.output.split("\n", 1)
    assert ("extend-exclude =" in command) == (unchanged == 1), command[:500]
    assert "lib/x.pyw" in found and "repo/" not in found, found[:2000]


def test_ruff_reads_the_projects_include_even_when_its_excludes_match_the_gates_empty_file(project):
    # The gate asks ruff about an empty root file named `tmp*.py`; a project that force-excludes such names
    # must still have its `extend-include` read, or an owned `.pyw` beside unchanged files goes unlinted.
    files = {f"d00/u{i}.py": "x = 1\n" for i in range(5)}
    _checkout(project / "repo", files)
    config = 'force-exclude = true\nextend-exclude = ["tmp*"]\nextend-include = ["*.pyw"]\n'
    _write(project, {"ruff.toml": config, "repo/d00/mine.pyw": RUFF_PY})
    assert "*.pyw" in _gate_rules._list_ruff_includes(["ruff", "check", "."], project)
    result = _run(project, _command("Enforced: command ruff check --select F401 ."))
    assert result.status is _gate_base.Status.FAIL, result.output[:2000]
    assert "repo/d00/mine.pyw" in result.output.split("\n", 1)[1], result.output[:2000]


# -- factory:ui-evidence --

UI_RULE = _command("Enforced: check factory:ui-evidence")


def _ui_status(project: Path) -> _gate_base.Status:
    results = _gates.standards_results(project, {"engineering.md": UI_RULE}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "factory:ui-evidence"]
    return result.status


def test_an_unchanged_upstream_page_is_not_the_projects_ui(project):
    _write(project, {"a.py": "x = 1\n"})
    _checkout(project / "repo", {"index.html": "<p>hi</p>\n"})
    assert _ui_status(project) is _gate_base.Status.NOT_APPLICABLE
    (project / "repo/index.html").write_text("<p>changed</p>\n")
    assert _ui_status(project) is _gate_base.Status.TOOL_UNAVAILABLE


def test_a_project_page_beside_an_unchanged_upstream_one_is_ui(project):
    _write(project, {"ui/index.html": "<p>mine</p>\n"})
    _checkout(project / "repo", {"index.html": "<p>hi</p>\n"})
    assert _ui_status(project) is _gate_base.Status.TOOL_UNAVAILABLE


# -- suppressions policy, when no base commit gives it a diff --

SUPPRESSED_PY = "x = 1  # noqa: E501\ntry:\n    pass\nexcept Exception:\n    pass\n"
LINT = _command("Enforced: lint ruff E722")


def _suppressions(project: Path) -> _gates.Result:
    results = _gates.standards_results(project, {"engineering.md": LINT}, configs=CONFIGS)
    [result] = [r for r in results if r.rule == "factory:suppressions"]
    return result


def test_a_suppression_in_an_unchanged_checkout_file_passes_without_a_base(project):
    _checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.PASS, result.output
    assert "checked every line: no base commit to diff against" in result.output
    assert "Nested git checkouts judged against their base: repo (base " in result.output


def test_a_suppression_in_a_changed_checkout_file_or_a_project_file_still_fails_without_a_base(project):
    _checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY})
    _write(project / "repo", {"pkg/a.py": SUPPRESSED_PY + "# changed\n"})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "repo/pkg/a.py:1" in result.output
    _git(project / "repo", "checkout", "-q", "--", ".")
    _write(project, {"mine.py": SUPPRESSED_PY})
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "mine.py:1" in result.output
    assert "repo/pkg/a.py" not in result.output.split("Nested git")[0]


def test_a_suppression_in_a_checkout_with_no_known_base_is_judged_as_the_projects(project):
    _checkout(project / "repo", {"pkg/a.py": SUPPRESSED_PY}, branch="topic")
    result = _suppressions(project)
    assert result.status is _gate_base.Status.FAIL and "repo/pkg/a.py:1" in result.output
    assert "base unknown, so its files are judged as the project's" in result.output
