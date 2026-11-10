"""Deployment profiles: every value that differs between one deployment and another.

A profile is one TOML file (`profiles/<name>.toml`, documented in `profiles/README.md`)
selected with `AGENT_FACTORY_PROFILE=<name or path>` or `--profile` on the ops tools.

A pod cannot read the file: the code bundle carries `.py` modules only. Deploys therefore put
the profile's text into the environment definition (`pod_env`), and `current()` reads it back
from there, so the same accessors work on an operator's machine and inside a task.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, TypedDict, cast

from . import _git, _github, _names, _standards

PROFILE_ENV = "AGENT_FACTORY_PROFILE"
#: The profile's text, set on deployed environments. Not a `AGENT_FACTORY_<ROLE>_*` policy key.
TOML_ENV = "AGENT_FACTORY_PROFILE_TOML"
# Overridable so a test can point at an empty folder (the owner's `.default` is otherwise found).
PROFILES_DIR = Path(
    _names.getenv("AGENT_FACTORY_PROFILES_DIR") or Path(__file__).resolve().parents[2] / "profiles"
)
#: Names the profile to use when nothing else selects one. A local, git-ignored file, so a
#: pod or shell deployed before profiles existed keeps working: `echo <name> > profiles/.default`.
DEFAULT_FILE = ".default"
#: A role-policy key becomes `AGENT_FACTORY_<key>`; the profile's own variables must not be
#: reachable through it.
_POLICY_KEY = re.compile(r"[A-Z0-9_]+")
#: Policy keys that name an engine: the coordinator's, and its old name.
_ENGINE_KEYS = ("COORDINATOR_ENGINE", "AUTHOR_ENGINE")
#: The marker a skeleton profile leaves for the values its owner must fill in.
PLACEHOLDER = "TODO-"
BACKENDS = ("union", "oss")
#: `roles/owner`, `projects/p/roles/custom`, `organizations/1/roles/custom`.
_GCP_ROLE = re.compile(r"(projects/[a-z0-9-]+/|organizations/\d+/)?roles/[A-Za-z0-9_.]+")
#: `compute` (short form) or `compute.googleapis.com`.
_GCP_API = re.compile(r"[a-z0-9-]+(\.googleapis\.com)?")
#: `s3://bucket/prefix`, `file:///tmp/x`: a URI with a scheme `flyte.storage` reaches, from the list below.
_TAILSCALE_TAG = re.compile(r"tag:[a-z0-9-]+")
_URI_SCHEME = re.compile(r"([a-z][a-z0-9+.-]*)://\S+")
_STATE_SCHEMES = ("s3", "gs", "gcs", "abfs", "abfss", "file")


class _GcpFields(TypedDict, total=False):
    secret_name: str


class _GcpLists(TypedDict, total=False):
    roles: tuple[str, ...]
    apis: tuple[str, ...]


class ProfileError(ValueError):
    """The profile is missing, unreadable or invalid. The message names the key."""


@dataclass(frozen=True)
class Backend:
    kind: str  # "union" or "oss": recorded for docs and guards, not branched on at runtime
    endpoint: str
    project: str
    domain: str
    org: str = ""
    console_host: str = ""
    signin_host: str = ""
    config: str = ""  # the Flyte config file `make` passes to the CLI


@dataclass(frozen=True)
class Slot:
    env: str  # the agent TaskEnvironment's name; its project task is `<env>.project`
    subagent_env: str
    policy: Mapping[str, str] = MappingProxyType({})  # this slot's role policy, over the global one
    #: A ChatGPT login (`codex login --device-auth`'s `auth.json`) stored as a secret, for this slot only. It
    #: refreshes and rotates, so no two slots may share one; on this slot it takes the place of `codex_token`.
    codex_login: str = ""

    @property
    def task(self) -> str:
        return f"{self.env}.project"


#: Projects a token profile without `[[slots]]` runs at once when `[concurrency]` is absent. A guess to be
#: measured: no sharing-probe result (`ops/probes/setup_token.py --max-k`) is recorded in this repo.
DEFAULT_MAX_PROJECTS = 4
#: The one environment of a token profile without `[[slots]]`; every project runs on it, so names are fixed.
SHARED_SLOT = Slot(env="agent-shared", subagent_env="agent-shared-sub")
ENGINES = ("claude", "codex")


def normalize_engine(value: str) -> str:
    """Return an engine name as `ENGINES` spells it (a pin may be written ` Codex `)."""
    return value.strip().lower()


#: `[secrets]` keys `_fields` may leave out. `deploy_key` is required there, and `claude_token` right after.
OPTIONAL_SECRETS = ("gateway_key", "api_key", "claude_token", "codex_token")
#: Policy keys read as numbers, with the check their reader applies (the readers fall back to a default
#: on a bad value; a profile fails instead). `PARK_RELEASE_MIN`: 0 keeps a parked session for ever.
_NUMERIC_POLICY: Mapping[str, tuple[type[int] | type[float], float, str]] = MappingProxyType(
    {
        "PARK_RELEASE_MIN": (float, 0, "a finite number of minutes, 0 or more"),
        "DELEGATE_MAX": (int, 1, "a whole number, 1 or more"),
    }
)


@dataclass(frozen=True)
class Concurrency:
    """How many projects a token profile without `[[slots]]` runs at once."""

    max_projects: int = DEFAULT_MAX_PROJECTS
    per_engine: Mapping[str, int] = MappingProxyType({})  # engine -> its own cap, within `max_projects`


@dataclass(frozen=True)
class Secrets:
    deploy_key: str
    gateway_key: str = ""
    api_key: str = ""
    claude_token: str = ""
    codex_token: str = ""


@dataclass(frozen=True)
class Mcp:
    public_url: str = ""
    # Keys of [repo] that chat may launch projects into; the project repo unless the profile says more.
    repos: tuple[str, ...] = ("project",)


@dataclass(frozen=True)
class Gcp:
    """Defaults for `gcp_bootstrap`; an argument to the task wins over each of them."""

    secret_name: str = ""
    roles: tuple[str, ...] = ()
    apis: tuple[str, ...] = ()


@dataclass(frozen=True)
class Deploy:
    auto: bool = False  # merged factory changes deploy themselves (the `factory-deployer` task)


@dataclass(frozen=True)
class Planner:
    """Admission policy; cutover requires single-writer deployment ownership."""

    admission: str = "legacy"


@dataclass(frozen=True)
class State:
    uri: str = ""  # object-storage prefix for legacy queue state


@dataclass(frozen=True)
class Github:
    """The GitHub App the deployer reads statuses and source through, and projects open pull requests as."""

    app_id: str = ""
    private_key_secret: str = ""  # Flyte secret holding the App's PEM private key
    webhook_secret: str = ""  # Mounted only in factory-webhook, never in project slots
    maintainers: tuple[str, ...] = ()  # lowercased logins allowed to submit issues and direction
    intake_label: str = "factory"

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.private_key_secret)

    @property
    def intake(self) -> bool:
        """Issue intake is on: the webhook delivers issue events and `factory-issues` is deployed."""
        return bool(self.webhook_secret and self.maintainers)


@dataclass(frozen=True)
class Board:
    owner: str
    owner_type: str
    number: int
    pat_secret: str
    pat_expires: str = ""
    #: The board's GraphQL node id (PVT_...). Optional; when set, board webhooks from other boards of
    #: the same organization are ignored.
    node_id: str = ""


@dataclass(frozen=True)
class Direction:
    """The repository the deployer reads this profile from, apart from the engine repo `[repo] factory`."""

    repo: str = ""
    profile: str = ""  # path inside `repo`; parse() fills in `profiles/<name>.toml`
    ref: str = ""  # branch; empty is the repository's default branch


_IDENTITY_NAME = re.compile(r"[a-z][a-z0-9-]{0,19}")
#: The identity a legacy `[browser] personal = true` profile gets: its app, secret and archive keep working.
LEGACY_IDENTITY = "personal"


@dataclass(frozen=True)
class BrowserIdentity:
    """One signed-in personal browser: its own app, token secret and profile archive."""

    name: str
    profile_uri: str
    exit_node: str | None = None  # None inherits `[browser] exit_node`; "" is direct egress

    @property
    def app(self) -> str:
        return f"claude-browser-{self.name}"

    @property
    def token_secret(self) -> str:
        return f"browser_{self.name.replace('-', '_')}_token"

    @property
    def token_env(self) -> str:
        return self.token_secret.upper()


@dataclass(frozen=True)
class Browser:
    """`[browser]`: the in-pod browser's exit node and the named personal browser identities."""

    exit_node: str = ""  # the in-pod browser's, and every identity's default
    tailscale_secret: str = ""
    profile_uri: str = ""  # legacy `personal = true` only: becomes the `personal` identity
    personal_env: str = ""  # deprecated: accepted and ignored, no longer grants a slot anything
    tailscale_tag: str = "tag:k8s"
    identities: tuple[BrowserIdentity, ...] = ()  # ordered by name

    @property
    def personal(self) -> bool:
        """Whether the profile has at least one personal browser identity."""
        return bool(self.identities)

    def identity(self, name: str) -> BrowserIdentity | None:
        return next((i for i in self.identities if i.name == name), None)


