"""Running the target repo's CI as a GitHub runner would: its environment, its toolchains, its base.

`_gates` decides what a result means; this module runs one command of the target's CI and says what happened.
The pod's environment (`_U_*`, `FLYTE_*`, `USER_LOG_LEVEL`, credentials) is not a runner's, and upstream's own
tests and CLIs read it, so a job gets a runner-like allowlist instead of `os.environ`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import glob
import itertools
import os
import re
import shlex
import shutil
import tempfile
import urllib.request
from collections.abc import Iterator, Sequence
from pathlib import Path

from . import _gate_base, _gate_start, _secret_env, _target_ci, _target_tools

#: Each command of the target repo's own CI whose step and job set no `timeout-minutes`. Its lint and tests
#: run at their full length, as they do upstream.
TARGET_CI_TIMEOUT = 900
DEFAULT_BRANCHES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")

#: What a runner's shell has from the machine. Everything else of the pod stays out.
_ALLOWED = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "TERM",
    "TZ",
    "TMPDIR",
    # Where tools keep their caches and toolchains; a clean cache would mean downloading everything again.
    "XDG_CACHE_HOME",
    "UV_CACHE_DIR",
    "CARGO_HOME",
    "RUSTUP_HOME",
    # The browsers Playwright drives: a workflow's step installing them is setup, which the gate does not run.
    "PLAYWRIGHT_BROWSERS_PATH",
    # How this machine reaches the network. Not credentials, unless a proxy URL carries them (see below).
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
}
_PROXY_VARS = {name for name in _ALLOWED if "proxy" in name.lower()}


def runner_env(checkout: Path, job_state: Path, repo: str, sha: str) -> dict[str, str]:
    """Return the environment of a job on a runner: the allowlisted basics and the `GITHUB_*` variables.

    `job_state` holds the files the steps of one job share (`$GITHUB_ENV`, `$GITHUB_PATH`, `$GITHUB_OUTPUT`,
    `$GITHUB_STEP_SUMMARY`); they are created empty here, so every job has its own.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        # A proxy URL with `user:password@` is a credential.
        if (name in _ALLOWED or name.startswith("LC_")) and not (name in _PROXY_VARS and "@" in value)
    }
    files = {name: job_state / name for name in ("env", "path", "output", "summary")}
    temp = job_state / "temp"
    temp.mkdir(parents=True, exist_ok=True)
    for path in files.values():
        path.touch()
    env.update(
        CI="true",
        GITHUB_ACTIONS="true",
        RUNNER_OS="Linux",
        RUNNER_ARCH="X64",
        RUNNER_TEMP=str(temp),
        RUNNER_TOOL_CACHE=str(job_state / "toolcache"),
        GITHUB_WORKSPACE=str(checkout),
        GITHUB_SHA=sha,
        GITHUB_REF="refs/pull/1/merge",
        GITHUB_BASE_REF="main",
        GITHUB_HEAD_REF="feature",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_ENV=str(files["env"]),
        GITHUB_PATH=str(files["path"]),
        GITHUB_OUTPUT=str(files["output"]),
        GITHUB_STEP_SUMMARY=str(files["summary"]),
    )
    if repo:
        env["GITHUB_REPOSITORY"] = _target_ci.repo_name(repo)
    # What earlier steps exported: `$GITHUB_ENV` rows, and `$GITHUB_PATH` directories (newest first).
    env.update(_target_ci.parse_env_file(files["env"].read_text()))
    directories = [d for d in files["path"].read_text().splitlines() if d]
    env["PATH"] = os.pathsep.join([*reversed(directories), env.get("PATH", "")])
    return env


@dataclasses.dataclass(frozen=True)
class Run:
    """What running one command came to.

    `not_run` is why it could not run at all (no toolchain): nothing is known about the project then.
    `final` marks a failure that is not a test result (the gate cannot run the step): not worth rerunning
    on the base. `root` is the checkout it ran in, so paths can be compared with another run's. `skipped` is
    why a step was not started at all (an earlier step of its job failed), as GitHub skips it.
    """

    code: int
    output: str
    root: str = ""
    not_run: str = ""
    final: bool = False
    skipped: str = ""


def target_base(checkout: Path) -> str:
    """Return the commit the target checkout began at, its merge-base with the default branch ("" if none)."""
    for ref in DEFAULT_BRANCHES:
        code, base = _gate_base._run(["git", "merge-base", "HEAD", ref], checkout)
        if not code and base.strip():
            return base.strip()
    return ""


