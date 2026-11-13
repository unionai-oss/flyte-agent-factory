"""The bootstrap's `gh` adapter, against a scripted `gh`: the commands it issues, how it reads answers."""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from flyte_agent_factory import _bootstrap_github, _github
from flyte_agent_factory._bootstrap_base import AppRegistration, BootstrapError, NotAllowed
from flyte_agent_factory._bootstrap_github import GhCli

CHECKOUT_REPO = "pat/ws"


def scripted(*replies: tuple[int, str, str] | str):
    """A `GhCli` whose `gh` answers each call in turn; a bare string is a successful stdout."""
    queue = list(replies)
    calls: list[list[str]] = []

    def run(cmd, input=None, **kwargs):
        calls.append([*cmd[1:]])
        reply = queue.pop(0) if queue else ""
        code, out, err = (0, reply, "") if isinstance(reply, str) else reply
        return subprocess.CompletedProcess(cmd, code, out, err)

    return GhCli(run=run), calls


@pytest.fixture(autouse=True)
def person(monkeypatch):
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Pat Example")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "pat@example.com")


# --- failures of `gh` itself ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr", ["HTTP 403: nope", "HTTP 404: Not Found", "your token has not the project scope"]
)
def test_a_refusal_by_github_is_not_allowed_with_gh_s_words(stderr):
    gh, _ = scripted((1, "", stderr))
    with pytest.raises(NotAllowed, match="gh api user: ") as refused:
        gh._run_gh("api", "user")
    assert stderr in str(refused.value)


def test_a_refusal_of_a_command_with_one_word_still_names_it():
    gh, _ = scripted((1, "", "HTTP 403"))
    with pytest.raises(NotAllowed, match="^gh api : HTTP 403$"):
        gh._run_gh("api")


def test_any_other_failure_is_a_bootstrap_error_with_the_exit_code_and_the_end_of_stderr():
    gh, _ = scripted((4, "", "x" * 600 + "the end"))
    with pytest.raises(BootstrapError, match=r"^gh repo create failed \(4\): x+the end$") as failed:
        gh.create_repo("pat/ws")
    assert not isinstance(failed.value, NotAllowed) and len(str(failed.value)) < 560


# --- reading who and what --------------------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "expected"), [("Organization\n", True), ("User\n", False)])
def test_the_owner_is_an_org_when_github_says_so(answer, expected):
    gh, calls = scripted(answer)
    assert gh.owner_is_org("acme") is expected
    assert calls == [["api", "users/acme", "--jq", ".type"]]


def test_the_viewer_login_is_stripped():
    gh, calls = scripted("pat\n")
    assert gh.viewer_login() == "pat"
    assert calls == [["api", "user", "--jq", ".login"]]


def test_the_visibility_is_lowercased():
    gh, _ = scripted("PRIVATE\n")
    assert gh.repo_visibility("pat/ws") == "private"


# --- repositories ----------------------------------------------------------------------------------------


def test_create_repo_makes_a_private_repository():
    gh, calls = scripted("")
    gh.create_repo("pat/ws")
    assert calls == [["repo", "create", "pat/ws", "--private"]]


def test_clone_asks_gh_to_clone_into_the_destination_with_a_long_timeout():
    timeouts: list[int] = []

    def run(cmd, input=None, timeout=None, **kwargs):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    GhCli(run=run).clone("pat/ws", Path("/somewhere/clone"))
    assert timeouts == [300]


def test_push_sends_head_to_origin_and_sets_upstream(tmp_path):
    remote, checkout = tmp_path / "remote.git", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True, capture_output=True)
    (checkout / "f.txt").write_text("x")
    for cmd in (["add", "f.txt"], ["commit", "-qm", "first"]):
        subprocess.run(["git", "-C", str(checkout), *cmd], check=True)
    GhCli().push(checkout)
    pushed = subprocess.run(
        ["git", "-C", str(remote), "log", "--format=%s", "main"], capture_output=True, text=True, check=True
    )
    assert pushed.stdout.strip() == "first"


# --- rulesets --------------------------------------------------------------------------------------------

LISTING = json.dumps([{"id": 7, "name": "main-rules"}, {"name": "no-id"}])


@pytest.mark.parametrize(("name", "expected"), [("main-rules", True), ("other", False), ("no-id", False)])
def test_ruleset_exists_looks_the_name_up_in_the_listing(name, expected):
    gh, calls = scripted(LISTING)
    assert gh.ruleset_exists(CHECKOUT_REPO, name) is expected
    assert calls == [["api", "repos/pat/ws/rulesets"]]


