"""A modified engine copy in a workspace repo's subtree is benchmarked in isolated envs (A32)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, cast

import flyte.errors
import pytest
from flyte import remote

from flyte_agent_factory import _bench, _bench_flow, _deploy, _github, _profile
from flyte_agent_factory._github import GithubApp
from tests._git import git
from tests.test_bench import SHA, profile

WORKSPACE = "example-org/direction"
SOURCE = _bench.Source(WORKSPACE, "projects/engine-v2")


@pytest.mark.parametrize("path", ["/etc", "../x", "a/../../x", "a/..", "projects/../../x"])
def test_a_subtree_that_leaves_the_checkout_is_refused(path):
    with pytest.raises(ValueError, match="subtree"):
        _bench.Source(WORKSPACE, path)


def test_a_subtree_is_normalised():
    assert _bench.Source(WORKSPACE, "projects//engine/").path == "projects/engine"
    assert _bench.Source(WORKSPACE, "./").path == ""
    assert _bench.Source(WORKSPACE).path == ""


def commit_tree(tmp_path, files, links=()):
    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for name, target in links:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).symlink_to(target)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", "m")
    return repo, git(repo, "rev-parse", "HEAD")


def test_the_subtree_src_is_found_in_a_fetched_tree(tmp_path):
    repo, sha = commit_tree(tmp_path, {"projects/engine-v2/src/pkg.py": "x = 1\n"})
    with _deploy.fetched_tree(repo.as_uri(), sha) as tree:
        src = _bench.candidate_src(tree, SOURCE.path)
        assert src == tree.resolve() / "projects" / "engine-v2" / "src" and (src / "pkg.py").is_file()
        with pytest.raises(ValueError, match="no src/"):
            _bench.candidate_src(tree, "projects/missing")


def test_a_symlink_out_of_the_checkout_is_refused(tmp_path):
    outside = tmp_path / "outside"
    (outside / "src").mkdir(parents=True)
    repo, sha = commit_tree(
        tmp_path,
        {"projects/real/src/a.py": ""},
        links=[
            ("projects/escape", str(outside)),  # a component of the subtree
            ("projects/engine-v2/src", str(outside / "src")),  # `src` itself
            ("projects/up", "../.."),
        ],
    )
    with _deploy.fetched_tree(repo.as_uri(), sha) as tree:
        for path in ("projects/escape", "projects/engine-v2", "projects/up"):
            with pytest.raises(ValueError, match="outside the checkout"):
                _bench.candidate_src(tree, path)


def test_a_symlink_under_src_that_leaves_the_checkout_is_refused(tmp_path):
    """The deployed bundle would carry what the link points at: a file outside the reviewed tree."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n")
    for name, target in (("pkg/leak.py", str(outside / "secret.py")), ("pkg/dir", str(outside))):
        root = tmp_path / name.replace("/", "-")
        root.mkdir()
        repo, sha = commit_tree(
            root, {"projects/engine-v2/src/pkg/a.py": ""}, links=[(f"projects/engine-v2/src/{name}", target)]
        )
        with _deploy.fetched_tree(repo.as_uri(), sha) as tree:
            with pytest.raises(ValueError, match="outside the checkout"):
                _bench.candidate_src(tree, SOURCE.path)


def test_a_symlink_inside_the_checkout_is_fine(tmp_path):
    repo, sha = commit_tree(tmp_path, {"projects/real/src/a.py": ""}, links=[("projects/alias", "real")])
    with _deploy.fetched_tree(repo.as_uri(), sha) as tree:
        assert (_bench.candidate_src(tree, "projects/alias") / "a.py").is_file()


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


def test_candidate_envs_never_meet_production_or_each_other():
    prod = profile()
    sources = [
        None,
        _bench.Source("example-org/factory", "sub"),
        _bench.Source(WORKSPACE),
        _bench.Source(WORKSPACE, "projects/engine-v2"),
        _bench.Source(WORKSPACE, "projects/engine-v3"),
        _bench.Source("example-org/other", "projects/engine-v2"),
    ]
    shas = [SHA, "f" * 40]
    production = set(_deploy.planned_envs(prod))
    seen: dict[str, tuple[object, ...]] = {}
    for source in sources:
        for sha in shas:
            for name in _bench.bench_envs(sha, 3, source):
                # The sub-agent env is deployed too.
                for env in (name, f"{name}-sub"):
                    assert not any(env == p or env.startswith(p) or p.startswith(env) for p in production)
                    assert seen.setdefault(env, (source, sha)) == (source, sha), env
    # Case-insensitive repo names are one repo.
    assert _bench.bench_envs(SHA, 1, _bench.Source("Example-Org/Direction", "p")) == _bench.bench_envs(
        SHA, 1, _bench.Source(WORKSPACE, "p")
    )


