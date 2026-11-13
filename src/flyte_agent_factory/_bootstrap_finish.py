"""The bootstrap steps for the GitHub App, the profile, the deploy, the factory URL and the chat connector."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from . import _bootstrap_profile as profile_file
from . import _profile, _workspace
from ._bootstrap_base import (
    API_KEY_REQUIREMENT,
    APP_LOST,
    BLOCKED,
    DONE,
    PROFILE_FILE,
    SECRET_APP_KEY,
    TODO,
    BootstrapError,
    Check,
    Context,
    NotAllowed,
    Outcome,
    build_app_name,
)
from ._bootstrap_keys import build_command, build_delete_secret_command, find_connector_url
from ._bootstrap_steps import commit_paths, describe_unclean_or_unpushed

#: Polls of the App installation, `_POLL_SECONDS` apart: ten minutes to find the install button and click it.
_POLLS, _POLL_SECONDS = 120, 5
_GAP = (
    "open-source Flyte stops here: the deploy needs Union-only parts (API keys, the remote image builder and "
    "MCP authentication); see docs/oss-flyte-compat.md. Everything above is done and stays valid"
)
# The one next action when the install cannot happen (an organization the person does not own, say) is the
# second sentence: the App is not written to the profile, so `--no-app` on a rerun leaves nothing to undo.
_NOT_INSTALLED = (
    "install the App on {workspace}: {url} , then run this again; if you cannot install it, run this "
    "again with --no-app (set up without it)"
)
# Local git config of the clone: what GitHub and Flyte will not tell the bootstrap on a later run (an App is
# only visible to its own JWT, and neither its key nor a deploy's contents can be read back from Flyte).
# Never a secret. These are *receipts*, not live state: the labels below say so wherever they are used.
_INSTALLED_KEY, _HOW_KEY, _SLUG_KEY = (
    "agent-factory.app-installed",
    "agent-factory.app-installed-how",
    "agent-factory.app-slug",
)
#: The App id between registering the App (its key is stored) and the person installing it. The profile is
#: written only once the installation is confirmed, so an App that cannot be installed leaves no trace in it.
_PENDING_KEY = "agent-factory.app-pending"
#: Digests of the profile a deploy finished with: the work slots and factory (without `[mcp]`, which only the
#: factory reads), and the factory alone (the whole file).
_WORK_RECEIPT, _FACTORY_RECEIPT = "agent-factory.deployed-work", "agent-factory.deployed-factory"


def _read_config(ctx: Context, key: str) -> str:
    try:
        return _workspace.git(
            ctx.settings.clone, "config", "--local", "--get", key, placeholder_identity=False
        ).strip()
    except (RuntimeError, OSError):  # no such key, or no clone yet
        return ""


def _remember(ctx: Context, key: str, value: str) -> None:
    _workspace.git(ctx.settings.clone, "config", "--local", key, value, placeholder_identity=False)


def _forget(ctx: Context, key: str) -> None:
    try:
        _workspace.git(
            ctx.settings.clone, "config", "--local", "--unset-all", key, placeholder_identity=False
        )
    except RuntimeError:  # git exits 5 when the key is not set
        pass


def _build_app_settings_url(ctx: Context, slug: str) -> str:
    """Return the page of the App's settings with its Delete button."""
    owner = ctx.settings.owner
    base = (
        f"https://github.com/organizations/{owner}/settings/apps"
        if ctx.github.owner_is_org(owner)
        else "https://github.com/settings/apps"
    )
    return f"{base}/{slug}/advanced"


