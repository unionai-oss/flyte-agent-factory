"""Credential-free task for applying candidate patches and running declared checks.

This module runs in a container whose image does not install `flyte_agent_factory`
(the code bundle only carries `src/` as files), so it must not import it. The
few lines it needs from the factory are copied below; a test pins them to the
originals.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import flyte
from flyte.io import Dir

# This environment deliberately declares no secrets. The image is chosen by the submitter
# (`Backend.mechanical` swaps in the agent image, which has git and the project's dependencies):
# this module cannot import `flyte_agent_factory` to name it, and `_F_IMG_ID` inside the factory pod
# is only the bare Flyte base image, so it must not be used here. "auto" (the SDK default) is a
# placeholder that is never run.
env = flyte.TaskEnvironment(
    name="model-eval-mechanical",
    image=os.environ.get("FLYTE_INTERNAL_IMAGE") or "auto",
    resources=flyte.Resources(cpu=1, memory="2Gi", disk="10Gi"),
)

# Copied from flyte_agent_factory.agent (_CHECKS_SCRIPT, _safe_project_dir, _checks_rel).
_CHECKS_SCRIPT = ".claude-flyte/checks.sh"


def checks_rel(name: str) -> str:
    """Return the `checks.sh` path for project `name`, or the default script when `name` is unsafe."""
    safe = bool(name) and not Path(name).is_absolute() and ".." not in Path(name).parts and name != "."
    return f"{name}/checks.sh" if safe else _CHECKS_SCRIPT


# The supervisor runs checks with the pod's environment minus the model-billing and
# credential variables (_session_setup._session_env and `_run_declared_checks`). Mirror that so
# results match production, and also drop anything credential-shaped (the Flyte
# runtime's own keys) because a candidate patch controls what the checks execute.
_CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CREDENTIALS_JSON", "CODEX_AUTH_JSON")
_CREDENTIAL_NAME = re.compile(r"(?i)token|secret|password|api_key|credential|^_U_|^_UNION_")


def checks_env(environ: Mapping[str, str], home: str) -> dict[str, str]:
    """Return `environ` without credential variables and with `HOME` set to `home`."""
    kept = {k: v for k, v in environ.items() if k not in _CREDENTIAL_VARS and not _CREDENTIAL_NAME.search(k)}
    return {**kept, "HOME": home}


async def run_check(workdir: Dir, patch: str, project_name: str, isolate: bool = False) -> dict[str, Any]:
    """Apply `patch` to the downloaded workspace and run its declared checks.

    `isolate` is for running on the operator's machine: the checks get a fresh
    `env -i` with only PATH and a scratch HOME. That does not sandbox them -- they
    are still the operator's user, so /proc/*/environ of the operator's other
    processes and every file that user can read stay reachable. Use the container
    task for untrusted patches.
    """
    with tempfile.TemporaryDirectory(prefix="model-eval-check-") as tmp:
        root = Path(tmp)
        await workdir.download(tmp)
        archive = root / "repo.tar.gz"
        if archive.exists():
            with tarfile.open(archive) as tar:
                tar.extractall(root, filter="data")
            root /= "repo"
        patch_path = Path(tmp) / "candidate.patch"
        patch_path.write_text(patch)
        home = str(Path(tmp) / "home")
        Path(home).mkdir()
        child_env = checks_env(os.environ, home)

        async def run(*args: str) -> tuple[int, str]:
            log = Path(tmp) / "mechanical.log"
            if isolate:
                args = ("env", "-i", f"PATH={os.environ.get('PATH', os.defpath)}", f"HOME={home}", *args)
                spawn_env = {"PATH": os.environ.get("PATH", os.defpath)}
            else:
                spawn_env = child_env
            with log.open("wb") as stream:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=root,
                    env=spawn_env,
                    stdout=stream,
                    stderr=stream,
                    start_new_session=True,
                )
                try:
                    code = await asyncio.wait_for(proc.wait(), 600)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                    code = 124
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 8000))
                tail = stream.read().decode(errors="replace")
            return code, tail

        code, tail = await run("git", "apply", str(patch_path))
        if code:
            return {"applied": False, "tests_passed": False, "exit_code": code, "tail": tail}
        script = checks_rel(project_name)
        if not project_name and (root / "checks.sh").is_file():
            script = "checks.sh"
        if not (root / script).is_file():
            return {
                "applied": True,
                "tests_passed": None,
                "exit_code": None,
                "tail": "declared checks unavailable",
            }
        code, tail = await run("bash", script)
        return {"applied": True, "tests_passed": code == 0, "exit_code": code, "tail": tail}


@env.task
async def check(workdir: Dir, patch: str, project_name: str) -> dict[str, Any]:
    """Apply `patch` to `workdir` and run the project's checks; return the result."""
    return await run_check(workdir, patch, project_name)