@dataclass(frozen=True)
class Bench:
    """The factory benchmark (`_bench_flow`): a repository of its own; its cases run on the work token."""

    repo: str  # the repository cases run in; `[repo] project` (the sandbox) when the table leaves it out
    repo_key: str  # that repository's deploy-key secret
    max_usd: float = 0.0  # the most one benchmark may commit to: the sum of its cases' cost caps
    claude_token: str = ""  # the secret the cases run on: `[secrets] claude_token` unless `[bench]` names one
    token_concurrency: int = 1  # cases at once, one bench env each


@dataclass(frozen=True)
class Loop:
    enabled: bool = False
    repo: str = ""
    merge: str = "dry-run"
    daily_usd: float = 0
    weekly_usd: float = 0
    owner_floor_pct: float = 30


@dataclass(frozen=True)
class Gardening:
    """How hard the weekly gardening project (`_gardening`) is guaranteed a slot."""

    min_runs_per_week: int = 1
    max_days_between: int = 7
    escalate_on_regression: bool = True
    max_open_issues: int = 10


@dataclass(frozen=True)
class JudgementPolicy:
    """Extras to the judge's built-in rules (`_judgement`); a profile adds to them and cannot remove any."""

    owner_reserved: tuple[str, ...] = ()
    standing_direction: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    name: str
    text: str  # the source, so a deploy can hand it to the pod
    backend: Backend
    repo: str  # the repository projects are pushed to
    repos: Mapping[str, str]  # every [repo] entry by key ("project" is `repo`)
    repo_keys: Mapping[str, str]  # [repo] key -> its deploy-key secret, beside [secrets] deploy_key
    slots: tuple[Slot, ...]
    cap: int
    secrets: Secrets
    policy: Mapping[str, str]  # AGENT_FACTORY_<key> -> value
    # Set when there are no `[[slots]]`: `slots` is then `SHARED_SLOT` alone and `cap` is its max_projects.
    concurrency: Concurrency | None = None
    mcp: Mcp | None = None
    gcp: Gcp | None = None
    browser: Browser = Browser()
    deploy: Deploy = Deploy()
    planner: Planner = Planner()
    state: State = State()
    github: Github = Github()
    board: Board | None = None
    direction: Direction | None = None
    bench: Bench | None = None
    loop: Loop = Loop()
    gardening: Gardening = Gardening()
    judgement: JudgementPolicy = JudgementPolicy()
    # [repo] key -> its earlier URLs (a GitHub rename): requests and projects from before still name them.
    repo_aliases: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    # How many projects on `[repo] factory` may run at once.
    factory_projects: int = 1
    standards: _standards.Standards = field(default_factory=_standards.Standards)
    standards_payload: str = field(default="", compare=False)
    source_dir: Path = field(default=Path("."), compare=False)
    source_root: Path | None = field(default=None, compare=False)

    def deployment_text(self) -> str:
        profile = self
        if not self.standards_payload and self.direction and self.source_root is not None:
            profile = _with_tree(self, self.name, self.source_root, workspace=True)
        payload = profile.standards_payload or _standards.encode(
            _standards.capture(profile.standards, profile.source_dir, root=profile.source_root)
        )
        # Put the internal bundle before all tables so it remains a top-level key.
        return f'standards_payload = "{payload}"\n' + self.text

    def captured_standards(self) -> dict[str, str]:
        """Return the standards this profile carries: its captured bundle, else a capture from its origin.

        As at deploy (`deployment_text`), a direction checkout's own `standards/` is the default when
        `[standards] dir` is unset; an engine checkout's never is (docs/standards.md).
        """
        if self.standards_payload:
            return _standards.decode(self.standards_payload)
        standards = self.standards
        if self.direction and self.source_root is not None and not standards.dir:
            default = self.source_root.resolve() / "standards"
            if default.is_dir():
                standards = replace(standards, dir=str(default))
        return _standards.capture(standards, self.source_dir, root=self.source_root)

    @property
    def shared(self) -> bool:
        """Whether every project runs on one environment (a token profile without `[[slots]]`)."""
        return self.concurrency is not None

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(s.task for s in self.slots)

    def slot(self, env: str) -> Slot:
        for s in self.slots:
            if s.env == env:
                return s
        raise KeyError(env)

    @property
    def meter_logins(self) -> dict[str, str]:
        """Logical account IDs and secret bindings: the work token, and the Codex token when there is one."""
        bindings = {f"claude {self.secrets.claude_token}": self.secrets.claude_token}
        if self.secrets.codex_token:
            bindings[f"codex {self.secrets.codex_token}"] = self.secrets.codex_token
        for slot in self.slots:
            if slot.codex_login:
                bindings[f"codex {slot.codex_login}"] = slot.codex_login
        return bindings

    def codex_secret(self, slot: Slot) -> str:
        """Return the Codex credential `slot` runs on: its own login, else the shared token, else ""."""
        return slot.codex_login or self.secrets.codex_token

    @property
    def has_codex(self) -> bool:
        """Whether any slot can run Codex."""
        return bool(self.secrets.codex_token or any(s.codex_login for s in self.slots))

    @property
    def console_base(self) -> str:
        """The console URL for this project, or "" when the backend has no console."""
        b = self.backend
        if not b.console_host:
            return ""
        return f"https://{b.console_host}/v2/domain/{b.domain}/project/{b.project}"

    @property
    def agent_secrets(self) -> str:
        """`AGENT_FACTORY_SECRETS` for a project slot: `secret=ENV_VAR,...`, set parts only."""
        s = self.secrets
        # PROJECT_ANTHROPIC_API_KEY, never ANTHROPIC_API_KEY: see _session_config._PROJECT_API_KEY_VAR.
        pairs = (
            (s.deploy_key, _git.GIT_DEPLOY_KEY_ENV_VAR),
            *((secret, _git.repo_key_env_var(key)) for key, secret in self.repo_keys.items()),
            (s.gateway_key, "LLM_GATEWAY_KEY"),
            (s.api_key, "PROJECT_ANTHROPIC_API_KEY"),
            # The supervisor removes it from os.environ (both names Flyte injects) before it spawns anything.
            (self.github.private_key_secret, "GITHUB_APP_PRIVATE_KEY"),
            (self.board.pat_secret if self.board else "", "GITHUB_BOARD_PAT"),
        )
        return ",".join(f"{name}={var}" for name, var in pairs if name)

    @property
    def repo_key_vars(self) -> dict[str, str]:
        """Repository URL -> the variable its `[repo_keys]` deploy key is mounted as.

        A `[repo_aliases]` URL maps to its repo's variable too: a project launched on the old URL
        still gets the key that reaches the (renamed) repository, not the default one.
        """
        return {
            url: _git.repo_key_env_var(key)
            for key in self.repo_keys
            for url in (*self.repo_aliases.get(key, ()), self.repos[key])
        }

    def canonical_repo(self, url: str) -> str:
        """`url`'s current `[repo]` URL when it is one of `[repo_aliases]`, else `url` unchanged."""
        if url.strip():
            identity = _git._repo_identity(url)
            for key, aliases in self.repo_aliases.items():
                if any(_git._repo_identity(alias) == identity for alias in aliases):
                    return self.repos[key]
        return url

    def deploy_env(self, slot: Slot) -> dict[str, str]:
        """Return what a project slot's deploy is built from (read by `agent` at import)."""
        env = {
            "AGENT_FACTORY_ENV_NAME": slot.env,
            "AGENT_FACTORY_SUBAGENT_ENV_NAME": slot.subagent_env,
            "AGENT_FACTORY_CLAUDE_TOKEN_SECRET": self.secrets.claude_token,
            "AGENT_FACTORY_BROWSER": "1",
            "AGENT_FACTORY_SECRETS": self.agent_secrets,
        }
        from . import _browser_exit_node

        env.update(_browser_exit_node.deploy_env(self))
        if codex := self.codex_secret(slot):
            env["AGENT_FACTORY_CODEX_TOKEN_SECRET"] = codex
        if self.repo_keys:
            env[_git.REPO_KEYS_ENV_VAR] = json.dumps(self.repo_key_vars)
        env.update(self.policy_env_for(slot))
        return env

    @property
    def policy_env(self) -> dict[str, str]:
        """The role policy as `AGENT_FACTORY_<KEY>` variables, as the deploy shell used to set them."""
        return {f"AGENT_FACTORY_{k}": v for k, v in self.policy.items()}

    def policy_env_for(self, slot: Slot | None) -> dict[str, str]:
        """`policy_env` with `slot`'s own policy merged over it (just the global one for no slot)."""
        merged = {**self.policy, **(slot.policy if slot else {})}
        return {f"AGENT_FACTORY_{k}": v for k, v in merged.items()}


