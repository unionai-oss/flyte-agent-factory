"""`credential-grant` against a live loopback server, and the no-browser `seal`."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from flyte_agent_factory import _grant_seal
from flyte_agent_factory import _grants_cli as cli
from flyte_agent_factory._grants import TOKEN_ENV, URL_ENV, GrantServer, GrantStore
from tests.test_grants import KEY_ID, NOW, SECRET, TOKEN, Clock, assert_no_secret, exports, seal_for

SRC = str(Path(__file__).resolve().parents[1] / "src")


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[GrantStore, GrantServer]]:
    store = GrantStore("proj-a")
    server = GrantServer(store)
    server.start()
    monkeypatch.setenv(URL_ENV, server.url)
    monkeypatch.setenv(TOKEN_ENV, server.token)
    yield store, server
    server.stop()


def grant(store: GrantStore, name: str = "aws-marketplace", **kwargs) -> None:
    # The store reads the real clock here, so seal with real times.
    now = time.time()
    payload = {
        "name": name,
        "kind": "aws",
        "expires_at": int(now + 3600),
        "sealed_at": int(now),
        "value": exports(),
        "region": kwargs.get("region", ""),
    }
    store.accept(_grant_seal.seal(store.key.public_b64, store.project, payload))


def test_missing_environment_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    for argv in (["list"], ["exec", "x", "--", "true"], ["revoke", "x"]):
        assert cli.main(argv) == 2
    assert "credential grants are not available in this session" in capsys.readouterr().err


def test_a_server_that_is_gone_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(TOKEN_ENV, "t")
    assert cli.main(["list"]) == 2
    assert "not available" in capsys.readouterr().err


def test_list(live, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = live
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == "no credentials granted"
    grant(store, "one")
    grant(store, "two")
    store.revoke("two")
    assert cli.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["one", "aws", "active", "expires", store.grants()[0].to_dict()["expires"]]
    assert lines[1].split(maxsplit=3) == ["two", "aws", "revoked", "revoked by the owner"]
    assert_no_secret("\n".join(lines))


def test_revoke(live, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = live
    grant(store)
    assert cli.main(["revoke", "aws-marketplace"]) == 0
    assert "ended: aws-marketplace" in capsys.readouterr().out
    assert store.grants()[0].reason == "released by the project"
    assert cli.main(["revoke", "aws-marketplace"]) == 1
    assert "no active credential" in capsys.readouterr().out


def test_exec_refuses_what_is_not_active(live, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = live
    assert cli.main(["exec", "aws-marketplace", "--", "true"]) == 2
    assert "credential-grant request aws-marketplace" in capsys.readouterr().err
    grant(store)
    store.revoke("aws-marketplace")
    assert cli.main(["exec", "aws-marketplace", "--", "true"]) == 2
    err = capsys.readouterr().err
    assert "ended: revoked by the owner" in err and "credential-grant request aws-marketplace" in err


def test_exec_refuses_a_grant_past_its_expiry_before_any_tick(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    store = GrantStore("proj-a", clock=clock)
    server = GrantServer(store)
    server.start()
    try:
        monkeypatch.setenv(URL_ENV, server.url)
        monkeypatch.setenv(TOKEN_ENV, server.token)
        store.accept(seal_for(store))
        clock.now = NOW + 3601  # past expiry; nothing has ticked
        calls = []
        monkeypatch.setattr(os, "execvpe", lambda *args: calls.append(args))
        assert cli.main(["exec", "aws-marketplace", "--", "true"]) == 2
        assert calls == [] and "ended: expired at" in capsys.readouterr().err
        assert cli.main(["list"]) == 0
        assert capsys.readouterr().out.split()[:3] == ["aws-marketplace", "aws", "expired"]
    finally:
        server.stop()


def test_exec_replaces_the_process_with_the_grant_environment(live, monkeypatch: pytest.MonkeyPatch) -> None:
    store, server = live
    grant(store, region="eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "stays-out")
    calls = []
    monkeypatch.setattr(os, "execvpe", lambda *args: calls.append(args))
    assert cli.main(["exec", "aws-marketplace", "--", "aws", "sts", "get-caller-identity"]) == 0
    file, argv, env = calls[0]
    assert (file, argv) == ("aws", ["aws", "sts", "get-caller-identity"])
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] == f"{server.url}/v1/aws/aws-marketplace"
    assert env["AWS_REGION"] == "eu-west-1" and "AWS_ACCESS_KEY_ID" not in env


def test_exec_runs_a_real_command_without_leaking_values(live, tmp_path: Path) -> None:
    store, server = live
    grant(store)
    script = (
        "for v in AWS_CONTAINER_AUTHORIZATION_TOKEN AWS_ACCESS_KEY_ID AWS_SESSION_TOKEN "
        "AWS_WEB_IDENTITY_TOKEN_FILE; "
        'do eval "x=\\${$v-}"; if [ -n "$x" ]; then echo "$v set"; else echo "$v unset"; fi; done; '
        'echo "ec2=$AWS_EC2_METADATA_DISABLED"'
    )
    env = {
        **os.environ,
        **server.env(),
        "PYTHONPATH": SRC,
        "AWS_ACCESS_KEY_ID": "pod-key",
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/token",
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "flyte_agent_factory._grants_cli",
            "exec",
            "aws-marketplace",
            "--",
            "sh",
            "-c",
            script,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:-1] == [
        "AWS_CONTAINER_AUTHORIZATION_TOKEN set",
        "AWS_ACCESS_KEY_ID unset",
        "AWS_SESSION_TOKEN unset",
        "AWS_WEB_IDENTITY_TOKEN_FILE unset",
        "ec2=true",
    ]


def test_exec_of_a_missing_command_exits_127(live, capsys: pytest.CaptureFixture[str]) -> None:
    store, _ = live
    grant(store)
    assert cli.main(["exec", "aws-marketplace", "--", "no-such-command-here"]) == 127
    assert "cannot run no-such-command-here" in capsys.readouterr().err


def test_exec_env() -> None:
    environ = {
        "PATH": "/bin",
        "AWS_REGION": "us-east-1",
        **{k: "x" for k in cli._DROPPED},
    }
    env = cli.build_exec_env(environ, "http://127.0.0.1:1/", "tok", "n", None)
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"] == "http://127.0.0.1:1/v1/aws/n"
    assert env["AWS_CONTAINER_AUTHORIZATION_TOKEN"] == "tok"
    assert not set(cli._DROPPED) & set(env)
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == env["AWS_CONFIG_FILE"] == "/dev/null"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_REGION"] == "us-east-1" and env["PATH"] == "/bin" and "AWS_DEFAULT_REGION" not in env
    env = cli.build_exec_env(environ, "http://127.0.0.1:1", "tok", "n", "eu-west-1")
    assert env["AWS_REGION"] == env["AWS_DEFAULT_REGION"] == "eu-west-1"
    assert environ["AWS_PROFILE"] == "x"  # the caller's mapping is untouched


def test_seal_cli_round_trip_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = GrantStore("proj-a")
    link = store.link("https://mcp.example", "aws-marketplace", 2)
    monkeypatch.setattr(sys, "stdin", io.StringIO(exports()))
    assert cli.run_seal_cli(["seal", "--link", link, "--region", "eu-west-1"]) == 0
    sealed = capsys.readouterr().out.strip()
    assert sealed.startswith("fgc1.") and "\n" not in sealed
    info = store.accept(sealed, expected_name="aws-marketplace")
    assert info.region == "eu-west-1"
    assert abs(info.expires_at - (time.time() + 2 * 3600)) < 10  # the link's hours
    monkeypatch.setattr(sys, "stdin", io.StringIO(exports()))
    assert cli.run_seal_cli(["seal", "--link", link, "--hours", "0.5"]) == 0
    assert abs(store.accept(capsys.readouterr().out.strip()).expires_at - (time.time() + 1800)) < 10


def test_seal_cli_refuses_what_the_supervisor_would(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    link = GrantStore("proj-a").link("https://mcp.example", "aws-marketplace", 2)
    monkeypatch.setattr(sys, "stdin", io.StringIO(exports(token=None)))
    assert cli.run_seal_cli(["seal", "--link", link]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "long-lived" in captured.err
    assert_no_secret(captured.err)
    assert cli.run_seal_cli(["seal", "--link", "https://mcp.example/nope"]) == 2


@pytest.mark.parametrize("key", ["A", "AAAA"])
def test_seal_cli_refuses_a_link_with_a_bad_key_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    kid = _grant_seal.derive_key_id(_grant_seal.decode_b64url("AAAA")) if key == "AAAA" else "abc"
    link = f"https://mcp.example/flyte-mcp/grant#v=1&p=proj-a&n=aws-marketplace&h=2&k={key}&kid={kid}"
    monkeypatch.setattr(sys, "stdin", io.StringIO(exports()))
    assert cli.run_seal_cli(["seal", "--link", link]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("seal: ") and len(captured.err.splitlines()) == 1


def test_seal_cli_reads_a_pasted_block_without_echo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = GrantStore("proj-a")
    link = store.link("https://mcp.example", "aws-marketplace", 1)
    lines = iter([*exports().splitlines(), "", "never read"])
    monkeypatch.setattr(sys, "stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(lines))
    assert cli.run_seal_cli(["seal", "--link", link]) == 0
    assert store.accept(capsys.readouterr().out.strip()).name == "aws-marketplace"


def test_module_main_seals(tmp_path: Path) -> None:
    store = GrantStore("proj-a")
    link = store.link("https://mcp.example", "aws-marketplace", 1)
    done = subprocess.run(
        [sys.executable, "-m", "flyte_agent_factory._grants", "seal", "--link", link],
        input=exports(),
        env={**os.environ, "PYTHONPATH": SRC},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert store.accept(done.stdout.strip()).name == "aws-marketplace"
    assert KEY_ID not in done.stdout and SECRET not in done.stdout and TOKEN not in done.stdout


def test_exec_env_leaves_no_legacy_boto_credential_source() -> None:
    env = cli.build_exec_env(
        {"AWS_CREDENTIAL_FILE": "/x", "BOTO_CONFIG": "/y"}, "http://127.0.0.1:1", "tok", "n", None
    )
    assert "AWS_CREDENTIAL_FILE" not in env
    assert env["BOTO_CONFIG"] == "/dev/null"  # also keeps ~/.boto and /etc/boto.cfg from being read


@pytest.mark.parametrize("hours", ["nan", "inf", "1e308"])
def test_seal_cli_refuses_hours_that_are_not_a_sane_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], hours: str
) -> None:
    link = GrantStore("proj-a").link("https://mcp.example", "aws-marketplace", 2)
    monkeypatch.setattr(sys, "stdin", io.StringIO(exports()))
    assert cli.run_seal_cli(["seal", "--link", link, "--hours", hours]) == 2
    assert "--hours" in capsys.readouterr().err