def execute(
    item: _target_ci.Command, checkout: Path, job_state: Path | None, repo: str, artifacts: Path | None = None
) -> Run:
    """Run one script of the target's CI as its runner would, with its whole output.

    `job_state` is a directory shared by the steps of one job: they append to its `env` and `path` files
    through `$GITHUB_ENV` and `$GITHUB_PATH`, and what earlier steps wrote reaches this one. `artifacts` is
    the run's artifact storage, shared by its jobs (None: a scratch one, as for a step run on its own).
    """
    if item.unrunnable:
        return Run(1, item.unrunnable, str(checkout), final=True)
    with tempfile.TemporaryDirectory(prefix="factory-target-ci-", ignore_cleanup_errors=True) as scratch:
        code, sha = _gate_base._run(["git", "rev-parse", "HEAD"], checkout)
        env = runner_env(
            checkout, job_state or Path(scratch) / "job", repo, sha.strip() if not code else "0" * 40
        )
        # A step's own `env:` beats what earlier steps exported. `${{ runner.temp }}` and
        # `${{ github.workspace }}` are this job's paths.
        env.update([(name, _target_ci.job_paths(value, env)) for name, value in item.env])
        if item.action:
            inputs = {name: _target_ci.job_paths(value, env) for name, value in item.inputs}
            return _artifact_step(item.action, inputs, checkout, artifacts or Path(scratch) / "artifacts")
        command = _target_ci.job_paths(item.command, env)
        if item.shell in _target_ci.SHELLS:
            argv = [*_target_ci.SHELLS[item.shell], command]
        else:
            # A shell template runs literally, as on GitHub: the script is a file, `{0}` is its path.
            script = Path(scratch) / ("script.py" if item.shell.startswith("python") else "script.sh")
            script.write_text(command)
            argv = shlex.split(item.shell.replace("{0}", shlex.quote(str(script))))
        cwd = checkout / _target_ci.job_paths(item.cwd, env) if item.cwd else checkout

        def launch() -> tuple[int, str]:
            return _gate_base._run(argv, cwd, env=env, timeout=item.timeout or TARGET_CI_TIMEOUT, tail=None)

        if reason := _target_tools.prepare(env, item.uv, item.python):
            return Run(0, "", str(checkout), not_run=reason)
        wanted = bool(item.rust or item.toolchain_steps) or bool(_NEEDS_RUST.search(item.command))
        if wanted and (reason := ensure_rust(env, item.rust, item.toolchain_steps, cwd)):
            return Run(0, "", str(checkout), not_run=reason)
        code, output = launch()
        # A script or Makefile can call cargo without the step saying so, and `make` says it with exit 2.
        if code and not wanted and _RUST_MISSING.search(output):
            if reason := ensure_rust(env, item.rust, item.toolchain_steps, cwd):
                return Run(0, "", str(checkout), not_run=reason)
            code, output = launch()
    return Run(code, output, str(checkout))


def run_job(
    items: Sequence[_target_ci.Command], checkout: Path, job_state: Path, repo: str, artifacts: Path | None
) -> list[tuple[_target_ci.Command, Run]]:
    """Run the steps of one job in order; once one fails, the rest come back skipped unless their `if:` says.

    Only a step whose `if:` runs it anyway (`always()`, `failure()`) still runs: GitHub reads any other `if:`
    as `success() && ...`.

    A step the gate could not run (`not_run`, a toolchain missing here) is not a failure: on a runner it
    would have run, and the later steps' own failures still count.
    """
    steps: list[tuple[_target_ci.Command, Run]] = []
    failed = ""
    for item in items:
        if failed and not item.runs_after_failure:
            steps.append((item, Run(0, "", str(checkout), skipped=f"an earlier step failed (`{failed}`)")))
            continue
        run = execute(item, checkout, job_state, repo, artifacts)
        if run.code and not failed:
            failed = item.command
        steps.append((item, run))
    return steps


def _job_key(workflow: str, job: str) -> str:
    """Return a name for a workflow's job, usable as a directory name."""
    return re.sub(r"\W", "_", f"{workflow}-{job}")


def _key_of(item: _target_ci.Command) -> str:
    # The origin starts with the workflow file (a merged check's lists it first).
    return _job_key(item.origin.partition(":")[0], item.job)


