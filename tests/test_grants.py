"""The credential grant store, its loopback server and the redaction of live values."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _bridge, _grant_seal
from flyte_agent_factory._grant_seal import GrantError
from flyte_agent_factory._grants import (
    MAX_GRANT_S,
    TOKEN_ENV,
    URL_ENV,
    WARN_BEFORE_S,
    GrantServer,
    GrantStore,
    format_utc,
    parse_aws,
)
from flyte_agent_factory._grants_cli import build_exec_env

NOW = 1_800_000_000.0
KEY_ID = "ASIA" + "ABCDEFGH12345678"
SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEYZ"[:40]
TOKEN = "IQoJb3JpZ2luX2VjEXAMPLE" + "x" * 120 + "/+tail=="
SECRETS = (SECRET, TOKEN)  # the access key id is an identifier, not a secret (but ASIA... is still redacted)
EXPIRY = format_utc(NOW + 3600)


def exports(key_id: str = KEY_ID, secret: str = SECRET, token: str | None = TOKEN) -> str:
    lines = [f'export AWS_ACCESS_KEY_ID="{key_id}"', f'export AWS_SECRET_ACCESS_KEY="{secret}"']
    if token is not None:
        lines.append(f'export AWS_SESSION_TOKEN="{token}"')
    return "\n".join(lines)


def assume_role_json(expiration: str) -> str:
    return json.dumps(
        {
            "Credentials": {
                "AccessKeyId": KEY_ID,
                "SecretAccessKey": SECRET,
                "SessionToken": TOKEN,
                "Expiration": expiration,
            }
        }
    )


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.notes: list[str] = []


def make_store(project: str = "proj-a", clock: Clock | None = None, **kwargs: Any) -> tuple[GrantStore, Sink]:
    sink = Sink()
    store = GrantStore(
        project,
        clock=clock or Clock(),
        on_event=lambda icon, text: sink.events.append((icon, text)),
        notify=sink.notes.append,
        **kwargs,
    )
    return store, sink


def seal_for(
    store: GrantStore,
    *,
    name: str = "aws-marketplace",
    value: str | None = None,
    expires_in: int = 3600,
    sealed_at: float = NOW,
    region: str = "",
    project: str | None = None,
) -> str:
    payload = {
        "name": name,
        "kind": "aws",
        "expires_at": int(NOW + expires_in),
        "sealed_at": int(sealed_at),
        "value": exports() if value is None else value,
        "region": region,
    }
    return _grant_seal.seal(store.key.public_b64, project or store.project, payload)


def assert_no_secret(text: str) -> None:
    for part in SECRETS:
        assert part not in text


# --- parse_aws -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        exports(),
        "# from the portal\n\n  " + exports().replace("\n", "\n  ") + "\n",
        exports().replace('"', ""),
        exports().replace("export ", "SET ").replace('"', ""),
        exports().replace("export ", "$Env:"),
        exports().replace("export AWS_SESSION_TOKEN", "export AWS_SECURITY_TOKEN"),
        f"[work]\naws_access_key_id = {KEY_ID}\naws_secret_access_key = {SECRET}\n"
        f"aws_session_token = {TOKEN}\n",
        assume_role_json(EXPIRY),
        json.dumps({"Version": 1, "AccessKeyId": KEY_ID, "SecretAccessKey": SECRET, "SessionToken": TOKEN}),
    ],
)
def test_every_accepted_format_parses(text: str) -> None:
    credential = parse_aws(text)
    assert (credential.access_key_id, credential.secret_access_key, credential.session_token) == (
        KEY_ID,
        SECRET,
        TOKEN,
    )


def test_expiration_with_z_or_offset() -> None:
    assert (
        parse_aws(assume_role_json("2026-11-06T18:00:00Z")).expiration
        == parse_aws(assume_role_json("2026-11-06T18:00:00+00:00")).expiration
    )
    assert parse_aws(exports()).expiration is None


def test_credential_repr_hides_values() -> None:
    assert_no_secret(repr(parse_aws(exports())))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nothing here", "no AWS access key id"),
        (f'export AWS_ACCESS_KEY_ID="{KEY_ID}"', "no AWS access key id"),
        (exports("AKIA" + "ABCDEFGH12345678"), "long-lived"),
        (exports(token=None), "long-lived"),
        (exports("ASIA123"), "not a temporary one"),
        (exports(secret="short"), "40 characters"),
        (exports(secret="!" * 40), "40 characters"),
    ],
)
def test_refusals_never_quote_a_value(text: str, message: str) -> None:
    with pytest.raises(GrantError, match=message) as err:
        parse_aws(text)
    assert_no_secret(str(err.value))


def test_long_lived_refusal_says_what_to_give_instead() -> None:
    with pytest.raises(GrantError) as err:
        parse_aws(exports(token=None))
    for hint in (
        "IAM Identity Center",
        "aws sts assume-role",
        "get-session-token",
        "docs/credential-grants.md",
    ):
        assert hint in str(err.value)


def test_expired_credential_is_refused_when_a_clock_is_given() -> None:
    text = assume_role_json("2020-01-01T00:00:00Z")
    parse_aws(text)  # no clock: not checked
    with pytest.raises(GrantError, match="expired") as err:
        parse_aws(text, NOW)
    assert_no_secret(str(err.value))


# --- the store -------------------------------------------------------------------------------------------


def test_accept_then_aws_response() -> None:
    store, sink = make_store()
    info = store.accept(seal_for(store, region="eu-west-1"), expected_name="aws-marketplace")
    assert (info.name, info.state, info.expires_at, info.region) == (
        "aws-marketplace",
        "active",
        NOW + 3600,
        "eu-west-1",
    )
    assert store.aws_response("aws-marketplace") == {
        "AccessKeyId": KEY_ID,
        "SecretAccessKey": SECRET,
        "Token": TOKEN,
        "Expiration": format_utc(NOW + 3600),
    }
    assert sink.events == [("🔑", f"credential aws-marketplace (aws) granted until {format_utc(NOW + 3600)}")]
    assert "credential-grant exec aws-marketplace -- aws sts get-caller-identity" in sink.notes[0]
    assert_no_secret(sink.notes[0] + sink.events[0][1])


def test_expected_name_mismatch_is_refused() -> None:
    store, _ = make_store()
    with pytest.raises(GrantError, match="aws-marketplace"):
        store.accept(seal_for(store), expected_name="other")
    assert store.grants() == []


def test_a_blob_for_another_project_is_refused() -> None:
    a, _ = make_store("proj-a")
    with pytest.raises(GrantError):
        a.accept(seal_for(a, project="proj-b"))


def test_refresh_replaces_and_wipes_the_old_one() -> None:
    store, _ = make_store()
    store.accept(seal_for(store))
    old = store._grants["aws-marketplace"]
    old_secret = old.secret
    store.accept(seal_for(store, expires_in=7200))
    assert [g.expires_at for g in store.grants()] == [NOW + 7200]
    assert bytes(old_secret) == b"" and not old.token


def test_expiry_is_the_earliest_of_the_owner_the_credential_and_seven_days() -> None:
    store, _ = make_store()
    sealed = seal_for(store, value=assume_role_json(format_utc(NOW + 600)), expires_in=3600)
    assert store.accept(sealed).expires_at == NOW + 600
    sealed = seal_for(store, expires_in=30 * 24 * 3600)
    assert store.accept(sealed).expires_at == NOW + MAX_GRANT_S


def test_an_expiry_not_in_the_future_is_refused() -> None:
    store, _ = make_store()
    with pytest.raises(GrantError, match="future"):
        store.accept(seal_for(store, expires_in=-5))


@pytest.mark.parametrize("sealed_at", [NOW - 24 * 3600 - 1, NOW + 601])
def test_stale_or_forged_sealed_at_is_refused(sealed_at: float) -> None:
    store, _ = make_store()
    with pytest.raises(GrantError):
        store.accept(seal_for(store, sealed_at=sealed_at))


def test_a_bad_credential_inside_a_valid_seal_is_refused() -> None:
    store, _ = make_store()
    with pytest.raises(GrantError, match="long-lived") as err:
        store.accept(seal_for(store, value=exports(token=None)))
    assert_no_secret(str(err.value))


def test_expiry_ends_the_grant_and_the_endpoint_answers_ended() -> None:
    clock = Clock()
    store, sink = make_store(clock=clock)
    store.accept(seal_for(store))
    clock.now = NOW + 3601
    with pytest.raises(GrantError, match="credential aws-marketplace ended: expired at"):
        store.aws_response("aws-marketplace")  # even though `run` never ticked
    assert store.live_values() == ()
    assert store.grants()[0].state == "expired"
    assert sink.events[-1][0] == "🔒" and "expired at" in sink.notes[-1]
    assert "credential-grant request aws-marketplace" in sink.notes[-1]


def test_a_grant_past_its_expiry_is_never_listed_as_active() -> None:
    clock = Clock()
    store, sink = make_store(clock=clock)
    store.accept(seal_for(store))
    clock.now = NOW + 3601  # `run` has not ticked since
    assert [g.state for g in store.grants()] == ["expired"]
    assert [g["state"] for g in store.report()["grants"]] == ["expired"]
    assert store.live_values() == ()
    assert sink.events[-1][0] == "🔒" and "expired at" in sink.notes[-1]


def test_unknown_grant_is_not_granted() -> None:
    store, _ = make_store()
    with pytest.raises(GrantError, match="no credential named nope has been granted to this project"):
        store.aws_response("nope")


def test_warning_fires_once_before_expiry() -> None:
    clock = Clock()
    store, sink = make_store(clock=clock)
    store.accept(seal_for(store, expires_in=3600))
    store.tick()
    assert len(sink.notes) == 1  # just the grant notice
    clock.now = NOW + 3600 - WARN_BEFORE_S + 60
    store.tick()
    store.tick()
    warnings = [n for n in sink.notes if "expires at" in n]
    assert len(warnings) == 1 and "(in 14 minutes)" in warnings[0]
    assert [i for i, _ in sink.events].count("⏳") == 1


def test_run_ticks_until_stopped() -> None:
    clock = Clock()
    store, _ = make_store(clock=clock)
    store.accept(seal_for(store))

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(store.run(stop, interval_s=0.01))
        clock.now = NOW + 4000
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    assert store.grants()[0].state == "expired"


def test_revoke_one_all_and_unknown() -> None:
    store, sink = make_store()
    store.accept(seal_for(store, name="one"))
    store.accept(seal_for(store, name="two"))
    assert store.revoke("nope") == []
    assert store.revoke("one") == ["one"]
    assert store.revoke("one") == []  # already ended
    assert sink.events[-1] == ("🔒", "credential one ended: revoked by the owner")
    assert "Access to credential one has ended: revoked by the owner." in sink.notes[-1]
    assert store.revoke(reason="because") == ["two"]
    assert {g.name: (g.state, g.reason) for g in store.grants()} == {
        "one": ("revoked", "revoked by the owner"),
        "two": ("revoked", "because"),
    }
    assert store.live_values() == ()


def test_close_wipes_ends_and_refuses_new_grants() -> None:
    store, sink = make_store()
    store.accept(seal_for(store))
    grant = store._grants["aws-marketplace"]
    sealed = seal_for(store, name="later")
    store.close("the project ended")
    assert not grant.secret and not grant.token
    assert store.grants()[0].state == "ended" and store.grants()[0].reason == "the project ended"
    assert store.key._private is None
    with pytest.raises(GrantError):
        store.accept(sealed)
    assert "credential aws-marketplace ended: the project ended" in sink.events[-1][1]
    store.close()  # idempotent


def test_live_values_and_report() -> None:
    store, _ = make_store()
    store.accept(seal_for(store))
    assert set(store.live_values()) == {SECRET, TOKEN}
    report = store.report()
    assert report["key"] == store.key.public_b64 and report["kid"] == store.key.kid
    assert report["grants"][0]["name"] == "aws-marketplace"
    assert report["grants"][0]["expires"] == format_utc(NOW + 3600)
    assert_no_secret(json.dumps(report))


def test_link_round_trips_and_refuses_a_bad_name() -> None:
    store, _ = make_store()
    link = _grant_seal.parse_link(store.link("https://mcp.example/", "aws-marketplace", 4))
    assert (link.project, link.name, link.public_b64, link.hours) == (
        "proj-a",
        "aws-marketplace",
        store.key.public_b64,
        4,
    )
    with pytest.raises(GrantError):
        store.link("https://mcp.example", "Bad Name", 4)


# --- persistence -----------------------------------------------------------------------------------------


def test_restore_marks_active_grants_ended_and_tells_the_session(tmp_path: Path) -> None:
    path = tmp_path / "state" / "grants.json"
    first, _ = make_store(state_path=path)
    first.accept(seal_for(first, name="one"))
    first.accept(seal_for(first, name="two"))
    first.revoke("two")
    second, sink = make_store(state_path=path)
    second.restore()
    states = {g.name: (g.state, g.reason) for g in second.grants()}
    assert states == {"one": ("ended", "the project restarted"), "two": ("revoked", "revoked by the owner")}
    assert sink.events == [("🔒", "credential one ended: the project restarted")]
    assert "did not survive" in sink.notes[0] and "credential-grant request one" in sink.notes[0]
    third, again = make_store(state_path=path)
    third.restore()
    assert again.notes == []  # said once
    with pytest.raises(GrantError, match="the project restarted"):
        second.aws_response("one")


def test_restore_without_a_file_or_with_a_damaged_one(tmp_path: Path) -> None:
    store, sink = make_store(state_path=tmp_path / "none.json")
    store.restore()
    (tmp_path / "bad.json").write_text("{not json")
    damaged, _ = make_store(state_path=tmp_path / "bad.json")
    damaged.restore()
    assert store.grants() == damaged.grants() == [] and sink.notes == []


def test_restore_keeps_the_good_entries_when_one_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    first, _ = make_store(state_path=path)
    first.accept(seal_for(first, name="one"))
    recorded = json.loads(path.read_text())
    recorded["grants"].append({"kind": "aws", "state": "active"})  # no name
    path.write_text(json.dumps(recorded))
    second, sink = make_store(state_path=path)
    second.restore()
    assert [(g.name, g.state) for g in second.grants()] == [("one", "ended")]
    assert ("🔒", "credential one ended: the project restarted") in sink.events
    assert ("⚠️", "grant state partly unreadable") in sink.events


def test_state_file_is_private_and_never_holds_a_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "workspace" / "grants.json"
    clock = Clock()
    store, sink = make_store(state_path=path, clock=clock)
    store.accept(seal_for(store))
    store.aws_response("aws-marketplace")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    clock.now = NOW + 4000
    store.tick()
    store.close()
    for part in SECRETS:
        assert part.encode() not in path.read_bytes()
        for found in tmp_path.rglob("*"):
            if found.is_file():
                assert part.encode() not in found.read_bytes(), found
    assert not list(path.parent.glob("*.tmp"))
    assert_no_secret(" ".join(sink.notes) + " ".join(text for _, text in sink.events))


# --- the server ------------------------------------------------------------------------------------------


@pytest.fixture
def served():
    started: list[GrantServer] = []

    def make(project: str, **kwargs: Any) -> tuple[GrantStore, GrantServer, Clock]:
        clock = Clock()
        store, _ = make_store(project, clock=clock, **kwargs)
        server = GrantServer(store)
        server.start()
        started.append(server)
        return store, server, clock

    yield make
    for server in started:
        server.stop()


def get(server: GrantServer, path: str, token: str | None = None, *, method: str = "GET", raw: bool = False):
    headers = {} if token is None else {"Authorization": token}
    req = urllib.request.Request(server.url + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), exc.headers


def test_server_binds_loopback_only(served) -> None:
    _, server, _ = served("proj-a")
    assert server.url.startswith("http://127.0.0.1:")
    assert server._httpd.server_address[0] == "127.0.0.1"
    assert server.env() == {URL_ENV: server.url, TOKEN_ENV: server.token}
    assert len(server.token) >= 43


def test_server_authentication(served) -> None:
    store, server, _ = served("proj-a")
    store.accept(seal_for(store))
    status, body, _ = get(server, "/v1/grants")
    assert status == 401 and set(body) == {"code", "message"}
    status, body, _ = get(server, "/v1/grants", "wrong")
    assert status == 403 and set(body) == {"code", "message"}
    assert get(server, "/v1/grants", server.token)[0] == 200
    assert get(server, "/v1/grants", f"Bearer {server.token}")[0] == 200
    assert get(server, "/v1/grants", "Bearer wrong")[0] == 403
    assert get(server, "/v1/aws/aws-marketplace")[0] == 401  # no route is answered before authentication
    assert get(server, "/v1/nothing")[0] == 401


def test_server_routes(served) -> None:
    store, server, clock = served("proj-a")
    assert get(server, "/v1/aws/aws-marketplace", server.token)[:2] == (
        404,
        {
            "code": "NoSuchGrant",
            "message": "no credential named aws-marketplace has been granted to this project",
        },
    )
    store.accept(seal_for(store))
    status, body, headers = get(server, "/v1/aws/aws-marketplace", server.token)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert body["SecretAccessKey"] == SECRET and body["Token"] == TOKEN and body["Expiration"].endswith("Z")
    status, body, _ = get(server, "/v1/grants", server.token)
    assert status == 200 and [g["name"] for g in body["grants"]] == ["aws-marketplace"]
    assert_no_secret(json.dumps(body))
    assert get(server, "/v1/elsewhere", server.token)[0] == 404
    assert get(server, "/v1/aws/aws-marketplace", server.token, method="POST")[0] == 404
    clock.now = NOW + 4000
    status, body, _ = get(server, "/v1/aws/aws-marketplace", server.token)
    assert (status, body["code"]) == (404, "GrantEnded") and "expired at" in body["message"]
    store.accept(seal_for(store, sealed_at=clock.now, expires_in=3600 + 4000))
    status, body, _ = get(server, "/v1/revoke/aws-marketplace", server.token, method="POST")
    assert (status, body) == (200, {"revoked": ["aws-marketplace"]})
    assert store.grants()[0].reason == "released by the project"


def test_server_logs_nothing(served, capsys: pytest.CaptureFixture[str]) -> None:
    _, server, _ = served("proj-a")
    get(server, "/v1/aws/aws-marketplace", server.token)
    get(server, "/v1/grants", "wrong")
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


# --- two projects at once ---------------------------------------------------------------------------------


def test_two_projects_share_nothing(served, monkeypatch: pytest.MonkeyPatch) -> None:
    a, server_a, _ = served("proj-a")
    b, server_b, _ = served("proj-b")
    sealed_a = seal_for(a, region="eu-west-1")
    with pytest.raises(GrantError):
        b.accept(sealed_a)
    a.accept(sealed_a)
    assert b.grants() == [] and b.live_values() == ()
    assert get(server_a, "/v1/grants", server_b.token)[0] == 403
    assert get(server_a, "/v1/aws/aws-marketplace", server_b.token)[0] == 403
    assert get(server_b, "/v1/grants", server_a.token)[0] == 403
    assert get(server_b, "/v1/aws/aws-marketplace", server_b.token)[0] == 404
    assert get(server_a, "/v1/aws/aws-marketplace", server_a.token)[0] == 200


#: Every other place an AWS SDK looks for credentials before (or instead of) the container endpoint.
_OTHER_SOURCES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CREDENTIAL_FILE",
)


def container_provider(env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """What an AWS SDK's container-credentials provider does with `env`: one GET, the token as Authorization.

    The SDK itself (botocore) is not a dependency of this repo: the round-1 reviewer ran real botocore against
    this endpoint; the test speaks the protocol itself so it never skips. This is its request:
    `AWS_CONTAINER_CREDENTIALS_FULL_URI` with `AWS_CONTAINER_AUTHORIZATION_TOKEN` sent verbatim.
    """
    req = urllib.request.Request(
        env["AWS_CONTAINER_CREDENTIALS_FULL_URI"],
        headers={"Authorization": env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_an_sdk_through_exec_env_gets_only_its_own_projects_grant(served) -> None:
    a, server_a, clock_a = served("proj-a")
    b, server_b, _ = served("proj-b")
    sealed_a = seal_for(a, region="eu-west-1")
    with pytest.raises(GrantError):
        b.accept(sealed_a)
    a.accept(sealed_a)
    pod = {
        "PATH": "/bin",
        "AWS_ROLE_ARN": "pod-role",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token",
        "AWS_PROFILE": "default",
        "AWS_ACCESS_KEY_ID": "stays-out",
        "AWS_CREDENTIAL_FILE": "/etc/legacy",
    }

    env_a = build_exec_env(pod, server_a.url, server_a.token, "aws-marketplace", "eu-west-1")
    assert not set(_OTHER_SOURCES) & set(env_a)
    assert env_a["AWS_EC2_METADATA_DISABLED"] == "true"
    assert (
        env_a["AWS_SHARED_CREDENTIALS_FILE"]
        == env_a["AWS_CONFIG_FILE"]
        == env_a["BOTO_CONFIG"]
        == "/dev/null"
    )
    status, body = container_provider(env_a)
    assert status == 200
    assert (body["AccessKeyId"], body["SecretAccessKey"], body["Token"]) == (KEY_ID, SECRET, TOKEN)

    # Project B's session: same name, its own endpoint and token. Nothing, and no other source to fall to.
    env_b = build_exec_env(pod, server_b.url, server_b.token, "aws-marketplace", None)
    assert not set(_OTHER_SOURCES) & set(env_b) and env_b["AWS_EC2_METADATA_DISABLED"] == "true"
    status, body = container_provider(env_b)
    assert (status, body["code"]) == (404, "NoSuchGrant")
    assert_no_secret(json.dumps(body))
    # B's token on A's endpoint, A's on B's: refused.
    assert container_provider({**env_a, "AWS_CONTAINER_AUTHORIZATION_TOKEN": server_b.token})[0] == 403
    assert container_provider({**env_b, "AWS_CONTAINER_AUTHORIZATION_TOKEN": server_a.token})[0] == 403

    clock_a.now = NOW + 4000
    status, body = container_provider(env_a)
    assert (status, body["code"]) == (404, "GrantEnded")
    assert_no_secret(json.dumps(body))


# --- redaction ---------------------------------------------------------------------------------------------


def test_registered_sources_and_aws_shapes_are_redacted() -> None:
    store, _ = make_store()
    store.accept(seal_for(store))
    _bridge.register_redaction_source(store.live_values)
    try:
        text = f"the secret {SECRET} and the token {TOKEN} appear"
        out = _bridge.redact_tokens(text)
        assert_no_secret(out)
        assert out.startswith("the secret <redacted, 40 chars>")
    finally:
        _bridge.unregister_redaction_source(store.live_values)
    # unregistered: the bare values are no longer known, only their shapes are
    assert SECRET in _bridge.redact_tokens(f"the secret {SECRET}")


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        (f"id {KEY_ID} here", "id <redacted> here"),
        (exports(), "AWS_SESSION_TOKEN"),
        (exports().replace('"', ""), "AWS_SECRET_ACCESS_KEY="),
        (f"aws_secret_access_key = {SECRET}", "aws_secret_access_key = <redacted>"),
        (f"AWS_SECURITY_TOKEN: {TOKEN}", "AWS_SECURITY_TOKEN: <redacted>"),
        (f"aws_session_token={TOKEN}&x=1", "aws_session_token=<redacted>&x=1"),
        (assume_role_json(EXPIRY), '"SessionToken": "<redacted>"'),
        (json.dumps({"SecretAccessKey": SECRET}), '"SecretAccessKey": "<redacted>"'),
        (json.dumps({"Token": TOKEN, "Expiration": EXPIRY}), '"Token": "<redacted>"'),
        (json.dumps(assume_role_json(EXPIRY)), '\\"SecretAccessKey\\": \\"<redacted>'),
    ],
)
def test_aws_shapes_are_redacted_without_a_source(text: str, kept: str) -> None:
    out = _bridge.redact_tokens(text)
    assert_no_secret(out)
    assert KEY_ID not in out
    assert kept in out


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        (exports(), "aws secret access key"),
        (exports().replace('"', ""), "aws secret access key"),
        (f"aws_secret_access_key = {SECRET}", "aws secret access key"),
        (f"AWS_SECURITY_TOKEN: {TOKEN}", "aws session token"),
        (f"aws_session_token={TOKEN}&x=1", "aws session token"),
        (assume_role_json(EXPIRY), "SecretAccessKey field"),
        (json.dumps({"SecretAccessKey": SECRET}), "SecretAccessKey field"),
        (json.dumps({"Token": TOKEN, "Expiration": EXPIRY}), "Token field"),
        (json.dumps(assume_role_json(EXPIRY)), "SecretAccessKey field"),
        (f"id {KEY_ID} here", ""),
        ("aws_session_token = abc123", ""),
        ('{"Token": "abc"}', ""),
    ],
)
def test_credential_shape_names_a_secret_bearing_shape_and_not_a_key_id(text: str, shape: str) -> None:
    assert _bridge.find_credential_shape(text) == shape


def test_a_short_token_field_is_left_alone() -> None:
    assert _bridge.redact_tokens('{"Token": "abc"}') == '{"Token": "abc"}'


def test_a_failing_source_does_not_stop_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    def broken() -> list[str]:
        raise RuntimeError("no")

    _bridge.register_redaction_source(broken)
    try:
        assert _bridge.redact_tokens(f"id {KEY_ID}") == "id <redacted>"
        assert "redaction source failed: RuntimeError" in capsys.readouterr().err
    finally:
        _bridge.unregister_redaction_source(broken)


def test_warning_says_one_minute_in_the_singular() -> None:
    clock = Clock()
    store, sink = make_store(clock=clock)
    store.accept(seal_for(store, expires_in=3600))
    clock.now = NOW + 3600 - 30
    store.tick()
    assert any("(in 1 minute)" in n for n in sink.notes)


def test_a_sealed_text_opens_once_so_a_revoke_cannot_be_undone_by_pasting_it_again() -> None:
    store, _ = make_store()
    blob = seal_for(store)
    store.accept(blob)
    store.revoke("aws-marketplace")
    with pytest.raises(GrantError, match="already been used"):
        store.accept(blob)
    assert [g.state for g in store.grants()] == ["revoked"] and store.live_values() == ()


def test_a_re_encoded_sealed_text_does_not_open_either() -> None:
    store, _ = make_store()
    blob = seal_for(store)
    store.accept(blob)
    store.revoke("aws-marketplace")
    env = json.loads(_grant_seal.decode_b64url(blob[5:]))
    reordered = dict(reversed(list(env.items())))
    for variant in (env, reordered):
        again = "fgc1." + _grant_seal.encode_b64url(json.dumps(variant, indent=1).encode())
        assert again != blob
        with pytest.raises(GrantError, match="already been used"):
            store.accept(again)
    assert [g.state for g in store.grants()] == ["revoked"] and store.live_values() == ()


def test_the_seven_day_cap_counts_from_when_it_was_sealed() -> None:
    store, _ = make_store()
    info = store.accept(seal_for(store, sealed_at=NOW - 23 * 3600, expires_in=MAX_GRANT_S + 3600))
    assert info.expires_at == NOW - 23 * 3600 + MAX_GRANT_S
