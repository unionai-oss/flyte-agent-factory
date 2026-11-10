"""`factory-bench-check`: a benchmark task's hidden check, run on the code a case pushed.

Its own environment and module because it executes code the agent under test wrote. The environment
mounts no secret and carries no profile, and this module imports nothing that reads one at import, so
the pod loads it without `AGENT_FACTORY_PROFILE_TOML`. The commands also run with a minimal
environment: Flyte puts an API key into every task pod, and the agent's code must not inherit it.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import flyte
import flyte.io

if TYPE_CHECKING:
    from . import _bench_suite
else:
    from flyte_agent_factory import _bench_suite
if TYPE_CHECKING:
    from ._images import TERRAFORM_SHA256, TERRAFORM_VERSION
else:
    from flyte_agent_factory._images import TERRAFORM_SHA256, TERRAFORM_VERSION

#: Seconds each check command may take; a hung server in the agent's code must not hold the case.
COMMAND_TIMEOUT_S = 15 * 60
_SUMMARY_CHARS = 2000

TERRAFORM_PROVIDER_VERSION = "5.100.0"
TERRAFORM_PLUGIN_CACHE = "/opt/bench-terraform/plugin-cache"

# Terraform files the image build writes. They go in as base64: the builder's rendering of a RUN command
# strips double quotes, and Terraform cannot parse `source = hashicorp/aws` unquoted (a build failed so).
_PROVIDER_PIN = f"""terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "{TERRAFORM_PROVIDER_VERSION}"
    }}
  }}
}}
"""
_MIRROR_RC = """provider_installation {
  filesystem_mirror {
    path = "/opt/bench-terraform/providers"
  }
}
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


env = flyte.TaskEnvironment(
    name="factory-bench-check",
    image=(
        flyte.Image.from_debian_base(name="factory-bench-check", python_version=(3, 12))
        .with_pip_packages("pytest", "scikit-learn==1.6.1", "pandas==2.2.3")
        .with_apt_packages("curl", "ca-certificates", "unzip")
        .with_commands(
            [
                (
                    "curl -fsSL -o /tmp/terraform.zip https://releases.hashicorp.com/terraform/"
                    f"{TERRAFORM_VERSION}/terraform_{TERRAFORM_VERSION}_linux_amd64.zip "
                    f"&& echo '{TERRAFORM_SHA256}  /tmp/terraform.zip' | sha256sum -c - "
                    "&& unzip -o /tmp/terraform.zip terraform -d /usr/local/bin "
                    "&& chmod a+rx /usr/local/bin/terraform && rm /tmp/terraform.zip "
                    "&& terraform version"
                ),
                (
                    "mkdir -p /tmp/bench-terraform-build /opt/bench-terraform/providers "
                    f"{TERRAFORM_PLUGIN_CACHE} "
                    f"&& echo {_b64(_PROVIDER_PIN)} | base64 -d > /tmp/bench-terraform-build/main.tf "
                    "&& terraform -chdir=/tmp/bench-terraform-build providers mirror "
                    "/opt/bench-terraform/providers "
                    f"&& echo {_b64(_MIRROR_RC)} | base64 -d > /opt/bench-terraform/terraform.rc "
                    "&& TF_CLI_CONFIG_FILE=/opt/bench-terraform/terraform.rc "
                    f"TF_PLUGIN_CACHE_DIR={TERRAFORM_PLUGIN_CACHE} "
                    "terraform -chdir=/tmp/bench-terraform-build init -backend=false -input=false "
                    "&& chmod -R a+rX /opt/bench-terraform "
                    "&& rm /tmp/bench-terraform-build/main.tf /tmp/bench-terraform-build/.terraform.lock.hcl"
                ),
            ]
        )
    ),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
    # A missed check is rerun with the case; it may be preempted.
    interruptible=True,
)


def seal() -> None:
    """Linux procfs retains startup secrets after unsetenv; deny same-user reads before spawning code."""
    # Python has no prctl wrapper; ctypes avoids an extra Linux-only dependency.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise OSError(ctypes.get_errno(), "could not seal the hidden-check parent")


