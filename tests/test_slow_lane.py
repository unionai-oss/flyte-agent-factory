"""The slow pytest lane: local by default, a child action with its own CPUs when asked."""

import asyncio
import dataclasses
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import flyte
import flyte.errors
import pytest
from flyte.extras import ContainerTask

from flyte_agent_factory import _images, _scanners, _slow_lane


def _fake_project(tmp_path: Path, exit_code: int = 0) -> Path:
    """A project whose `pytest` is a module that records its argv and writes a junit file."""
    (tmp_path / "pytest.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "argv = sys.argv[1:]\n"
        "Path('argv.txt').write_text(' '.join(argv))\n"
        "for a in argv:\n"
        "    if a.startswith('--junitxml='):\n"
        "        Path(a.split('=', 1)[1]).write_text('<testsuite/>')\n"
        f"print('local pytest ran'); sys.exit({exit_code})\n"
    )
    return tmp_path


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def no_run(monkeypatch):
    """Nothing is attachable: `attach()` raises as it does outside a Flyte run."""

    def refuse():
        raise RuntimeError("no run context")

    monkeypatch.setattr(_slow_lane, "_attach", refuse)


def test_runs_locally_without_the_env_var(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    junit = tmp_path / "slow.xml"

    code = _slow_lane.main(["--junit", str(junit), "-x"], submit=cast(Any, pytest.fail))

    assert code == 0
    assert junit.read_text() == "<testsuite/>"
    argv = (tmp_path / "argv.txt").read_text().split()
    assert argv[:5] == ["tests", "-q", "-m", "slow", f"--junitxml={junit}"] and argv[-1] == "-x"
    assert "local pytest ran" in capfd.readouterr().out


def test_local_exit_code_is_propagated(tmp_path, monkeypatch):
    monkeypatch.chdir(_fake_project(tmp_path, exit_code=1))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    assert _slow_lane.main(["--junit", str(tmp_path / "slow.xml")]) == 1


def test_no_attachable_run_falls_back_locally_and_says_so(tmp_path, monkeypatch, capfd, no_run):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=cast(Any, pytest.fail))

    assert code == 0
    captured = capfd.readouterr()
    assert "slow lane: running locally" in captured.err and "no run context" in captured.err
    assert "local pytest ran" in captured.out


