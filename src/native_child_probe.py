r"""Probe whether a subprocess of a task can create a child action of that task natively.

If yes, `flyte_agent_factory._jobs` -- a CLI, a JSON file protocol and a runner,
about 450 lines -- collapses into a short helper plus a documented pattern, and
the agent writes ordinary Flyte code instead of calling an interface I invented.

The mechanism would be `flyte.load_interactive_ctx()`, which restores a task's
context in another process so `flyte.ctx()` works there. It is documented for
debuggers and notebooks, and it deliberately does *not* create a controller --
which is what child-action submission goes through. So this probe stitches two
private APIs together to find out whether submission works anyway:

    flyte._interactive_run_context.write_interactive_run_context   (private)
    flyte._internal.controllers.create_controller                  (private)

That dependency is the real cost to weigh against the bridge, whatever the
answer here turns out to be.

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/native_child_probe.py probe
"""

import asyncio
import json
import sys
from pathlib import Path

import flyte

from flyte_agent_factory._images import agent_image

env = flyte.TaskEnvironment(
    name="native-child-probe",
    image=agent_image(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)

# Run in a subprocess that has no task context of its own -- exactly the
# position the Claude Code session is in.
# Run in a subprocess that has no task context of its own -- exactly the
# position the Claude Code session is in. Two stages, because they test
# different things:
#
#   stage 1  flyte.sandbox.create(...)  ships its code as a task *input*, so
#            the cluster needs no code bundle for it.
#   stage 2  a plain `@env.task` the script just defined. This is the ordinary
#            v2 programming model -- calling an annotated function is a child
#            action -- but the container has to *import* that function, and the
#            restored context's code bundle is the parent's, not this script's.
#            Whether Flyte uploads a bundle for it is the whole question.
CHILD = """
import asyncio, json, sys, traceback

result = {"loaded_ctx": False, "controller": None, "sandbox_child": None, "task_child": None}
try:
    import flyte, flyte.sandbox

    ctx = flyte.load_interactive_ctx()
    result["loaded_ctx"] = True
    result["action"] = f"{ctx.action.run_name}/{ctx.action.name}"

    from flyte._internal.controllers import create_controller, get_controller

    try:
        get_controller()
        result["controller"] = "already present"
    except RuntimeError:
        create_controller("rust")
        result["controller"] = "created (rust)"
except Exception as e:
    result["setup_error"] = f"{type(e).__name__}: {e}"
    result["traceback"] = traceback.format_exc()[-1200:]
    print("PROBE_RESULT " + json.dumps(result))
    sys.exit(0)

# --- stage 1: code shipped as an input ----------------------------------- #
try:
    sandbox = flyte.sandbox.create(
        name="native-child-sandbox",
        command=["/bin/sh", "-c", "echo hello-from-sandbox-child"],
    )
    asyncio.run(sandbox.run.aio())
    result["sandbox_child"] = "ok"
except Exception as e:
    result["sandbox_child"] = f"{type(e).__name__}: {e}"

# --- stage 2: an ordinary annotated task, image built on the spot --------- #
# Stage 2 previously failed with ImagePullBackOff -- submission worked, but
# nothing had built the image. `flyte.build_images` is the missing call, and it
# makes this path pure vanilla Flyte: define an environment with whatever
# packages you want, build it, call the task.
try:
    child_env = flyte.TaskEnvironment(
        name="native-child-task",
        image=flyte.Image.from_debian_base(name="native-child-task").with_pip_packages("cowsay"),
        resources=flyte.Resources(cpu=1, memory="1Gi"),
    )

    @child_env.task
    async def moo(x: int) -> str:
        import cowsay

        cowsay.cow(f"child action says {x * 2}")
        return f"doubled to {x * 2} with cowsay available"

    async def build_then_call():
        await flyte.build_images.aio(child_env)
        return await moo(21)

    result["task_child"] = f"returned: {asyncio.run(build_then_call())}"
except Exception as e:
    result["task_child"] = f"{type(e).__name__}: {e}"
    result["task_traceback"] = traceback.format_exc()[-1200:]

print("PROBE_RESULT " + json.dumps(result))
"""


@env.task(report=True)
async def probe() -> str:
    """Write the run-context file, then try to launch a child action from a subprocess."""
    from flyte._interactive_run_context import write_interactive_run_context

    ctx = flyte.ctx()
    assert ctx is not None

    # `load_interactive_ctx` reads these keys; assemble them from the live
    # context rather than from the debug-mode path that normally writes them.
    bundle = ctx.code_bundle
    params = {
        "name": ctx.action.name,
        "run_name": ctx.action.run_name,
        "project": ctx.action.project,
        "domain": ctx.action.domain,
        "org": ctx.action.org,
        "version": ctx.version,
        "inputs": ctx.input_path,
        "outputs_path": ctx.output_path,
        "run_base_dir": ctx.run_base_dir,
        "raw_data_path": ctx.raw_data_path.path,
        "checkpoint_path": ctx.checkpoint_paths.checkpoint_path if ctx.checkpoint_paths else None,
        "prev_checkpoint": ctx.checkpoint_paths.prev_checkpoint_path if ctx.checkpoint_paths else None,
        "run_start_time": ctx.run_start_time.isoformat() if ctx.run_start_time else None,
        "tgz": bundle.tgz if bundle else None,
        "pkl": bundle.pkl if bundle else None,
        "dest": bundle.destination if bundle else None,
    }
    written = write_interactive_run_context(params)
    print(f"wrote run context to {written}", flush=True)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CHILD,
        cwd=str(Path(written).parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    print(text[-4000:], flush=True)

    line = next((ln for ln in text.splitlines() if ln.startswith("PROBE_RESULT ")), None)
    verdict = json.loads(line[len("PROBE_RESULT ") :]) if line else {"error": "no result line"}
    return json.dumps(verdict, indent=2)
