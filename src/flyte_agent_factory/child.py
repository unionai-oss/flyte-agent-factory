"""Let the session run work as child actions of its own run.

The session is a subprocess of the agent task, so it has no task context, and
anything it submits would become a *new run*. `attach()` restores the parent's
context in the calling process, after which ordinary Flyte code produces child
actions of this run -- own container, own hardware, own logs, one run to watch.

This replaced a CLI with six verbs, a JSON file protocol and a runner, about
450 lines, all of it a worse version of calling Flyte directly.

    import asyncio, flyte
    from flyte_agent_factory.child import attach

    attach()

    env = flyte.TaskEnvironment(name="train", image="<an image that exists>",
                                resources=flyte.Resources(cpu=2, memory="8Gi"))

    @env.task
    async def train(lr: float) -> float:
        ...

    print(asyncio.run(train(0.01)))

Two caveats, both load-bearing:

**Images must already exist.** Submission works regardless, but a bare
`flyte.Image` name has no registry inside a pod, so the child fails to pull.
Reuse this container's image, or a public one, or use
`flyte.sandbox.create(packages=[...])`, which goes through the platform's
builder and can produce a new image from inside a run.

**It leans on two private APIs.** `flyte.load_interactive_ctx` is public but
documented for debuggers; `create_controller` is not, and neither is the
`write_interactive_run_context` the supervisor uses to drop the context file.
If a future SDK moves them this breaks loudly and immediately, and it is twenty
lines to re-pin -- which is the trade against maintaining a bridge forever.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flyte.models import TaskContext

from . import _names

CONTEXT_SUBPATH = Path(".flyte") / "run_context.json"
CHILDREN_SUBPATH = Path(".flyte") / "children.json"

#: What an action name looks like. Anything else (a glob, a list, a path) is refused rather than
#: interpreted: an abort by pattern is how a project's root action was once killed.
_ACTION_NAME = re.compile(r"[A-Za-z0-9_-]+")


def context_path(base_dir: str | Path) -> Path:
    """Where the supervisor drops the run context for `attach` to find."""
    return Path(base_dir) / CONTEXT_SUBPATH


def write_context(base_dir: str | Path) -> Path:
    """Persist the current task's context so subprocesses can attach to it.

    Called by the supervisor, which *is* in a task. Only the debug-mode VS Code
    path does this normally, so the parameters are assembled from the live
    context rather than read from somewhere.
    """
    import flyte
    from flyte._interactive_run_context import write_interactive_run_context

    ctx = flyte.ctx()
    if not ctx:  # outside a task this is a falsy null context, not None
        raise RuntimeError("write_context must be called from inside a task")

    bundle = ctx.code_bundle
    checkpoints = ctx.checkpoint_paths
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
        "checkpoint_path": checkpoints.checkpoint_path if checkpoints else None,
        "prev_checkpoint": checkpoints.prev_checkpoint_path if checkpoints else None,
        "run_start_time": ctx.run_start_time.isoformat() if ctx.run_start_time else None,
        "tgz": bundle.tgz if bundle else None,
        "pkl": bundle.pkl if bundle else None,
        "dest": bundle.destination if bundle else None,
    }
    return Path(write_interactive_run_context(params, base_dir=base_dir))


def attach(base_dir: str | Path | None = None) -> TaskContext:
    """Join the agent's run, so submitted tasks become child actions of it.

    Returns the restored `TaskContext`. Safe to call more than once.
    """
    import flyte
    from flyte._internal.controllers import create_controller, get_controller

    path = context_path(base_dir) if base_dir is not None else None
    ctx = flyte.load_interactive_ctx(path=path) if path else flyte.load_interactive_ctx()

    # `load_interactive_ctx` installs the context but deliberately creates no
    # controller -- it was written for debuggers, which only read. Submission
    # goes through one, and "rust" authenticates from the API key already in
    # this pod's environment.
    try:
        get_controller()
    except RuntimeError:
        create_controller("rust")

    # Whatever is submitted from here on is this caller's to stop, and nobody else's.
    _install_hook()
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    global _process_sink
    _process_sink = lambda name: record_child(base, ctx.action.name, name)  # noqa: E731 -- replaces, not stacks
    return ctx


# --- who started what ------------------------------------------------------------------------
#
# A session shares its run with the root action, the other sessions and every sub-agent, so "the
# actions of this run" is not "my actions". Aborting by what a listing shows once killed the whole
# project. Instead each caller records the names it submitted, and `abort_children` touches only those.
#
# A caller is a Flyte action name: the one in the context it attached with. Everything in one pod --
# the session and every helper it runs in the same container -- attaches as that pod's action, so they
# share one record and may stop each other's children. That is the granularity Flyte gives. a0 is
# always refused, and so is anything another pod started: only this pod's record is consulted.
#
# Every controller names a child through `convert.generate_sub_action_id_and_output_path`, called as a
# module attribute, so wrapping it sees every submission without touching the controller (which is a
# compiled extension for "rust"). The listing was the alternative: it cannot tell siblings apart.

#: The sink for submissions made anywhere in this process (set by `attach`, so a second call replaces it),
#: and the sinks for those made in one asyncio task tree.
_process_sink: Callable[[str], None] | None = None
_scoped_sinks: ContextVar[tuple[Callable[[str], None], ...]] = ContextVar("child_sinks", default=())


def notify_submitted(name: str) -> None:
    """Tell every interested sink that this process just named a child action."""
    for sink in (_process_sink, *_scoped_sinks.get()):
        if sink is not None:
            sink(name)


def _install_hook() -> None:
    from flyte._internal.runtime import convert

    original = convert.generate_sub_action_id_and_output_path
    if getattr(original, "_records_submissions", False):
        return

    def recording(*args, **kwargs):  # type: ignore[no-untyped-def]  # mirrors the SDK function
        action_id, output_path = original(*args, **kwargs)
        notify_submitted(action_id.name)
        return action_id, output_path

    recording._records_submissions = True  # type: ignore[attr-defined]  # marker read via getattr above
    convert.generate_sub_action_id_and_output_path = recording


@contextlib.contextmanager
def track_submissions(sink: Callable[[str], None]) -> Iterator[None]:
    """Call `sink(name)` for each child action submitted inside the block, by this task and its subtasks."""
    _install_hook()
    token = _scoped_sinks.set((*_scoped_sinks.get(), sink))
    try:
        yield
    finally:
        _scoped_sinks.reset(token)


def children_path(base_dir: str | Path) -> Path:
    """Return the file recording which child actions each caller submitted."""
    return Path(base_dir) / CHILDREN_SUBPATH


def recorded_children(base_dir: str | Path, caller: str) -> list[str]:
    """Return the child action names `caller` has submitted, oldest first."""
    try:
        return list(json.loads(children_path(base_dir).read_text()).get(caller, []))
    except (OSError, ValueError):
        return []


def record_child(base_dir: str | Path, caller: str, name: str) -> None:
    """Add `name` to `caller`'s children. Several processes share the file, so it is locked."""
    path = children_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            known = json.loads(path.read_text())
        except (OSError, ValueError):
            known = {}
        if name in known.get(caller, []):
            return
        known[caller] = [*known.get(caller, []), name]
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".children-")
        with os.fdopen(fd, "w") as f:
            json.dump(known, f)
        os.replace(tmp, path)


