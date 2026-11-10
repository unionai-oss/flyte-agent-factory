# Personal browser

A personal browser is a single-seat, logged-in Chromium, separate from the shared project browser. A
session reaches it only through the **grant gate** in its supervisor, and only if its project was granted
that browser. This page covers the design, the owner's setup, and the live checks.

## Identities

The profile names one **identity** per signed-in account (`github`, `linkedin`, ...). Each is its own app
`claude-browser-<name>`, with its own token secret `browser_<name>_token` (dashes in the name become
underscores) and its own profile archive. Every slot and the factory mount every identity's token as the
environment variable `BROWSER_<NAME>_TOKEN`; the supervisor reads it, and it is removed from the session's
environment. The earlier shape, `[browser] personal = true` with `profile_uri`, is exactly one identity named
`personal`: the same app `claude-browser-personal`, the same secret `browser_personal_token` and the same
archive. Migrating to named identities needs no wipe and no new sign-in. `personal_env` is accepted and
ignored: it no longer picks a slot.

## Grants

Which projects may use which identity is decided per project, never by the profile:

- `factory.launch(browser="github,linkedin")` grants the named identities. A resume (launching the same name
  again) inherits the project's grant. `browser="none"` removes it; leaving the argument out keeps it.
- A GitHub issue picked up by the intake carries the grant as labels `browser:<identity>`. A label naming an
  identity the profile lacks grants nothing, and the intake says so. The labels are read as they stand when a
  maintainer's intake label starts the project, whoever added them, and anyone with triage rights on the repo
  can add a label: a maintainer applying the intake label vouches for the `browser:` labels on the issue.
- The grant reaches the run as `AGENT_FACTORY_BROWSER_IDENTITIES`. No grant, no gate and no entry: the
  project's `.mcp.json` has no personal browser and the gate answers nothing for it.

## The gate

A personal browser acts as its owner, so the session never holds its bearer token. The supervisor runs the
gate on `127.0.0.1` (an ephemeral port, announced to the session as `AGENT_FACTORY_BROWSER_GATE`) inside its
own non-dumpable process. For each granted identity the session's MCP config has an entry
`browser-<identity>` whose URL is `http://127.0.0.1:<port>/<identity>/mcp` and which carries no header. The
gate adds `Authorization: Bearer <token>` from the supervisor's environment as it relays to the identity's
app; nothing writes the token anywhere, and `.mcp.json` is left out of the workspace uploaded to review and
planner sub-agents. Any other path or identity is answered 403 and never reaches an app, and so is a request
that carries an `Origin` header or a `Host` other than the loopback one (a web page in a browser on the pod).

Union authentication protects the public app URL; the app's `/mcp`, `/output/`, `/exit-check` and `/health`
also require `Authorization: Bearer <token>`, including inside the cluster. The pinned Playwright MCP 0.0.83
exposes `browser_run_code_unsafe` as a core tool and `--caps` cannot disable it, so the gate is what stands
between a session and arbitrary code in the owner's browser.

## Approvals

The gate lets **reading** through: navigating to an `http(s)` URL (or `about:blank`), snapshots, screenshots,
waiting, tab listing and the recording and debugging aids, and **clicks on links**. It knows a link from what
the app reported in the last snapshot of that MCP session (a page that prints a forged snapshot to the console
is not believed). Everything that can act as the owner is answered by the gate itself with an error telling
the session to ask: clicking anything but a link, typing, filling forms, pressing keys, selecting, dragging,
uploads, dialogs, `browser_evaluate`, `browser_run_code_unsafe`, and any tool the gate does not know. So is
navigating or opening a tab to a URL that is not `http(s)`: `javascript:` in the address bar runs script in
the owner's page.

The session runs `ask-human --approve --browser <identity> "<what you will do, and where>"`. The owner's
prompt names the identity and the page the gate last saw. On yes, the supervisor opens a ten-minute window
for that identity in the gate (an in-process call; no HTTP route opens one), and the command prints
`approved`. A no, a timeout or a withdrawn question opens nothing. A batch of JSON-RPC calls with one held-back
call is refused whole. `--browser` takes an identity the project is granted; any other is an error answer.

Residual risks, accepted: a link whose own handler runs script (the click passes as a link); a process with
the supervisor's uid, or someone with access to the Union API, can approve or read the token as the owner can.

