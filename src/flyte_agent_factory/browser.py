"""The browser the agent drives: a long-running Flyte app, not a per-call task.

Deployed once with `flyte serve`, then shared by every project. The agent talks
to it as a remote MCP server, so browsing is an ordinary tool call inside the
same Claude Code session rather than a separate Flyte run, and the browser
keeps its cookies between calls -- which is what makes a multi-step flow behind
a login work at all.

    flyte serve src/flyte_agent_factory/browser.py browser_env

Two paths on the one port the platform gives an app:

- `/mcp`     — the Playwright MCP endpoint. Point a client at
               `<endpoint>/mcp`.
- `/output/` — recordings, screenshots and PDFs the agent produced,
               directory-listable as JSON. This is how a video gets out of the
               browser container and into a run's outputs.
- `/view/`   — noVNC. Open the app's URL in a browser to watch the live
               session; `/` redirects here.

The personal app requires a bearer token on its MCP and output endpoints, in
addition to Union authentication on its public URL, and a one-time-ticket cookie
on `/view/`. The shared app trusts cluster-internal callers.

The browser runs in its own pod, which has two consequences worth knowing
before planning a demo: it cannot reach anything on the agent's localhost, and
it has no git credentials. To show it something the agent built, hand it the
content rather than a URL -- `browser_navigate` to a
`data:text/html;base64,<...>` URL renders a page the agent assembled in its own
workspace, which is the verified way across.

The browser runs with `--browser chromium` -- the default is the branded Chrome
channel, which the image does not contain -- and `--caps=devtools,pdf`, which offers
`browser_start_video` / `browser_stop_video` (with chapter cards and action
overlays), tracing, and `browser_pdf_save`. Without those caps the tools are
simply absent.

The profile is persistent, so a human can sign in once through `/view/` -- to
the Union console, or anything else behind a login -- and every later tool call
inherits that session. Only one client may use a persistent profile at a time,
so concurrent browser users need `--isolated` or their own profile directory.

Scaled `(1, 1)` rather than scale-to-zero: a browser that is torn down between
tool calls loses the session the agent was halfway through.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

import flyte
import flyte.app

# Keep runtime imports usable when deployment tools load this file by path.
if TYPE_CHECKING:
    from kubernetes.client import V1PodSpec

    from . import _names, _profile
    from ._images import browser_image
else:
    from flyte_agent_factory import _names, _profile
    from flyte_agent_factory._images import browser_image

APP_NAME = "claude-browser"
APP_PORT = 8080

browser_env = flyte.app.AppEnvironment(
    name=APP_NAME,
    image=browser_image(),
    command=["/bin/bash", "/opt/browser/start.sh"],
    port=APP_PORT,
    # Chromium is memory-hungry and the agent may drive several tabs at once;
    # shm matters because Chromium's default /dev/shm in a container is 64MB,
    # which crashes renderers on real pages.
    resources=flyte.Resources(cpu=(1, 4), memory=("2Gi", "8Gi"), disk="20Gi", shm="2Gi"),
    scaling=flyte.app.Scaling(replicas=(1, 1)),
    env_vars={
        "BROWSER_APP_PORT": str(APP_PORT),
        # Isolated profiles so several projects can drive the browser at once.
        # A persistent profile admits exactly one client and its lock can
        # outlive an unclean disconnect, which blocked a project from recording
        # with nothing else using the browser. Set to "0" to trade concurrency
        # back for inherited logins -- see start.sh.
        "BROWSER_ISOLATED": _names.getenv("AGENT_FACTORY_BROWSER_ISOLATED", "1"),
    },
    links=[
        flyte.app.Link(path="/view/vnc.html?autoconnect=1&resize=scale", title="Watch the browser"),
        flyte.app.Link(path="/output/", title="Recordings and screenshots"),
    ],
)


_PROFILE = _profile.current()
#: The deploy-time override of `[browser] exit_node`; an identity's own `exit_node` still wins over it.
_EXIT_NODE = _names.getenv("AGENT_FACTORY_BROWSER_EXIT_NODE", _PROFILE.browser.exit_node if _PROFILE else "")
#: Seconds from SIGTERM to SIGKILL: shutdown stops Chromium, saves the profile, logs out of Tailscale.
_PERSONAL_GRACE_S = 90


class _PersonalPodTemplate(flyte.PodTemplate):
    """The personal app's pod template, whose Kubernetes objects are built only when Flyte serializes it.

    This module is imported where `kubernetes` is not installed: by the deployer and by every slot pod
    (agent image). Flyte reads `pod_spec` only in its app serializer, which imports `kubernetes` itself
    first, so building the `V1PodSpec` on first read keeps that package a need of deploying the app alone.
    Flyte's app serializer requires the primary container to be named `app`.
    """

    def __init__(self) -> None:
        self._spec: V1PodSpec | None = None
        super().__init__(primary_container_name="app")

    @property
    def pod_spec(self) -> V1PodSpec:
        if self._spec is None:
            from kubernetes.client import V1Container, V1PodSpec

            self._spec = V1PodSpec(
                containers=[V1Container(name=self.primary_container_name)],
                termination_grace_period_seconds=_PERSONAL_GRACE_S,
            )
        return self._spec

    @pod_spec.setter
    def pod_spec(self, value: V1PodSpec | None) -> None:
        self._spec = value

    def __repr__(self) -> str:
        # The dataclass repr would read `pod_spec`, and so import `kubernetes`.
        return f"{type(self).__name__}(termination_grace_period_seconds={_PERSONAL_GRACE_S})"


def identity_exit_node(identity: _profile.BrowserIdentity) -> str:
    """Return the exit node `identity` browses through: its own, else the deploy's, else the profile's."""
    return _EXIT_NODE if identity.exit_node is None else identity.exit_node


def personal_app(
    profile: _profile.Profile, identity: _profile.BrowserIdentity, exit_node: str
) -> flyte.app.AppEnvironment:
    """Return the personal browser app of `identity`. Needs no `kubernetes`: see `_PersonalPodTemplate`."""
    return flyte.app.AppEnvironment(
        name=identity.app,
        pod_template=_PersonalPodTemplate(),
        image=browser_image(tailscale=bool(exit_node)),
        command=["/bin/bash", "/opt/browser/start.sh"],
        port=APP_PORT,
        resources=flyte.Resources(cpu=(1, 4), memory=("2Gi", "8Gi"), disk="20Gi", shm="2Gi"),
        scaling=flyte.app.Scaling(replicas=(1, 1)),
        env_vars={
            "BROWSER_APP_PORT": str(APP_PORT),
            "BROWSER_ISOLATED": "0",
            "BROWSER_PERSONAL": "1",
            "BROWSER_IDENTITY": identity.name,
            "BROWSER_PROFILE_URI": identity.profile_uri,
            "BROWSER_EXIT_NODE": exit_node,
            "BROWSER_TAILSCALE_TAG": profile.browser.tailscale_tag,
            _profile.TOML_ENV: profile.deployment_text(),
        },
        # Whatever the identity, the app reads its token as BROWSER_PERSONAL_TOKEN (start.sh, personal.py).
        secrets=[
            flyte.Secret(key=identity.token_secret, as_env_var="BROWSER_PERSONAL_TOKEN"),
            *(
                flyte.Secret(key=key, as_env_var=key.upper())
                for key in (
                    ("tailscale_oauth_client_id", "tailscale_oauth_client_secret") if exit_node else ()
                )
            ),
        ],
        # No link to /view/: it needs a one-time ticket, which comes from `factory.browser_link` (or
        # `view_link(identity)` in a session that is granted the identity).
        links=[flyte.app.Link(path="/health", title="Exit node health")],
    )


# Defined only for the identities the profile has: `flyte deploy --all` deploys every environment the
# module defines, and an unconditional one stood up a logged-in browser (and mounted its secrets) for
# profiles that never asked for it.
identity_apps: dict[str, flyte.app.AppEnvironment] = (
    {i.name: personal_app(_PROFILE, i, identity_exit_node(i)) for i in _PROFILE.browser.identities}
    if _PROFILE
    else {}
)
#: The legacy `personal` identity's app, for callers that predate named identities.
personal_browser_env = identity_apps.get(_profile.LEGACY_IDENTITY)


def view_link_from(endpoint: str, public_endpoint: str, token: str) -> str:
    """Return a one-time link (valid ten minutes) to the live view of the browser app at `endpoint`.

    Asks the app for a ticket with the bearer `token`; the link is on `public_endpoint`, which is where a
    person opens it. Opening the link while signed in to Union sets a short-lived cookie for `/view/`.
    """
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/view-ticket", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        ticket = response.read().decode()
    return f"{public_endpoint.rstrip('/')}/view-login?ticket={ticket}"


def view_link_for(identity: str) -> str:
    """Return a one-time view link for `identity`, from a pod that has its token mounted (the factory's).

    Raises RuntimeError for an identity the profile lacks or whose token is not in this pod's environment.
    """
    token = os.environ.get(_profile.BrowserIdentity(identity, "").token_env, "")
    if not token:
        raise RuntimeError(f"the token of browser identity {identity!r} is not mounted in this pod")
    return view_link_from(identity_endpoint(identity), identity_public_endpoint(identity), token)


def view_link(identity: str = "") -> str:
    """Return a one-time link that opens `identity`'s live view (valid ten minutes), through the gate.

    For a session: it holds no token, so the supervisor's gate asks the app. `identity` may be left out when
    the project is granted exactly one.
    """
    gate = os.environ.get(_GATE_ENV, "")
    granted = os.environ.get(_GRANT_ENV, "").replace(",", " ").split()
    if not gate:
        raise RuntimeError("this project has no personal browser grant")
    if not identity:
        if len(granted) != 1:
            raise RuntimeError(f"name the identity: this project is granted {', '.join(granted) or 'none'}")
        identity = granted[0]
    try:
        with urllib.request.urlopen(f"{gate}/{identity}/view-link", timeout=20) as response:
            return cast(str, json.load(response)["url"])
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"the gate refused a view link for {identity!r} (HTTP {e.code})") from e


# Mirrored from `_browser_gate`, which imports this module.
_GATE_ENV = "AGENT_FACTORY_BROWSER_GATE"
_GRANT_ENV = "AGENT_FACTORY_BROWSER_IDENTITIES"


def _public_endpoint(app: flyte.app.AppEnvironment) -> str:
    """Return `app`'s public URL, initialising Flyte from the pod's credentials if it has no client yet.

    A fresh process in a slot pod (the supervisor's `python -c`) has none, so the lookup raises first.
    """
    from flyte.errors import InitializationError

    try:
        return app.endpoint
    except InitializationError:
        flyte.init_in_cluster()
        return app.endpoint


def identity_endpoint(name: str) -> str:
    """Return the URL of identity `name`'s browser: in-cluster in a pod, else the deployed app's endpoint.

    Raises RuntimeError when the profile has no such identity.
    """
    app = identity_apps.get(name)
    if app is None:
        raise RuntimeError(f"The profile has no browser identity {name!r}")
    try:
        namespace = Path(_NAMESPACE_FILE).read_text().strip()
    except OSError:
        namespace = ""
    if namespace:
        return f"http://{app.name}.{namespace}.svc.cluster.local:{_SERVICE_PORT}"
    return app.endpoint


def identity_public_endpoint(name: str) -> str:
    """Return the public URL of identity `name`'s browser. Raises RuntimeError for an unknown identity."""
    app = identity_apps.get(name)
    if app is None:
        raise RuntimeError(f"The profile has no browser identity {name!r}")
    return _public_endpoint(app)


def _only_identity_name() -> str:
    """Return the `personal` identity, else the only one; the single browser the older callers mean."""
    if _profile.LEGACY_IDENTITY in identity_apps:
        return _profile.LEGACY_IDENTITY
    if len(identity_apps) == 1:
        return next(iter(identity_apps))
    raise RuntimeError("The personal browser is not enabled in this profile")


def personal_endpoint() -> str:
    """Return the URL of the `personal` identity (else the only one) as `identity_endpoint` does.

    Raises RuntimeError when the profile has no such browser.
    """
    return identity_endpoint(_only_identity_name())


def personal_guidance(identities: Sequence[str]) -> str:
    """Return the standing instructions for a project granted the browser `identities` (names), else ""."""
    if not identities:
        return ""
    servers = ", ".join(f"`browser-{name}`" for name in identities)
    first = identities[0]
    return (
        f"\nPersonal browsers ({servers}): each is a browser signed in as the human, and everything you "
        "do in it acts as them. Use one only for tasks they asked for, at a human pace and read-mostly. "
        "Never bulk scrape, mass message or send connection requests. "
        "Never post, comment, like, message, submit or change anything without explicit human approval. "
        "LinkedIn and Instagram prohibit automation and may restrict the account. "
        "Reading pages, navigating and clicking links needs no approval. "
        "Anything else (clicking a button, typing, filling a form, pressing keys, running script) is refused "
        "by a gate until the human approves it: run "
        f'`ask-human --approve --browser {first} "<what you will do, and where>"`, wait for `approved`, '
        "then retry; the approval covers that browser for ten minutes. On pages inside the project's own "
        "sandbox repository on GitHub, the gate asks for no approval. "
        "When the human needs to sign in or watch, run `python -c 'from flyte_agent_factory.browser import "
        f'view_link; print(view_link("{first}"))\'` and give them '
        "the one-time link it prints (valid ten minutes; they open it while signed in to Union).\n"
    )


class McpServer(TypedDict):
    """One server entry in a `.mcp.json` file."""

    type: str
    url: str
    headers: NotRequired[dict[str, str]]


class McpConfig(TypedDict):
    """The `.mcp.json` document: servers by name."""

    mcpServers: dict[str, McpServer]


def mcp_config(endpoint: str, headers: dict[str, str] | None = None) -> McpConfig:
    """Return the `.mcp.json` fragment that points a Claude Code session at a browser.

    `headers` carries what the transport needs rather than what the server
    authenticates with -- there is no authentication here. The in-pod browser
    needs an `Origin`, because playwright-mcp validates it as DNS-rebinding
    protection and 403s anything it does not recognise. The shared app does
    not, because its nginx front door sets that header itself.
    """
    server: McpServer = {"type": "http", "url": f"{endpoint.rstrip('/')}/mcp"}
    if headers:
        server["headers"] = headers
    return {"mcpServers": {"browser": server}}


def identity_exit_ready(name: str, token: str) -> bool:
    """Whether identity `name` may be offered: always without an exit node, else only while it is up.

    The app already refuses traffic when its exit node is down; this keeps a Codex author from being
    handed a browser entry that can only fail, and from learning that mid-task. `token` is the identity's
    bearer token, read by the caller that holds it (the supervisor).
    """
    identity = _PROFILE.browser.identity(name) if _PROFILE else None
    if not (identity_exit_node(identity) if identity else _EXIT_NODE):
        return True
    try:
        request = urllib.request.Request(
            f"{identity_endpoint(name)}/health", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            # The health endpoint returns a JSON object with a string exit_node status.
            return cast(dict[str, str], json.load(response)).get("exit_node") == "up"
    # A 503 (HTTPError), a timeout and a non-JSON body all mean "not up", and so does an app that
    # cannot be located here (identity_endpoint raises RuntimeError outside a cluster namespace).
    except (OSError, ValueError, RuntimeError):
        return False


_ENV_BEARER = re.compile(r"Bearer \$\{(\w+)\}")


def codex_mcp_config(servers: dict[str, McpServer]) -> str:
    """Return the `config.toml` text that gives Codex the `.mcp.json` servers `mcp_config` describes.

    Rendered from the same dicts so the two engines cannot disagree about endpoint or headers.
    Codex has no `${VAR}` expansion in headers; its equivalent of the Authorization entry is
    `bearer_token_env_var`, which likewise keeps the secret out of the file. Written by hand because
    the shape is two string keys and one inline table, and TOML basic strings are JSON strings.
    """
    lines: list[str] = []
    for name, server in servers.items():
        lines += [f"[mcp_servers.{name}]", f"url = {json.dumps(server['url'])}"]
        headers = dict(server.get("headers", {}))
        match = _ENV_BEARER.fullmatch(headers.get("Authorization", ""))
        if match:
            del headers["Authorization"]
            lines.append(f"bearer_token_env_var = {json.dumps(match.group(1))}")
        if headers:
            pairs = ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in headers.items())
            lines.append(f"http_headers = {{ {pairs} }}")
        lines.append("")
    return "\n".join(lines)


OUTPUT_PATH = "/output/"

# Where a pod learns its own namespace. The app and the agent task share one, so
# this is the namespace to address the app's Service in.
_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

# The app's Service listens on 80, which is not `APP_PORT`: the platform's proxy
# terminates on 80 and forwards to the container's port. Addressing the pod port
# directly connects to nothing.
_SERVICE_PORT = 80


def browser_endpoint() -> str:
    """Resolve the browser app's address from inside the cluster.

    `AppEnvironment.endpoint` is documented to give the cluster-internal URL in
    a container and the public one elsewhere, but it decides that from
    `INTERNAL_APP_ENDPOINT_PATTERN`, which task pods do not set. In a task it
    therefore hands back the *public* URL, and a request to that is answered by
    the platform's auth proxy with a 302 to a login page -- an MCP client
    pointed there sees a redirect to HTML, not a server.

    So address the app's Kubernetes Service directly. The agent task and the app
    run in the same namespace, which the pod can read rather than infer from a
    `<project>-<domain>` convention.
    """
    try:
        namespace = Path(_NAMESPACE_FILE).read_text().strip()
    except OSError:
        # Not in a pod (a local probe, a test): the public URL is the only one
        # that resolves, and platform auth is then the caller's problem.
        return browser_env.endpoint
    if not namespace:
        return browser_env.endpoint
    return f"http://{APP_NAME}.{namespace}.svc.cluster.local:{_SERVICE_PORT}"


if __name__ == "__main__":
    flyte.init_from_config()
    app = flyte.serve(browser_env)
    print(f"browser app: {app.url}")
    print(f"  MCP   : {app.url.rstrip('/')}/mcp")
    print(f"  output: {app.url.rstrip('/')}/output/   (recordings, screenshots, PDFs)")
    print(f"  watch : {app.url.rstrip('/')}/view/vnc.html?autoconnect=1&resize=scale")
    print()
    print("Sign in to anything the agent needs (e.g. the Union console) via the watch URL;")
    print("the browser profile is persistent, so tool calls inherit that session.")
