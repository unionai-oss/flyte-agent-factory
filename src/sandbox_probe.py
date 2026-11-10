r"""Verify the sandbox child-action contract against a real cluster.

This is the piece agent-submitted sandbox jobs depend on, and the piece I could not test
without a cluster: a verbatim-mode sandbox with a `Dir` in and a `Dir` out,
built on demand from runtime-declared packages. If this passes, an ML job the
agent writes will land its inputs and hand back its artifacts.

It reuses the agent image so it doesn't trigger a second image build.

It lives under `src/` rather than `scripts/` because a task's file has to sit
inside `--root-dir` -- Flyte derives the in-container module name from the path
relative to it, and refuses a file it can't place.

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/sandbox_probe.py probe
"""

from pathlib import Path

import flyte
import flyte.sandbox
from flyte.io import Dir

from flyte_agent_factory._images import agent_image

env = flyte.TaskEnvironment(
    name="sandbox-probe",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)

# Exactly the contract the agent is told to write against in `_project_memory`:
# read /var/inputs/workdir, write /var/outputs/artifacts.
SCRIPT = """
import pathlib
import sys

src = pathlib.Path("/var/inputs/workdir")
out = pathlib.Path("/var/outputs/artifacts")
out.mkdir(parents=True, exist_ok=True)

found = sorted(p.name for p in src.rglob("*") if p.is_file()) if src.exists() else []
(out / "listing.txt").write_text("\\n".join(found) + "\\n")

import numpy as np
(out / "env.txt").write_text(f"numpy={np.__version__}\\npython={sys.version.split()[0]}\\n")
(out / "computed.txt").write_text(str(int(np.arange(10).sum())) + "\\n")

print(f"input files seen: {found}", flush=True)
"""


@env.task(report=True)
async def probe() -> str:
    """Ship a directory to a built-on-demand container and read its output back."""
    work = Path("/tmp/probe-input")
    work.mkdir(parents=True, exist_ok=True)
    (work / "alpha.txt").write_text("first\n")
    (work / "nested").mkdir(exist_ok=True)
    (work / "nested" / "beta.txt").write_text("second\n")

    sandbox = flyte.sandbox.create(
        name="probe",
        code=SCRIPT,
        auto_io=False,
        inputs={"workdir": Dir},
        outputs={"artifacts": Dir},
        packages=["numpy"],
        cache="auto",
    )

    with flyte.group("probe-job"):
        result = await sandbox.run.aio(workdir=await Dir.from_local(str(work)))

    artifacts = result[0] if isinstance(result, tuple) else result
    local = Path(await artifacts.download())
    report = "\n".join(
        f"{p.relative_to(local)}: {p.read_text().strip()}" for p in sorted(local.rglob("*")) if p.is_file()
    )
    print(report, flush=True)
    return report


if __name__ == "__main__":
    flyte.init_from_config()
    run = flyte.run(probe)
    print(f"run: {run.url}")
    run.wait()
    print(run.outputs())
