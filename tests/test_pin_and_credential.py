"""#63: route and meter on the credential actually mounted; an explicit pin wins or fails loudly.

The 2026-10-30 case: a slot's file login was at 100% weekly while the profile's setup-token had been
swapped to a fresh account, and projects pinned to Claude started as the Codex author.
"""

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import flyte.errors
import pytest
from flyte import remote

from flyte_agent_factory import (
    _author,
    _factory,
    _factory_base,
    _factory_meters,
    _factory_policy,
    _issues,
    _meter,
    _planner,
    _planner_facts,
    _queue,
    _session_config,
    _timeline,
    _usage,
    agent,
)
from flyte_agent_factory._profile import Profile

NOW = 1_800_000_000
PIN = "AGENT_FACTORY_COORDINATOR_ENGINE"
SECRET = "claude_code_oauth_token"


def _without_codex(profile: Any) -> Any:
    return replace(profile, secrets=replace(profile.secrets, codex_token=""))


def _token_profile() -> Any:
    return replace(_factory_base.PROFILE, secrets=replace(_factory_base.PROFILE.secrets, claude_token=SECRET))


# --------------------------------------------------------------------------- #
# R1: a pin is never silently overwritten
# --------------------------------------------------------------------------- #


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make the planner's runtime a git checkout with a known default branch."""
    from flyte_agent_factory import _planner_runtime as module
    from tests.test_planner_admission import Git

    git: Any = Git()
    monkeypatch.setattr(module, "_intents", lambda *_: [])
    monkeypatch.setattr(remote.Run, "listall", lambda **_: [])
    monkeypatch.setattr(_factory, "_resolve_repo", lambda repo, **_: repo)
    monkeypatch.setattr(_issues, "repo_targets", lambda _: {"team/repo": ("team/repo", "team/repo")})
    git.default_branch_sha = lambda *_: ("main", "base")
    run = SimpleNamespace(name="project-run", url="https://console.example/run", phase="RUNNING")
    submitted = []

    def submit(*args, **kwargs):
        submitted.append(kwargs)
        return run

    monkeypatch.setattr(_factory, "launch_on_slot", submit)
    monkeypatch.setattr(module, "_matches", lambda _: [run])
    monkeypatch.setattr(module, "reconcile", lambda *_: None)  # closes the queue entry, not under test
    monkeypatch.setattr(_queue, "effective_brief", lambda request: request.brief)
    board: Any = SimpleNamespace(set_status=lambda *_: None, set_text=lambda *_: None)
    item = _planner.BoardItem("I1", 1, "team/repo", status="Ready", body="## Files\n- a.py")
    rows: Any = [{"node_id": "I1", "repo": "team/repo", "number": 1, "item_id": "card", "status": "Ready"}]

    # Deliberately None: the stubbed runtime never reads the profile.
    none_profile: Any = None

    def execute(capacity, policy, author_engine="claude"):
        facts = _planner.Facts(items=(item,), capacity=tuple(capacity), author_engine=author_engine)
        request = _queue.Request("I1", 1, "new", "brief", "opus", "team/repo", "", policy, NOW)
        launched = module.execute(
            none_profile, git, "team/direction", "base", board, rows, facts, {"I1": request}
        )
        return launched, submitted

    return execute


def _caps(claude: Any = None, codex: Any = None) -> list[Any]:
    return [
        _planner.Capacity("claude", "claude-login", claude, slot="slot"),
        _planner.Capacity("codex", "codex-login", codex, slot="slot"),
    ]


def test_r1_a_request_pin_is_carried_through_admission_unchanged(runtime):
    # The preferred engine is Codex and Codex has the headroom; the request's pin still decides.
    """R1 a request pin is carried through admission unchanged."""
    codex_fresh = _meter.Meter("codex codex-login", "app-server", time.time(), 5, time.time() + 3600)
    launched, submitted = runtime(_caps(codex=codex_fresh), {PIN: "claude"}, author_engine="codex")
    assert launched == ["project-run"]
    assert submitted[0]["policy"][PIN] == "claude"
    assert _planner_note_key() not in submitted[0]["policy"]


