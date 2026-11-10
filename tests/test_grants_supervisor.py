"""Credential grants in the project supervisor: `credential-grant request`, the owner's inbox, the notices."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from flyte_agent_factory import (
    _bridge,
    _grant_seal,
    _grants,
    _grants_cli,
    _grants_supervisor,
    _inbox,
    _session_config,
    _supervise_support,
    _timeline,
)
from flyte_agent_factory._delegation import snapshot
from flyte_agent_factory._grants import TOKEN_ENV, URL_ENV, GrantStore
from tests._wait import wait_for
from tests.test_bridge import _session_transcript
from tests.test_grants import KEY_ID, SECRET, TOKEN, exports

BASE = "https://mcp.example"


class Owner:
    """Everything the owner's side of the fake Union sees: the questions opened, and a way to answer them."""

    def __init__(self) -> None:
        self.opened: list[SimpleNamespace] = []

    def answer(self, index: int, value: str) -> None:
        self.opened[index].future.set_result(value)

    def link(self, index: int = 0) -> str:
        found = re.search(r"https://\S+?(?=['\s])", self.opened[index].prompt)
        assert found, self.opened[index].prompt
        return found.group(0)


@pytest.fixture
async def setup(tmp_path, monkeypatch, bridge_root):
    """The grant side of a supervisor: a fake `open_condition`, isolated bridge, workspace and state dirs."""
    workspace = tmp_path / "ws"
    (workspace / _session_config._INBOX_DIR).mkdir(parents=True)
    monkeypatch.setattr(_session_config, "WORKSPACE", workspace)
    monkeypatch.setattr(_session_config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(_session_config, "CONFIG_DIR", tmp_path / "claude")
    monkeypatch.setattr(_inbox, "_directions", [])
    monkeypatch.setattr(_inbox, "_seen_human_entries", set())
    monkeypatch.setattr(_grants_cli, "_POLL_INTERVAL_S", 0.02)
    monkeypatch.setattr(_grants_supervisor, "read_page_base_url", lambda *args: BASE)
    owner = Owner()

    async def open_condition(base, seq, **kwargs):
        opened = SimpleNamespace(
            name=f"{base}-a0-{seq:03d}",
            prompt=kwargs["prompt"],
            kwargs=kwargs,
            future=asyncio.get_running_loop().create_future(),
        )
        opened.wait = SimpleNamespace(aio=lambda: opened.future)
        owner.opened.append(opened)
        return opened, seq

    monkeypatch.setattr(_grants_supervisor, "open_condition", open_condition)
    timeline = _timeline._Timeline()
    bridge = _bridge.Bridge(bridge_root, on_event=timeline.row)
    credentials, session_env = _grants_supervisor.start_credentials("proj-a", timeline, bridge)
    yield SimpleNamespace(
        credentials=credentials,
        session_env=session_env,
        store=credentials.store,
        bridge=bridge,
        timeline=timeline,
        owner=owner,
        root=tmp_path,
        workspace=workspace,
    )
    credentials.close()


def seal(store: GrantStore, name: str = "aws-marketplace", project: str | None = None, **kwargs: Any) -> str:
    now = time.time()
    payload = {
        "name": name,
        "kind": "aws",
        "expires_at": int(now + 3600),
        "sealed_at": int(now),
        "value": exports(),
        "region": "",
        **kwargs,
    }
    return _grant_seal.seal(store.key.public_b64, project or store.project, payload)


def everything_written(root: Path) -> str:
    """Every file under `root`, as text: nothing may hold a credential or its sealed form.

    Not the fake Claude transcript (`claude/`): it is the owner's own typing, which the supervisor does not
    write.
    """
    return "\n".join(
        p.read_text(errors="replace")
        for p in sorted(root.rglob("*"))
        if p.is_file() and "claude" not in p.relative_to(root).parts
    )


def assert_clean(setup, *blobs: str) -> None:
    text = everything_written(setup.root) + "\n".join(
        f"{icon} {row}" for _, icon, row in setup.timeline._rows
    )
    text += json.dumps(setup.timeline.snapshot()) + json.dumps(list(setup.bridge.ask_history))
    for forbidden in (SECRET, TOKEN, *blobs):
        assert forbidden not in text


async def run_cli(setup, *argv: str) -> tuple[int, str, str]:
    """`credential-grant request ...` in a thread, with the supervisor's request service scanning."""
    done = asyncio.create_task(asyncio.to_thread(_grants_cli.main, ["request", *argv]))
    service = asyncio.create_task(_scan_until(setup, done))
    code = await done
    await service
    return code, "", ""


async def _scan_until(setup, done: asyncio.Future[Any]) -> None:
    while not done.done():
        setup.credentials.requests.scan()
        await asyncio.sleep(0.02)
    await asyncio.gather(*setup.credentials.requests._pending)


async def ask(setup, name="aws-marketplace", hours="2", why="install the offering", timeout="30"):
    """Start a request and wait until the owner's question is open."""
    task = asyncio.create_task(run_cli(setup, name, "--hours", hours, "--why", why, "--timeout", timeout))
    await wait_for(lambda: setup.owner.opened)
    return task


async def test_a_request_asks_the_owner_with_a_link_that_names_this_project_key_and_hours(setup):
    task = await ask(setup)
    [question] = setup.owner.opened
    assert question.name == "ask-a0-001"
    assert question.kwargs["data_type"] is str and question.kwargs["timeout"] > 30
    link = _grant_seal.parse_link(setup.owner.link())
    assert (link.project, link.name, link.hours) == ("proj-a", "aws-marketplace", 2)
    assert (link.public_b64, link.kid) == (setup.store.key.public_b64, setup.store.key.kid)
    prompt = question.prompt
    assert "proj-a" in prompt and "aws-marketplace" in prompt and "2 hours" in prompt
    assert "install the offering" in prompt and "`no`" in prompt and "No credential is given unless" in prompt
    assert f"python -m flyte_agent_factory._grants seal --link '{setup.owner.link()}'" in prompt
    setup.owner.answer(0, "no")
    await task


async def test_a_sealed_answer_grants_it_and_the_cli_exits_0(setup, capsys):
    task = await ask(setup)
    blob = seal(setup.store)
    setup.owner.answer(0, blob)
    code, _, _ = await task
    assert code == 0
    assert "granted until" in capsys.readouterr().out
    [grant] = setup.store.grants()
    assert (grant.name, grant.state) == ("aws-marketplace", "active")
    rows = [text for _, icon, text in setup.timeline._rows]
    assert "credential aws-marketplace: sealed answer received" in rows
    answers = list((setup.bridge.root / _bridge.ANSWERS).glob("grant-*.json"))
    assert len(answers) == 1 and json.loads(answers[0].read_text())["message"].startswith("granted until")
    assert_clean(setup, blob)


@pytest.mark.parametrize("wrap", ["`{}`", "here it is: {}", "{}\n", "> {}"])
async def test_a_quoted_or_wrapped_sealed_answer_is_still_taken(setup, capsys, wrap):
    task = await ask(setup)
    blob = seal(setup.store)
    setup.owner.answer(0, wrap.format(blob))
    code, _, _ = await task
    assert code == 0 and "granted until" in capsys.readouterr().out
    assert_clean(setup, blob)


async def test_a_line_wrapped_sealed_answer_is_taken(setup, capsys):
    task = await ask(setup)
    blob = seal(setup.store)
    setup.owner.answer(0, "\n".join(blob[i : i + 76] for i in range(0, len(blob), 76)))
    code, _, _ = await task
    assert code == 0
    assert_clean(setup, blob)


async def test_no_refuses_and_says_so(setup, capsys):
    task = await ask(setup)
    setup.owner.answer(0, "no")
    code, _, _ = await task
    assert code == 1 and setup.store.grants() == []
    assert "refused by the owner" in capsys.readouterr().err


async def test_a_blob_for_another_name_is_refused_with_the_stores_reason(setup, capsys):
    task = await ask(setup)
    blob = seal(setup.store, name="other-thing")
    setup.owner.answer(0, blob)
    code, _, _ = await task
    assert code == 1 and setup.store.grants() == []
    err = capsys.readouterr().err
    assert "refused: this sealed credential is for 'other-thing', but credential 'aws-marketplace'" in err
    assert any("sealed answer refused" in text for _, _, text in setup.timeline._rows)
    assert_clean(setup, blob)


async def test_a_blob_sealed_to_another_projects_key_is_refused(setup, capsys):
    task = await ask(setup)
    other = GrantStore("proj-b")
    blob = seal(other, project="proj-b")
    setup.owner.answer(0, blob)
    code, _, _ = await task
    assert code == 1 and setup.store.grants() == []
    assert "made for another key" in capsys.readouterr().err
    assert_clean(setup, blob)


async def test_a_raw_credential_pasted_as_the_answer_is_refused_and_not_echoed(setup, capsys):
    task = await ask(setup)
    setup.owner.answer(0, exports())
    code, _, _ = await task
    captured = capsys.readouterr()
    assert code == 1 and setup.store.grants() == []
    assert "raw credential" in captured.err and SECRET not in captured.err + captured.out
    assert_clean(setup)


@pytest.mark.parametrize(
    "answer",
    [
        f"{KEY_ID}\n{SECRET}\n{TOKEN}",  # the three values copied without their names
        f"here you go: {SECRET}",
        f"{KEY_ID} {SECRET}",
    ],
)
async def test_bare_credential_values_pasted_as_the_answer_are_refused_and_not_echoed(setup, capsys, answer):
    task = await ask(setup)
    setup.owner.answer(0, answer)
    code, _, _ = await task
    captured = capsys.readouterr()
    assert code == 1 and setup.store.grants() == []
    assert "raw credential" in captured.err
    assert SECRET not in captured.err + captured.out and TOKEN not in captured.err + captured.out
    assert any("raw credential" in text for _, _, text in setup.timeline._rows)
    assert_clean(setup)


async def test_free_text_answers_are_refused_without_echoing_any_of_it(setup, capsys):
    task = await ask(setup)
    setup.owner.answer(0, "not now, token=" + "ghp_" + "a" * 36)
    code, _, _ = await task
    err = capsys.readouterr().err
    assert code == 1 and "refused by the owner" in err and "not now" not in err and "ghp_" not in err


# Secret shapes the raw-credential heuristic does not recognise: one class of letters, or split by whitespace.
UNKNOWN_SHAPES = [
    "Q" * 20 + "W" * 20,
    "q" * 20 + "w" * 20,
    "ABCDEFGHIJ" * 4,
    "abcdefghij" * 4,
    ("QWERTYUIOP" * 2) + " " + ("ASDFGHJKLZ" * 2),
    ("qwertyuiop" * 2) + "\n" + ("asdfghjklz" * 2),
    "\t".join(["zxcvbnmasd"] * 4),
]


@pytest.mark.parametrize("secret", UNKNOWN_SHAPES)
async def test_unrecognised_secret_shapes_as_the_answer_are_never_echoed(setup, capsys, secret):
    task = await ask(setup)
    setup.owner.answer(0, secret)
    code, _, _ = await task
    captured = capsys.readouterr()
    assert code == 1 and setup.store.grants() == []
    answers = list((setup.bridge.root / _bridge.ANSWERS).glob("grant-*.json"))
    assert len(answers) == 1
    written = answers[0].read_text()
    assert "refused by the owner" in json.loads(written)["message"]
    shown = captured.err + captured.out + written + repr(setup.timeline._rows)
    for piece in secret.split():
        assert piece[:12] not in shown
    assert_clean(setup)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["Bad_Name", "--hours", "1"], "not a valid credential name"),
        (["aws-marketplace", "--hours", "0"], "--hours must be more than 0"),
        (["aws-marketplace", "--hours", "169"], "at most 168"),
    ],
)
async def test_a_bad_request_is_refused_without_asking(setup, capsys, argv, message):
    task = asyncio.create_task(run_cli(setup, *argv, "--timeout", "30"))
    code, _, _ = await task
    assert code == 1 and setup.owner.opened == []
    assert message in capsys.readouterr().err


