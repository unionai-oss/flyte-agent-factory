#!/bin/sh
# Shared by browser and slot images so the compiler and browser use the same Node.
set -eu
: "${NODE_VERSION:?NODE_VERSION must be set}"
if command -v node >/dev/null 2>&1 && [ "$(node --version)" = "v${NODE_VERSION}" ]; then
    exit 0
fi

case "$(uname -m)" in
    x86_64) node_arch=x64 ;;
    aarch64 | arm64) node_arch=arm64 ;;
    *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

tarball="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"
echo "[install] node v${NODE_VERSION} (${node_arch})"
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}" -o /tmp/node.tar.xz
# --strip-components=1 drops the version-named top directory, so node's
# bin/lib/include land directly in /usr/local and are already on PATH.
tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1
rm -f /tmp/node.tar.xz
node --version