def test_r1_an_exhausted_pin_stays_the_pin_and_the_pod_says_why(runtime):
    """R1 an exhausted pin stays the pin and the pod says why."""
    limited = _meter.Meter(
        "claude claude-login", "cli-headers", time.time(), limited_until=time.time() + 3600
    )
    launched, submitted = runtime(_caps(claude=limited), {PIN: "claude"})
    assert launched == ["project-run"]
    # The slot runs the other engine only if the pod's own router finds the pin still exhausted, and
    # then the pod writes the "pin overridden" row (R4); admission does not rewrite the request.
    assert submitted[0]["policy"][PIN] == "claude"


def test_r1_a_pin_to_an_engine_without_a_login_waits_instead_of_running_on_the_other(runtime):
    """R1 a pin to an engine without a login waits instead of running on the other."""
    launched, submitted = runtime([_planner.Capacity("claude", "claude-login", slot="slot")], {PIN: "codex"})
    assert launched == []
    assert submitted == []


def test_r1_the_planner_names_why_a_pinned_request_waits():
    """R1 the planner names why a pinned request waits."""
    item = _planner.ManualRequest("m", "team/repo", files=("a",))
    facts = _planner.Facts(
        manual_requests=(item,),
        capacity=(_planner.Capacity("claude", "login", slot="s"),),
        pins={"m": "codex"},
    )
    result = _planner.plan(facts)
    assert not result.launches
    assert result.waiting[0].reason == _planner.Reason("pin_unconfigured", "codex")


def test_r1_without_a_pin_admission_records_why_it_left_the_preferred_engine(runtime):
    """R1 without a pin admission records why it left the preferred engine."""
    from flyte_agent_factory import _planner_runtime

    launched, submitted = runtime([_planner.Capacity("codex", "codex-login", slot="slot")], {})
    assert launched == ["project-run"]
    policy = submitted[0]["policy"]
    # Admission's choice is a preference under its own key, never the user's pin (see R4).
    assert PIN not in policy
    assert policy[_planner_runtime.ADMITTED_KEY] == "codex"
    assert policy[_planner_note_key()]


def _planner_note_key() -> str:
    from flyte_agent_factory import _planner_runtime

    return str(_planner_runtime.NOTE_KEY)


def test_r1_the_launch_is_refused_when_the_pinned_engine_has_no_login(monkeypatch):
    """R1 the launch is refused when the pinned engine has no login."""
    profile = _without_codex(_factory_base.PROFILE)
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    with pytest.raises(ValueError, match="pins codex, but this profile has no codex token"):
        _factory._check_author_pin({PIN: "codex"})
    _factory._check_author_pin({PIN: "claude"})
    _factory._check_author_pin({})


def test_r1_a_pin_written_in_any_case_is_refused_the_same_way(monkeypatch):
    """R1 a pin written in any case is refused the same way."""
    monkeypatch.setattr(_factory_base, "PROFILE", _without_codex(_factory_base.PROFILE))
    with pytest.raises(ValueError, match="pins codex, but this profile has no codex token"):
        _factory._check_author_pin(_factory_policy.normalize_policy({"AUTHOR_ENGINE": "Codex"}))


def test_r1_the_admission_note_reaches_the_timeline_of_a_first_start(monkeypatch, tmp_path):
    """R1 the admission note reaches the timeline of a first start."""
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_ADMISSION_NOTE", "claude exhausted; only codex has headroom")
    first, resumed = _timeline._Timeline(), _timeline._Timeline()
    _author._record_author_choice("codex", False, first)
    _author._record_author_choice("codex", True, resumed)
    assert [t for _, _, t in first._rows] == [
        "admission chose codex: claude exhausted; only codex has headroom"
    ]
    assert not resumed._rows


# --------------------------------------------------------------------------- #
# R2: a token session is metered on the token, not on the slot's login
# --------------------------------------------------------------------------- #


