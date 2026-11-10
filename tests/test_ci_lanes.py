"""`.github/ci_lanes.py`: which CI jobs a change can affect. A wrong `false` turns a check green unrun."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("ci_lanes", ROOT / ".github" / "ci_lanes.py")
assert _SPEC and _SPEC.loader
ci_lanes = importlib.util.module_from_spec(_SPEC)
sys.modules["ci_lanes"] = ci_lanes
_SPEC.loader.exec_module(ci_lanes)

ALL = {"fast": True, "slow": True, "lint": True}


@pytest.fixture(scope="module")
def closure() -> set[str]:
    found: set[str] = ci_lanes.slow_closure()
    return found


def test_the_slow_closure_starts_at_every_slow_test_file(closure):
    marked = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").glob("test_*.py")
        if "mark.slow" in p.read_text() and p.name != "test_ci_lanes.py"
    }
    assert marked and marked <= closure
    assert "tests/test_browser_gate.py" in marked and "tests/test_slow_lane.py" in marked


def test_the_slow_closure_follows_imports_transitively(closure):
    # Imported by the slow tests directly, through a relative import inside the package, and from ops/.
    for path in (
        "src/flyte_agent_factory/_bench_check.py",
        "src/flyte_agent_factory/_browser_gate.py",
        "src/flyte_agent_factory/_slow_lane.py",
        "src/flyte_agent_factory/__init__.py",
        "ops/bench_seed.py",
        "tests/fake_flyte.py",
    ):
        assert path in closure, path
    # Nothing the slow tests import reaches these.
    assert "ops/scan_public.py" not in closure


@pytest.mark.parametrize(
    "changed, expected",
    [
        (["docs/setup.md"], {"fast": True, "slow": False, "lint": False}),
        (["README.md", "profiles/README.md"], {"fast": True, "slow": False, "lint": False}),
        (["ops/README.md"], {"fast": True, "slow": False, "lint": True}),
        (["ops/scan_public.py"], {"fast": True, "slow": False, "lint": True}),
        (["src/flyte_agent_factory/_bench_check.py"], ALL),
        (["tests/test_bench_task_tf_bucket.py"], ALL),
        (["tests/fixtures/bench/shortlinks_ref/app.py"], ALL),
        (["standards/rules.md"], {"fast": True, "slow": True, "lint": False}),
        (["src/flyte_agent_factory/_assets/browser/install.sh"], ALL),
        # Other projects' harness files: nothing reads them, but shellcheck covers every tracked .sh.
        ([".claude-flyte/demo/ACCEPTANCE.md"], {"fast": False, "slow": False, "lint": False}),
        ([".claude-flyte/demo/checks.sh"], {"fast": False, "slow": False, "lint": True}),
        # The CI definition and the test environment move every lane.
        ([".github/ci_lanes.py"], ALL),
        ([".github/workflows/checks.yml"], ALL),
        (["pyproject.toml"], ALL),
        (["uv.lock"], ALL),
        (["factory-improvement/checks.sh"], ALL),
        (["tests/conftest.py"], ALL),
        ([], {"fast": False, "slow": False, "lint": False}),
    ],
)
def test_lanes(changed, expected):
    assert ci_lanes.lanes(changed) == expected


def test_a_change_to_any_slow_closure_file_runs_the_slow_lane(closure, monkeypatch):
    # The closure of this tree, computed once: each `lanes` call would walk the whole tree again (minutes).
    monkeypatch.setattr(ci_lanes, "slow_closure", lambda root=ci_lanes.ROOT: closure)
    for path in sorted(closure):
        assert ci_lanes.lanes([path])["slow"], path


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _outputs(text: str) -> dict[str, bool]:
    pairs = dict(line.split("=", 1) for line in text.splitlines())
    return {k: v == "true" for k, v in pairs.items()}


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    _run("git", "init", "-q", cwd=tmp_path)
    for key, value in (("user.email", "ci@example.invalid"), ("user.name", "ci")):
        _run("git", "config", key, value, cwd=tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("a\n")
    _run("git", "add", ".", cwd=tmp_path)
    _run("git", "commit", "-qm", "base", cwd=tmp_path)
    base = _run("git", "rev-parse", "HEAD", cwd=tmp_path)
    (tmp_path / "docs" / "a.md").write_text("b\n")
    _run("git", "commit", "-qam", "change", cwd=tmp_path)
    return tmp_path, base, _run("git", "rev-parse", "HEAD", cwd=tmp_path)


def test_changed_files_lists_the_diff(repo):
    root, base, head = repo
    assert ci_lanes.changed_files(base, head, root) == ["docs/a.md"]


def test_changed_files_returns_non_ascii_names_as_written(repo):
    # Without -z git prints "ops/caf\303\251.py" (quoted, octal), which no suffix test matches: lint=false.
    root, base, _ = repo
    (root / "ops").mkdir()
    (root / "ops" / "café.py").write_text("x = 1\n")
    _run("git", "add", ".", cwd=root)
    _run("git", "commit", "-qm", "accent", cwd=root)
    head = _run("git", "rev-parse", "HEAD", cwd=root)
    changed = ci_lanes.changed_files(base, head, root)
    assert changed == ["docs/a.md", "ops/café.py"]
    assert ci_lanes.lanes(changed)["lint"] is True


@pytest.mark.parametrize("base", ["0" * 40, "", "HEAD^", "not-a-sha", "f" * 40])
def test_changed_files_fails_closed_without_a_usable_base(repo, base):
    # All-zero is a new branch's `before`; an unknown sha is what a force push leaves (and cannot be fetched).
    root, _, head = repo
    assert ci_lanes.changed_files(base, head, root) is None


def test_the_cli_prints_github_outputs_and_runs_everything_when_it_cannot_list(repo):
    script = str(ROOT / ".github" / "ci_lanes.py")
    root, base, _ = repo
    assert _outputs(_run(sys.executable, script, "--all", cwd=root)) == ALL
    assert _outputs(_run(sys.executable, script, "--base", "0" * 40, cwd=root)) == ALL
    # From inside this repository a docs-only diff is measured with this repository's rules.
    head = _run("git", "rev-parse", "HEAD", cwd=ROOT)
    out = _run(sys.executable, script, "--base", head, "--head", head, cwd=ROOT)
    assert _outputs(out) == {"fast": False, "slow": False, "lint": False}


def test_a_loop_pull_request_cannot_change_what_ci_skips():
    from flyte_agent_factory import _loop

    assert _loop.classify([".github/ci_lanes.py"]) == "needs_owner"


# The public-cutover checks.sh, which runs both lanes as children (`_slow_lane --no-local --on-demand`): fast
# lane retried after the slow one, exit 74 terminal. Other projects' harness scripts call `_slow_lane` their
# own way and are not held to this contract. Harness directories are left out of the public tree, so there
# the list is empty and these tests collect no cases (an empty parametrisation, not a skip).
_LANE_SCRIPTS = [
    p
    for p in sorted(ROOT.glob(".claude-flyte/public-cutover/checks.sh"))
    if "_slow_lane --no-local" in p.read_text()
]


def _gone(pid: int, within: float = 2.0) -> bool:
    """Whether `pid` has exited within `within` seconds: absent, or a zombie its new parent has not reaped."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
        except (ProcessLookupError, FileNotFoundError):
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("fast_lane", ["child", "here"])
@pytest.mark.parametrize("script", _LANE_SCRIPTS, ids=lambda p: p.parent.name)
def test_terminating_the_checks_script_stops_its_launcher(script, fast_lane, tmp_path):
    # TERM while both lanes run in child actions, and while the fast lane runs here because no child could
    # take it (NOT_RUN, 75) beside the slow one nor after it: neither a launcher nor anything a lane
    # started may outlive the script.
    root = tmp_path / "repo"
    (root / script.parent.relative_to(ROOT)).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "t",
        ],
        cwd=root,
        check=True,
    )
    (root / script.relative_to(ROOT)).write_text(script.read_text())
    bin_dir = root / "bin"
    bin_dir.mkdir()
    # The local fast lane's `uv run python -m pytest`: a process that starts another and waits for it.
    (bin_dir / "uv").write_text("#!/bin/bash\nsleep 60 & echo $! > fast-here.pid; wait\n")
    (bin_dir / "uv").chmod(0o755)
    (root / "launcher.py").write_text(
        "import os, pathlib, sys, time\n"
        "if sys.argv[1] == '-':\n"
        "    print('IMAGE=test-image')\n"
        "    sys.exit(0)\n"
        "lane = 'fast' if 'not slow' in sys.argv else 'slow'\n"
        "if os.environ.get(f'FAKE_{lane.upper()}_EXIT'):\n"
        "    sys.exit(int(os.environ[f'FAKE_{lane.upper()}_EXIT']))\n"
        "pathlib.Path(f'{lane}-child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AGENT_FACTORY_SLOW_LANE": "child",
        "AGENT_FACTORY_SLOW_LANE_PYTHON": f"{sys.executable} {root / 'launcher.py'}",
        "AGENT_FACTORY_PYTEST_JUNIT_DIR": "",
    }
    if fast_lane == "here":
        env.update(FAKE_FAST_EXIT="75", FAKE_SLOW_EXIT="0")  # the slow lane is done; its node is free
    pid_files = ["slow-child.pid", "fast-child.pid"] if fast_lane == "child" else ["fast-here.pid"]
    proc = subprocess.Popen(
        ["bash", str(script.relative_to(ROOT))], cwd=root, env=env, start_new_session=True
    )
    try:
        deadline = time.monotonic() + 20
        while not all((root / name).is_file() for name in pid_files):
            assert time.monotonic() < deadline, "the script never started both lanes"
            time.sleep(0.05)
        time.sleep(0.3)  # let the pid files' writers finish, and the script reach its wait
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) != 0
        for name in pid_files:
            assert _gone(int((root / name).read_text())), f"{name.removesuffix('.pid')} outlived the script"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        for name in ("slow-child.pid", "fast-child.pid", "fast-here.pid"):  # lanes run in their own groups
            text = (root / name).read_text().strip() if (root / name).is_file() else ""
            if text.isdigit():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(text), signal.SIGKILL)


