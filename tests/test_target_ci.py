"""A project that delivers to another repo is held to that repo's own CI and patch coverage."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _contract,
    _delivery,
    _gate_base,
    _gates,
    _instructions,
    _session_config,
    _target_ci,
    _target_run,
    _timeline,
)

pytestmark = pytest.mark.real_factory_gates

BRIEF = "Add clamp to calc.\nDelivers to: acme/widgets at widgets\n"

WORKFLOW = """\
name: ci
on:
  pull_request:
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: pip install -e .
      - name: Lint
        run: make lint
      - name: Types
        run: |
          make typecheck
  greet:
    runs-on: ubuntu-latest
    steps:
      - name: Say hello
        run: echo hello
"""

NIGHTLY = """\
name: nightly
on: schedule
jobs:
  slow:
    runs-on: ubuntu-latest
    steps:
      - run: make nightly-test
"""

MAKEFILE = """\
.PHONY: lint typecheck test coverage

lint:
\tpython check_style.py

typecheck:
\tpython -c "print('typed')"

test:
\tpython -m pytest -q

coverage:
\tpython -m coverage run --branch -m pytest -q
"""

CHECK_STYLE = """\
import pathlib
import sys

bad = [p for p in pathlib.Path(".").glob("*.py") if "FIXME" in p.read_text() and p.name != "check_style.py"]
sys.exit(f"FIXME left in {bad[0]}" if bad else 0)
"""

CALC = "def sign(x):\n    if x < 0:\n        return -1\n    return 1\n"
CALC_TEST = (
    "from calc import sign\n\n\ndef test_sign():\n    assert sign(-2) == -1\n    assert sign(2) == 1\n"
)


@pytest.fixture(autouse=True)
def _project_python_first(monkeypatch):
    """Fixture targets run `python ...`: this interpreter, as under `uv run`, not another on PATH."""
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")


def git(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _build_delivery(root):
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "test")
    (root / "app").mkdir()
    (root / "app/NOTES.md").write_text("notes\n")
    git(root, "add", "app")
    git(root, "commit", "-m", "base")
    (root / ".git/info/exclude").write_text("app/widgets/\n")
    checkout = root / "app/widgets"
    (checkout / ".github/workflows").mkdir(parents=True)
    (checkout / ".github/workflows/ci.yml").write_text(WORKFLOW)
    (checkout / ".github/workflows/nightly.yml").write_text(NIGHTLY)
    (checkout / "Makefile").write_text(MAKEFILE)
    (checkout / "check_style.py").write_text(CHECK_STYLE)
    (checkout / "calc.py").write_text(CALC)
    (checkout / "test_calc.py").write_text(CALC_TEST)
    git(checkout, "init", "-b", "main")
    git(checkout, "config", "user.email", "test@example.com")
    git(checkout, "config", "user.name", "test")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "upstream")
    git(checkout, "checkout", "-b", "feature")


@pytest.fixture(scope="module")
def _delivery_template(tmp_path_factory):
    """The delivery workspace built once (two `git init`s and their commits); each test gets a copy."""
    root = tmp_path_factory.mktemp("delivery-template")
    _build_delivery(root)
    return root


@pytest.fixture
def delivery(tmp_path, _delivery_template):
    """A workspace whose project `app` holds a checkout of the target repo, branched off `main`."""
    shutil.copytree(_delivery_template, tmp_path, symlinks=True, dirs_exist_ok=True)
    _gates.capture_base(tmp_path, "app")
    return tmp_path, tmp_path / "app/widgets"


def gate(workspace, brief=BRIEF):
    """Run the DONE gate; return its blocking text and the recorded status of each rule."""
    problem = _gates.run(workspace, "app", brief, "", "DONE", {}, _timeline._Timeline())
    recorded = json.loads((workspace / ".claude-flyte/app/standards-results.json").read_text())
    return problem, {r["rule"]: r for r in recorded}


def coverage(workspace, checkout):
    """The patch-coverage verdict alone; the full gate around it is covered by the tests that call `gate`."""
    result = _gates._target_coverage_result(workspace / "app", checkout)
    return {"status": result.status.value, "output": result.output}


def change(checkout, calc, test):
    (checkout / "calc.py").write_text(calc)
    (checkout / "test_calc.py").write_text(test)
    git(checkout, "commit", "--allow-empty", "-am", "change")


def test_target_is_parsed_from_the_brief():
    assert _target_ci.parse_target(BRIEF) == _target_ci.Target("acme/widgets", "widgets")
    assert _target_ci.parse_target("- Delivers to: https://github.com/a/b.git\n") == _target_ci.Target(
        "https://github.com/a/b.git", None
    )
    assert _target_ci.parse_target("An ordinary brief.\n") is None


def test_discovery_merges_workflow_steps_and_build_file_rules(delivery):
    _, checkout = delivery
    found = _target_ci.discover(checkout)
    assert [c.command for c in found] == ["make lint", "make typecheck", "make test"]
    lint, typecheck, test = found
    assert lint.origin == ".github/workflows/ci.yml:12, Makefile:3"
    assert typecheck.origin.startswith(".github/workflows/ci.yml:")
    assert test.origin == "Makefile:9"


def test_discovery_without_ci_files_finds_nothing(tmp_path):
    assert _target_ci.discover(tmp_path) == []


def test_failing_target_lint_fails_pre_review(delivery):
    workspace, checkout = delivery
    change(checkout, CALC + "# FIXME\n", CALC_TEST)
    problem, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "FAIL"
    assert "make lint" in problem and "FIXME left in" in problem


def test_passing_target_lint_passes(delivery):
    workspace, checkout = delivery
    change(checkout, CALC, CALC_TEST)
    problem, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "PASS"
    assert "factory:target-ci" not in problem


COVERED_CLAMP = CALC + "\n\ndef clamp(x, hi):\n    if x > hi:\n        return hi\n    return x\n"
CLAMP_TEST = CALC_TEST + "\n\ndef test_clamp():\n    assert clamp(1, 5) == 1\n"


def test_uncovered_changed_line_fails_coverage(delivery):
    workspace, checkout = delivery
    change(checkout, COVERED_CLAMP, CLAMP_TEST.replace("import sign", "import clamp, sign"))
    problem, rules = gate(workspace)
    assert rules["factory:target-coverage"]["status"] == "FAIL"
    assert "calc.py:9" in rules["factory:target-coverage"]["output"]
    assert "calc.py:9" in problem


def test_untaken_branch_of_a_changed_line_fails_coverage(delivery):
    workspace, checkout = delivery
    calc = CALC + "\n\ndef note(flag):\n    out = 0\n    if flag:\n        out = 1\n    return out\n"
    test = (
        CALC_TEST.replace("import sign", "import note, sign")
        + "\n\ndef test_note():\n    assert note(1) == 1\n"
    )
    change(checkout, calc, test)
    rules = {"factory:target-coverage": coverage(workspace, checkout)}
    assert rules["factory:target-coverage"]["status"] == "FAIL"
    assert "calc.py:9" in rules["factory:target-coverage"]["output"]


def test_listed_uncovered_line_passes_coverage(delivery):
    workspace, checkout = delivery
    change(checkout, COVERED_CLAMP, CLAMP_TEST.replace("import sign", "import clamp, sign"))
    (workspace / "app/SUMMARY.md").write_text(
        "# Summary\n\n## Uncovered changed lines\n\n- calc.py:9 -- defensive exit, no input reaches it\n"
    )
    problem, rules = gate(workspace)
    assert rules["factory:target-coverage"]["status"] == "PASS"
    assert "factory:target-coverage" not in problem


def test_listed_line_needs_a_reason(delivery):
    workspace, checkout = delivery
    change(checkout, COVERED_CLAMP, CLAMP_TEST.replace("import sign", "import clamp, sign"))
    (workspace / "app/SUMMARY.md").write_text("## Uncovered changed lines\n\ncalc.py:9 --\n")
    rules = {"factory:target-coverage": coverage(workspace, checkout)}
    assert rules["factory:target-coverage"]["status"] == "FAIL"


def test_fully_covered_change_passes_coverage(delivery):
    workspace, checkout = delivery
    test = CLAMP_TEST.replace("import sign", "import clamp, sign") + "    assert clamp(9, 5) == 5\n"
    change(checkout, COVERED_CLAMP, test)
    rules = {"factory:target-coverage": coverage(workspace, checkout)}
    assert rules["factory:target-coverage"]["status"] == "PASS"


# A test that execs code compiled under a made-up filename (as a docs test does) leaves coverage a file with
# no source; the report must still be read, and the change judged on its own lines.
PHANTOM = "\n\ndef test_phantom():\n    exec(compile('x = 1', 'phantom-probe', 'exec'), {})\n"


def test_a_test_that_execs_a_phantom_file_does_not_break_the_coverage_report(delivery):
    workspace, checkout = delivery
    test = CLAMP_TEST.replace("import sign", "import clamp, sign") + "    assert clamp(9, 5) == 5\n" + PHANTOM
    change(checkout, COVERED_CLAMP, test)
    assert coverage(workspace, checkout)["status"] == "PASS"
    change(checkout, COVERED_CLAMP, CLAMP_TEST.replace("import sign", "import clamp, sign") + PHANTOM)
    result = coverage(workspace, checkout)
    assert result["status"] == "FAIL" and "calc.py:9" in result["output"]


UNIMPORTED = "def pick(x):\n    if x:\n        return 1\n    return 2\n"


def test_a_new_module_no_test_imports_fails_coverage(delivery):
    workspace, checkout = delivery
    (checkout / "b.py").write_text(UNIMPORTED)
    change(checkout, CALC, CALC_TEST)
    git(checkout, "add", "b.py")
    git(checkout, "commit", "-m", "add b")
    result = coverage(workspace, checkout)
    assert result["status"] == "FAIL"
    assert "b.py:1, b.py:2, b.py:3, b.py:4" in result["output"]


def test_a_new_module_a_test_imports_and_runs_passes_coverage(delivery):
    workspace, checkout = delivery
    (checkout / "b.py").write_text(UNIMPORTED)
    (checkout / "test_b.py").write_text(
        "from b import pick\n\n\ndef test_pick():\n    assert pick(1) == 1\n    assert pick(0) == 2\n"
    )
    change(checkout, CALC, CALC_TEST)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "add b")
    assert coverage(workspace, checkout)["status"] == "PASS"


def test_a_changed_module_outside_the_report_is_listed_like_any_uncovered_line(delivery):
    workspace, checkout = delivery
    (checkout / "b.py").write_text(UNIMPORTED)
    change(checkout, CALC, CALC_TEST)
    git(checkout, "add", "b.py")
    git(checkout, "commit", "-m", "add b")
    rows = "".join(f"- b.py:{n} -- not used yet\n" for n in (1, 2, 3, 4))
    (workspace / "app/SUMMARY.md").write_text("## Uncovered changed lines\n" + rows)
    assert coverage(workspace, checkout)["status"] == "PASS"


def test_ordinary_project_runs_neither_check(delivery):
    workspace, checkout = delivery
    change(checkout, CALC + "# FIXME\n", CALC_TEST)
    _, rules = gate(workspace, brief="Add clamp to calc.\n")
    assert not [rule for rule in rules if rule.startswith("factory:target-")]


def test_declared_target_without_checkout_fails(delivery):
    workspace, _ = delivery
    problem, rules = gate(workspace, brief="Delivers to: acme/widgets at missing\n")
    assert rules["factory:target-ci"]["status"] == "FAIL"
    assert "missing" in problem


def test_instructions_carry_default_criteria_only_for_a_target(monkeypatch):
    monkeypatch.setattr(_session_config, "_project_brief", "Delivers to: acme/widgets\n")
    memory = _instructions._project_memory(False, name="app")
    assert "target repo CI green locally" in memory
    assert "100% branch coverage of changed lines" in memory
    monkeypatch.setattr(_session_config, "_project_brief", "An ordinary brief.\n")
    memory = _instructions._project_memory(False, name="app")
    assert "target repo CI green locally" not in memory


def test_contract_prompt_names_the_discovered_commands(delivery, monkeypatch):
    workspace, _ = delivery
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "_project_name", "app")
    monkeypatch.setattr(_session_config, "_project_brief", BRIEF)
    prompt = _contract._contract_prompt(BRIEF, "")
    assert "`make lint`" in prompt and "`make typecheck`" in prompt and "`make test`" in prompt
    assert "make lint" not in _contract._contract_prompt("An ordinary brief.\n", "")


def _as_project(monkeypatch, workspace):
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "_project_name", "app")
    monkeypatch.setattr(_session_config, "_project_brief", BRIEF)


def test_contract_facts_come_from_the_scratch_checkout_of_a_delivery_project(delivery, monkeypatch):
    workspace, checkout = delivery
    _as_project(monkeypatch, workspace)
    (workspace / "app/promote.toml").write_text('repo = "acme/widgets"\n')
    used = []

    @contextlib.contextmanager
    def scratch(project, delivery_):
        used.append((project, delivery_.repo))
        yield checkout

    monkeypatch.setattr(_delivery, "target_checkout", scratch)
    facts = _contract._target_ci_facts(BRIEF)
    assert used == [(workspace / "app", "acme/widgets")]
    assert "this project delivers to acme/widgets" in facts
    assert "`make lint`" in facts and "`make test`" in facts


def test_contract_facts_are_left_out_when_the_manifest_names_another_repo(delivery, monkeypatch):
    workspace, _ = delivery
    _as_project(monkeypatch, workspace)
    (workspace / "app/promote.toml").write_text('repo = "acme/elsewhere"\n')
    assert _contract._target_ci_facts(BRIEF) == ""


def test_contract_facts_are_left_out_when_the_target_has_no_checkout(delivery, monkeypatch):
    workspace, checkout = delivery
    _as_project(monkeypatch, workspace)
    shutil.rmtree(checkout)
    assert _contract._target_ci_facts(BRIEF) == ""


def _only_workflow(checkout, steps, *, job_extra=""):
    """Replace the target's CI with one job `lint` holding `steps`, and add the file `pkg/broken`."""
    (checkout / ".github/workflows/ci.yml").write_text(
        f"name: ci\non:\n  pull_request:\njobs:\n  lint:\n    runs-on: ubuntu-latest\n{job_extra}"
        f"    steps:\n      - name: Lint\n{steps}"
    )
    (checkout / "Makefile").unlink()
    (checkout / "pkg").mkdir()
    (checkout / "pkg/broken").write_text("")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "ci")


