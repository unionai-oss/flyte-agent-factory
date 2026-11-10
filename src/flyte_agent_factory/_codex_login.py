"""Sign Codex in to a ChatGPT account inside the cluster and store the login as a slot's `codex_login` secret.

For a personal Plus or Pro plan, which cannot create Codex access tokens. `codex login --device-auth` prints a
link and a one-time code; they are shown as a HITL prompt (and in the report). The person opens the link,
signs in and enters the code; Codex notices the approval itself and writes `auth.json`, stored as the secret.
Signing in here mints a separate session, so nothing is copied off a laptop and the person's own Codex login
is untouched. A code that expires unused starts a fresh one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from flyte_agent_factory import _codex_auth, _session_config, _session_setup, _timeline
from flyte_agent_factory._conditions import open_condition
from flyte_agent_factory._errors import REMOTE_ERRORS

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DEVICE_URL = re.compile(r"https://auth\.openai\.com/\S*device\S*")
#: Only a complete code: followed by whitespace or an escape, never cut off at the end of a read.
_DEVICE_CODE = re.compile(r"\b([A-Z0-9]{3,6}-[A-Z0-9]{3,8})(?=[\s\x1b])")
#: How long one device code stays usable, as Codex says when it prints one.
CODE_LIFE_S = 15 * 60


def profile_logins() -> list[str]:
    """Return the Codex login secrets the loaded profile's slots name."""
    profile = _session_config._PROFILE
    return [slot.codex_login for slot in profile.slots if slot.codex_login] if profile else []


def device_prompt(output: str) -> tuple[str, str]:
    """(url, code) from `codex login --device-auth` output, or ("", "") while not printed yet."""
    clean = _ANSI_RE.sub("", output)
    url = _DEVICE_URL.search(clean)
    code = _DEVICE_CODE.search(clean[url.end() :] if url else clean)
    return (url.group(0) if url else ""), (code.group(1) if code else "")


async def _device_login(
    home: Path, max_wait_s: int, timeline: _timeline._Timeline, attempt: int
) -> tuple[int, str]:
    """One `codex login --device-auth`; shows its link and code as a HITL prompt. (exit code, output)."""
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "login",
        "--device-auth",
        # A container has no keyring: make sure the login lands in auth.json.
        "-c",
        'cli_auth_credentials_store="file"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**_session_setup._session_env(dict(os.environ)), "CODEX_HOME": str(home)},
        start_new_session=True,
    )
    assert proc.stdout is not None
    seen = ""
    shown = False
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=5)
        except asyncio.TimeoutError:
            chunk = b""
        if chunk:
            seen += chunk.decode(errors="replace")
        url, code = device_prompt(seen)
        if url and code and not shown:
            shown = True
            timeline.header("open", url, link=True)
            timeline.header("code", code)
            timeline.row("🔐", f"open {url}, sign in, and enter the code {code} (expires in 15 minutes)")
            await timeline.flush(force=True)
            # Nothing needs typing back (Codex notices the approval itself), so the prompt is not awaited;
            # answering it is only an acknowledgement.
            with contextlib.suppress(*REMOTE_ERRORS):
                await open_condition(
                    "codex-login",
                    attempt,
                    prompt=(
                        f"**Sign Codex in to ChatGPT**\n\n1. Open {url}\n2. Sign in\n3. Enter the code "
                        f"**{code}** (expires in 15 minutes)\n\nThe run continues by itself once you "
                        "approve; reply `done` if you like."
                    ),
                    prompt_type="markdown",
                    data_type=str,
                )
        if proc.returncode is not None or (not chunk and proc.stdout.at_eof()):
            break
    if proc.returncode is None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=5)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=10)
        return -1, seen
    return proc.returncode, seen


async def bootstrap(
    secret_name: str,
    *,
    store: Callable[[str, str], Awaitable[None]],
    timeline: _timeline._Timeline,
    max_wait_s: int = CODE_LIFE_S + 60,
    attempts: int = 3,
) -> str:
    """Sign in by device code and store the login as `secret_name`; a summary. Raises after `attempts`."""
    timeline.header("what", "signing Codex in to ChatGPT, inside the cluster")
    timeline.header("stores as", f"secret {secret_name!r}")
    await timeline.flush(force=True)
    last = ""
    with tempfile.TemporaryDirectory(prefix="codex-login-") as tmp:
        home = Path(tmp)
        home.chmod(0o700)
        auth = home / "auth.json"
        for attempt in range(1, attempts + 1):
            code, out = await _device_login(home, max_wait_s, timeline, attempt)
            if code == 0 and auth.is_file() and _codex_auth.has_refresh_token(auth.read_text()):
                payload = auth.read_text()
                await store(secret_name, payload)
                timeline.row("🔑", f"stored the Codex login ({len(payload)} bytes) as secret {secret_name!r}")
                await timeline.flush(force=True)
                return f"stored a Codex ChatGPT login as Flyte secret {secret_name!r}"
            last = _ANSI_RE.sub("", out)[-400:]
            timeline.row(
                "⏳",
                f"sign-in attempt {attempt}/{attempts} did not complete; "
                + ("starting a fresh code" if attempt < attempts else "giving up"),
            )
            await timeline.flush(force=True)
    raise RuntimeError(f"codex login did not complete in {attempts} attempts: {last}")
