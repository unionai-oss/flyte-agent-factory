"""The target repo's CI runs as on a runner: a clean environment, its toolchains, judged against its base."""

import hashlib
import io
import os
import shutil
import stat
import sys
import tarfile
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_tests,
    _gates,
    _module_size,
    _target_ci,
    _target_run,
    _target_tools,
)
from tests.test_target_ci import _build_delivery, _named, _only_steps, git

pytestmark = pytest.mark.real_factory_gates

PLANTED = {
    "_U_X": "1",
    "FLYTE_X": "1",
    "USER_LOG_LEVEL": "20",
    "AGENT_FACTORY_X": "1",
    "ANTHROPIC_API_KEY": "sk-planted",
}


@pytest.fixture(autouse=True)
def _project_python_first(monkeypatch):
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(Path(os.environ.get("TMPDIR", "/tmp")) / "factory-env-test-home"))


@pytest.fixture(scope="module")
def _template(tmp_path_factory):
    root = tmp_path_factory.mktemp("env-template")
    _build_delivery(root)
    return root


@pytest.fixture
def checkout(tmp_path, _template):
    shutil.copytree(_template, tmp_path, symlinks=True, dirs_exist_ok=True)
    return tmp_path / "app/widgets"


def _verdict(checkout, steps, **kwargs):
    _only_steps(checkout, steps, **kwargs)
    return _gates.target_ci_result(checkout)


def _on_main(checkout, files):
    """Commit `files` to the upstream branch and restart the feature branch from there."""
    git(checkout, "checkout", "main")
    for name, text in files.items():
        (checkout / name).write_text(text)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "upstream change")
    git(checkout, "checkout", "-B", "feature", "main")


# -- the runner-like environment --


def test_a_job_sees_none_of_the_pods_environment(checkout, monkeypatch):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    leaked = "env | cut -d= -f1 | grep -E '^(_U_|FLYTE_|USER_LOG_LEVEL|AGENT_FACTORY_|ANTHROPIC_)'"
    result = _verdict(checkout, _named("lint", [f"! {leaked}"]))
    assert result.status is _gate_base.Status.PASS, result.output
    # A variable the workflow sets itself is the workflow's to set.
    own = _named("lint", [leaked], "        env:\n          FLYTE_X: 1\n")
    assert _verdict(checkout, own).status is _gate_base.Status.PASS


def test_a_job_looks_like_a_runner(checkout):
    rows = [
        'test "$CI" = true',
        'test "$GITHUB_ACTIONS" = true',
        'test "$RUNNER_OS" = Linux',
        'test "$GITHUB_EVENT_NAME" = pull_request',
        'test "$GITHUB_REPOSITORY" = acme/widgets',
        'test "$GITHUB_WORKSPACE" = "$PWD"',
        'test -d "$RUNNER_TEMP"',
        'test -n "$GITHUB_SHA"',
        'test -n "$GITHUB_REF"',
        'test -n "$HOME" && test -n "$PATH"',
    ]
    _only_steps(checkout, _named("lint", rows))
    result = _gates.target_ci_result(checkout, "acme/widgets")
    assert result.status is _gate_base.Status.PASS, result.output


def test_github_output_is_a_real_file_per_job(checkout):
    first = _named("lint", ['test -f "$GITHUB_OUTPUT"', 'echo "plugins=a,b" >> "$GITHUB_OUTPUT"'])
    second = _named("lint", ['grep -q "^plugins=a,b$" "$GITHUB_OUTPUT"'])
    other = "  other:\n    runs-on: ubuntu-latest\n    steps:\n" + _named(
        "lint", ['test ! -s "$GITHUB_OUTPUT"']
    )
    assert _verdict(checkout, first + second + other).status is _gate_base.Status.PASS


def test_the_other_runner_files_exist_and_github_env_carries_on(checkout):
    steps = _named(
        "lint",
        ['echo "STRICT=1" >> "$GITHUB_ENV"', 'test -f "$GITHUB_STEP_SUMMARY"', 'test -f "$GITHUB_PATH"'],
    ) + _named("types", ['test "$STRICT" = 1'])
    assert _verdict(checkout, steps).status is _gate_base.Status.PASS


def test_workflow_job_and_step_env_reach_the_step(checkout):
    steps = _named("lint", ['test "$A$B$C" = 123'], "        env:\n          C: 3\n")
    result = _verdict(checkout, steps, top="env:\n  A: 1\n", job_extra="    env:\n      B: 2\n")
    assert result.status is _gate_base.Status.PASS, result.output


def test_the_runner_env_keeps_the_basics_and_drops_credentials(monkeypatch, tmp_path):
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    env = _target_run.runner_env(tmp_path, tmp_path / "state", "acme/widgets", "a" * 40)
    assert not set(PLANTED) & set(env)
    assert env["PATH"] == os.environ["PATH"] and env["LC_ALL"] == "C.UTF-8"
    assert env["GITHUB_REPOSITORY"] == "acme/widgets" and env["GITHUB_SHA"] == "a" * 40


# -- toolchains --


