"""The loop's admission and decisions, independent of Flyte and GitHub."""

import json
import re
from dataclasses import replace
from typing import Any, cast

import pytest

from flyte_agent_factory import _github, _issues, _loop, _profile
from flyte_agent_factory._loop import ChangedFile
from tests.conftest import TEST_PROFILE


def verdict(**changes):
    return dict(
        verdict="no_worse",
        reason="cost",
        band_source="measured",
        b=0.1,
        sigma_aa=0.1,  # b = max(0.1, 1.64 * 0.1 / sqrt(6)) = 0.1
        m=6,
        S=0.0,
        L=-0.1,
        U=0.15,
        **changes,
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        (verdict(), "merge"),
        ({**verdict(), "verdict": "better", "S": -0.3, "L": -0.4, "U": -0.2}, "merge"),
        ({**verdict(), "verdict": "worse", "L": 0.2}, "close"),
        ({**verdict(), "verdict": "inconclusive"}, "inconclusive"),
        ({**verdict(), "reason": "underpowered", "m": 5}, "inconclusive"),
        ({**verdict(), "verdict": "underpowered"}, "inconclusive"),
        ({**verdict(), "band_source": "assumed"}, "inconclusive"),
        ({**verdict(), "reason": "visible pass shortfall"}, "inconclusive"),
        ({**verdict(), "S": 0.11}, "inconclusive"),
        ({**verdict(), "U": 0.21}, "inconclusive"),
    ],
)
def test_decisions(value, expected):
    assert _loop.decide(value, "benchmark", True) == expected


def test_review_owner_and_docs():
    assert _loop.decide(verdict(), "needs_owner", True) == "needs_owner"
    assert _loop.decide(verdict(), "benchmark", False) == "skip"
    assert _loop.decide({}, "docs_only", True) == "merge"


@pytest.mark.parametrize(
    "path",
    [
        "src/flyte_agent_factory/_gates.py",
        "src/flyte_agent_factory/_standards_gate.py",
        "standards/rules.md",
        "profiles/example.toml",
        "src/flyte_agent_factory/_bench_stats.py",
        "src/flyte_agent_factory/_loop_flow.py",
        "src/flyte_agent_factory/_github.py",
        "src/flyte_agent_factory/_deploy.py",
        ".github/workflows/checks.yml",
        "DESIGN.md",
        "docs/security.md",
        "SECURITY.md",
        "ops/briefs/factory-improvement.md",
        "ops/bench_seed.py",
    ],
)
def test_owner_paths(path):
    assert _loop.classify(["docs/guide.md", path]) == "needs_owner"


def test_classify_docs_and_runtime():
    assert _loop.classify(["docs/guide.md", "docs/other.md"]) == "docs_only"
    assert _loop.classify(["src/flyte_agent_factory/_dashboard.py"]) == "benchmark"
    assert _loop.classify(["ops/state_report.py"]) == "benchmark"
    assert _loop.classify([]) == "needs_owner"
    assert (
        _loop.classify([{"filename": "docs/a.md", "previous_filename": "src/flyte_agent_factory/_gates.py"}])
        == "needs_owner"
    )


def test_comment_carries_measurement_and_marker():
    body = _loop.decision_comment("merge", verdict(), "head", "main")
    for value in ("sigma_aa", "m", "S", "L", "U", "b"):
        assert value in body
    marker = body.split("<!-- factory-loop:decision ")[1].split(" -->")[0]
    assert json.loads(marker)["head"] == "head"


def test_owner_path_modules_have_not_drifted():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (root / "src/flyte_agent_factory").glob("*.py"):
        if any(word in path.stem for word in ("gate", "standards", "deploy", "bench", "loop")):
            assert _loop.classify([str(path.relative_to(root))]) == "needs_owner"


