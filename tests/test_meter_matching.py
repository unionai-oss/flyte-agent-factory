"""Admission keeps old slot reports useful without crossing credential boundaries."""

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from flyte import remote

from flyte_agent_factory import _factory_base, _factory_meters, _meter, _planner_facts

NOW = 1_800_000_000


@pytest.mark.parametrize("admission", ["legacy", "planner"])
def test_admission_lists_each_slot_once_and_shares_busy_token_refusal(monkeypatch, admission):
    secrets = replace(_factory_base.PROFILE.secrets, claude_token="shared_token", codex_token="codex_tok_x")
    profile = replace(_factory_base.PROFILE, secrets=secrets)
    first, second = profile.slots[:2]
    codex = f"codex {profile.secrets.codex_token}"
    reports = {
        # A slot may also run on the shared Codex token (QA finding 4); it refused too here, so the
        # token's shared refusal is what closes the free slot.
        first.task: [
            _meter.Meter("claude shared_token", "cli-headers", NOW, limited_until=NOW + 600),
            _meter.Meter(codex, "app-server", NOW, limited_until=NOW + 600),
        ],
        second.task: [_meter.Meter("claude shared_token", "cli-headers", NOW + 1)],
    }
    calls: Counter[str] = Counter()

    def listall(*, task_name, **kwargs):
        calls[task_name] += 1
        if task_name not in profile.tasks:
            return []
        return [
            SimpleNamespace(
                task_name=task_name,
                to_dict=lambda: {"action": {"metadata": {"task": {"id": {"name": task_name}}}}},
                get_report=lambda: "".join(_meter.report_comment(m) for m in reports.get(task_name, [])),
            )
        ]

    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    monkeypatch.setattr(remote.Run, "listall", listall)
    monkeypatch.setattr(_factory_base, "cap", lambda: len(profile.slots))
    if admission == "legacy":
        headroom = _factory_meters.slot_headroom([second.task], now=NOW)
        # This was `== 0`; a slot every engine of which refused is now `LIMITED` (0.0 is a full meter).
        assert headroom[second.task] == _factory_meters.LIMITED
        assert calls["factory.read_login_meter"] == 1
    else:
        live: Any = [SimpleNamespace(task_name=first.task)]  # fake runs: only `task_name` is read
        monkeypatch.setattr(_factory_base, "_task_name", lambda run: run.task_name)
        capacity = _planner_facts.free_capacity(profile, live, _factory_meters.slot_meters(profile.tasks))
        claude = [c for c in capacity if c.engine == "claude"]
        assert claude and all(c.meter is not None and c.meter.limited_until == NOW + 600 for c in claude)
    assert {slot: calls[slot] for slot in profile.tasks} == dict.fromkeys(profile.tasks, 1)


@pytest.mark.parametrize("account", ["claude", "claude retired_login"])
def test_token_admission_ignores_legacy_oauth_accounts(monkeypatch, account):
    profile = replace(
        _factory_base.PROFILE, secrets=replace(_factory_base.PROFILE.secrets, claude_token="shared_token")
    )
    first = profile.slots[0]
    reports = {first.task: [_meter.Meter(account, "oauth-usage", NOW, limited_until=NOW + 600)]}
    monkeypatch.setattr(_factory_base, "PROFILE", profile)
    monkeypatch.setattr(_factory_base, "SLOTS", profile.tasks)
    monkeypatch.setattr(_factory_base, "cap", lambda: len(profile.slots))
    monkeypatch.setattr(_factory_meters, "slot_meters", lambda slots: reports)
    monkeypatch.setattr(remote.Run, "listall", lambda **kwargs: [])
    assert _factory_meters.slot_headroom([first.task], now=NOW) == {first.task: None}
    assert all(
        c.meter is None for c in _planner_facts.free_capacity(profile, [], reports) if c.engine == "claude"
    )