def _no_cargo(monkeypatch, tmp_path):
    """A PATH and HOME with no cargo or rustup, and a network that is down."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    bare = tmp_path / "bin"
    bare.mkdir()
    for tool in ("bash", "sh", "env", "git", "python", "grep", "cat", "test"):
        found = shutil.which(tool)
        if found:
            (bare / tool).symlink_to(found)
    monkeypatch.setenv("PATH", str(bare) + os.pathsep + str(Path(sys.executable).parent))
    assert shutil.which("cargo") is None and shutil.which("rustup") is None

    def down():
        raise OSError("network is down")

    monkeypatch.setattr(_target_run, "_download_rustup_init", down)
    return home


def _fake_cargo(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / "cargo"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)


def test_a_cargo_job_with_no_toolchain_is_not_run_not_failed(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    result = _verdict(checkout, _named("fmt", ["cargo fmt --check"]))
    assert result.status is _gate_base.Status.NOT_RUN
    assert not result.status.blocks
    assert "NOT RUN" in result.output and "cargo fmt --check" in result.output
    assert "network is down" in result.output


def test_a_script_that_needs_cargo_is_not_run_either(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    (checkout / "build.sh").write_text("cargo build\n")
    result = _verdict(checkout, _named("lint", ["bash build.sh"]))
    assert result.status is _gate_base.Status.NOT_RUN
    assert "cargo" in result.output


def test_a_failure_elsewhere_still_fails_beside_a_not_run_command(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    result = _verdict(checkout, _named("fmt", ["cargo fmt --check"]) + _named("lint", ["test -e Makefile"]))
    assert result.status is _gate_base.Status.FAIL
    assert "NOT RUN" in result.output


def test_an_installed_cargo_in_home_is_used(checkout, monkeypatch, tmp_path):
    home = _no_cargo(monkeypatch, tmp_path)
    _fake_cargo(home / ".cargo/bin")
    assert _verdict(checkout, _named("fmt", ["cargo fmt --check"])).status is _gate_base.Status.PASS


def test_a_cargo_on_path_is_used(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    _fake_cargo(tmp_path / "tools")
    monkeypatch.setenv("PATH", f"{tmp_path / 'tools'}{os.pathsep}{os.environ['PATH']}")
    assert _verdict(checkout, _named("fmt", ["cargo fmt --check"])).status is _gate_base.Status.PASS


def test_a_rust_setup_action_is_read_with_its_toolchain_and_components(checkout):
    steps = (
        "      - uses: dtolnay/rust-toolchain@nightly\n        with:\n          components: rustfmt, clippy\n"
        + _named("fmt", ["make fmt"])
    )
    _only_steps(checkout, steps)
    (command,) = _target_ci.discover(checkout)
    assert command.rust == ("nightly", "rustfmt", "clippy")


def test_the_same_check_under_another_toolchain_is_its_own_command(checkout):
    """An MSRV job's `cargo test` is not the stable job's: each runs, under its own toolchain."""
    job = "  {name}:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"
    jobs = {
        "stable": "      - uses: dtolnay/rust-toolchain@stable\n",
        "msrv": "      - uses: dtolnay/rust-toolchain@1.70\n",
        "pinned": _named("setup", ["rustup toolchain install 1.75 && rustup default 1.75"]),
    }
    text = "name: ci\non:\n  pull_request:\njobs:\n" + "".join(
        job.format(name=name, steps=setup + _named("test", ["cargo test"])) for name, setup in jobs.items()
    )
    (checkout / ".github/workflows/ci.yml").write_text(text)
    (checkout / "Makefile").unlink(missing_ok=True)
    commands = [c for c in _target_ci.discover(checkout) if c.command == "cargo test"]
    assert sorted((c.rust, c.toolchain_steps) for c in commands) == [
        ((), ("rustup toolchain install 1.75 && rustup default 1.75",)),
        (("1.70",), ()),
        (("stable",), ()),
    ]


@pytest.mark.parametrize(
    "setup",
    [
        "      - uses: dtolnay/rust-toolchain@stable\n",
        '      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n',
    ],
)
def test_a_build_file_target_is_one_check_with_the_step_that_runs_it_under_a_toolchain(checkout, setup):
    # The Makefile target has no toolchain of its own: it is the job's `make test`, not a second check.
    _only_steps(checkout, setup + _named("test", ["make test"]))
    (checkout / "Makefile").write_text("test:\n\tcargo test\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "make test")
    (command,) = [c for c in _target_ci.discover(checkout) if c.command == "make test"]
    assert command.origin.endswith(", Makefile:1"), command.origin
    assert command.rust or command.python


def test_a_rust_setup_action_makes_a_job_need_rust(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    steps = "      - uses: dtolnay/rust-toolchain@stable\n" + _named("lint", ["make lint"])
    _only_steps(checkout, steps)
    (checkout / "Makefile").write_text("lint:\n\ttrue\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "make")
    assert _gates.target_ci_result(checkout).status is _gate_base.Status.NOT_RUN


# -- failures the base has too --

FAILS = "import sys\nprint('FAILED tests/test_a.py::test_one - boom')\nsys.exit(1)\n"
FAILS_MORE = FAILS.replace("sys.exit", "print('FAILED tests/test_b.py::test_two - new')\nsys.exit")
PASSES = "print('fine')\n"


def _check_step():
    return _named("lint", ["python check.py"])


def _ci_on_main(checkout, files, steps):
    """Commit `files` and a CI of `steps` upstream, so the base's own CI runs them, then branch from there."""
    git(checkout, "checkout", "main")
    for name, text in files.items():
        (checkout / name).write_text(text)
    _only_steps(checkout, steps)
    git(checkout, "checkout", "-B", "feature", "main")
    git(checkout, "commit", "--allow-empty", "-m", "the patch")
    return _gates.target_ci_result(checkout)


def test_a_command_that_fails_on_the_base_too_is_pre_existing(checkout):
    result = _ci_on_main(checkout, {"check.py": FAILS}, _check_step())
    assert result.status is _gate_base.Status.PASS
    assert "pre-existing" in result.output


