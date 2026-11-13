"""Offline stand-ins for the bootstrap's adapters: a GitHub with real local bare repositories, and a Flyte."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from flyte_agent_factory import _workspace
from flyte_agent_factory._bootstrap_base import (
    AccessDenied,
    AppRegistration,
    BootstrapError,
    Context,
    IssuedKey,
    KeyExists,
    NotAllowed,
    NotSignedIn,
    Settings,
)

CLAUDE_TOKEN = "sentinel-claude-token-4f9a1c"
APP_PEM = (  # gitleaks:allow
    "-----BEGIN RSA PRIVATE KEY-----\nsentinel-app-pem-77be20\n-----END RSA PRIVATE KEY-----\n"
)
DEPLOY_SENTINEL = "sentinel-deploy-credential"
KEY_SECRET = "sentinel-union-key-secret-31c8"  # gitleaks:allow (a test sentinel, not a key)


class Interrupted(RuntimeError):
    """Stands in for a crash, a lost connection or Ctrl-C at one effect."""


class World:
    """Everything that exists outside the process, plus the log of every change made to it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.events: list[str] = []
        self.interrupt_at: int | None = None  # the n-th change (1-based) raises before it takes effect
        self.repos: set[str] = set()
        self.rulesets: dict[str, bool] = {}  # name -> whether the DeployKey may bypass it
        self.ruleset_forbidden = False
        self.deploy_keys: dict[str, str] = {}
        self.board_linked = False
        self.board_scope_missing = False
        self.app_installed = True
        self.projects: set[str] = set()
        self.secrets: dict[str, str] = {}
        self.signed_in = False
        self.registered = False
        self.mcp_url: str | None = None
        self.api_key = True
        self.dead = False
        self.closed = 0  # times the run released the deployer (the ephemeral API key)
        self.login = "pat"  # the signed-in GitHub user
        self.org = False  # the owner is an organization, whose App installations can be listed
        self.installations = ["agent-factory-pat"]
        self.apps: dict[str, str] = {}  # slug -> id of the Apps registered on GitHub under the owner
        self.app_slug = "agent-factory-pat"  # the slug GitHub gives the next App registered
        self.visibility: dict[str, str] = {}  # repo -> "public"; anything else is private
        self.can_manage_keys = True
        self.union_keys: dict[str, str] = {}  # key name -> client id of the Union API keys that exist
        self.policies: dict[str, list[tuple[str, str, str]]] = {}  # name -> (role, project, domain) bindings
        self.assignments: list[tuple[str, str]] = []  # (client id, policy)
        self.assign_error = False
        # Union refuses the person the right to manage access (a policy, an assignment): another admin grants.
        self.policy_denied = False
        self.assign_denied = False
        self.tty: list[str] | None = []  # what reached the terminal; None when there is no terminal
        self.commands: list[list[str]] = []  # every command run through `run_command`
        self.claude_list = "No MCP servers configured."  # what `claude mcp list` prints
        self.secret_error: str | None = None  # creating this secret fails (a call that errors, not a crash)

    def change(self, what: str) -> None:
        # A crash ends the process: nothing after it (a rollback, say) runs either.
        if self.interrupt_at is None:
            self.dead = False
        elif self.dead or len(self.events) + 1 == self.interrupt_at:
            self.dead = True
            raise Interrupted(f"interrupted at {what}")
        self.events.append(what)

    def count(self, prefix: str) -> int:
        return sum(e.startswith(prefix) for e in self.events)

    def bare(self, repo: str) -> Path:
        return self.root / "remotes" / (repo.replace("/", "__") + ".git")


