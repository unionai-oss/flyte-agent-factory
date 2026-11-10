"""`ops/create_personal_browser_token.py` names the secret after the identity and never prints the token."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "create_personal_browser_token.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("create_personal_browser_token", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(monkeypatch, script, argv):
    calls = []

    def run(cmd, check):
        calls.append((cmd, Path(cmd[cmd.index("--from-file") + 1]).read_text()))

    monkeypatch.setattr(script.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["create_personal_browser_token.py", *argv])
    script.main()
    return calls


@pytest.mark.parametrize(
    ("argv", "secret"),
    [
        ([], "browser_personal_token"),
        (["--identity", "github"], "browser_github_token"),
        (["--identity", "my-bank", "--project", "p"], "browser_my_bank_token"),
    ],
)
def test_the_secret_is_named_after_the_identity(monkeypatch, script, capsys, argv, secret):
    ((cmd, token),) = _capture(monkeypatch, script, argv)
    assert cmd[:4] == ["flyte", "create", "secret", secret]
    assert token and token not in capsys.readouterr().out + " ".join(cmd)
    assert ("--project" in cmd) == ("--project" in argv)  # other arguments go to flyte


def test_a_bad_identity_name_is_refused(monkeypatch, script):
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: pytest.fail("must not run flyte"))
    monkeypatch.setattr(sys, "argv", ["x", "--identity", "Bad Name"])
    with pytest.raises(SystemExit):
        script.main()