def test_r2_the_planner_meters_a_token_profile_on_the_token_not_the_slot_login():
    """R2 the planner meters a token profile on the token not the slot login."""
    profile = _token_profile()
    slot = profile.slots[0]
    login = _meter.Meter(
        "claude other_login",
        "oauth-usage",
        NOW,
        10,
        NOW + 3600,
        100,
        NOW + 86400,
        limited_until=NOW + 600,
    )
    token = _meter.Meter(_meter.token_account(SECRET, "fresh-token"), "cli-headers", NOW - 60)
    reports = {slot.task: [login, token]}
    readings = {m.account: m for m in reports[slot.task]}
    assert _factory_meters.match_claude_meter(profile, slot, readings, reports) is token
    capacity = _planner_facts.free_capacity(profile, [], reports)
    claude = [c for c in capacity if c.engine == "claude" and c.slot == slot.task]
    assert claude and claude[0].meter is token


def test_r2_the_planner_picks_claude_for_a_pinned_request_while_the_slot_login_is_full():
    """R2 the planner picks claude for a pinned request while the slot login is full."""
    profile = _token_profile()
    slot = profile.slots[0]
    token = _meter.Meter(_meter.token_account(SECRET, "fresh-token"), "cli-headers", time.time())
    capacity = [
        _planner.Capacity("claude", SECRET, token, slot=slot.task),
        _planner.Capacity("codex", "codex-login", slot=slot.task),
    ]
    item = _planner.ManualRequest("m", "team/repo", files=("a",))
    facts = _planner.Facts(
        manual_requests=(item,), capacity=tuple(capacity), pins={"m": "claude"}, now=time.time()
    )
    assert [launch.engine for launch in _planner.plan(facts).launches] == ["claude"]


def _token_pod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str = "fresh-token", secret: str = SECRET
) -> Any:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    monkeypatch.setenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET", secret)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "codex_token")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(_author, "_CODEX_THREAD_FILE", tmp_path / "codex-thread")
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    store = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", store)

    async def noop(_timeline):
        pass

    monkeypatch.setattr(_usage, "_read_meters", noop)
    return store


def test_r2_the_slot_login_meter_never_decides_a_token_session(monkeypatch, tmp_path):
    """R2 the slot login meter never decides a token session."""
    store = _token_pod(monkeypatch, tmp_path)
    now = time.time()
    store.update(
        _meter.Meter(
            "claude slot_login",
            "oauth-usage",
            now,
            90,
            now + 3600,
            100,
            now + 86400,
            limited_until=now + 3600,
        )
    )
    store.latest["claude"] = store.readings["claude slot_login"]
    assert _usage._child_meter("claude") is None
    assert not _usage._engine_exhausted("claude")
    assert _usage._route("delegate", None).engine == "claude"


async def test_r2_the_author_is_claude_when_only_the_slot_login_is_limited(monkeypatch, tmp_path):
    """R2 the author is claude when only the slot login is limited."""
    store = _token_pod(monkeypatch, tmp_path)
    now = time.time()
    store.update(_meter.Meter("claude slot_login", "oauth-usage", now, limited_until=now + 3600))
    monkeypatch.setenv(PIN, "claude")
    assert (
        await _author._choose_author_engine("", ("claude", "codex"), False, _timeline._Timeline()) == "claude"
    )


def test_r2_a_token_session_without_a_named_secret_is_still_the_tokens_account(monkeypatch, tmp_path):
    """R2 a token session without a named secret is still the tokens account."""
    _token_pod(monkeypatch, tmp_path)
    monkeypatch.delenv("AGENT_FACTORY_CLAUDE_TOKEN_SECRET")
    account = _session_config._claude_account()
    assert account != "claude slot_login"
    assert "fresh-token" not in account


# --------------------------------------------------------------------------- #
# R3: a swapped token does not inherit the old one's refusal
# --------------------------------------------------------------------------- #


def test_r3_a_token_swapped_under_the_same_secret_name_inherits_no_refusal(monkeypatch, tmp_path):
    """R3 a token swapped under the same secret name inherits no refusal."""
    store = _token_pod(monkeypatch, tmp_path, token="old-token")
    _usage._note_engine_limit("claude", "usage limit, resets 8:40pm (UTC)")
    assert _usage._engine_exhausted("claude")
    old_account = _session_config._claude_account()
    # The secret is replaced; the pod restores the checkpointed meters and mounts the new value.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "new-token")
    assert _session_config._claude_account() != old_account
    reloaded = _meter.MeterStore(tmp_path / "meters")
    monkeypatch.setattr(_usage, "_meters", reloaded)
    assert old_account in reloaded.readings
    assert _usage._child_meter("claude") is None
    assert not _usage._engine_exhausted("claude")
    assert not _usage._route("delegate", None).deferred
    assert store.readings[old_account].limited_until