def _readable_key(key_vars: tuple[str, ...]) -> str | None:
    # Scan as a child: the parent can still read its own environ after becoming non-dumpable.
    probe = r"""
import pathlib, sys
names = {name.encode() for name in sys.argv[1:]}
for path in pathlib.Path('/proc').glob('[0-9]*/environ'):
    try:
        fields = path.read_bytes().split(b'\0')
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        continue
    if any(field.partition(b'=')[0] in names for field in fields):
        print(path.parent.name)
        sys.exit(1)
"""
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", probe, *key_vars],
            env={},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "refused: credential scan could not complete"
    if done.returncode:
        pid = done.stdout.strip()
        if pid.isdigit():
            return f"refused: credential readable in pid {pid}"
        return "refused: credential scan failed"
    return None


def _argv(argv: tuple[str, ...], hidden: Path, project: Path) -> list[str]:
    args = [sys.executable if a == "python" else a.replace("{hidden}", str(hidden)) for a in argv]
    if args[0] == sys.executable:
        args.insert(1, "-I")
    if "pytest" in args:
        # pytest must load from the installed interpreter before case imports become visible.
        args.extend(["-o", f"pythonpath={project}"])
        if not any(a.startswith(str(hidden)) for a in args):
            args.append(str(project / "tests"))
    return args


class CheckResult(TypedDict):
    exit_code: int | None
    summary: str


def run_hidden_check(
    task: _bench_suite.Task,
    project: Path,
    *,
    timeout: float = COMMAND_TIMEOUT_S,
    key_vars: tuple[str, ...] = ("_UNION_EAGER_API_KEY",),
    terraform_budget_s: float | None = None,
) -> CheckResult:
    """`{"exit_code", "summary"}` of `task`'s check commands run in `project`, stopping at the first failure.

    The hidden files are written to a directory outside `project`, so nothing the check adds is mistaken
    for the agent's work. The summary is the end of the output, which is where pytest puts its verdict.
    """
    if key_vars and (refusal := _readable_key(key_vars)):
        return {"exit_code": None, "summary": refusal}
    project = project.resolve()
    with tempfile.TemporaryDirectory(prefix="bench-check-") as tmp:
        hidden, home = Path(tmp) / "hidden", Path(tmp) / "home"
        hidden.mkdir()
        home.mkdir()
        for name, text in task.check_files.items():
            (hidden / name).write_text(text)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "BENCH_PROJECT": str(project),
            "BENCH_HIDDEN": str(hidden),
            # For the servers a test starts (`python -m <pkg>`): pytest itself runs with -I, which ignores it,
            # so a module in the case directory still cannot shadow pytest.
            "PYTHONPATH": str(project),
        }
        if task.id == "tf-bucket":
            if terraform_budget_s is not None:
                env["BENCH_TF_BUDGET_S"] = str(terraform_budget_s)
            cache = os.environ.get("TF_PLUGIN_CACHE_DIR", TERRAFORM_PLUGIN_CACHE)
            if Path(cache).is_dir():
                env["TF_PLUGIN_CACHE_DIR"] = cache
                # These modules commit no lock file, and without one Terraform ignores the cache and
                # downloads the provider again on every `init` (several per case).
                env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "true"
        output = ""
        for argv in task.check_commands:
            # Its own session, so a timeout kills the servers a test started too, as `_deploy._run_in_tree`.
            with subprocess.Popen(
                _argv(argv, hidden, project),
                cwd=tmp,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            ) as proc:
                try:
                    out, _ = proc.communicate(timeout=timeout)
                    code = proc.returncode
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    out, _ = proc.communicate()
                    out, code = f"{out}\ntimed out after {timeout:.0f} s", 124
            output += f"$ {' '.join(argv)}\n{out}"
            if code:
                return {"exit_code": code, "summary": output[-_SUMMARY_CHARS:]}
        return {"exit_code": 0, "summary": output[-_SUMMARY_CHARS:]}


@env.task
async def hidden_check(code: flyte.io.Dir, task: str) -> str:
    """JSON `{"exit_code", "summary"}` of suite task `task`'s hidden check on the case directory `code`."""
    seal()
    project = Path(await code.download())
    return json.dumps(await asyncio.to_thread(run_hidden_check, _bench_suite.get(task), project))