async def test_nobody_answering_closes_the_question_and_grants_nothing(setup, monkeypatch, capsys):
    # As in production the asker gives up first and the question is closed a little later.
    monkeypatch.setattr(_grants_supervisor, "ASK_GRACE_S", 0.5)
    monkeypatch.setattr(_grants_supervisor, "_CLOSE_MARGIN_S", 0.0)
    closed: list[str] = []

    async def close(name, approve, reason, *args, **kwargs):
        closed.append(name)
        return True

    monkeypatch.setattr(setup.bridge, "_close", close)
    task = await ask(setup, timeout="0.3")
    code, _, _ = await task
    assert code == 3 and closed == ["ask-a0-001"] and setup.store.grants() == []
    assert "nobody answered in time; no credential" in capsys.readouterr().err


async def test_the_question_shares_the_bridges_counter(setup):
    first = setup.bridge.next_seq()
    task = await ask(setup)
    assert setup.owner.opened[0].name == f"ask-a0-{first + 1:03d}"
    assert setup.bridge.next_seq() == first + 2
    setup.owner.answer(0, "no")
    await task


async def test_without_a_page_url_the_prompt_says_so_and_the_fallback_still_works(setup, monkeypatch):
    monkeypatch.setattr(_grants_supervisor, "read_page_base_url", lambda *args: "")
    task = await ask(setup)
    prompt = setup.owner.opened[0].prompt
    assert "seal page is unavailable" in prompt
    link = _grant_seal.parse_link(setup.owner.link())
    assert link.public_b64 == setup.store.key.public_b64
    setup.owner.answer(0, "no")
    await task


