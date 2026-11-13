"""What every bootstrap step shares: the settings, the step contract and the two adapters' interfaces.

The adapters are the only places the bootstrap touches the world (`gh`, Flyte, subprocesses), so tests run
the whole step table against fakes. No value that is a secret (the Claude token, the App key, a private
deploy key, an API key) is ever stored in a `Result`, printed or put into an exception here: adapters take
them as arguments and hand them straight on.
"""

from __future__ import annotations

import getpass
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from connectrpc.errors import ConnectError

APP_NAME_PREFIX = "agent-factory"
DEPLOY_KEY_TITLE = "agent-factory"
RULESET_NAME = "agent-workspace-main"
#: The Flyte secret names the profile uses (docs/secrets.md); defaults match the profile examples' purpose.
SECRET_CLAUDE = "agent_factory_claude_token"
SECRET_DEPLOY_KEY = "agent_factory_deploy_key"
SECRET_APP_KEY = "agent_factory_github_app_key"
PROFILE_FILE = "profile.toml"
#: GitHub refuses App names over 34 characters; the manifest name and the slug derived from it are cut alike.
APP_NAME_LIMIT = 34

DONE, TODO, BLOCKED = "done", "todo", "blocked"
#: What `--no-app` costs, in the words the step output, `--help`, the skill and the docs all use
#: (`tests/test_bootstrap_review.py` holds the documents to it). The App is how the factory reads CI, opens
#: draft pull requests, sets `factory/verifier`, deploys merged changes and takes in issues.
APP_LOST = (
    "what is lost without the GitHub App: CI visibility (check-runs and statuses are read through the App); "
    "draft pull requests and the factory/verifier status; auto-deploy and the [direction] repository; "
    "issue intake. The backlog board is kept: it is made with gh and needs no App"
)


#: The one fact about the deploy's API key that no command can get around; the step output and the documents
#: quote it as it is (`tests/test_bootstrap_review.py`). `flyteplugins-union` ships a Linux x86_64 wheel only.
API_KEY_REQUIREMENT = (
    "the deploy makes its one-off Union API key with flyteplugins-union, which installs on Linux x86_64 only"
)


def normalise_endpoint(raw: str) -> str:
    """Return the bare `host[:port]` of an endpoint given as a URL, `dns:///host` or `host`.

    `flyte create config` writes `dns:///<host>` whatever form it was given, so the sign-in step compares
    hosts in this one form, exactly (`example.com` is not `flyte.example.com`).
    """
    host = raw.strip().lower()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.lstrip("/")
    return host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def build_app_name(owner: str) -> str:
    return f"{APP_NAME_PREFIX}-{owner}"[:APP_NAME_LIMIT].rstrip("-")


class BootstrapError(Exception):
    """A step could not do its work; the message is safe to print (never holds a secret)."""


class NotSignedIn(BootstrapError):
    """Flyte cannot be asked yet: there is no usable config, which the sign-in step writes.

    A check that meets this has nothing to report but "sign in first".
    """


class KeyExists(BootstrapError):
    """Union already has an API key of this name; its secret cannot be read back, so it cannot be reused."""


class AccessDenied(BootstrapError):
    """Union refused to make a policy or an assignment: the person may not manage access, an admin must."""


class NotAllowed(BootstrapError):
    """GitHub refused with 403/404 for lack of permission (a ruleset on a free plan, a missing scope)."""


#: What a step or adapter may raise for a failure outside this process (the control plane, `gh`, git, disk).
#: An adapter turns a reply it cannot parse into a `BootstrapError` (`GhCli._parse_json`). Anything else is a
#: bug and should surface as a traceback, not a `blocked` line.
EXPECTED_ERRORS = (
    BootstrapError,
    RuntimeError,
    OSError,
    subprocess.SubprocessError,
    ConnectError,
)


@dataclass(frozen=True)
class Check:
    """What a step's live check found: `ok` means there is nothing to do."""

    ok: bool
    detail: str


@dataclass(frozen=True)
class Outcome:
    """What a step's `do()` ended with: `done`, or `todo`/`blocked` with the one next action in `detail`."""

    status: str
    detail: str


@dataclass(frozen=True)
class IssuedKey:
    """A Union API key just made. `encoded` is its secret, the bearer value: kept in memory, never printed."""

    client_id: str
    encoded: str = field(repr=False)


def write_to_terminal(text: str) -> None:
    """Write `text` to the controlling terminal only, so it reaches neither stdout nor a captured log.

    Raises OSError when the process has none (a CI job, a chat tool's shell).
    """
    with open("/dev/tty", "w") as tty:
        tty.write(text)