def test_a_pre_existing_failure_ignores_paths_and_timings(checkout):
    noisy = (
        "import os, sys, time\nprint(f'error in {os.getcwd()}/a.py after {time.time():.2f}s')\nsys.exit(1)\n"
    )
    assert "pre-existing" in _ci_on_main(checkout, {"check.py": noisy}, _check_step()).output


def test_a_failing_command_the_patch_added_to_ci_is_the_patchs(checkout):
    # The base fails it too when made to run it, but its own CI never did: the patch brought it in.
    _on_main(checkout, {"check.py": FAILS})
    result = _verdict(checkout, _check_step())
    assert result.status is _gate_base.Status.FAIL
    assert "the unpatched base's CI does not run this command" in result.output


def test_a_command_that_fails_only_on_the_head_fails(checkout):
    _on_main(checkout, {"check.py": PASSES})
    _only_steps(checkout, _check_step())
    (checkout / "check.py").write_text(FAILS)
    git(checkout, "commit", "-am", "break it")
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL
    assert "pre-existing" not in result.output


def test_a_new_failing_test_beside_the_old_one_fails(checkout):
    _on_main(checkout, {"check.py": FAILS})
    _only_steps(checkout, _check_step())
    (checkout / "check.py").write_text(FAILS_MORE)
    git(checkout, "commit", "-am", "break more")
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL
    assert "test_two" in result.output


def test_a_different_exit_code_is_not_the_same_failure(checkout):
    _on_main(checkout, {"check.py": FAILS})
    _only_steps(checkout, _check_step())
    (checkout / "check.py").write_text(FAILS.replace("exit(1)", "exit(2)"))
    git(checkout, "commit", "-am", "worse")
    assert _gates.target_ci_result(checkout).status is _gate_base.Status.FAIL


def test_a_failure_with_no_base_to_compare_with_fails_and_says_why(checkout):
    _on_main(checkout, {"check.py": FAILS})
    git(checkout, "branch", "-m", "main", "trunk")
    result = _verdict(checkout, _check_step())
    assert result.status is _gate_base.Status.FAIL
    assert "base" in result.output and "unavailable" in result.output


def test_later_steps_of_the_job_run_on_the_base_too(checkout):
    """What an earlier step generated is there on the base, as it was on the head."""
    _on_main(checkout, {"check.py": "import sys\nsys.exit(0 if open('gen.txt') else 1)\n"})
    steps = _named("gen", ["echo x > gen.txt"]) + _check_step()
    _only_steps(checkout, steps)
    (checkout / "check.py").write_text("import sys\nopen('gen.txt')\nprint('FAILED t::a - x')\nsys.exit(1)\n")
    git(checkout, "commit", "-am", "break it")
    assert _gates.target_ci_result(checkout).status is _gate_base.Status.FAIL


def _judged_against(monkeypatch, checkout, item, run, base):
    """Judge one step that ran as `run` against a base that ran it as `base` (its own CI runs it too)."""
    monkeypatch.setattr(_target_run, "runs_on_base", lambda *args: ([base], {item.command}))
    return _target_run.judge(checkout, [(item, run)], "")


def test_a_head_failure_beside_a_missing_command_on_a_passing_base_still_fails(checkout, monkeypatch):
    # The base printed the same "command not found" and passed: the environment did not stop the check.
    item = _target_ci.Command("make test", "ci.yml:1")
    missing = "bash: line 1: shellcheck: command not found\n"
    run = _target_run.Run(1, missing + "FAILED tests/test_a.py::test_one - boom\n", str(checkout))
    judged = _judged_against(monkeypatch, checkout, item, run, _target_run.Run(0, missing, "/base"))
    assert judged.failures and not judged.not_run


def test_a_head_failure_naming_failing_tests_is_not_an_environment_failure(checkout, monkeypatch):
    item = _target_ci.Command("echo 'FAILED tests/test_a.py::test_one - boom'; exit 1", "ci.yml:1")
    missing = "bash: line 1: shellcheck: command not found\n"
    run = _target_run.Run(1, missing + "FAILED tests/test_a.py::test_one - boom\n", str(checkout))
    judged = _judged_against(monkeypatch, checkout, item, run, _target_run.Run(2, missing, "/base"))
    assert judged.failures and not judged.not_run


def test_the_rerun_of_a_failed_command_leaves_the_checkout_alone(checkout, monkeypatch):
    """It runs in a fresh copy of HEAD, as the first run does: no edits land, no uncommitted file is read."""
    (checkout / "wip.txt").write_text("not committed\n")
    item = _target_ci.Command("touch dirt.txt; test ! -e wip.txt", "ci.yml:1")
    run = _target_run.Run(1, "FAILED t.py::test_one - boom\n", "/head")
    judged = _judged_against(monkeypatch, checkout, item, run, _target_run.Run(1, "other\n", "/base"))
    assert not (checkout / "dirt.txt").exists()
    assert judged.pre_existing and "passed when run again" in judged.pre_existing[0]
    assert "dirt.txt" not in git(checkout, "worktree", "list")


def test_the_coverage_run_is_rerun_on_the_checkout_it_first_ran_on(checkout, monkeypatch):
    """The gate's coverage run (not a CI step) ran in the checkout, uncommitted files and all."""
    (checkout / "wip.txt").write_text("not committed\n")
    item = _target_ci.Command("test -e wip.txt", "coverage")
    run = _target_run.Run(1, "FAILED t.py::test_one - boom\n", str(checkout))
    monkeypatch.setattr(_target_run, "runs_on_base", lambda *args: ([_target_run.Run(1, "other\n")], set()))
    judged = _target_run.judge(checkout, [(item, run)], "", from_ci=False)
    assert judged.pre_existing and "passed when run again" in judged.pre_existing[0]