def _block(*rows):
    return "        run: |\n" + "".join(f"          {row}\n" for row in rows)


STEP_CASES = [
    pytest.param(
        _block("if [ -f .github/workflows/ci.yml ]; then", "  true", "fi"), "", "PASS", id="multi-line-if"
    ),
    pytest.param(_block("cd pkg", "test ! -e broken"), "", "FAIL", id="cd-carries-over"),
    pytest.param(
        _block("export STRICT=1", 'test -z "$STRICT" || test ! -e pkg/broken'), "", "FAIL", id="export"
    ),
    pytest.param(
        _block('test -z "$STRICT" || test ! -e pkg/broken'),
        "    env:\n      STRICT: '1'\n",
        "FAIL",
        id="job-env",
    ),
]


@pytest.mark.parametrize(("steps", "job_extra", "expected"), STEP_CASES)
def test_a_workflow_step_runs_as_one_script(delivery, steps, job_extra, expected):
    workspace, checkout = delivery
    _only_workflow(checkout, steps, job_extra=job_extra)
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == expected


def test_step_env_and_working_directory_apply(delivery):
    workspace, checkout = delivery
    steps = "        working-directory: pkg\n        env:\n          STRICT: '1'\n" + _block(
        'test -z "$STRICT" || test ! -e broken'
    )
    _only_workflow(checkout, steps)
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "FAIL"