def _needs_order(jobs: dict[str, list[_target_ci.Command]]) -> list[str]:
    """Return the job keys so that each comes after the jobs it needs; ties keep the workflow's order.

    A need on a job that is not here (it had nothing to run) holds nothing back, nor does a cycle, which
    GitHub would reject outright.
    """
    needs = {
        key: {_job_key(items[0].origin.partition(":")[0], need) for need in items[0].needs} - {key}
        for key, items in jobs.items()
    }
    order: list[str] = []
    waiting = list(jobs)
    while waiting:
        ready = next((key for key in waiting if not needs[key] & set(waiting)), waiting[0])
        waiting.remove(ready)
        order.append(ready)
    return order


def run_jobs(
    checkout: Path, commands: Sequence[_target_ci.Command], repo: str, scratch: Path
) -> tuple[list[list[tuple[_target_ci.Command, Run]]], list[str]]:
    """Run the discovered steps job by job, in `needs` order, each job in a fresh worktree of HEAD.

    The steps of one job share `$GITHUB_ENV` and one copy of the checkout, as on a runner: a formatter step
    edits that copy, not the session's files. The jobs share `scratch/artifacts`, the run's artifact storage.
    A job one of whose needed jobs failed or was skipped is skipped too, as GitHub skips it, unless its `if:`
    says to run anyway. Returns each job's (step, run) pairs, and what was skipped and why. Raises
    `WorktreeUnavailable`.
    """
    jobs: dict[str, list[_target_ci.Command]] = {}
    for item in commands:
        jobs.setdefault(_key_of(item), []).append(item)
    outcome: dict[str, str] = {}  # "" for a job that passed, else what became of it
    ran: list[list[tuple[_target_ci.Command, Run]]] = []
    skipped: list[str] = []
    for key in _needs_order(jobs):
        first = jobs[key][0]
        workflow = first.origin.partition(":")[0]
        held = [
            f"needed job `{need}` {outcome[needed]}"
            for need in first.needs
            if outcome.get(needed := _job_key(workflow, need))
        ]
        if held and not first.job_runs_after_failure:
            outcome[key] = "was skipped"
            skipped.append(f"skipped job `{first.job}` ({workflow}): {held[0]}")
            continue
        with _open_worktree(checkout, "HEAD", "factory-target-job-") as copy:
            steps = run_job(jobs[key], copy, scratch / key, repo, scratch / "artifacts")
        ran.append(steps)
        # As for a step, a job that only could not run here is not a failure its dependents stop for.
        outcome[key] = "failed" if any(run.code for _, run in steps) else ""
        skipped.extend(
            f"skipped `{item.command}` ({item.origin}): {run.skipped}" for item, run in steps if run.skipped
        )
    return ran, skipped


# -- artifacts --


def _artifact_step(action: str, inputs: dict[str, str], checkout: Path, artifacts: Path) -> Run:
    """Do offline what `actions/upload-artifact` or `actions/download-artifact` does on GitHub.

    `artifacts` holds one directory per artifact name. Only the actions' standard inputs are read: upload's
    `name`, `path`, `if-no-files-found`, `overwrite`, `include-hidden-files`; download's `name`, `pattern`,
    `path`, `merge-multiple`. Relative paths are the workspace's, as on a runner.
    """
    if action == "upload-artifact":
        code, output = _upload(inputs, checkout, artifacts)
    else:
        code, output = _download(inputs, checkout, artifacts)
    return Run(code, output, str(checkout))


def _workspace_path(text: str, checkout: Path) -> Path:
    return checkout / Path(text.strip()).expanduser()


def _matches(pattern: Path) -> list[Path]:
    """Return what a path or glob of the upload's `path` names; a directory stands for every file in it."""
    if glob.has_magic(str(pattern)):
        found = [Path(p) for p in glob.glob(str(pattern), recursive=True, include_hidden=True)]
    else:
        found = [pattern] if pattern.exists() else []
    return [f for p in found for f in ([p] if p.is_file() else sorted(p.rglob("*"))) if f.is_file()]


def _search_root(pattern: Path) -> Path:
    """Return the part of a path before its first glob segment, as `@actions/glob` searches from it."""
    literal = list(itertools.takewhile(lambda part: not glob.has_magic(part), pattern.parts))
    return Path(*literal)