# --- the owner's inbox ---------------------------------------------------------------------------------


async def run_inbox(setup, messages: list[str]) -> SimpleNamespace:
    """The real `_inbox_loop` fed `messages` (then `/stop`), with a fake session."""
    feed = iter([*messages, "/stop"])

    async def direct(base, seq, **kwargs):
        assert base == "direct"
        return SimpleNamespace(wait=SimpleNamespace(aio=lambda: asyncio.sleep(0, next(feed)))), seq

    proc = SimpleNamespace(submitted=[])

    async def submit(text, **kwargs):
        proc.submitted.append(text)

    proc.submit = submit
    import flyte_agent_factory._inbox as inbox

    with mock.patch.object(inbox, "open_condition", direct):
        await inbox._inbox_loop(
            cast(Any, proc),
            setup.timeline,
            asyncio.Event(),
            _supervise_support._grant_round,
            setup.credentials,
        )
    return proc


async def test_a_sealed_message_in_the_direct_inbox_grants_and_goes_nowhere_else(setup):
    blob = seal(setup.store)
    proc = await run_inbox(setup, [blob])
    [grant] = setup.store.grants()
    assert grant.state == "active"
    assert proc.submitted == [] and _inbox._directions == []
    assert list((setup.workspace / _session_config._INBOX_DIR).glob("*.md")) == []
    assert any("granted until" in text for _, _, text in setup.timeline._rows)
    assert not any("📨" == icon for _, icon, _ in setup.timeline._rows)
    assert_clean(setup, blob)