def test_a_ruleset_that_is_not_there_cannot_be_inspected_or_updated():
    gh, _ = scripted(LISTING)
    with pytest.raises(BootstrapError, match="ruleset nope does not exist on pat/ws"):
        gh.ruleset_deploy_key_bypass(CHECKOUT_REPO, "nope")
    gh, calls = scripted(LISTING)
    with pytest.raises(BootstrapError, match="ruleset nope does not exist on pat/ws"):
        gh.update_ruleset(CHECKOUT_REPO, "nope", deploy_key_bypass=True)
    assert len(calls) == 1, "nothing was written"


# --- deploy keys -----------------------------------------------------------------------------------------


def test_deploy_key_titles_lists_the_titles_and_tolerates_an_empty_answer():
    gh, calls = scripted(json.dumps([{"title": "a"}, {"id": 2}]))
    assert gh.deploy_key_titles(CHECKOUT_REPO) == ["a", ""]
    assert calls == [["api", "repos/pat/ws/keys"]]
    gh, _ = scripted("")
    assert gh.deploy_key_titles(CHECKOUT_REPO) == []


def test_add_deploy_key_gives_gh_the_public_key_file_with_write_access():
    gh, calls = scripted("")
    gh.add_deploy_key(CHECKOUT_REPO, Path("/k/id.pub"), "factory")
    assert calls == [
        ["repo", "deploy-key", "add", "/k/id.pub", "--repo", "pat/ws", "--title", "factory", "--allow-write"]
    ]


def test_delete_deploy_key_deletes_only_the_keys_of_that_title():
    keys = [{"id": 1, "title": "factory"}, {"id": 2, "title": "mine"}, {"title": "factory"}, {"id": 4}]
    gh, calls = scripted(json.dumps(keys), "", "")
    gh.delete_deploy_key(CHECKOUT_REPO, "factory")
    assert calls[1:] == [["api", "-X", "DELETE", "repos/pat/ws/keys/1"]]


def test_delete_deploy_key_with_no_keys_deletes_nothing():
    gh, calls = scripted("")
    gh.delete_deploy_key(CHECKOUT_REPO, "factory")
    assert len(calls) == 1


# --- the board -------------------------------------------------------------------------------------------


def _count(n: int) -> str:
    return json.dumps({"data": {"repository": {"projectsV2": {"totalCount": n}}}})


@pytest.mark.parametrize(("count", "expected"), [(0, False), (1, True)])
def test_board_linked_reads_the_projects_count(count, expected):
    gh, calls = scripted(_count(count))
    assert gh.board_linked(CHECKOUT_REPO) is expected
    assert calls[0][:3] == ["api", "graphql", "-f"] and "o=pat" in calls[0] and "n=ws" in calls[0]


@pytest.mark.parametrize("answer", ['{"data": null}', '{"errors": []}', "[]"])
def test_board_linked_names_an_answer_it_cannot_read(answer):
    gh, _ = scripted(answer)
    with pytest.raises(BootstrapError, match="unexpected answer to the board query"):
        gh.board_linked(CHECKOUT_REPO)


def test_create_board_makes_and_links_a_board_when_none_has_the_title():
    created = json.dumps({"number": 3, "url": "https://x/3"})
    gh, calls = scripted(json.dumps({"projects": [{"title": "other", "number": 1}]}), created, "")
    assert gh.create_board("pat", "ws", "backlog") == "https://x/3"
    assert calls[1][:2] == ["project", "create"]
    assert calls[2] == ["project", "link", "3", "--owner", "pat", "--repo", "pat/ws"]


@pytest.mark.parametrize("made", ['{"url": "https://x"}', "[]", ""])
def test_a_board_without_a_number_is_an_error_and_is_not_linked(made):
    gh, calls = scripted(json.dumps({"projects": []}), made)
    with pytest.raises(BootstrapError, match="without a number"):
        gh.create_board("pat", "ws", "backlog")
    assert len(calls) == 2


# --- the App ---------------------------------------------------------------------------------------------


def test_app_installations_lists_the_slugs():
    gh, calls = scripted('["a", "b"]')
    assert gh.app_installations("acme") == ["a", "b"]
    assert calls == [["api", "orgs/acme/installations", "--jq", "[.installations[].app_slug]"]]
    gh, _ = scripted("")
    assert gh.app_installations("acme") == []


@pytest.mark.parametrize("code", ["", "a b", "../x", "x" * 101])
def test_a_malformed_manifest_code_never_reaches_gh(code):
    gh, calls = scripted()
    with pytest.raises(BootstrapError, match="malformed manifest code"):
        gh.exchange_manifest_code(code)
    assert calls == []


