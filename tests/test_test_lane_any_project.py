"""The test lane is offered to every project's `checks.sh` the same way, whatever repo it delivers to."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import flyte.errors
import pytest

from flyte_agent_factory import (
    _gate_base,
    _gate_tests,
    _session_config,
    _slow_lane,
    _supervise_support,
    _timeline,
)

_SEEN = (
    'echo "lane=$AGENT_FACTORY_TEST_LANE slow=$AGENT_FACTORY_SLOW_LANE junit=$AGENT_FACTORY_PYTEST_JUNIT_DIR"'
    ' > seen.txt\ntest -x "$AGENT_FACTORY_TEST_LANE"\n'
)


def _workspace(tmp_path: Path, rel: str) -> Path:
    workspace = tmp_path / "work"
    script = workspace / rel
    script.parent.mkdir(parents=True)
    script.write_text(_SEEN)
    return workspace


@pytest.mark.asyncio
async def test_a_root_layout_repo_and_a_workspace_project_get_the_same_lane(tmp_path, monkeypatch):
    monkeypatch.setattr(_session_config, "_project_name", "other")
    seen = []
    for name, rel in (("root", ".claude-flyte/other/checks.sh"), ("ws", "projects/other/checks.sh")):
        workspace = _workspace(tmp_path / name, rel)
        junit = tmp_path / name / "junit"
        junit.mkdir()
        problem = await _supervise_support._run_declared_checks(
            workspace, _timeline._Timeline(), junit, script=rel
        )
        assert problem == ""
        line = (workspace / "seen.txt").read_text().strip()
        seen.append(line.replace(str(junit), "<junit>"))
    assert seen[0] == seen[1]
    assert seen[0].startswith("lane=") and "slow=child" in seen[0] and "junit=<junit>" in seen[0]
    # The wrapper lives under the junit directory, which is removed with it.
    assert seen[0].split()[0] == "lane=<junit>/lane/test-lane"


def _bare_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "AGENT_FACTORY_SLOW_LANE")}
    env["HOME"] = str(tmp_path)
    return env


def test_the_launcher_runs_a_command_with_no_engine_importable_from_the_project(tmp_path):
    lane = _gate_tests.write_lane_launcher(tmp_path / "lane")
    project = tmp_path / "project"
    project.mkdir()
    for code in (0, 3):
        proc = subprocess.run(
            [str(lane), "--", "bash", "-c", f"echo ran-in-$(basename $PWD); exit {code}"],
            cwd=project,
            env=_bare_env(tmp_path),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == code, proc.stderr
        assert "ran-in-project" in proc.stdout


def test_the_launcher_hands_a_command_the_junit_path(tmp_path):
    lane = _gate_tests.write_lane_launcher(tmp_path / "lane")
    junit = tmp_path / "out" / "x.xml"
    proc = subprocess.run(
        [str(lane), "--junit", str(junit), "--", "bash", "-c", 'echo "$AGENT_FACTORY_LANE_JUNIT"'],
        cwd=tmp_path,
        env=_bare_env(tmp_path),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and proc.stdout.strip() == str(junit)


def test_the_launcher_sends_a_command_to_the_child_when_a_run_is_attached(tmp_path, monkeypatch):
    calls: list[Any] = []

    def fake_submit(args, interruptible, result=None, mode="pytest"):
        calls.append((args, interruptible, mode))
        return 4, "child tail\n", ""

    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)
    monkeypatch.setattr(_slow_lane, "_submit", fake_submit)

    assert _slow_lane.main(["--", "make", "check", "-j2"]) == 4
    assert calls == [(["make", "check", "-j2"], True, "command")]


def test_a_command_the_child_could_not_place_runs_here(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FACTORY_SLOW_LANE", "child")
    monkeypatch.setattr(_slow_lane, "_attach", lambda: None)

    def unplaced(*_a, **_k):
        raise flyte.errors.MaxQueuedTimeExceededError("no capacity")

    monkeypatch.setattr(_slow_lane, "_submit", unplaced)
    monkeypatch.chdir(tmp_path)
    assert _slow_lane.main(["--", sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_the_flag_without_a_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        _slow_lane.main(["--junit", "x.xml", "--"])
    assert cast(int, exc.value.code) == 2


# --- the run image -------------------------------------------------------------------------------
# In the supervisor FLYTE_INTERNAL_IMAGE names the plain Flyte image (no git), so a child started with it
# cannot set up a project. The launcher must carry the run's own image, whatever the project is.


def test_the_launcher_carries_the_runs_image_to_the_child(tmp_path):
    lane = _gate_tests.write_lane_launcher(tmp_path / "lane", image="registry.example/agent:run-image")
    env = {**_bare_env(tmp_path), "FLYTE_INTERNAL_IMAGE": "registry.example/plain:flyte"}
    # No run to attach to, so the command runs here and shows the environment the launcher gave it.
    proc = subprocess.run(
        [str(lane), "--", "bash", "-c", 'echo "image=$FLYTE_INTERNAL_IMAGE slow=$AGENT_FACTORY_SLOW_LANE"'],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "image=registry.example/agent:run-image slow=child" in proc.stdout
    # Only the image name is written: nothing of the supervisor's environment.
    assert "sk-" not in lane.read_text()


def test_without_the_runs_image_the_launcher_runs_locally_not_in_the_plain_image(tmp_path, monkeypatch):
    lane = _gate_tests.write_lane_launcher(tmp_path / "lane")
    text = lane.read_text()
    assert "FLYTE_INTERNAL_IMAGE" not in text and "SLOW_LANE=child" not in text
    assert "unset AGENT_FACTORY_SLOW_LANE" in text
    proc = subprocess.run(
        [str(lane), "--", "bash", "-c", 'echo "slow=${AGENT_FACTORY_SLOW_LANE:-unset}"'],
        cwd=tmp_path,
        env={**_bare_env(tmp_path), "AGENT_FACTORY_SLOW_LANE": "child"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and "slow=unset" in proc.stdout, proc.stderr


@pytest.mark.asyncio
async def test_the_pre_review_looks_up_the_runs_image_once_for_any_project(tmp_path, monkeypatch):
    looked_up: list[tuple[Path, dict[str, str]]] = []

    def fake_image(project: Path, env: dict[str, str]) -> str:
        looked_up.append((project, env))
        return "registry.example/agent:run-image"

    monkeypatch.setattr(_gate_tests, "_run_image", fake_image)
    monkeypatch.setattr(_session_config, "_project_name", "other")
    monkeypatch.setenv("FLYTE_INTERNAL_IMAGE", "registry.example/plain:flyte")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    rel = "projects/other/checks.sh"
    workspace = _workspace(tmp_path, rel)
    junit = tmp_path / "junit"
    junit.mkdir()
    assert (
        await _supervise_support._run_declared_checks(workspace, _timeline._Timeline(), junit, script=rel)
        == ""
    )
    assert [project for project, _ in looked_up] == [workspace]
    assert "sk-secret-value" not in " ".join(looked_up[0][1].values())
    text = (junit / "lane" / "test-lane").read_text()
    assert "registry.example/agent:run-image" in text and "sk-secret-value" not in text


def test_without_a_run_the_image_is_not_looked_up(tmp_path, monkeypatch):
    # Outside a Flyte run there is no root action to ask: no subprocess, and the lane runs here.
    monkeypatch.delenv("FLYTE_INTERNAL_EXECUTION_ID", raising=False)
    monkeypatch.setattr(_gate_base, "_run", lambda *a, **k: pytest.fail("looked up"))
    assert _gate_tests._run_image(tmp_path, {}) == ""


def test_the_runs_image_is_looked_up_once_per_run(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_run(*args, **kwargs):
        calls.append("lookup")
        return 0, "image=registry.example/agent:run-image\n"

    monkeypatch.setattr(_gate_tests, "_RUN_IMAGES", {})
    monkeypatch.setattr(_gate_base, "_run", fake_run)
    monkeypatch.setenv("FLYTE_INTERNAL_EXECUTION_ID", "run-1")
    assert _gate_tests._run_image(tmp_path, {}) == "registry.example/agent:run-image"
    assert _gate_tests._run_image(tmp_path, {}) == "registry.example/agent:run-image"
    assert calls == ["lookup"]