def test_r3_a_new_reading_of_the_new_token_does_not_carry_the_old_reset_forward(monkeypatch, tmp_path):
    """R3 a new reading of the new token does not carry the old reset forward."""
    store = _token_pod(monkeypatch, tmp_path, token="old-token")
    _usage._note_engine_limit("claude", "usage limit, resets 8:40pm (UTC)")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "new-token")
    store.update(_meter.Meter(_session_config._claude_account(), "cli-headers", time.time()))
    assert store.readings[_session_config._claude_account()].limited_until is None


def test_r3_the_planner_trusts_only_the_mounted_tokens_readings_over_an_old_refusal(monkeypatch):
    """R3 the planner trusts only the mounted tokens readings over an old refusal."""
    profile = _token_profile()
    slot = profile.slots[0]
    old = _meter.Meter(_meter.token_account(SECRET, "old"), "cli-headers", NOW, limited_until=NOW + 3600)
    legacy = _meter.Meter(f"claude {SECRET}", "cli-headers", NOW - 10, limited_until=NOW + 3600)
    new = _meter.Meter(_meter.token_account(SECRET, "new"), "cli-headers", NOW + 5)
    readings = {m.account: m for m in (old, legacy, new)}
    monkeypatch.setenv(_factory_meters.TOKEN_ENV, "new")
    assert _factory_meters.match_claude_meter(profile, slot, readings, {}) is new
    # Before the new token reports, the old token's newer refusal does not hold the slot. This asserted
    # `is old` while the planner could not see which token is mounted: a swapped token then waited out
    # the old one's refusal (up to a week).
    readings.pop(new.account)
    assert _factory_meters.match_claude_meter(profile, slot, readings, {}) is legacy
    readings.pop(legacy.account)
    assert _factory_meters.match_claude_meter(profile, slot, readings, {}) is None


def test_r3_without_the_mount_the_newest_reading_of_the_secret_speaks_for_it():
    """R3 without the mount the newest reading of the secret speaks for it."""
    profile = _token_profile()
    slot = profile.slots[0]
    old = _meter.Meter(_meter.token_account(SECRET, "old"), "cli-headers", NOW, limited_until=NOW + 3600)
    new = _meter.Meter(_meter.token_account(SECRET, "new"), "cli-headers", NOW + 5)
    readings = {m.account: m for m in (old, new)}
    assert _factory_meters.match_claude_meter(profile, slot, readings, {}) is new
    readings.pop(new.account)
    assert _factory_meters.match_claude_meter(profile, slot, readings, {}) is old


def test_r3_admission_launches_claude_after_a_token_swap(monkeypatch):
    """The QA repro: only the old token's six-day refusal is on record, and the new token is mounted."""
    profile = _token_profile()
    slot = profile.slots[0]
    now = time.time()
    old = _meter.Meter(_meter.token_account(SECRET, "old"), "cli-headers", now, limited_until=now + 6 * 86400)
    monkeypatch.setenv(_factory_meters.TOKEN_ENV, "new")
    capacity = _planner_facts.free_capacity(profile, [], {slot.task: [old]})
    item = _planner.ManualRequest("m", "team/repo", files=("a",))
    facts = _planner.Facts(manual_requests=(item,), capacity=tuple(capacity), now=now)
    assert [launch.engine for launch in _planner.plan(facts).launches] == ["claude"]


def _secrets(profile: Profile) -> list[flyte.Secret]:
    mounted = _factory._env_secrets(profile)
    assert all(isinstance(s, flyte.Secret) for s in mounted)
    return cast(list[flyte.Secret], mounted)