class FakeGitHub:
    def __init__(self, world: World) -> None:
        self.w = world

    def authenticated(self) -> bool:
        return True

    def _need_repo(self, repo: str) -> None:
        """As `gh` does: asking about a repository that is not there is an error, not an empty answer."""
        if repo not in self.w.repos:
            raise BootstrapError(
                f"gh api graphql failed (1): Could not resolve to a Repository with the name {repo!r}"
            )

    def owner_is_org(self, owner: str) -> bool:
        return self.w.org

    def viewer_login(self) -> str:
        return self.w.login

    def repo_visibility(self, repo: str) -> str | None:
        if repo not in self.w.repos:
            return None
        return self.w.visibility.get(repo, "private")

    def clone_origin_is(self, checkout: Path, repo: str) -> bool:
        url = _workspace.git(checkout, "config", "--get", "remote.origin.url", placeholder_identity=False)
        return Path(url.strip()) == self.w.bare(repo)

    def create_repo(self, repo: str) -> None:
        self.w.change(f"create_repo {repo}")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.w.bare(repo))], check=True)
        self.w.repos.add(repo)

    def clone(self, repo: str, dest: Path) -> None:
        self.w.change(f"clone {repo}")
        subprocess.run(
            ["git", "clone", "-q", str(self.w.bare(repo)), str(dest)], check=True, capture_output=True
        )

    def push(self, checkout: Path) -> None:
        self.w.change("push")
        _workspace.git(checkout, "push", "-q", "-u", "origin", "HEAD", placeholder_identity=False)

    def ruleset_exists(self, repo: str, name: str) -> bool:
        return name in self.w.rulesets

    def ruleset_deploy_key_bypass(self, repo: str, name: str) -> bool:
        return self.w.rulesets[name]

    def create_ruleset(self, repo: str, name: str, deploy_key_bypass: bool = False) -> None:
        if self.w.ruleset_forbidden:
            raise NotAllowed("HTTP 403: Upgrade to GitHub Pro")
        self.w.change(f"create_ruleset {name}")
        self.w.rulesets[name] = deploy_key_bypass

    def update_ruleset(self, repo: str, name: str, deploy_key_bypass: bool) -> None:
        if self.w.ruleset_forbidden:
            raise NotAllowed("HTTP 403: Upgrade to GitHub Pro")
        self.w.change(f"update_ruleset {name}")
        self.w.rulesets[name] = deploy_key_bypass

    def deploy_key_titles(self, repo: str) -> list[str]:
        self._need_repo(repo)
        return list(self.w.deploy_keys)

    def add_deploy_key(self, repo: str, public_key_file: Path, title: str) -> None:
        self.w.change(f"add_deploy_key {title}")
        public = public_key_file.read_text()
        assert public.startswith("ssh-ed25519 "), "only the public half goes to GitHub"
        self.w.deploy_keys[title] = public

    def delete_deploy_key(self, repo: str, title: str) -> None:
        self.w.change(f"delete_deploy_key {title}")
        self.w.deploy_keys.pop(title, None)

    def board_linked(self, repo: str) -> bool:
        self._need_repo(repo)
        if self.w.board_scope_missing:
            raise NotAllowed("missing scope project")
        return self.w.board_linked

    def create_board(self, owner: str, repo: str, title: str) -> str:
        if self.w.board_scope_missing:
            raise NotAllowed("missing scope project")
        self.w.change(f"create_board {title}")
        self.w.board_linked = True
        return f"https://github.com/users/{owner}/projects/1"

    def app_installations(self, owner: str) -> list[str]:
        return list(self.w.installations)

    def register_app(self, owner: str, repo: str, say) -> str:
        # GitHub makes the App when the person submits the form; the conversion only hands over its key.
        self.w.change("register_app")
        assert self.w.app_slug not in self.w.apps, "GitHub refuses a second App of the same name"
        self.w.apps[self.w.app_slug] = "424242"
        return "code-from-github"

    def exchange_manifest_code(self, code: str) -> AppRegistration:
        assert code == "code-from-github"
        self.w.change("exchange_manifest_code")
        return AppRegistration(app_id=self.w.apps[self.w.app_slug], slug=self.w.app_slug, pem=APP_PEM)

    def app_exists(self, owner: str, slug: str) -> bool:
        return slug in self.w.apps

    def app_installed(self, registration: AppRegistration, repo: str) -> bool:
        return self.w.app_installed