### The sandbox exemption

While the page is inside the project's sandbox repository on GitHub (`[repo] project`, ssh or https URL, any
case; exactly that `owner/repo` on `https://github.com`), gated calls need no approval, so a project can
work on its own repo's pages. The gate takes the page from the URL the app reported in that MCP session; a
lookalike (`owner/repo-other`, another host, a userinfo URL, plain http) is not exempt. The exemption does not
cover `browser_evaluate` and `browser_run_code_unsafe` (code can drive the browser anywhere, e.g.
`github.com/settings`), nor a URL that is not `http(s)`: those always need an approval. File upload and drop
stay exempt, because they hand data to an element of the current page and cannot leave it.

## View links

To sign in the first time (from your phone, say), ask in chat for a link: `factory.browser_link` with the
identity returns `{identity, url}`, a one-time URL (ten minutes, single use) that needs no project. In a
session that is granted the identity, `view_link("<identity>")` from `flyte_agent_factory.browser` (the
identity may be left out when exactly one is granted) asks the gate, which asks the app with the token; the
session never sees the token. Open the link in a browser that is signed in to Union (the platform still
guards the public URL).

The live view (`/view/`, including the `/view/websockify` websocket) is gated by nginx too. Whether the Union
app proxy forwards an authenticated-user identity that the pod can trust is not documented, so the gate does
not rely on one: `/view/` needs either the bearer token or a short-lived signed cookie (HttpOnly, Secure,
SameSite=Lax, one hour). The cookie comes from `/view-login?ticket=...`, and a ticket (ten minutes, single
use) comes only from `/view-ticket`, which needs the bearer token. Ten minutes because the link is relayed
through chat, which can take over a minute. A GET of `/view-login` only shows a small page (no-store, no
referrer, a strict CSP) whose form spends the ticket by POST, submitted at once by its script or by its
button; so a chat app's link-preview fetch, which does not run scripts, leaves the ticket unused. A bad, used
or expired ticket gets a 401 HTML page saying the link has expired and to ask for a new one. websockify
listens on loopback (it used to listen on every interface, reachable by any pod around nginx), x11vnc has a
random per-boot password in a 0600 file, and `/view-login` hands that password to noVNC in the redirect its
POST issues after the ticket check, in the URL fragment (never sent to a server) rather than the query.
`/view/` is served with a Content-Security-Policy that admits only the app's own files, because the platform's
edge injects third-party analytics into pages and those read the page URL. Residual risk: any process in the
pod that can read the x11vnc password file or loopback ports acts as the human, and `browser_run_code_unsafe`
already allows that to the bearer holder.

## Profile

Add to the deployment profile:

```toml
[browser]
exit_node = "home-router"              # default exit node for every identity
tailscale_tag = "tag:factory-browser"  # optional, default tag:k8s

[browser.identities.github]
profile_uri = "s3://YOUR-PROJECT-PREFIX/browser-github/profile.tar.gz"
# exit_node = "other-node"   # optional override for this identity; "" means direct egress
```

An identity name matches `^[a-z][a-z0-9-]{0,19}$`, needs a `profile_uri`, and two identities may not share one.
The earlier shape still works and is exactly one identity named `personal`:
`[browser] personal = true` with `profile_uri` (and `exit_node`). Do not combine it with
`[browser.identities.personal]`. `personal_env` is deprecated: it is accepted and ignored, because it no longer
grants anything.