def _table(data: Mapping[str, Any], key: str, where: str, *, required: bool = True) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None:
        if required:
            raise ProfileError(f"{where}: missing table [{key}]")
        return {}
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: [{key}] must be a table")
    return value


def _policy(table: Mapping[str, Any], section: str, where: str) -> Mapping[str, str]:
    for key, value in table.items():
        if not _POLICY_KEY.fullmatch(key) or key.startswith((_names.PREFIX, "CLAUDE_", "PROFILE")):
            raise ProfileError(
                f"{where}: [{section}] {key!r} must be A-Z, 0-9 and _, without an AGENT_FACTORY_ "
                "or CLAUDE_ prefix and not starting with PROFILE"
            )
        if not isinstance(value, str):
            raise ProfileError(f"{where}: [{section}] {key} must be a string")
        if key in _ENGINE_KEYS and value and normalize_engine(value) not in ENGINES:
            raise ProfileError(f"{where}: [{section}] {key} = {value!r} must be one of {ENGINES}")
        if key in _NUMERIC_POLICY:
            kind, minimum, wanted = _NUMERIC_POLICY[key]
            try:
                number = kind(value)
                if not math.isfinite(number) or number < minimum:
                    raise ValueError(value)
            except ValueError:
                raise ProfileError(f"{where}: [{section}] {key} = {value!r} must be {wanted}") from None
    out = dict(table)
    # AUTHOR_ENGINE is the old name of COORDINATOR_ENGINE: everything downstream reads only the new one.
    # Folded per table, so a slot's COORDINATOR_ENGINE still overrides the profile's AUTHOR_ENGINE.
    if alias := out.pop("AUTHOR_ENGINE", ""):
        if out.get("COORDINATOR_ENGINE") and normalize_engine(out["COORDINATOR_ENGINE"]) != normalize_engine(
            alias
        ):
            raise ProfileError(
                f"{where}: [{section}] AUTHOR_ENGINE = {alias!r} and COORDINATOR_ENGINE = "
                f"{out['COORDINATOR_ENGINE']!r} disagree; set only COORDINATOR_ENGINE"
            )
        _warn_once(f"{where}: [{section}] AUTHOR_ENGINE is deprecated; use COORDINATOR_ENGINE")
        out.setdefault("COORDINATOR_ENGINE", alias)
    if out.get("COORDINATOR_ENGINE"):
        out["COORDINATOR_ENGINE"] = normalize_engine(out["COORDINATOR_ENGINE"])
    return MappingProxyType(out)


def _placeholders(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _placeholders(v, f"{path}.{k}" if path else k)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _placeholders(v, f"{path}[{i}]")]
    return [path] if isinstance(value, str) and PLACEHOLDER in value else []


def _fields(
    table: Mapping[str, Any], section: str, where: str, required: tuple[str, ...], optional: tuple[str, ...]
) -> dict[str, str]:
    unknown = sorted(set(table) - set(required) - set(optional))
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) in [{section}]: {', '.join(unknown)}")
    out = {}
    for key in (*required, *optional):
        if key not in table:
            if key in required:
                raise ProfileError(f"{where}: missing key {key!r} in [{section}]")
            continue
        value = table[key]
        if not isinstance(value, str) or (key in required and not value.strip()):
            raise ProfileError(f"{where}: [{section}] {key} must be a non-empty string")
        out[key] = value.strip()
    return out