def _upload(inputs: dict[str, str], checkout: Path, artifacts: Path) -> tuple[int, str]:
    name = inputs.get("name") or "artifact"
    # GitHub refuses these; here they would also write outside the run's storage.
    if not re.fullmatch(r"[^/\\:<>|*?\"\r\n]+", name) or name in (".", ".."):
        return 1, f"invalid artifact name `{name}`"
    rows = [row.strip() for row in inputs.get("path", "").splitlines() if row.strip()]
    patterns = [_workspace_path(row, checkout) for row in rows if not row.startswith("!")]
    excluded = {
        f for row in rows if row.startswith("!") for f in _matches(_workspace_path(row[1:], checkout))
    }
    files = sorted({f for pattern in patterns for f in _matches(pattern)} - excluded)
    # upload-artifact's root: the one search path (a lone file's directory), else the paths' common ancestor.
    roots = [_search_root(pattern) for pattern in patterns]
    if len(roots) == 1:
        root = roots[0].parent if files == [roots[0]] else roots[0]
    else:
        root = Path(os.path.commonpath(roots)) if roots else checkout
    if inputs.get("include-hidden-files", "false").lower() != "true":
        files = [f for f in files if not any(part.startswith(".") for part in f.relative_to(root).parts)]
    if not files:
        path = inputs.get("path", "")
        message = f"No files were found with the provided path: {path}. No artifacts will be uploaded."
        return (1 if inputs.get("if-no-files-found", "warn").lower() == "error" else 0), message
    target = artifacts / name
    if target.exists():
        if inputs.get("overwrite", "false").lower() != "true":
            return 1, f"an artifact named `{name}` already exists in this run"
        shutil.rmtree(target)
    for found in files:
        copy = target / found.relative_to(root)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, copy)
    return 0, f"uploaded artifact `{name}`: {len(files)} file(s) from {root}"


def _download(inputs: dict[str, str], checkout: Path, artifacts: Path) -> tuple[int, str]:
    destination = _workspace_path(inputs.get("path") or ".", checkout)
    available = sorted(d.name for d in artifacts.iterdir() if d.is_dir()) if artifacts.is_dir() else []
    name = inputs.get("name")
    if name:
        if name not in available:
            return 1, f"Unable to download artifact(s): Artifact not found for name: {name}"
        chosen = [name]
    else:
        pattern = inputs.get("pattern")
        chosen = [a for a in available if not pattern or fnmatch.fnmatchcase(a, pattern)]
    # As download-artifact lays them out: one named artifact's files, or every artifact merged, go straight
    # into `path`; several unmerged ones each into `path/<name>/`.
    flat = bool(name) or inputs.get("merge-multiple", "false").lower() == "true"
    for artifact in chosen:
        shutil.copytree(
            artifacts / artifact, destination if flat else destination / artifact, dirs_exist_ok=True
        )
    return 0, f"downloaded {len(chosen)} artifact(s) to {destination}: " + ", ".join(chosen)


def report(item: _target_ci.Command, run: Run) -> str:
    """Return the failure report of a run, or "" if it passed. A run that could not happen is labelled."""
    if run.not_run:
        return f"NOT RUN `{item.command}` ({item.origin}): {run.not_run}"
    if run.final:
        return f"`{item.command}` ({item.origin}): {run.output}"
    if not run.code:
        return ""
    tail = run.output[-3000:]
    # stderr follows stdout: an installer's chatter can push pytest's summary of what failed out of the tail.
    lost = [row for row in run.output[:-3000].splitlines() if _PYTEST_SUMMARY.match(row)][-20:]
    kept = "".join(f"{row}\n" for row in lost) + "...\n" if lost else ""
    return f"`{item.command}` ({item.origin}) exited {run.code}:\n{kept}{tail}"


#: pytest's short test summary rows and its final count line.
_PYTEST_SUMMARY = re.compile(r"(FAILED|ERROR) \S|=+ .*\b(failed|errors?)\b.* in [\d.]+s")


# -- toolchains --

_NEEDS_RUST = re.compile(r"\b(cargo|rustup|rustc|maturin)\b")
_RUST_MISSING = re.compile(
    r"\b(cargo|rustup|rustc)\b.*(?:not found|No such file or directory)|not found.*\b(cargo|rustup|rustc)\b"
)
_RUSTUP_TIMEOUT = 900


def _download_rustup_init() -> bytes:
    """Fetch rustup's installer script. Its own function so a test can take the network away."""
    with urllib.request.urlopen("https://sh.rustup.rs", timeout=60) as response:
        data: bytes = response.read()
    return data


