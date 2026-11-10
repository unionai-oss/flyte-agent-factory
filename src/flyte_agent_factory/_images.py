"""Container images for the agent task and the browser app.

Two images, built on demand by Flyte's remote builder:

- `agent_image()` — Debian + git + the native Claude Code CLI and the tools required
  by the shipped standards packs.
- `agent_image(browser=True)` — the same, plus Chromium and the Playwright MCP
  server, so a project can run its own browser inside its own pod.
- `browser_image()` — the shared browser *app*: the same stack plus noVNC and
  nginx as the single front door (an `AppEnvironment` exposes exactly one port,
  so `/mcp` and the live `/view/` have to share it).

In-pod versus the shared app is a real choice, and in-pod is the default for a
project that wants a browser:

- The app is one Chromium shared by every project. Playwright allows a single
  client per persistent profile, so projects contend for it and a client that
  dies badly leaves the lock held; `--isolated` buys concurrency at the cost of
  the profile. Rolling it out also needs room for two browser pods at once,
  which on a full cluster stalls.
- In-pod, a project's browser is its own: no contention, a profile that rides
  the project's checkpoint instead of being wiped on every app redeploy, and --
  the reason that matters most -- it shares a network namespace with the agent,
  so the agent can serve a page on localhost and browse it. Against the app
  that is impossible, which is why showing it agent-authored content means
  base64 `data:` URLs.

What in-pod gives up is the live `/view/` noVNC window and the ability for a
human to sign in by hand once: a task has no Service and no public URL. The
shared app stays deployed for that.
"""

from __future__ import annotations

import base64
from pathlib import Path

import flyte

from flyte_agent_factory import _scanners

# Claude Code's `stable` channel at the time of writing; don't go below v2.1.283, the floor
# for several behaviors we rely on.
CLAUDE_VERSION = "2.1.287"

# `@playwright/mcp` pins an alpha build of `playwright` itself, so we never
# install `playwright` separately -- the install script drives the copy bundled
# inside the MCP package to fetch Chromium.
PLAYWRIGHT_MCP_VERSION = "0.0.83"

# Debian bookworm ships Node 18; the playwright build the MCP server pins
# requires >=20, and npm's EBADENGINE warning is not fatal, so the failure
# surfaces later as a broken browser. Pin a current LTS instead.
NODE_VERSION = "22.23.3"
#: The Codex CLI, a standalone binary (no Node needed): a second engine with its own usage meter.
CODEX_VERSION = "0.163.0"
#: sha256 of that release's codex-package-x86_64-unknown-linux-musl.tar.gz (matches GitHub's release digest).
CODEX_SHA256 = "582646c6bf0561eb0de540f7fc85b9121b0091a826bc6e96830b7f1722122182"

TAILSCALE_VERSION = "1.82.5"
# Official tailscale_1.82.5_amd64.tgz.sha256; these images target linux/amd64.
TAILSCALE_SHA256 = "41a8931fa52055bd7ea4b51df9acff2ba2d4e9000c2380b667539b5b99991464"


def _with_tailscale(image: flyte.Image) -> flyte.Image:
    return image.with_commands(
        [
            "curl -fsSL -o /tmp/tailscale.tgz https://pkgs.tailscale.com/stable/"
            f"tailscale_{TAILSCALE_VERSION}_amd64.tgz "
            f"&& echo '{TAILSCALE_SHA256}  /tmp/tailscale.tgz' | sha256sum -c - "
            f"&& tar xzf /tmp/tailscale.tgz --no-same-owner -C /tmp "
            f"&& install /tmp/tailscale_{TAILSCALE_VERSION}_amd64/tailscale /usr/local/bin/tailscale "
            f"&& install /tmp/tailscale_{TAILSCALE_VERSION}_amd64/tailscaled /usr/local/bin/tailscaled "
            f"&& rm -rf /tmp/tailscale.tgz /tmp/tailscale_{TAILSCALE_VERSION}_amd64 "
            "&& tailscale version"
        ]
    )