Choose a URI inside this project's own storage prefix, distinct for each deployment
profile. The app's workload identity needs read/write access there. S3 and GCS
are supported through fsspec using the workload's credentials. No Volume is used.
The archive excludes Chromium caches and host-specific locks; it is restored
before the browser starts, saved every ten minutes, and saved on SIGTERM.
A forced kill can lose the last ten minutes. Periodic archives of an active
Chromium profile are best-effort; a graceful shutdown is the reliable final copy.
The app scales to exactly one replica. A conditional-write lease object next to
the archive admits only one profile owner across overlapping deployments. Owners
renew every 30 seconds and before publishing snapshots; expiry is 120 seconds.
A replacement waits at most 180 seconds for ownership before failing startup.
Lease loss stops Chromium and suppresses snapshots. S3/GCS conditional writes
come from obstore (fsspec exists/write cannot arbitrate ownership). A corrupt lease
fails startup closed. Flyte AppEnvironment exposes no rollout strategy and scaling
alone does not prevent rollout overlap, so the deployer always stops the old app
before it starts the new one (below). The pod has an explicit 90-second termination grace. Shutdown
stops Chromium's process group within five seconds, saves the profile, then logs
out of Tailscale within five seconds. Forced termination can still lose data.
Two immutable archive generations are referenced by the lease object. Publishing
an archive conditionally updates that pointer, so even a delayed write from an
expired owner cannot overwrite the next owner's snapshot. Unreferenced older
generations are removed after publication. Restore validates in a staging
directory, tries the previous generation on corruption, then uses an empty
profile if both are corrupt. Only a successful read of a bad archive counts as corruption.
Storage errors (resets, timeouts, 5xx) are retried with backoff for two minutes; if storage is
still unreachable the app exits without starting Chromium or saving anything, and the platform
restarts it. After an all-corrupt start the unreadable generations stay referenced by the lease
(they are never dropped for a snapshot of a profile that did not come from them); the next
start that restores a snapshot returns to keeping two generations. Existing archives at the configured URI and its `.prev`
sibling are read during migration. Only error classes are logged.

### Egress per identity

Each identity browses through its own exit node (`exit_node` on the identity; else `[browser] exit_node`; `""`
is direct datacenter egress). Egress fails closed, as described below, and that is unchanged by grants. The
app's Tailscale node is tagged `[browser] tailscale_tag` (default `tag:k8s`). To stop depending on the interim
ACL grant that lets `tag:k8s` reach `autogroup:internet`, give the browsers a tag of their own: set
`tailscale_tag = "tag:factory-browser"`, make the OAuth client own that tag (and `tagOwners` allow
`tag:k8s-operator` to own it), add a grant from `tag:factory-browser` to `autogroup:internet` (the exit node's
routes), push, and once the browsers have redeployed under the new tag and still reach the internet, remove the
`tag:k8s` to `autogroup:internet` grant.

Enable an exit node on a home device (for example a GL.iNet router with Tailscale,
or a supported Android device with “Run as exit node”), and approve its exit-node
routes in the Tailscale admin console. Device support varies; check the device's
Tailscale app for that option. Grant the browser's tagged identity permission to
use the exit node. The OAuth client must carry `tag:k8s-operator`, and tag ownership
must include `"tag:k8s": ["tag:k8s-operator"]`. Grant auth-key write permissions.
Create Flyte secrets `tailscale_oauth_client_id` and
`tailscale_oauth_client_secret` in this project's domain. At startup the app exchanges
these for an OAuth token and a nonreusable, ephemeral, preauthorized key tagged `[browser] tailscale_tag` (default `tag:k8s`, which the OAuth
client must own).
Neither token nor key is logged. Logout on shutdown removes the ephemeral node;
an abrupt kill relies on Tailscale's ephemeral-node expiry.

`AGENT_FACTORY_BROWSER_EXIT_NODE` overrides `[browser] exit_node` when deploying the apps; an identity that
sets its own `exit_node` keeps it.
An empty value uses direct datacenter egress. A configured but offline exit node
blocks MCP/output requests and Chromium traffic. Tailscaled's own SOCKS proxy
can egress directly when logged out, so Chromium instead connects to a local
forwarder that refuses traffic unless status says Running with an online exit
node. Status is cached for at most two seconds; open tunnels are rechecked too.
The egress-IP lookup is independently cached for 60 seconds. Tailscaled uses a
private 0700 socket directory and disables support logging. A crashed daemon is
restarted with bounded exponential backoff. OAuth credentials are removed from
every child environment before launching browser, desktop, or network processes.
The image pins Tailscale 1.82.5 from the official static distribution at
https://pkgs.tailscale.com/stable/#static.

Create an identity's token with `python ops/create_personal_browser_token.py --identity github` (the default
identity is `personal`; pass the usual Flyte CLI project/domain/config options if needed). The helper
creates 32 random bytes, writes a private temporary file and never prints the
token. To rotate it, remove the identity from the profile and push (the deployer stops the app),
replace `browser_<name>_token` with the same helper (delete the existing Flyte secret first
if the backend rejects replacement), then add the identity back and push: the deployer
redeploys the stopped app and the slot, so both mount the new value. Projects already running
keep their old environment until they end; new projects get the new one.

