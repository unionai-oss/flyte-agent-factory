"""Run a pytest lane (`slow` by default) of the project in a raw-container child action with its own CPUs.

`checks.sh` runs its two pytest lanes at once. On one pod they contend for CPU and the slow lane takes
twice as long alone (measured: 8m53s against 4m47s), which put the pre-review past its five minutes.
With `AGENT_FACTORY_SLOW_LANE=child` the pre-review's `checks.sh` sends both lanes to containers of their own:

    python -m flyte_agent_factory._slow_lane --junit <path> [pytest args...]

The lane is the `slow` marker unless the pytest args say otherwise (`-m "not slow"` makes it the fast
lane). Without that variable, or when this process cannot attach to its run, the lane runs here exactly as
before, so CI and local runs are unchanged. The optional `--result` records pytest completion before
report handling. The CLI writes the lane's junit file to `--junit` and exits with pytest's own code,
remote or local.

The child is a `ContainerTask`, not a Python task: a child action runs the *deployed* engine's code bundle,
which lacks this module until the engine is redeployed, so a Python task died on `ModuleNotFoundError`. A raw
container needs nothing from the bundle. It gets the packed working tree and a git bundle of HEAD (so tests
that read git see the repository they see locally), builds the locked environment with `uv sync --frozen`,
installs a pinned Terraform when the image has none (its tests skip without it), uses the gitleaks and tflint
the image bakes, and runs pytest. Any child failure, or a setup failure inside the container, ends in the
local run, except a tree that could not be uploaded after its retries: that lane fails (`UPLOAD_FAILED`).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar, cast

import flyte
import flyte.errors
import httpx
from connectrpc.errors import ConnectError
from flyte.extras import ContainerTask
from flyte.io import File
from obstore.exceptions import BaseError as ObstoreError

#: Same name as `_gate_tests.SLOW_LANE_ENV`, which sets it for the pre-review.
ENV_VAR = "AGENT_FACTORY_SLOW_LANE"
#: How long the spot placement may queue before the lane runs here instead. Short on purpose: a
#: pre-review waiting on the cluster is slower than one that runs the lane itself beside the fast one.
QUEUE_BOUND = timedelta(minutes=2)
#: The longest a lane may run in its container; the full suite takes minutes.
MAX_RUNTIME = timedelta(minutes=30)
#: How much of pytest's output the child returns; the junit file carries the detail.
TAIL_CHARS = 12000
#: The container's exit code when it could not set up the lane (no `uv sync`, no Terraform). Pytest's own
#: codes are 0-5, so this cannot be mistaken for a test result.
SETUP_FAILED = 70
#: What `--no-local` exits with when the lane could not run in a child action: nothing ran. 75 is
#: `EX_TEMPFAIL`; pytest's own codes are 0-5, so a caller can tell it from a test result.
NOT_RUN = 75
#: With an upload that failed `UPLOAD_ATTEMPTS` times, the exit code: the lane did not run, and is not run
#: here either, with or without `--no-local` (EX_IOERR; outside pytest's 0-5, apart from 70 and 75).
UPLOAD_FAILED = 74
#: Attempts at uploading the packed tree, and the wait before the next (times the attempt). Each asks for
#: a fresh presigned URL, so a refused one (an HTTP 400 seen on this cluster) or an expired one is retried;
#: when every attempt fails the lane fails (`UPLOAD_FAILED`) rather than move to the shared pod.
UPLOAD_ATTEMPTS = 4
UPLOAD_BACKOFF_S = 5.0
#: How often the launcher asks the cluster whether its child has ended, and how long it then still waits for
#: the run's watch to say so. The watch can drop and reconnect without the completion: a pre-review's slow
#: child succeeded at 13:41 while its launcher waited until checks.sh's 900 s ran out at 13:48.
POLL_S = 30.0
GRACE_S = 30.0
#: Trees are never packed from these, whatever git lists.
_EXCLUDED_DIRS = {".git", ".venv"}

#: The Terraform the Terraform tests expect (`~> 1.9.0`). The same pin as `_images.TERRAFORM_VERSION` and
#: `TERRAFORM_SHA256`, copied because importing `_images` here would put another module ahead of the junit
#: file (`_gates._TEST_HARNESS`); a test keeps the two equal.
TERRAFORM_VERSION = "1.9.8"
TERRAFORM_SHA256 = "186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8"
TERRAFORM_URL = f"https://releases.hashicorp.com/terraform/{TERRAFORM_VERSION}/terraform_{TERRAFORM_VERSION}_linux_amd64.zip"

#: The container's whole job. Arguments: inputs dir, outputs dir, Terraform zip URL and sha256, and tail size.
#: gitleaks and tflint come baked into the image (`_scanners`): the lane never fetches them.
#: It always exits 0 so that the result is reported; the lane's own exit code is the `code` output.
#: A task input arrives as the file /var/inputs/<name>, an output is the file /var/outputs/<name>.
_LANE_SCRIPT = f"""
in=$1 out=$2 tf_url=$3 tf_sha=$4 tail_bytes=$5
mkdir -p "$out"
work=$(mktemp -d "${{TMPDIR:-/tmp}}/test-lane-XXXXXX")
export PATH="$work/bin:$PATH" UV_CACHE_DIR="$work/uv-cache" UV_PYTHON_DOWNLOADS=never
# The benchmark check uses this cache when the directory exists. The image has none, and without one every
# `terraform init` downloads the AWS provider again.
cache=${{TF_PLUGIN_CACHE_DIR:-/opt/bench-terraform/plugin-cache}}
[[ -d $cache ]] || {{ mkdir -p "$work/plugin-cache"; export TF_PLUGIN_CACHE_DIR="$work/plugin-cache"; }}