@pytest.mark.parametrize("script", _LANE_SCRIPTS, ids=lambda p: p.parent.name)
def test_a_lane_whose_upload_failed_fails_the_checks_without_a_local_run(script, tmp_path):
    # `_slow_lane` exits UPLOAD_FAILED (74) when the tree could not be uploaded after its retries: the checks
    # fail, and neither lane runs on this pod.
    root = tmp_path / "repo"
    (root / script.parent.relative_to(ROOT)).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "t",
        ],
        cwd=root,
        check=True,
    )
    (root / script.relative_to(ROOT)).write_text(script.read_text())
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(f'#!/bin/bash\necho "$*" >> {root / "uv.log"}\n')
    (bin_dir / "uv").chmod(0o755)
    (root / "launcher.py").write_text(
        "import sys\nif sys.argv[1] == '-':\n    print('IMAGE=test-image')\n    sys.exit(0)\nsys.exit(74)\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AGENT_FACTORY_SLOW_LANE": "child",
        "AGENT_FACTORY_SLOW_LANE_PYTHON": f"{sys.executable} {root / 'launcher.py'}",
        "AGENT_FACTORY_PYTEST_JUNIT_DIR": "",
    }
    proc = subprocess.run(
        ["bash", str(script.relative_to(ROOT))], cwd=root, env=env, capture_output=True, timeout=60
    )
    assert proc.returncode == 1
    ran = (root / "uv.log").read_text() if (root / "uv.log").is_file() else ""
    assert "pytest" not in ran and "_slow_lane" not in ran, ran


