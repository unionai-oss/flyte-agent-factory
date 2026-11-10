"""Cloning, and the one credential that makes it possible.

Its own module rather than part of `agent.py` because the voice factory needs
to clone too, and it must not drag the whole agent in to do so. Flyte builds a
code bundle from the import graph it can see, so a *function-level* import of
something in `agent.py` is invisible to it: the factory's first run failed with
`No module named 'flyte_agent_factory.agent'` for exactly that reason. A
module-level import would have fixed the bundle and pulled the browser, the
state machinery and the bridge in with it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import _names
from ._auth import write_private

#: Environment variable the single-repo deploy key is mounted as.
GIT_DEPLOY_KEY_ENV_VAR = "GIT_DEPLOY_KEY"
#: JSON `{repo URL: env var}` for the keys a slot mounts beside the default one (`[repo_keys]`).
REPO_KEYS_ENV_VAR = "AGENT_FACTORY_REPO_KEYS"


def repo_key_env_var(repo_key: str) -> str:
    """Return the variable a `[repo_keys]` entry's deploy key is mounted as: `GIT_DEPLOY_KEY_FACTORY`."""
    return f"{GIT_DEPLOY_KEY_ENV_VAR}_{repo_key.upper()}"


def _repo_identity(url: str) -> str:
    """`host/path` of a repository URL, so its ssh, https and local spellings compare equal.

    `git@h:o/r.git`, `ssh://git@h/o/r.git`, `https://h/o/r?x#y` are one repository to the person who
    wrote the profile, as are `file:///p` and `/p`. Host and path are compared case-insensitively
    (hosting services do; a profile typo in case should not turn a sandbox into a root project).
    User, credentials, port, scheme, query, fragment, repeated or trailing `/` and `.git` are not
    part of the identity.
    """
    url = url.strip()
    if "://" in url:
        # urlsplit takes the host after the last `@`, so a password containing `@` is still dropped.
        parts = urlsplit(url)
        host, path = parts.hostname or "", parts.path
    else:
        scp = re.fullmatch(r"(?:[^/]*@)?([^/:@]+):(.+)", re.split(r"[?#]", url)[0])  # git@host:owner/repo
        host, path = (scp[1], scp[2]) if scp else ("", re.split(r"[?#]", url)[0])  # a local path
    path = re.sub(r"/+", "/", path).strip("/").lower().removesuffix(".git")
    return f"{host.lower()}/{path}"


def project_layout(repo: str, sandbox_repo: str, is_workspace: bool = False) -> str:
    """`workspace`, `sandbox` or `root`: where a project on `repo` keeps its files.

    `is_workspace` is `_gate_base.is_workspace(checkout)`, passed in because this module cannot import it
    (`_secret_env` imports this one). A workspace gives each project `projects/<name>/`. Otherwise `sandbox`
    when `repo` is the profile's multi-project sandbox: the project owns `<name>/` in a repo it shares
    with other projects. Any other repo (the factory's own, say) is worked at its root, so the harness
    must not create `<name>/` there. No repo given, or no profile to compare against, is the sandbox:
    that is today's behaviour.
    """
    if is_workspace:
        return "workspace"
    repo, sandbox_repo = repo.strip(), sandbox_repo.strip()
    if not repo or not sandbox_repo:
        return "sandbox"
    return "sandbox" if _repo_identity(repo) == _repo_identity(sandbox_repo) else "root"


def _key_dir() -> Path:
    """Somewhere writable. Tasks run unprivileged, so not `/root`."""
    return Path(_names.getenv("AGENT_FACTORY_BASE") or os.environ.get("HOME") or "/tmp")


def install_deploy_key(repo: str = "") -> str | None:
    """Write the deploy key for `repo` to disk and point git at it, exclusively.

    A deploy key is scoped to a single repository, so this is what confines an
    agent's git reach: other repositories are unreachable because it holds no
    credential for them, not because a rule forbids them. `IdentitiesOnly=yes`
    stops ssh from offering any other key it happens to find, and
    `StrictHostKeyChecking=accept-new` keeps a first clone from hanging on a
    host-key prompt nobody is there to answer.

    A slot may mount a second repository's key (`REPO_KEYS_ENV_VAR`); `repo`
    picks it, and anything else gets the default key. Every mounted key is then
    removed from the environment, so the session and everything it spawns
    hold only the one chosen key, as a file. That makes the first call the
    choice for the pod: later calls find no material and keep what is installed.
    """
    key_path = _key_dir() / ".ssh" / "deploy_key"
    mounted = {n: os.environ.pop(n) for n in [n for n in os.environ if n.startswith(GIT_DEPLOY_KEY_ENV_VAR)]}
    # Removed before anything can fail: an empty key or a bad map must not leave the others behind.
    var = json.loads(_names.getenv(REPO_KEYS_ENV_VAR) or "{}").get(repo, GIT_DEPLOY_KEY_ENV_VAR)
    material = mounted.get(var, "").strip()
    if not material:
        installed = key_path.is_file() and f"-i {key_path} " in os.environ.get("GIT_SSH_COMMAND", "")
        return str(key_path) if installed else None

    # ssh refuses a key others can read, and silently ignores one with a
    # missing trailing newline.
    write_private(key_path, material if material.endswith("\n") else material + "\n")

    os.environ["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )
    return str(key_path)


async def clone(repo: str, branch: str, dest: Path) -> None:
    """Clone `repo` into `dest`, installing its deploy key first.

    A single helper rather than the three copies this replaced, because the key
    is what makes a clone possible at all and two of those copies had
    forgotten it: without it ssh has no identity and no known_hosts entry, so
    the clone tries to run ssh-askpass and dies on `Host key verification
    failed`. Installing it here means a new call site cannot reintroduce that.
    """
    install_deploy_key(repo)
    cmd = ["git", "clone", *(["--branch", branch] if branch else []), repo, str(dest)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed ({proc.returncode}): {out.decode(errors='replace')[-2000:]}")
