"""One project's runs, read and acted on from the factory tasks: its name, its queue entry, its inbox.

Split from `_factory` (that module is at its size limit); `_factory` re-exports these names.
"""

from __future__ import annotations

from flyte import remote

from flyte_agent_factory import _factory_base, _queue
from flyte_agent_factory._errors import REMOTE_ERRORS


def project_name(run: remote.Run) -> str:
    try:
        inputs = run.inputs()
    except REMOTE_ERRORS:  # a label for a listing; the run name is shown beside it
        return ""
    # `Action.inputs()` is `cast(None)`, not an empty mapping, when the response carried no inputs.
    if inputs is None:
        return ""  # type: ignore[unreachable]  # the SDK types `inputs()` non-optional but returns None
    try:
        return str(inputs["name"])
    except KeyError:
        return ""


def cancel(name: str) -> dict[str, list[int]]:
    """Remove `name` from the queue: {cancelled: [gen...]}.

    Writes the marker and then aborts any live run of the request; the dispatcher checks the marker,
    creates the run and checks the marker again. Whichever order they interleave in, one of the two
    sees the other's write, so no run of a cancelled request stays live. A cancel that crosses a dispatch
    can leave both `.started` and `.cancelled`; `_queue.entries` reads the later one (see `_outcome`).
    """
    queue = _factory_base.queue_root()
    waiting = _queue.pending(queue, name)
    if not waiting:
        raise ValueError(f"{name!r} is not queued")
    cancelled = []
    for entry in waiting:
        _queue.mark(queue, name, entry.gen, "cancelled")
        aborted = []
        for run in _factory_base._live_labelled(name, entry.gen):
            run.abort(reason=_factory_base.CANCEL_REASON)
            aborted.append(run.name)
        if aborted:
            _queue.mark(queue, name, entry.gen, "cancelled", aborted_run=", ".join(aborted))
        cancelled.append(entry.gen)
    return {"cancelled": cancelled}


def direct_condition(run: remote.Run) -> str:
    """Return the name of the run's open `direct-` condition (its owner inbox); empty if there is none."""
    for condition in remote.Condition.listall(run_name=run.name):
        if condition.name.startswith("direct-") and "PAUSED" in str(condition.phase):
            return str(condition.name)
    return ""