def run_captured(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command with its output captured: `claude mcp add` is given a key and its output is not ours."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


class GitHubOps(Protocol):
    """Everything the bootstrap asks of GitHub. The real one shells out to `gh` (`_bootstrap_github`)."""

    def authenticated(self) -> bool: ...
    def owner_is_org(self, owner: str) -> bool: ...
    def viewer_login(self) -> str: ...
    def repo_visibility(self, repo: str) -> str | None:
        """Return `public`, `private` or `internal`; None when the repository does not exist.

        Raises when GitHub cannot be asked (a network failure is not "absent").
        """
        ...

    def clone_origin_is(self, checkout: Path, repo: str) -> bool: ...
    def create_repo(self, repo: str) -> None: ...
    def clone(self, repo: str, dest: Path) -> None: ...
    def push(self, checkout: Path) -> None: ...
    def ruleset_exists(self, repo: str, name: str) -> bool: ...
    def ruleset_deploy_key_bypass(self, repo: str, name: str) -> bool:
        """Whether the existing ruleset lets a deploy key bypass it."""
        ...

    def update_ruleset(self, repo: str, name: str, deploy_key_bypass: bool) -> None: ...
    def create_ruleset(self, repo: str, name: str, deploy_key_bypass: bool = False) -> None: ...
    def deploy_key_titles(self, repo: str) -> list[str]: ...
    def add_deploy_key(self, repo: str, public_key_file: Path, title: str) -> None: ...
    def delete_deploy_key(self, repo: str, title: str) -> None: ...
    def board_linked(self, repo: str) -> bool: ...
    def create_board(self, owner: str, repo: str, title: str) -> str: ...
    def app_installations(self, owner: str) -> list[str]: ...
    def app_exists(self, owner: str, slug: str) -> bool:
        """Whether `owner` has an App with this slug. Raises when GitHub cannot be asked."""
        ...

    def register_app(self, owner: str, repo: str, say: Callable[[str], None]) -> str: ...
    def exchange_manifest_code(self, code: str) -> "AppRegistration": ...
    def app_installed(self, registration: "AppRegistration", repo: str) -> bool: ...


class FlyteOps(Protocol):
    """Everything the bootstrap asks of Flyte. The real one uses `flyte.remote` (`_bootstrap_flyte`)."""

    config_path: Path

    def config_endpoint(self) -> str | None: ...
    def create_config(self) -> None: ...
    def sign_in(self) -> None: ...
    def project_exists(self) -> bool: ...
    def create_project(self) -> None: ...
    def secret_exists(self, name: str) -> bool: ...
    def create_secret(self, name: str, value: str) -> None: ...
    def factory_registered(self) -> bool: ...
    def factory_mcp_url(self) -> str | None: ...
    def can_manage_keys(self) -> bool:
        """Whether this machine can make Union API keys and policies (`flyteplugins-union`, Linux x86_64)."""
        ...

    def create_api_key(self, name: str) -> IssuedKey:
        """Make a key with no default policies; raises `KeyExists` when Union has one of that name."""
        ...

    def delete_api_key(self, client_id: str) -> None: ...
    def ensure_policy(self, name: str) -> None:
        """Make sure policy `name` binds `contributor` to the settings' project and domain, and nothing else.

        Creates it when missing; raises `BootstrapError` when it exists with other bindings, and
        `AccessDenied` when Union refuses this person the right to read or make it.
        """
        ...

    def assign_policy(self, client_id: str, policy: str) -> None:
        """Give `policy` to the key `client_id`; an existing assignment is fine. Raises `AccessDenied`."""
        ...


class Deployer(Protocol):
    """Runs the two deploy scripts; the real one starts `ops/deploy_*.py` as subprocesses."""

    def api_key_available(self) -> bool: ...
    def deploy(self, profile: Path, *, work: bool = True) -> str | None:
        """Deploy the work slots (when `work`) and the factory; return the factory-mcp URL if printed."""
        ...

    def close(self) -> None:
        """Release what `deploy` made (the ephemeral API key); called once at the end of every run."""
        ...


@dataclass
class AppRegistration:
    """A registered GitHub App. `pem` is the private key: kept in memory, never printed."""

    app_id: str
    slug: str
    pem: str = field(repr=False)


#: The `--domain` default.
DEFAULT_DOMAIN = "development"


@dataclass
class Settings:
    """The values the person gave on the command line."""

    endpoint: str
    project: str
    workspace: str  # owner/repo
    state_uri: str
    domain: str = DEFAULT_DOMAIN
    backend: str = "union"
    engine: str = "claude"
    no_app: bool = False
    no_board: bool = False
    directory: Path | None = None
    dry_run: bool = False
    redeploy: bool = False
    status_only: bool = False

    @property
    def read_only(self) -> bool:
        """Whether this run must change nothing and open no browser (`--status`, `--dry-run`)."""
        return self.dry_run or self.status_only

    @property
    def owner(self) -> str:
        return self.workspace.split("/", 1)[0]

    @property
    def repo_name(self) -> str:
        return self.workspace.split("/", 1)[1]

    @property
    def clone(self) -> Path:
        return self.directory or Path.home() / self.repo_name


@dataclass
class Context:
    """Settings, adapters and what earlier steps learned. `say` prints; `ask_secret` is a hidden prompt."""

    settings: Settings
    github: GitHubOps
    flyte: FlyteOps
    deployer: Deployer
    say: Callable[[str], None] = print
    ask_secret: Callable[[str], str] = getpass.getpass
    # Reads a line from the person: "press Enter once you have installed the App".
    ask_line: Callable[[str], str] = input
    which: Callable[[str], str | None] = shutil.which
    run_command: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = run_captured
    write_tty: Callable[[str], None] = write_to_terminal
    # `sys.platform`: the tools step picks its install commands by it.
    platform: str = sys.platform
    sleep: Callable[[float], None] = time.sleep
    # Learned while running: the factory-mcp URL, the App just registered, and so on.
    facts: dict[str, str] = field(default_factory=dict)

    @property
    def profile_path(self) -> Path:
        return self.settings.clone / PROFILE_FILE


class Step(Protocol):
    """One row of the table.

    `check` reads live state; `do` changes it and is called only when `check` is not ok.
    """

    id: str
    #: Shown on every run, never "done": a step that only tells the person something (`connect`).
    informational: bool
    #: A step that ends `todo` or `blocked` stops the run unless it is advisory (a ruleset the plan forbids).
    advisory: bool

    def check(self, ctx: Context) -> Check: ...
    def plan(self, ctx: Context) -> str: ...
    def do(self, ctx: Context) -> Outcome: ...
