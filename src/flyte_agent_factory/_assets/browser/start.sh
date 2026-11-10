#!/bin/bash
# Bring up a watchable headed Chromium behind one port.
#
#   Xvfb :99          virtual display
#   x11vnc            exposes it over VNC on localhost
#   websockify        wraps VNC for noVNC in the browser
#   playwright-mcp    headed Chromium + the MCP endpoint the agent drives
#   nginx             the single front door (foreground; it is PID 1's child
#                     and the process whose exit ends the container)
set -euo pipefail

# Secrets arrive as environment variables, which stay readable in /proc/<pid>/environ for the
# life of the process (unset does not change that) and are inherited by every child. Move them
# into private files and re-exec without them: exec replaces this process's environ, and
# personal.py reads and deletes the files. Flyte mounts secrets as files only at
# /etc/flyte/secrets, whose layout for apps is not documented, so this does not depend on it.
if [ "${BROWSER_PERSONAL:-0}" = "1" ] && [ -z "${BROWSER_SECRETS_DIR:-}" ]; then
    secrets_dir=$(mktemp -d)
    for name in BROWSER_PERSONAL_TOKEN TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET; do
        [ -n "${!name:-}" ] && (umask 077 && printf %s "${!name}" > "$secrets_dir/$name")
    done
    exec env -u BROWSER_PERSONAL_TOKEN -u TAILSCALE_OAUTH_CLIENT_ID -u TAILSCALE_OAUTH_CLIENT_SECRET \
        BROWSER_SECRETS_DIR="$secrets_dir" /bin/bash "$0" "$@"
fi

PORT="${BROWSER_APP_PORT:-8080}"
SCREEN="${BROWSER_SCREEN:-1920x1080x24}"
VIEWPORT="${BROWSER_VIEWPORT:-1600x900}"
# Under HOME, not /var/lib: this container runs as an unprivileged user and
# cannot create a directory at the filesystem root. `set -e` turned that into
# an immediate exit 1 and a crash loop on every start.
STATE_BASE="${HOME:-/tmp}"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-$STATE_BASE/browser-profile}"
OUTPUT_DIR="${BROWSER_OUTPUT_DIR:-$STATE_BASE/browser-output}"

export DISPLAY=:99
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

mkdir -p "$PROFILE_DIR" "$OUTPUT_DIR" /tmp/nginx-body

