"""Finding the deployed commit by content, and what a deploy would bring."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "deploy_status", Path(__file__).resolve().parents[1] / "ops" / "deploy_status.py"
)
assert SPEC is not None and SPEC.loader is not None
deploy_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_status)


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    pkg = repo / "src" / "flyte_agent_factory"
    pkg.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i, body in enumerate(["v1", "v2", "v3"]):
        (pkg / "agent.py").write_text(body)
        (pkg / "_bridge.py").write_text("bridge")
        (pkg / "unused.py").write_text(f"unused {i}")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"change {body}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    (repo / "README.md").write_text("docs only")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs only")
    shas.append(_git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(deploy_status, "REPO", repo)
    return repo, shas


def _bundle(tmp_path, files):
    root = tmp_path / "bundle" / "flyte_agent_factory"
    root.mkdir(parents=True)
    for name, body in files.items():
        (root / name).write_text(body)
    return tmp_path / "bundle"


def test_a_partial_bundle_matches_the_newest_commit_with_its_files(history, tmp_path):
    """Bundles carry only loaded modules: `unused.py` is absent and must not prevent a match."""
    _, shas = history
    blobs = deploy_status.bundle_blobs(_bundle(tmp_path, {"agent.py": "v2", "_bridge.py": "bridge"}))
    commits = list(reversed(shas))
    assert deploy_status.match_commit(blobs, commits) == shas[1]


def test_behind_lists_only_changes_to_the_package(history):
    _, shas = history
    assert [line.split(" ", 1)[1] for line in deploy_status.behind(shas[0], shas[3])] == [
        "change v3",
        "change v2",
    ]
    assert deploy_status.behind(shas[2], shas[3]) == [], "a docs-only commit needs no deploy"


def test_unknown_code_is_said_plainly(history, tmp_path):
    _, shas = history
    blobs = deploy_status.bundle_blobs(_bundle(tmp_path, {"agent.py": "local edit"}))
    assert deploy_status.match_commit(blobs, list(reversed(shas))) is None
    rows = [{"task": "agent-own.project", "version": "abcdef12", "commit": None, "note": "matches no commit"}]
    assert "matches no commit" in deploy_status.render(rows)


def test_render_says_how_far_behind_and_names_no_human_deploy():
    rows = [
        {
            "task": "agent-one.project",
            "version": "e97446f8xx",
            "commit": "0766e24aaaa",
            "behind": ["8ed950c harness-files"],
        }
    ]
    text = deploy_status.render(rows)
    assert "BEHIND by 1" in text and "8ed950c harness-files" in text
    assert "HUMAN" not in text and "deploy_self" not in text and "deploy is by" not in text


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "ops" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_only_the_slots_project_tasks_are_checked_so_retired_envs_are_never_behind():
    assert deploy_status.TASKS == deploy_status.PROFILE.tasks
    assert not {"claude-self.project", "claude-agent.project", "agent-factory.project"} & set(
        deploy_status.TASKS
    )


def test_deploy_status_emits_no_deploy_needed_note():
    """The `DEPLOY NEEDED (human)` row and its `--note` flag are gone; a stale env is plain information."""
    source = (Path(__file__).resolve().parents[1] / "ops" / "deploy_status.py").read_text()
    assert not hasattr(deploy_status, "human_handoff")
    assert "DEPLOY NEEDED" not in source and "--note" not in source
    rows = [
        {"task": "agent-one.project", "version": "abcdef12xx", "commit": "a" * 7, "behind": ["a x", "b y"]},
        {"task": "agent-two.project", "commit": None, "version": "abcdef12xx", "note": "matches no commit"},
        {"task": "agent-three.project", "error": "LookupError: not fast-registered"},
    ]
    rendered = deploy_status.render(rows)
    assert "DEPLOY NEEDED" not in rendered
    assert "could not check (LookupError: not fast-registered)" in rendered  # unknown status is still shown


def test_a_change_to_an_unbundled_module_is_reported_uncertain_not_hidden(tmp_path, monkeypatch):
    """qa: deployed A, then B changed only voice.py; the newest match was B and 'up to date' was reported."""
    repo = tmp_path / "r"
    pkg = repo / "src" / "flyte_agent_factory"
    pkg.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@e")
    _git(repo, "config", "user.name", "t")
    (pkg / "agent.py").write_text("a1")
    (pkg / "voice.py").write_text("v1")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "A")
    (pkg / "voice.py").write_text("v2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "B voice only")
    monkeypatch.setattr(deploy_status, "REPO", repo)
    commits = _git(repo, "rev-list", "HEAD").split()
    blobs = deploy_status.bundle_blobs(_bundle(tmp_path, {"agent.py": "a1"}))
    newest, oldest = deploy_status.match_range(blobs, commits)
    assert newest == commits[0] and oldest == commits[1]
    assert [c.split(" ", 1)[1] for c in deploy_status.behind(oldest, commits[0])] == ["B voice only"]


def test_a_failed_fetch_falls_back_instead_of_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_status, "REPO", tmp_path)  # not a repo: fetch fails fast
    assert deploy_status._fetch(timeout=5) != ""


def test_an_agent_deploy_no_longer_posts_a_human_deploy_note():
    work = (Path(__file__).resolve().parents[1] / "ops" / "deploy_work.py").read_text()
    assert "deploy_status" not in work and "--note" not in work


def test_deployer_run_count_uses_24_hour_server_filter(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from flyte import remote

    from ops import deploy_status

    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    seen = {}

    def runs(**kwargs):
        seen.update(kwargs)
        return iter([SimpleNamespace(name="one"), SimpleNamespace(name="two")])

    monkeypatch.setattr(remote.Run, "listall", runs)
    assert deploy_status.deployer_runs_24h(now) == 2
    assert seen["task_name"] == "factory-deployer.deploy_if_changed"
    assert seen["created_at"].after == now - timedelta(hours=24)
    assert seen["limit"] >= 288


def test_deployed_pair_is_read_from_the_newest_release(monkeypatch):
    from types import SimpleNamespace

    from flyte_agent_factory import _deployer

    async def latest(profile):
        return SimpleNamespace(name="release"), ("a" * 40, "d" * 40)

    monkeypatch.setattr(_deployer, "deployed_pair", latest)
    assert deploy_status.deployed_pair() == {
        "task": "factory-deployer.deploy_pair",
        "run": "release",
        "deployed_pair": {"sha": "a" * 40, "direction_sha": "d" * 40},
    }