def test_a_step_with_an_expression_cannot_run_offline(delivery):
    workspace, checkout = delivery
    _only_workflow(checkout, _block("test -n '${{ steps.x.outputs.y }}'"))
    problem, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "FAIL"
    assert "cannot run offline" in rules["factory:target-ci"]["output"]
    assert "cannot run offline" in problem


@pytest.mark.parametrize("jobs", ["[unclosed", "[lint]", "{lint: [x]}"])
def test_malformed_workflow_is_a_failed_result_not_a_crash(delivery, jobs):
    workspace, checkout = delivery
    (checkout / ".github/workflows/ci.yml").write_text(f"name: ci\non:\n  pull_request:\njobs: {jobs}\n")
    git(checkout, "commit", "-am", "break ci")
    # Through the whole gate: what this pins is that `_gates.run` survives and still records its results.
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "FAIL"
    assert "cannot read .github/workflows/ci.yml" in rules["factory:target-ci"]["output"]


def _named(name, run_rows, extra=""):
    return f"      - name: {name}\n{extra}" + _block(*run_rows)


def _only_steps(checkout, steps, *, job_extra="", top=""):
    """Replace the target's CI with one job `lint` holding the complete `steps` text."""
    (checkout / ".github/workflows/ci.yml").write_text(
        f"name: ci\non:\n  pull_request:\n{top}jobs:\n  lint:\n    runs-on: ubuntu-latest\n{job_extra}"
        f"    steps:\n{steps}"
    )
    (checkout / "Makefile").unlink(missing_ok=True)
    git(checkout, "add", "-A")
    git(checkout, "commit", "--allow-empty", "-m", "ci")


def _ci_status(delivery, steps, **kwargs):
    """The target-CI verdict alone; the full gate around it is covered by the tests that call `gate`."""
    _, checkout = delivery
    _only_steps(checkout, steps, **kwargs)
    result = _gates.target_ci_result(checkout)
    return {"status": result.status.value, "output": result.output}


@pytest.mark.parametrize(
    "rows",
    [["echo checking && exit 1"], ['echo "running ruff" && false'], ["export A=1", "exit 1"]],
    ids=["echo-exit", "echo-then-fail", "export-then-fail"],
)
def test_a_check_by_name_is_not_skipped_as_setup(delivery, rows):
    assert _ci_status(delivery, _named("lint", rows))["status"] == "FAIL"


def test_a_step_that_only_installs_is_setup(delivery):
    steps = _named("Prepare", ["pip install nothing-that-exists-xyz"]) + _named("lint", ["true"])
    assert _ci_status(delivery, steps)["status"] == "PASS"


def test_a_setup_named_step_is_setup_even_if_it_mentions_tests(delivery):
    assert _ci_status(delivery, _named("Install test deps", ["exit 1"]))["status"] == "not applicable"