# Kill the whole process group on the way out: without this, a crashed
# playwright-mcp leaves Xvfb and nginx up and the app looks healthy while the
# agent's browser tools return nothing.
# ShellCheck cannot follow the indirect invocation through trap.
# shellcheck disable=SC2317,SC2329
cleanup() {
    trap - EXIT INT TERM
    if [ -n "${PERSONAL_PID:-}" ]; then
        kill "$PERSONAL_PID" 2>/dev/null || true
        wait "$PERSONAL_PID" 2>/dev/null || true
    fi
    if [ -n "${MCP_PID:-}" ]; then
        kill -- "-$MCP_PID" 2>/dev/null || true
    fi
    # Under mcp_supervisor.sh MCP_PID is the supervisor; the server's group is in the pid file.
    if [ -s /tmp/personal-mcp.pid ]; then
        kill -- "-$(cat /tmp/personal-mcp.pid)" 2>/dev/null || true
    fi
    kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

PROXY_ARGS=()
export BROWSER_PROFILE_DIR="$PROFILE_DIR"
if [ "${BROWSER_PERSONAL:-0}" = "1" ]; then
    chmod 700 "$PROFILE_DIR"
    rm -f /tmp/personal-ready /tmp/personal-mcp.pid
    python3 /opt/browser/personal.py &
    PERSONAL_PID=$!
    unset TAILSCALE_OAUTH_CLIENT_ID TAILSCALE_OAUTH_CLIENT_SECRET BROWSER_PERSONAL_TOKEN BROWSER_SECRETS_DIR
    for _ in $(seq 1 240); do
        [ -e /tmp/personal-ready ] && break
        kill -0 "$PERSONAL_PID" 2>/dev/null || exit 1
        sleep 1
    done
    [ -e /tmp/personal-ready ] || exit 1
    if [ -n "${BROWSER_EXIT_NODE:-}" ]; then
        # WebRTC UDP otherwise bypasses Chromium's HTTP/SOCKS proxy.
        cat > /tmp/personal-mcp.json <<'JSON'
{"browser":{"launchOptions":{"args":["--force-webrtc-ip-handling-policy=disable_non_proxied_udp","--proxy-bypass-list=<-loopback>"]}}}
JSON
        PROXY_ARGS=(--proxy-server socks5://127.0.0.1:1056 --config /tmp/personal-mcp.json)
    fi
fi

echo "[start] profile=$PROFILE_DIR output=$OUTPUT_DIR"
echo "[start] Xvfb on :99 ($SCREEN)"
Xvfb :99 -screen 0 "$SCREEN" -nolisten tcp &

for _ in $(seq 1 100); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.1
done
if [ ! -e /tmp/.X11-unix/X99 ]; then
    echo "[start] Xvfb failed to come up" >&2
    exit 1
fi

echo "[start] x11vnc + websockify"
# websockify listens on loopback only: on 0.0.0.0 it was reachable by every pod in the
# namespace, around nginx. The personal app also puts a per-boot password on x11vnc, which
# personal.py hands to the human's browser only after the /view-login check; the shared app
# keeps -nopw because its view is behind platform auth alone.
if [ "${BROWSER_PERSONAL:-0}" = "1" ]; then
    VNC_AUTH=(-passwdfile /tmp/personal-vnc/passwd)
else
    VNC_AUTH=(-nopw)
fi
x11vnc -display :99 -rfbport 5900 -forever -shared "${VNC_AUTH[@]}" -localhost -quiet &
websockify --web=/usr/share/novnc 127.0.0.1:6080 localhost:5900 &

echo "[start] playwright-mcp on 127.0.0.1:8931 (headed, output $OUTPUT_DIR)"
# Headed on purpose, so the noVNC view shows the session the agent is driving.
# --no-sandbox because this runs as root in a container.
# --caps=devtools unlocks browser_start_video / browser_stop_video (plus
# chapter cards and action overlays) and tracing; pdf unlocks browser_pdf_save.
# Without them those tools simply are not offered, which is the difference
# between "drove the UI" and "produced something you can send someone".
# --browser chromium, explicitly. The default is the *branded Chrome* channel
# (/opt/google/chrome/chrome), which is not what `playwright install chromium`
# puts in the image -- so every navigation failed with "Chromium distribution
# 'chrome' is not found" while the MCP server itself looked perfectly healthy.
#
# --isolated vs a persistent profile is a real trade, and this app is shared by
# every project at once:
#
#   persistent  keeps cookies between tool calls, so a human signs into a
#               dashboard through noVNC once and the agent inherits it -- but
#               Playwright allows exactly ONE client per persistent profile.
#               A second one gets "Browser is already in use for <dir>" on
#               *every* call, and a client that disconnects uncleanly can leave
#               the lock held, which blocked a project from recording its demo
#               with nothing else actually using the browser.
#   isolated    a fresh in-memory profile per session, so several agents can
#               drive it concurrently. No inherited logins.
#
# Isolated is the default because the one login that mattered -- the Union
# console -- turned out to be reachable by setting an Authorization header from
# the pod's own API key, so persistence bought little and cost concurrency.
# Set BROWSER_ISOLATED=0 to go back to a shared, single-seat profile.
if [ "${BROWSER_ISOLATED:-1}" = "0" ]; then
    echo "[start] persistent profile at $PROFILE_DIR (single client only)"
    PROFILE_ARGS=(--user-data-dir "$PROFILE_DIR")
else
    echo "[start] isolated profiles (concurrent clients, no inherited logins)"
    PROFILE_ARGS=(--isolated)
fi

MCP_COMMAND=(playwright-mcp \
    --port 8931 \
    --host 127.0.0.1 \
    --browser chromium \
    --no-sandbox \
    "--caps=devtools,pdf" \
    --viewport-size "$VIEWPORT" \
    "${PROFILE_ARGS[@]}" \
    "${PROXY_ARGS[@]}" \
    --output-dir "$OUTPUT_DIR")
if [ "${BROWSER_PERSONAL:-0}" = "1" ]; then
    bash /opt/browser/mcp_supervisor.sh /tmp/personal-mcp.pid /tmp/personal-mcp.restart "${MCP_COMMAND[@]}" &
else
    setsid "${MCP_COMMAND[@]}" &
fi
MCP_PID=$!

echo "[start] personal endpoints require bearer auth; public URLs use platform auth"

python3 - "$PORT" "$OUTPUT_DIR" <<'PYEOF'
import pathlib
import sys
import os

port = sys.argv[1]
template = pathlib.Path("/opt/browser/nginx.conf.template").read_text()
rendered = template.replace("__PORT__", port).replace("__OUTPUT_DIR__", sys.argv[2])
if os.environ.get("BROWSER_PERSONAL") == "1":
    for location in ("location /mcp {", "location /output/ {"):
        rendered = rendered.replace(location, location + "\n            auth_request /_personal_gate;\n            error_page 500 = @exit_unavailable;")
    # noVNC and its websocket (/view/websockify) both live under /view/. The cookie is the
    # credential and websockify has no use for it.
    rendered = rendered.replace(
        "location /view/ {",
        'location /view/ {\n            auth_request /_view_gate;\n            proxy_set_header Cookie "";'
        # The platform's edge injects third-party analytics into pages; noVNC loads only its own files.
        '\n            add_header Content-Security-Policy "default-src \'self\'; img-src \'self\' data:; '
        'style-src \'self\' \'unsafe-inline\'; frame-ancestors \'none\'" always;'
        '\n            add_header Referrer-Policy no-referrer always;',
    )
    rendered = rendered.replace("location = /healthz {", """location @exit_unavailable {
            default_type application/json;
            return 503 '{"error":"Personal browser exit node unavailable; access blocked"}';
        }
        location = /_personal_gate {
            internal;
            proxy_pass http://127.0.0.1:8932/exit-check;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
            # Subrequests are GETs; personal.py needs the real method to spot an MCP initialize.
            proxy_set_header X-Original-Method $request_method;
            # A reset snapshots the profile before it answers.
            proxy_read_timeout 120s;
        }
        location = /_view_gate {
            internal;
            proxy_pass http://127.0.0.1:8932/view-auth;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }
        location = /view-login {
            proxy_pass http://127.0.0.1:8932/view-login;
        }
        location = /view-ticket {
            proxy_pass http://127.0.0.1:8932/view-ticket;
        }
        location = /admin/reset {
            proxy_pass http://127.0.0.1:8932/admin/reset;
            proxy_read_timeout 120s;
        }
        location = /_personal_auth {
            internal;
            proxy_pass http://127.0.0.1:8932/auth;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }
        location = /exit-check {
            proxy_pass http://127.0.0.1:8932/exit-check;
        }
        location = /health {
            auth_request /_personal_auth;
            proxy_pass http://127.0.0.1:8932/health;
        }
        location = /healthz {""")
pathlib.Path("/tmp/nginx.conf").write_text(rendered)
PYEOF

# nginx must not start answering before the MCP server does, or the first
# calls come back 502 and a client reasonably concludes the app is broken.
echo "[start] waiting for playwright-mcp to listen"
for _ in $(seq 1 60); do
    curl -fsS -m 2 -o /dev/null "http://127.0.0.1:8931/mcp" 2>/dev/null && break
    # A 403 means it is listening and refusing the bare request, which is the
    # DNS-rebinding check doing its job -- good enough to proceed.
    curl -sS -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:8931/mcp" 2>/dev/null \
        | grep -qE '40[0-9]' && break
    sleep 1
done

echo "[start] nginx on :$PORT  (/mcp, /view/)"
nginx -c /tmp/nginx.conf &

# Exit as soon as *any* component dies, rather than exec'ing nginx and leaving
# a container that passes its readiness probe while the browser behind it is
# gone. Exiting non-zero lets the platform restart the whole app cleanly.
wait -n
echo "[start] a component exited; bringing the app down" >&2
exit 1