@pytest.mark.parametrize("script", _LANE_SCRIPTS, ids=lambda p: p.parent.name)
def test_an_unplaced_slow_lane_runs_here_while_the_fast_lane_tries_a_child_again(script, tmp_path):
    # Neither lane found a node. The slow lane freed none, so waiting for it before retrying the fast lane
    # only adds the fast retry's wait to the slow lane's run here: a pre-review ran out of its 900 s that way,
    # every test passing. The slow lane starts here at once; the fast lane, in a child, shares nothing here.
    root = tmp_path / "repo"
    (root / script.parent.relative_to(ROOT)).mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "t",
        ],
        cwd=root,
        check=True,
    )
    (root / script.relative_to(ROOT)).write_text(script.read_text())
    bin_dir = root / "bin"
    bin_dir.mkdir()
    # The local runs: the slow lane marks that it started; ruff passes; a local fast lane is logged.
    (bin_dir / "uv").write_text(
        f'#!/bin/bash\necho "$*" >> {root / "uv.log"}\n'
        f'[[ "$*" == *_slow_lane* ]] && touch {root / "slow-here.started"}\nexit 0\n'
    )
    (bin_dir / "uv").chmod(0o755)
    (root / "launcher.py").write_text(
        "import pathlib, sys, time\n"
        "if sys.argv[1] == '-':\n"
        "    print('IMAGE=test-image')\n"
        "    sys.exit(0)\n"
        "if 'not slow' not in sys.argv:\n"
        "    sys.exit(75)  # the slow lane is never placed\n"
        "tries = pathlib.Path('fast-tries')\n"
        "n = int(tries.read_text()) + 1 if tries.exists() else 1\n"
        "tries.write_text(str(n))\n"
        "if n == 1:\n"
        "    sys.exit(75)\n"
        "deadline = time.monotonic() + 10\n"
        "while not pathlib.Path('slow-here.started').exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        "pathlib.Path('fast-retry.saw').write_text(str(pathlib.Path('slow-here.started').exists()))\n"
        "sys.exit(0)\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AGENT_FACTORY_SLOW_LANE": "child",
        "AGENT_FACTORY_SLOW_LANE_PYTHON": f"{sys.executable} {root / 'launcher.py'}",
        "AGENT_FACTORY_PYTEST_JUNIT_DIR": "",
    }
    proc = subprocess.run(
        ["bash", str(script.relative_to(ROOT))], cwd=root, env=env, capture_output=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert (root / "fast-tries").read_text() == "2"
    assert (root / "fast-retry.saw").read_text() == "True", "the slow lane waited for the fast lane's retry"
    ran = (root / "uv.log").read_text()
    assert ran.count("_slow_lane") == 1, ran  # the slow lane ran here once
    assert "pytest tests" not in ran, ran  # the fast lane ran in its child, not here
