"""The bootstrap's GitHub side: `gh` for repos, rulesets, deploy keys and boards, plus the App manifest flow.

`gh` rather than the REST client in `_github.py`: it already holds the person's login, which is the credential
this needs (the App's own credentials do not exist yet). `_github.GithubApp` is reused to check that the App
got installed, since it already speaks the App JWT.
"""

from __future__ import annotations

import hmac
import html
import http.server
import json
import re
import secrets
import subprocess
import threading
import urllib.parse
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import _github, _workspace
from ._bootstrap_base import AppRegistration, BootstrapError, NotAllowed, build_app_name

#: `gh` prints its token problems on stderr with these words; a missing scope is the one a person can fix.
_SCOPE_HINT = "gh auth refresh -s project"
# github.com only, in the three forms git remotes take (https, scp-like ssh, ssh://); the host is anchored so
# that `evilgithub.com` or `github.com.evil.test` is not taken for it.
_REMOTE = re.compile(
    r"(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$"
)
_LIST_LIMIT = "1000"
_CODE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_PERMISSIONS = {
    "contents": "read",
    "metadata": "read",
    "pull_requests": "write",
    "statuses": "write",
    "checks": "read",
    "issues": "write",
}


class GhCli:
    """The real `GitHubOps`: every call is one `gh` (or `git`) process; output is parsed, never echoed."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        open_url: Callable[[str], object] = webbrowser.open,
    ) -> None:
        self._run = run
        self._open_url = open_url

    def _call(
        self, args: Sequence[str], *, stdin: str | None = None, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        """Run `gh` once; a command that hangs (a dead network, a login prompt) ends as a clear error."""
        try:
            return self._run(["gh", *args], input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BootstrapError(f"gh {' '.join(args[:2])} did not answer within {timeout}s") from None

    def _run_gh(self, *args: str, stdin: str | None = None, timeout: int = 120) -> str:
        proc = self._call(args, stdin=stdin, timeout=timeout)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[-500:]
            if "HTTP 403" in err or "HTTP 404" in err or "scope" in err.lower():
                raise NotAllowed(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: {err}".strip())
            raise BootstrapError(f"gh {' '.join(args[:2])} failed ({proc.returncode}): {err}")
        return proc.stdout

    def _parse_json(self, out: str, what: str) -> Any:
        """Parse `gh`'s JSON; a reply that is not what `gh` documents is an error to report, not a crash."""
        try:
            return json.loads(out or "null")
        except ValueError:
            raise BootstrapError(f"gh returned something other than JSON for {what}") from None

    def authenticated(self) -> bool:
        return self._call(["auth", "status"]).returncode == 0

    def owner_is_org(self, owner: str) -> bool:
        return self._run_gh("api", f"users/{owner}", "--jq", ".type").strip() == "Organization"

    def viewer_login(self) -> str:
        return self._run_gh("api", "user", "--jq", ".login").strip()

    def repo_visibility(self, repo: str) -> str | None:
        proc = self._call(["repo", "view", repo, "--json", "visibility", "--jq", ".visibility"])
        if proc.returncode == 0:
            return proc.stdout.strip().lower()
        err = (proc.stderr or "").strip()
        # Only GitHub's own "no such repository" means absent: a dropped connection or an expired login must
        # not read as "create it" (the create would then fail, or worse, race a repository that exists).
        if "Could not resolve to a Repository" in err or "HTTP 404" in err:
            return None
        raise BootstrapError(f"gh repo view {repo} failed ({proc.returncode}): {err[-300:]}")

    def clone_origin_is(self, checkout: Path, repo: str) -> bool:
        url = _workspace.git(checkout, "config", "--get", "remote.origin.url", placeholder_identity=False)
        match = _REMOTE.match(url.strip())
        return match is not None and f"{match[1]}/{match[2]}".lower() == repo.lower()

    def create_repo(self, repo: str) -> None:
        self._run_gh("repo", "create", repo, "--private")

    def clone(self, repo: str, dest: Path) -> None:
        self._run_gh("repo", "clone", repo, str(dest), timeout=300)

    def push(self, checkout: Path) -> None:
        _workspace.git(checkout, "push", "-q", "-u", "origin", "HEAD", placeholder_identity=False)

    def _find_ruleset_id(self, repo: str, name: str) -> int | None:
        listed = self._parse_json(self._run_gh("api", f"repos/{repo}/rulesets"), "the rulesets") or []
        return next((r["id"] for r in listed if r.get("name") == name and "id" in r), None)

    def ruleset_exists(self, repo: str, name: str) -> bool:
        return self._find_ruleset_id(repo, name) is not None

    def ruleset_deploy_key_bypass(self, repo: str, name: str) -> bool:
        # The listing leaves out `bypass_actors`; only the ruleset itself has them.
        found = self._find_ruleset_id(repo, name)
        if found is None:
            raise BootstrapError(f"ruleset {name} does not exist on {repo}")
        ruleset = self._parse_json(self._run_gh("api", f"repos/{repo}/rulesets/{found}"), "the ruleset")
        actors = (ruleset or {}).get("bypass_actors") or []
        return any(a.get("actor_type") == "DeployKey" for a in actors)

    def update_ruleset(self, repo: str, name: str, deploy_key_bypass: bool) -> None:
        found = self._find_ruleset_id(repo, name)
        if found is None:
            raise BootstrapError(f"ruleset {name} does not exist on {repo}")
        self._run_gh(
            "api", "-X", "PUT", f"repos/{repo}/rulesets/{found}", "--input", "-",
            stdin=json.dumps(build_ruleset_body(name, deploy_key_bypass)),
        )  # fmt: skip

    def create_ruleset(self, repo: str, name: str, deploy_key_bypass: bool = False) -> None:
        self._run_gh(
            "api", "-X", "POST", f"repos/{repo}/rulesets", "--input", "-",
            stdin=json.dumps(build_ruleset_body(name, deploy_key_bypass)),
        )  # fmt: skip

    def deploy_key_titles(self, repo: str) -> list[str]:
        keys = self._parse_json(self._run_gh("api", f"repos/{repo}/keys"), "the deploy keys") or []
        return [k.get("title", "") for k in keys]

    def add_deploy_key(self, repo: str, public_key_file: Path, title: str) -> None:
        self._run_gh(
            "repo",
            "deploy-key",
            "add",
            str(public_key_file),
            "--repo",
            repo,
            "--title",
            title,
            "--allow-write",
        )

    def delete_deploy_key(self, repo: str, title: str) -> None:
        keys = self._parse_json(self._run_gh("api", f"repos/{repo}/keys"), "the deploy keys") or []
        for key in keys:
            if key.get("title") == title and "id" in key:
                self._run_gh("api", "-X", "DELETE", f"repos/{repo}/keys/{key['id']}")

    def board_linked(self, repo: str) -> bool:
        owner, name = repo.split("/", 1)
        query = "query($o:String!,$n:String!){repository(owner:$o,name:$n){projectsV2(first:1){totalCount}}}"
        out = self._run_gh("api", "graphql", "-f", f"query={query}", "-f", f"o={owner}", "-f", f"n={name}")
        try:
            count: int = self._parse_json(out, "the board query")["data"]["repository"]["projectsV2"][
                "totalCount"
            ]
        except (KeyError, TypeError):
            raise BootstrapError("gh returned an unexpected answer to the board query") from None
        return count > 0

    def create_board(self, owner: str, repo: str, title: str) -> str:
        """Create (or find by title) the owner's Projects board and link it to the repo; return its URL."""
        # `--limit` far above `gh`'s default of 30: a board past the first page must be found, or a rerun
        # makes a second one.
        out = self._run_gh("project", "list", "--owner", owner, "--format", "json", "--limit", _LIST_LIMIT)
        listed = self._parse_json(out, "the project list") or {}
        found = [p for p in listed.get("projects", []) if p.get("title") == title]
        board = (
            found[0]
            if found
            else self._parse_json(
                self._run_gh("project", "create", "--owner", owner, "--title", title, "--format", "json"),
                "the new project",
            )
        )
        if not isinstance(board, dict) or "number" not in board:
            raise BootstrapError("gh returned a project without a number")
        self._run_gh("project", "link", str(board["number"]), "--owner", owner, "--repo", f"{owner}/{repo}")
        return str(board.get("url", ""))

    def app_installations(self, owner: str) -> list[str]:
        """Slugs of the Apps installed on an organization; a user account has no such listing."""
        out = self._run_gh("api", f"orgs/{owner}/installations", "--jq", "[.installations[].app_slug]")
        slugs: list[str] = self._parse_json(out, "the installations") or []
        return slugs

    def app_exists(self, owner: str, slug: str) -> bool:
        proc = self._call(["api", f"apps/{slug}", "--jq", ".owner.login"])
        if proc.returncode == 0:
            # App names are global: one of that name held by someone else is not the owner's.
            return proc.stdout.strip().lower() == owner.lower()
        err = (proc.stderr or "").strip()
        if "HTTP 404" in err:  # no such App, or a private one of someone else's
            return False
        raise BootstrapError(f"gh api apps/{slug} failed ({proc.returncode}): {err[-300:]}")

    def exchange_manifest_code(self, code: str) -> AppRegistration:
        if not _CODE.fullmatch(code):
            raise BootstrapError("GitHub returned a malformed manifest code")
        data = self._parse_json(
            self._run_gh("api", "-X", "POST", f"/app-manifests/{code}/conversions"), "the App"
        )
        # The conversion also returns a webhook secret and a client secret; they are not read, so they go
        # out of scope with `data`. The webhook is configured later (docs/deploy-webhook.md).
        try:
            return AppRegistration(app_id=str(data["id"]), slug=str(data["slug"]), pem=str(data["pem"]))
        except (KeyError, TypeError):
            raise BootstrapError("GitHub's reply to the App conversion lacked its id, slug or key") from None

    def app_installed(self, registration: AppRegistration, repo: str) -> bool:
        # `gh` cannot sign an App JWT and GitHub lists a user account's installations only to the App itself,
        # so the check is the App's own: asking for an installation token on the repository.
        try:
            _github.GithubApp(registration.app_id, registration.pem).installation_token(repo)
        except _github.GithubError as exc:
            if exc.status == 404:
                return False
            raise BootstrapError(f"checking the App installation failed: {exc}") from exc
        return True

    def register_app(self, owner: str, repo: str, say: Callable[[str], None]) -> str:
        """Run the manifest flow in the browser; return GitHub's one-time code."""
        return register_app(owner, repo, self.owner_is_org(owner), self._open_url, say)