def test_sh_shell_has_no_pipefail(delivery):
    steps = _named("lint", ["false | true"], "        shell: sh\n")
    assert _ci_status(delivery, steps)["status"] == "PASS"
    assert _ci_status(delivery, _named("lint", ["false | true"]))["status"] == "FAIL"


def test_sh_shell_stops_at_the_first_error(delivery):
    assert _ci_status(delivery, _named("lint", ["false", "true"], "        shell: sh\n"))["status"] == "FAIL"


def test_python_shell_runs_the_script_with_python(delivery):
    ok = _named("lint", ["import sys", "sys.exit(0)"], "        shell: python\n")
    bad = _named("lint", ["import sys", "sys.exit(3)"], "        shell: python\n")
    assert _ci_status(delivery, ok)["status"] == "PASS"
    assert _ci_status(delivery, bad)["status"] == "FAIL"


@pytest.mark.parametrize("shell", ["pwsh", "powershell", "cmd", "fish {0}"])
def test_other_shells_cannot_run_offline(delivery, shell):
    result = _ci_status(delivery, _named("lint", ["true"], f"        shell: {shell}\n"))
    assert result["status"] == "FAIL"
    assert f"cannot run offline: shell {shell}" in result["output"]


def test_job_and_workflow_default_shell_apply(delivery):
    job = _ci_status(
        delivery, _named("lint", ["false | true"]), job_extra="    defaults:\n      run:\n        shell: sh\n"
    )
    assert job["status"] == "PASS"
    top = _ci_status(delivery, _named("lint", ["true"]), top="defaults:\n  run:\n    shell: pwsh\n")
    assert "cannot run offline: shell pwsh" in top["output"]
    # A job default beats the workflow default, a step's own shell beats both.
    both = _ci_status(
        delivery,
        _named("lint", ["true"]),
        job_extra="    defaults:\n      run:\n        shell: bash\n",
        top="defaults:\n  run:\n    shell: pwsh\n",
    )
    assert both["status"] == "PASS"


def test_continue_on_error_step_is_not_a_required_check(delivery):
    steps = _named("lint", ["exit 1"], "        continue-on-error: true\n") + _named("mypy", ["true"])
    result = _ci_status(delivery, steps)
    assert result["status"] == "PASS"
    assert "skipped" in result["output"]
    assert "continue-on-error" in result["output"]


def test_continue_on_error_job_is_skipped(delivery):
    result = _ci_status(delivery, _named("lint", ["exit 1"]), job_extra="    continue-on-error: true\n")
    assert result["status"] == "not applicable"


def test_discover_leaves_out_continue_on_error(delivery):
    _, checkout = delivery
    _only_steps(checkout, _named("lint", ["exit 1"], "        continue-on-error: true\n"))
    assert _target_ci.discover(checkout) == []


def test_job_if_false_excludes_the_job(delivery):
    result = _ci_status(delivery, _named("lint", ["exit 1"]), job_extra="    if: false\n")
    assert result["status"] == "not applicable"


@pytest.mark.parametrize("condition", ["true", "always()", "success()"])
def test_literal_true_conditions_run_normally(delivery, condition):
    steps = _named("lint", ["exit 1"], f"        if: {condition}\n")
    assert _ci_status(delivery, steps)["status"] == "FAIL"
    result = _ci_status(delivery, _named("lint", ["true"]), job_extra=f"    if: {condition}\n")
    assert result["status"] == "PASS"


@pytest.mark.parametrize("where", ["step", "job"])
def test_other_if_expressions_are_decided_for_a_pull_request(delivery, where):
    # Superseded: GitHub evaluates these for a pull request, so they are listed as skipped, not failed.
    if where == "step":
        result = _ci_status(delivery, _named("lint", ["true"], "        if: github.ref == 'refs/heads/x'\n"))
    else:
        result = _ci_status(
            delivery, _named("lint", ["true"]), job_extra="    if: github.event_name == 'push'\n"
        )
    assert result["status"] == "not applicable"
    assert "its if is false for a pull request" in result["output"]


def test_env_values_are_stringified_as_github_does(delivery):
    # Not YES/NO: PyYAML reads those bare keys as booleans.
    env = (
        "        env:\n          EMPTY:\n          FLAG_T: true\n          FLAG_F: false\n          NUM: 1\n"
    )
    script = [
        'test "${EMPTY-unset}" = ""',
        'test "$FLAG_T" = true',
        'test "$FLAG_F" = false',
        'test "$NUM" = 1',
    ]
    assert _ci_status(delivery, _named("lint", script, env))["status"] == "PASS"


@pytest.mark.parametrize("filename", ["a b.py", "ü.py"])
def test_changed_lines_of_odd_filenames(delivery, filename):
    _, checkout = delivery
    (checkout / filename).write_text("x = 1\ny = 2\n")
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "odd")
    base = git(checkout, "rev-parse", "HEAD~1")
    changed = _gates.changed_lines(checkout, base)
    assert changed is not None
    assert changed[filename] == {1, 2}


@pytest.mark.parametrize("filename", ["a b.py", "ü.py"])
def test_uncovered_line_in_an_odd_filename_fails_coverage(delivery, filename):
    workspace, checkout = delivery
    change(checkout, CALC, CALC_TEST)
    module = filename.removesuffix(".py")
    (checkout / filename).write_text("def f(x):\n    if x:\n        return 1\n    return 0\n")
    (checkout / "test_odd.py").write_text(
        f"import importlib\n\n\ndef test_f():\n    assert importlib.import_module({module!r}).f(0) == 0\n"
    )
    git(checkout, "add", ".")
    git(checkout, "commit", "-m", "odd")
    rules = {"factory:target-coverage": coverage(workspace, checkout)}
    assert rules["factory:target-coverage"]["status"] == "FAIL"
    assert f"{filename}:3" in rules["factory:target-coverage"]["output"]


def _workflow(checkout, text):
    """Replace the target's CI with the complete workflow `text`."""
    (checkout / ".github/workflows/ci.yml").write_text(text)
    (checkout / "Makefile").unlink(missing_ok=True)
    git(checkout, "add", "-A")
    git(checkout, "commit", "--allow-empty", "-m", "ci")