class AbortRefused(RuntimeError):
    """The names do not all belong to the caller; nothing was aborted."""


def abort_children(names: Sequence[str], reason: str, base_dir: str | Path | None = None) -> None:
    """Abort exactly these child actions, all of which this caller must have started.

    Refuses the whole batch, aborting nothing, if any name is the root `a0`, the caller's own action,
    not in the caller's record, or not a plain action name (a pattern, a list, a path).
    """
    import flyte
    import flyte.remote as remote

    ctx = flyte.ctx()
    if ctx is None or ctx.action is None:
        raise RuntimeError("abort_children must be called after attach()")
    own, run_name = ctx.action.name, ctx.action.run_name
    mine = recorded_children(base_dir if base_dir is not None else Path.cwd(), own)

    if not names:
        raise AbortRefused("no action names given")
    for name in names:
        if not _ACTION_NAME.fullmatch(name):
            raise AbortRefused(f"{name!r} is not an exact action name; patterns are never accepted")
        if name == "a0":
            raise AbortRefused("a0 is the root action of the project; aborting it kills everything")
        if name == own:
            raise AbortRefused(f"{name} is your own action")
        if name not in mine:
            raise AbortRefused(f"{name} was not started by this action ({own}); it is not yours to abort")
    for name in names:
        # `abort` is an instance method: the action has to be fetched first.
        remote.Action.get(run_name=run_name, name=name).abort(reason=reason)


def main(argv: Sequence[str] | None = None, base_dir: str | Path | None = None) -> int:
    """Run the `python -m flyte_agent_factory.child` command line; return the exit status."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory.child")
    sub = parser.add_subparsers(dest="verb", required=True)
    abort = sub.add_parser("abort", help="abort child actions this session started, by exact name")
    abort.add_argument("names", nargs="+", metavar="name")
    abort.add_argument("--reason", default="Aborted by the session that started it.")
    args = parser.parse_args(argv)

    base = Path(base_dir) if base_dir is not None else Path.cwd()
    if not context_path(base).is_file():
        # The record of who started what lives beside the context, so another directory has neither.
        where = _names.getenv("AGENT_FACTORY_WORKSPACE") or "the workspace root"
        print(f"refused, nothing aborted: no run context here ({base}); run from {where}", file=sys.stderr)
        return 1
    attach(base_dir)
    try:
        abort_children(args.names, args.reason, base_dir)
    except AbortRefused as exc:
        print(f"refused, nothing aborted: {exc}", file=sys.stderr)
        return 1
    print(f"aborted: {' '.join(args.names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
