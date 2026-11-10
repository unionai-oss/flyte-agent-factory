"""The browser app's address, as seen from the agent's pod."""

from __future__ import annotations

from flyte_agent_factory import _instructions, _session_setup, browser
from flyte_agent_factory.browser import APP_NAME, browser_endpoint, mcp_config


def test_endpoint_addresses_the_service_in_the_pods_own_namespace(tmp_path, monkeypatch):
    """A task pod must not be handed the public URL.

    `AppEnvironment.endpoint` decides internal-vs-public from
    `INTERNAL_APP_ENDPOINT_PATTERN`, which task pods do not set, so it returns
    the public URL -- where the platform's auth proxy answers with a 302 to a
    login page. An MCP client pointed there sees a redirect to HTML. The
    namespace comes from the pod rather than a `<project>-<domain>` guess.
    """
    namespace_file = tmp_path / "namespace"
    namespace_file.write_text("proj-work-development\n")
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", str(namespace_file))

    assert browser_endpoint() == f"http://{APP_NAME}.proj-work-development.svc.cluster.local:80"


def test_endpoint_falls_back_off_cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", str(tmp_path / "absent"))
    sentinel = "https://cool-salad-0f2a1.apps.union.example.org"

    class _Env:
        endpoint = sentinel

    monkeypatch.setattr(browser, "browser_env", _Env)
    assert browser_endpoint() == sentinel


def test_endpoint_ignores_an_empty_namespace_file(tmp_path, monkeypatch):
    # A projected volume that exists but has not been populated would otherwise
    # produce `http://claude-browser..svc.cluster.local`, which resolves to
    # nothing and fails as a timeout rather than as a clear error.
    (tmp_path / "namespace").write_text("\n")
    monkeypatch.setattr(browser, "_NAMESPACE_FILE", str(tmp_path / "namespace"))

    class _Env:
        endpoint = "https://public.example"

    monkeypatch.setattr(browser, "browser_env", _Env)
    assert browser_endpoint() == "https://public.example"


def test_mcp_config_points_at_the_mcp_path():
    config = mcp_config("http://claude-browser.ns.svc.cluster.local:80")
    server = config["mcpServers"]["browser"]
    assert server == {"type": "http", "url": "http://claude-browser.ns.svc.cluster.local:80/mcp"}


def test_memory_leads_with_localhost_working():
    """In-pod is the normal case, and it changes what the agent should reach for.

    Sharing a network namespace with the browser means a served page is
    reachable, which is strictly better than base64-encoding a file into a
    `data:` URL -- the workaround the cross-pod app forced. The memory used to
    present that workaround as "the verified way across", full stop.
    """

    memory = _instructions._project_memory(browser_enabled=True)

    assert "localhost" in memory
    assert "in this pod" in memory
    # The data-URL fallback is still documented, but as the app's constraint.
    assert "data:text/html;base64" in memory


def test_memory_still_warns_about_the_shared_app_being_one_seat():
    """The fallback path has a failure mode worth recognising.

    Playwright refuses concurrent use of a persistent profile, so a probe that
    ran while a project held the browser saw `Browser is already in use` for
    navigate, snapshot, video start and video stop alike. Read as a broken
    browser, an agent retries instead of reporting.
    """

    memory = _instructions._project_memory(browser_enabled=True)

    assert "single seat" in memory.lower()
    assert "already in use" in memory
    assert "do not retry in a loop" in memory.lower()

    # And none of it appears when there is no browser at all.
    assert "already in use" not in _instructions._project_memory(browser_enabled=False)


def test_the_in_pod_browser_is_addressed_as_localhost_not_127_0_0_1():
    """playwright-mcp 403s a Host it does not recognise, `127.0.0.1` included.

    It binds 127.0.0.1 so nothing outside the pod can reach it, but it has to
    be *addressed* as localhost. The shared app hides this because nginx
    rewrites Host and Origin on the way through; in-pod there is no nginx, and
    a pre-flight check against 127.0.0.1 cost a run to diagnose.
    """
    from flyte_agent_factory._local_browser import MCP_HOST, MCP_PORT, LocalBrowser

    assert MCP_HOST == "localhost"
    browser = LocalBrowser(
        profile_dir=__import__("pathlib").Path("/tmp/p"), output_dir=__import__("pathlib").Path("/tmp/o")
    )
    assert browser.endpoint == f"http://localhost:{MCP_PORT}"
    assert "127.0.0.1" not in browser.endpoint


def test_a_client_is_told_to_send_an_origin():
    """urllib sends no Origin, and Claude Code needs it configured explicitly."""
    import pathlib

    from flyte_agent_factory._local_browser import MCP_PORT, LocalBrowser
    from flyte_agent_factory.browser import mcp_config

    browser = LocalBrowser(profile_dir=pathlib.Path("/tmp/p"), output_dir=pathlib.Path("/tmp/o"))
    assert browser.client_headers == {"Origin": f"http://localhost:{MCP_PORT}"}

    server = mcp_config(browser.endpoint, browser.client_headers)["mcpServers"]["browser"]
    assert server["headers"]["Origin"] == f"http://localhost:{MCP_PORT}"


def test_the_shared_app_needs_no_headers():
    """Its nginx front door sets Host and Origin itself."""
    from flyte_agent_factory.browser import mcp_config

    server = mcp_config("http://claude-browser.ns.svc.cluster.local:80")["mcpServers"]["browser"]
    assert "headers" not in server


def test_the_session_is_given_its_mcp_config_explicitly():
    """A project-level `.mcp.json` is offered, not loaded.

    Claude Code asks "Use this MCP server?" with "Continue without using this
    MCP server" highlighted, and nobody is at the terminal to answer. Sessions
    ran with a browser started, healthy and advertised in their memory, and no
    `browser_*` tools at all -- which is far more confusing than having no
    browser. Two projects lost hours to it.
    """
    import inspect

    from flyte_agent_factory import agent

    source = inspect.getsource(agent.project.func)
    assert '"--mcp-config"' in source
    assert '"--strict-mcp-config"' in source
    # Only when there is a browser: the flag with no server would strip the
    # session of any MCP configuration it might otherwise have.
    assert "if browser:" in source


def test_writing_the_config_reports_where_it_went():
    """The path has to reach the command line, so it cannot be implicit."""
    import inspect

    assert inspect.signature(_session_setup._write_mcp_config).return_annotation == "Path"


def test_the_console_snippet_uses_esm_imports():
    """`browser_run_code_unsafe` runs as an ES module; `require` is undefined.

    The snippet handed sessions CommonJS, and one reported -- reasonably --
    that its browser sandbox had no filesystem access, then stopped rather than
    pasting the token inline. It was right to refuse; the snippet was wrong.
    """
    import pathlib

    note = _instructions._console_auth_note(pathlib.Path("/home/flyte/.console-token"))
    assert "await import('node:fs')" in note
    assert "require(" not in note
    assert "never paste it into the snippet" in note
