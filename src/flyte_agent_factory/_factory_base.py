"""What the factory modules share: the deployment profile, and a run's name, phase and labels."""

from __future__ import annotations

import os
from typing import Any

from flyte import remote

from flyte_agent_factory import _deploy, _profile, _queue

PROFILE = _profile.require()


SLOTS = PROFILE.tasks


# What the server accepts for `in_phase`; LIVE also names WAITING for the phase strings readers compare.
SERVER_LIVE = _deploy.LIVE_PHASES
LIVE = ("RUNNING", "QUEUED", "WAITING", "WAITING_FOR_RESOURCES", "INITIALIZING", "PAUSED")


#: Every run the queue starts carries these, so a project's newest run is found on the server by name.
LABEL_PROJECT = "factory-project"
LABEL_GEN = "factory-gen"
#: The engine a shared-environment launch was admitted for, so the per-engine caps can count live projects.
LABEL_ENGINE = "factory-engine"
#: Abort reasons are for the humans reading the console. The server does not hand them back
#: (`abort_info.reason` reads None), so nothing may use one as state: outcomes go in queue markers.
CONTENTION_REASON = "Concurrent factory launches selected the same slot"
DUPLICATE_REASON = "Another run of the same launch request is older"
CANCEL_REASON = "The launch request was cancelled"


def cap() -> int:
    wanted = int(os.environ.get("OPERATE_CAP", str(PROFILE.cap)))
    # One shared environment holds many projects; with slots each holds one.
    return wanted if PROFILE.shared else min(wanted, len(SLOTS))


def _phase(run: remote.Run | remote.Action) -> str:
    return str(run.phase).split(".")[-1]


def _task_name(run: remote.Run) -> str:
    try:
        name: str = run.to_dict()["action"]["metadata"]["task"]["id"]["name"]
        return name
    except (KeyError, TypeError, AttributeError):
        return ""


def report_html(run: remote.Run) -> str:
    return str(run.get_report()) if hasattr(run, "get_report") else ""


def queue_root() -> str:
    if not PROFILE.state.uri:
        raise ValueError("the launch queue needs [state] uri in the deployment profile")
    return _queue.root(PROFILE.state.uri)


def run_labels(run: remote.Run) -> dict[str, str]:
    """Return a run's labels. `flyte.remote.Run` has no `.labels`; they are on its protobuf."""
    return dict(run.pb2.labels)


def _labelled_runs(name: str, gen: int | None = None, **kwargs: Any) -> list[remote.Run]:
    """Return runs of project `name` (of generation `gen` if given), newest first.

    The server filters on the labels.
    """
    labels = {LABEL_PROJECT: name, **({LABEL_GEN: str(gen)} if gen is not None else {})}
    return list(remote.Run.listall(with_labels=labels, sort_by=("created_at", "desc"), **kwargs))


def _live_labelled(name: str, gen: int) -> list[remote.Run]:
    return [r for r in _labelled_runs(name, gen, in_phase=SERVER_LIVE) if _phase(r) in LIVE]


def _oldest_first(runs: list[remote.Run]) -> list[remote.Run]:
    """Return `runs` as the server listed them (newest created first), oldest first.

    Creation order, not `action.start_time`: a run that has not started yet reads as the epoch, which
    would rank the newest, unstarted duplicate first. Every dispatcher sees the same server order.
    """
    return list(reversed(runs))