def test_the_conversion_returns_the_id_slug_and_key_and_nothing_else():
    reply = json.dumps({"id": 42, "slug": "agent-factory-pat", "pem": "KEY", "client_secret": "s"})
    gh, calls = scripted(reply)
    assert gh.exchange_manifest_code("abc_1-2") == AppRegistration("42", "agent-factory-pat", "KEY")
    assert calls == [["api", "-X", "POST", "/app-manifests/abc_1-2/conversions"]]


@pytest.mark.parametrize("reply", ['{"id": 1, "slug": "s"}', "[]", ""])
def test_a_conversion_without_its_id_slug_or_key_is_an_error(reply):
    gh, _ = scripted(reply)
    with pytest.raises(BootstrapError, match="lacked its id, slug or key"):
        gh.exchange_manifest_code("abc")


class _App:
    """Stands in for `_github.GithubApp`: the installation token either arrives or fails with `error`."""

    error: _github.GithubError | None = None
    asked: list[tuple[str, str, str]] = []

    def __init__(self, app_id: str, pem: str) -> None:
        self.ids = (app_id, pem)

    def installation_token(self, repo: str) -> str:
        self.asked.append((*self.ids, repo))
        if self.error:
            raise self.error
        return "token"


@pytest.fixture
def fake_app(monkeypatch):
    app = type("App", (_App,), {"asked": []})
    monkeypatch.setattr(_github, "GithubApp", app)
    return app


REGISTRATION = AppRegistration("42", "agent-factory-pat", "KEY")


def test_the_app_is_installed_when_it_can_get_an_installation_token(fake_app):
    assert GhCli().app_installed(REGISTRATION, CHECKOUT_REPO) is True
    assert fake_app.asked == [("42", "KEY", "pat/ws")]


def test_the_app_is_not_installed_when_github_has_no_installation_for_it(fake_app):
    fake_app.error = _github.GithubError("not found", status=404)
    assert GhCli().app_installed(REGISTRATION, CHECKOUT_REPO) is False


def test_any_other_failure_to_get_a_token_is_an_error_not_a_no(fake_app):
    fake_app.error = _github.GithubError("bad credentials", status=401)
    with pytest.raises(BootstrapError, match="checking the App installation failed: bad credentials"):
        GhCli().app_installed(REGISTRATION, CHECKOUT_REPO)


def test_register_app_runs_the_flow_for_the_owner_kind_gh_reports(monkeypatch):
    seen: list[tuple[object, ...]] = []

    def register(*args: object) -> str:
        seen.append(args)
        return "the-code"

    monkeypatch.setattr(_bootstrap_github, "register_app", register)
    gh, _ = scripted("Organization")
    opened: list[str] = []
    gh._open_url = opened.append
    say = print
    assert gh.register_app("acme", "ws", say) == "the-code"
    assert seen == [("acme", "ws", True, opened.append, say)]


# --- the local server ------------------------------------------------------------------------------------


def test_a_redirect_without_a_code_is_refused_even_with_the_right_state():
    with pytest.raises(BootstrapError, match="no code"):
        _bootstrap_github.read_callback_code("s", "state=s")


def test_the_local_server_answers_an_unknown_path_with_404_and_takes_it_for_no_redirect():
    flow = _bootstrap_github.ManifestFlow("pat", "ws", False, state="s")
    statuses: list[int] = []

    def browser() -> None:
        try:
            urllib.request.urlopen(flow.start_url + "nope", timeout=5)
        except urllib.error.HTTPError as err:
            statuses.append(err.code)

    thread = threading.Thread(target=browser)
    thread.start()
    with pytest.raises(BootstrapError, match="no redirect from GitHub"):
        flow.wait(attempts=1)  # one request is served, and it is not the callback
    thread.join(5)
    assert statuses == [404] and flow.code is None


def test_wait_gives_up_with_a_retry_hint_when_no_redirect_arrives():
    flow = _bootstrap_github.ManifestFlow("pat", "ws", False)
    flow._server.timeout = 0.01
    with pytest.raises(BootstrapError, match="run the bootstrap again to retry"):
        flow.wait(attempts=2)


def test_register_app_announces_the_page_opens_it_and_returns_the_code():
    said: list[str] = []

    def browser(url: str) -> None:
        # The person's browser: load the page, then follow GitHub's redirect with the state the page carried.
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        state = page.split("state=")[1].split('"')[0]
        urllib.request.urlopen(f"{url}callback?code=the-code&state={state}", timeout=5).read()

    code = _bootstrap_github.register_app("pat", "ws", False, browser, said.append)
    assert code == "the-code"
    assert len(said) == 1 and said[0].startswith("Opening http://127.0.0.1:") and "GitHub App" in said[0]