class FakeFlyte:
    def __init__(self, world: World, settings: Settings) -> None:
        self.w = world
        self.s = settings
        self.config_path = world.root / "flyte-config.yaml"

    def config_endpoint(self) -> str | None:
        if not self.config_path.exists():
            return None
        return self.config_path.read_text().split("endpoint:")[1].strip()

    def create_config(self) -> None:
        self.w.change("create_config")
        self.config_path.write_text(f"admin:\n  endpoint: dns:///{self.s.endpoint}\n")

    def sign_in(self) -> None:
        if not self.w.signed_in:
            if self.s.read_only:  # as the real adapter: a read-only run never starts the browser sign-in
                raise NotSignedIn("not signed in, and a read-only run does not open the browser")
            self.w.change("sign_in")
            self.w.signed_in = True

    def _init(self) -> None:
        # As the real adapter: every call but the sign-in itself needs the config the sign-in writes.
        if not self.config_path.exists():
            raise NotSignedIn(f"no Flyte config at {self.config_path}: sign in first")

    def project_exists(self) -> bool:
        self._init()
        return self.s.project in self.w.projects

    def create_project(self) -> None:
        self._init()
        self.w.change(f"create_project {self.s.project}")
        self.w.projects.add(self.s.project)

    def secret_exists(self, name: str) -> bool:
        self._init()
        return name in self.w.secrets

    def create_secret(self, name: str, value: str) -> None:
        self._init()
        assert name not in self.w.secrets, f"secret {name} would be rotated"
        if name == self.w.secret_error:
            raise BootstrapError(f"creating secret {name} failed: ConnectError")
        self.w.change(f"create_secret {name}")
        self.w.secrets[name] = value

    def factory_registered(self) -> bool:
        self._init()
        return self.w.registered

    def factory_mcp_url(self) -> str | None:
        self._init()
        return self.w.mcp_url

    def can_manage_keys(self) -> bool:
        return self.w.can_manage_keys

    def create_api_key(self, name: str) -> IssuedKey:
        self._init()
        if name in self.w.union_keys:
            raise KeyExists(name)
        self.w.change(f"create_api_key {name}")
        self.w.union_keys[name] = f"client-{name}"
        return IssuedKey(client_id=f"client-{name}", encoded=KEY_SECRET)

    def delete_api_key(self, client_id: str) -> None:
        self.w.change(f"delete_api_key {client_id}")
        self.w.union_keys = {n: c for n, c in self.w.union_keys.items() if c != client_id}

    def ensure_policy(self, name: str) -> None:
        self._init()
        wanted = ("contributor", self.s.project, self.s.domain)
        if self.w.policy_denied:
            raise AccessDenied(f"managing policy {name!r} was refused")
        if name not in self.w.policies:
            self.w.change(f"create_policy {name}")
            self.w.policies[name] = [wanted]
        elif self.w.policies[name] != [wanted]:
            raise BootstrapError(f"policy {name!r} exists but does not bind exactly contributor")

    def assign_policy(self, client_id: str, policy: str) -> None:
        if self.w.assign_error:
            raise BootstrapError("assigning failed")
        if self.w.assign_denied:
            raise AccessDenied(f"assigning policy {policy!r} was refused")
        self.w.change(f"assign_policy {policy}")
        self.w.assignments.append((client_id, policy))


class FakeDeployer:
    def __init__(self, world: World) -> None:
        self.w = world

    def api_key_available(self) -> bool:
        return self.w.api_key

    def deploy(self, profile: Path, *, work: bool = True) -> str | None:
        self.w.change(f"deploy work={work}")
        self.w.registered = True
        self.w.mcp_url = "https://factory-mcp.example.test"
        return self.w.mcp_url

    def close(self) -> None:
        self.w.closed += 1


def make_context(world: World, settings: Settings, out: list[str], token: str = CLAUDE_TOKEN) -> Context:
    return Context(
        settings,
        FakeGitHub(world),
        FakeFlyte(world, settings),
        FakeDeployer(world),
        say=out.append,
        ask_secret=lambda prompt: token,
        ask_line=lambda prompt: "",
        which=lambda name: f"/usr/bin/{name}",
        run_command=lambda argv: _run_command(world, argv),
        write_tty=lambda text: _write_tty(world, text),
        sleep=lambda seconds: None,
    )


def _run_command(world: World, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    world.commands.append(list(argv))
    if list(argv[:3]) == ["claude", "mcp", "list"]:
        return subprocess.CompletedProcess(argv, 0, world.claude_list, "")
    if list(argv[:3]) == ["claude", "mcp", "add"]:
        world.claude_list += "\nagent-factory: https://x - connected"
        return subprocess.CompletedProcess(argv, 0, f"Added; header {KEY_SECRET}", "")
    raise AssertionError(f"unexpected command {argv}")


def _write_tty(world: World, text: str) -> None:
    if world.tty is None:
        raise OSError("no controlling terminal")
    world.tty.append(text)