def test_a_failing_test_id_with_spaces_is_one_finding():
    output = "FAILED t.py::test_read[utf-8 bom] - UnicodeError: x\nERROR t.py::test_open[a b]\n"
    assert _target_run._findings(_target_run.Run(1, output)) == {
        "t.py::test_read[utf-8 bom]",
        "t.py::test_open[a b]",
    }


def test_test_ids_that_differ_only_in_a_duration_are_different_failures():
    head = _target_run.Run(1, "FAILED t.py::test_backoff[10s] - boom after 1.5s\n")
    base = _target_run.Run(1, "FAILED t.py::test_backoff[5s] - boom after 2.5s\n")
    assert not _target_run.same_failure(head, base)
    assert _target_run._findings(head) == {"t.py::test_backoff[10s]"}


def test_a_parameter_id_holding_a_dash_is_kept_whole():
    # A parameter ID may hold " - "; the message starts at the first " - " after the closing bracket.
    head = _target_run.Run(1, "FAILED t.py::test_sign[utf-8 - nobom] - assert -1 == 1\n")
    base = _target_run.Run(1, "FAILED t.py::test_sign[utf-8 - bom] - assert 1 == -1\n")
    assert _target_run._findings(head) == {"t.py::test_sign[utf-8 - nobom]"}
    assert _target_run._findings(base) == {"t.py::test_sign[utf-8 - bom]"}
    assert not _target_run.same_failure(head, base)


def test_a_new_failure_whose_id_holds_a_dash_is_not_pre_existing(checkout):
    base_fails = "print('FAILED t.py::test_sign[utf-8 - bom] - assert 1 == -1')\nraise SystemExit(1)\n"
    _ci_on_main(checkout, {"check.py": base_fails}, _check_step())
    (checkout / "check.py").write_text(
        base_fails.replace("[utf-8 - bom] - assert 1 == -1", "[utf-8 - nobom] - x")
    )
    git(checkout, "commit", "-am", "fix one parameter, break the other")
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "pre-existing" not in result.output


@pytest.mark.parametrize(
    "head, base",
    [
        # pytest prints these for ids=["a] - b", "a] - c"]: where the id ends cannot be told from the row.
        (
            "FAILED t.py::test_x[a] - b] - AssertionError: x\n",
            "FAILED t.py::test_x[a] - c] - AssertionError: x\n",
        ),
        ("FAILED t.py::test_x[a] - b]\n", "FAILED t.py::test_x[a] - c]\n"),
        # ids=["a] - 10s", "a] - 5s"]: a duration inside an id that cannot be split off is part of the id,
        # not a timing the output may differ by (round-1 review).
        (
            "FAILED t.py::test_x[a] - 10s] - AssertionError: broken\n",
            "FAILED t.py::test_x[a] - 5s] - AssertionError: broken\n",
        ),
        ("FAILED t.py::test_x[a] - 10s]\n", "FAILED t.py::test_x[a] - 5s]\n"),
        # A path with a space: the row is still a failure, beside one the base shares.
        ("FAILED t.py::test_old - x\nFAILED my dir/t.py::test_new - y\n", "FAILED t.py::test_old - x\n"),
        ("FAILED t.py::test_old - x\nFAILED my - dir/t.py::test_new - y\n", "FAILED t.py::test_old - x\n"),
    ],
)
def test_a_summary_row_whose_id_cannot_be_split_off_is_not_taken_for_another(head, base):
    assert not _target_run.same_failure(_target_run.Run(1, head), _target_run.Run(1, base))
    assert _target_run._findings(_target_run.Run(1, head)) - _target_run._findings(_target_run.Run(1, base))


def test_an_ambiguous_row_the_base_prints_the_same_is_still_pre_existing():
    row = "FAILED t.py::test_x[a] - assert x == [1]\nFAILED my dir/t.py::test_y - boom\n"
    assert _target_run.same_failure(_target_run.Run(1, row), _target_run.Run(1, row))


def test_a_new_failure_whose_id_holds_a_bracket_and_a_dash_is_not_pre_existing(checkout):
    base_fails = "print('FAILED t.py::test_x[a] - b] - AssertionError')\nraise SystemExit(1)\n"
    _ci_on_main(checkout, {"check.py": base_fails}, _check_step())
    (checkout / "check.py").write_text(base_fails.replace("[a] - b]", "[a] - c]"))
    git(checkout, "commit", "-am", "fix one parameter, break the other")
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "pre-existing" not in result.output


def test_a_new_failure_whose_ambiguous_id_differs_only_by_a_duration_is_not_pre_existing(checkout):
    # The base fails test_x[a] - 5s]; HEAD fixes it and breaks test_x[a] - 10s], with the same message.
    base_fails = "print('FAILED t.py::test_x[a] - 5s] - AssertionError: broken')\nraise SystemExit(1)\n"
    _ci_on_main(checkout, {"check.py": base_fails}, _check_step())
    (checkout / "check.py").write_text(base_fails.replace("5s]", "10s]"))
    git(checkout, "commit", "-am", "fix one parameter, break the other")
    result = _gates.target_ci_result(checkout)
    assert result.status is _gate_base.Status.FAIL, result.output
    assert "pre-existing" not in result.output


def test_a_timing_in_the_message_of_a_row_whose_id_is_clear_is_still_ignored():
    # What the timing scrub is for: the same test failing in 10s on one run and 5s on the other.
    head = "FAILED t.py::test_x[a] - TimeoutError: gave up after 10s\n"
    base = "FAILED t.py::test_x[a] - TimeoutError: gave up after 5s\n"
    assert _target_run.same_failure(_target_run.Run(1, head), _target_run.Run(1, base))