def test_r3_the_factory_mounts_the_token_only_to_fingerprint_it(monkeypatch):
    """R3 the factory mounts the token only to fingerprint it."""
    mounted = {s.key: s.as_env_var for s in _secrets(_token_profile())}
    assert mounted[SECRET] == _factory_meters.TOKEN_ENV != "CLAUDE_CODE_OAUTH_TOKEN"
    login_only = replace(
        _factory_base.PROFILE, secrets=replace(_factory_base.PROFILE.secrets, claude_token="")
    )
    assert _factory_meters.TOKEN_ENV not in {s.as_env_var for s in _secrets(login_only)}
    monkeypatch.setenv(_factory_meters.TOKEN_ENV, "sk-ant-oat01-very-secret")
    expected = _meter.token_account(SECRET, "sk-ant-oat01-very-secret").rpartition("#")[2]
    assert _factory_meters.mounted_token_fingerprint() == expected
    # Read once, and gone from the environment of anything the task spawns.
    assert _factory_meters.TOKEN_ENV not in os.environ
    assert _factory_meters.mounted_token_fingerprint() == expected


def test_r3_a_saved_refusal_of_another_credential_is_not_restored(monkeypatch, tmp_path):
    """R3 a saved refusal of another credential is not restored."""
    from flyte_agent_factory._author_session import AuthorProcess, AuthorSession
    from tests.test_nudge import _Proc

    _token_pod(monkeypatch, tmp_path, token="new-token")
    monkeypatch.setattr(_usage, "_author_engine", "claude")
    until = time.time() + 600
    old = AuthorSession(cast(AuthorProcess, _Proc()), tmp_path / "pending-old.json")
    _author._restore_refusal(
        old, {"engine": "claude", "limited_until": until, "account": "claude secret#abc"}
    )
    assert not old._paused
    # A record from before refusals named their account is not trusted either.
    legacy = AuthorSession(cast(AuthorProcess, _Proc()), tmp_path / "pending-legacy.json")
    _author._restore_refusal(legacy, {"engine": "claude", "limited_until": until})
    assert not legacy._paused
    same = AuthorSession(cast(AuthorProcess, _Proc()), tmp_path / "pending-same.json")
    _author._restore_refusal(
        same, {"engine": "claude", "limited_until": until, "account": _session_config._claude_account()}
    )
    assert same._paused


def test_the_fingerprint_names_the_credential_without_exposing_it():
    """The fingerprint names the credential without exposing it."""
    account = _meter.token_account(SECRET, "sk-ant-oat01-very-secret")
    assert account.startswith(f"claude {SECRET}#") and len(account.rpartition("#")[2]) == 12
    assert "very-secret" not in account
    assert _meter.token_account(SECRET, "sk-ant-oat01-very-secret") == account
    assert _meter.token_account(SECRET, "") == f"claude {SECRET}"
    assert _meter.token_account_secret(account) == SECRET
    assert _meter.token_account_secret(f"claude {SECRET}") == ""


def test_a_stored_reading_without_a_fingerprint_loads_and_is_ignored(monkeypatch, tmp_path):
    """A stored reading without a fingerprint loads and is ignored."""
    store = _token_pod(monkeypatch, tmp_path)
    legacy = _meter.Meter(f"claude {SECRET}", "limit-text", time.time(), limited_until=time.time() + 3600)
    store.update(legacy)
    reloaded = _meter.MeterStore(tmp_path / "meters")
    assert f"claude {SECRET}" in reloaded.readings
    monkeypatch.setattr(_usage, "_meters", reloaded)
    assert _usage._child_meter("claude") is None


# --------------------------------------------------------------------------- #
# R4: a pin beats resume stickiness; it fails loudly or says it was overridden
# --------------------------------------------------------------------------- #


async def _choose(
    pin: str,
    resumed: bool,
    owner: dict[str, Any] | None = None,
    tmp_path: Any = None,
    available: tuple[str, ...] = ("claude", "codex"),
) -> tuple[Any, list[str]]:
    if owner is not None:
        (tmp_path / "author-conversation.json").write_text(json.dumps(owner))
    timeline = _timeline._Timeline()
    engine = await _author._choose_author_engine(pin, available, resumed, timeline)
    return engine, [text for _, _, text in timeline._rows]


