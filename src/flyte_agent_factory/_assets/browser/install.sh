#!/bin/sh
# Install Node and the Playwright MCP server, plus Chromium.
#
# This is a script rather than inline `with_commands` entries for a reason:
# Flyte renders each command into a Dockerfile `RUN` and does not preserve
# shell quoting, so `root="$(npm root -g)"` reaches the builder as
# `root=$(npm root -g)` and dies with `Syntax error: end of file unexpected`.
# Anything needing quotes, subshells or loops has to live in a file.
#
# Node comes from nodejs.org rather than apt because Debian bookworm ships
# Node 18 and `@playwright/mcp` pins a playwright build that requires >=20.
# Version inputs arrive as environment variables, so the invocation needs no
# arguments and therefore no quoting.
set -eu

: "${NODE_VERSION:?NODE_VERSION must be set}"
: "${PLAYWRIGHT_MCP_VERSION:?PLAYWRIGHT_MCP_VERSION must be set}"

sh "$(dirname "$0")/install-node.sh"

echo "[install] @playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"
npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"

# npm installs only the top-level package's bins, so there is no `playwright`
# on PATH. Drive the copy bundled inside the MCP package instead -- and accept
# either layout, since npm may hoist playwright-core out of it.
root="$(npm root -g)"
for cli in \
    "$root/@playwright/mcp/node_modules/playwright-core/cli.js" \
    "$root/playwright-core/cli.js"
do
    if [ -f "$cli" ]; then
        echo "[install] chromium via $cli"
        exec node "$cli" install --with-deps chromium
    fi
done

echo "could not locate playwright-core cli.js under $root" >&2
exit 1