def test_a_profile_slot_named_like_a_candidate_env_is_refused():
    name = _bench.bench_envs(SHA, 1, SOURCE)[0]
    clash = profile(text=_bench_profile_text().replace('env = "agent-one"', f'env = "{name}"'))
    with pytest.raises(ValueError, match="production"):
        _bench.bench_profile(clash, SHA, SOURCE)


def _bench_profile_text():
    from tests.test_bench import PROFILE_TEXT

    return PROFILE_TEXT


def test_the_candidate_profile_has_candidate_slots_and_no_deploy_source():
    candidate = _bench.bench_profile(profile(), SHA, SOURCE)
    assert [s.env for s in candidate.slots] == _bench.bench_envs(SHA, 2, SOURCE)
    assert candidate.deploy.repo == "" and not candidate.deploy.auto


# --------------------------------------------------------------------------- #
# The flow
# --------------------------------------------------------------------------- #


class World:
    def __init__(self, monkeypatch, public: bool = False):
        self.public = public
        self.fetched: list[str] = []
        self.deploys: list[tuple[Any, ...]] = []
        monkeypatch.setattr(_bench_flow, "PROFILE", profile())

        def missing(name, **kw):
            class Lazy:
                def fetch(self):
                    raise flyte.errors.RemoteTaskNotFoundError(f"no {name}")

            return Lazy()

        monkeypatch.setattr(remote.Task, "get", missing)
        monkeypatch.setattr(_deploy, "deploy_slots", self.deploy_slots)
        monkeypatch.setattr(_deploy, "fetched_tree", self.fetched_tree)
        self.tree = Path("/engine")
        self.confined: list[tuple[object, ...]] = []
        monkeypatch.setattr(_bench, "candidate_src", self.candidate_src)

    def candidate_src(self, tree, path):
        self.confined.append((tree, path))
        return tree / path / "src"

    def deploy_slots(self, prof, src, slots, **kw):
        self.deploys.append((prof, src, slots, kw["ref"]))
        return 0

    def fetched_tree(self, url, sha):
        self.fetched.append(url)
        world = self

        class Tree:
            def __enter__(self):
                return world.tree

            def __exit__(self, *exc):
                return False

        return Tree()

    def app(self):
        world = self

        class App:
            def installation_token(self, repo):
                if world.public:
                    raise _github.GithubError("not installed", status=404)
                return "token"

        return cast(GithubApp, App())


def test_a_candidate_is_fetched_from_its_repo_and_deployed_from_its_subtree(monkeypatch):
    world = World(monkeypatch)
    envs = _bench_flow.deploy_engine_sync(world.app(), SHA, SOURCE)
    assert envs == _bench.bench_envs(SHA, 2, SOURCE)
    assert world.fetched == [_github.clone_url(WORKSPACE, "token")]
    prof, src, slots, ref = world.deploys[0]
    assert (src, slots, ref) == (Path("/engine/projects/engine-v2/src"), envs, SHA)
    assert world.confined == [(Path("/engine"), "projects/engine-v2")]


def test_a_public_candidate_repo_is_read_anonymously_when_the_app_is_not_installed(monkeypatch):
    world = World(monkeypatch, public=True)
    _bench_flow.deploy_engine_sync(world.app(), SHA, SOURCE)
    assert world.fetched == [f"https://github.com/{WORKSPACE}.git"]


def test_the_default_still_deploys_the_factory_repo_root_src(monkeypatch):
    world = World(monkeypatch)
    envs = _bench_flow.deploy_engine_sync(world.app(), SHA)
    assert envs == _bench.bench_envs(SHA, 2)
    assert world.fetched == [_github.clone_url("example-org/factory", "token")]
    assert world.deploys[0][1] == Path("/engine/src") and world.confined == []


def test_a_candidate_deploy_leaves_the_production_deploy_source_alone(monkeypatch):
    world = World(monkeypatch)
    production = _bench_flow.PROFILE
    before = (production.deploy, dict(production.repos))
    _bench_flow.deploy_engine_sync(world.app(), SHA, SOURCE)
    assert (production.deploy, dict(production.repos)) == before and _bench_flow.PROFILE is production
    # What was deployed carries no deploy source, and the flow cannot reach the deployer's lock or state.
    assert world.deploys[0][0].deploy.repo == ""
    assert not hasattr(_bench_flow, "_deployer")