def test_matrix_values_come_from_the_first_combination(delivery):
    steps = "      - name: lint\n        if: ${{ matrix.py != '3.11' }}\n        run: 'true'\n"
    job_extra = "    strategy:\n      matrix:\n        py: ['3.12']\n"
    assert _ci_status(delivery, steps, job_extra=job_extra)["status"] == "PASS"
    _, checkout = delivery
    assert "[py=3.12]" in _target_ci.discover(checkout)[0].origin


def test_matrix_expansion_in_a_script(delivery):
    job_extra = (
        "    strategy:\n      matrix:\n        py: ['3.12', '3.13']\n        include:\n          - flag: on\n"
    )
    steps = _named("lint", ["test '${{ matrix.py }}' = 3.12", "test '${{ matrix.flag }}' = true"])
    assert _ci_status(delivery, steps, job_extra=job_extra)["status"] == "PASS"
    job_extra = "    strategy:\n      matrix: ${{ fromJSON(needs.x.outputs.m) }}\n"
    result = _ci_status(delivery, _named("lint", ["test -n '${{ matrix.py }}'"]), job_extra=job_extra)
    assert result["status"] == "not applicable"
    assert "its matrix cannot be decided offline" in result["output"]


def test_secrets_in_workflow_env_are_empty_strings(delivery):
    top = "env:\n  TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
    assert _ci_status(delivery, _named("Lint", ['test -z "$TOKEN"']), top=top)["status"] == "PASS"


def _filtered(paths):
    return (
        f"name: ci\non:\n  pull_request:\n    paths: {paths}\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - name: Lint\n        run: exit 1\n"
    )


def test_a_paths_filter_that_misses_the_change_leaves_the_workflow_out(delivery):
    workspace, checkout = delivery
    change(checkout, CALC + "\n", CALC_TEST)
    _workflow(checkout, _filtered("[release.yml]"))
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "not applicable"


def test_a_paths_filter_that_matches_keeps_the_workflow(delivery):
    workspace, checkout = delivery
    change(checkout, CALC + "\n", CALC_TEST)
    _workflow(checkout, _filtered("['**.py']"))
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "FAIL"


@pytest.mark.parametrize(
    ("paths", "kept"),
    [
        ("['*.py']", True),
        ("['src/*.py']", False),
        ("['**/calc.py']", True),
        ("['pkg/**']", False),
        ("['*.py', '!calc.py']", False),
    ],
)
def test_paths_use_github_globs(delivery, paths, kept):
    workspace, checkout = delivery
    change(checkout, CALC + "\n", CALC_TEST)
    _workflow(checkout, _filtered(paths))
    _, rules = gate(workspace)
    # The commit that rewrites ci.yml is part of the change too, so a pattern for `*.yml` would match.
    assert rules["factory:target-ci"]["status"] == ("FAIL" if kept else "not applicable")


def test_paths_ignore_drops_a_workflow_that_only_sees_ignored_files(delivery):
    workspace, checkout = delivery
    change(checkout, CALC + "\n", CALC_TEST)
    text = _filtered("[x]").replace(
        "    paths: [x]\n", "    paths-ignore: ['*.py', 'Makefile', '.github/**']\n"
    )
    _workflow(checkout, text)
    _, rules = gate(workspace)
    assert rules["factory:target-ci"]["status"] == "not applicable"


def test_a_job_gated_on_another_event_is_skipped_not_failed(delivery):
    steps = _named("lint", ["exit 1"], "        if: github.event_name == 'release'\n")
    result = _ci_status(delivery, steps)
    assert result["status"] == "not applicable"
    assert "skipped `exit 1`" in result["output"]
    ran = _ci_status(
        delivery, _named("lint", ["exit 1"], "        if: github.event_name == 'pull_request'\n")
    )
    assert ran["status"] == "FAIL"


def test_a_tags_only_push_workflow_is_not_a_pull_request_check(delivery):
    workspace, checkout = delivery
    steps = "    steps:\n      - name: Lint\n        run: exit 1\n"
    head = "name: ci\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
    _workflow(checkout, "on:\n  push:\n    tags: ['v*']\n" + head + steps)
    assert _target_ci.discover(checkout) == []
    _workflow(checkout, "on:\n  push:\n    branches: [release]\n" + head + steps)
    assert _target_ci.discover(checkout) == []
    _workflow(checkout, "on:\n  push:\n    branches: [main]\n" + head + steps)
    assert [c.command for c in _target_ci.discover(checkout)] == ["exit 1"]
    _workflow(checkout, "on: push\n" + head + steps)
    assert [c.command for c in _target_ci.discover(checkout)] == ["exit 1"]


def test_an_undecidable_if_is_reported_as_skipped(delivery):
    steps = _named("lint", ["exit 1"], "        if: ${{ hashFiles('x') != '' }}\n")
    result = _ci_status(delivery, steps)
    assert result["status"] == "not applicable"
    assert "cannot be decided offline" in result["output"]


@pytest.mark.parametrize(
    ("condition", "runs"),
    [
        ("github.event_name == 'pull_request' && !startsWith(github.head_ref, 'x')", True),
        ("(github.event_name == 'push' || github.event_name == 'release') && always()", False),
        ("contains(github.repository, 'nothing') || endsWith(github.base_ref, 'nin')", False),
        ("failure() || cancelled()", False),
        ("github.event_name != 'push' || hashFiles('x') != ''", True),
        ("github.event_name == 'push' && hashFiles('x') != ''", False),
    ],
)
def test_if_operators(delivery, condition, runs):
    result = _ci_status(delivery, _named("lint", ["exit 1"], f"        if: {condition}\n"))
    assert result["status"] == ("FAIL" if runs else "not applicable")


def test_coverage_command_ignores_an_artifact_step_beside_the_coverage_run(delivery):
    _, checkout = delivery
    upload = _uses("actions/upload-artifact@v4", name="coverage", path="coverage.xml")
    _only_steps(checkout, upload + _named("cov", ["pytest --cov=calc"]))
    assert _target_ci.coverage_command(checkout).command == "pytest --cov=calc"
    _only_steps(checkout, upload)
    assert _target_ci.coverage_command(checkout).command == _target_ci.FALLBACK_COVERAGE