def ensure_rust(
    env: dict[str, str], spec: Sequence[str], steps: Sequence[str] = (), cwd: Path | None = None
) -> str:
    """Make `cargo` and `rustup` runnable in `env` (edited in place); return why not, or "".

    An installed toolchain is used: on `PATH`, else `~/.cargo/bin`. Otherwise rustup is installed, as it is
    preinstalled on a runner, into a cache under `$HOME`, so the next run finds it. Then the job's own
    `rustup ...` scripts (`steps`, run in `cwd`) and the toolchain and components of `spec`, which is
    `(toolchain, *components)` or empty, are set up before its checks.
    """
    key = ("rust", env.get("HOME", ""), env.get("PATH", ""), tuple(spec), tuple(steps), cwd)
    return _target_tools.once(key, env, lambda: _ensure_rust(env, spec, steps, cwd))


def _ensure_rust(env: dict[str, str], spec: Sequence[str], steps: Sequence[str], cwd: Path | None) -> str:
    if not env.get("HOME"):
        return "HOME is not set: nowhere to keep a Rust toolchain"
    home = Path(env["HOME"])
    cached = home / ".cache/flyte-agent-factory/rust"
    for bin_dir in (home / ".cargo/bin", cached / "cargo/bin"):
        if bin_dir.is_dir() and str(bin_dir) not in env["PATH"].split(os.pathsep):
            env["PATH"] += os.pathsep + str(bin_dir)
            if bin_dir == cached / "cargo/bin":
                env.update(CARGO_HOME=str(cached / "cargo"), RUSTUP_HOME=str(cached / "rustup"))
    toolchain, components = (spec[0] if spec else ""), list(spec[1:])
    if not shutil.which("rustup", path=env["PATH"]):
        if shutil.which("cargo", path=env["PATH"]):
            return ""  # a system cargo without rustup: nothing to name a toolchain with
        if failure := _install_rustup(env, cached):
            return failure
    # A runner's rustup comes with stable as its default toolchain, with clippy and rustfmt, which steps
    # like a bare `rustup toolchain install` or `cargo clippy` count on.
    if failure := _prepare_toolchain(env, "", ["clippy", "rustfmt"]):
        return failure
    for script in steps:
        code, output = _gate_base._run(
            [*_target_ci.SHELLS["bash"], script], cwd or Path(env["HOME"]), env=env, timeout=_RUSTUP_TIMEOUT
        )
        if code:
            first = script.splitlines()[0]
            return f"the job's toolchain setup `{first}` failed with exit {code}:\n{output[-1500:]}"
    return _prepare_toolchain(env, toolchain, components)


def _install_rustup(env: dict[str, str], cached: Path) -> str:
    try:
        installer = _download_rustup_init()
        cached.mkdir(parents=True, exist_ok=True)
        script = cached / "rustup-init.sh"
        script.write_bytes(installer)
    except OSError as exc:
        return f"no cargo or rustup on PATH or in ~/.cargo/bin, and rustup cannot be downloaded: {exc}"
    env.update(CARGO_HOME=str(cached / "cargo"), RUSTUP_HOME=str(cached / "rustup"))
    code, output = _gate_base._run(
        [
            "sh",
            str(script),
            "-y",
            "--no-modify-path",
            "--default-toolchain",
            "none",  # the job's own steps (or `_prepare_toolchain`) install what it uses
        ],
        cached,
        env=env,
        timeout=_RUSTUP_TIMEOUT,
    )
    if code:
        return f"installing rustup failed with exit {code}:\n{output[-1500:]}"
    env["PATH"] += os.pathsep + str(cached / "cargo/bin")
    return ""


def _rustup(env: dict[str, str], *args: str) -> tuple[int, str]:
    return _gate_base._run(["rustup", *args], Path(env["HOME"]), env=env, timeout=_RUSTUP_TIMEOUT)


def _prepare_toolchain(env: dict[str, str], toolchain: str, components: list[str]) -> str:
    """Install the named toolchain (else a default one) and its components into rustup, if not yet there."""
    code, listing = _rustup(env, "toolchain", "list")
    installed = [row.split()[0] for row in listing.splitlines() if row.strip()] if not code else []
    wanted = toolchain or "stable"
    if not any(name.startswith(wanted) for name in installed):
        code, output = _rustup(env, "toolchain", "install", wanted, "--profile", "minimal")
        if code:
            return f"`rustup toolchain install {wanted}` failed with exit {code}:\n{output[-1500:]}"
    if toolchain:
        env["RUSTUP_TOOLCHAIN"] = toolchain
    elif not any("(default)" in row for row in listing.splitlines()):
        code, output = _rustup(env, "default", wanted)
        if code:
            return f"`rustup default {wanted}` failed with exit {code}:\n{output[-1500:]}"
    code, have = _rustup(
        env, "component", "list", "--installed", *(["--toolchain", toolchain] if toolchain else [])
    )
    missing = [c for c in components if code or not re.search(rf"^{re.escape(c)}-", have, re.MULTILINE)]
    if missing:
        code, output = _rustup(
            env, "component", "add", *missing, *(["--toolchain", toolchain] if toolchain else [])
        )
        if code:
            return f"`rustup component add {' '.join(missing)}` failed with exit {code}:\n{output[-1500:]}"
    return ""