async def test_r4_a_resumed_codex_owner_with_a_claude_pin_goes_back_to_claude(monkeypatch, tmp_path):
    """R4 a resumed codex owner with a claude pin goes back to claude."""
    _token_pod(monkeypatch, tmp_path)
    codex_owner = {"engine": "codex", "thread": "t-1", "pin": "claude"}
    engine, _ = await _choose("claude", True, codex_owner, tmp_path)
    assert engine == "claude"
    monkeypatch.setenv(PIN, "claude")
    assert (await _choose("", True, codex_owner, tmp_path))[0] == "claude"
    # Without a pin the conversation's owner keeps it, as before.
    monkeypatch.delenv(PIN)
    assert (await _choose("", True, codex_owner, tmp_path))[0] == "codex"


def test_r4_the_conversation_owner_follows_the_pin_so_claude_resumes_its_own_transcript(
    monkeypatch, tmp_path
):
    # The codex thread must not be read as Claude's session id when the pin brings Claude back.
    """R4 the conversation owner follows the pin so claude resumes its own transcript."""
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path)
    owner_file = tmp_path / "author-conversation.json"
    owner_file.write_text(json.dumps({"engine": "codex", "thread": "codex-thread", "fresh": True}))
    _author._record_author_choice("claude", True, _timeline._Timeline())
    # This asserted `== {"engine": "claude"}`, which threw the other engine's thread away. It is kept, but
    # under `previous`, where nothing reads it as Claude's.
    assert json.loads(owner_file.read_text()) == {
        "engine": "claude",
        "previous": {"engine": "codex", "thread": "codex-thread", "fresh": True},
    }
    owner_file.write_text(json.dumps({"engine": "claude", "thread": "kept"}))
    _author._record_author_choice("claude", True, _timeline._Timeline())
    assert json.loads(owner_file.read_text())["thread"] == "kept"


async def test_r4_an_exhausted_pin_is_overridden_and_the_timeline_says_so(monkeypatch, tmp_path):
    """R4 an exhausted pin is overridden and the timeline says so."""
    store = _token_pod(monkeypatch, tmp_path)
    store.note_limit("claude", time.time() + 3600, _session_config._claude_account())
    engine, rows = await _choose("claude", False)
    assert engine == "codex"
    assert any(row.startswith("pin overridden: claude") and "exhausted" in row for row in rows)


async def test_r4_a_pin_that_is_only_open_is_not_reported_as_overridden(monkeypatch, tmp_path):
    """R4 a pin that is only open is not reported as overridden."""
    _token_pod(monkeypatch, tmp_path)
    engine, rows = await _choose("claude", False)
    assert engine == "claude"
    assert not any("pin overridden" in row for row in rows)


async def test_r4_a_pin_to_an_engine_with_no_credential_fails_loudly(monkeypatch, tmp_path):
    """R4 a pin to an engine with no credential fails loudly."""
    _token_pod(monkeypatch, tmp_path)
    monkeypatch.setattr(_session_config, "CODEX_TOKEN_SECRET", "")
    with pytest.raises(flyte.errors.NonRecoverableError, match="pinned to codex.*no codex login mounted"):
        await _choose("codex", False, available=("claude",))
    monkeypatch.setenv(PIN, "codex")
    with pytest.raises(flyte.errors.NonRecoverableError, match="will not run on the other engine"):
        await _choose("", True, {"engine": "claude"}, tmp_path, available=("claude",))
    with pytest.raises(flyte.errors.NonRecoverableError):
        await _choose("claude", False, available=("codex",))


async def test_r4_the_10_30_scenario_end_to_end(monkeypatch, tmp_path):
    """Slot login full and limited, a fresh token mounted, the launch pinned to Claude."""
    store = _token_pod(monkeypatch, tmp_path)
    monkeypatch.setenv(PIN, "claude")
    now = time.time()
    store.update(
        _meter.Meter(
            "claude slot_login",
            "oauth-usage",
            now,
            100,
            now + 3600,
            100,
            now + 86400,
            limited_until=now + 86400,
        )
    )
    store.latest["claude"] = store.readings["claude slot_login"]
    # A project that an earlier attempt handed to Codex, resumed with the pin still set.
    owner = {"engine": "codex", "pin": "claude", "limited_until": now + 3600}
    engine, rows = await _choose("", True, owner, tmp_path)
    assert engine == "claude"
    assert not any("pin overridden" in row for row in rows)