def test_loop_profile():
    text = TEST_PROFILE.read_text()
    configured = text.replace("[repo]\n", '[repo]\nfactory = "git@github.com:acme/factory.git"\n')
    configured += '\n[github]\napp_id = "123"\nprivate_key_secret = "app_key"\n'
    configured += (
        '\n[bench]\nlogins = ["bench_login"]\n'
        'repo = "git@github.com:acme/bench.git"\nrepo_key = "bench_key"\n'
    )

    assert not _profile.parse(text).loop.enabled
    parsed = _profile.parse(
        configured + '\n[loop]\nenabled = true\nrepo = "acme/factory"\nmerge = "dry-run"\n'
    )
    assert parsed.loop.enabled and parsed.loop.repo == "acme/factory"
    for extra in ('merge = "yes"', "band = 0.1", 'enabled = "yes"'):
        with pytest.raises(_profile.ProfileError):
            _profile.parse(text + '\n[loop]\nrepo = "acme/factory"\n' + extra)


def test_classify_reads_paths_relative_to_the_engine_subtree():
    sub = "projects/engine"
    assert _loop.classify([f"{sub}/src/flyte_agent_factory/_dashboard.py"], path=sub) == "benchmark"
    assert _loop.classify([f"{sub}/docs/guide.md"], path=sub) == "docs_only"
    # The owner's files are the engine's own, wherever the engine sits in the repository.
    for owner in ("src/flyte_agent_factory/_bench_flow.py", "profiles/x.toml", "pyproject.toml"):
        assert _loop.classify([f"{sub}/{owner}"], path=sub) == "needs_owner"
    # What is not under the subtree is not the engine: the loop does not judge it.
    assert _loop.classify([f"{sub}/docs/guide.md", "README.md"], path=sub) == "needs_owner"
    assert _loop.classify([f"{sub}2/docs/guide.md"], path=sub) == "needs_owner"
    renamed: _loop.ChangedFile = {"filename": f"{sub}/docs/a.md", "previous_filename": "elsewhere/a.md"}
    assert _loop.classify([renamed], path=sub) == "needs_owner"


def test_kill_switch_requires_trusted_label_and_owner_restart():
    profile = replace(
        _profile.parse(TEST_PROFILE.read_text()),
        github=_profile.Github(maintainers=("owner",)),
        loop=_profile.Loop(enabled=True, repo="acme/factory"),
    )

    class App:
        events: list[dict[str, Any]]

        def app_login(self):
            return "factory[bot]"

        def labelled_issues(self, repo, label):
            assert label == "loop-kill"
            return [{"number": 1, "state": "closed"}]

        def issue_events(self, repo, number):
            return self.events

    fake = App()
    # The fake has only the three methods `killed` calls.
    app = cast(_github.GithubApp, fake)
    events: list[dict[str, Any]] = [
        {"event": "labeled", "label": {"name": "loop-kill"}, "actor": {"login": "stranger"}}
    ]
    fake.events = events
    assert not _loop.killed(profile, app)
    events.append({"event": "reopened", "actor": {"login": "factory[bot]"}})
    assert not _loop.killed(profile, app)
    events.pop()
    events[0]["actor"]["login"] = "factory[bot]"
    events.append({"event": "closed", "actor": {"login": "stranger"}})
    assert _loop.killed(profile, app)
    events.append({"event": "closed", "actor": {"login": profile.github.maintainers[0]}})
    assert not _loop.killed(profile, app)
    events.append({"event": "reopened", "actor": {"login": "stranger"}})
    assert _loop.killed(profile, app)


@pytest.mark.parametrize("ratio,expected", [(0.5, "merge"), (1.0, "merge"), (2.0, "close")])
def test_real_benchmark_verdicts(ratio, expected):
    from flyte_agent_factory import _bench_stats
    from tests.test_bench_stats import cases

    result = _bench_stats.verdict(cases(ratio=ratio), [cases()])
    assert _loop.decide(result, "benchmark", True) == expected


@pytest.mark.parametrize("heldout", [False, True])
@pytest.mark.parametrize("shortfall,expected", [(1, "inconclusive"), (2, "close")])
def test_lower_pass_count_never_merges(heldout, shortfall, expected):
    from flyte_agent_factory import _bench_stats
    from tests.test_bench_stats import cases

    records = cases(ratio=0.1)
    if heldout:
        records += [{**row, "task": "hidden-" + row["task"], "heldout": True} for row in cases()]
    candidates = [row for row in records if row["arm"] == "candidate" and row["heldout"] == heldout]
    for row in candidates[:shortfall]:
        row["accepted_verified"] = False
    assert _loop.decide(_bench_stats.verdict(records, [cases()]), "benchmark", True) == expected