COVERAGE_TEST = (
    "from calc import sign\n\n\ndef test_sign():\n    assert sign(-2) == -1\n    assert sign(2) == 1\n\n\n"
    "def test_old_and_broken():\n    assert False\n"
)
TWICE = "def sign(x):\n    if x < 0:\n        return -1\n    return 1\n\n\ndef twice(x):\n    return 2 * x\n"
TWICE_TEST = COVERAGE_TEST.replace("from calc import sign", "from calc import sign, twice") + (
    "\n\ndef test_twice():\n    assert twice(2) == 4\n"
)


def _coverage(checkout):
    result = _gates._target_coverage_result(checkout.parent, checkout)
    return result.status, result.output


def test_upstream_tests_failing_on_the_base_do_not_fail_coverage(checkout):
    _on_main(checkout, {"test_calc.py": COVERAGE_TEST})
    (checkout / "calc.py").write_text(TWICE)
    (checkout / "test_calc.py").write_text(TWICE_TEST)
    git(checkout, "commit", "-am", "add twice")
    status, output = _coverage(checkout)
    assert status is _gate_base.Status.PASS, output


def test_an_upstream_test_that_fails_only_once_does_not_fail_coverage(checkout, tmp_path):
    # Fails on its first run anywhere, then passes: the head's first run has one failure more than the base.
    marker = tmp_path / "ran-once"
    flaky = (
        f"\n\ndef test_now_and_then():\n    import pathlib\n    m = pathlib.Path({str(marker)!r})\n"
        "    first = not m.exists()\n    m.touch()\n    assert not first\n"
    )
    _on_main(checkout, {"test_calc.py": COVERAGE_TEST + flaky})
    (checkout / "calc.py").write_text(TWICE)
    (checkout / "test_calc.py").write_text(TWICE_TEST + flaky)
    git(checkout, "commit", "-am", "add twice")
    status, output = _coverage(checkout)
    assert status is _gate_base.Status.PASS, output
    assert "come and go between runs" in output


def test_a_test_that_fails_in_one_base_run_only_is_not_the_patchs(checkout, tmp_path):
    # Fails in every run but the second one anywhere: head runs 1 and 3 fail it, base runs 2 and 4 differ.
    counter = tmp_path / "runs"
    flaky = (
        f"\n\ndef test_now_and_then():\n    import pathlib\n    c = pathlib.Path({str(counter)!r})\n"
        "    n = int(c.read_text()) + 1 if c.exists() else 1\n    c.write_text(str(n))\n    assert n == 2\n"
    )
    _on_main(checkout, {"test_calc.py": COVERAGE_TEST + flaky})
    (checkout / "calc.py").write_text(TWICE)
    (checkout / "test_calc.py").write_text(TWICE_TEST + flaky)
    git(checkout, "commit", "-am", "add twice")
    status, output = _coverage(checkout)
    assert status is _gate_base.Status.PASS, output


def test_changed_lines_must_still_be_covered_beside_old_failures(checkout):
    _on_main(checkout, {"test_calc.py": COVERAGE_TEST})
    (checkout / "calc.py").write_text(TWICE)
    git(checkout, "commit", "-am", "add twice without a test")
    status, output = _coverage(checkout)
    assert status is _gate_base.Status.FAIL and "calc.py" in output


def test_a_test_that_fails_only_on_the_head_fails_coverage(checkout):
    _on_main(checkout, {"test_calc.py": COVERAGE_TEST})
    (checkout / "calc.py").write_text(TWICE)
    (checkout / "test_calc.py").write_text(TWICE_TEST.replace("== 4", "== 5"))
    git(checkout, "commit", "-am", "add a broken test")
    status, output = _coverage(checkout)
    assert status is _gate_base.Status.FAIL and "test_twice" in output


# -- the nested checkout and the size gate --

BIG = "x = 1\n" * (_module_size.MODULE_LINE_LIMIT + 100)


@pytest.fixture
def nested(tmp_path):
    """A project holding a nested upstream checkout with one over-limit module, on a feature branch."""
    project = tmp_path / "proj"
    upstream = project / ".upstream"
    upstream.mkdir(parents=True)
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "t@example.com")
    git(upstream, "config", "user.name", "t")
    (upstream / "big.py").write_text(BIG)
    (upstream / "small.py").write_text("y = 1\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "base")
    git(upstream, "checkout", "-b", "feature")
    return project, upstream


def _size(project):
    return _module_size.check(project, None, _gate_tests._is_test)


def test_an_unchanged_over_limit_file_of_the_nested_checkout_is_skipped(nested):
    project, upstream = nested
    (upstream / "small.py").write_text("y = 2\n")
    git(upstream, "commit", "-am", "small change")
    assert _size(project)[0] == 0


def test_a_changed_nested_file_is_still_checked(nested):
    project, upstream = nested
    (upstream / "big.py").write_text(BIG + "x = 2\n")
    git(upstream, "commit", "-am", "grow")
    code, output = _size(project)
    assert code == 1 and ".upstream/big.py" in output and "was" in output


def test_a_changed_nested_file_that_did_not_grow_is_listed_as_debt(nested):
    project, upstream = nested
    (upstream / "big.py").write_text(BIG.replace("x = 1", "x = 3", 1))
    git(upstream, "commit", "-am", "edit")
    code, output = _size(project)
    assert code == 0 and "over the limit but not grown: .upstream/big.py" in output


def test_a_grown_nested_file_with_form_feeds_is_not_debt(nested):
    # str.splitlines() also breaks on \x0c: counted that way, the base had twice the lines it has.
    project, upstream = nested
    paged = "x = 1  # \x0c\n" * (_module_size.MODULE_LINE_LIMIT + 100)
    (upstream / "big.py").write_text(paged)
    git(upstream, "commit", "-am", "paged")
    git(upstream, "branch", "-f", "main")
    (upstream / "big.py").write_text(paged + "x = 2\n" * 10)
    git(upstream, "commit", "-am", "grow")
    code, output = _size(project)
    assert code == 1 and f"was {_module_size.MODULE_LINE_LIMIT + 100}" in output, output


def test_a_new_nested_file_over_the_limit_fails(nested):
    project, upstream = nested
    (upstream / "new.py").write_text(BIG)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "new")
    code, output = _size(project)
    assert code == 1 and ".upstream/new.py" in output