def build_ruleset_body(name: str, deploy_key_bypass: bool) -> dict[str, object]:
    """Pull request with code-owner review on the default branch: the App can only open PRs.

    Repository admins (the owner) bypass `always`: the bootstrap pushes `profile.toml` and the factory URL as
    the owner after this ruleset exists, and a `pull_request`-only bypass would refuse those pushes. Without
    the App the factory's output goes straight to the default branch (docs/install-audit.md), so then the
    deploy key bypasses too; with the App it stays out of the list (the workspace's branch-protection.md).
    The ruleset step keeps the list in step with the mode on every run, both ways.
    """
    actors = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    if deploy_key_bypass:
        actors.append({"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"})
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": actors,
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
        ],
    }


def build_manifest(owner: str, repo: str, redirect_url: str) -> dict[str, object]:
    """Return the App manifest for the workspace.

    Webhooks stay off: `docs/deploy-webhook.md` sets them up later.
    """
    workspace_url = f"https://github.com/{owner}/{repo}"
    return {
        "name": build_app_name(owner),
        "url": workspace_url,
        "redirect_url": redirect_url,
        "public": False,
        "hook_attributes": {"url": workspace_url, "active": False},
        "default_permissions": dict(_PERMISSIONS),
        "default_events": [],
    }


def build_registration_url(owner: str, is_org: bool, state: str) -> str:
    base = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if is_org
        else ("https://github.com/settings/apps/new")
    )
    return f"{base}?state={urllib.parse.quote(state)}"


