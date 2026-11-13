"""The bootstrap steps for the machine, Flyte and the workspace repository.

They are tools, signin, project, workspace, template, ruleset, deploy-key, engine-token and board. Each step
is `check()` (reads live state, changes nothing) then `do()` (changes it). A step whose check passes is
skipped, so a run after any interruption resumes where it stopped. `_bootstrap.py` holds the table
(the order) and the command line; `_bootstrap_finish.py` the other steps.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from . import _workspace
from ._bootstrap_base import (
    BLOCKED,
    DEPLOY_KEY_TITLE,
    DONE,
    EXPECTED_ERRORS,
    RULESET_NAME,
    SECRET_CLAUDE,
    SECRET_DEPLOY_KEY,
    TODO,
    BootstrapError,
    Check,
    Context,
    NotAllowed,
    NotSignedIn,
    Outcome,
    Settings,
    normalise_endpoint,
)
from ._bootstrap_flyte import ENGINE_ROOT
from ._bootstrap_keys import build_delete_secret_command
from ._bootstrap_profile import FLYTE_CONFIG, SECRET_CODEX_LOGIN

_CODEOWNERS = ".github/CODEOWNERS"
_OWNER_PLACEHOLDER = re.compile(r"@OWNER\b")
REQUIRED_TOOLS = ("git", "ssh-keygen", "gh", "uv")
# The board step's own count; the docs quote it and tests/test_guided_setup_docs.py holds them to it. The
# second click is conditional, but it is still one the person may have to make.
BOARD_CLICKS = "three clicks remain"
_RULESET_STEPS = (
    "Settings > Rules > Rulesets > New branch ruleset, named {name}: target the default branch, require a "
    "pull request and review from Code Owners, block force pushes and deletion (docs/branch-protection.md "
    "in the workspace)"
)


def build_codex_login_command(settings: Settings) -> str:
    """Return the one command that makes the Codex login secret on a machine with `codex` and this checkout.

    It does not need a deployed factory (the in-cluster `codex_bootstrap_login` does). The login file goes
    from a private temporary directory straight into Flyte and the directory is removed whatever happens.
    """
    return (
        f'd=$(mktemp -d); CODEX_HOME="$d" codex login --device-auth && (cd {shlex.quote(str(ENGINE_ROOT))} '
        f"&& uv run flyte --config {FLYTE_CONFIG} create secret {SECRET_CODEX_LOGIN} "
        f'--from-file "$d/auth.json" -p {shlex.quote(settings.project)} -d {shlex.quote(settings.domain)}); '
        'rm -rf "$d"'
    )


def describe_unclean_or_unpushed(checkout: Path, paths: tuple[str, ...] = ()) -> str | None:
    """None when `paths` (or everything) is committed and pushed; else what is left to do."""
    pathspec = ["--", *paths] if paths else []
    if _workspace.git(checkout, "status", "--porcelain", *pathspec, placeholder_identity=False).strip():
        return "uncommitted changes"
    try:
        ahead = _workspace.git(checkout, "rev-list", "--count", "@{u}..HEAD", placeholder_identity=False)
    except RuntimeError:  # no upstream yet: a first push is due (or this is not a repository)
        return "not pushed"
    return "not pushed" if ahead.strip() != "0" else None


def commit_paths(checkout: Path, paths: tuple[str, ...], message: str) -> None:
    """Commit just `paths`, as the person: a rerun must not sweep their other edits into the commit."""
    _workspace.require_identity(checkout)
    _workspace.git(checkout, "add", "--", *paths, placeholder_identity=False)
    _workspace.git(checkout, "commit", "-q", "-m", message, "--", *paths, placeholder_identity=False)


def check_waiting_on_repository(ctx: Context) -> Check | None:
    """Return a `Check` saying the step waits on the workspace repository, or None when it exists.

    A check that asks GitHub about a repository that is not there yet (`--status` on a fresh install) would
    otherwise end in GitHub's "Could not resolve to a Repository".
    """
    if ctx.github.repo_visibility(ctx.settings.workspace) is None:
        return Check(False, f"waits on the repository {ctx.settings.workspace}, which does not exist yet")
    return None


# One install command per tool and platform, for both `plan()` and `do()`. "other" is a page to read, since
# there is no command we can promise on a platform we did not test. macOS ships `ssh-keygen`, so brew only
# matters for a stripped-down PATH; Linux names the Debian/Ubuntu line because it is the common one.
_INSTALL_COMMANDS: dict[str, dict[str, str]] = {
    "git": {
        "macos": "`brew install git`",
        "linux": "`sudo apt-get install -y git`",
        "other": "https://git-scm.com/downloads",
    },
    "ssh-keygen": {
        "macos": "`brew install openssh`",
        "linux": "`sudo apt-get install -y openssh-client`",
        "other": "install OpenSSH (it provides ssh-keygen)",
    },
    "gh": {
        "macos": "`brew install gh`",
        "linux": "`sudo apt-get install -y gh` (Debian 12, Ubuntu 22.04 or later; otherwise "
        "https://github.com/cli/cli/blob/trunk/docs/install_linux.md)",
        "other": "https://cli.github.com",
    },
    "uv": {
        "macos": "`brew install uv`",
        "linux": "`curl -LsSf https://astral.sh/uv/install.sh | sh`",
        "other": "https://docs.astral.sh/uv",
    },
}
assert set(_INSTALL_COMMANDS) == set(REQUIRED_TOOLS)


def _platform_key(platform: str) -> str:
    return {"darwin": "macos", "linux": "linux"}.get(platform, "other")


class ToolsStep:
    id = "tools"
    informational = False
    advisory = False

    def _find_missing_tools(self, ctx: Context) -> list[str]:
        return [tool for tool in REQUIRED_TOOLS if not ctx.which(tool)]

    def _next_action(self, ctx: Context) -> str:
        """Return what the person has to do now: the install line of each missing tool, else sign in to gh."""
        if missing := self._find_missing_tools(ctx):
            key = _platform_key(ctx.platform)
            lines = [f"{tool}: {_INSTALL_COMMANDS[tool][key]}" for tool in missing]
            return f"install {'; '.join(lines)}, then run this again"
        return "run `gh auth login`, then run this again"

    def check(self, ctx: Context) -> Check:
        if missing := self._find_missing_tools(ctx):
            return Check(False, f"not on PATH: {', '.join(missing)}; {self._next_action(ctx)}")
        if not ctx.github.authenticated():
            return Check(False, f"gh is not signed in; {self._next_action(ctx)}")
        return Check(True, "git, ssh-keygen, gh (signed in), uv")

    def plan(self, ctx: Context) -> str:
        return f"ask you to {self._next_action(ctx)}"

    def do(self, ctx: Context) -> Outcome:
        return Outcome(BLOCKED, self._next_action(ctx))


class SignInStep:
    id = "signin"
    informational = False
    advisory = False

    def check(self, ctx: Context) -> Check:
        found = ctx.flyte.config_endpoint()
        if found is None:
            return Check(False, f"no Flyte config at {ctx.flyte.config_path}")
        if normalise_endpoint(found) != ctx.settings.endpoint:
            return Check(False, f"{ctx.flyte.config_path} is for another endpoint ({found})")
        try:
            ctx.flyte.sign_in()
        except NotSignedIn as exc:
            return Check(False, str(exc))
        except EXPECTED_ERRORS as exc:  # any failure here means "not signed in"
            return Check(False, f"not signed in ({type(exc).__name__})")
        return Check(True, f"signed in to {ctx.settings.endpoint}")

    def plan(self, ctx: Context) -> str:
        return f"write {ctx.flyte.config_path} and open the browser sign-in for {ctx.settings.endpoint}"

    def do(self, ctx: Context) -> Outcome:
        found = ctx.flyte.config_endpoint()
        if found is None:
            ctx.flyte.create_config()
        elif normalise_endpoint(found) != ctx.settings.endpoint:
            return Outcome(
                BLOCKED,
                f"{ctx.flyte.config_path} points at {found}; move it aside (it is not overwritten) and run "
                f"again",
            )
        ctx.say("Signing in to Flyte: a browser window opens; finish the sign-in there.")
        ctx.flyte.sign_in()
        return Outcome(DONE, f"signed in to {ctx.settings.endpoint}")


class ProjectStep:
    id = "project"
    informational = False
    advisory = False

    def check(self, ctx: Context) -> Check:
        found = ctx.flyte.project_exists()
        return Check(found, f"project {ctx.settings.project}" + ("" if found else " does not exist"))

    def plan(self, ctx: Context) -> str:
        return f"create Flyte project {ctx.settings.project}"

    def do(self, ctx: Context) -> Outcome:
        ctx.flyte.create_project()
        return Outcome(DONE, f"created project {ctx.settings.project}")


class WorkspaceStep:
    """The repository exists and is not public.

    The profile written into it names private deployment coordinates (host, project, bucket, secret
    names), and the operator's notes live there too.
    """

    id = "workspace"
    informational = False
    advisory = False

    def _describe_public_problem(self, ctx: Context, visibility: str) -> str:
        return (
            f"{ctx.settings.workspace} is {visibility}: its profile and operator notes are private. Make it "
            "private yourself (repository Settings > General > Danger Zone > Change visibility); this tool "
            "never changes visibility. Then run this again"
        )

    def check(self, ctx: Context) -> Check:
        visibility = ctx.github.repo_visibility(ctx.settings.workspace)
        if visibility is None:
            return Check(False, ctx.settings.workspace + " does not exist")
        if visibility == "public":
            return Check(False, self._describe_public_problem(ctx, visibility))
        return Check(True, f"{ctx.settings.workspace} ({visibility})")

    def plan(self, ctx: Context) -> str:
        return f"create the private repository {ctx.settings.workspace}"

    def do(self, ctx: Context) -> Outcome:
        visibility = ctx.github.repo_visibility(ctx.settings.workspace)
        if visibility is not None:  # the check failed because it is public
            return Outcome(BLOCKED, self._describe_public_problem(ctx, visibility))
        ctx.github.create_repo(ctx.settings.workspace)
        return Outcome(DONE, f"created private {ctx.settings.workspace}")


class TemplateStep:
    """The template in the clone, committed as the person and pushed; every sub-step is re-entrant."""

    id = "template"
    informational = False
    advisory = False

    def _list_template_paths(self) -> tuple[str, ...]:
        return tuple(sorted(p.name for p in _workspace.TEMPLATE.iterdir()))

    def _needs_clone(self, ctx: Context) -> bool:
        """Return True when `--dir` is absent or an empty directory, which `git clone` can fill."""
        clone = ctx.settings.clone
        return not clone.exists() or (clone.is_dir() and not any(clone.iterdir()))

    def _explain_wrong_directory(self, ctx: Context) -> str | None:
        """Why `--dir` cannot be used: another repository's checkout, or a directory of somebody's files."""
        clone, workspace = ctx.settings.clone, ctx.settings.workspace
        if (clone / ".git").exists():
            if not ctx.github.clone_origin_is(clone, workspace):
                return f"{clone} is not a clone of {workspace} (check its `origin`); pick another --dir"
        elif not self._needs_clone(ctx):
            return (
                f"{clone} is not empty and is not a git checkout; use an empty directory or a clone of "
                f"{workspace}: pick another --dir"
            )
        return None

    def _find_leftover(self, ctx: Context) -> str | None:
        clone = ctx.settings.clone
        if problem := self._explain_wrong_directory(ctx):
            return problem
        if self._needs_clone(ctx):
            return "not cloned yet"
        if not (clone / "profile.example.toml").is_file() or not (clone / "standards").is_dir():
            return "the template is not in the clone"
        if _OWNER_PLACEHOLDER.search(
            (clone / _CODEOWNERS).read_text() if (clone / _CODEOWNERS).is_file() else ""
        ):
            return "CODEOWNERS still names @OWNER"
        # Only the template's own paths: the person's profile.toml and other edits are not this step's
        # business.
        return describe_unclean_or_unpushed(clone, self._list_template_paths())

    def check(self, ctx: Context) -> Check:
        left = self._find_leftover(ctx)
        return Check(left is None, f"{ctx.settings.clone}" + (f": {left}" if left else ""))

    def plan(self, ctx: Context) -> str:
        return f"clone {ctx.settings.workspace} to {ctx.settings.clone}, add the template, commit and push"

    def do(self, ctx: Context) -> Outcome:
        clone = ctx.settings.clone
        if problem := self._explain_wrong_directory(ctx):
            return Outcome(BLOCKED, problem)  # before anything is written into somebody else's files
        if self._needs_clone(ctx):
            ctx.github.clone(ctx.settings.workspace, clone)
        try:
            if not (clone / "profile.example.toml").is_file():
                _workspace.create(clone, commit=True)
            elif _workspace.git(
                clone, "status", "--porcelain", "--", *self._list_template_paths(), placeholder_identity=False
            ).strip():
                commit_paths(clone, self._list_template_paths(), "Workspace from template")
            self._name_the_owner(ctx)
        except FileExistsError as exc:
            return Outcome(BLOCKED, f"{exc}; pick another --dir")
        except RuntimeError as exc:
            return Outcome(BLOCKED, str(exc))
        ctx.github.push(clone)
        return Outcome(DONE, f"template pushed from {clone}")

    def _name_the_owner(self, ctx: Context) -> None:
        """Put the owner into CODEOWNERS and commit it.

        With `@OWNER` left in, code-owner review (the ruleset's rule) is requested from nobody, and the
        protection is a rule that cannot be satisfied.
        """
        clone = ctx.settings.clone
        path = clone / _CODEOWNERS
        if not path.is_file() or not _OWNER_PLACEHOLDER.search(path.read_text()):
            return
        # A bare @org is not a valid code owner (only users and @org/team are), so an organization's
        # workspace names the person running setup.
        owner = ctx.settings.owner
        if ctx.github.owner_is_org(owner):
            owner = ctx.github.viewer_login()
        path.write_text(_OWNER_PLACEHOLDER.sub(f"@{owner}", path.read_text()))
        commit_paths(clone, (_CODEOWNERS,), "CODEOWNERS: name the workspace owner")


class RulesetStep:
    id = "ruleset"
    informational = False
    advisory = True

    def check(self, ctx: Context) -> Check:
        # The deploy key bypasses the ruleset exactly when there is no App (without one the factory's output
        # goes straight to the default branch), so a ruleset made in the other mode is out of date.
        want = ctx.settings.no_app
        if waiting := check_waiting_on_repository(ctx):
            return waiting
        try:
            if not ctx.github.ruleset_exists(ctx.settings.workspace, RULESET_NAME):
                return Check(False, f"{RULESET_NAME} does not exist")
            has = ctx.github.ruleset_deploy_key_bypass(ctx.settings.workspace, RULESET_NAME)
        except NotAllowed:
            return Check(False, "cannot read rulesets on this repository")
        if has != want:
            return Check(
                False,
                f"{RULESET_NAME} {'lets' if has else 'does not let'} the deploy key bypass it, "
                f"which does not fit {'--no-app' if want else 'the App'}",
            )
        return Check(True, RULESET_NAME + (" (deploy key may push: no App)" if want else ""))

    def plan(self, ctx: Context) -> str:
        return f"create or update the ruleset {RULESET_NAME} on {ctx.settings.workspace}"

    def do(self, ctx: Context) -> Outcome:
        repo, want = ctx.settings.workspace, ctx.settings.no_app
        try:
            if ctx.github.ruleset_exists(repo, RULESET_NAME):
                ctx.github.update_ruleset(repo, RULESET_NAME, deploy_key_bypass=want)
                return Outcome(
                    DONE, f"updated ruleset {RULESET_NAME}: deploy key bypass {'on' if want else 'off'}"
                )
            ctx.github.create_ruleset(repo, RULESET_NAME, deploy_key_bypass=want)
        except NotAllowed:
            # Private repositories on a free plan cannot have rulesets: say what to do by hand.
            return Outcome(TODO, _RULESET_STEPS.format(name=RULESET_NAME))
        return Outcome(DONE, f"created ruleset {RULESET_NAME}")


_KEY_DIR_PREFIX = ".agent-factory-key-"
#: A key directory is named `<prefix><pid of the run that made it>-<random>`, so another run can tell a
#: leftover from a directory a bootstrap running at the same time is using.
_KEY_DIR_OWNER = re.compile(rf"{re.escape(_KEY_DIR_PREFIX)}(\d+)-")


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists, and is somebody else's
        return True
    return True


def _remove_stale_key_dirs(home: Path) -> None:
    """Remove the key directories an earlier run left in `home` when it was killed before its `finally`.

    Each holds the private half of a key pair whose public half is gone from GitHub (this step deletes it),
    so it is useless and should not linger. Only real directories of this user with the exact prefix: never
    a symlink, so nothing outside `home` is followed. One whose recorded pid is a live process belongs to a
    bootstrap running now, and stays.
    """
    for path in home.glob(f"{_KEY_DIR_PREFIX}*"):
        owner = _KEY_DIR_OWNER.match(path.name)
        if owner and _process_is_alive(int(owner[1])):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
            shutil.rmtree(path, ignore_errors=True)


class DeployKeyStep:
    id = "deploy-key"
    informational = False
    advisory = False

    def _read_state(self, ctx: Context) -> tuple[bool, bool]:
        secret = ctx.flyte.secret_exists(SECRET_DEPLOY_KEY)
        key = DEPLOY_KEY_TITLE in ctx.github.deploy_key_titles(ctx.settings.workspace)
        return secret, key

    def check(self, ctx: Context) -> Check:
        if waiting := check_waiting_on_repository(ctx):
            return waiting
        secret, key = self._read_state(ctx)
        return Check(
            secret and key,
            f"secret {'present' if secret else 'missing'}, deploy key {'present' if key else 'missing'}",
        )

    def plan(self, ctx: Context) -> str:
        return (
            f"make a key pair, add the public key to {ctx.settings.workspace} "
            "and keep the private key as a Flyte secret"
        )

    def do(self, ctx: Context) -> Outcome:
        secret, key = self._read_state(ctx)
        if secret:
            # The private half lives only in Flyte, which cannot return it, so there is nothing to pair with.
            return Outcome(
                BLOCKED,
                f"secret {SECRET_DEPLOY_KEY} exists but the repository has no deploy key "
                f"{DEPLOY_KEY_TITLE!r}; "
                f"delete it with: {build_delete_secret_command(ctx.settings, SECRET_DEPLOY_KEY)} , then run "
                "this again",
            )
        if key:  # a public half whose private half was never stored (an interrupted run): nothing uses it
            ctx.github.delete_deploy_key(ctx.settings.workspace, DEPLOY_KEY_TITLE)
        # mkdtemp: mode 0700. Under the home directory, not a shared /tmp, when there is one.
        home = Path.home()
        if home.is_dir():
            _remove_stale_key_dirs(home)
        workdir = Path(
            tempfile.mkdtemp(prefix=f"{_KEY_DIR_PREFIX}{os.getpid()}-", dir=home if home.is_dir() else None)
        )
        try:
            private = workdir / "key"
            try:
                proc = subprocess.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-C",
                        DEPLOY_KEY_TITLE,
                        "-f",
                        str(private),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                raise BootstrapError("ssh-keygen did not finish within 60s") from None
            if proc.returncode != 0:
                raise BootstrapError(f"ssh-keygen failed ({proc.returncode})")
            ctx.github.add_deploy_key(ctx.settings.workspace, workdir / "key.pub", DEPLOY_KEY_TITLE)
            try:
                ctx.flyte.create_secret(SECRET_DEPLOY_KEY, private.read_text())
            except BaseException:
                # Undo the public half, so a rerun starts clean instead of finding half a pair.
                ctx.github.delete_deploy_key(ctx.settings.workspace, DEPLOY_KEY_TITLE)
                raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return Outcome(
            DONE, f"deploy key {DEPLOY_KEY_TITLE!r} added and stored as secret {SECRET_DEPLOY_KEY}"
        )


class EngineTokenStep:
    id = "engine-token"
    informational = False
    advisory = False

    def _list_secret_names(self, ctx: Context) -> list[str]:
        return [SECRET_CLAUDE] + ([SECRET_CODEX_LOGIN] if ctx.settings.engine == "codex" else [])

    def check(self, ctx: Context) -> Check:
        missing = [n for n in self._list_secret_names(ctx) if not ctx.flyte.secret_exists(n)]
        return Check(not missing, f"secret {', '.join(missing)} missing" if missing else "secrets present")

    def plan(self, ctx: Context) -> str:
        return f"ask for your `claude setup-token` token (hidden) and store it as secret {SECRET_CLAUDE}"

    def do(self, ctx: Context) -> Outcome:
        if not ctx.flyte.secret_exists(SECRET_CLAUDE):
            try:
                token = ctx.ask_secret(
                    "Paste the token `claude setup-token` printed (input is hidden; Enter to do it later): "
                ).strip()
            except (EOFError, OSError):  # no terminal to prompt on (a chat running this)
                token = ""
            if not token:
                return Outcome(
                    TODO,
                    "run `claude setup-token` in another terminal, then run this again and paste the token "
                    "at the prompt (it goes straight into Flyte and is never shown)",
                )
            ctx.flyte.create_secret(SECRET_CLAUDE, token)
        if ctx.settings.engine == "codex" and not ctx.flyte.secret_exists(SECRET_CODEX_LOGIN):
            return Outcome(
                TODO,
                f"create the Codex login secret {SECRET_CODEX_LOGIN} (sign in to ChatGPT when asked; the "
                f"login goes straight into Flyte and is never shown) with this one command, then run this "
                f"again: "
                f"{build_codex_login_command(ctx.settings)}",
            )
        return Outcome(DONE, f"stored secret {SECRET_CLAUDE}")


class BoardStep:
    id = "board"
    informational = False
    advisory = True

    def check(self, ctx: Context) -> Check:
        if ctx.settings.no_board:
            return Check(True, "skipped (--no-board): no backlog board")
        if waiting := check_waiting_on_repository(ctx):
            return waiting
        try:
            found = ctx.github.board_linked(ctx.settings.workspace)
        except NotAllowed:
            return Check(False, "cannot read boards: run `gh auth refresh -s project`")
        return Check(
            found,
            "a board is already linked to the repository; the bootstrap makes none"
            if found
            else "no board linked to the repository",
        )

    def plan(self, ctx: Context) -> str:
        return f"create a Projects board for {ctx.settings.owner} and link it to {ctx.settings.workspace}"

    def do(self, ctx: Context) -> Outcome:
        try:
            url = ctx.github.create_board(
                ctx.settings.owner, ctx.settings.repo_name, f"{ctx.settings.repo_name} backlog"
            )
        except NotAllowed:
            return Outcome(TODO, "run `gh auth refresh -s project`, then run this again")
        return Outcome(
            DONE,
            f"board {url} linked; {BOARD_CLICKS} (GitHub offers no API): in the board's Settings > "
            "Default repository, choose this repository; check that the repository's Projects tab lists "
            "the board (link it there if not); and turn on the built-in workflows that add new issues to "
            "the board",
        )
