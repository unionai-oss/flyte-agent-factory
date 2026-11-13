"""The two key modes of the bootstrap: `--chat-key` and `--deploy-key`, one command each.

Each makes a Union API key with no default policies, a policy of the same name (`contributor` on the
factory's project and domain) and its assignment, then hands the key over without it reaching stdout, stderr
or a chat: to `claude mcp add` as a subprocess argument, or once to the controlling terminal. When Union
refuses the person the policy or the assignment, the key is still handed over and the line ends with one
filled-in `--grant POLICY CLIENT_ID` command for an admin to run (docs/factory-mcp-setup.md, "Grant the key a
role"); that command makes the same policy and assignment, and no key.
"""

from __future__ import annotations

import shlex
import subprocess
import tomllib

from ._bootstrap_base import (
    BLOCKED,
    DEFAULT_DOMAIN,
    DONE,
    EXPECTED_ERRORS,
    TODO,
    AccessDenied,
    BootstrapError,
    Context,
    IssuedKey,
    KeyExists,
    Outcome,
    Settings,
)
from ._bootstrap_flyte import ENGINE_ROOT
from ._bootstrap_profile import ENGINE_REPO_URL

CHAT_KEY, DEPLOY_KEY = "factory-chat", "factory-deploy"
#: The only policies `--grant` gives: an admin's run must not become a way to assign anything else.
GRANTABLE = (CHAT_KEY, DEPLOY_KEY)
#: The connector names that count as "already connected": the one this adds, and the one the hidden-prompt
#: block in docs/chat-operator.md adds.
CONNECTOR_NAMES = ("agent-factory", "factory")
_CLONE_AND_SYNC = (
    'd=~/flyte-agent-factory; u={url}; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && '
    'case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; '
    '*) echo "$d is not a clone of $u" >&2; false ;; esac && '
    'git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && '
    "uv sync -q --python 3.12 && uv run python ops/bootstrap.py"
)


def build_command(settings: Settings, flag: str = "") -> str:
    """Return the one copy-paste command: clone or update the engine, then run the bootstrap with `flag`.

    The prefix is the one skills/agent-factory/references/setup.md hands over; tests hold the two together.
    """
    parts = [
        _CLONE_AND_SYNC.replace("{url}", ENGINE_REPO_URL),
        f"--endpoint {shlex.quote(settings.endpoint)}",
        f"--project {shlex.quote(settings.project)}",
        f"--workspace {shlex.quote(settings.workspace)}",
        f"--state-uri {shlex.quote(settings.state_uri)}",
    ]
    if settings.domain != DEFAULT_DOMAIN:
        parts.append(f"--domain {shlex.quote(settings.domain)}")
    if flag:
        parts.append(flag)
    return " ".join(parts)


def build_delete_secret_command(settings: Settings, name: str) -> str:
    """Return the command that deletes Flyte secret `name` in the run's project and domain."""
    return (
        f"cd {shlex.quote(str(ENGINE_ROOT))} && uv run flyte delete secret {shlex.quote(name)} "
        f"-p {shlex.quote(settings.project)} -d {shlex.quote(settings.domain)}"
    )