@pytest.mark.parametrize(
    "paths",
    [
        ["src/flyte_agent_factory/agent.py"],
        ["src/flyte_agent_factory/_policy.py"],
        ["tests/test_x.py"],
        ["docs/guide.md", "tests/test_x.py"],
    ],
)
def test_ordinary_code_and_tests_only_changes_are_benchmarked(paths):
    # Owner, inbox 028: the loop merges on a measured win; agent.py and the policy knobs are what it improves,
    # and a tests-only change is measured rather than merged as docs.
    assert _loop.classify(paths) == "benchmark"


@pytest.mark.parametrize(
    "path",
    [
        "src/flyte_agent_factory/_gates/__init__.py",  # a package shadows the owner module
        "src/flyte_agent_factory/_github/core.py",
        "factory-improvement/checks.sh",  # the CI gate
        "pyproject.toml",  # lint and test configuration
        "ops/briefs/factory-improvement.md",
        "ops/briefs/new-steering.md",
        ".briefs/design-loop.md",
        "tests/conftest.py",
    ],
)
def test_judge_files_and_shadowing_packages_need_the_owner(path):
    # QA loop L1: each of these let a PR change its own gate or skip the benchmark.
    assert _loop.classify(["docs/guide.md", path]) == "needs_owner"


def test_a_truncated_file_list_needs_the_owner():
    # QA loop L1: GitHub lists at most 3000 files; the rest (a gate change) was invisible.
    docs: list[ChangedFile] = [{"filename": f"docs/{i}.md"} for i in range(3000)]
    assert _loop.classify(docs, changed_files=3001) == "needs_owner"
    assert _loop.classify(docs, changed_files=3000) == "needs_owner"
    assert _loop.classify(docs[:5], changed_files=6) == "needs_owner"
    assert _loop.classify(docs[:5], changed_files=5) == "docs_only"


@pytest.mark.parametrize(
    "change",
    [
        {"m": float("nan")},
        {"m": float("inf")},
        {"m": 6.5},
        {"m": True},
        {"b": 1e9, "U": 5e8, "S": 1e8},  # a band no A/A could produce
        {"b": 0.05},
    ],
)
def test_a_malformed_verdict_never_merges(change):
    assert _loop.decide({**verdict(), **change}, "benchmark", True) == "inconclusive"


@pytest.mark.parametrize(
    "path",
    [
        "./src/flyte_agent_factory/_gates.py",
        "src//flyte_agent_factory/_gates.py",
        "src/flyte_agent_factory/./_gates.py",
        "../x.py",
    ],
)
def test_an_unnormalised_owner_path_is_still_the_owner_file(path):
    # QA loop L4: these spellings of a gate file passed the owner check and went to Ready.
    assert _loop._owner(path)


def test_a_contract_field_hidden_in_an_html_comment_does_not_count():
    body = "## Goal\nx\n## Why\nx\n## Acceptance criteria\n<!-- - [ ] `x` passes -->\n"
    assert any("Acceptance" in problem for problem in _loop.lint_contract(body))


class LoopApp:
    def __init__(self, issues):
        self._issues = issues

    def app_login(self):
        return "factory[bot]"

    def labelled_issues(self, repo, label):
        assert label == "loop"
        return self._issues


def loop_app(issues: list[dict[str, Any]]) -> _github.GithubApp:
    # The fake has only the two methods `loop_pull` calls.
    return cast(_github.GithubApp, LoopApp(issues))


def loop_issue(number=7, **kwargs):
    issue = {
        "number": number,
        "labels": [{"name": "Loop"}],
        "body": "<!-- loop-source: abc -->\n\ncontract",
        "user": {"login": "Factory[bot]"},
    }
    return {**issue, **kwargs}


def pr(ref="factory/issue-7-abc", repo="acme/factory") -> Any:
    return {"number": 3, "head": {"ref": ref, "sha": "a" * 40, "repo": {"full_name": repo}}}


@pytest.fixture
def launched_for(monkeypatch):
    """Projects whose run carries `factory-loop`; the label names the issue it launched."""
    labels = {"issue-7-abc": [_issues.issue_label("acme/factory#7")]}
    monkeypatch.setattr(_loop, "_loop_run_labels", lambda project: labels.get(project, []))
    return labels


