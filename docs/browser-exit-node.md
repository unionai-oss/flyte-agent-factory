# Browser exit node

The factory's in-pod Chromium can opt into a local Tailscale SOCKS5 proxy.
`tailscaled --tun=userspace-networking` shares the browser's pod, uses a private
temporary state directory, and changes no kernel routes. Only Playwright gets
`--proxy-server`; no HTTP_PROXY, HTTPS_PROXY or ALL_PROXY is exported. GitHub,
Claude, Codex, Flyte and other supervisor/session HTTP calls remain direct.
Chromium disables non-proxied WebRTC UDP and local DNS resolution. Loopback is
explicitly bypassed so the browser can still view a local development server.

Enable this in the deployment profile:

```toml
[browser]
exit_node = "my-exit-node"
tailscale_secret = "my_browser_tailnet_key"
```

Create a Tailscale **reusable, ephemeral, pre-approved auth key**, tagged
`tag:factory-browser`, and store its value in that Union secret. Restrict the
key to that tag; do not use an administrator's personal key. In tailnet policy,
configure `tagOwners` so the operator can issue that tag, approve the exit node's
advertised default routes, and grant `tag:factory-browser` access to
`autogroup:internet` (all IP traffic) so it may use the exit node. The configured
node must be online and have exit routes approved. Rotate the Union secret
before the auth key expires. The key is file-mounted, read by the supervisor
only for Tailscale authentication, and passed to the CLI by a private temporary
file, never as an argument value or in the session environment. This is process
environment isolation, not a security boundary against code running as the same
Unix user that can read mounted files.

With both settings absent the slot deploy environment and secret mounts remain
unchanged. Tailscale binaries are installed only for enabled images, from a
version and SHA256 pinned static distribution. Socket, state and auth files are
under a private temporary directory and removed at shutdown. Startup is bounded;
failed authentication, missing binaries or an offline/unselected exit node
produce a timeline warning and no browser. An enabled slot never falls back to
the direct shared app. Chromium keeps its fixed proxy if the daemon or exit node
subsequently disappears, so navigation fails instead of going direct.

The separate personal app already has its own userspace SOCKS proxy, OAuth
credentials, exit health gate and launch flags. Its existing `exit_node`-only
configuration remains supported; its proxy is in that app's pod, not the slot.
The new `tailscale_secret` setting opts the in-pod browser into that same exit
node independently: a profile may enable both browsers, with the auth key mounted
only in the slot and the existing OAuth credentials mounted only in the personal
app. The shared app remains direct and gets no new secrets. Routing the shared app would change
egress for unrelated callers, so enabled slots refuse to use it. Personal images
install the pinned binaries only when their existing exit node is configured.

For a live smoke, run `ops/probes/browser_exit_node.py --exit-node NAME
--secret SECRET_NAME` inside a browser-enabled slot with that Union secret
mounted. It uses the same LocalBrowser startup and teardown and prints exactly
the browser's public IP and a direct HTTP public IP, one per line. A QA operator
should confirm they differ, stop the exit node and confirm browser requests
fail, and confirm direct HTTP still succeeds. Offline unit tests cannot prove
actual tailnet routing or image startup.

Implementation references: [Tailscale exit-node policy](https://tailscale.com/docs/features/exit-nodes),
[Union file secret mounts](https://www.union.ai/docs/v2/union/user-guide/tasks/task-configuration/secrets/),
and the [pinned Tailscale CLI](https://github.com/tailscale/tailscale/blob/v1.82.5/cmd/tailscale/cli/up.go).