def test_only_the_profiles_own_repos_can_be_a_candidate_source(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    for url in (f"git@github.com:{WORKSPACE}.git", "https://github.com/example-org/factory"):
        source = _bench_flow.candidate_source(url, "projects/x")
        assert source is not None and source.path == "projects/x"
    with pytest.raises(ValueError, match="not one of"):
        _bench_flow.candidate_source("example-org/elsewhere", "projects/x")
    assert _bench_flow.candidate_source("", "") is None
    assert _bench_flow.candidate_source("example-org/factory", "") is None  # the default, spelled out
    # A path alone means the engine repo.
    assert _bench_flow.candidate_source("", "sub") == _bench.Source("example-org/factory", "sub")


def test_a_ref_resolves_in_the_candidates_repo(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    asked = []

    class App:
        def default_branch_sha(self, repo, ref):
            asked.append((repo, ref))
            return "main", SHA

    app = cast(GithubApp, App())
    assert _bench_flow.resolve_sha(app, "main", SOURCE) == SHA
    assert _bench_flow.resolve_sha(app, "main") == SHA
    assert asked == [(WORKSPACE, "main"), ("example-org/factory", "main")]
    assert _bench_flow.resolve_sha(app, SHA, SOURCE) == SHA and len(asked) == 2


def test_the_default_source_is_the_loops_engine_when_there_is_no_factory_repo(monkeypatch):
    loop = _profile.Loop(enabled=True, repo=WORKSPACE, path="projects/engine")
    configured = profile()
    # No `[repo] factory`: the loop's repository and subtree are the engine an unqualified bench means.
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(
            configured, repos={k: v for k, v in configured.repos.items() if k != "factory"}, loop=loop
        ),
    )
    assert _bench_flow.candidate_source("", "") == _bench.Source(WORKSPACE, "projects/engine")
    assert _bench_flow.candidate_source(WORKSPACE, "other") == _bench.Source(WORKSPACE, "other")
    app = cast(GithubApp, type("App", (), {"default_branch_sha": lambda self, repo, ref: (ref, SHA)})())
    assert _bench_flow.resolve_sha(app, "main", _bench_flow.candidate_source("", "")) == SHA
    # `benchmark` always has that source; a caller with none has no engine repo to resolve in.
    with pytest.raises(ValueError, match="no .repo. factory"):
        _bench_flow.resolve_sha(app, "main")


def test_a_candidate_repo_the_app_fails_on_for_another_reason_is_not_read_anonymously(monkeypatch):
    world = World(monkeypatch)

    class Broken:
        def installation_token(self, repo):
            raise _github.GithubError("bad credentials", status=401)

    with pytest.raises(_github.GithubError, match="bad credentials"):
        _bench_flow.deploy_engine_sync(cast(GithubApp, Broken()), SHA, SOURCE)
    assert world.fetched == []


def test_there_is_no_default_source_without_a_factory_or_loop_repo(monkeypatch):
    configured = profile()
    monkeypatch.setattr(
        _bench_flow,
        "PROFILE",
        dataclasses.replace(configured, repos={k: v for k, v in configured.repos.items() if k != "factory"}),
    )
    with pytest.raises(ValueError, match="no .repo. factory or .loop. repo"):
        _bench_flow.candidate_source("", "")


def test_the_deploy_source_repo_is_a_candidate_source(monkeypatch):
    configured = profile()
    deploy = dataclasses.replace(configured.deploy, repo="git@github.com:example-org/deployed.git")
    monkeypatch.setattr(_bench_flow, "PROFILE", dataclasses.replace(configured, deploy=deploy))
    assert _bench_flow.candidate_source("example-org/deployed", "p") == _bench.Source(
        "example-org/deployed", "p"
    )


def test_a_profile_with_no_direction_or_deploy_repo_still_resolves_its_own_candidate_source(monkeypatch):
    configured = profile()
    deploy = dataclasses.replace(configured.deploy, repo="")
    monkeypatch.setattr(
        _bench_flow, "PROFILE", dataclasses.replace(configured, deploy=deploy, direction=None)
    )
    assert _bench_flow.candidate_source("example-org/factory", "sub") == _bench.Source(
        "example-org/factory", "sub"
    )
    # The workspace repo is only a candidate source when the profile names it as `[direction]`.
    with pytest.raises(
        ValueError, match="not one of the profile's repositories .example-org/factory."
    ) as refused:
        _bench_flow.candidate_source(WORKSPACE, "projects/x")
    assert WORKSPACE not in str(refused.value).split("(")[1]


def test_the_deploy_engine_task_deploys_the_candidate_asked_for(monkeypatch):
    monkeypatch.setattr(_bench_flow, "PROFILE", profile())
    app = object()
    monkeypatch.setattr(_bench_flow, "_app", lambda: app)
    deployed = []

    def deploy(got_app, sha, source):
        deployed.append((got_app, sha, source))
        return ["bench-env"]

    monkeypatch.setattr(_bench_flow, "deploy_engine_sync", deploy)
    envs = asyncio.run(_bench_flow.deploy_engine.func(SHA, WORKSPACE, "projects/engine-v2"))
    assert envs == ["bench-env"]
    assert deployed == [(app, SHA, _bench.Source(WORKSPACE, "projects/engine-v2"))]