def test_remote_result_is_written_and_its_exit_code_propagated(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    seen = {}

    def submit(args, interruptible):
        seen["args"], seen["interruptible"] = args, interruptible
        return 3, "remote tail\n", "<testsuite tests='1'/>"

    junit = tmp_path / "out" / "slow.xml"
    code = _slow_lane.main(["--junit", str(junit), "-k", "tf"], submit=submit)

    assert code == 3
    assert junit.read_text() == "<testsuite tests='1'/>"
    assert "remote tail" in capsys.readouterr().out
    assert seen == {"args": ["-k", "tf"], "interruptible": True}


def test_a_spot_queue_timeout_falls_back_to_a_local_run(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    assert attempts == [True]  # spot only, then here: on-demand is the scarce pool
    captured = capfd.readouterr()
    assert code == 0 and "local pytest ran" in captured.out
    assert "could not be placed" in captured.err


def test_no_local_leaves_an_unplaced_lane_to_the_caller(tmp_path, monkeypatch, capfd):
    """With --no-local the lane does not run here: the caller runs it once its own work is done."""
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code = _slow_lane.main(["--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit)

    captured = capfd.readouterr()
    assert code == _slow_lane.NOT_RUN
    assert "local pytest ran" not in captured.out and "could not be placed" in captured.err


def test_no_local_without_an_attachable_run_is_left_to_the_caller(tmp_path, monkeypatch, capfd, no_run):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")

    code = _slow_lane.main(["--no-local"], submit=cast(Any, pytest.fail))

    assert code == _slow_lane.NOT_RUN
    assert "local pytest ran" not in capfd.readouterr().out


def test_a_failure_other_than_queueing_is_not_retried_elsewhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise RuntimeError("task crashed")

    with pytest.raises(RuntimeError, match="task crashed"):
        _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)


def test_a_child_action_that_fails_before_reporting_runs_locally(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(_fake_project(tmp_path, exit_code=3))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise flyte.errors.RuntimeUserError("ModuleNotFoundError", "no module named x")

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    captured = capfd.readouterr()
    assert code == 3  # pytest's own code from the local run
    assert "the child action failed before reporting (RuntimeUserError:" in captured.err
    assert "local pytest ran" in captured.out


def test_the_spot_wait_is_bounded_short_and_the_request_fits_a_spot_node():
    assert _slow_lane.QUEUE_BOUND == timedelta(minutes=2)
    assert _slow_lane.task.resources is not None
    assert _slow_lane.task.resources.cpu == 8  # 12 and 16 found no spot capacity; 8 is placeable


def test_tree_tar_has_tracked_and_untracked_files_but_not_ignored_or_vcs(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (tmp_path / "tracked.py").write_text("t")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.py").write_text("n")  # untracked, not ignored
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("v")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "x.pyc").write_text("c")
    _git(tmp_path, "add", ".gitignore", "tracked.py")
    (tmp_path / "gone.py").write_text("g")
    _git(tmp_path, "add", "gone.py")
    (tmp_path / "gone.py").unlink()  # tracked but deleted in the working tree

    data = _slow_lane.pack_tree(tmp_path)

    names = set(tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getnames())
    assert names == {".gitignore", "tracked.py", "sub/new.py"}


def test_tree_tar_never_carries_dot_git_or_venv_even_if_listed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("c")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "a.py").write_text("a")
    (tmp_path / "ok.py").write_text("o")
    monkeypatch.setattr(_slow_lane, "_listed_files", lambda root: [".git/config", ".venv/a.py", "ok.py"])

    data = _slow_lane.pack_tree(tmp_path)

    assert tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").getnames() == ["ok.py"]


def _committed_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with one commit, then an uncommitted change and an untracked file in its working tree."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "src").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".venv/\n__pycache__/\n")
    (repo / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    (repo / "a.txt").write_text("committed\n")
    (repo / "tests" / "test_git.py").write_text(
        "import os, subprocess\n\n"
        "def _git(*args):\n"
        "    return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout\n\n"
        "def test_git_sees_head_and_the_working_tree():\n"
        "    report = os.path.join(os.environ['TMPDIR'], 'git-report.txt')\n"
        "    with open(report, 'w') as f:\n"
        "        f.write(_git('rev-parse', 'HEAD') + _git('status', '--porcelain'))\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "one")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.txt").write_text("changed\n")
    (repo / "b.txt").write_text("untracked\n")
    return repo, sha


def test_history_is_a_bundle_of_head(tmp_path):
    repo, sha = _committed_repo(tmp_path)
    bundle = tmp_path / "history"
    bundle.write_bytes(_slow_lane.pack_history(repo))

    heads = subprocess.run(
        ["git", "-C", str(repo), "bundle", "list-heads", str(bundle)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert sha in heads


def test_a_repository_without_commits_has_no_history(tmp_path):
    _git(tmp_path, "init", "-q")
    assert _slow_lane.pack_history(tmp_path) == b""


def test_a_subdirectory_project_has_no_history_of_its_own(tmp_path):
    # HEAD of the enclosing repository holds other paths: the container commits the packed tree instead.
    repo, _ = _committed_repo(tmp_path)
    sub = repo / "projects" / "p"
    sub.mkdir(parents=True)
    (sub / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "p")

    assert _slow_lane.pack_history(sub) == b""
    assert _slow_lane.pack_history(repo) != b""


def test_the_lane_task_takes_the_history_beside_the_tree():
    from flyte.io import File

    inputs = _slow_lane.task._inputs
    assert inputs is not None
    assert inputs["tree"] is File and inputs["history"] is File and inputs["mode"] is str


def test_unpacked_tree_runs_pytest_with_the_slow_marker(tmp_path):
    """The task body, minus the cluster: unpack, run the lane, return code, tail and junit."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "import pytest\n\n@pytest.mark.slow\ndef test_a():\n    pass\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")

    code, tail, junit = _slow_lane.run_in_tree(tmp_path, ["-p", "no:cacheprovider"], python=sys.executable)

    assert code == 0, tail
    assert "<testsuite" in junit


# The pytest lanes as a raw-container task: its command script, run here with bash against a tiny tree.


def _lane_tree(tmp_path: Path, tests: dict[str, str]) -> Path:
    """Pack a tree whose `uv sync` (faked) makes a `.venv/bin/python` that is this test process's own."""
    tree = tmp_path / "src-tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "src").mkdir()
    (tree / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    for name, body in tests.items():
        (tree / "tests" / name).write_text(body)
    inputs = tmp_path / "in"
    inputs.mkdir()
    with tarfile.open(inputs / "tree", "w:gz") as tar:
        for path in sorted(tree.rglob("*")):
            tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)
    return inputs


def _fake_bin(tmp_path: Path, terraform: bool) -> Path:
    """A `uv` that logs its arguments and builds the venv, and optionally a `terraform` already on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!/bin/bash\necho "$*" >> {tmp_path / "uv.log"}\n'
        "mkdir -p .venv/bin\n"
        f"printf '#!/bin/bash\\nexec {sys.executable} \"$@\"\\n' > .venv/bin/python\n"
        "chmod +x .venv/bin/python\n"
    )
    uv.chmod(0o755)
    if terraform:
        (bin_dir / "terraform").write_text("#!/bin/bash\n")
        (bin_dir / "terraform").chmod(0o755)
    return bin_dir


def _run_lane(
    tmp_path: Path,
    inputs: Path,
    args: list[str],
    terraform: bool = True,
    zip_url: str = "file:///nonexistent",
    sha: str = "0" * 64,
) -> Path:
    (inputs / "pytest_args").write_text(json.dumps(args))
    out = tmp_path / "out"
    # Without `terraform` the PATH must not hold a real one either, as a host that has it installed does.
    path = [
        d for d in os.environ["PATH"].split(os.pathsep) if terraform or not (Path(d) / "terraform").exists()
    ]
    env = {
        **os.environ,
        "PATH": os.pathsep.join([str(_fake_bin(tmp_path, terraform)), *path]),
        "TMPDIR": str(tmp_path),
    }
    cmd = [*_slow_lane.LANE_COMMAND, str(inputs), str(out), zip_url, sha, "12000"]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr  # the script always reports through its output files
    return out


def test_the_lane_script_reports_a_passing_run(tmp_path):
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})

    out = _run_lane(tmp_path, inputs, ["-q"])

    assert (out / "code").read_text() == "0"
    assert "1 passed" in (out / "tail").read_text()
    assert "<testsuite" in (out / "junit").read_text()
    assert "sync --frozen" in (tmp_path / "uv.log").read_text()


def test_the_lane_script_reports_a_failing_run_and_passes_the_arguments_through(tmp_path):
    inputs = _lane_tree(
        tmp_path,
        {
            "test_a.py": "def test_a():\n    assert False\n",
            "test_b.py": "def test_b():\n    pass\n",
        },
    )

    out = _run_lane(tmp_path, inputs, ["-k", "test_a or test_b", "-m", "not slow"])

    assert (out / "code").read_text() == "1"
    assert "1 failed, 1 passed" in (out / "tail").read_text()
    assert 'failures="1"' in (out / "junit").read_text()


def test_the_lane_script_restores_head_so_git_sees_the_same_tree_as_the_pod(tmp_path):
    """Tests that read git (tracked files, HEAD) failed in the child: the tree tar carries no `.git`."""
    repo, sha = _committed_repo(tmp_path)
    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "tree").write_bytes(_slow_lane.pack_tree(repo))
    (inputs / "history").write_bytes(_slow_lane.pack_history(repo))

    out = _run_lane(tmp_path, inputs, ["-q"])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert (tmp_path / "git-report.txt").read_text() == f"{sha}\n M a.txt\n?? b.txt\n"


def _command_lane(tmp_path: Path, command: list[str], lock: bool) -> Path:
    inputs = _lane_tree(tmp_path, {})
    if lock:
        (tmp_path / "src-tree" / "uv.lock").write_text("")
        with tarfile.open(inputs / "tree", "w:gz") as tar:
            for path in sorted((tmp_path / "src-tree").rglob("*")):
                tar.add(path, arcname=path.relative_to(tmp_path / "src-tree").as_posix(), recursive=False)
    (inputs / "pytest_args").write_text(json.dumps(command))
    out = tmp_path / "out"
    env = {**os.environ, "PATH": f"{_fake_bin(tmp_path, True)}{os.pathsep}{os.environ['PATH']}"}
    env["TMPDIR"] = str(tmp_path)
    (inputs / "mode").write_text("command")
    cmd = [*_slow_lane.LANE_COMMAND, str(inputs), str(out), "file:///nonexistent", "0" * 64, "12000"]
    assert subprocess.run(cmd, env=env, capture_output=True, text=True).returncode == 0
    return out


def test_the_lane_script_runs_a_command_and_reports_its_exit_code(tmp_path):
    command = ["bash", "-c", 'echo "$AGENT_FACTORY_LANE_JUNIT" > "$PWD/../junit-path"; echo hi; exit 3']
    out = _command_lane(tmp_path, command, lock=False)

    assert (out / "code").read_text() == "3"
    assert "hi" in (out / "tail").read_text()
    assert not (tmp_path / "uv.log").exists(), "no uv.lock: nothing to sync"


def test_the_lane_script_syncs_a_command_project_that_has_a_lock(tmp_path):
    out = _command_lane(tmp_path, ["bash", "-c", "command -v python"], lock=True)

    assert (out / "code").read_text() == "0"
    assert "sync --frozen" in (tmp_path / "uv.log").read_text()
    assert "/.venv/bin/python" in (out / "tail").read_text()


def test_the_lane_script_keeps_only_the_tail_of_a_long_output(tmp_path):
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    print('x' * 50000)\n    assert False\n"})

    out = _run_lane(tmp_path, inputs, [])

    assert len((out / "tail").read_bytes()) <= 12000


def test_the_lane_script_installs_a_pinned_terraform_that_is_not_on_path(tmp_path):
    zip_path = tmp_path / "terraform.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("terraform", "#!/bin/bash\necho fake-terraform\n")
    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    runs_terraform = "import subprocess\n\ndef test_tf():\n    subprocess.run(['terraform'], check=True)\n"
    inputs = _lane_tree(tmp_path, {"test_tf.py": runs_terraform})

    out = _run_lane(tmp_path, inputs, [], terraform=False, zip_url=zip_path.as_uri(), sha=sha)

    assert (out / "code").read_text() == "0", (out / "tail").read_text()


def test_the_lane_script_refuses_a_terraform_whose_checksum_differs(tmp_path):
    zip_path = tmp_path / "terraform.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("terraform", "#!/bin/bash\n")
    inputs = _lane_tree(tmp_path, {"test_a.py": "def test_a():\n    pass\n"})

    out = _run_lane(tmp_path, inputs, [], terraform=False, zip_url=zip_path.as_uri())

    assert (out / "code").read_text() == str(_slow_lane.SETUP_FAILED)
    assert "checksum" in (out / "tail").read_text()
    assert (out / "junit").exists()  # an output the task declares is always there


def test_the_lane_runs_the_baked_scanners_with_no_network_and_no_fallback(tmp_path, monkeypatch):
    """The image bakes gitleaks and tflint: the lane uses them as found, and fetches nothing for them."""
    baked = tmp_path / "baked"
    baked.mkdir()
    for scanner in _scanners.SCANNERS.values():
        (baked / scanner.name).write_text(f"#!/bin/bash\necho baked-{scanner.name}\n")
        (baked / scanner.name).chmod(0o755)
    # Every proxied request goes to a closed port, and the release hosts resolve nowhere.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    monkeypatch.setenv("PATH", os.pathsep.join([str(baked), os.environ["PATH"]]))
    runs_both = (
        "import subprocess\n\n"
        "def test_scanners():\n"
        "    for tool in ('gitleaks', 'tflint'):\n"
        "        out = subprocess.run([tool], capture_output=True, text=True, check=True).stdout\n"
        "        assert out == f'baked-{tool}\\n'\n"
    )
    inputs = _lane_tree(tmp_path, {"test_scanners.py": runs_both})

    out = _run_lane(tmp_path, inputs, [])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "1 passed" in (out / "tail").read_text()


def test_no_lane_or_gate_module_downloads_a_scanner():
    package = Path(_slow_lane.__file__).parent
    modules = [package / "_slow_lane.py", *package.glob("_gate*.py")]
    assert len(modules) > 2
    for module in modules:
        text = module.read_text()
        for scanner in _scanners.SCANNERS.values():
            assert scanner.url not in text and f"{scanner.name}/releases" not in text, module.name


def test_the_lane_task_is_a_raw_container_registered_in_its_own_environment():
    task = _slow_lane.task
    assert isinstance(task, ContainerTask)
    assert task.name == "test-lane"
    assert task._cmd == _slow_lane.LANE_COMMAND  # no public accessor for the command
    assert task.resources is not None and task.resources.cpu == 8 and task.resources.memory == "8Gi"
    assert task.interruptible is True  # `.override()` drops a ContainerTask's command: set at construction
    assert task.timeout == flyte.Timeout(
        max_queued_time=_slow_lane.QUEUE_BOUND, max_runtime=timedelta(minutes=30)
    )
    assert _slow_lane.env.name == "test-lanes"
    assert list(_slow_lane.env.tasks) == ["test-lane"]


def test_the_on_demand_lane_task_is_the_same_container_off_spot():
    """`.override()` drops a ContainerTask's command, so on-demand is a second task, in its own env."""
    spot, on_demand = _slow_lane.task, _slow_lane.task_on_demand
    assert isinstance(on_demand, ContainerTask)
    assert on_demand.name == "test-lane-on-demand"
    assert on_demand._cmd == spot._cmd and on_demand.image == spot.image
    assert on_demand.resources == spot.resources and on_demand.timeout == spot.timeout
    assert on_demand.interruptible is False
    assert list(_slow_lane.env_on_demand.tasks) == ["test-lane-on-demand"]


def test_with_on_demand_a_spot_queue_timeout_tries_on_demand_before_the_local_run(
    tmp_path, monkeypatch, capfd
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    attempts = []

    def submit(args, interruptible):
        attempts.append(interruptible)
        if interruptible:
            raise flyte.errors.MaxQueuedTimeExceededError("queued too long")
        return 0, "on-demand tail\n", "<testsuite/>"

    code = _slow_lane.main(
        ["--on-demand", "--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit
    )

    assert attempts == [True, False]
    assert code == 0 and (tmp_path / "slow.xml").read_text() == "<testsuite/>"
    assert "on-demand tail" in capfd.readouterr().out


def test_with_on_demand_both_queue_timeouts_leave_the_lane_unplaced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    assert _slow_lane.main(["--on-demand", "--no-local"], submit=submit) == _slow_lane.NOT_RUN


def test_the_pinned_terraform_is_the_one_the_benchmark_image_installs():
    assert _slow_lane.TERRAFORM_VERSION == _images.TERRAFORM_VERSION
    assert _slow_lane.TERRAFORM_SHA256 == _images.TERRAFORM_SHA256
    assert _slow_lane.TERRAFORM_URL.endswith(
        f"/terraform/{_images.TERRAFORM_VERSION}/terraform_{_images.TERRAFORM_VERSION}_linux_amd64.zip"
    )


def test_a_lane_whose_setup_failed_runs_locally_instead(tmp_path, monkeypatch, capfd):
    monkeypatch.chdir(_fake_project(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def submit(args, interruptible):
        return _slow_lane.SETUP_FAILED, "uv: no network\n", ""

    code = _slow_lane.main(["--junit", str(tmp_path / "slow.xml")], submit=submit)

    captured = capfd.readouterr()
    assert code == 0 and "local pytest ran" in captured.out
    assert "setup failed" in captured.err and "uv: no network" in captured.err


def test_only_the_exit_code_is_a_scalar_output():
    """A raw container's scalar output is capped at 1 KiB; a 12,000-character tail failed each lane."""
    from flyte.io import File

    outputs = _slow_lane.task._outputs
    assert outputs is not None
    assert outputs["code"] is str
    assert outputs["tail"] is File and outputs["junit"] is File


def test_a_refused_tree_upload_is_retried_with_a_fresh_url(tmp_path, monkeypatch, capsys):
    # A presigned URL can be refused (HTTP 400) or expire; each attempt asks for a new one, so the lane still
    # reaches its child rather than falling back to the shared pod.
    calls: list[Path] = []

    async def upload(path: Path) -> tuple[str, str]:
        calls.append(path)
        if len(calls) < 3:
            raise flyte.errors.RuntimeSystemError("HTTPStatusError", "Client error '400 Bad Request'")
        return "md5", "s3://bucket/tree.tar.gz"

    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    tarball = tmp_path / "tree.tar.gz"
    tarball.write_bytes(b"tree")
    uri = asyncio.run(_slow_lane._upload_tree(tarball, upload=upload, sleep=sleep))
    assert uri == "s3://bucket/tree.tar.gz"
    assert calls == [tarball] * 3 and len(waits) == 2 and all(w > 0 for w in waits)
    assert capsys.readouterr().err.count("upload of the tree failed") == 2


def test_a_tree_upload_that_keeps_failing_raises_after_its_attempts(tmp_path):
    calls: list[Path] = []

    async def upload(path: Path) -> tuple[str, str]:
        calls.append(path)
        raise flyte.errors.RuntimeSystemError("HTTPStatusError", "Client error '400 Bad Request'")

    async def sleep(seconds: float) -> None:
        return None

    tarball = tmp_path / "tree.tar.gz"
    tarball.write_bytes(b"tree")
    with pytest.raises(flyte.errors.RuntimeSystemError):
        asyncio.run(_slow_lane._upload_tree(tarball, upload=upload, sleep=sleep))
    assert len(calls) == _slow_lane.UPLOAD_ATTEMPTS > 1


def test_the_lane_script_gives_the_tree_a_repository_tracking_exactly_what_was_packed(tmp_path):
    # The packed tree has no `.git`; tests that read the repository (`git ls-files`, `git archive HEAD`) need
    # one, tracking the packed files, including one tracked in the checkout despite an ignore rule.
    probe = (
        "import subprocess\n"
        "def git(*a):\n"
        "    return subprocess.run(['git', *a], check=True, capture_output=True, text=True).stdout\n"
        "def test_the_repository():\n"
        "    files = set(git('ls-files').split())\n"
        "    assert files == {'.gitignore', 'dist/skill.zip', 'pytest.ini', 'tests/test_git.py'}, files\n"
        "    assert len(git('rev-list', '--all').split()) == 1\n"
        "    assert git('check-ignore', '--no-index', 'dist/skill.zip').strip() == 'dist/skill.zip'\n"
    )
    inputs = tmp_path / "in"
    inputs.mkdir()
    tree = tmp_path / "src-tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "dist").mkdir()
    (tree / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n")
    (tree / ".gitignore").write_text("dist/\n.venv/\n")
    (tree / "dist" / "skill.zip").write_bytes(b"generated")
    (tree / "tests" / "test_git.py").write_text(probe)
    with tarfile.open(inputs / "tree", "w:gz") as tar:
        for path in sorted(tree.rglob("*")):
            tar.add(path, arcname=path.relative_to(tree).as_posix(), recursive=False)

    out = _run_lane(tmp_path, inputs, ["-q", "-m", "not slow"])

    assert (out / "code").read_text() == "0", (out / "tail").read_text()
    assert "1 passed" in (out / "tail").read_text()


def _gitleaks_tarball(tmp_path: Path) -> tuple[str, str]:
    """A release-shaped gitleaks archive (the binary at its root) and its sha256."""
    binary = tmp_path / "gitleaks-bin"
    binary.write_text("#!/bin/bash\necho fake-gitleaks\n")
    binary.chmod(0o755)
    archive = tmp_path / "gitleaks.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="gitleaks")
    return archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_lane_script_installs_a_pinned_gitleaks_that_is_not_on_path(tmp_path):
    """The installer unpacks a gitleaks archive that verifies against its pin into an executable binary."""
    url, sha = _gitleaks_tarball(tmp_path)
    scanner = dataclasses.replace(_scanners.GITLEAKS, url=url, sha256=sha)
    binary = _scanners.install(
        scanner,
        tmp_path / "cache",
        tmp_path / "installed",
        fetch=lambda _: (tmp_path / scanner.archive).read_bytes(),
    )

    result = subprocess.run([binary], check=True, capture_output=True, text=True, timeout=10)
    assert result.stdout == "fake-gitleaks\n"


def test_the_lane_script_refuses_a_gitleaks_whose_checksum_differs(tmp_path):
    """A gitleaks archive that does not match its pinned sha256 fails the install and leaves no binary."""
    url, _ = _gitleaks_tarball(tmp_path)
    scanner = dataclasses.replace(_scanners.GITLEAKS, url=url, sha256="0" * 64)

    with pytest.raises(_scanners.Unavailable, match="checksum"):
        _scanners.install(
            scanner,
            tmp_path / "cache",
            tmp_path / "installed",
            fetch=lambda _: (tmp_path / scanner.archive).read_bytes(),
            sleep=lambda _: None,
        )
    assert not (tmp_path / "installed" / scanner.name).exists()


def test_the_lane_installs_the_gitleaks_ci_pins():
    """The image carries gitleaks at the pin; the lane task's arguments hold no scanner URL or checksum."""
    scanner = _scanners.GITLEAKS
    assert scanner.url in _images.TOOLS[scanner.name]
    assert scanner.sha256 in _images.TOOLS[scanner.name]
    arguments = list(_slow_lane.task._args or [])
    assert arguments == [
        "/var/inputs",
        "/var/outputs",
        _slow_lane.TERRAFORM_URL,
        _slow_lane.TERRAFORM_SHA256,
        str(_slow_lane.TAIL_CHARS),
    ]


def test_exhausted_upload_retries_fail_the_lane_without_a_local_run(monkeypatch):
    # A tree that could not be uploaded is a failed lane, not an unplaced one: neither the caller (75) nor
    # this process may then run it on the shared pod.
    monkeypatch.setenv(_slow_lane.ENV_VAR, "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def no_local(junit, pytest_args):
        pytest.fail("the lane ran locally")

    monkeypatch.setattr(_slow_lane, "_run_local", no_local)

    async def upload(path: Path) -> tuple[str, str]:
        raise flyte.errors.RuntimeSystemError("UploadFailed", "HTTP 400")

    async def sleep(seconds: float) -> None:
        return None

    def submit(pytest_args: list[str], interruptible: bool) -> tuple[int, str, str]:
        asyncio.run(_slow_lane._upload_tree(Path("unused"), upload=upload, sleep=sleep))
        raise AssertionError("the upload cannot succeed")

    assert _slow_lane.main(["--no-local", "--on-demand"], submit=submit) == _slow_lane.UPLOAD_FAILED
    assert _slow_lane.main(["--on-demand"], submit=submit) == _slow_lane.UPLOAD_FAILED
    assert _slow_lane.UPLOAD_FAILED not in {0, 1, 2, 3, 4, 5, _slow_lane.SETUP_FAILED, _slow_lane.NOT_RUN}


@pytest.fixture
def local_pytest_must_not_run(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("pytest ran locally")

    monkeypatch.setattr(_slow_lane, "_run_local", refuse)


def _no_local(tmp_path, monkeypatch, submit, capfd, attach=lambda: None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", attach)
    code = _slow_lane.main(["--no-local", "--junit", str(tmp_path / "slow.xml")], submit=submit)
    return code, capfd.readouterr().err


def test_not_run_is_a_code_pytest_never_uses():
    assert _slow_lane.NOT_RUN not in range(0, 6) and _slow_lane.NOT_RUN != _slow_lane.SETUP_FAILED


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_no_run_can_be_attached(tmp_path, monkeypatch, capfd):
    def refuse():
        raise RuntimeError("no run context")

    code, err = _no_local(tmp_path, monkeypatch, cast(Any, pytest.fail), capfd, attach=refuse)

    assert code == _slow_lane.NOT_RUN and "no run context" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_spot_capacity_is_missing(tmp_path, monkeypatch, capfd):
    def submit(args, interruptible):
        raise flyte.errors.MaxQueuedTimeExceededError("queued too long")

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.NOT_RUN and "no spot capacity" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_the_child_fails_before_reporting(tmp_path, monkeypatch, capfd):
    def submit(args, interruptible):
        raise flyte.errors.RuntimeUserError("ModuleNotFoundError", "no module named x")

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.NOT_RUN and "failed before reporting" in err


@pytest.mark.usefixtures("local_pytest_must_not_run")
def test_no_local_reports_not_run_when_setup_failed_in_the_child(tmp_path, monkeypatch, capfd):
    def submit(args, interruptible):
        return _slow_lane.SETUP_FAILED, "uv: no network\n", ""

    code, err = _no_local(tmp_path, monkeypatch, submit, capfd)

    assert code == _slow_lane.NOT_RUN and "setup failed" in err


def test_no_local_still_returns_a_lane_that_ran_in_the_child(tmp_path, monkeypatch, capfd):
    code, _ = _no_local(tmp_path, monkeypatch, lambda args, interruptible: (1, "failed\n", "<x/>"), capfd)

    assert code == 1 and (tmp_path / "slow.xml").read_text() == "<x/>"


@pytest.mark.parametrize("code", range(6))
def test_remote_completion_precedes_junit_failure(tmp_path, monkeypatch, code):
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    result = tmp_path / "result.json"
    junit = tmp_path / "directory"
    junit.mkdir()

    with pytest.raises(IsADirectoryError):
        _slow_lane.main(
            ["--no-local", "--result", str(result), "--junit", str(junit)],
            submit=lambda args, spot: (code, "pytest finished", ""),
        )

    assert json.loads(result.read_text()) == {"completed": True, "code": code}


def test_not_run_leaves_no_completion(tmp_path, monkeypatch, no_run):
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    result = tmp_path / "result.json"

    assert _slow_lane.main(["--no-local", "--result", str(result)]) == _slow_lane.NOT_RUN
    assert not result.exists()


@pytest.mark.parametrize("code", range(6))
def test_local_completion_is_optional_and_preserves_pytest_arguments(tmp_path, monkeypatch, code):
    monkeypatch.chdir(_fake_project(tmp_path, code))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.delenv("AGENT_FACTORY_SLOW_LANE", raising=False)
    result = tmp_path / "result.json"

    assert _slow_lane.main(["--result", str(result), "-x"]) == code
    assert json.loads(result.read_text()) == {"completed": True, "code": code}
    assert (tmp_path / "argv.txt").read_text() == "tests -q -m slow -x"


async def test_completion_precedes_remote_report_download(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from flyte import remote
    from flyte.io import File

    async def upload(path):
        return None, "s3://test-bucket/input"

    async def download(self, path):
        raise OSError("report download failed")

    async def submit(**kwargs):
        return (
            "2",
            File.from_existing_remote("s3://test-bucket/tail"),
            File.from_existing_remote("s3://test-bucket/junit"),
        )

    monkeypatch.setattr(remote.upload_file, "aio", upload)
    monkeypatch.setattr(_slow_lane, "pack_tree", lambda root: b"")
    monkeypatch.setattr(_slow_lane, "pack_history", lambda root: b"")
    monkeypatch.setattr(_slow_lane, "task", SimpleNamespace(aio=submit))
    monkeypatch.setattr(File, "download", download)
    result = tmp_path / "result.json"

    with pytest.raises(OSError, match="report download failed"):
        await _slow_lane._submit_async([], True, result=result)

    assert json.loads(result.read_text()) == {"completed": True, "code": 2}


# The launcher learns that its child finished from a watch on the run. When that watch drops and reconnects,
# the completion can be lost: a pre-review's slow child succeeded at 13:41 and its launcher waited until
# checks.sh's 900 s ran out at 13:48. The launcher also asks the cluster itself.


def test_a_lane_whose_completion_the_watch_lost_is_read_from_the_cluster():
    async def never() -> tuple[str, str, str]:
        await asyncio.Event().wait()  # the watch never delivers
        raise AssertionError("unreachable")

    polls: list[int] = []

    async def finished() -> tuple[str, str, str] | None:
        polls.append(1)
        return ("0", "tail-uri", "junit-uri") if len(polls) >= 2 else None

    got = asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.02))
    assert got == ("0", "tail-uri", "junit-uri")
    assert len(polls) >= 2


def test_a_lane_the_watch_reports_is_taken_from_the_watch():
    async def soon() -> str:
        await asyncio.sleep(0.01)
        return "from-watch"

    async def finished() -> str | None:
        return None

    assert asyncio.run(_slow_lane._settle(soon(), finished, poll_s=0.05, grace_s=0.05)) == "from-watch"


def test_the_watch_still_wins_within_the_grace_after_the_cluster_says_done():
    # The poll may see the end first; the watch's own answer, arriving within the grace, is the one used.
    async def late() -> str:
        await asyncio.sleep(0.05)
        return "from-watch"

    async def finished() -> str | None:
        return "from-poll"

    assert asyncio.run(_slow_lane._settle(late(), finished, poll_s=0.01, grace_s=1.0)) == "from-watch"


def test_a_poll_that_fails_does_not_end_the_wait():
    async def soon() -> str:
        await asyncio.sleep(0.05)
        return "from-watch"

    async def finished() -> str | None:
        raise OSError("connection reset")  # one of the remote errors

    assert asyncio.run(_slow_lane._settle(soon(), finished, poll_s=0.01, grace_s=0.01)) == "from-watch"


def test_a_child_that_ended_without_succeeding_is_a_runtime_error():
    async def never() -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def finished() -> str | None:
        raise _slow_lane.LaneEnded("RuntimeSystemError", "child action x ended failed")

    with pytest.raises(flyte.errors.BaseRuntimeError):
        asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.01))


def test_the_outputs_are_read_from_the_actions_literals():
    # Shaped as `ActionDetails.output_literals()` returned them for a real lane child: `code` a string
    # primitive, `tail` and `junit` blobs.
    from types import SimpleNamespace as NS

    def blob(uri: str) -> Any:
        return NS(scalar=NS(blob=NS(uri=uri)))

    literals = {
        "code": NS(scalar=NS(primitive=NS(string_value="1"))),
        "tail": blob("s3://bucket/run/tail"),
        "junit": blob("s3://bucket/run/junit"),
    }
    code, tail, junit = _slow_lane._read_lane_outputs(literals)
    assert code == "1"
    assert tail.path == "s3://bucket/run/tail"
    assert junit.path == "s3://bucket/run/junit"


def test_the_poll_tolerates_exactly_the_remote_errors():
    from flyte_agent_factory import _errors

    assert _slow_lane._POLL_ERRORS == _errors.REMOTE_ERRORS


def test_a_bug_in_the_poll_is_not_taken_for_a_failed_call():
    async def never() -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def finished() -> str | None:
        raise AttributeError("a bug, not the network")

    with pytest.raises(AttributeError):
        asyncio.run(_slow_lane._settle(never(), finished, poll_s=0.01, grace_s=0.01))


def test_submit_runs_the_async_submission_to_completion(monkeypatch):
    seen = []

    async def submit_async(pytest_args, interruptible, result, mode):
        seen.append((pytest_args, interruptible, result, mode))
        return 0, "tail", "<testsuites/>"

    monkeypatch.setattr(_slow_lane, "_submit_async", submit_async)
    assert _slow_lane._submit(["-k", "x"], True, None, "command") == (0, "tail", "<testsuites/>")
    assert seen == [(["-k", "x"], True, None, "command")]


def test_a_command_that_cannot_start_exits_127_and_is_recorded(tmp_path, capsys):
    result = tmp_path / "result.json"
    missing = str(tmp_path / "no-such-program")
    assert _slow_lane._run_local_command(None, [missing, "arg"], result) == 127
    assert f"slow lane: cannot run {missing}:" in capsys.readouterr().err
    # 127 is outside the codes a finished lane reports, so no completion record is written for it.
    assert not result.exists()