# -- the unpatched base --


class BaseUnavailable(Exception):
    """The target's base could not be checked out; the message says why."""


class WorktreeUnavailable(Exception):
    """A commit of the checkout could not be checked out into a worktree; the message is git's output."""


@contextlib.contextmanager
def _open_worktree(checkout: Path, commit: str, prefix: str) -> Iterator[Path]:
    """Yield a detached worktree of `commit`, removed afterwards; raise `WorktreeUnavailable`."""
    with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / "checkout"
        code, output = _gate_base._run(["git", "worktree", "add", "--detach", str(copy), commit], checkout)
        if code:
            raise WorktreeUnavailable(output.strip())
        try:
            yield copy
        finally:
            # The worktree is registered in the checkout's .git: leave nothing of it there.
            _gate_base._run(["git", "worktree", "remove", "--force", str(copy)], checkout)
            _gate_base._run(["git", "worktree", "prune"], checkout)


@contextlib.contextmanager
def base_copy(checkout: Path) -> Iterator[Path]:
    """Yield a worktree of the commit the checkout began at, removed afterwards; raise `BaseUnavailable`."""
    base = target_base(checkout)
    if not base:
        raise BaseUnavailable(
            "the checkout has no merge-base with its default branch, so the base is unavailable"
        )
    try:
        with _open_worktree(checkout, base, "factory-target-base-") as copy:
            yield copy
    except WorktreeUnavailable as exc:
        raise BaseUnavailable(f"cannot check out the base {base[:12]}: {exc}") from exc


def runs_on_base(
    checkout: Path, items: Sequence[_target_ci.Command], repo: str, artifacts: Path | None = None
) -> tuple[list[Run], set[str]]:
    """Run `items`, the steps of one job in order, on the unpatched base; raises `BaseUnavailable`.

    Also returns the commands the base's own CI runs: a failing command the patch added (or changed) is the
    patch's, however the base fares when made to run it. The jobs the steps' job needs are not run again on
    the base: it downloads what they uploaded on the head (`artifacts`), from a copy its own uploads replace.
    """
    with (
        base_copy(checkout) as copy,
        tempfile.TemporaryDirectory(prefix="factory-target-base-job-", ignore_cleanup_errors=True) as state,
    ):
        own = {c.command for c in _target_ci.discover(copy, None, repo)}
        stored = Path(state) / "artifacts"
        if artifacts is not None and artifacts.is_dir():
            shutil.copytree(artifacts, stored)
        for item in items:
            if item.action == "upload-artifact":
                shutil.rmtree(stored / (dict(item.inputs).get("name") or "artifact"), ignore_errors=True)
        steps = run_job(items, copy, Path(state) / "job", repo, stored)
        return [run for _, run in steps], own


#: Output of a tool that could not start the project's check at all: a tool missing, or one that cannot read
#: the project (an old uv on a newer lock file). The same on the base says nothing about the project.
_ENVIRONMENT_FAILURE = re.compile(
    r"Failed to (?:parse|read)\W+\S*(?:uv\.lock|pyproject\.toml)"
    r"|\bcommand not found\b"
    r"|^(?:make(?:\[\d+\])?|\S*sh|env): .*No such file or directory\s*$"
    r"|toolchain '[^']*' is not installed|toolchain not found|no default toolchain"
    r"|rustup could not choose a version",
    re.MULTILINE,
)


def environment_failure(output: str) -> str:
    """Return the line of `output` that shows the environment failed before the check started, or ""."""
    found = _ENVIRONMENT_FAILURE.search(output)
    return output[output.rfind("\n", 0, found.start()) + 1 :].partition("\n")[0].strip() if found else ""


_TIMING = re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|secs|seconds?)\b")