async def test_a_refused_sealed_message_says_so_on_the_timeline_only(setup):
    blob = seal(GrantStore("proj-b"), project="proj-b")
    proc = await run_inbox(setup, [f"  {blob}\n"])
    assert setup.store.grants() == [] and proc.submitted == [] and _inbox._directions == []
    assert any(text.startswith("sealed credential refused:") for _, _, text in setup.timeline._rows)
    assert_clean(setup, blob)


async def test_revoke_credential_names_one_or_all_and_is_not_direction(setup):
    setup.store.accept(seal(setup.store, "one"))
    setup.store.accept(seal(setup.store, "two"))
    proc = await run_inbox(setup, ["/revoke-credential one"])
    assert {g.name: g.state for g in setup.store.grants()} == {"one": "revoked", "two": "active"}
    proc = await run_inbox(setup, ["/revoke-credential"])
    assert {g.state for g in setup.store.grants()} == {"revoked"}
    assert proc.submitted == [] and _inbox._directions == []
    assert [g.reason for g in setup.store.grants()] == ["revoked by the owner"] * 2


async def test_a_re_encoded_sealed_text_cannot_undo_a_revoke(setup):
    blob = seal(setup.store)
    setup.credentials.handle(blob)
    setup.credentials.handle("/revoke-credential")
    env = json.loads(_grant_seal.decode_b64url(blob[5:]))
    again = "fgc1." + _grant_seal.encode_b64url(json.dumps(env, indent=1).encode())
    assert setup.credentials.handle(again) is True
    assert [g.state for g in setup.store.grants()] == ["revoked"] and setup.store.live_values() == ()
    assert any("already been used" in text for _, _, text in setup.timeline._rows)