def test_coverage_command_skips_an_unrunnable_step(delivery):
    _, checkout = delivery
    unrunnable = _named("cov", ["pytest --cov ${{ steps.x.outputs.y }}"])
    _only_steps(checkout, unrunnable)
    assert _target_ci.coverage_command(checkout).command == _target_ci.FALLBACK_COVERAGE
    _only_steps(checkout, unrunnable + _named("cov2", ["pytest --cov=calc"]))
    assert _target_ci.coverage_command(checkout).command == "pytest --cov=calc"


def test_a_shell_template_runs_as_given(delivery):
    as_given = _named("lint", ["false | true"], "        shell: bash {0}\n")
    assert _ci_status(delivery, as_given)["status"] == "PASS"
    strict = _named("lint", ["false | true"], "        shell: bash -eo pipefail {0}\n")
    assert _ci_status(delivery, strict)["status"] == "FAIL"


def test_github_env_carries_to_the_next_step(delivery):
    steps = _named(
        "lint", ['echo "STRICT=1" >> "$GITHUB_ENV"', 'echo "$PWD/pkg" >> "$GITHUB_PATH"']
    ) + _named("mypy", ['test "$STRICT" = 1', 'case "$PATH" in "$PWD/pkg":*) ;; *) exit 1 ;; esac'])
    assert _ci_status(delivery, steps)["status"] == "PASS"
    # Another job does not see it.
    other = _named("mypy", ['test -z "$STRICT"'])
    steps += "  other:\n    runs-on: ubuntu-latest\n    steps:\n" + other
    assert _ci_status(delivery, steps)["status"] == "PASS"