def _normalised(run: Run) -> str:
    """Return the output without what differs between checkouts of one commit: paths, timings, addresses."""
    text = run.output.replace(run.root, "<checkout>") if run.root else run.output
    text = re.sub(r"\S*factory-target-[\w.-]+", "<tmp>", text)
    text = re.sub(r"pytest-(?:of-\w+/)?(?:pytest-)?\d+", "pytest-N", text)
    text = re.sub(r"0x[0-9a-f]+", "0x?", text)

    def scrub_timings(row: str) -> str:
        # A summary row's key is kept whole, as `_gate_start.parse_pytest_row` reads it: `test_backoff[10s]`
        # and `test_backoff[5s]` are two tests, and so are `test_x[a] - 10s]` and `test_x[a] - 5s]`, whose
        # key is the whole row because where the id ends cannot be told.
        kept = f"{found[0]} {found[1]}" if (found := _gate_start.parse_pytest_row(row)) else ""
        return kept + _TIMING.sub("<time>", row[len(kept) :])

    return "\n".join(scrub_timings(row) for row in text.split("\n"))


def same_failure(head: Run, base: Run) -> bool:
    """Whether `head` failed as `base` did: the same exit code and no failing test, lint or type finding more.

    Findings are compared as `_gate_start._judge_against_start` does, by key and without line numbers. A
    failure with none recognised is compared by its whole output, normalised. A timeout is never the same.
    """
    if head.code in (0, 124) or head.final or head.not_run or base.not_run or head.code != base.code:
        return False
    now, then = _normalised(head), _normalised(base)
    if findings := _gate_start.finding_keys(now):
        return not findings - _gate_start.finding_keys(then)
    return now == then


def _list_failed_tests(text: str) -> set[str]:
    """Return the keys of the tests pytest's short summary in `text` names as failed."""
    return {found[1] for line in text.split("\n") if (found := _gate_start.parse_pytest_row(line))}


def _findings(run: Run) -> set[str]:
    """Return what a run found: its failing test IDs if pytest summarised them, else its tools' findings.

    Log lines a test suite prints (`ERROR ... Task exception was never retrieved`) are not findings when the
    run says which tests failed.
    """
    text = _normalised(run)
    if tests := _list_failed_tests(text):
        return tests
    return {" ".join(part for part in key if part) for key in _gate_start.finding_keys(text)}


@dataclasses.dataclass
class Judged:
    """The runs of one job (or one coverage command), sorted into what they mean for the project."""

    failures: list[str] = dataclasses.field(default_factory=list)
    pre_existing: list[str] = dataclasses.field(default_factory=list)
    not_run: list[str] = dataclasses.field(default_factory=list)