def compute_profile_digest(path: Path, *, with_mcp: bool) -> str:
    """Return a digest of what a deploy of `path` would ship.

    It hashes the parsed content, so comments and layout do not count, and so a new App or slot does.
    """
    data = tomllib.loads(path.read_text())
    if not with_mcp:
        data.pop("mcp", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def build_install_url(slug: str) -> str:
    return f"https://github.com/apps/{slug}/installations/new"


def _commit_and_push_profile(ctx: Context, message: str) -> None:
    """Commit just `profile.toml` as the person (when it changed) and push."""
    clone = ctx.settings.clone
    if _workspace.git(clone, "status", "--porcelain", "--", PROFILE_FILE, placeholder_identity=False).strip():
        commit_paths(clone, (PROFILE_FILE,), message)
    ctx.github.push(clone)


class GithubAppStep:
    id = "github-app"
    informational = False
    advisory = False

    def _find_app_id(self, ctx: Context) -> str | None:
        """Return the App this workspace has.

        That is the profile's (written only after the install was confirmed, or by hand), else the one
        registered and not yet installed.
        """
        return profile_file.read_app_id(ctx.profile_path) or _read_config(ctx, _PENDING_KEY) or None

    def _find_slug(self, ctx: Context) -> str:
        return _read_config(ctx, _SLUG_KEY) or build_app_name(ctx.settings.owner).lower()

    def _describe_installation(self, ctx: Context, app_id: str) -> str | None:
        """Return how the installation is known, or None when it is not.

        It is asked live where GitHub answers (an organization lists its installations), else it is the
        receipt of an earlier run.

        A user account's installation is visible only to the App's own key, which lives in Flyte and cannot
        be read here, so the receipt (checked live when the App was registered, or confirmed by the person)
        is all there is and is labelled as such. An uninstall after that is invisible from this machine.
        """
        try:
            if ctx.github.owner_is_org(ctx.settings.owner):
                live = self._find_slug(ctx) in ctx.github.app_installations(ctx.settings.owner)
                return "checked just now" if live else None
        except NotAllowed:  # an org member who may not list installations: fall back to the receipt
            pass
        if _read_config(ctx, _INSTALLED_KEY) != app_id:
            return None
        how = _read_config(ctx, _HOW_KEY)
        return (
            "checked live when it was registered, not re-checked since"
            if how == "live"
            else "you confirmed it, not re-checked since"
        )

    def check(self, ctx: Context) -> Check:
        if ctx.settings.no_app:
            no_board = "; and no board (--no-board)" if ctx.settings.no_board else ""
            return Check(True, f"skipped (--no-app); {APP_LOST}{no_board}")
        app_id = self._find_app_id(ctx)
        if not app_id:
            return Check(False, "no App registered")
        if not ctx.flyte.secret_exists(SECRET_APP_KEY):
            return Check(False, f"secret {SECRET_APP_KEY} missing")
        how = self._describe_installation(ctx, app_id)
        if how is None:
            return Check(
                False,
                f"App {app_id} is not installed on the workspace: {build_install_url(self._find_slug(ctx))}",
            )
        if lacking := profile_file.find_missing_keys(ctx.profile_path, ctx.settings, app_id):
            return Check(False, f"App {app_id} is installed ({how}); profile.toml lacks {', '.join(lacking)}")
        if left := describe_unclean_or_unpushed(ctx.settings.clone, (PROFILE_FILE,)):
            return Check(False, f"App {app_id} is installed ({how}); {PROFILE_FILE} {left}")
        return Check(True, f"App {app_id} installed ({how})")

    def plan(self, ctx: Context) -> str:
        return (
            "register your GitHub App in the browser, keep its key as a Flyte secret "
            "and wait for you to install it"
        )

    def do(self, ctx: Context) -> Outcome:
        app_id = self._find_app_id(ctx)
        if app_id:
            return self._resume(ctx, app_id)
        if ctx.flyte.secret_exists(SECRET_APP_KEY):
            return Outcome(
                BLOCKED,
                f"secret {SECRET_APP_KEY} exists but no App is recorded (an earlier run stopped half-way); "
                f"delete the secret with: "
                f"{build_delete_secret_command(ctx.settings, SECRET_APP_KEY)} and the App at "
                f"{_build_app_settings_url(ctx, self._find_slug(ctx))} , then run this again",
            )
        slug = build_app_name(ctx.settings.owner).lower()
        if ctx.github.app_exists(ctx.settings.owner, slug):
            # GitHub makes the App when the person submits the form and hands its key over afterwards, once:
            # a run that died in between left an App nobody holds a key for, and its name is taken.
            return Outcome(
                BLOCKED,
                f"an App {slug!r} already exists but this workspace has no record of it (an earlier run "
                f"stopped while registering it, so its key is lost); delete it at "
                f"{_build_app_settings_url(ctx, slug)} , then run this again (an App of yours from "
                "elsewhere: docs/github-app.md, Register it by hand)",
            )
        return self._register(ctx)

    def _register(self, ctx: Context) -> Outcome:
        code = ctx.github.register_app(ctx.settings.owner, ctx.settings.repo_name, ctx.say)
        registration = ctx.github.exchange_manifest_code(code)
        # Recorded before the key is stored: a run that stops from here on finds this App, by GitHub's own
        # slug (which a custom or clashing name changes), rather than registering a second one.
        _remember(ctx, _SLUG_KEY, registration.slug)
        _remember(ctx, _PENDING_KEY, registration.app_id)
        try:
            ctx.flyte.create_secret(SECRET_APP_KEY, registration.pem)
        except BootstrapError as exc:
            raise BootstrapError(
                f"{exc}; the App {registration.slug!r} is registered but its key was lost: delete it at "
                f"{_build_app_settings_url(ctx, registration.slug)} and run this again"
            ) from None
        url = build_install_url(registration.slug)
        ctx.say(f"Install the App on {ctx.settings.workspace} only: {url}")
        for _ in range(_POLLS):
            if ctx.github.app_installed(registration, ctx.settings.workspace):
                _remember(ctx, _INSTALLED_KEY, registration.app_id)
                _remember(ctx, _HOW_KEY, "live")
                return self._adopt(ctx, registration.app_id, "registered and installed")
            ctx.sleep(_POLL_SECONDS)
        return Outcome(TODO, _NOT_INSTALLED.format(workspace=ctx.settings.workspace, url=url))

    def _handle_lost_key(self, ctx: Context, app_id: str) -> Outcome:
        """Handle an App that was registered and recorded, but whose key never reached Flyte.

        The run stopped before the key was stored. GitHub shows a key only once, so the App is no use: the
        person deletes it, and the run after that starts over.
        """
        slug = self._find_slug(ctx)
        if ctx.github.app_exists(ctx.settings.owner, slug):
            return Outcome(
                BLOCKED,
                f"App {app_id} ({slug}) was registered but an earlier run stopped before storing its key in "
                f"secret {SECRET_APP_KEY}, and GitHub shows the key only once; delete the App at "
                f"{_build_app_settings_url(ctx, slug)} , then run this again",
            )
        for key in (_PENDING_KEY, _SLUG_KEY, _INSTALLED_KEY, _HOW_KEY):
            _forget(ctx, key)
        return self._register(ctx)

    def _adopt(self, ctx: Context, app_id: str, what: str) -> Outcome:
        """Record a confirmed installation: only now does the profile name the App and turn on deploys."""
        profile_file.write(ctx.profile_path, ctx.settings, app_id)
        _commit_and_push_profile(ctx, "Add the GitHub App to profile.toml")
        return Outcome(DONE, f"App {app_id} {what}; profile.toml names it")

    def _resume(self, ctx: Context, app_id: str) -> Outcome:
        """Finish an App that exists; only its installation (or its entry in the profile) is unfinished.

        Its key is gone from memory, so a user account's installation can only be confirmed by asking.
        """
        url = build_install_url(self._find_slug(ctx))
        if not ctx.flyte.secret_exists(SECRET_APP_KEY):
            if profile_file.read_app_id(ctx.profile_path):  # an App in use whose secret went missing later
                return Outcome(
                    BLOCKED,
                    f"App {app_id} was registered but secret {SECRET_APP_KEY} is missing; "
                    "see docs/github-app.md",
                )
            return self._handle_lost_key(ctx, app_id)
        if self._describe_installation(
            ctx, app_id
        ):  # confirmed on an earlier run, which stopped before the profile
            return self._adopt(ctx, app_id, "installation already confirmed")
        ctx.say(f"Install the App on {ctx.settings.workspace} only: {url}")
        try:
            ctx.ask_line("Press Enter once it is installed: ")
        except (EOFError, OSError):
            return Outcome(TODO, _NOT_INSTALLED.format(workspace=ctx.settings.workspace, url=url))
        _remember(ctx, _INSTALLED_KEY, app_id)
        _remember(ctx, _HOW_KEY, "attested")
        return self._adopt(ctx, app_id, "installation confirmed by you")


class ProfileStep:
    id = "profile"
    informational = False
    advisory = False

    def _validate(self, ctx: Context) -> str | None:
        try:
            _profile.load(str(ctx.profile_path))
        except _profile.ProfileError as exc:
            # `exc` starts with the file's path, which is the one the person has to edit.
            return f"profile.toml does not validate: {exc}; edit it, commit and push, then run this again"
        return None

    def _find_conflict(self, ctx: Context) -> str | None:
        """Return the message when the command line disagrees with the committed profile.

        `profile.toml` is the person's file, and the secrets, keys and deploy of the steps after this one
        are scoped by the command line's project and domain: carrying on would act on another project.
        """
        try:
            conflicts = profile_file.find_conflicts(ctx.profile_path, ctx.settings)
        except (OSError, tomllib.TOMLDecodeError):  # `_validate` reports a file that does not parse
            return None
        if not conflicts:
            return None
        has = ", ".join(f"{name.split('.')[1]} {have}" for name, (have, _) in conflicts.items())
        gave = ", ".join(given for _, given in conflicts.values())
        theirs = profile_file.read_settings(ctx.profile_path, ctx.settings)
        flag = f"--backend {theirs.backend}" if "backend.kind" in conflicts else ""
        return (
            f"profile.toml has {has}; you gave {gave}. Run it with the profile's values: "
            f"{build_command(theirs, flag)} , or edit profile.toml, commit and push, then run this again"
        )

    def check(self, ctx: Context) -> Check:
        path = ctx.profile_path
        if not path.is_file():
            return Check(False, f"no {PROFILE_FILE} in {ctx.settings.clone}")
        if problem := self._validate(ctx):
            return Check(False, problem)
        if conflict := self._find_conflict(ctx):
            return Check(False, conflict)
        if ctx.settings.no_app and (app_keys := profile_file.find_app_keys(path)):
            return Check(False, f"{', '.join(app_keys)} are set but you chose --no-app")
        if lacking := profile_file.find_missing_keys(path, ctx.settings, self._find_app_id(ctx)):
            return Check(False, f"missing keys: {', '.join(lacking)}")
        if left := describe_unclean_or_unpushed(ctx.settings.clone, (PROFILE_FILE,)):
            return Check(False, f"{PROFILE_FILE} {left}")
        return Check(True, f"{path} valid, committed and pushed")

    def _find_app_id(self, ctx: Context) -> str | None:
        """Return the App whose keys belong in the file: only one the person is using.

        `GithubAppStep` adds them once the installation is confirmed, so this step never does for a new App.
        """
        return None if ctx.settings.no_app else profile_file.read_app_id(ctx.profile_path)

    def plan(self, ctx: Context) -> str:
        return f"write {ctx.profile_path} (never overwriting), commit and push it"

    def do(self, ctx: Context) -> Outcome:
        # `--no-app` is the person's explicit choice, so App keys left by an earlier run are taken out: a
        # profile naming an App that will not be installed makes the factory fail to open pull requests.
        # A file that does not validate is left to the check after `write` below, which names it.
        if ctx.profile_path.is_file() and not self._validate(ctx) and (conflict := self._find_conflict(ctx)):
            return Outcome(BLOCKED, conflict)  # before `write`, which would fill and go on
        removed = []
        if ctx.settings.no_app and ctx.profile_path.is_file():
            removed = profile_file.drop_app_keys(ctx.profile_path)
        result = profile_file.write(ctx.profile_path, ctx.settings, self._find_app_id(ctx))
        if problem := self._validate(ctx):
            return Outcome(BLOCKED, problem)
        _commit_and_push_profile(ctx, "Update profile.toml")
        note = f"; removed {', '.join(removed)} because of --no-app" if removed else ""
        return Outcome(DONE, f"{PROFILE_FILE} {result}, committed and pushed{note}")


class DeployStep:
    id = "deploy"
    informational = False
    advisory = False

    def check(self, ctx: Context) -> Check:
        if ctx.settings.backend == "oss":
            return Check(False, _GAP)
        if ctx.settings.redeploy:
            return Check(False, "redeploy requested (--redeploy)")
        if not ctx.flyte.factory_registered():
            return Check(False, "factory.launch is not registered")
        # Registered is not current: an earlier run may have stopped between deploys, or the profile may have
        # gained an App or slot since. The receipt is the digest of the profile the last finished deploy used.
        if _read_config(ctx, _WORK_RECEIPT) != compute_profile_digest(ctx.profile_path, with_mcp=False):
            return Check(False, "factory tasks are registered but not deployed from the current profile")
        return Check(True, "factory tasks are registered and match the profile (deploy receipt)")

    def plan(self, ctx: Context) -> str:
        return (
            _GAP
            if ctx.settings.backend == "oss"
            else "deploy the work slots and the factory with your profile"
        )

    def do(self, ctx: Context) -> Outcome:
        if ctx.settings.backend == "oss":
            return Outcome(BLOCKED, _GAP)
        if not ctx.deployer.api_key_available():
            return Outcome(
                BLOCKED,
                "the deploy needs a Union API key and this machine cannot make one: "
                f"{API_KEY_REQUIREMENT}. Run this on a Linux x86_64 machine, where it makes the key and "
                f"shows it once on that terminal: {build_command(ctx.settings, '--deploy-key')} . Store the "
                "key in a password manager and export _UNION_EAGER_API_KEY with it "
                "(docs/quickstart.md, `Local Union deployment bootstrap`), then run this again",
            )
        url = ctx.deployer.deploy(ctx.profile_path)
        if url:
            ctx.facts["mcp_url"] = url
        # Only a deploy that returned: an exception above leaves the old receipt, so the rerun deploys again.
        _remember(ctx, _WORK_RECEIPT, compute_profile_digest(ctx.profile_path, with_mcp=False))
        _remember(ctx, _FACTORY_RECEIPT, compute_profile_digest(ctx.profile_path, with_mcp=True))
        return Outcome(DONE, "work slots and factory deployed")


class McpUrlStep:
    id = "mcp-url"
    informational = False
    advisory = False

    def check(self, ctx: Context) -> Check:
        if ctx.settings.backend == "oss":
            return Check(False, _GAP)
        if not profile_file.is_mcp_url_set(ctx.profile_path):
            return Check(False, "[mcp] public_url is empty")
        # The URL in the file is not proof the factory runs with it: the redeploy after setting it may have
        # failed. Only the receipt of a finished deploy of this very content is.
        if _read_config(ctx, _FACTORY_RECEIPT) != compute_profile_digest(ctx.profile_path, with_mcp=True):
            return Check(False, "[mcp] public_url is set but the factory was not deployed with it")
        return Check(True, "[mcp] public_url is set and the factory was deployed with it (deploy receipt)")

    def plan(self, ctx: Context) -> str:
        return "put the factory-mcp URL into profile.toml, commit, push and redeploy the factory"

    def do(self, ctx: Context) -> Outcome:
        if ctx.settings.backend == "oss":
            return Outcome(BLOCKED, _GAP)
        url = ctx.facts.get("mcp_url") or ctx.flyte.factory_mcp_url()
        if not url:
            return Outcome(TODO, "factory-mcp has no public URL yet; wait a minute and run this again")
        profile_file.set_mcp_url(ctx.profile_path, url)
        # A rerun after a failed deploy finds the URL already committed and pushed.
        if describe_unclean_or_unpushed(ctx.settings.clone, (PROFILE_FILE,)):
            commit_paths(ctx.settings.clone, (PROFILE_FILE,), "Set the factory-mcp public URL")
            ctx.github.push(ctx.settings.clone)
        ctx.deployer.deploy(ctx.profile_path, work=False)
        _remember(ctx, _FACTORY_RECEIPT, compute_profile_digest(ctx.profile_path, with_mcp=True))
        return Outcome(DONE, f"[mcp] public_url = {url}; factory redeployed")


class ConnectStep:
    """The one step no check can see (the chat client is off this machine): it prints what to do every run."""

    id = "connect"
    informational = True
    advisory = False

    def check(self, ctx: Context) -> Check:
        return Check(True, "cannot be checked from here (it happens in your chat client)")

    def plan(self, ctx: Context) -> str:
        return "print the connector URL and the steps for claude.ai and Claude Code"

    def do(self, ctx: Context) -> Outcome:
        connector = find_connector_url(ctx)
        return Outcome(
            DONE,
            f"connector URL {connector}\n"
            "  claude.ai: Settings > Connectors > Add custom connector, paste that URL, and authenticate as "
            "docs/factory-mcp-setup.md says; then start a chat and run /agent-factory\n"
            "  Key for either client, one command on a Linux x86_64 machine (it makes the factory-chat key "
            "and its grant; Claude Code gets the connector added, otherwise the key is shown once on that "
            "terminal, to paste into the connector's header, never into a chat):\n"
            f"  {build_command(ctx.settings, '--chat-key')}",
        )