install_terraform() {{
  command -v terraform >/dev/null && return 0
  python3 - "$tf_url" "$tf_sha" "$work/bin" <<'PY'
import hashlib, io, os, sys, urllib.request, zipfile
url, sha, dest = sys.argv[1:]
data = urllib.request.urlopen(url, timeout=120).read()
if hashlib.sha256(data).hexdigest() != sha:
    sys.exit("terraform zip: checksum differs from the pinned one")
os.makedirs(dest, exist_ok=True)
zipfile.ZipFile(io.BytesIO(data)).extract("terraform", dest)
os.chmod(os.path.join(dest, "terraform"), 0o755)
PY
}}

# The tree tar carries no `.git`; the history bundle gives the tree its HEAD back, index at HEAD and the
# working tree as packed, so tests that read git (tracked files, HEAD) see what they see in the pod.
restore_history() {{
  git init -q && git fetch -q "$in/history" HEAD && git update-ref HEAD FETCH_HEAD && git reset -q
}}

# Without a history (no bundle, or a checkout with no commit), tests still read the repository: one commit
# tracking every packed file, forced past ignore rules, as the checkout tracks them. No user config.
commit_packed_tree() {{
  local git=(git -c user.name=test-lane -c user.email=test-lane@invalid -c commit.gpgsign=false)
  GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" init -q &&
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" add -A -f &&
    GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 "${{git[@]}}" commit -q --no-verify -m "test lane tree"
}}

setup() {{
  mkdir "$work/tree" && tar xzf "$in/tree" -C "$work/tree" && cd "$work/tree" &&
    {{ if [[ -s $in/history ]]; then restore_history; else commit_packed_tree; fi; }} &&
    uv sync --frozen && install_terraform
}}

# Null-separated, so an argument with a space or a quote reaches pytest as it was given.
mapfile -d '' args < <(
  python3 -c 'import json, sys; sys.stdout.write(chr(0).join(json.load(open(sys.argv[1]))))' "$in/pytest_args"
)
{{
  setup || {{ echo "test-lane: setup failed" >&2; exit {SETUP_FAILED}; }}
  PYTHONPATH=src .venv/bin/python -m pytest tests "${{args[@]}}" -n auto -p no:cacheprovider \\
    "--junitxml=$out/junit"
}} 2>&1 | tee "$work/log"
code=${{PIPESTATUS[0]}}
printf '%s' "$code" >"$out/code"
tail -c "$tail_bytes" "$work/log" >"$out/tail"
[[ -e $out/junit ]] || : >"$out/junit"
exit 0
"""
LANE_COMMAND = ["bash", "-c", _LANE_SCRIPT, "test-lane"]


def _make_lane_task(name: str, interruptible: bool) -> ContainerTask:
    """Return the lane's container task; everything is set here, as `.override()` drops its command."""
    return ContainerTask(
        name=name,
        image=os.environ.get("FLYTE_INTERNAL_IMAGE", "auto"),
        command=LANE_COMMAND,
        arguments=[
            "/var/inputs",
            "/var/outputs",
            TERRAFORM_URL,
            TERRAFORM_SHA256,
            str(TAIL_CHARS),
        ],
        inputs={"tree": File, "history": File, "pytest_args": str},
        # The junit XML is a File: a large suite's report would not fit a scalar output.
        # A scalar output is capped at 1 KiB on the platform, so the tail is a file like the junit.
        outputs={"code": str, "tail": File, "junit": File},
        # 8 CPUs are placeable on a spot node; 12 and 16 found no capacity.
        resources=flyte.Resources(cpu=8, memory="8Gi"),
        interruptible=interruptible,
        # Each attempt is bounded, so a full cluster ends in the fallback instead of an unbounded wait.
        timeout=flyte.Timeout(max_queued_time=QUEUE_BOUND, max_runtime=MAX_RUNTIME),
    )