The owner only creates the secrets and enables the exit node. Exactly these secrets: `browser_<name>_token`,
once per new identity (`ops/create_personal_browser_token.py --identity <name>`), and the Tailscale pair
`tailscale_oauth_client_id` and `tailscale_oauth_client_secret` if they do not exist yet. The legacy `personal`
identity needs nothing new: its secret exists already. The deployer does the rest: for each identity in the profile, the
`factory-deployer` deploys its app, and updates it, on its next tick after a merge, together with the slots
that mount the tokens. An app is defined only for an identity the profile has, so no other profile gets one. There is nothing to deploy by hand.

Removing an identity from the profile (or dropping the `[browser]` table) stops the app on the deployer's
next tick, whatever state it is in (running, starting or failed). Any deployed `claude-browser-<x>` app that is
neither the shared `claude-browser` nor an identity of the profile is stopped; the shared browser is never
touched. It is deactivated, not deleted: the profile archive and its `.prev` and `.lease` siblings stay in
the bucket, so enabling it again later brings the same profile back. To wipe the archive, follow the steps
below.

An update replaces the app stop-then-start, not as a rolling update: the running pod keeps renewing its
lease on the profile archive and a new pod waits at most 180 seconds for it, so a new revision started
beside the old one could never become ready. The browser is therefore unavailable for a short time while it
is replaced. The deployer builds the new image first, so a failed build leaves the running app alone, and if
the new revision does not become active within 15 minutes it restores the previous spec and fails the
deploy, naming the app's own condition messages. An app whose spec has not changed is left running.

If a secret the profile needs does not exist yet, the deployer deploys nothing, and does not count it
as a failed attempt: its run report and its tick message name each missing secret and the environment
that needs it. Create it and the next tick deploys.

Flyte needs the `kubernetes` package to serialize the app's pod template (its 90-second termination
grace). The `factory-deployer` image carries it for that reason; the agent image (the slot pods, and the
base of the deployer's) does not, and nothing else needs it: `browser.py` builds that pod spec only when
Flyte serializes the app, so the slot pods import it and define `identity_apps` without the package.
A deployer from before this was added lacks `kubernetes`: its apps stage fails with a message saying so,
after the slots and the factory (including the new deployer) are deployed, and the new deployer deploys the
app on its next tick.

Every slot is redeployed with the rest of the slots on the next merge, which is what mounts the tokens
there. Before deploying anything, the deployer's check
(`python -m flyte_agent_factory._deploy_child check`) imports every
environment the profile plans, as each deploy child would, and for a profile with the personal
browser also defines every identity's app; if any of that fails, nothing is deployed.

To sign in, ask in chat for a link with `factory.browser_link` for the identity. Open it on your phone while
signed in to Union, sign in to LinkedIn/Instagram in the remote browser (including 2FA), then close the tab.
(Playwright opens Chromium lazily on its first tool call, so the page shows the desktop once the app is up.)
The browser stays running; a project granted the identity reuses that profile.
Only browse for tasks the human requested, at a human pace, read-mostly. No bulk
scraping, mass messaging or connection requests. Posting, commenting, liking or
messaging requires explicit human approval in that conversation. LinkedIn and
Instagram prohibit automation and can restrict accounts.

### Union and GKE

Secrets reach every pod through Flyte on both, so the steps above are the same. On GKE, give `profile_uri` a
`gs://` URI and let the app's workload identity read and write it. In a cluster the supervisor reaches an
identity's app by its service URL (`http://claude-browser-<name>.<namespace>.svc.cluster.local`), not through
the public URL. On Union the app's ingress authentication is Union's; on an OSS deployment you have to
provide it in front of the app (the bearer token still protects `/mcp`, `/health` and `/output/`, but not the
page itself).

### Live verification

Run these after a deploy, with two projects of your own running at once, one granted and one not. The
examples say `github`; on a profile still in the legacy shape the identity is `personal` (its app is
`claude-browser-personal`, and it keeps the sign-in it already has).

1. Request `/health` with the bearer token through the authenticated app URL. It reports `exit_node: up`
   and `egress_ip` from https://api.ipify.org, fetched via SOCKS5 with remote DNS.
   Compare this to the home device's public IP.
2. Sign in through the phone view (`factory.browser_link`). Start both projects at once, and in each ask for
   a page that needs the login (github.com/notifications). Only the granted project loads a signed-in page.
   The other has no gate at all: its `.mcp.json` has no `browser-*` entry other than the shared `browser`,
   `AGENT_FACTORY_BROWSER_GATE` is unset in its session, and its session environment holds no
   `BROWSER_*_TOKEN`. (A 403 from a gate is what a project granted some *other* identity gets for this one.)
3. In the granted project, ask it to click a button outside the sandbox repo: it is refused with the
   `ask-human --approve --browser github` instruction; approve it on your phone and the retry works; ten
   minutes later it is refused again. Clicking a link needs no approval.
4. Stop the home exit node. Confirm `/health` reports `down` with HTTP 503 and new MCP calls and output
   downloads fail. Test an already-open tab and background requests too: no datacenter egress may appear
   during NeedsLogin, Stopped, offline exit-node, status errors or daemon restart. To observe that, ask the
   granted project to navigate to https://api.ipify.org while the node is down (the call must fail, not show
   an address) and compare any address that does appear with the cluster's NAT egress address (what
   `curl https://api.ipify.org` prints from a slot pod): it must never be that one. Restore the exit node and
   confirm recovery and home IP.
