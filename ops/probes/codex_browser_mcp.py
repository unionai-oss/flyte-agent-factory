"""Live probe: the real Codex CLI drives the in-pod browser through the rendered `config.toml`.

    PYTHONPATH=src uv run python ops/probes/codex_browser_mcp.py

Run in an agent pod (Chromium and playwright-mcp installed, the Codex access token mounted as
CODEX_ACCESS_TOKEN). Starts the in-pod browser, renders the same server entry the author gets, asks
Codex to open https://example.com and prints only the page title it reports.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from flyte_agent_factory import _local_browser, browser
from flyte_agent_factory._author import _login_with_access_token
from flyte_agent_factory._session_config import CODEX_TOKEN_ENV_VAR

PROMPT = (
    "Use the browser tools to open https://example.com and read the page title. "
    "Reply with the title only, nothing else."
)


async def probe() -> str:
    """Return the result of a Codex session using the in-pod browser through MCP."""
    token = os.environ.get(CODEX_TOKEN_ENV_VAR, "").strip()
    if not token:
        raise RuntimeError(f"mount the Codex access token as {CODEX_TOKEN_ENV_VAR} before running the probe")
    if not _local_browser.available():
        raise RuntimeError("this image has no in-pod browser")
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch) / "codex"
        home.mkdir()
        await asyncio.to_thread(_login_with_access_token, home, token)
        local = _local_browser.LocalBrowser(
            profile_dir=Path(scratch) / "profile", output_dir=Path(scratch) / "out"
        )
        endpoint = await local.start()
        try:
            servers = browser.mcp_config(endpoint, local.client_headers)["mcpServers"]
            (home / "config.toml").write_text(browser.codex_mcp_config(servers))
            answer = Path(scratch) / "answer"
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "codex",
                    "exec",
                    "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "-o",
                    str(answer),
                    "--",
                    PROMPT,
                ],
                cwd=scratch,
                env={**os.environ, "CODEX_HOME": str(home)},
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0 or not answer.is_file():
                raise RuntimeError(f"codex exec failed with exit code {result.returncode}")
            return answer.read_text().strip()
        finally:
            await local.stop()


if __name__ == "__main__":
    print(asyncio.run(probe()))