async def test_revoke_credential_takes_the_first_word_as_the_name(setup):
    setup.store.accept(seal(setup.store, "one"))
    await run_inbox(setup, ["/revoke-credential one please, now"])
    assert [g.state for g in setup.store.grants()] == ["revoked"]


async def test_sealed_text_inside_a_longer_message_is_taken_and_not_forwarded(setup):
    blob = seal(setup.store)
    proc = await run_inbox(setup, [f"here it is:\n{blob}\nthanks"])
    assert [g.state for g in setup.store.grants()] == ["active"]
    assert proc.submitted == [] and _inbox._directions == []
    assert_clean(setup, blob)


async def test_a_line_wrapped_sealed_message_in_the_inbox_grants(setup):
    blob = seal(setup.store)
    wrapped = "\n".join(blob[i : i + 76] for i in range(0, len(blob), 76))
    proc = await run_inbox(setup, [f"here:\n{wrapped}\nthanks"])
    assert [g.state for g in setup.store.grants()] == ["active"] and proc.submitted == []


async def test_a_raw_credential_sent_to_the_inbox_is_not_forwarded(setup):
    proc = await run_inbox(setup, [exports()])
    assert setup.store.grants() == [] and proc.submitted == [] and _inbox._directions == []
    assert any("raw credential" in text for _, _, text in setup.timeline._rows)
    assert_clean(setup, SECRET)


async def test_bare_credential_values_sent_to_the_inbox_are_not_forwarded(setup):
    proc = await run_inbox(setup, [f"{KEY_ID}\n{SECRET}\n{TOKEN}"])
    assert setup.store.grants() == [] and proc.submitted == [] and _inbox._directions == []
    assert any("raw credential" in text for _, _, text in setup.timeline._rows)
    assert_clean(setup)


async def test_a_message_naming_only_a_key_id_is_delivered_as_direction(setup):
    proc = await run_inbox(setup, [f"CloudTrail shows {KEY_ID} was used at 10:02"])
    assert len(proc.submitted) == 1 and not any("raw credential" in t for _, _, t in setup.timeline._rows)


async def test_the_loopback_token_is_redacted_from_published_text(setup):
    token = setup.credentials.session_env()[_grants.TOKEN_ENV]
    assert token not in _bridge.redact_tokens(f"env shows {token}")


async def test_revoking_what_is_not_held_is_said(setup):
    await run_inbox(setup, ["/revoke-credential nothing-here"])
    assert any("no active credential nothing-here" in text for _, _, text in setup.timeline._rows)