def test_the_projects_own_big_file_still_fails(nested):
    project, _ = nested
    (project / "mine.py").write_text(BIG)
    code, output = _size(project)
    assert code == 1 and "mine.py" in output and ".upstream" not in output


# -- declared evidence commands --

EVIDENCE = "## EVD-001 MUST\nShow it in a browser.\n\nEnforced: tool\n"
WITH_COMMAND = "## EVD-002 MUST\nLint it.\n\nEnforced: tool true\n"
RULES = {"standards.md": EVIDENCE + "\n" + WITH_COMMAND}


def _evidence(tmp_path, declared=None, text=None):
    if declared is not None:
        body = "".join(f'"{rule}" = {command!r}\n' for rule, command in declared.items())
        (tmp_path / "factory-evidence.toml").write_text(text or f"[evidence]\n{body}")
    elif text:
        (tmp_path / "factory-evidence.toml").write_text(text)
    return {r.rule: r for r in _gates.standards_results(tmp_path, RULES, configs=tmp_path)}


def test_an_evidence_rule_with_no_declared_command_is_reported_as_before(tmp_path):
    result = _evidence(tmp_path)["EVD-001"]
    assert result.status is _gate_base.Status.TOOL_UNAVAILABLE
    assert "no project command configured for this evidence check" in result.output


def test_a_declared_command_that_passes_passes_the_rule(tmp_path):
    results = _evidence(tmp_path, {"EVD-001": "true"})
    assert results["EVD-001"].status is _gate_base.Status.PASS
    assert "factory:evidence" not in results


def test_a_declared_command_that_fails_fails_the_rule_with_its_output(tmp_path):
    result = _evidence(tmp_path, {"EVD-001": "sh -c 'echo screenshot missing; exit 3'"})["EVD-001"]
    assert result.status is _gate_base.Status.FAIL
    assert "screenshot missing" in result.output


def test_a_declared_command_runs_in_the_project_dir(tmp_path):
    (tmp_path / "marker").write_text("")
    assert _evidence(tmp_path, {"EVD-001": "test -f marker"})["EVD-001"].status is _gate_base.Status.PASS


def test_a_command_for_a_rule_that_has_its_own_check_is_rejected(tmp_path):
    results = _evidence(tmp_path, {"EVD-002": "true", "EVD-001": "true"})
    rejected = results["factory:evidence"]
    assert rejected.status is _gate_base.Status.FAIL
    assert "EVD-002" in rejected.output and "not an evidence rule" in rejected.output
    assert "EVD-001" not in rejected.output
    assert results["EVD-001"].status is _gate_base.Status.PASS


def test_a_command_for_an_unknown_rule_is_rejected(tmp_path):
    results = _evidence(tmp_path, {"EVD-099": "true"})
    assert results["factory:evidence"].status is _gate_base.Status.FAIL
    assert "EVD-099" in results["factory:evidence"].output


@pytest.mark.parametrize(
    "text",
    ["not toml [", '[evidence]\n"EVD-001" = 3\n', "evidence = 'x'\n"],
    ids=["syntax", "not-a-string", "not-a-table"],
)
def test_a_malformed_evidence_file_fails_clearly(tmp_path, text):
    results = _evidence(tmp_path, text=text)
    assert results["factory:evidence"].status is _gate_base.Status.FAIL
    assert "factory-evidence.toml" in results["factory:evidence"].output
    assert results["EVD-001"].status is _gate_base.Status.TOOL_UNAVAILABLE


def test_the_gate_tells_a_project_how_to_declare_evidence(tmp_path):
    output = _evidence(tmp_path)["EVD-001"].output
    assert "factory-evidence.toml" in output and '"EVD-001" = "<command>"' in output


# -- the uv and Python a workflow sets up --


def _uses(action, version=None, name="Install uv"):
    inputs = f"        with:\n          version: {version}\n" if version else ""
    return f"      - name: {name}\n        uses: {action}\n{inputs}"


def test_a_setup_uv_step_names_the_uv_of_the_job(checkout):
    _only_steps(checkout, _uses("astral-sh/setup-uv@v10.0.0", '"0.13.4"') + _named("lint", ["make lint"]))
    (command,) = _target_ci.discover(checkout)
    assert command.uv == "0.13.4"


def test_a_setup_uv_step_with_no_version_wants_the_latest(checkout):
    _only_steps(checkout, _uses("astral-sh/setup-uv@v10") + _named("lint", ["make lint"]))
    assert _target_ci.discover(checkout)[0].uv == "latest"


def test_a_job_with_no_setup_uv_step_wants_no_uv(checkout):
    _only_steps(checkout, _named("lint", ["make lint"]))
    assert _target_ci.discover(checkout)[0].uv == ""