def _browser(data: Mapping[str, Any], where: str) -> Browser:
    """Parse `[browser]`, including the legacy `personal = true` shape (one identity named `personal`)."""
    table = dict(_table(data, "browser", where, required=False))
    legacy = table.pop("personal", False)
    if not isinstance(legacy, bool):
        raise ProfileError(f"{where}: [browser] personal must be true or false")
    identity_tables = table.pop("identities", {})
    if not isinstance(identity_tables, dict) or not all(
        isinstance(t, dict) for t in identity_tables.values()
    ):
        raise ProfileError(f"{where}: [browser.identities] must hold one table per identity")
    fields = _fields(
        table,
        "browser",
        where,
        (),
        ("exit_node", "tailscale_secret", "profile_uri", "personal_env", "tailscale_tag"),
    )
    tag = fields.get("tailscale_tag", "")
    if "tailscale_tag" in fields and not _TAILSCALE_TAG.fullmatch(tag):
        raise ProfileError(f"{where}: [browser] tailscale_tag must look like 'tag:name', not {tag!r}")

    identities = []
    for name, identity_table in identity_tables.items():
        if not _IDENTITY_NAME.fullmatch(name):
            raise ProfileError(
                f"{where}: [browser.identities.{name}] name must match {_IDENTITY_NAME.pattern}"
            )
        values = _fields(
            identity_table, f"browser.identities.{name}", where, ("profile_uri",), ("exit_node",)
        )
        identities.append(BrowserIdentity(name, values["profile_uri"], values.get("exit_node")))
    if legacy:
        if LEGACY_IDENTITY in identity_tables:
            raise ProfileError(
                f"{where}: [browser] personal = true and [browser.identities.{LEGACY_IDENTITY}] both define "
                f"the {LEGACY_IDENTITY!r} identity; keep one"
            )
        if not fields.get("profile_uri"):
            raise ProfileError(f"{where}: personal browser requires profile_uri")
        identities.append(BrowserIdentity(LEGACY_IDENTITY, fields["profile_uri"]))
    uris = [i.profile_uri for i in identities]
    if len(set(uris)) != len(uris):
        raise ProfileError(f"{where}: [browser.identities] two identities share one profile_uri")

    browser = Browser(
        identities=tuple(sorted(identities, key=lambda i: i.name)),
        **{k: v for k, v in fields.items() if k != "tailscale_tag"},
        **({"tailscale_tag": tag} if tag else {}),
    )
    if browser.tailscale_secret and not browser.exit_node:
        raise ProfileError(f"{where}: [browser] tailscale_secret requires exit_node")
    if browser.exit_node and not (browser.personal or browser.tailscale_secret):
        raise ProfileError(f"{where}: [browser] in-pod exit_node requires tailscale_secret")
    return browser


#: Credential keys earlier profiles carried: still read, no longer used. key -> what replaces it.
_LEGACY_KEYS = {
    "[[slots]] claude_login": "[secrets] claude_token",
    "[secrets] claude_login": "[secrets] claude_token",
    "[secrets] codex_login": "[secrets] codex_token",
    "[bench] logins": "[secrets] claude_token",
}

_NO_CLAUDE_TOKEN = (
    "[secrets] claude_token is required: run `claude setup-token` and store the token as a Flyte secret "
    "(`flyte create secret <name>`), then name that secret in [secrets] claude_token"
)


def _check_pinned_engine(merged: Mapping[str, str], slot: Slot, secrets: Secrets, where: str) -> None:
    """Refuse a slot whose policy pins the Codex coordinator engine when the profile has no Codex token."""
    if merged.get("COORDINATOR_ENGINE") == "codex" and not (secrets.codex_token or slot.codex_login):
        raise ProfileError(
            f"{where}: [[slots]] {slot.env} pins COORDINATOR_ENGINE=codex but has no codex_login "
            "and the profile has no codex_token"
        )


def _warn_legacy(key: str, where: str) -> None:
    _warn_once(f"{where}: {key} is no longer used and is ignored: set {_LEGACY_KEYS[key]} instead")


def _slot(table: Any, where: str) -> Slot:
    if not isinstance(table, dict):
        raise ProfileError(f"{where}: [[slots]] entries must be tables")
    own = {k: v for k, v in table.items() if k != "policy"}
    policy = _policy(_table(table, "policy", where, required=False), "slots.policy", where)
    if "claude_login" in own:
        _warn_legacy("[[slots]] claude_login", where)
    fields = _fields(own, "slots", where, ("env", "subagent_env"), ("claude_login", "codex_login"))
    return Slot(
        env=fields["env"],
        subagent_env=fields["subagent_env"],
        policy=policy,
        codex_login=fields.get("codex_login", ""),
    )


def _parse_concurrency(table: Mapping[str, Any], where: str) -> Concurrency:
    fields = dict(table)
    max_projects = fields.pop("max_projects", DEFAULT_MAX_PROJECTS)
    if isinstance(max_projects, bool) or not isinstance(max_projects, int) or max_projects < 1:
        raise ProfileError(f"{where}: [concurrency] max_projects must be an integer of at least 1")
    per_engine = fields.pop("per_engine", {})
    if not isinstance(per_engine, dict):
        raise ProfileError(f"{where}: [concurrency] per_engine must be a table such as {{ claude = 3 }}")
    for engine, cap in per_engine.items():
        if engine not in ENGINES:
            raise ProfileError(f"{where}: [concurrency] per_engine names {engine!r}, not one of {ENGINES}")
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= max_projects:
            raise ProfileError(
                f"{where}: [concurrency] per_engine {engine} must be an integer from 1 to "
                f"max_projects ({max_projects})"
            )
    if fields:
        raise ProfileError(f"{where}: [concurrency] unknown key(s): {', '.join(sorted(fields))}")
    return Concurrency(max_projects=max_projects, per_engine=MappingProxyType(dict(per_engine)))


def _repo_keys(
    table: Mapping[str, Any], repos: Mapping[str, str], secrets: Secrets, where: str
) -> Mapping[str, str]:
    for key, value in table.items():
        if key == "project":
            raise ProfileError(
                f"{where}: [repo_keys] project: the project repository uses [secrets] deploy_key"
            )
        if key not in repos:
            raise ProfileError(f"{where}: [repo_keys] {key} is not a key of [repo] ({', '.join(repos)})")
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{where}: [repo_keys] {key} must be a non-empty string")
        # The project's own key under a second name is a copy-paste slip: this repo would get
        # the project repository's key, which cannot reach it.
        if value.strip() == secrets.deploy_key:
            raise ProfileError(f"{where}: [repo_keys] {key} must differ from [secrets] deploy_key")
    return MappingProxyType({key: value.strip() for key, value in table.items()})


def _repo_aliases(
    table: Mapping[str, Any], repos: Mapping[str, str], where: str
) -> Mapping[str, tuple[str, ...]]:
    current = {_git._repo_identity(url): key for key, url in repos.items()}
    seen: dict[str, str] = {}
    for key, urls in table.items():
        if key not in repos:
            raise ProfileError(f"{where}: [repo_aliases] {key} is not a key of [repo] ({', '.join(repos)})")
        if not isinstance(urls, list) or not all(isinstance(u, str) and u.strip() for u in urls):
            raise ProfileError(f"{where}: [repo_aliases] {key} must be a list of repository URLs")
        for url in urls:
            identity = _git._repo_identity(url)
            # An alias that is a current URL would silently send that repo's projects elsewhere.
            if identity in current:
                raise ProfileError(
                    f"{where}: [repo_aliases] {key}: {url!r} is the current [repo] {current[identity]} URL"
                )
            if seen.setdefault(identity, key) != key:
                raise ProfileError(
                    f"{where}: [repo_aliases] {url!r} is listed under both {seen[identity]} and {key}"
                )
    return MappingProxyType({key: tuple(u.strip() for u in urls) for key, urls in table.items()})