async def test_ordinary_direction_still_flows(setup):
    proc = await run_inbox(setup, ["please use port 9090"])
    assert proc.submitted and any(d.endswith("please use port 9090") for d in _inbox._directions)


def typed_in_the_app(setup, *texts: str) -> None:
    stamp = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    entries = [
        {
            "type": "user",
            "uuid": f"u{i}",
            "timestamp": stamp,
            "origin": {"kind": "human"},
            "entrypoint": "cli",
            "message": {"role": "user", "content": text},
        }
        for i, text in enumerate(texts)
    ]
    _session_transcript(setup.root / "claude", setup.workspace, entries)


async def test_sealed_and_revoke_text_typed_into_the_claude_app_is_taken_the_same_way(setup):
    blob = seal(setup.store)
    typed_in_the_app(
        setup, blob, "/revoke-credential aws-marketplace", exports(), "and then carry on with the work"
    )
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(
            cast(Any, SimpleNamespace(sent_texts=[])), setup.timeline, stop, setup.credentials
        )
    )
    await wait_for(lambda: any("carry on" in d for d in _inbox._directions))
    stop.set()
    await watcher
    [grant] = setup.store.grants()
    assert grant.state == "revoked"
    assert [d for d in _inbox._directions if "fgc1" in d or "revoke" in d] == []
    [row] = [text for _, _, text in setup.timeline._rows if "raw credential" in text]
    assert "sent to the Claude app" in row and "the session has already seen it" in row
    assert_clean(setup, blob)


async def test_a_sealed_text_typed_in_the_app_does_not_close_open_questions(setup):
    asked = SimpleNamespace(closed=[])

    async def close_answered_in_session(message, source, at):
        asked.closed.append(message)

    setup.timeline.bridge = cast(
        Any, SimpleNamespace(questions={"r": {}}, close_answered_in_session=close_answered_in_session)
    )
    typed_in_the_app(setup, seal(setup.store))
    stop = asyncio.Event()
    watcher = asyncio.create_task(
        _inbox._watch_app_messages(
            cast(Any, SimpleNamespace(sent_texts=[])), setup.timeline, stop, setup.credentials
        )
    )
    await wait_for(lambda: setup.store.grants())
    stop.set()
    await watcher
    assert asked.closed == []


# --- notices to the session ----------------------------------------------------------------------------


async def test_the_session_is_told_of_a_grant_and_of_its_end_without_a_value(setup):
    session: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    sent: list[str] = []

    async def submit(text, **kwargs):
        sent.append(text)

    session.set_result(SimpleNamespace(submit=submit))
    deliver = asyncio.create_task(setup.credentials._deliver(session))
    setup.store.accept(seal(setup.store))
    await wait_for(lambda: sent)
    setup.store.revoke("aws-marketplace")
    await wait_for(lambda: len(sent) == 2)
    deliver.cancel()
    assert sent[0].startswith("Credential notice: Credential aws-marketplace (aws) is granted until")
    assert "has ended: revoked by the owner" in sent[1]
    assert not list((setup.workspace / _session_config._INBOX_DIR).glob("credential-*.notice"))
    assert_clean(setup)


async def test_a_notice_written_before_a_crash_is_delivered_by_the_next_attempt(setup):
    setup.credentials.close()  # the first attempt
    (setup.workspace / _session_config._INBOX_DIR / "credential-1-x.notice").write_text(
        "Credential x ended\n"
    )
    again = _grants_supervisor.Credentials("proj-a", setup.timeline, setup.bridge)
    again.start()
    sent: list[str] = []

    async def submit(text, **kwargs):
        sent.append(text)

    session: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    session.set_result(SimpleNamespace(submit=submit))
    deliver = asyncio.create_task(again._deliver(session))
    await wait_for(lambda: sent)
    deliver.cancel()
    again.close()
    assert sent == ["Credential notice: Credential x ended"]