COMPOSITE = """name: Setup
inputs:
  python-version:
    default: "3.12"
  uv-version:
    default: "0.13.4"
runs:
  using: composite
  steps:
    - uses: actions/setup-python@v7
      with:
        python-version: ${{ inputs.python-version }}
    - uses: astral-sh/setup-uv@v10.0.0
      with:
        version: ${{ inputs.uv-version }}
"""


def _composite(checkout, text=COMPOSITE):
    action = checkout / ".github/actions/setup-python-env"
    action.mkdir(parents=True, exist_ok=True)
    (action / "action.yml").write_text(text)


def test_a_local_action_names_the_uv_and_python_of_the_job_through_its_inputs(checkout):
    _composite(checkout)
    steps = (
        "      - uses: ./.github/actions/setup-python-env\n        with:\n          python-version: '3.11'\n"
    )
    _only_steps(checkout, steps + _named("lint", ["make lint"]))
    (command,) = _target_ci.discover(checkout)
    assert (command.uv, command.python) == ("0.13.4", "3.11")


def test_a_local_action_takes_its_defaults_when_the_job_gives_no_inputs(checkout):
    _composite(checkout)
    _only_steps(
        checkout, "      - uses: ./.github/actions/setup-python-env\n" + _named("lint", ["make lint"])
    )
    assert _target_ci.discover(checkout)[0].python == "3.12"


def test_a_missing_local_action_is_no_error(checkout):
    _only_steps(checkout, "      - uses: ./.github/actions/gone\n" + _named("lint", ["make lint"]))
    assert _target_ci.discover(checkout)[0].uv == ""


def _release(monkeypatch, version="0.13.4", digest=None):
    """A uv release whose binary prints its version; the network is this dictionary."""
    binary = f"#!/bin/sh\necho 'uv {version}'\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"uv-{_target_tools.triple()}/uv")
        info.size, info.mode = len(binary), 0o755
        archive.addfile(info, io.BytesIO(binary))
    tarball = buffer.getvalue()
    asked = []

    def download(url):
        asked.append(url)
        if url.endswith(".sha256"):
            return f"{digest or hashlib.sha256(tarball).hexdigest()}  uv.tar.gz\n".encode()
        return tarball

    monkeypatch.setattr(_target_tools, "_download", download)
    return asked


def _uv_job(version, check="uv --version | grep -q '^uv 0.13.4$'"):
    return _uses("astral-sh/setup-uv@v10", f'"{version}"') + _named("lint", [check])


