"""Claude sessions run on a setup-token.

A setup-token is mounted as CLAUDE_CODE_OAUTH_TOKEN and never saved to disk.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from . import _names

# Tool-permission rules the agent always runs with. The user's requirement is
# that a swarm stays inside one Flyte run: Claude Code's own subagents already
# do, because they are threads of the same process, but nothing stops the model
# from shelling out to `flyte run` and starting a second run that Union would
# bill and nobody would be watching. These rules close that door.
DENIED_TOOLS = [
    "Bash(flyte run:*)",
    "Bash(flyte deploy:*)",
    "Bash(flyte serve:*)",
    "Bash(flyte abort:*)",
    "Bash(flyte delete:*)",
    # Conditions are for humans and the operator to answer: `/review-again`, `/stop`.
    "Bash(flyte signal:*)",
    # Best effort only: the Python client is reachable from any shell and no pattern can see all of it.
    # What these catch is the shape that once aborted a project's root action `a0` -- an abort chosen
    # from a listing. The real fix is `child.abort_children` and the instructions that point at it.
    "Bash(*Action.abort*)",
    "Bash(*.abort(*)",
    "Bash(*abort*listall*)",
    "Bash(*listall*abort*)",
    # `a0` as a whole word only (then a space, a quote or the end): `a0f3c9` is an ordinary child name.
    "Bash(*abort* a0 *)",
    "Bash(*abort* a0)",
    "Bash(*abort*'a0'*)",
    'Bash(*abort*"a0"*)',
]


#: Where the pod's Union API key is, in the order the SDK reads them (`flyte._initialize`).
API_KEY_ENV_VARS = ("_UNION_EAGER_API_KEY", "EAGER_API_KEY")


#: Claude Code reads the setup-token from this inherited file descriptor when it is set (Claude Code
#: 2.1.x; `strings` on the binary lists it beside `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`). It keeps the
#: token out of the session's environment, and so out of `/proc/<pid>/environ` and every `env`
#: dump a Bash tool call makes.
TOKEN_FD_VAR = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"
#: Set to 1 to hand the token over as `CLAUDE_CODE_OAUTH_TOKEN` again, should a Claude Code build stop
#: honouring the descriptor.
TOKEN_IN_ENV_VAR = "AGENT_FACTORY_TOKEN_IN_ENV"


@contextlib.contextmanager
def token_fd(env: Mapping[str, str], token: str) -> Iterator[tuple[dict[str, str], tuple[int, ...]]]:
    """Yield `(env, pass_fds)` that give a child `token` through a pipe instead of its environment.

    The token is written into a pipe that is already closed at the write end, so the child reads it
    to EOF and nothing can read it a second time: a Bash tool call that inherits the descriptor finds
    it empty. A token is far below the pipe buffer, so the write cannot block. The caller spawns the
    child inside the `with` block (`pass_fds=pass_fds`); the read end is closed on leaving it.
    """
    child_env = dict(env)
    if not token:
        yield child_env, ()
        return
    if _names.getenv(TOKEN_IN_ENV_VAR) == "1":
        yield {**child_env, "CLAUDE_CODE_OAUTH_TOKEN": token}, ()
        return
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "wb") as write_end:  # closes write_fd, also if the write fails
            write_end.write(token.encode())
        yield {**child_env, TOKEN_FD_VAR: str(read_fd)}, (read_fd,)
    finally:
        os.close(read_fd)


def write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by this user only, in a directory only this user can enter.

    The file is created 0600 rather than chmod-ed after a default-mode write, so there is no moment
    when it is readable by others; the directory is 0700 so a sibling listing is not possible either.
    Only the directory directly above the file is tightened.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(fd, 0o600)  # the file may have existed with another mode
        f.write(text)


def set_non_dumpable() -> bool:
    """Make this process non-dumpable (Linux `prctl(PR_SET_DUMPABLE, 0)`). True when the call succeeded.

    A non-dumpable process's `/proc/<pid>/environ` and `/proc/<pid>/mem` are owned by root, so a child
    running as the same user cannot read the supervisor's credentials. The flag resets to dumpable at
    every `execve`, so the `claude` child is not protected by this: what it must not hold is kept out
    of its environment instead (`_session_setup._session_env`, `token_fd`).
    """
    if not sys.platform.startswith("linux"):
        return False
    PR_SET_DUMPABLE = 4
    return bool(ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0)


def own_client_id() -> str:
    """Return the client id of this pod's API key, or "" if there is none or it is unreadable.

    The server names an API key by this id when it aborted something. Only the id is returned: the
    key also holds the secret, which never leaves `decode_api_key`.
    """
    from flyte.remote._client.auth._auth_utils import decode_api_key  # the SDK's own parser of the format

    key = next((v for v in (os.environ.get(n, "") for n in API_KEY_ENV_VARS) if v), "")
    if not key:
        return ""
    try:
        return decode_api_key(key)[1]
    except ValueError:  # binascii.Error and UnicodeDecodeError are ValueErrors
        return ""


def credentials_path(config_dir: Path) -> Path:
    return config_dir / ".credentials.json"


def drop_stale_login(config_dir: Path) -> None:
    """Delete a login left in a restored config: a token session must never reuse or save one.

    A checkpoint taken under a login deployment carries `.credentials.json`; Claude Code would prefer it over
    the setup-token in the environment, so it is removed before the session starts.
    """
    credentials_path(config_dir).unlink(missing_ok=True)


def write_settings(
    config_dir: Path, *, hold_dialogs: bool = True, extra: dict[str, Any] | None = None
) -> Path:
    """Write the user-scope settings the agent needs.

    Args:
        config_dir: The `CLAUDE_CONFIG_DIR` to write into.
        hold_dialogs: Keep forwarded dialogs open indefinitely instead of
            letting them expire. Right when somebody is attached and would
            answer -- expiring takes the dialog's no-action default, turning a
            question into a silent wrong answer. Wrong when nobody is watching,
            where it means the session hangs forever instead of carrying on.
        extra: Additional settings merged over the defaults.

    """
    settings: dict[str, Any] = {
        # Without this, a message from one of the user's other Claude sessions
        # is *held* rather than delivered, because this session bypasses
        # permission prompts and the inbound default distrusts that direction.
        # Holding it would silently break chat-agent direction.
        "crossSessionInbound": "accept",
        # Read AGENTS.md as well as CLAUDE.md. Claude Code 2.1.303 reads this only as a builtin
        # plugin option in user, --settings or managed settings: a top-level `instructionFiles`
        # is ignored, and project settings are not consulted for plugin options.
        "pluginConfigs": {"agents-md@builtin": {"options": {"instructionFiles": "claude-md-and-agents-md"}}},
        "permissions": {"deny": DENIED_TOOLS},
    }
    if hold_dialogs:
        settings["dialogExpiry"] = "never"
    if extra:
        # Merge per plugin, so extra options add to ours rather than dropping AGENTS.md support.
        plugins = settings["pluginConfigs"]
        for plugin, config in extra.get("pluginConfigs", {}).items():
            options = {**plugins.get(plugin, {}).get("options", {}), **config.get("options", {})}
            plugins[plugin] = {**plugins.get(plugin, {}), **config, "options": options}
        settings.update({k: v for k, v in extra.items() if k != "pluginConfigs"})

    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


def mark_trusted(config_dir: Path, workspace: Path) -> None:
    """Best-effort pre-acceptance of the first-run dialogs.

    Claude Code asks `Trust <directory>?` the first time, and exits with
    `Workspace not trusted` when stdin/stdout isn't a terminal. These config keys are Claude Code internals
    rather than documented settings, so this is a best-effort head start only --
    the reliable mechanism is that `_process` runs the CLI under a real pty and
    answers the prompts. Both are in place because either alone is fragile.
    """
    for path in (config_dir / ".claude.json", Path.home() / ".claude.json"):
        try:
            current = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(current, dict):
                current = {}
        except (OSError, json.JSONDecodeError):
            current = {}

        current["hasCompletedOnboarding"] = True
        current["hasTrustDialogAccepted"] = True
        projects = current.setdefault("projects", {})
        if isinstance(projects, dict):
            entry = projects.setdefault(str(workspace), {})
            if isinstance(entry, dict):
                entry["hasTrustDialogAccepted"] = True
                entry["hasCompletedProjectOnboarding"] = True

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, indent=2))
        except OSError:
            # Not fatal: the pty prompt-answering path still covers this.
            pass


def assert_claude_available() -> None:
    """Fail with something actionable when the CLI is not reachable.

    The image installs Claude Code as root and the task runs as an
    unprivileged user, so a permissions or PATH mistake shows up here rather
    than as a confusing pty that produces no output.
    """
    import shutil

    found = shutil.which("claude")
    if found is None:
        raise RuntimeError(
            "the `claude` CLI is not on PATH in this container. PATH="
            f"{os.environ.get('PATH', '')!r}. The image installs it under "
            "`_images.CLAUDE_HOME`; check that layer built and is world-readable."
        )
    if not os.access(found, os.X_OK):
        raise RuntimeError(f"`claude` at {found} is not executable by this user (uid {os.getuid()})")