5. Redeploy (merge a change and let the deployer replace the app, or restart the single replica gracefully).
   Confirm the login survives: the granted project is still signed in, with no new sign-in.
   Test an abrupt restart separately; inspect snapshot age without exposing cookies.
6. Confirm shutting down the app removes its ephemeral device from Tailscale.

The archive contains cookies and can impersonate the human: anyone with access
to that object can act as the logged-in user. Restrict bucket IAM and enable
storage encryption. Do not download it into project workspaces or logs. To wipe,
stop the app, delete the archive objects referenced in the `.lease` sibling,
the configured object and its `.prev`/`.lease` siblings, any orphan `.pending-*`
uploads, and the app's local profile directory
(or replace its pod), then restart. Revoke site sessions from account security
settings as well if access might have been compromised.

Startup failures include missing storage permissions, invalid OAuth credentials,
unapproved exit-node routes and an active Chromium lock. A lock belonging to a
live process or another host is never removed automatically. Stop the owner
before restarting. A dead local owner's Singleton locks are cleared safely.

Implementation references: [Tailscale userspace networking](https://tailscale.com/docs/concepts/userspace-networking)
and [OAuth clients](https://tailscale.com/docs/features/oauth-clients).

## Recovery, secrets and supervision

- **Stuck profile lock.** The persistent profile admits one MCP client. nginx's gate sees each
  MCP request; when a new session starts (a POST without `Mcp-Session-Id`) and every earlier
  session has been silent for `BROWSER_MCP_IDLE_S` seconds (default 120), personal.py stops
  playwright-mcp, saves the profile if it can, clears Chromium's stale lock, and
  `mcp_supervisor.sh` relaunches the server; the profile stays on disk. A client that is merely
  idle for longer than the limit loses its browser tabs when another client initializes. A
  closed TCP connection is not observed; only silence is. Manual escape hatch:
  `curl -X POST -H "Authorization: Bearer $TOKEN" $ENDPOINT/admin/reset` (`$TOKEN` is the identity's token).
- **Secrets.** `start.sh` writes the token and OAuth credentials to a private 0700 directory and
  re-executes itself without them, so neither the entrypoint's nor its children's
  `/proc/<pid>/environ` holds them; personal.py reads and deletes the files. Flyte mounts
  secrets as files only at `/etc/flyte/secrets`, with a layout for apps that is not documented,
  so the secrets still arrive as environment variables at container start. Residual risk: that
  initial environment is visible to the container runtime and to anyone who can read the pod
  spec or the file for the instant it exists; personal.py's memory holds them for its lifetime;
  the supervisor's own pod has the token in its environment by design.
- **Gate proxy.** The local forwarder Chromium uses (127.0.0.1:1056) is restarted with backoff
  if it dies. `/health` and `/exit-check` report `"gate": "up"|"down"` and return 503 while it
  is down.