# --------------------------------------------------------------------------- #
# QA follow-ups to #63
# --------------------------------------------------------------------------- #


async def test_admissions_choice_is_no_pin_so_a_handed_off_project_stays_on_codex(monkeypatch, tmp_path):
    # Admission chose Claude; a mid-work handoff moved the conversation to Codex; the pod restarts.
    """Admissions choice is no pin so a handed off project stays on codex."""
    _token_pod(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_ADMITTED_ENGINE", "claude")
    owner = {"engine": "codex", "thread": "t-1", "fresh": True, "pin": ""}
    assert (await _choose("", True, owner, tmp_path))[0] == "codex"


async def test_admissions_choice_is_preferred_for_a_new_conversation(monkeypatch, tmp_path):
    """Admissions choice is preferred for a new conversation."""
    _token_pod(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENT_FACTORY_ADMITTED_ENGINE", "codex")
    engine, rows = await _choose("", False)
    assert engine == "codex"
    assert not any("pin overridden" in row for row in rows)


async def test_a_pin_that_moves_a_resumed_project_hands_it_over_with_a_briefing(monkeypatch, tmp_path):
    """A pin that moves a resumed project hands it over with a briefing."""
    _token_pod(monkeypatch, tmp_path)
    monkeypatch.setattr(_session_config, "WORKSPACE", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    owner_file = tmp_path / "author-conversation.json"
    until = time.time() + 600
    owner_file.write_text(
        json.dumps({"engine": "codex", "thread": "t-1", "limited_until": until, "account": "codex x"})
    )
    (tmp_path / "author-pending.json").write_text(json.dumps(["queued direction"]))
    timeline = _timeline._Timeline()
    previous = _author._record_author_choice("claude", True, timeline)
    assert previous == {"engine": "codex", "thread": "t-1"}
    assert json.loads(owner_file.read_text()) == {
        "engine": "claude",
        "limited_until": until,
        "account": "codex x",
        "previous": {"engine": "codex", "thread": "t-1"},
    }
    assert any("author resumes on claude" in text and "t-1" in text for _, _, text in timeline._rows)
    await _author._queue_resume_handoff(previous, "claude", "the brief", timeline)
    pending = json.loads((tmp_path / "author-pending.json").read_text())
    assert pending[1:] == ["queued direction"]
    assert pending[0].startswith("AUTHOR HANDOFF: codex -> claude (resumed on claude)")
    assert "Git status:" in pending[0] and "Diff summary:" in pending[0] and "thread t-1" in pending[0]
    # Nothing moved, nothing to hand over.
    assert _author._record_author_choice("claude", True, _timeline._Timeline()) == {}


def test_the_loop_spend_guard_sees_the_tokens_fingerprinted_reading(monkeypatch):
    """The loop spend guard sees the tokens fingerprinted reading."""
    from flyte_agent_factory import _loop, _loop_flow

    profile = _token_profile()
    profile = replace(profile, loop=replace(profile.loop, daily_usd=100, weekly_usd=500))
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    now = time.time()
    meter = _meter.Meter(_meter.token_account(SECRET, "t"), "cli-headers", now, limited_until=now + 3600)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda **_: {meter.account: meter})
    monkeypatch.setattr(_loop_flow, "_spend", lambda _: [])
    seen = {}

    def budget(now, spend, meters, *args, **kwargs):
        seen.update(meters)
        return _loop.Budget("wait", "limited")

    monkeypatch.setattr(_loop, "budget", budget)
    _factory_meters._loop_slot_budget(profile.slots[0].task, demand=False)
    assert seen == {"claude": meter}


def test_meter_logins_finds_the_tokens_cached_fingerprinted_reading(monkeypatch, capsys):
    """Meter logins finds the tokens cached fingerprinted reading."""
    from ops import meter_logins

    profile = _without_codex(_token_profile())
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    cached = _meter.Meter(_meter.token_account(SECRET, "t"), "cli-headers", time.time(), 42)
    monkeypatch.setattr(_factory_meters, "login_meters", lambda: {cached.account: cached})
    monkeypatch.setattr(meter_logins, "_init", lambda: None)

    def submit(slot, **kwargs):
        raise AssertionError("a fresh cached reading was probed again")

    meter_logins.main([], submit=cast(Any, submit))
    assert "unavailable" not in capsys.readouterr().out


def test_a_queued_pin_to_an_engine_without_a_login_fails_instead_of_waiting(monkeypatch):
    """A queued pin to an engine without a login fails instead of waiting."""
    from flyte_agent_factory import _planner_runtime as module

    profile = _without_codex(_factory_base.PROFILE)
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(module, "_intents", lambda *_: [])
    marked: list[tuple[Any, Any]] = []
    monkeypatch.setattr(_queue, "mark", lambda *args, **kwargs: marked.append((args, kwargs)))
    monkeypatch.setattr(_factory_base, "queue_root", lambda: "queue")
    request = _queue.Request("m", 3, "new", "brief", "opus", "team/repo", "", {PIN: "codex"}, NOW)
    item = _planner.ManualRequest("m", "team/repo", files=("a",))
    facts = _planner.Facts(
        manual_requests=(item,), capacity=(_planner.Capacity("claude", "login", slot="s"),)
    )
    # Deliberately None: a pin that cannot run is failed before the profile, app or board are touched.
    none: Any = None
    assert module.execute(none, none, "team/direction", "base", none, [], facts, {"m": request}) == []
    ((args, kwargs),) = marked
    assert args == ("queue", "m", 3, "failed")
    assert "pins codex, but this profile has no codex token" in kwargs["error"]


def test_a_pin_the_router_moves_off_still_names_the_pin_in_the_launch():
    # The pod is handed the pin; the record and the board must name what it will run.
    """A pin the router moves off still names the pin in the launch."""
    now = time.time()
    limited = _meter.Meter("claude login", "cli-headers", now, limited_until=now + 3600)
    capacity = (
        _planner.Capacity("claude", "login", limited, slot="s"),
        _planner.Capacity("codex", "codex-login", slot="s"),
    )
    item = _planner.ManualRequest("m", "team/repo", files=("a",))
    facts = _planner.Facts(manual_requests=(item,), capacity=capacity, pins={"m": "claude"}, now=now)
    (launch,) = _planner.plan(facts).launches
    assert (launch.engine, launch.login) == ("claude", "login")
    assert "exhausted" in launch.reason
    # Without a pin the router's choice stands.
    (unpinned,) = _planner.plan(replace(facts, pins={})).launches
    assert unpinned.engine == "codex"


async def test_a_pinned_resume_on_the_other_engine_delivers_the_briefing_as_its_first_turn(
    monkeypatch, tmp_path
):
    """A pinned resume on the other engine delivers the briefing as its first turn."""
    from tests.test_codex_author import project as project_fixture

    rig = project_fixture.__wrapped__(tmp_path, monkeypatch)  # type: ignore[attr-defined]  # pytest keeps the raw fixture here
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fresh-token")  # both engines: an AuthorSession
    rig.seen["resumed"] = True
    owner = {"engine": "claude", "thread": "claude-session-1"}
    (rig.state_root / "author-conversation.json").write_text(json.dumps(owner))
    rig.pump_that()
    await agent.project.func("brief", browser=False, engine="codex")
    assert rig.seen["started"] == []
    _, lines = rig.thread()
    first_turn = "\n".join(lines)
    assert first_turn.startswith("Resume the pending work item.")
    assert "AUTHOR HANDOFF: claude -> codex (resumed on codex)" in first_turn
    assert "thread claude-session-1" in first_turn
    saved = json.loads((rig.state_root / "author-conversation.json").read_text())
    assert saved["engine"] == "codex" and saved["previous"] == {
        "engine": "claude",
        "thread": "claude-session-1",
    }
