"""Browser grants: carried from the launch input through the queue to the run's environment."""

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from flyte_agent_factory import _factory, _factory_base, _factory_mcp, _factory_policy, _issues, _queue
from tests.test_issues import event, profile  # noqa: F401 -- pytest fixture, used by name in the tests below

ENV = "AGENT_FACTORY_BROWSER_IDENTITIES"


@pytest.fixture(autouse=True)
def identities(monkeypatch):
    monkeypatch.setattr(
        _factory_policy, "browser_identity_names", lambda _: frozenset({"github", "linkedin"})
    )


def requests(name):
    queue = _factory_base.queue_root()
    return [_queue.load(queue, e).request for e in _queue.entries(queue, name)]


def env_of(world, index=-1):
    return world.created[index]["context"].get("env_vars", {})


def test_old_queue_json_loads_without_a_grant():
    request = _queue.Request("n", 1, "new", "b", "m", "r", "", {}, 1.0)
    raw = json.loads(request.to_json())
    del raw["browser"]
    assert _queue.Request.from_json(json.dumps(raw).encode()).browser == ()


def test_the_grant_round_trips_through_the_queue_json():
    request = _queue.Request("n", 1, "new", "b", "m", "r", "", {}, 1.0, browser=("github", "linkedin"))
    assert _queue.Request.from_json(request.to_json()).browser == ("github", "linkedin")


def test_a_malformed_grant_in_the_queue_json_is_refused():
    raw = json.loads(_queue.Request("n", 1, "new", "b", "m", "r", "", {}, 1.0).to_json())
    raw["browser"] = [1]
    with pytest.raises(ValueError, match="wrong type"):
        _queue.Request.from_json(json.dumps(raw).encode())


@pytest.mark.parametrize(
    ("text", "expected"), [("github", ("github",)), (" linkedin,github  github", ("github", "linkedin"))]
)
def test_names_are_split_sorted_and_deduplicated(text, expected):
    assert _factory_policy.parse_browser(text) == expected


def test_empty_means_not_given_and_none_means_no_grant():
    assert _factory_policy.parse_browser("  ") is None
    assert _factory_policy.parse_browser("none") == ()


def test_an_unknown_identity_is_refused_naming_the_configured_ones_and_nothing_is_enqueued(flyte_world):
    with pytest.raises(ValueError, match=r"unknown browser identity 'bank'.*github, linkedin"):
        _factory.launch_project("brief", "grant-one", browser="github,bank")
    assert requests("grant-one") == []
    assert flyte_world.created == []


def test_unknown_identity_with_none_configured_says_so(monkeypatch):
    monkeypatch.setattr(_factory_policy, "browser_identity_names", lambda _: frozenset())
    with pytest.raises(ValueError, match="configures no browser identities"):
        _factory_policy.parse_browser("github")


@pytest.mark.parametrize("name", ["GitHub", "1abc", "a" * 21, "a_b"])
def test_a_malformed_identity_name_is_refused(name):
    with pytest.raises(ValueError, match="must match"):
        _factory_policy.parse_browser(name)


def test_a_granted_run_gets_the_env_var_and_an_ungranted_one_does_not(flyte_world):
    _factory.launch_project("brief", "granted", browser="linkedin github")
    _factory.launch_project("brief", "plain")
    assert env_of(flyte_world, 0)[ENV] == "github,linkedin"
    assert ENV not in env_of(flyte_world, 1)
    assert requests("granted")[0].browser == ("github", "linkedin")


@pytest.mark.parametrize(
    "key",
    [
        "BROWSER_IDENTITIES",
        ENV,
        "BROWSER_GATE",
        "AGENT_FACTORY_BROWSER_GATE",
        "BROWSER_IDENTITY_NAMES",
    ],
)
def test_policy_cannot_set_the_grant(key, flyte_world):
    with pytest.raises(ValueError, match="browser"):
        _factory.launch_project("brief", "sneaky", policy={key: "github"})
    assert requests("sneaky") == []