#: Warnings already printed: a process parses its profile several times, and one notice is enough.
_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        print(message, file=sys.stderr)


def parse(text: str, where: str = "profile", *, tolerate_unknown: bool = False) -> Profile:
    """Return the profile in `text`. Strict: an unknown key anywhere is an error.

    `tolerate_unknown` lets unknown top-level keys through with a warning, and nothing else: every
    table this code knows is still validated. It is for the deployer, which runs the previous release
    and must accept a key that the release it deploys introduces; that release's own code then reads
    the profile, strictly, and does the deploy (`_deploy_child`).

    The credential keys of earlier profiles (`_LEGACY_KEYS`) are accepted, ignored and warned about once
    each: the live profile carries them until its owner edits it, and the deployer that reads it is the
    previous release, which still requires them.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{where}: not valid TOML: {exc}") from exc
    if "self_agent" in data:
        # Retired. Pods deployed before the retirement still carry it in their profile text, and they may run
        # newer code (a self-improve project on an old slot, or a coordinator): ignore it, loudly, rather than
        # make every such import fail.
        print(
            f"{where}: [self_agent] was retired and is ignored: self-improve runs on an ordinary slot",
            file=sys.stderr,
        )
    unknown = sorted(
        set(data)
        - {"self_agent"}
        - {
            "name",
            "allow_placeholders",
            "repo",
            "repo_keys",
            "repo_aliases",
            "backend",
            "slots",
            "cap",
            "concurrency",
            "secrets",
            "policy",
            "mcp",
            "gcp",
            "browser",
            "deploy",
            "planner",
            "state",
            "github",
            "board",
            "direction",
            "bench",
            "loop",
            "gardening",
            "judgement",
            "standards",
            "standards_payload",
        }
    )
    if unknown and not tolerate_unknown:
        raise ProfileError(f"{where}: unknown top-level key(s): {', '.join(unknown)}")
    if unknown:
        _warn_once(f"{where}: unknown top-level key(s) not read by this code: {', '.join(unknown)}")

    try:
        standards = _standards.settings(dict(_table(data, "standards", where, required=False)))
        payload = data.get("standards_payload", "")
        if not isinstance(payload, str):
            raise ProfileError(f"{where}: [standards] payload must be a string")
        if payload:
            _standards.decode(payload)
    except ValueError as exc:
        raise ProfileError(f"{where}: {exc}") from exc

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ProfileError(f"{where}: missing key 'name'")

    allow_placeholders = data.get("allow_placeholders", False)
    if not isinstance(allow_placeholders, bool):
        raise ProfileError(f"{where}: allow_placeholders must be true or false")
    left = _placeholders(data)
    if left and not allow_placeholders:
        raise ProfileError(
            f"{where}: {PLACEHOLDER}... placeholder left in {', '.join(left)}: fill it in "
            "(a skeleton can set allow_placeholders = true)"
        )

    backend = _fields(
        _table(data, "backend", where),
        "backend",
        where,
        ("kind", "endpoint", "project", "domain"),
        ("org", "console_host", "signin_host", "config"),
    )
    if backend["kind"] not in BACKENDS:
        raise ProfileError(f"{where}: [backend] kind must be one of {BACKENDS}, not {backend['kind']!r}")

    repo_table = dict(_table(data, "repo", where))
    # The one integer in [repo], whose other keys are all URLs: take it out before they are read as such.
    factory_projects = repo_table.pop("factory_projects", 1)
    repo = _fields(repo_table, "repo", where, ("project",), ("factory",))
    if isinstance(factory_projects, bool) or not isinstance(factory_projects, int) or factory_projects < 1:
        raise ProfileError(f"{where}: [repo] factory_projects must be an integer of at least 1")
    if "factory_projects" in data["repo"] and not repo.get("factory"):
        raise ProfileError(f"{where}: [repo] factory_projects requires [repo] factory")

    secrets_table = dict(_table(data, "secrets", where))
    for key in ("claude_login", "codex_login"):
        if secrets_table.pop(key, None):
            _warn_legacy(f"[secrets] {key}", where)
    secrets = Secrets(**_fields(secrets_table, "secrets", where, ("deploy_key",), OPTIONAL_SECRETS))
    if not secrets.claude_token:
        raise ProfileError(f"{where}: {_NO_CLAUDE_TOKEN}")
    policy = _policy(_table(data, "policy", where, required=False), "policy", where)

    raw_slots = data.get("slots")
    concurrency = None
    if raw_slots is None:
        # Every project runs on the one token, so they share one environment (`SHARED_SLOT`) and
        # `[concurrency]` bounds them.
        if "cap" in data:
            raise ProfileError(
                f"{where}: cap needs [[slots]]: without them [concurrency] max_projects is the cap"
            )
        concurrency = _parse_concurrency(_table(data, "concurrency", where, required=False), where)
        slots: tuple[Slot, ...] = (SHARED_SLOT,)
        cap = concurrency.max_projects
    else:
        if "concurrency" in data:
            raise ProfileError(
                f"{where}: [concurrency] is for a profile without [[slots]]; "
                "[[slots]] bound the projects with `cap`"
            )
        if not isinstance(raw_slots, list) or not raw_slots:
            raise ProfileError(f"{where}: missing [[slots]]: at least one slot is required")
        slots = tuple(_slot(t, where) for t in raw_slots)
        for attr in ("env", "subagent_env"):
            values = [getattr(s, attr) for s in slots]
            if len(set(values)) != len(values):
                raise ProfileError(f"{where}: [[slots]] {attr} must be unique across slots, got {values}")
        # Across both name fields too: a sub-agent env named like another slot's env deploys over it.
        names = [x for s in slots for x in (s.env, s.subagent_env)]
        if len(set(names)) != len(names):
            raise ProfileError(f"{where}: [[slots]] env and subagent_env names must all differ, got {names}")
        # A login rotates on refresh: two slots refreshing one chain kill it for both.
        logins = [s.codex_login for s in slots if s.codex_login]
        if len(set(logins)) != len(logins) or secrets.codex_token in logins:
            raise ProfileError(
                f"{where}: [[slots]] codex_login must be a different secret on every slot, and not "
                f"[secrets] codex_token, got {logins}"
            )

        cap = data.get("cap", len(slots))
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= len(slots):
            raise ProfileError(
                f"{where}: cap must be an integer from 1 to the number of slots ({len(slots)})"
            )

    for s in slots:
        _check_pinned_engine({**policy, **s.policy}, s, secrets, where)

    repo_keys = _repo_keys(_table(data, "repo_keys", where, required=False), repo, secrets, where)

    repo_aliases = _repo_aliases(_table(data, "repo_aliases", where, required=False), repo, where)

    mcp = None
    if "mcp" in data:
        table = dict(_table(data, "mcp", where))
        repos = table.pop("repos", ["project"])
        if not isinstance(repos, list) or not all(isinstance(key, str) for key in repos):
            raise ProfileError(f"{where}: [mcp] repos must be a list of strings")
        if not repos:
            raise ProfileError(f"{where}: [mcp] repos must name at least one [repo] key")
        unknown_repos = [key for key in repos if key not in repo]
        if unknown_repos:
            raise ProfileError(
                f"{where}: [mcp] repos names {', '.join(unknown_repos)}, which are not keys of [repo] "
                f"({', '.join(repo)})"
            )
        mcp = Mcp(
            repos=tuple(repos),
            **_fields(
                table,
                "mcp",
                where,
                (),
                ("public_url",),
            ),
        )

    gcp = None
    if "gcp" in data:
        table = dict(_table(data, "gcp", where))
        lists = {}
        for key, shape in (("roles", _GCP_ROLE), ("apis", _GCP_API)):
            values = table.pop(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ProfileError(f"{where}: [gcp] {key} must be a list of strings")
            bad = [v for v in values if not shape.fullmatch(v)]
            if bad:
                raise ProfileError(f"{where}: [gcp] {key} has invalid entries: {', '.join(bad)}")
            lists[key] = tuple(values)
        # The loop above validates the two list fields; _fields only permits secret_name.
        gcp = Gcp(
            **cast(_GcpFields, _fields(table, "gcp", where, (), ("secret_name",))),
            **cast(_GcpLists, lists),
        )

    browser = _browser(data, where)

    deploy_table = dict(_table(data, "deploy", where, required=False))
    auto = deploy_table.pop("auto", False)
    if not isinstance(auto, bool):
        raise ProfileError(f"{where}: [deploy] auto must be true or false")
    if deploy_table:
        raise ProfileError(f"{where}: unknown key(s) in [deploy]: {', '.join(sorted(deploy_table))}")
    state = State(**_fields(_table(data, "state", where, required=False), "state", where, (), ("uri",)))
    if state.uri:
        scheme = _URI_SCHEME.fullmatch(state.uri)
        if not scheme:
            raise ProfileError(
                f"{where}: [state] uri must start with a scheme such as s3://, not {state.uri!r}"
            )
        if scheme[1] not in _STATE_SCHEMES:
            raise ProfileError(
                f"{where}: [state] uri scheme must be one of {', '.join(_STATE_SCHEMES)}, not {scheme[1]!r}"
            )
    github_table = dict(_table(data, "github", where, required=False))
    # The App id is a number on GitHub's settings page, so a TOML integer is what people write.
    if isinstance(github_table.get("app_id"), int) and not isinstance(github_table["app_id"], bool):
        github_table["app_id"] = str(github_table["app_id"])
    maintainers = github_table.pop("maintainers", [])
    if not isinstance(maintainers, list) or not all(
        isinstance(login, str)
        and "--" not in login
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", login)
        for login in maintainers
    ):
        raise ProfileError(f"{where}: [github] maintainers must be a list of GitHub logins")
    intake_label = github_table.pop("intake_label", "factory")
    if isinstance(intake_label, str):
        intake_label = intake_label.strip()
    if not isinstance(intake_label, str) or not intake_label or len(intake_label) > 50:
        raise ProfileError(
            f"{where}: [github] intake_label must be a non-empty label of at most 50 characters"
        )
    github = Github(
        maintainers=tuple(login.lower() for login in maintainers),
        intake_label=intake_label,
        **_fields(
            github_table,
            "github",
            where,
            (),
            ("app_id", "private_key_secret", "webhook_secret"),
        ),
    )
    if github.maintainers and not github.webhook_secret:
        # Not an error: a profile may list maintainers before the webhook secret exists.
        _warn_once(f"{where}: [github] maintainers has no effect without webhook_secret")
    # isdigit() alone accepts other scripts' digits, which GitHub would not.
    if github.app_id and not (github.app_id.isascii() and github.app_id.isdigit()):
        raise ProfileError(f"{where}: [github] app_id must be digits, not {github.app_id!r}")
    if auto:
        for section, key, value in (
            ("github", "app_id", github.app_id),
            ("github", "private_key_secret", github.private_key_secret),
        ):
            if not value:
                raise ProfileError(f"{where}: [deploy] auto = true requires [{section}] {key}")
        factory = repo.get("factory")
        if not factory:
            raise ProfileError(f"{where}: [deploy] auto = true requires [repo] factory")
        try:
            _github.parse_repo(factory)
        except _github.GithubError as exc:
            raise ProfileError(f"{where}: [repo] factory must be a github.com repository: {exc}") from exc

    board = None
    if "board" in data:
        table = dict(_table(data, "board", where))
        number = table.pop("number", None)
        fields = _fields(
            table, "board", where, ("owner", "owner_type", "pat_secret"), ("pat_expires", "node_id")
        )
        if fields["owner_type"] not in ("user", "org"):
            raise ProfileError(f"{where}: [board] owner_type must be user or org")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", fields["owner"]):
            raise ProfileError(f"{where}: [board] owner must be a GitHub login")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ProfileError(f"{where}: [board] number must be a positive integer")
        expires = fields.get("pat_expires", "")
        if expires:
            try:
                if date.fromisoformat(expires).isoformat() != expires:
                    raise ValueError("noncanonical date")
            except ValueError as exc:
                raise ProfileError(f"{where}: [board] pat_expires must be YYYY-MM-DD") from exc
        board = Board(number=number, **fields)

    direction = None
    if "direction" in data:
        fields = _fields(_table(data, "direction", where), "direction", where, ("repo",), ("profile", "ref"))
        try:
            _github.parse_repo(fields["repo"])
        except _github.GithubError as exc:
            raise ProfileError(f"{where}: [direction] repo must be a github.com repository: {exc}") from exc
        # Relative and inside the checkout: the path is joined onto a fetched tree.
        path = PurePosixPath(fields.get("profile") or f"profiles/{name}.toml")
        if path.is_absolute() or ".." in path.parts:
            raise ProfileError(f"{where}: [direction] profile must be a path inside the repository: {path}")
        if not auto:
            # Only the deployer reads it, through the App.
            raise ProfileError(f"{where}: [direction] requires [deploy] auto = true")
        direction = Direction(fields["repo"], str(path), fields.get("ref", ""))

    bench = (
        _bench(data, where, repo, secrets, repo_keys, repo_aliases, github, board, gcp)
        if "bench" in data
        else None
    )

    loop = Loop()
    if "loop" in data:
        table = dict(_table(data, "loop", where))
        enabled = table.pop("enabled", False)
        if not isinstance(enabled, bool):
            raise ProfileError(f"{where}: [loop] enabled must be a boolean")
        limits = {}
        for key, default in (("daily_usd", 0), ("weekly_usd", 0), ("owner_floor_pct", 30)):
            value = table.pop(key, default)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (key == "owner_floor_pct" and value > 100)
            ):
                raise ProfileError(
                    f"{where}: [loop] {key} must be a finite non-negative number"
                    + (" at most 100" if key == "owner_floor_pct" else "")
                )
            limits[key] = value
        fields = _fields(table, "loop", where, ("repo",), ("merge",))
        loop_repo = fields["repo"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", loop_repo):
            raise ProfileError(f"{where}: [loop] repo must be owner/repo")
        mode = fields.get("merge", "dry-run")
        if mode not in ("dry-run", "on"):
            raise ProfileError(f"{where}: [loop] merge must be dry-run or on")
        if enabled and not github.configured:
            raise ProfileError(f"{where}: [loop] requires [github] app_id and private_key_secret")
        if enabled and (not repo.get("factory") or _github.parse_repo(repo["factory"]) != loop_repo):
            raise ProfileError(f"{where}: [loop] repo must be [repo] factory")
        if enabled and bench is None:
            raise ProfileError(f"{where}: [loop] requires [bench]")
        loop = Loop(enabled=enabled, repo=loop_repo, merge=mode, **limits)

    gardening = _gardening_table(_table(data, "gardening", where, required=False), where)
    judgement = _judgement_table(_table(data, "judgement", where, required=False), where)

    planner_fields = _fields(
        _table(data, "planner", where, required=False), "planner", where, (), ("admission",)
    )
    admission = planner_fields.get("admission", "legacy")
    if admission not in ("legacy", "planner"):
        raise ProfileError(f"{where}: [planner] admission must be legacy or planner")
    if concurrency and admission == "planner":
        raise ProfileError(
            f"{where}: [planner] admission = planner needs [[slots]]: it places launches on slots"
        )

    return Profile(
        name=name,
        text=re.sub(r'^standards_payload = "[^"\n]*"\n', "", text, count=1),
        backend=Backend(**backend),
        repo=repo["project"],
        repos=repo,
        repo_keys=repo_keys,
        factory_projects=factory_projects,
        slots=slots,
        cap=cap,
        secrets=secrets,
        policy=policy,
        concurrency=concurrency,
        mcp=mcp,
        gcp=gcp,
        browser=browser,
        deploy=Deploy(auto=auto),
        planner=Planner(admission=admission),
        state=state,
        github=github,
        board=board,
        direction=direction,
        bench=bench,
        loop=loop,
        gardening=gardening,
        judgement=judgement,
        repo_aliases=repo_aliases,
        standards=standards,
        standards_payload=payload,
        source_dir=Path(where).resolve().parent if where != TOML_ENV else Path("."),
    )


def _gardening_table(table: Mapping[str, Any], where: str) -> Gardening:
    unknown = sorted(set(table) - set(Gardening.__dataclass_fields__))
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) in [gardening]: {', '.join(unknown)}")
    values = dict(table)
    for key, minimum in (("min_runs_per_week", 0), ("max_days_between", 1), ("max_open_issues", 0)):
        value = values.get(key, getattr(Gardening, key))
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ProfileError(f"{where}: [gardening] {key} must be an integer of at least {minimum}")
    if not isinstance(values.get("escalate_on_regression", True), bool):
        raise ProfileError(f"{where}: [gardening] escalate_on_regression must be a boolean")
    return Gardening(**values)


def _judgement_table(table: Mapping[str, Any], where: str) -> JudgementPolicy:
    unknown = sorted(set(table) - set(JudgementPolicy.__dataclass_fields__))
    if unknown:
        raise ProfileError(f"{where}: unknown key(s) in [judgement]: {', '.join(unknown)}")
    lists: dict[str, tuple[str, ...]] = {}
    for key, value in table.items():
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ProfileError(f"{where}: [judgement] {key} must be a list of non-empty strings")
        lists[key] = tuple(v.strip() for v in value)
    return JudgementPolicy(**lists)


def _bench(
    data: Mapping[str, Any],
    where: str,
    repo: Mapping[str, str],
    secrets: Secrets,
    repo_keys: Mapping[str, str],
    repo_aliases: Mapping[str, tuple[str, ...]],
    github: Github,
    board: Board | None,
    gcp: Gcp | None = None,
) -> Bench:
    table = dict(_table(data, "bench", where))
    if table.pop("logins", None) is not None:
        _warn_legacy("[bench] logins", where)
    max_usd = table.pop("max_usd", 0)
    if (
        isinstance(max_usd, bool)
        or not isinstance(max_usd, (int, float))
        or not math.isfinite(max_usd)
        or max_usd < 0
    ):
        raise ProfileError(f"{where}: [bench] max_usd must be a number of dollars, 0 or more")
    token_concurrency = table.pop("token_concurrency", 1)
    if isinstance(token_concurrency, bool) or not isinstance(token_concurrency, int) or token_concurrency < 1:
        raise ProfileError(f"{where}: [bench] token_concurrency must be a positive integer")
    fields = _fields(table, "bench", where, ("repo", "repo_key"), ("claude_token",))
    # No check that a bench token differs from production's (#84): one work factory, one work token. What
    # bounds the bench's usage of the shared account is the account reserve, not a second credential.
    bench_repo, repo_key = fields["repo"], fields["repo_key"]
    # The bench repo's key is mounted into bench pods only, so it must not be a secret production uses.
    production_secrets = {
        *(getattr(secrets, f.name) for f in dataclasses.fields(secrets)),
        github.private_key_secret,
        github.webhook_secret,
        board.pat_secret if board else "",
        gcp.secret_name if gcp else "",  # gcp_bootstrap creates or replaces it
        *repo_keys.values(),
    } - {""}
    if repo_key in production_secrets:
        raise ProfileError(f"{where}: [bench] repo_key must not be a secret the production profile uses")
    bench = Bench(
        repo=bench_repo,
        repo_key=repo_key,
        max_usd=float(max_usd),
        claude_token=fields.get("claude_token") or secrets.claude_token,
        token_concurrency=token_concurrency,
    )
    bench_identity = _git._repo_identity(bench_repo)
    for name, production_repo in repo.items():
        if bench_identity == _git._repo_identity(production_repo):
            raise ProfileError(f"{where}: [bench] repo must not be [repo] {name}")
    for name, urls in repo_aliases.items():
        if bench_identity in {_git._repo_identity(url) for url in urls}:
            raise ProfileError(f"{where}: [bench] repo must not be a [repo_aliases] {name} URL")
    try:
        _github.parse_repo(bench_repo)
    except _github.GithubError as exc:
        raise ProfileError(f"{where}: [bench] repo must be a github.com repository: {exc}") from exc
    # The engine is fetched at a SHA, and each case's branch read back, through the App.
    if not github.configured:
        raise ProfileError(f"{where}: [bench] requires [github] app_id and private_key_secret")
    if not repo.get("factory"):
        raise ProfileError(f"{where}: [bench] requires [repo] factory, the engine it deploys at a SHA")
    return bench


def load(spec: str, *, tolerate_unknown: bool = False) -> Profile:
    """`spec` is a profile name (`profiles/<name>.toml`) or a path to a TOML file.

    Anything without a path separator is a name, with or without `.toml`; use `./x.toml` for a
    file in the current directory.
    """
    path = Path(spec).expanduser()
    if os.sep not in spec:
        path = PROFILES_DIR / (spec if spec.endswith(".toml") else f"{spec}.toml")
    try:
        text = path.read_text()
    except OSError as exc:
        raise ProfileError(f"profile {spec!r}: cannot read {path}: {exc.strerror}") from exc
    profile = parse(text, str(path), tolerate_unknown=tolerate_unknown)
    absolute = path.resolve()
    if profile.direction:
        suffix = PurePosixPath(profile.direction.profile).parts
        # A declared repository path identifies the checkout without probing parents for markers.
        if absolute.parts[-len(suffix) :] == suffix:
            profile = replace(profile, source_root=absolute.parents[len(suffix) - 1])
    elif absolute.parent.name == "profiles" and absolute.suffix == ".toml":
        # Conventional engine profiles identify their root without searching for checkout markers.
        profile = replace(profile, source_root=absolute.parent.parent)
    return profile


def _load_shipped(
    path: Path, tree: Path, *, tolerate_unknown: bool = False, workspace: bool = False
) -> Profile:
    """Read `path`, resolving relative paths beside it first, then against `tree`.

    The standards are captured now so a bad bundle fails here. `workspace`: `tree` is the profile's own
    repository (a direction repo), whose `standards/` applies when `[standards] dir` is unset.
    """
    return _with_tree(
        load(str(path), tolerate_unknown=tolerate_unknown), str(path), tree, workspace=workspace
    )


def shipped(text: str, where: str, tree: Path) -> Profile:
    """`text` read as `_load_shipped` reads a file: what a fetched engine tree deploys a profile as.

    The auto-deployer hands the engine's own code the text (`_deploy_child`), not a file: the
    direction checkout it came from is gone by then. An uncaptured engine profile lives at
    `tree/profiles/<name>.toml`: relative paths resolve beside that file first, then against
    `tree` for repository-relative compatibility. `where` is only a diagnostic label.
    """
    return _with_tree(replace(parse(text, where), source_dir=tree.resolve() / "profiles"), where, tree)


def _with_tree(profile: Profile, where: str, tree: Path, *, workspace: bool = False) -> Profile:
    tree = tree.resolve()
    profile = replace(profile, source_root=tree)
    # A captured bundle belongs to the original checkout, which may already have been deleted.
    if profile.standards_payload:
        return profile
    standards = profile.standards
    # A workspace's own `standards/` is its default; an engine checkout's is never one (docs/standards.md):
    # there only an explicit `[standards] dir` names standards.
    if workspace and not standards.dir and (tree / "standards").is_dir():
        standards = replace(standards, dir=str(tree / "standards"))
    try:
        captured = _standards.capture(standards, profile.source_dir, root=tree)
        payload = _standards.encode(captured)
    except ValueError as exc:
        raise ProfileError(f"{where}: {exc}") from exc
    if not captured:
        print(
            f"{where}: {_standards.NO_STANDARDS}: [standards] dir is unset"
            + (f" and {tree} has no standards/" if workspace else "")
            + ", "
            "so projects deployed from this profile have no rules (docs/standards.md)",
            file=sys.stderr,
        )
    return replace(profile, standards=standards, standards_payload=payload)


def load_from_tree(tree: Path, name: str, *, tolerate_unknown: bool = False) -> Profile | None:
    """`profiles/<name>.toml` of a checkout of the factory repo, or None when the tree has none.

    Relative paths resolve beside the profile first, then against the tree for repository-relative
    compatibility. The installed copy never supplies standards.
    """
    path = tree / "profiles" / f"{name}.toml"
    return _load_shipped(path, tree, tolerate_unknown=tolerate_unknown) if path.is_file() else None


def load_from_direction(direction: Path, path: str, *, tolerate_unknown: bool = False) -> Profile:
    """`path` of a checkout of the direction repo. Unlike `load_from_tree`, a missing file is an error.

    The standards are the direction repository's: a relative `[standards] dir` or `brand` resolves beside
    it, and with `dir` unset its `standards/` is used. The engine's own standards are never read.
    """
    file = direction / path
    if not file.is_file():
        raise ProfileError(f"{path} is not in the direction repository")
    return _load_shipped(file, direction, tolerate_unknown=tolerate_unknown, workspace=True)


def current() -> Profile | None:
    """Return the selected profile, or None when nothing selects one."""
    spec = _names.getenv(PROFILE_ENV, "").strip()
    if spec:
        return load(spec)
    text = _names.getenv(TOML_ENV, "")
    if text.strip():
        return parse(text, TOML_ENV)
    default = PROFILES_DIR / DEFAULT_FILE
    if default.is_file():
        try:
            return load(default.read_text().strip())
        except ProfileError as exc:
            raise ProfileError(f"{exc} (the profile named by {default})") from exc
    return None


def planner_ready(profile: Profile) -> None:
    if profile.planner.admission == "planner" and not (
        profile.direction
        and profile.board
        and profile.github.configured
        and profile.github.webhook_secret
        and profile.state.uri
    ):
        raise ProfileError(
            "planner cutover needs [direction], [board], GitHub App, webhook and [state]; "
            "one replica and stop-before-start replacement remain deployment safeguards"
        )


def require() -> Profile:
    profile = current()
    if profile is None:
        available = ", ".join(sorted(p.stem for p in PROFILES_DIR.glob("*.toml"))) or "none yet"
        raise ProfileError(
            f"no deployment profile selected: set {PROFILE_ENV}=<name> or pass --profile <name>, "
            f"or make one the local default with `echo <name> > {PROFILES_DIR / DEFAULT_FILE}` "
            f"(profiles in {PROFILES_DIR}: {available}; see profiles/README.md)"
        )
    planner_ready(profile)
    return profile


def child_env(profile: Profile, env: dict[str, str]) -> dict[str, str]:
    """`env` for a deploy subprocess that imports code from a throwaway worktree.

    The worktree has no `profiles/` (real profiles are untracked), so the child gets the text.
    """
    out = {k: v for k, v in env.items() if k != PROFILE_ENV}
    out[TOML_ENV] = profile.deployment_text()
    return out


def pod_env() -> dict[str, str]:
    """For a deployed environment's `env_vars`: the profile, as the pod will read it."""
    profile = current()
    return {TOML_ENV: profile.deployment_text()} if profile else {}


def select_from_argv(argv: list[str] | None = None) -> None:
    """Honour `--profile NAME` before modules that read the profile at import are imported."""
    args = sys.argv[1:] if argv is None else argv
    for i, arg in enumerate(args):
        if arg == "--profile" and i + 1 < len(args):
            os.environ[PROFILE_ENV] = args[i + 1]
        elif arg.startswith("--profile="):
            os.environ[PROFILE_ENV] = arg.split("=", 1)[1]


def _get(profile: Profile, key: str) -> str:
    values = {
        "name": profile.name,
        "project": profile.backend.project,
        "domain": profile.backend.domain,
        "config": str(Path(profile.backend.config).expanduser()) if profile.backend.config else "",
        "repo": profile.repo,
        "agent_secrets": profile.agent_secrets,
    }
    if key not in values:
        raise ProfileError(f"unknown key {key!r}; one of {', '.join(values)}")
    return values[key]


if __name__ == "__main__":
    # For direct shell commands: `python -m flyte_agent_factory._profile project` prints one value.
    try:
        print(_get(require(), sys.argv[1]))
    except (ProfileError, IndexError) as exc:
        sys.exit(f"profile: {exc or 'usage: python -m flyte_agent_factory._profile KEY'}")