async def test_the_delegate_snapshot_carries_no_grant_value(setup):
    """A delegate worker is another pod: what the workspace holds must be metadata."""
    repo = setup.workspace
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    blob = seal(setup.store)
    await run_inbox(setup, [blob])
    setup.store.revoke()
    (repo / "tracked").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    target = setup.root / "snap"
    target.mkdir()
    snapshot(repo, target)
    with tarfile.open(target / "repo.tar.gz") as archive:
        archive.extractall(setup.root / "unpacked", filter="data")
    text = everything_written(setup.root / "unpacked")
    assert blob not in text and SECRET not in text and TOKEN not in text


# --- the whole path, request to expiry ------------------------------------------------------------------

_READ_GRANT = (
    "import json, os, urllib.request; "
    "request = urllib.request.Request("
    "os.environ['AWS_CONTAINER_CREDENTIALS_FULL_URI'], "
    "headers={'Authorization': os.environ['AWS_CONTAINER_AUTHORIZATION_TOKEN']}); "
    "got = json.load(urllib.request.urlopen(request, timeout=10)); "
    "print(got['SecretAccessKey'] == os.environ['EXPECTED_SECRET'], "
    "got['Token'] == os.environ['EXPECTED_TOKEN'])"
)


def _exec_in_subprocess(setup, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    src = str(Path(_grants_cli.__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._grants_cli", "exec", "aws-marketplace", "--"]
        + [sys.executable, "-c", _READ_GRANT],
        env={**os.environ, **setup.credentials.session_env(), "PYTHONPATH": src, **extra_env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def test_request_to_expiry_end_to_end(setup, capsys, tmp_path):
    now = [time.time()]
    setup.store._clock = lambda: now[0]
    task = await ask(setup)
    link = _grant_seal.parse_link(setup.owner.link())
    assert link.public_b64 == setup.store.key.public_b64
    blob = _grant_seal.seal(
        link.public_b64,
        link.project,
        {
            "name": link.name,
            "kind": "aws",
            "expires_at": int(now[0] + 3600),
            "sealed_at": int(now[0]),
            "value": exports(),
            "region": "",
        },
    )
    setup.owner.answer(0, blob)
    code, _, _ = await task
    assert code == 0 and "granted until" in capsys.readouterr().out

    # `exec` hands a command the endpoint; it serves the value (compared in the child, never printed).
    env = {"EXPECTED_SECRET": SECRET, "EXPECTED_TOKEN": TOKEN}
    done = await asyncio.to_thread(_exec_in_subprocess, setup, env)
    assert (done.returncode, done.stdout.strip()) == (0, "True True"), done.stderr
    assert SECRET not in done.stdout + done.stderr and TOKEN not in done.stdout + done.stderr

    # Past the expiry the endpoint refuses and `exec` will not start the command.
    now[0] += 3601
    refused = await asyncio.to_thread(_exec_in_subprocess, setup, env)
    assert refused.returncode != 0 and "True" not in refused.stdout
    assert SECRET not in refused.stdout + refused.stderr and TOKEN not in refused.stdout + refused.stderr
    assert setup.store.live_values() == ()
    [grant] = setup.store.grants()
    assert grant.state == "expired"
    assert_clean(setup, blob)

    # Another project's store (another key) refuses the same sealed text pasted into its inbox.
    other_timeline = _timeline._Timeline()
    other_bridge = _bridge.Bridge(tmp_path / "other-bridge", on_event=other_timeline.row)
    other = _grants_supervisor.Credentials("proj-b", other_timeline, other_bridge, state_root=tmp_path / "b")
    other.start()
    try:
        assert other.handle(blob)
        assert other.store.grants() == []
        assert any(text.startswith("sealed credential refused:") for _, _, text in other_timeline._rows)
    finally:
        other.close()
    assert_clean(setup, blob)


async def test_start_credentials_returns_the_running_server_env_without_touching_os_environ(setup):
    assert setup.session_env == setup.credentials.server.env()
    assert setup.session_env[URL_ENV] == setup.credentials.server.url
    assert URL_ENV not in os.environ and TOKEN_ENV not in os.environ