def find_connector_url(ctx: Context) -> str:
    """Return the factory-mcp connector URL from the profile, else from the deployed app; "" when unknown."""
    try:
        url = tomllib.loads(ctx.profile_path.read_text())["mcp"]["public_url"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        url = ""
    url = url or ctx.flyte.factory_mcp_url() or ""
    return f"{url.rstrip('/')}/flyte-mcp/mcp" if url else ""


def _make_key(ctx: Context, name: str) -> tuple[IssuedKey, bool] | Outcome:
    """Make policy `name`, the key `name` and the assignment; undo the key when a later call fails.

    Returns the key and whether it was granted. A key Union refused this person the right to grant is kept:
    its secret goes to the person as usual and an admin grants it by client ID (`_admin_must_grant`).
    """
    # The policy first: one that exists with other bindings stops the run before a key is made, which would
    # otherwise be lost with its secret. A refusal for want of access-management rights does not stop it.
    try:
        ctx.flyte.ensure_policy(name)
        granted = True
    except AccessDenied:
        granted = False
    try:
        key = ctx.flyte.create_api_key(name)
    except KeyExists:
        return Outcome(
            BLOCKED,
            f"a Union API key named {name!r} already exists and its secret cannot be read back; delete it "
            "(`flyte get api-key` lists its client ID, `flyte delete api-key <client ID>` removes it) "
            "and run this again, or reuse the key you stored when you made it "
            "(docs/factory-mcp-setup.md, Connecting a client)",
        )
    if not granted:
        return key, False
    try:
        ctx.flyte.assign_policy(key.client_id, name)
    except AccessDenied:
        return key, False
    except EXPECTED_ERRORS as exc:
        _discard(ctx, key)
        raise BootstrapError(
            f"assigning policy {name!r} to the new key failed ({type(exc).__name__}); the key was deleted"
        ) from None
    except Exception:  # an unexpected failure: its secret never reached the person either, so the key goes
        _discard(ctx, key)
        raise
    return key, True


def _admin_must_grant(ctx: Context, key: IssuedKey, name: str, handed_over: str) -> Outcome:
    """Return the `todo` for a key the person holds but may not grant: one filled-in command for an admin."""
    command = build_command(ctx.settings, f"--grant {name} {shlex.quote(key.client_id)}")
    return Outcome(
        TODO,
        f"key {name} made and {handed_over}, but Union refused you the right to give it policy {name}, so "
        f"it can do nothing yet. Send this one command to a Union admin; it gives policy {name} "
        f"(contributor on {ctx.settings.project}/{ctx.settings.domain}) to the key's client ID "
        f"{key.client_id} and makes no key: {command}",
    )


def grant_key(ctx: Context, policy: str, client_id: str) -> Outcome:
    """`--grant POLICY CLIENT_ID`: the admin's half, for a key whose maker may not grant it."""
    ctx.flyte.ensure_policy(policy)
    ctx.flyte.assign_policy(client_id, policy)
    return Outcome(
        DONE,
        f"policy {policy} (contributor on {ctx.settings.project}/{ctx.settings.domain}) assigned to the key "
        f"with client ID {client_id}",
    )


def _discard(ctx: Context, key: IssuedKey) -> None:
    """Delete a key that was made but never reached the person: its secret is lost, so it is of no use."""
    try:
        ctx.flyte.delete_api_key(key.client_id)
    except EXPECTED_ERRORS as exc:
        ctx.say(
            f"could not delete the unused API key {key.client_id!r} ({type(exc).__name__}); delete it by hand"
        )


def _show_once(ctx: Context, key: IssuedKey, lines: str) -> Outcome | None:
    """Write `lines` and then the key to the terminal. Returns an outcome only when that is impossible."""
    try:
        ctx.write_tty(f"\n{lines}\n{key.encoded}\n\n")
    except OSError:
        _discard(ctx, key)
        return Outcome(
            BLOCKED,
            "there is no terminal to show the new key on, so it was deleted; run this command in a terminal "
            "window, not through a tool that captures output",
        )
    return None


def _connector_present(ctx: Context) -> bool:
    proc = ctx.run_command(["claude", "mcp", "list"])
    listing = proc.stdout.lower() if proc.returncode == 0 else ""
    return any(f"{name}:" in listing for name in CONNECTOR_NAMES)


def make_chat_key(ctx: Context) -> Outcome:
    """`--chat-key`: make the chat key and connector credentials, then hand the key over."""
    connector = find_connector_url(ctx)
    if not connector:
        return Outcome(TODO, "factory-mcp has no public URL yet; run the bootstrap without --chat-key first")
    have_claude = ctx.which("claude") is not None
    if have_claude and _connector_present(ctx):
        return Outcome(DONE, "the agent-factory connector is already in Claude Code; no new key made")
    made = _make_key(ctx, CHAT_KEY)
    if isinstance(made, Outcome):
        return made
    key, granted = made
    if have_claude:
        try:
            proc = ctx.run_command(
                [
                    "claude", "mcp", "add", "-s", "user", "--transport", "http", "agent-factory", connector,
                    "--header", f"Authorization: Bearer {key.encoded}",
                ]
            )  # fmt: skip
        except (subprocess.SubprocessError, OSError) as exc:  # its text holds the argv, so the key: unprinted
            _discard(ctx, key)
            return Outcome(
                BLOCKED,
                f"claude mcp add failed ({type(exc).__name__}); the new key was deleted, run this again",
            )
        if proc.returncode != 0:
            # Neither stream is printed: claude may echo the header it was given.
            _discard(ctx, key)
            return Outcome(
                BLOCKED, f"claude mcp add failed ({proc.returncode}); the new key was deleted, run this again"
            )
        if not granted:
            return _admin_must_grant(ctx, key, CHAT_KEY, "the agent-factory connector added to Claude Code")
        return Outcome(
            DONE,
            f"key {CHAT_KEY} made, policy {CHAT_KEY} assigned (contributor on {ctx.settings.project}/"
            f"{ctx.settings.domain}); the agent-factory connector was added to Claude Code",
        )
    shown = _show_once(
        ctx,
        key,
        f"Your {CHAT_KEY} key is below, shown once, on this terminal only. In claude.ai add a custom "
        f"connector for {connector} with a request header Authorization: Bearer <the key>; paste it into "
        "that header and never into a chat.",
    )
    if shown:
        return shown
    if not granted:
        return _admin_must_grant(ctx, key, CHAT_KEY, "shown once on your terminal for the connector's header")
    return Outcome(
        DONE,
        f"key {CHAT_KEY} made, policy {CHAT_KEY} assigned (contributor on {ctx.settings.project}/"
        f"{ctx.settings.domain}); the key is shown once on your terminal only, to paste into the "
        "connector's header",
    )


def make_deploy_key(ctx: Context) -> Outcome:
    """`--deploy-key`: make the deployment key and show it once on the terminal."""
    made = _make_key(ctx, DEPLOY_KEY)
    if isinstance(made, Outcome):
        return made
    key, granted = made
    shown = _show_once(
        ctx,
        key,
        f"Your {DEPLOY_KEY} key is below, shown once, on this terminal only. Store it in a password manager, "
        "then export _UNION_EAGER_API_KEY with the hidden-prompt block in docs/quickstart.md "
        "(Local Union deployment bootstrap); never paste it into a chat.",
    )
    if shown:
        return shown
    if not granted:
        return _admin_must_grant(ctx, key, DEPLOY_KEY, "shown once on your terminal")
    return Outcome(
        DONE,
        f"key {DEPLOY_KEY} made, policy {DEPLOY_KEY} assigned (contributor on {ctx.settings.project}/"
        f"{ctx.settings.domain}); the key is shown once on your terminal only",
    )