# Rootless podman, so a session can build and run images in place (`podman build` / `podman run`) without a
# daemon or privileges. vfs storage because overlay needs kernel support a pod does not give an unprivileged
# user; chroot isolation because buildah's default runc isolation needs namespaces the pod forbids.
# The packages come from the Debian release the Flyte base is built on and are not pinned individually: apt
# keeps no old versions to pin to, so a pin would break the build when the release is updated. The version
# that was installed is printed by the last build step.
PODMAN_PACKAGES = ("podman", "uidmap", "slirp4netns", "fuse-overlayfs")
# The unprivileged user Flyte's debian base runs tasks as; created here only if the base lacks it.
PODMAN_USER = "flyte"
# podman refuses to start without both roots set here; a rootless user's own storage still goes under
# ~/.local/share/containers, only the driver is inherited from this file.
_STORAGE_CONF = """[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
"""
_CONTAINERS_CONF = """[containers]
# The pod already has its own network namespace and an unprivileged user cannot create another one.
netns = "host"
[engine]
# No systemd inside a pod.
cgroup_manager = "cgroupfs"
events_logger = "file"
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _with_podman(image: flyte.Image) -> flyte.Image:
    return (
        image.with_apt_packages(*PODMAN_PACKAGES)
        # STORAGE_OPTS, not storage.conf: rootless podman ignores the system file's driver options (probed).
        # With a single-uid mapping, files a base image owns as other uids cannot be chowned; keep them.
        .with_env_vars(
            {
                "STORAGE_DRIVER": "vfs",
                "STORAGE_OPTS": "vfs.ignore_chown_errors=true",
                "BUILDAH_ISOLATION": "chroot",
            }
        )
        .with_commands(
            [
                # base64 because the remote builder strips quotes from printf'd files (see _bench_check).
                f"(id -u {PODMAN_USER} >/dev/null 2>&1 "
                f"|| useradd --create-home --shell /bin/bash {PODMAN_USER}) "
                f"&& mkdir -p /etc/containers && echo {_b64(_STORAGE_CONF)} | base64 -d "
                f"> /etc/containers/storage.conf "
                f"&& echo {_b64(_CONTAINERS_CONF)} | base64 -d > /etc/containers/containers.conf "
                # No subuid/subgid range for the user: the cluster refuses newuidmap's multi-range map
                # (EPERM, probed 2026-10-31) but allows an unprivileged single-uid user namespace, which
                # podman falls back to when the user has no range. Truncated, not left to the base.
                f"&& : > /etc/subuid && : > /etc/subgid "
                "&& podman --version"
            ]
        )
    )


# The linux_amd64 zip digest published in terraform_1.9.8_SHA256SUMS.
TERRAFORM_VERSION = "1.9.8"
TERRAFORM_SHA256 = "186e0145f5e5f2eb97cbd785bc78f21bae4ef15119349f6ad4fa535b83b10df8"

# Exact install steps let the image guard test check the definition, rather than a second tool list.
TOOLS = {
    "ruff": "/opt/gate-tools/bin/pip install ruff==0.17.0",
    "mypy": "/opt/gate-tools/bin/pip install mypy==2.4.0",
    "pytest": "/opt/gate-tools/bin/pip install pytest==8.3.5",
    "shellcheck": "shellcheck",
    "terraform": (
        "curl -fsSL -o /tmp/terraform.zip https://releases.hashicorp.com/terraform/"
        f"{TERRAFORM_VERSION}/terraform_{TERRAFORM_VERSION}_linux_amd64.zip "
        f"&& echo '{TERRAFORM_SHA256}  /tmp/terraform.zip' | sha256sum -c - "
        "&& unzip -o /tmp/terraform.zip terraform -d /usr/local/bin "
        "&& chmod a+rx /usr/local/bin/terraform && rm /tmp/terraform.zip "
        "&& terraform version"
    ),
    "tflint": _scanners.render_image_install_step(_scanners.TFLINT),
    # Not a standards tool: the scanner tests (ops/scan_public.py) run it, here instead of downloading it.
    "gitleaks": _scanners.render_image_install_step(_scanners.GITLEAKS),
    "tsc": "npm install -g typescript@5.8.3",
    "eslint": "npm install -g eslint@9.22.0",
    "prettier": "npm install -g prettier@3.5.3",
}

# Where Chromium lands. Set explicitly so the browsers are found no matter what
# HOME the container ends up running with.
BROWSERS_PATH = "/ms-playwright"

# Claude Code is installed here rather than under /root: the image builds as
# root but tasks run as an unprivileged user, which cannot read /root.
CLAUDE_HOME = "/opt/claude"

# Where the role subagent definitions live in the image.
AGENT_ROLES_DIR = "/opt/claude-roles"

# The package set the voice work actually got running, lifted from its own
# cluster.py. Shared by the factory (which trains) and the demo image (which
# narrates), so the two cannot drift. `setuptools<81` is the load-bearing pin:
# `openai-whisper` ships no wheel, its setup.py imports `pkg_resources`, and
# setuptools>=81 removed it.
VOICE_PACKAGES = (
    "torch==2.8.0",
    "torchaudio==2.8.0",
    "f5-tts",
    "resemblyzer",
    "openai-whisper",
    "jiwer",
    "setuptools<81",
    "soundfile",
    "onnxruntime",
    "silero-vad",
    "scikit-learn",
    "scipy",
    "pyarrow",
    "pandas",
    "datasets",
)

_ASSETS = Path(__file__).parent / "_assets"


def agent_image(
    claude_version: str = CLAUDE_VERSION,
    *,
    browser: bool = False,
    tailscale: bool = False,
    mcp_version: str = PLAYWRIGHT_MCP_VERSION,
    node_version: str = NODE_VERSION,
) -> flyte.Image:
    """Return the image the long-lived agent task runs in.

    With `browser=True` it also carries Chromium and the Playwright MCP server
    so the project drives a browser in its own pod. That is a separate image
    name, not a variant of the same one: a project that never browses should
    not pay Chromium's cold start.
    """
    image = (
        flyte.Image.from_debian_base(
            name="agent-factory-browser" if browser else "agent-factory",
            python_version=(3, 12),
        )
        .with_apt_packages(
            "git",
            "curl",
            "ca-certificates",
            "openssh-client",
            # Claude Code reaches for these constantly; without them it falls
            # back to slower paths or just can't do the work.
            "ripgrep",
            "jq",
            "less",
            "unzip",
            "zstd",
            "procps",
            "xz-utils",
            TOOLS["shellcheck"],
        )
        # CLAUDE_CONFIG_DIR is set at runtime, since it points into the restored state dir.
        # Volumes come from this plugin. Linux-x86 only, which is exactly what
        # this image is -- it just cannot be installed on a macOS dev machine,
        # hence the lazy imports in `_volume`.
        # The role definitions have to be in the *image*. Flyte's code bundle
        # carries Python modules, not package data, so reading them from the
        # installed package at runtime found nothing and the swarm roles
        # silently never installed.
        .with_source_folder(_ASSETS / "agents", dst=AGENT_ROLES_DIR)
        # The GitHub App JWT (`_github`); the factory-deployer runs in this image.
        .with_pip_packages("pyjwt[crypto]>=2.8,<3")
        .with_env_vars(
            {
                "PATH": f"{CLAUDE_HOME}/.local/bin:${{PATH}}:/opt/gate-tools/bin",
                "NODE_VERSION": node_version,
            }
        )
        .with_source_folder(_ASSETS / "browser", dst="/opt/browser")
        # Separate from the factory and project environments so dependency installs cannot remove them.
        .with_commands(["python -m venv /opt/gate-tools", "sh /opt/browser/install-node.sh"])
        .with_commands([step for tool, step in TOOLS.items() if tool != "shellcheck"])
        .with_commands(
            [
                # The installer always targets $HOME/.local/bin, and the build
                # runs as root while the task does not -- left at the default
                # the binary lands in /root, which the task's user cannot even
                # read. Point HOME at a shared location and open it up.
                f"curl -fsSL https://claude.ai/install.sh | HOME={CLAUDE_HOME} bash -s {claude_version}",
                f"chmod -R a+rX {CLAUDE_HOME}",
                f"{CLAUDE_HOME}/.local/bin/claude --version",
                # Root-owned (--no-same-owner) and checksum-verified; the task's user only needs to run it.
                (
                    # The full package, not the bare binary: Codex's tools run through
                    # `codex-code-mode-host` beside it, and without it every tool call failed
                    # ("failed to spawn code-mode host") in the first live review.
                    "curl -fsSL -o /tmp/codex.tgz https://github.com/openai/codex/releases/download/"
                    f"rust-v{CODEX_VERSION}/codex-package-x86_64-unknown-linux-musl.tar.gz "
                    f"&& echo '{CODEX_SHA256}  /tmp/codex.tgz' | sha256sum -c - "
                    "&& mkdir -p /opt/codex && tar xzf /tmp/codex.tgz --no-same-owner -C /opt/codex && rm "
                    "/tmp/codex.tgz "
                    "&& chmod -R a+rX /opt/codex "
                    "&& ln -sf /opt/codex/bin/codex /usr/local/bin/codex "
                    "&& ln -sf /opt/codex/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host "
                    "&& codex --version"
                ),
            ]
        )
    )
    image = _with_podman(image)
    if tailscale:
        image = _with_tailscale(image)
    if not browser:
        return image
    return (
        image
        # Xvfb because Chromium runs headed: the proven-working configuration,
        # and a headed browser is what records a watchable video.
        #
        # ffmpeg because a browser recording has **no audio track**. Voiceover
        # and a music bed cannot be captured, only composited afterwards -- and
        # compositing is also what makes the pieces swappable, so a later
        # instruction to change the music or one line of narration re-renders
        # from the kept assets instead of re-recording the demo.
        .with_apt_packages("xvfb", "fonts-liberation", "fonts-dejavu-core", "ffmpeg")
        .with_env_vars(
            {
                "PLAYWRIGHT_BROWSERS_PATH": BROWSERS_PATH,
                "DISPLAY": ":99",
                # Read by install.sh, so its invocation takes no arguments and
                # needs no shell quoting (see the note in that script).
                "NODE_VERSION": node_version,
                "PLAYWRIGHT_MCP_VERSION": mcp_version,
            }
        )
        # The same installer the app image uses, rather than a second copy of
        # the Node/Chromium dance that would drift from it.
        .with_commands(["chmod +x /opt/browser/install.sh /opt/browser/start.sh"])
        .with_commands(["/opt/browser/install.sh"])
    )


BROWSER_APT_PACKAGES = (
    "nginx",
    "xvfb",
    "x11vnc",
    "novnc",
    "websockify",
    "curl",
    "ca-certificates",
    "procps",
    # For the Node tarball, which is .tar.xz.
    "xz-utils",
    # Chromium renders boxes instead of text without these.
    "fonts-liberation",
    "fonts-dejavu-core",
)


def browser_image(
    mcp_version: str = PLAYWRIGHT_MCP_VERSION,
    node_version: str = NODE_VERSION,
    *,
    tailscale: bool = False,
) -> flyte.Image:
    """Return the image the browser app runs in: Chromium + Playwright MCP + noVNC."""
    image = (
        flyte.Image.from_debian_base(name="claude-browser", python_version=(3, 12))
        # Not with_apt_packages: the remote builder reuses a cached package index, so a Debian security
        # update that removes a pinned .deb fails the install with a 404. Refreshing it in the same step
        # avoids that.
        .with_commands(
            [
                "apt-get update && apt-get install -y --no-install-recommends "
                + " ".join(BROWSER_APT_PACKAGES)
                + " && rm -rf /var/lib/apt/lists/*"
            ]
        )
        .with_env_vars(
            {
                "PLAYWRIGHT_BROWSERS_PATH": BROWSERS_PATH,
                "DISPLAY": ":99",
                # Read by install.sh, so its invocation takes no arguments and
                # needs no shell quoting (see the note in that script).
                "NODE_VERSION": node_version,
                "PLAYWRIGHT_MCP_VERSION": mcp_version,
            }
        )
        .with_pip_packages("fsspec", "s3fs", "gcsfs", "obstore>=0.5")
        .with_source_folder(_ASSETS / "browser", dst="/opt/browser")
        .with_commands(["chmod +x /opt/browser/install.sh /opt/browser/start.sh"])
        .with_commands(["/opt/browser/install.sh"])
    )

    return _with_tailscale(image) if tailscale else image