def test_format_then_fail_on_a_diff_fails_and_leaves_the_checkout_alone(delivery):
    """GitHub runs both steps in order and the second goes red; the gate must not pass nor edit the files."""
    _, checkout = delivery
    steps = _named("Format", ["make fmt"]) + _named(
        "Fail if the formatter changed anything", ["git diff --exit-code"]
    )
    _only_steps(checkout, steps)
    (checkout / "Makefile").write_text('fmt:\n\tsed -i "s/BAD/good/" a.txt\n')
    (checkout / "a.txt").write_text("BAD\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "feature")
    result = _gates.target_ci_result(checkout)
    assert result.status.value == "FAIL"
    assert "git diff --exit-code" in result.output
    assert git(checkout, "status", "--porcelain") == ""
    assert (checkout / "a.txt").read_text() == "BAD\n"


def test_a_check_step_may_depend_on_an_earlier_step_that_is_not_one(delivery):
    steps = _named("Generate", ["echo generated > gen.txt"]) + _named("lint", ["test -f gen.txt"])
    assert _ci_status(delivery, steps)["status"] == "PASS"
    # The job's own work is not in the next job's fresh copy, nor in the session's checkout.
    other = _named("mypy", ["test ! -e gen.txt"])
    steps += "  other:\n    runs-on: ubuntu-latest\n    steps:\n" + other
    assert _ci_status(delivery, steps)["status"] == "PASS"
    assert not (delivery[1] / "gen.txt").exists()


def test_a_job_without_a_check_step_is_not_run(delivery):
    assert _ci_status(delivery, _named("Deploy", ["exit 1"]))["status"] == "not applicable"


def test_a_step_runs_in_the_first_combination_whose_if_holds(delivery):
    """GitHub runs a matrix-gated step in the combinations it allows, so it is a check, not a skip."""
    job_extra = "    strategy:\n      matrix:\n        py: ['3.10', '3.11', '3.12']\n"
    steps = "      - name: Run tests\n        if: ${{ matrix.py == '3.11' }}\n        run: exit 1\n"
    result = _ci_status(delivery, steps, job_extra=job_extra)
    assert result["status"] == "FAIL"
    _, checkout = delivery
    assert _target_ci.discover(checkout)[0].origin.endswith("[py=3.11]")


def test_excluded_combinations_are_not_run_and_includes_add_one(delivery):
    job_extra = (
        "    strategy:\n      matrix:\n        py: ['3.10', '3.11']\n        os: [linux]\n"
        "        exclude:\n          - py: '3.11'\n"
        "        include:\n          - py: '3.13'\n            os: mac\n"
    )
    steps = (
        "      - name: Run tests\n        if: ${{ matrix.py != '3.10' }}\n"
        "        run: test '${{ matrix.os }}' = mac\n"
    )
    assert _ci_status(delivery, steps, job_extra=job_extra)["status"] == "PASS"
    _, checkout = delivery
    assert _target_ci.discover(checkout)[0].origin.endswith("[py=3.13,os=mac]")


def test_a_step_whose_if_is_false_in_every_combination_is_skipped(delivery):
    job_extra = "    strategy:\n      matrix:\n        py: ['3.10', '3.12']\n"
    steps = "      - name: Run tests\n        if: ${{ matrix.py == '3.11' }}\n        run: exit 1\n"
    result = _ci_status(delivery, steps, job_extra=job_extra)
    assert result["status"] == "not applicable"
    assert "its if is false for a pull request" in result["output"]


def test_runner_temp_and_workspace_expressions_are_the_jobs_own_paths(delivery):
    # The engine's own CI writes pytest's junit under `${{ runner.temp }}` and reads it from `$RUNNER_TEMP`.
    steps = (
        "      - name: pytest\n        env:\n          OUT: ${{ runner.temp }}/junit/fast.xml\n"
        "        working-directory: ${{ github.workspace }}\n"
        + _block('mkdir -p "$(dirname "$OUT")" && echo ok > "$OUT" && test -f a.txt')
        + _named("lint the junit", ['test "$(cat "$RUNNER_TEMP/junit/fast.xml")" = ok'])
        + _named("ruff", ['test "${{ runner.temp }}" = "$RUNNER_TEMP" && test "$PWD" = "$GITHUB_WORKSPACE"'])
    )
    _, checkout = delivery
    (checkout / "a.txt").write_text("x\n")
    status = _ci_status(delivery, steps)
    assert status["status"] == "PASS", status["output"]
    assert "cannot run offline" not in status["output"]


# -- jobs: their order, their timeouts, and the files they pass on --


def _job(name, steps, extra=""):
    return f"  {name}:\n    runs-on: ubuntu-latest\n{extra}    steps:\n{steps}"


def _jobs(checkout, *jobs):
    """Replace the target's CI with one pull-request workflow of the complete `jobs` texts."""
    _workflow(checkout, "name: ci\non:\n  pull_request:\njobs:\n" + "".join(jobs))


def _uses(action, **inputs):
    rows = "".join(f"          {key}: {value}\n" for key, value in inputs.items())
    return f"      - uses: {action}\n" + (f"        with:\n{rows}" if rows else "")


def test_a_step_runs_under_its_own_or_its_jobs_timeout_minutes(delivery):
    _, checkout = delivery
    _jobs(
        checkout,
        _job("a", _named("lint", ["true"], "        timeout-minutes: 2\n"), "    timeout-minutes: 45\n"),
        _job("b", _named("lint", ["true; true"]), "    timeout-minutes: 45\n"),
        _job("c", _named("lint", ["true; true; true"])),
        _job("d", _named("lint", [":"], "        timeout-minutes: ${{ matrix.t }}\n")),
    )
    assert [c.timeout for c in _target_ci.discover(checkout)] == [120, 2700, None, None]


def test_a_fractional_timeout_minutes_written_as_a_string_is_read(delivery):
    _, checkout = delivery
    _jobs(
        checkout,
        _job("a", _named("lint", ["true"], "        timeout-minutes: '1.5'\n")),
        _job("b", _named("lint", ["true; true"], "        timeout-minutes: 'soon'\n")),
    )
    assert [c.timeout for c in _target_ci.discover(checkout)] == [90, None]


def test_the_timeout_minutes_is_the_limit_a_command_runs_under(delivery, monkeypatch):
    monkeypatch.setattr(_target_run, "TARGET_CI_TIMEOUT", 1)
    steps = _named("lint", ["sleep 2"])
    assert _ci_status(delivery, steps, job_extra="    timeout-minutes: 1\n")["status"] == "PASS"
    assert "command timed out after 1 seconds" in _ci_status(delivery, steps)["output"]


def _junit_job(lane):
    write = (
        f'mkdir -p "$RUNNER_TEMP/junit" && echo {lane} > "$RUNNER_TEMP/junit/{lane}.xml"'
        f' && echo sha > "$RUNNER_TEMP/junit/head"'
    )
    upload = _uses(
        "actions/upload-artifact@v4",
        name=f"junit-{lane}",
        path="${{ runner.temp }}/junit/",
        **{"if-no-files-found": "error"},
    )
    return _job(lane, _named("pytest", [write]) + upload)


def test_artifacts_pass_between_jobs_as_github_lays_them_out(delivery):
    # The engine's own CI: two test jobs upload their junit, the standards job downloads both, merged.
    _, checkout = delivery
    downloads = (
        _uses(
            "actions/download-artifact@v4",
            pattern="junit-*",
            path="${{ runner.temp }}/junit",
            **{"merge-multiple": "true"},
        )
        + _uses("actions/download-artifact@v4", pattern="junit-*", path="apart")
        + _uses("actions/download-artifact@v4", name="junit-slow", path="one")
    )
    check = _named(
        "lint",
        [
            'test -f "$RUNNER_TEMP/junit/fast.xml" && test -f "$RUNNER_TEMP/junit/slow.xml"',
            'test -f "$RUNNER_TEMP/junit/head"',
            "test -f apart/junit-fast/fast.xml && test -f apart/junit-slow/slow.xml",
            "test -f one/slow.xml && test ! -e one/fast.xml",
        ],
    )
    # The gate comes first in the file: jobs run in `needs` order, not in the order they are written.
    _jobs(
        checkout,
        _job("gate", downloads + check, "    needs: [fast, slow]\n"),
        _junit_job("fast"),
        _junit_job("slow"),
    )
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.PASS, result.output


def test_an_upload_of_nothing_fails_only_when_it_says_error(delivery):
    _, checkout = delivery
    upload = _uses("actions/upload-artifact@v4", name="out", path="missing/")
    _jobs(checkout, _job("a", _named("lint", ["true"]) + upload))
    assert _gates.target_ci_result(checkout).status is _gate_base.Status.PASS
    _jobs(checkout, _job("a", _named("lint", ["true"]) + upload + "          if-no-files-found: error\n"))
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL
    assert "missing" in result.output


def test_a_job_whose_needed_job_failed_is_skipped_not_failed(delivery):
    _, checkout = delivery
    _jobs(
        checkout,
        _job("report", _named("lint", ["exit 7"]), "    needs: build\n"),
        _job("build", _named("test", ["exit 1"])),
        _job("cleanup", _named("lint", ["exit 5"]), "    needs: [build]\n    if: always()\n"),
        # Not a check: discovery leaves the job out, and a job that needs it still runs.
        _job("changes", _named("lanes", ["echo lint=true"])),
        _job("after", _named("lint", ["exit 9"]), "    needs: changes\n"),
    )
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL
    assert "`exit 1`" in result.output and "exited 1" in result.output
    assert "exited 7" not in result.output
    assert "skipped job `report`" in result.output and "`build` failed" in result.output
    assert "exited 5" in result.output
    assert "exited 9" in result.output


def test_steps_after_a_failed_step_do_not_run_unless_their_if_says_so(delivery):
    steps = (
        _named("lint", ["exit 1"])
        + _named("test", ["exit 3"])
        + _named("test again", ["exit 4"], "        if: always()\n")
        + _named("test once more", ["exit 6"], "        if: ${{ success() || failure() }}\n")
        + _named("tests on success", ["exit 8"], "        if: success()\n")
    )
    result = _ci_status(delivery, steps)
    assert result["status"] == "FAIL"
    assert "exited 1" in result["output"]
    assert "exited 3" not in result["output"] and "exited 8" not in result["output"]
    assert "skipped `exit 3`" in result["output"] and "an earlier step failed" in result["output"]
    assert "exited 4" in result["output"] and "exited 6" in result["output"]


def test_a_steps_origin_is_its_own_line_not_the_first_with_the_same_text(delivery):
    _, checkout = delivery
    keep = _named("keep", ["git rev-parse HEAD > head.txt"])
    folded = "      - name: lint\n        run: >-\n          echo\n          b\n"
    _jobs(checkout, _job("a", _named("lint", ["echo a"]) + keep), _job("b", folded + keep))
    rows = (checkout / ".github/workflows/ci.yml").read_text().splitlines()
    runs = [number for number, row in enumerate(rows, 1) if row.strip().startswith("run:")]
    found = {(c.job, c.command): c.origin for c in _target_ci.discover(checkout)}
    assert found == {
        ("a", "echo a"): f".github/workflows/ci.yml:{runs[0]}",
        ("a", "git rev-parse HEAD > head.txt"): f".github/workflows/ci.yml:{runs[1]}",
        ("b", "echo b"): f".github/workflows/ci.yml:{runs[2]}",
        ("b", "git rev-parse HEAD > head.txt"): f".github/workflows/ci.yml:{runs[3]}",
    }


def fake_uv(bin_dir, log):
    """Put a stand-in uv first on PATH's `bin_dir`; it logs each call to `log`.

    It runs the project's tests only after `uv sync` and `uv pip install pytest-cov` put pytest-cov into the
    project's own environment, and never as an overlay (`--with`): the engine's own tests build venvs from
    that one environment's site-packages, which an overlay splits in two. It measures with `coverage run`,
    since this test env has coverage but not pytest-cov.
    """
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "uv").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {log}\n'
        "venv_python=$PWD/.venv/bin/python\n"
        'case "$1 $2" in\n'
        "  'sync '*) touch .synced; exit 0 ;;\n"
        "  'pip install')\n"
        '    [[ -f .synced && "$3 $4 $5" == "--python $venv_python pytest-cov" ]] || exit 6\n'
        "    touch .has-cov; exit 0 ;;\n"
        "  'run --no-sync') [[ -f .synced ]] || exit 8; shift 2 ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
        'if [[ "$*" == "python -c "*sys.executable* ]]; then echo "$venv_python"; exit 0; fi\n'
        "[[ -f .has-cov ]] || exit 7\n"
        f'if [[ "$*" == *pytest* ]]; then exec {sys.executable} -m coverage run --branch -m pytest -q; fi\n'
        f'exec {sys.executable} "${{@:2}}"\n'
    )
    (bin_dir / "uv").chmod(0o755)


