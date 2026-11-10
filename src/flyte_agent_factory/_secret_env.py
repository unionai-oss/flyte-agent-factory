"""The environment and git invocation for anything the supervisor starts within the session's reach.

Its own module so the gates, the checkpoint, delegation and the browser can use it without importing
`agent`, which imports all of them. `agent._session_env` is this module's `session_env`.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import _auth, _board, _browser_exit_node, _git, _names, _profile, _pull_requests

if TYPE_CHECKING:
    from ._profile import Profile

#: The Codex (ChatGPT) access token, under the name it is mounted as.
CODEX_TOKEN_ENV_VAR = "CODEX_ACCESS_TOKEN"
#: Everything of the Codex credential a spawned process must not inherit.
CODEX_SECRET_VARS = (CODEX_TOKEN_ENV_VAR,)

#: Variables that make Claude Code bill the Anthropic API instead of the
#: subscription this agent is supposed to run on. A session that sees one comes
#: up as "API Usage Billing", reports "Not logged in", and spends
#: real API credit for work the subscription already covers.
#:
#: The pod may still hold the key: a project that benchmarks against the API
#: needs it, and `_export_runtime_env()` keeps it for the task. It is removed
#: from the environment handed to the *session*, which is the only place it
#: does harm. Union/Flyte keys are deliberately not removed here -- the session
#: needs those to submit child actions.
MODEL_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: Where a project's own Anthropic key is mounted instead. Deploy it with
#: `AGENT_FACTORY_SECRETS=<secret name>=PROJECT_ANTHROPIC_API_KEY`: under a
#: name Claude Code does not read, the key is inert until a command is told to
#: use it, and there is no file anywhere for it to leak into.
PROJECT_API_KEY_VAR = "PROJECT_ANTHROPIC_API_KEY"

#: Raw credentials the pod is given by name, whatever the profile mounts. The mounted list below catches
#: the profile's own secrets; these are the ones the code itself relies on and a hand-built pod may
#: carry without `AGENT_FACTORY_SECRETS`. The setup-token reaches a Claude process through a pipe
#: (`_auth.token_fd`), never through this environment.
CREDENTIAL_VARS = (
    *MODEL_BILLING_VARS,
    *CODEX_SECRET_VARS,
    "CLAUDE_CODE_OAUTH_TOKEN",
    "LLM_GATEWAY_KEY",
    _git.REPO_KEYS_ENV_VAR,
)

#: What a session keeps although it is a secret, because its documented work needs it:
#: - the Union API key: `flyte_agent_factory.child.attach()` initialises the Flyte SDK from it to submit
#:   child actions. It is the residual exposure: a Bash tool call can print it, and it is redacted
#:   from published text (`_bridge.redact_tokens`) only where its format is recognisable.
#: - `PROJECT_ANTHROPIC_API_KEY`: the session instructions reserve it for the project's own API calls.
SESSION_KEEPS = (*_auth.API_KEY_ENV_VARS, PROJECT_API_KEY_VAR)

#: Put before the subcommand of every git call the supervisor makes in a workspace the session writes.
#: The session owns `.git/config` and `.git/hooks`, and `core.fsmonitor` and a hook are programs git
#: runs as the supervisor's child. The environment is scrubbed as well, so what they could read is
#: what the session already has; these stop them running at all.
GIT_SAFE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
#: For a call that renders a diff or a blob: a textconv or external diff driver is a configured
#: program too. Diff options, so they go after the subcommand.
GIT_NO_DRIVERS = ("--no-textconv", "--no-ext-diff")


def mounted_secret_vars(env: Mapping[str, str], profile: Profile | None) -> set[str]:
    """Names of the variables the pod's secrets are mounted under, from the deploy's own list.

    `AGENT_FACTORY_SECRETS` (`secret=ENV_VAR,...`) is what `agent._secrets()` mounted, and Flyte injects
    every secret a second time under `<prefix><SECRET KEY UPPER>`. Read from the list so a new profile
    secret is hidden without anyone editing a name here.
    """
    prefix = env.get(_pull_requests.SECRETS_PREFIX_ENV_VAR, _pull_requests.DEFAULT_SECRETS_PREFIX)
    specs = _names.getenv("AGENT_FACTORY_SECRETS", env=env) or (profile.agent_secrets if profile else "")
    names = set()
    for spec in filter(None, (s.strip() for s in specs.split(","))):
        key, _, var = spec.partition("=")
        names |= {var, f"{prefix}{key.upper()}"}
    return names - {""}


def browser_token_vars(env: Mapping[str, str], profile: Profile | None) -> set[str]:
    """Names of the variables holding the personal browsers' bearer tokens: the supervisor's alone.

    From the profile, and from the identity list the deploy put in the pod's environment, so a pod
    deployed from an older profile still hides the tokens it was given.
    """
    names = {i.token_env for i in profile.browser.identities} if profile else set()
    listed = filter(None, env.get("AGENT_FACTORY_BROWSER_IDENTITY_NAMES", "").split(","))
    return names | {_profile.BrowserIdentity(name, "").token_env for name in listed}


def session_env(env: Mapping[str, str], profile: Profile | None) -> dict[str, str]:
    """Return `env` without any raw credential, for the session and every child it starts.

    Removed: what changes how Claude is billed, the Claude and Codex logins, the deploy keys, the GitHub
    App's and the board's credentials, the tailscale key, the personal browsers' tokens, every secret the
    deploy mounted (by name and under Flyte's `<prefix>` copy) and anything else under that prefix.
    `SESSION_KEEPS` is the short list that stays. A same-uid process can still read the files these were
    written to (`_git`, the Codex home), but those are 0600 in a 0700 directory and are referenced by path,
    not by value.
    """
    secret = profile.github.private_key_secret if profile else ""
    board_secret = profile.board.pat_secret if profile and profile.board else ""
    prefix = env.get(_pull_requests.SECRETS_PREFIX_ENV_VAR, _pull_requests.DEFAULT_SECRETS_PREFIX)
    hidden = {
        *CREDENTIAL_VARS,
        *_pull_requests.credential_env_vars(secret, env),
        *_board.credential_env_vars(board_secret, env),
        *_browser_exit_node.credential_env_vars(_browser_exit_node.settings()["tailscale_secret"], env),
        *mounted_secret_vars(env, profile),
        *browser_token_vars(env, profile),
    }
    return {
        k: v
        for k, v in env.items()
        if k in SESSION_KEEPS
        or not (k in hidden or k.startswith(_git.GIT_DEPLOY_KEY_ENV_VAR) or (prefix and k.startswith(prefix)))
    }


def child_env(**extra: str) -> dict[str, str]:
    """Return this process's environment, scrubbed as for the session, plus `extra`.

    For whatever the supervisor starts that the session can reach: a gate running the session's tests,
    git reading the session's `.git/config`, the browser the session drives. The profile is read
    again here because these modules cannot see `_session_config._PROFILE`; it is the same selection.
    """
    return {**session_env(os.environ, _profile.current()), **extra}


def git_argv(*args: str) -> list[str]:
    """`git` with `GIT_SAFE_CONFIG`, for a workspace the session controls. Run it with `child_env()`."""
    return ["git", *GIT_SAFE_CONFIG, *args]


def _remove(path: Path) -> None:
    """Delete `path`: a real directory with its contents, anything else (a symlink included) by unlinking."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def disarm_git_dir(repo: Path) -> None:
    """Strip what in `repo/.git` makes git run a program, so git can be run on a tree a worker handed back.

    `GIT_SAFE_CONFIG` and `GIT_NO_DRIVERS` do not cover a `filter.*.clean` driver, which `git add` and
    `git diff` run, so the config, the hooks and the attributes file are removed instead. This edits
    `repo` in place: give it only a throwaway copy the supervisor owns. A `.git` that is a symlink or a
    gitfile (`gitdir: ...`) could lead to the author's own repo, so it is refused.
    `objects/info/alternates` goes too: it names directories outside the copy, and a diff needs only
    the objects the worker's tree carries. `.gitattributes` in the worktree is not touched; with no
    driver configured it names nothing git can run.

    After this, `repo/.git` is the only git directory git can open under `repo`, and it holds no
    config, hooks or attributes. Nested `.git` entries (an embedded repo, a gitfile into
    `.git/modules`) are removed, so their files show up as plain files in a diff.
    """
    git_dir = repo / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError(f"{repo}: .git is not a plain directory; refusing to run git in it")
    # Every path below is removed, never written through: a symlink there would otherwise reach a file
    # outside the copy. `commondir` would point git at another repository's config and objects.
    for parent in ("info", "objects", "objects/info"):
        if (git_dir / parent).is_symlink():
            raise ValueError(f"{repo}: .git/{parent} is a symlink; refusing to run git in it")
    for name in (
        "config",
        "hooks",
        "info/attributes",
        "config.worktree",
        "objects/info/alternates",
        "commondir",
        # Worker-written git dirs; `snapshot()` ships neither, so a legitimate tree has neither.
        "modules",
        "worktrees",
    ):
        _remove(git_dir / name)
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
    )
    for root, dirs, files in os.walk(repo, followlinks=False):
        if Path(root) == repo and ".git" in dirs:
            dirs.remove(".git")  # the one git dir that stays
        # A symlink to a directory is listed in `dirs` too; `_remove` unlinks it, never follows it.
        for name in (*dirs, *files):
            if name == ".git":
                _remove(Path(root) / name)
        dirs[:] = [d for d in dirs if d != ".git"]
