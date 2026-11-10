"""Recreating the runs the deployer aborted, and the checkpointed workspace a new run starts from."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import flyte.io
from flyte import remote

from flyte_agent_factory import _deploy, _factory_base, _queue
from flyte_agent_factory._errors import REMOTE_ERRORS

#: How long a queue sweep may wait to be placed (`sweep`'s `max_queued_time`).
SWEEP_QUEUE_BOUND = timedelta(minutes=5)


def _recreate_lookback() -> timedelta:
    """How far back `recreate_aborted` lists aborted runs: two sweep periods plus a sweep's queue bound.

    A run aborted just after one sweep listed must still be listed by the next, even when that sweep
    waited its full queue bound or one tick was missed. A longer look-back costs nothing: a run already
    handled no longer matches the request's `started` marker and is skipped.
    """
    return timedelta(minutes=2 * _deploy.QUEUE_SWEEP_MINUTES) + SWEEP_QUEUE_BOUND


def _aborted_by_own_key(run_name: str, app: str) -> bool:
    """Whether the server names `app`, the client id of the run's own API key, as who aborted the run.

    Read from the root action's details: GetRunDetails never fills in the action's `result`, so
    `Run.details()` has no `abort_info` at all. A session aborts with the key in its pod; the owner at
    the console is a user, and the owner's scripts use their own key. Where either id is missing the
    abort is taken to be the owner's: recreating a run the owner stopped is worse than not recreating
    one a session killed.

    Residual risk: the key in a pod is issued per project and domain, not per run, so every task pod
    there -- other sessions, the factory itself, anything the owner runs in-cluster -- aborts under
    the same client id. Factory aborts leave a marker and are excluded by the caller; an owner who
    aborts a run from inside a pod of the same domain would see it recreated, once.
    """
    if not app:
        return False
    try:
        info = remote.ActionDetails.get(run_name=run_name, name="a0").abort_info
    except REMOTE_ERRORS:
        return False
    if info is None or not info.aborted_by.HasField("application"):
        return False
    subject: str = info.aborted_by.application.id.subject
    return subject == app


def recreate_aborted() -> None:
    """Put back in the queue each project whose run was just aborted from inside the run.

    A started request is closed, and nothing else looks at its run again. A run the queue started that
    went ABORTED within `_recreate_lookback()` is reopened if the server names the run's own API key as
    who aborted it (`_aborted_by_own_key`) and the factory did not abort it (every factory abort leaves
    a marker naming the run, which makes it not the `started` run). The request then waits as attempt
    n+1, with `recreated_from` for `_recreated_workspace`. At most once per request (project and gen),
    ever, so a project that keeps aborting itself is not started forever; and not while a run of the
    request is live, which a dispatcher racing this one may already have started.

    Apart from `dispatch`, which an empty queue makes free: this asks Flyte for the aborted runs of
    every slot, which only the periodic sweep should pay for.
    """
    if _factory_base.PROFILE.planner.admission == "planner" or not _factory_base.PROFILE.state.uri:
        return
    queue = _factory_base.queue_root()
    after = datetime.now(timezone.utc) - _recreate_lookback()
    for slot in _factory_base.SLOTS:
        aborted = remote.Run.listall(
            task_name=slot,
            in_phase=("ABORTED",),
            limit=20,
            sort_by=("created_at", "desc"),
            updated_at=remote.TimeFilter(after=after),
        )
        for run in aborted:
            labels = _factory_base.run_labels(run)
            if (
                _factory_base._phase(run) != "ABORTED"
                or _factory_base.LABEL_PROJECT not in labels
                or not labels.get(_factory_base.LABEL_GEN, "").isdigit()
            ):
                continue
            name, gen = labels[_factory_base.LABEL_PROJECT], int(labels[_factory_base.LABEL_GEN])
            entry = next((e for e in _queue.entries(queue, name) if e.gen == gen), None)
            if entry is None or entry.closed != "started":
                continue
            if _queue.marker(queue, name, gen, "started").get("run") != run.name:
                continue
            if not _aborted_by_own_key(
                run.name, _queue.read_app_client_id(_factory_base.PROFILE.state.uri, run.name)
            ):
                continue
            recreated = {
                _queue.marker(queue, name, gen, f"attempt-{n}").get("recreated_from")
                for n in range(1, entry.attempt + 1)
            } - {None, ""}
            if recreated - {run.name}:
                continue  # this request was recreated once already; its second internal abort is final
            if _factory_base._live_labelled(name, gen):
                continue
            if run.name not in recreated:
                _queue.mark(
                    queue,
                    name,
                    gen,
                    f"attempt-{entry.attempt + 1}",
                    aborted_run=run.name,
                    recreated_from=run.name,
                )
            _queue.reopen(queue, name, gen)  # after the marker: a crash between them is retried
            print(f"[queue] {name}: {run.name} was aborted from inside the run; recreating it", flush=True)


def _load_checkpoint_workspace(location: str) -> flyte.io.Dir | None:
    """Return the workspace in the checkpoint saved at `location` (see `_state.save_checkpoint`), if any."""
    checkpoint = flyte.Checkpoint(checkpoint_dest=location, checkpoint_src=location)
    tree = checkpoint.load_sync()
    if tree is None or not (tree / "workspace").is_dir():
        return None
    # `from_local` is syncified: it blocks here, but the SDK types it as a coroutine function.
    return flyte.io.Dir.from_local(str(tree / "workspace"))  # type: ignore[return-value]  # see above: syncified


def _recreated_workspace(queue: str, entry: _queue.Entry) -> tuple[flyte.io.Dir | None, str] | None:
    """(workspace_in, note) for an entry whose run is being recreated after an internal abort, else None.

    The aborted run published nothing -- only a SUCCEEDED run has outputs -- but its project saved
    checkpoints, so the workspace is read from the last one.
    """
    for n in range(entry.attempt, 0, -1):
        source = _queue.marker(queue, entry.name, entry.gen, f"attempt-{n}").get("recreated_from")
        if source:
            break
    else:
        return None
    workspace = None
    location = _queue.read_checkpoint_uri(_factory_base.PROFILE.state.uri, source)
    if location:
        try:
            workspace = _load_checkpoint_workspace(location)
        except REMOTE_ERRORS as exc:  # an unreadable checkpoint must not keep the project down
            print(f"[queue] {entry.name}: checkpoint of {source} unreadable: {exc}", flush=True)
    resumed = (
        "this run resumes from the workspace in its last checkpoint"
        if workspace is not None
        else "no checkpoint of it could be read, so this run starts from an empty workspace"
    )
    return workspace, (
        f"\n\n(Operator note: the previous run of {entry.name}, {source}, was aborted from inside the "
        f"run -- not by the owner and not by the factory -- and was recreated; {resumed}. Check what "
        "state it left behind before relaunching anything, and never abort `a0`.)"
    )


def _keep_oldest(entry: _queue.Entry, run: remote.Run) -> remote.Run:
    """Abort every live run of this request but the oldest; the one kept.

    Dispatchers are serialised only approximately (the lease), and Flyte does not refuse a second run
    of the same request, so two of them can each create one. They find each other here by label.
    """
    listed = _factory_base._live_labelled(entry.name, entry.gen)
    # Ours may not be listed yet, in which case every listed run is older.
    runs = (
        _factory_base._oldest_first(listed)
        if any(r.name == run.name for r in listed)
        else [*_factory_base._oldest_first(listed), run]
    )
    for loser in runs[1:]:
        loser.abort(reason=_factory_base.DUPLICATE_REASON)
    return runs[0]


#: Runs of a project looked at when searching for a workspace to resume; a project has few runs.
RESUME_CANDIDATES = 20


def _workspace_for(request: _queue.Request) -> tuple[flyte.io.Dir | None, str]:
    """(workspace_in, note) for a request: the newest earlier run's published workspace, or why there is none.

    Only a SUCCEEDED run has outputs: a FAILED, ABORTED (cancelled, or a dedupe loser) newest run must not
    hide an older good workspace, so the runs are walked newest first until one yields a `Dir`. The search is
    here, at dispatch, and not in the request: outputs are read as late as possible, and a run that fails to
    read just hands over to the next older one.
    """
    if request.kind != "resume":
        return None, ""
    reason = "no earlier run of it published a workspace"
    candidates = [
        r
        for r in _factory_base._labelled_runs(request.name, limit=RESUME_CANDIDATES)
        if _factory_base._phase(r) == "SUCCEEDED"
        and _factory_base.run_labels(r).get(_factory_base.LABEL_GEN) != str(request.gen)
    ]
    for run in candidates:
        try:
            workspace = run.outputs()[1]
        except (
            *REMOTE_ERRORS,
            IndexError,
        ) as exc:  # an unreadable run is skipped; a resume must still run, fresh if need be
            reason = f"the outputs of {run.name} could not be read ({type(exc).__name__})"
            continue
        if isinstance(workspace, flyte.io.Dir):
            return workspace, ""
        reason = f"{run.name} published no workspace"
    return None, (
        f"\n\n(Operator note: the workspace of the previous run of {request.name} could not be "
        f"resumed because {reason}; this run starts from an empty workspace.)"
    )