def assert_measured_in_the_project_env(log):
    """The fake uv's calls: sync, pytest-cov into that env's python, then measure and read, no re-sync."""
    runs = log.read_text().splitlines()
    assert runs[0] == "sync", runs
    installs = [row for row in runs if row.startswith("pip install ")]
    assert installs and all(row.endswith("/.venv/bin/python pytest-cov") for row in installs), runs
    assert any(row.startswith("run --no-sync python -m pytest --cov --cov-branch") for row in runs), runs
    assert any(row.startswith("run --no-sync python -m coverage json") for row in runs), runs
    assert not any("--with" in row for row in runs), runs


def test_the_fallback_coverage_run_gets_the_time_the_target_gives_its_own_pytest(delivery):
    # The fallback reruns the suite under --cov: a suite the target's CI gives 30 minutes must not be cut
    # off at the gate's per-step default. A target whose pytest sets no limit leaves the default.
    _, checkout = delivery
    _only_steps(checkout, _named("lint", ["ruff check"]) + _named("tests", ["pytest -q"]))
    assert _target_ci.coverage_command(checkout).timeout is None
    tests = _named("tests", ["python -m pytest -q"], "        timeout-minutes: 30\n")
    _only_steps(checkout, _named("lint", ["ruff check"], "        timeout-minutes: 60\n") + tests)
    assert _target_ci.coverage_command(checkout).timeout == 30 * 60
    (checkout / "uv.lock").write_text("version = 1\n")
    assert _target_ci.coverage_command(checkout).timeout == 30 * 60


def test_a_uv_target_measures_coverage_in_its_own_env_with_pytest_cov(delivery):
    # A uv project need not declare pytest-cov: the fallback installs it into the project's own environment
    # (not an overlay), with the uv its workflow installs (an older uv may not parse the lock file).
    _, checkout = delivery
    setup = "      - uses: astral-sh/setup-uv@v6\n        with:\n          version: 0.9.2\n"
    _only_steps(checkout, setup + _named("lint", ["uv run ruff check"]))
    assert _target_ci.coverage_command(checkout).command == _target_ci.FALLBACK_COVERAGE
    (checkout / "uv.lock").write_text("version = 1\n")
    command = _target_ci.coverage_command(checkout)
    assert command.command == _target_ci.FALLBACK_COVERAGE_UV
    assert "--with" not in command.command
    assert command.uv == "0.9.2"


def test_a_uv_targets_coverage_gate_reads_the_report_in_its_own_env(delivery, tmp_path, monkeypatch):
    # The gate's coverage rule, like promotion, measures and reads a uv target in the project's own env
    # with pytest-cov installed there: the python on PATH has no pytest-cov and may not be the project's.
    workspace, checkout = delivery
    (checkout / "uv.lock").write_text("version = 1\n")
    _only_steps(checkout, _named("lint", ["true"]))
    change(checkout, COVERED_CLAMP, CLAMP_TEST.replace("import sign", "import clamp, sign"))
    log = tmp_path / "uv.log"
    fake_uv(tmp_path / "fake-bin", log)
    monkeypatch.setenv("PATH", f"{tmp_path / 'fake-bin'}{os.pathsep}{os.environ['PATH']}")
    result = coverage(workspace, checkout)
    assert result["status"] == "FAIL", result
    assert "calc.py:9" in result["output"]
    assert_measured_in_the_project_env(log)


def test_a_failure_report_keeps_pytests_failed_lines_that_stderr_noise_pushed_out_of_its_tail():
    # stdout comes first and the report keeps its tail: an installer's stderr hid which test failed.
    stdout = "x" * 50 + "\nFAILED tests/test_a.py::test_one - assert 1 == 2\n= 1 failed, 9 passed in 3s =\n"
    stderr = "".join(f" + package-{n}==1.0\n" for n in range(400))
    item = _target_ci.Command("pytest", "ci.yml:9")
    report = _target_run.report(item, _target_run.Run(1, stdout + stderr))
    assert "FAILED tests/test_a.py::test_one - assert 1 == 2" in report
    assert "= 1 failed, 9 passed in 3s =" in report
    assert report.endswith(" + package-399==1.0\n")
    short = _target_run.report(item, _target_run.Run(1, stdout))
    assert short.count("FAILED tests/test_a.py::test_one") == 1