def test_a_job_gets_the_uv_its_workflow_sets_up_first_on_path(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    asked = _release(monkeypatch)
    result = _verdict(checkout, _uv_job("0.13.4"))
    assert result.status is _gate_base.Status.PASS, result.output
    assert any("/0.13.4/" in url for url in asked)


def test_the_uv_is_kept_for_the_next_job(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    asked = _release(monkeypatch)
    _verdict(checkout, _uv_job("0.13.4"))
    asked.clear()
    assert _gates.target_ci_result(checkout).status is _gate_base.Status.PASS
    assert asked == []


def test_a_uv_release_whose_checksum_differs_is_not_used(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    _release(monkeypatch, digest="0" * 64)
    result = _verdict(checkout, _uv_job("0.13.4"))
    assert result.status is _gate_base.Status.NOT_RUN
    assert "checksum" in result.output


def test_a_uv_release_with_no_published_checksum_is_not_installed(monkeypatch, tmp_path):
    def download(url):
        if url.endswith(".sha256"):
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return b"an archive nobody can verify"

    monkeypatch.setattr(_target_tools, "_download", download)
    reason = _target_tools._install_uv(tmp_path / "uv", "0.13.4")
    assert reason == "uv 0.13.4: the release publishes no checksum"
    assert not (tmp_path / "uv/uv").exists()


def test_a_cached_uv_that_was_never_verified_is_not_run(monkeypatch, tmp_path):
    # A uv cached before checksums were checked (or by anything else) is verified again, not trusted.
    cached = tmp_path / ".cache/flyte-agent-factory/uv/0.13.4"
    cached.mkdir(parents=True)
    (cached / "uv").write_text("#!/bin/sh\necho unverified\n")
    _release(monkeypatch, digest="0" * 64)
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    reason = _target_tools._ensure_uv(env, "0.13.4")
    assert "checksum" in reason
    assert not env["PATH"].startswith(str(cached))
    asked = _release(monkeypatch)
    assert _target_tools._ensure_uv(env, "0.13.4") == ""
    assert asked and "0.13.4" in (cached / "uv").read_text()
    asked.clear()
    assert _target_tools._ensure_uv(dict(env), "0.13.4") == "" and asked == []  # verified once, then kept


def test_an_empty_published_checksum_is_no_checksum(monkeypatch, tmp_path):
    def download(url):
        return b"" if url.endswith(".sha256") else b"an archive nobody can verify"

    monkeypatch.setattr(_target_tools, "_download", download)
    assert (
        _target_tools._install_uv(tmp_path / "uv", "0.13.4") == "uv 0.13.4: the release publishes no checksum"
    )
    assert not (tmp_path / "uv/uv").exists()


def test_a_checksum_published_in_upper_case_is_accepted(monkeypatch, tmp_path):
    _release(monkeypatch)
    real = _target_tools._download
    monkeypatch.setattr(
        _target_tools, "_download", lambda url: real(url).upper() if url.endswith(".sha256") else real(url)
    )
    assert _target_tools._install_uv(tmp_path / "uv", "0.13.4") == ""
    assert (tmp_path / "uv/uv").is_file()


def test_a_job_that_needs_a_uv_it_cannot_download_is_not_run(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)

    def down(url):
        raise OSError("network is down")

    monkeypatch.setattr(_target_tools, "_download", down)
    result = _verdict(checkout, _uv_job("0.13.4"))
    assert result.status is _gate_base.Status.NOT_RUN
    assert "uv 0.13.4" in result.output and "network is down" in result.output


def test_a_uv_version_that_is_not_exact_is_not_run(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    result = _verdict(checkout, _uv_job(">=0.9"))
    assert result.status is _gate_base.Status.NOT_RUN
    assert ">=0.9" in result.output


# -- rustup, as ubuntu-latest has it --


def _fake_rustup(home, default=" (default)"):
    """A rustup that logs what it is asked and knows one toolchain, `nightly`."""
    bin_dir = home / ".cargo/bin"
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "rustup"
    tool.write_text(
        '#!/bin/sh\necho "$@" >> "$HOME/rustup.log"\n'
        'case "$1 $2" in "toolchain list") echo "nightly-x86_64-unknown-linux-gnu%s";; '
        '"component list") echo "rustfmt-x86_64-unknown-linux-gnu";; esac\n' % default
    )
    tool.chmod(0o755)
    _fake_cargo(bin_dir)


def test_the_jobs_own_rustup_steps_run_before_its_checks(checkout, monkeypatch, tmp_path):
    home = _no_cargo(monkeypatch, tmp_path)
    _fake_rustup(home)
    install = _named(
        "Install nightly toolchain",
        ["rustup toolchain install nightly", "rustup component add --toolchain nightly rustfmt"],
    )
    result = _verdict(
        checkout,
        install + _named("fmt", ['grep -q "toolchain install nightly" "$HOME/rustup.log"', "cargo fmt"]),
    )
    assert result.status is _gate_base.Status.PASS, result.output
    log = (home / "rustup.log").read_text()
    assert "toolchain install nightly" in log and "component add --toolchain nightly rustfmt" in log


def test_a_build_script_that_calls_a_missing_rustup_is_noticed_whatever_the_exit_code(
    checkout, monkeypatch, tmp_path
):
    _no_cargo(monkeypatch, tmp_path)
    fails = "echo 'make: rustup: No such file or directory' >&2; exit 2"
    result = _verdict(checkout, _named("fmt", [fails]))
    assert result.status is _gate_base.Status.NOT_RUN
    assert "network is down" in result.output


def test_a_rustup_step_makes_a_job_need_rust(checkout, monkeypatch, tmp_path):
    _no_cargo(monkeypatch, tmp_path)
    steps = _named("Install toolchain", ["rustup toolchain install nightly"]) + _named("fmt", ["make fmt"])
    result = _verdict(checkout, steps)
    assert result.status is _gate_base.Status.NOT_RUN
    assert "network is down" in result.output


# -- no silent pass --

UV_LOCK = "echo 'error: Failed to parse `uv.lock`' >&2; exit 2"
NOT_FOUND = "echo 'bash: line 1: uv: command not found' >&2; exit 127"
TOOLCHAIN = "echo \"error: toolchain 'nightly' is not installed\" >&2; exit 1"
MAKE_MISSING = "echo 'make: uvx: No such file or directory' >&2; exit 2"


@pytest.mark.parametrize(
    "failure", [UV_LOCK, NOT_FOUND, TOOLCHAIN, MAKE_MISSING], ids="lock cmd chain make".split()
)
def test_a_failure_of_the_environment_on_head_and_base_is_not_run_never_a_pass(checkout, failure):
    result = _ci_on_main(checkout, {}, _named("tests", [failure + "  # pytest"]))
    assert result.status is _gate_base.Status.NOT_RUN, result.output
    assert "NOT RUN" in result.output and "pre-existing" not in result.output


def test_a_genuine_test_failure_on_head_and_base_is_still_pre_existing(checkout):
    result = _ci_on_main(checkout, {"check.py": FAILS}, _check_step())
    assert result.status is _gate_base.Status.PASS and "pre-existing" in result.output


def test_a_test_that_mentions_a_missing_file_is_a_genuine_failure(checkout):
    sad = (
        "import sys\nprint('FileNotFoundError: [Errno 2] No such file or directory: data.csv')\nsys.exit(1)\n"
    )
    result = _ci_on_main(checkout, {"check.py": sad}, _check_step())
    assert result.status is _gate_base.Status.PASS and "pre-existing" in result.output


def test_a_runner_has_stable_as_the_default_toolchain(checkout, monkeypatch, tmp_path):
    home = _no_cargo(monkeypatch, tmp_path)
    _fake_rustup(home, default="")
    install = _named("Install toolchain", ["rustup toolchain install"])
    assert _verdict(checkout, install + _named("lint", ["cargo clippy"])).status is _gate_base.Status.PASS
    assert "default stable" in (home / "rustup.log").read_text().splitlines()


def test_build_targets_get_the_uv_the_workflows_install(checkout):
    steps = _uses("astral-sh/setup-uv@v10", '"0.13.4"') + _named("lint", ["make lint"])
    _only_steps(checkout, steps)
    (checkout / "Makefile").write_text("fmt:\n\ttrue\n")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-m", "make")
    uvs = {c.command: c.uv for c in _target_ci.discover(checkout)}
    assert uvs == {"make lint": "0.13.4", "make fmt": "0.13.4"}


def test_coverage_findings_are_the_failing_test_ids_when_pytest_names_them():
    out = (
        "  asyncio:base_events.py:1833 Task exception was never retrieved ERROR\n"
        "=== short test summary info ===\n"
        "FAILED tests/test_a.py::test_one - AssertionError: boom\n"
        "ERROR tests/test_b.py::test_two\n"
    )
    assert _target_run._findings(_target_run.Run(1, out)) == {
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    }