task = _make_lane_task("test-lane", interruptible=True)
env = flyte.TaskEnvironment.from_task("test-lanes", task)
#: The same lane off spot, for a caller that asks for it (`--on-demand`) when spot has no capacity.
task_on_demand = _make_lane_task("test-lane-on-demand", interruptible=False)
env_on_demand = flyte.TaskEnvironment.from_task("test-lanes-on-demand", task_on_demand)

#: Runs the lane: (exit code, output tail, junit XML text).
Submit = Callable[[list[str], bool], tuple[int, str, str]]


def _listed_files(root: Path) -> list[str]:
    """Return the tracked and untracked-but-not-ignored files, relative to `root`."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [name for name in out.decode().split("\0") if name]


def pack_tree(root: Path) -> bytes:
    """Return a gzipped tar of the repository's working tree, without VCS data or virtualenvs."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in _listed_files(root):
            path = root / name
            # `ls-files` still lists a tracked file that was deleted from the working tree.
            if _EXCLUDED_DIRS & set(Path(name).parts) or not path.exists():
                continue
            tar.add(path, arcname=name, recursive=False)
    return buffer.getvalue()


def pack_history(root: Path) -> bytes:
    """Return a git bundle of the repository's HEAD, or nothing when it has no commit."""
    if subprocess.run(
        ["git", "-C", str(root), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True
    ).returncode:
        return b""
    with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
        bundle = Path(tmp) / "history.bundle"
        subprocess.run(
            ["git", "-C", str(root), "bundle", "create", "-q", str(bundle), "HEAD"],
            check=True,
            capture_output=True,
        )
        return bundle.read_bytes()


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:]