def test_policy_keeps_its_other_keys_next_to_a_grant(flyte_world):
    _factory.launch_project("brief", "both", policy={"FOO": "1"}, browser="github")
    assert env_of(flyte_world) == {"AGENT_FACTORY_FOO": "1", ENV: "github"}


def finish(world, result):
    world.runs[result["run"]].phase = "SUCCEEDED"


def test_a_resume_inherits_the_grant_none_revokes_and_names_replace(flyte_world):
    finish(flyte_world, _factory.launch_project("brief", "carry", browser="github"))
    finish(flyte_world, _factory.launch_project("more", "carry"))
    assert env_of(flyte_world)[ENV] == "github"
    finish(flyte_world, _factory.launch_project("more", "carry", browser="linkedin"))
    assert env_of(flyte_world)[ENV] == "linkedin"
    finish(flyte_world, _factory.launch_project("more", "carry"))
    assert env_of(flyte_world)[ENV] == "linkedin"
    _factory.launch_project("more", "carry", browser="none")
    assert ENV not in env_of(flyte_world)
    assert requests("carry")[-1].browser == ()


def test_replacing_a_waiting_request_keeps_its_grant_unless_told_otherwise(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    _factory.launch_project("brief", "waits", browser="github")
    assert _factory.launch_project("changed", "waits")["status"] == "updated"
    assert requests("waits")[0].browser == ("github",)
    _factory.launch_project("changed", "waits", browser="none")
    assert requests("waits")[0].browser == ()


def test_a_new_project_with_no_browser_input_has_no_grant(flyte_world):
    _factory.launch_project("brief", "fresh")
    assert requests("fresh")[0].browser == ()


def test_the_queue_and_status_rows_show_the_grant(flyte_world):
    flyte_world.add("one", _factory_base.SLOTS[0])
    flyte_world.add("two", _factory_base.SLOTS[1])
    _factory.launch_project("brief", "shown", browser="github")
    _factory.launch_project("brief", "hidden")
    rows = {row["name"]: row for row in _factory.queue_view()["queue"]}
    assert rows["shown"]["browser"] == ["github"]
    assert "browser" not in rows["hidden"]


def test_the_launch_task_takes_the_browser_input(flyte_world):
    import asyncio

    result = json.loads(asyncio.run(_factory.launch.func("brief", "viatask", browser="github")))
    assert result["status"] == "started"
    assert env_of(flyte_world)[ENV] == "github"


def test_the_planner_path_passes_the_grant(queue_state, monkeypatch):
    # Same fakes as test_planner_admission's `runtime` fixture, with a queued request instead of a board item.
    from flyte import remote

    from flyte_agent_factory import _planner
    from tests.test_planner_admission import Git

    monkeypatch.setattr(remote.Run, "listall", lambda **_: [])
    monkeypatch.setattr(_factory, "_resolve_repo", lambda repo, **_: repo)
    from flyte_agent_factory import _planner_runtime

    monkeypatch.setattr(_planner_runtime, "_intents", lambda *_: [])
    run = SimpleNamespace(name="project-run", url="https://console.example/run", phase="RUNNING")
    submitted = []

    def launch_on_slot(*a, **kw):
        submitted.append(kw)
        return run

    monkeypatch.setattr(_factory, "launch_on_slot", launch_on_slot)
    monkeypatch.setattr(_planner_runtime, "_matches", lambda _: [run])
    git = Git()
    git.default_branch_sha = lambda *_: ("main", "base")  # type: ignore[attr-defined]
    board = SimpleNamespace(set_status=lambda *_: None, set_text=lambda *_: None)
    facts = _planner.Facts(
        manual_requests=(_planner.ManualRequest("waiting", "team/repo", body="x"),),
        capacity=(_planner.Capacity("claude", "login", slot="slot"),),
    )
    request = _queue.Request("waiting", 1, "new", "x", "", "team/repo", "", {}, 1.0, browser=("github",))
    plain = replace(request, name="plain", browser=())
    _planner_runtime.execute(
        cast(Any, None),
        cast(Any, git),
        "team/direction",
        "base",
        cast(Any, board),
        [],
        facts,
        {"waiting": request},
    )
    assert submitted[0]["identities"] == ("github",)
    facts = replace(facts, manual_requests=(_planner.ManualRequest("plain", "team/repo", body="x"),))
    _planner_runtime.execute(
        cast(Any, None),
        cast(Any, git),
        "team/direction",
        "base",
        cast(Any, board),
        [],
        facts,
        {"plain": plain},
    )
    assert submitted[1]["identities"] == ()


def label_event(*labels):
    payload = event()
    payload["issue"]["labels"] = [{"name": n} for n in ("factory", *labels)]
    return payload


def test_intake_labels_become_the_grant(profile, flyte_world):  # noqa: F811 -- the imported pytest fixture
    result = _issues.process_event(profile, "issues", label_event("browser:GitHub", "bug"))
    assert result["status"] == "started"
    assert env_of(flyte_world)[ENV] == "github"


def test_an_unknown_intake_label_is_ignored_and_reported(profile, flyte_world, caplog):  # noqa: F811 -- the imported pytest fixture
    result = _issues.process_event(profile, "issues", label_event("browser:github", "browser:bank"))
    assert env_of(flyte_world)[ENV] == "github"
    assert "browser:bank" in str(result["browser_ignored"]) and "github, linkedin" in str(
        result["browser_ignored"]
    )
    assert "browser:bank" in caplog.text


def test_an_issue_without_a_browser_label_launches_with_no_input(profile, flyte_world, monkeypatch):  # noqa: F811 -- the imported pytest fixture
    seen = []
    real = _factory.launch_project

    def launch_project(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(_factory, "launch_project", launch_project)
    _issues.process_event(profile, "issues", event())
    assert seen[0]["browser"] == ""


def test_browser_link_validates_the_identity_and_returns_json(monkeypatch):
    import asyncio

    from flyte_agent_factory import browser

    monkeypatch.setattr(browser, "view_link_for", lambda identity: f"https://view.example/{identity}")
    result = asyncio.run(_factory.browser_link.func("github"))
    assert json.loads(result) == {"identity": "github", "url": "https://view.example/github"}
    with pytest.raises(ValueError, match="unknown browser identity"):
        asyncio.run(_factory.browser_link.func("bank"))


def test_browser_link_asks_the_app_with_the_token_mounted_in_the_factory_pod(monkeypatch):
    import asyncio

    from flyte_agent_factory import browser

    asked = []
    monkeypatch.setattr(browser, "identity_endpoint", lambda name: f"http://app-{name}")
    monkeypatch.setattr(browser, "identity_public_endpoint", lambda name: f"https://public/{name}")

    def view_link_from(endpoint, public, token):
        asked.append((endpoint, token))
        return f"{public}/view-login?ticket=t"

    monkeypatch.setattr(browser, "view_link_from", view_link_from)
    monkeypatch.setenv("BROWSER_GITHUB_TOKEN", "the-token")
    result = asyncio.run(_factory.browser_link.func("github"))
    assert json.loads(result)["url"] == "https://public/github/view-login?ticket=t"
    assert asked == [("http://app-github", "the-token")]
    assert "the-token" not in result


def test_chat_may_call_browser_link_and_is_told_about_it():
    assert "browser_link" in _factory_mcp.CHAT_TASKS
    assert "factory.browser_link" in _factory_mcp.INSTRUCTIONS


def test_status_shows_the_projects_grant(flyte_world, monkeypatch):
    monkeypatch.setattr(_factory_base, "report_html", lambda run: "")
    _factory.launch_project("brief", "statused", browser="github")
    assert _factory.read_status("statused")["browser"] == ["github"]


def test_a_grant_key_in_a_queued_requests_policy_is_not_a_grant(flyte_world):
    """A request file written by hand (not through `launch`) cannot grant through its policy."""
    _factory.run_project(cast(Any, object()), "brief", "hand", policy={ENV: "github"})
    assert ENV not in env_of(flyte_world)
    _factory.run_project(
        cast(Any, object()), "brief", "hand", policy={ENV: "github"}, identities=("linkedin",)
    )
    assert env_of(flyte_world)[ENV] == "linkedin"
