"""The suite-wide guard: no test runs the pod's real `claude`/`codex` (QA round 5, test_nudge)."""

import asyncio
import os
import stat
import tempfile
from pathlib import Path

import pytest


def _script(path: Path, code: int) -> Path:
    path.write_text(f"#!/bin/sh\nexit {code}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["claude", "codex"])
async def test_a_bare_engine_name_skips_a_real_cli_ahead_of_the_fake(tmp_path, name):
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as pod:
        _script(Path(pod) / name, 97)  # stands in for the pod's real CLI, listed first
        fakes = tmp_path / "bin"
        fakes.mkdir()
        _script(fakes / name, 0)
        env = {**os.environ, "PATH": f"{pod}{os.pathsep}{fakes}{os.pathsep}{os.environ['PATH']}"}
        proc = await asyncio.create_subprocess_exec(name, env=env)
        assert await proc.wait() == 0


@pytest.mark.asyncio
async def test_a_real_engine_cli_is_refused(tmp_path):
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as pod:
        real = _script(Path(pod) / "codex", 97)
        with pytest.raises(RuntimeError, match="real engine"):
            await asyncio.create_subprocess_exec(str(real))
        env = {**os.environ, "PATH": pod}
        with pytest.raises(RuntimeError, match="no fake"):
            await asyncio.create_subprocess_exec("codex", env=env)