def read_callback_code(expected_state: str, query: str) -> str:
    """Return the manifest code from GitHub's redirect, or raise `BootstrapError` when the state is not ours.

    The state ties the redirect to the page this process served: without the check any local page could
    hand the bootstrap a code of its choosing.
    """
    params = urllib.parse.parse_qs(query)
    state, code = params.get("state", [""])[0], params.get("code", [""])[0]
    if not hmac.compare_digest(state.encode(), expected_state.encode()):
        raise BootstrapError("the GitHub redirect carried the wrong state; ignoring it")
    if not code:
        raise BootstrapError("the GitHub redirect carried no code")
    return code


class ManifestFlow:
    """A one-shot local web server: serves the auto-submitting form, then waits for GitHub's redirect."""

    def __init__(self, owner: str, repo: str, is_org: bool, state: str | None = None) -> None:
        self.state = state or secrets.token_urlsafe(24)
        self._owner, self._repo, self._is_org = owner, repo, is_org
        self.code: str | None = None
        self._server = http.server.HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.timeout = 1

    @property
    def start_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/"

    def _render_page(self) -> bytes:
        redirect = f"http://127.0.0.1:{self._server.server_port}/callback"
        document = json.dumps(build_manifest(self._owner, self._repo, redirect))
        target = build_registration_url(self._owner, self._is_org, self.state)
        return (
            "<!doctype html><title>Register the factory's GitHub App</title>"
            f'<form id="f" method="post" action="{html.escape(target)}">'
            f'<input type="hidden" name="manifest" value="{html.escape(document)}">'
            "<noscript><button>Continue to GitHub</button></noscript></form>"
            "<p>Sending you to GitHub to register your App...</p>"
            '<script>document.getElementById("f").submit()</script>'
        ).encode()

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        flow = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # the callback URL carries the code
                pass

            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/":
                    self._reply(200, flow._render_page())
                elif parsed.path == "/callback":
                    try:
                        flow.code = read_callback_code(flow.state, parsed.query)
                    except BootstrapError as exc:
                        self._reply(400, html.escape(str(exc)).encode())
                    else:
                        self._reply(
                            200, b"App registered. You can close this tab and go back to the terminal."
                        )
                else:
                    self._reply(404, b"not found")

        return Handler

    def wait(self, attempts: int = 600) -> str:
        """Serve until a valid callback arrives (about `attempts` seconds); raise if none does."""
        for _ in range(attempts):
            self._server.handle_request()
            if self.code:
                self._server.server_close()
                return self.code
        self._server.server_close()
        raise BootstrapError("no redirect from GitHub arrived; run the bootstrap again to retry")


def register_app(
    owner: str, repo: str, is_org: bool, open_url: Callable[[str], object], say: Callable[[str], None]
) -> str:
    """Run the manifest flow in the browser and return GitHub's one-time code."""
    flow = ManifestFlow(owner, repo, is_org)
    say(f"Opening {flow.start_url} to register your GitHub App (open it yourself if no browser appears).")
    threading.Thread(target=open_url, args=(flow.start_url,), daemon=True).start()
    return flow.wait()