def run_in_tree(root: Path, pytest_args: Sequence[str], python: str = sys.executable) -> tuple[int, str, str]:
    """Run the slow lane in the unpacked tree `root`; return exit code, output tail and junit text."""
    junit = root / ".slow-lane-junit.xml"
    proc = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "tests",
            "-q",
            "-m",
            "slow",
            "-n",
            "auto",
            f"--junitxml={junit}",
            *pytest_args,
        ],
        cwd=root,
        env={**os.environ, "PYTHONPATH": "src"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc.returncode, _tail(proc.stdout), junit.read_text() if junit.exists() else ""


def _attach() -> None:
    from flyte_agent_factory.child import attach

    attach()


class UploadFailed(flyte.errors.RuntimeSystemError):
    """The packed tree could not be uploaded in `UPLOAD_ATTEMPTS` attempts."""


async def _upload_tree(
    tarball: Path,
    upload: Callable[[Path], Awaitable[tuple[str, str]]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """Upload the packed tree; return its remote URI, trying up to `UPLOAD_ATTEMPTS` times."""
    if upload is None:
        from flyte import remote

        # A syncified function: `.aio` is its coroutine form, plain calls would block the loop.
        upload = remote.upload_file.aio
    for attempt in range(1, UPLOAD_ATTEMPTS):
        try:
            _, uri = await upload(tarball)
            return uri
        except flyte.errors.BaseRuntimeError as exc:
            print(
                f"slow lane: upload of the tree failed (attempt {attempt}/{UPLOAD_ATTEMPTS}: {exc}); "
                "retrying",
                file=sys.stderr,
            )
            await sleep(UPLOAD_BACKOFF_S * attempt)
    try:
        _, uri = await upload(tarball)
    except flyte.errors.BaseRuntimeError as exc:
        raise UploadFailed(
            "UploadFailed", f"upload of the tree failed {UPLOAD_ATTEMPTS} times, last: {exc}"
        ) from exc
    return uri


_T = TypeVar("_T")
#: What a remote call raises when the cluster or the network fails: `_errors.REMOTE_ERRORS`, copied because
#: importing `_errors` here would put another module ahead of the junit file (`_gates._TEST_HARNESS`); a test
#: keeps the two equal.
_POLL_ERRORS = (ConnectError, RuntimeError, OSError, ObstoreError, httpx.HTTPError, ValueError, KeyError)


class LaneEnded(flyte.errors.RuntimeSystemError):
    """The child action ended without succeeding, and the watch did not say so."""


async def _settle(
    result: Awaitable[_T],
    finished: Callable[[], Awaitable[_T | None]],
    poll_s: float = POLL_S,
    grace_s: float = GRACE_S,
) -> _T:
    """Await `result`; poll `finished` every `poll_s` and, once it reports the end, wait `grace_s` more.

    `finished` returns None while the child runs, its outputs once it succeeded, and raises `LaneEnded` when
    it ended otherwise. A poll that fails (the network, the API) is not an answer: the wait goes on.
    """
    task = asyncio.ensure_future(result)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_s)
            if done:
                return task.result()
            try:
                found = await finished()
            except LaneEnded:
                done, _ = await asyncio.wait({task}, timeout=grace_s)
                if done:
                    return task.result()
                raise
            except _POLL_ERRORS as exc:
                print(f"slow lane: could not read the child's state ({exc}); still waiting", file=sys.stderr)
                continue
            if found is None:
                continue
            done, _ = await asyncio.wait({task}, timeout=grace_s)
            if done:
                return task.result()
            print(
                "slow lane: the child ended but the run's watch did not say so; using its outputs",
                file=sys.stderr,
            )
            return found
    finally:
        if not task.done():
            task.cancel()


def _read_lane_outputs(literals: dict[str, Any]) -> tuple[str, File, File]:
    """Return the lane's outputs (code, tail, junit) from the child action's output literals."""
    return (
        literals["code"].scalar.primitive.string_value,
        File.from_existing_remote(literals["tail"].scalar.blob.uri),
        File.from_existing_remote(literals["junit"].scalar.blob.uri),
    )


async def _poll_child_outputs(names: list[str]) -> tuple[str, File, File] | None:
    """Ask the cluster about the lane's child action: None while it runs (or is not submitted yet)."""
    from flyte import remote
    from flyte.models import ActionPhase

    if not names:
        return None
    details = await remote.ActionDetails.get.aio(run_name=flyte.ctx().action.run_name, name=names[-1])
    if not details.done():
        return None
    if details.phase != ActionPhase.SUCCEEDED:
        raise LaneEnded("LaneEnded", f"child action {names[-1]} ended {details.phase.value}")
    return _read_lane_outputs(await details.output_literals())


async def _submit_async(
    pytest_args: list[str], interruptible: bool, result: Path | None = None
) -> tuple[int, str, str]:
    from flyte_agent_factory import _slow_lane, child

    # A ContainerTask cannot be overridden, so on-demand is a task of its own.
    lane = _slow_lane.task if interruptible else _slow_lane.task_on_demand
    with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
        tarball = Path(tmp) / "tree.tar.gz"
        tarball.write_bytes(pack_tree(Path.cwd()))
        history = Path(tmp) / "history.bundle"
        history.write_bytes(pack_history(Path.cwd()))
        uri = await _upload_tree(tarball)
        history_uri = await _upload_tree(history)
    uploaded = File.from_existing_remote(uri)
    uploaded_history = File.from_existing_remote(history_uri)

    # A lane that names no marker is the slow lane, as in the local run; a later `-m` wins in pytest.
    args = json.dumps(["-m", "slow", *pytest_args])
    names: list[str] = []
    with child.track_submissions(names.append):
        try:
            # The remote task's declared outputs are not visible to the typechecker.
            code, tail, junit = cast(
                tuple[str, File, File],
                await _settle(
                    lane.aio(tree=uploaded, history=uploaded_history, pytest_args=args),
                    lambda: _poll_child_outputs(names),
                ),
            )
            _write_result(result, int(code))
        finally:
            for name in names:
                print(
                    f"slow lane: child action {name} ({'spot' if interruptible else 'on-demand'})",
                    file=sys.stderr,
                )
    with tempfile.TemporaryDirectory(prefix="slow-lane-") as tmp:
        await junit.download(str(Path(tmp) / "junit"))
        await tail.download(str(Path(tmp) / "tail"))
        return int(code), (Path(tmp) / "tail").read_text(errors="replace"), (Path(tmp) / "junit").read_text()


def _submit(pytest_args: list[str], interruptible: bool, result: Path | None = None) -> tuple[int, str, str]:
    return asyncio.run(_submit_async(pytest_args, interruptible, result))


def _write_result(result: Path | None, code: int) -> None:
    if result is None or type(code) is not int or code not in range(6):
        return
    # A reader must see either no completion or the whole record, even if the launcher is killed.
    result.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=result.parent, delete=False) as record:
        temporary = Path(record.name)
        try:
            json.dump({"completed": True, "code": code}, record)
            record.close()
            temporary.replace(result)
        finally:
            temporary.unlink(missing_ok=True)


def _run_local(junit: Path | None, pytest_args: Sequence[str], result: Path | None = None) -> int:
    record = [f"--junitxml={junit}"] if junit is not None else []
    # The launcher may run on the supervisor's interpreter (it can attach), which has no pytest: the
    # project's own environment runs the tests then.
    python = [sys.executable] if importlib.util.find_spec("pytest") else ["uv", "run", "python"]
    cmd = [*python, "-m", "pytest", "tests", "-q", "-m", "slow", *record, *pytest_args]
    code = subprocess.run(cmd).returncode
    _write_result(result, code)
    return code


def _run_remote(
    junit: Path | None,
    pytest_args: list[str],
    submit: Submit,
    on_demand: bool = False,
    result: Path | None = None,
) -> int | None:
    """Run the lane in a child action; return its exit code, or None if it could not be placed."""
    # Spot first: on-demand capacity is the scarce pool here, so it is tried only when asked for.
    try:
        try:
            code, tail, junit_xml = submit(pytest_args, True)
        except flyte.errors.MaxQueuedTimeExceededError:
            print(f"slow lane: no spot capacity within {QUEUE_BOUND}", file=sys.stderr)
            if not on_demand:
                return None
            code, tail, junit_xml = submit(pytest_args, False)
    except flyte.errors.MaxQueuedTimeExceededError:
        print(f"slow lane: no on-demand capacity within {QUEUE_BOUND}", file=sys.stderr)
        return None
    except UploadFailed as exc:
        # Before the generic case, which it is a kind of: nothing was submitted, and a lane whose tree could
        # not reach the cluster does not move to the shared pod either.
        print(f"slow lane: {exc}; the lane did not run", file=sys.stderr)
        return UPLOAD_FAILED
    except flyte.errors.BaseRuntimeError as exc:
        # The child died before it reported (image pull, a module the bundle lacks): the lane still has
        # to run, and a local run is the same as the queue-bound fallback. Other errors stay loud.
        print(
            f"slow lane: the child action failed before reporting ({type(exc).__name__}: {exc}); "
            "running locally",
            file=sys.stderr,
        )
        return None
    if code == SETUP_FAILED:
        # The container could not build the environment: nothing ran, and pytest never exits with this code.
        print(f"slow lane: setup failed in the child action, running locally\n{tail}", file=sys.stderr)
        return None
    _write_result(result, code)
    if junit is not None:
        junit.parent.mkdir(parents=True, exist_ok=True)
        junit.write_text(junit_xml)
    print(tail, end="" if tail.endswith("\n") else "\n")
    return code


def main(argv: Sequence[str] | None = None, submit: Submit | None = None) -> int:
    """Run the `python -m flyte_agent_factory._slow_lane` command line; return pytest's exit status."""
    parser = argparse.ArgumentParser(prog="python -m flyte_agent_factory._slow_lane")
    parser.add_argument("--junit", type=Path, help="where to write the lane's junit XML")
    parser.add_argument(
        "--result", type=Path, help="where to atomically record pytest completion and exit code"
    )
    parser.add_argument(
        "--no-local",
        action="store_true",
        help=f"when the lane cannot run in a child action, exit {NOT_RUN} instead of running it here",
    )
    parser.add_argument("--on-demand", action="store_true", help="try on-demand when spot has no capacity")
    args, pytest_args = parser.parse_known_args(argv)

    if os.environ.get(ENV_VAR) == "child":
        try:
            _attach()
        # No run context, no controller extension in this interpreter, or no API key: each means run here.
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            print(f"slow lane: running locally, cannot attach to a Flyte run ({exc})", file=sys.stderr)
        else:
            runner = submit or (lambda pytest_args, spot: _submit(pytest_args, spot, args.result))
            code = _run_remote(args.junit, pytest_args, runner, args.on_demand, args.result)
            if code is not None:
                return code
            print("slow lane: running locally, the child action could not be placed", file=sys.stderr)
    if args.no_local:
        # The messages above say "running locally", the default; with this flag the caller decides.
        print(f"slow lane: --no-local, so it was not run here (exit {NOT_RUN})", file=sys.stderr)
        return NOT_RUN
    return _run_local(args.junit, pytest_args, args.result)


if __name__ == "__main__":
    sys.exit(main())