def test_loop_pull_needs_the_launch_record_and_the_backlog_issue(launched_for):
    assert _loop.loop_pull(loop_app([loop_issue()]), "acme/factory", pr())


@pytest.mark.parametrize(
    "app_issues, pull",
    [
        ([loop_issue()], pr(ref="feature/docs")),  # not a factory branch
        ([loop_issue()], pr(ref="factory/issue-9-zzz")),  # no loop run launched this project
        ([loop_issue()], pr(repo="fork/factory")),  # a fork's branch of the same name
        ([loop_issue(labels=[{"name": "bug"}])], pr()),  # issue lost the loop label
        ([loop_issue(body="no marker")], pr()),  # not made by the backlog generator
        ([loop_issue(user={"login": "stranger"})], pr()),  # marker typed by someone else
        ([loop_issue(number=8)], pr()),  # the run names another issue
        ([loop_issue(pull_request={})], pr()),  # a pull request is not a backlog issue
    ],
)
def test_loop_pull_rejects_everything_else(launched_for, app_issues, pull):
    assert not _loop.loop_pull(loop_app(app_issues), "acme/factory", pull)


def test_pr_body_text_is_not_a_link(launched_for):
    pull: Any = {**pr(ref="factory/other"), "body": "Closes #7"}
    assert not _loop.loop_pull(loop_app([loop_issue()]), "acme/factory", pull)


def loop_profile(loop: str = "", *, factory: bool = True) -> _profile.Profile:
    """The bench test profile (it has [deploy] and [direction]), with a [loop] table if `loop` is given."""
    from tests.test_bench import BENCH, PROFILE_TEXT

    text = PROFILE_TEXT
    if not factory:
        text = (
            text.replace('factory = "git@github.com:example-org/factory.git"\n', "")
            .replace('[repo_keys]\nfactory = "factory_key"\n', "")
            .replace('[repo_aliases]\nfactory = ["git@github.com:example-org/old-factory.git"]\n', "")
            .replace('repos = ["project", "factory"]', 'repos = ["project"]')
            # Auto deploy needs the repository it deploys from named once it cannot default to factory.
            .replace("[deploy]\n", '[deploy]\nrepo = "git@github.com:example-org/engine.git"\n')
        )
    return _profile.parse(text + BENCH + ("\n[loop]\n" + loop if loop else ""), "loop-test")


@pytest.mark.parametrize(
    "repo, path, expected",
    [
        ("example-org/direction", "projects/engine", "projects/engine"),
        ("example-org/direction", "projects//engine/", "projects/engine"),
        ("example-org/direction", "", ""),
        ("example-org/factory", "engine", "engine"),
        ("example-org/factory", "", ""),
    ],
)
def test_loop_repo_is_any_profile_repo_with_an_optional_subtree(repo, path, expected):
    parsed = loop_profile(f'enabled = true\nrepo = "{repo}"\npath = "{path}"\n').loop
    assert (parsed.repo, parsed.path) == (repo, expected)


def test_loop_path_defaults_to_the_repo_root():
    assert loop_profile('repo = "example-org/factory"\n').loop.path == ""


@pytest.mark.parametrize(
    "loop, error",
    [
        ('repo = "example-org/elsewhere"\n', "must be one of the profile's repositories"),
        ('repo = "example-org/direction"\npath = "../x"\n', "[loop] path"),
        ('repo = "example-org/direction"\npath = "a/../../x"\n', "[loop] path"),
        ('repo = "example-org/direction"\npath = "/etc"\n', "[loop] path"),
        ('repo = "example-org/direction"\npath = 3\n', "[loop] path must be a string"),
    ],
)
def test_a_bad_loop_repo_or_path_is_refused(loop, error):
    with pytest.raises(_profile.ProfileError, match=re.escape(error)):
        loop_profile("enabled = true\n" + loop)


def test_a_bench_needs_the_factory_repo_unless_the_loop_names_the_engine():
    # The loop's repo is then the bench's default source.
    loop = 'enabled = true\nrepo = "example-org/direction"\npath = "projects/engine"\n'
    assert loop_profile(loop, factory=False).bench is not None
    with pytest.raises(_profile.ProfileError, match=r"requires \[repo\] factory"):
        loop_profile(factory=False)