def judge(
    checkout: Path,
    steps: Sequence[tuple[_target_ci.Command, Run]],
    repo: str,
    *,
    from_ci: bool = True,
    against_base: bool = True,
    artifacts: Path | None = None,
) -> Judged:
    """Judge the runs of one job's steps: a failure the unpatched base has identically is not the project's.

    The whole job is replayed on the base when a step failed (an earlier step may have generated what a
    later one reads), in the same runner-like environment. Without a base to run on, a failure stays one.
    With `from_ci` (steps of the target's CI) a failing command that the base's own CI does not run is the
    patch's, however the base fares when made to run it; the coverage run is the gate's own command.
    Without `against_base` (promotion, which pushes to the target) every failure counts. `artifacts` is
    what the run's jobs uploaded, which the job's replay on the base downloads.
    """
    judged = Judged()
    failed = [i for i, (_, run) in enumerate(steps) if run.code and not run.not_run]
    for item, run in steps:
        if run.not_run:
            judged.not_run.append(report(item, run))
    if not failed:
        return judged
    if not against_base:
        judged.failures.extend(report(*steps[i]) for i in failed)
        return judged
    try:
        base_runs, base_commands = runs_on_base(checkout, [item for item, _ in steps], repo, artifacts)
    except BaseUnavailable as exc:
        judged.failures.extend(f"{report(*steps[i])}\n(not compared with the base: {exc})" for i in failed)
        return judged
    for i in failed:
        (item, run), base = steps[i], base_runs[i]
        if from_ci and item.command not in base_commands:
            judged.failures.append(
                f"{report(item, run)}\n(the unpatched base's CI does not run this command)"
            )
            continue
        # The check never started, on head or base: the project is not shown to pass or to fail. A base that
        # passed beside the same message, or a head run that names failing tests, shows the check did start.
        base_failed = base.code and not base.not_run
        names_tests = _list_failed_tests(_normalised(run))
        if (
            base_failed
            and not names_tests
            and (why := environment_failure(run.output))
            and environment_failure(base.output)
        ):
            judged.not_run.append(
                f"NOT RUN `{item.command}` ({item.origin}): the environment failed before the check "
                f"started (exit {run.code}): {why}"
            )
            continue
        if same_failure(run, base):
            judged.pre_existing.append(
                f"`{item.command}` ({item.origin}) exited {run.code}, as it does on the unpatched base"
            )
            continue
        if len(steps) == 1 and base.code and not base.not_run:
            # Upstream tests that depend on the machine (timing, docker, kubectl) can fail in one run and not
            # the next, on identical code. A single command (the coverage run) runs once more on each side: a
            # finding is the patch's only if both head runs have it and neither base run does. A step of a
            # longer job is not rerun out of its job's context.
            # On the code the first run saw. A step of the target's CI ran in a fresh copy of HEAD, so it runs
            # again in another (a step that edits files must not touch the session's checkout, whose
            # uncommitted files are not what is judged); the coverage run ran in the checkout itself.
            if from_ci:
                try:
                    with _open_worktree(checkout, "HEAD", "factory-target-rerun-") as copy:
                        again = execute(item, copy, None, repo)
                except WorktreeUnavailable as exc:
                    judged.failures.append(f"{report(item, run)}\n(not run again: cannot copy HEAD: {exc})")
                    continue
            else:
                again = execute(item, checkout, None, repo)
            if not again.code:
                judged.pre_existing.append(
                    f"`{item.command}` ({item.origin}) exited {run.code} once and passed when run again: "
                    "a test that fails only now and then, not the patch's"
                )
                continue
            steady = _findings(run) & _findings(again)
            if steady:
                try:
                    (base_again,), _ = runs_on_base(checkout, [item], repo)
                except BaseUnavailable:
                    base_again = base
                seen = _findings(base) | _findings(base_again)
                if not steady - seen:
                    judged.pre_existing.append(
                        f"`{item.command}` ({item.origin}) exited {run.code}: every finding both head runs "
                        "have, a run of the unpatched base has too (the others come and go between runs)"
                    )
                    continue
                new_findings = sorted(steady - seen)
                judged.failures.append(
                    f"{report(item, run)}\n(in both runs on the patched tree and in neither on the base: "
                    + "; ".join(new_findings[:20])
                    + ")"
                )
                continue
        if base.not_run or base.skipped:
            then = "not run" if base.not_run else f"is skipped: {base.skipped}"
        else:
            then = "passes" if not base.code else f"exits {base.code}, differently"
        judged.failures.append(f"{report(item, run)}\n(on the unpatched base the same command {then})")
    return judged


# -- the nested checkout --


def _git_names(checkout: Path, *args: str) -> set[str] | None:
    code, output = _gate_base._run(["git", *args, "-z"], checkout)
    return None if code else {name for name in output.split("\0") if name}


def changed_since_base(checkout: Path) -> set[str] | None:
    """Return the files of `checkout` that differ from its base, committed or not; None if unknown."""
    base = target_base(checkout)
    tracked = _git_names(checkout, "diff", "--name-only", base) if base else None
    untracked = _git_names(checkout, "ls-files", "--others", "--exclude-standard")
    return None if tracked is None or untracked is None else tracked | untracked


@dataclasses.dataclass(frozen=True)
class Nested:
    """A git checkout inside a project (the target's), and which of its files the project changed."""

    root: Path
    changed: set[str] | None
    base: str

    def untouched(self, relative: str) -> bool:
        """Whether `relative` (relative to the checkout) is as upstream has it. Unknown counts as touched."""
        return self.changed is not None and relative not in self.changed

    def bytes_at_base(self, relative: str) -> bytes | None:
        """Return the file's content at the base, None if it was not there."""
        if not self.base:
            return None
        try:
            return _gate_start._git_bytes(
                ["show", *_secret_env.GIT_NO_DRIVERS, f"{self.base}:{relative}"], self.root
            )
        except _gate_start._GateError:
            return None


def nested_checkouts(project: Path, skip_dirs: set[str]) -> list[Nested]:
    """Return the git checkouts inside `project` (not itself), outside the dependency and harness trees."""
    found: list[Nested] = []
    for directory, children, _ in os.walk(project):
        children[:] = [n for n in children if n not in skip_dirs]
        for child in list(children):
            path = Path(directory) / child
            try:
                checkout = (path / ".git").exists()
            except OSError:  # an unreadable directory is no checkout; os.walk skips it as well
                checkout = False
            if checkout:
                children.remove(child)
                found.append(Nested(path, changed_since_base(path), target_base(path)))
    return found
