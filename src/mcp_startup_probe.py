"""Check that playwright-mcp starts with the flags start.sh passes it.

The app serves /healthz and /output/ but answers /mcp with 502, so nginx is
fine and the MCP server behind it is not. App logs are not reachable through
the CLI, so this runs the identical command in a task and prints what it said.

Background processes get their own log file rather than inheriting stdout: an
earlier version of this probe had Xvfb hold the pipe open and `communicate()`
waited forever for an EOF that never came.
"""

import asyncio

import flyte

from flyte_agent_factory._images import browser_image

env = flyte.TaskEnvironment(
    name="mcp-startup-probe",
    image=browser_image(),
    resources=flyte.Resources(cpu=2, memory="4Gi"),
)

SCRIPT = r"""
set -u
export DISPLAY=:99 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
mkdir -p "$HOME/prof" "$HOME/out"

Xvfb :99 -screen 0 1600x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 60); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.2; done
echo "xvfb up: $([ -e /tmp/.X11-unix/X99 ] && echo yes || echo NO)"

# Exactly what start.sh runs.
playwright-mcp --port 8931 --host 127.0.0.1 --no-sandbox \
    --caps=devtools,pdf --viewport-size 1600x900 \
    --user-data-dir "$HOME/prof" --output-dir "$HOME/out" >/tmp/mcp.log 2>&1 &
MCP_PID=$!

for i in $(seq 1 30); do
    sleep 1
    if ! kill -0 "$MCP_PID" 2>/dev/null; then echo "mcp EXITED after ${i}s"; break; fi
    if curl -fsS -m 2 -o /dev/null http://127.0.0.1:8931/mcp 2>/dev/null; then
        echo "mcp answering after ${i}s"; break
    fi
done

echo "--- alive? ---"
kill -0 "$MCP_PID" 2>/dev/null && echo RUNNING || echo DEAD
echo "--- listening sockets ---"
(ss -ltnp 2>/dev/null || netstat -ltnp 2>/dev/null) | head -10
echo "--- mcp.log ---"
cat /tmp/mcp.log
echo "--- probe POST /mcp ---"
curl -sS -m 5 -o /dev/null -w 'status=%{http_code}\n' -X POST http://127.0.0.1:8931/mcp \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"p","version":"0"}}}' 2>&1

# Does it start without the caps flag? Isolates the flag from everything else.
kill "$MCP_PID" 2>/dev/null || true
sleep 2
playwright-mcp --port 8932 --host 127.0.0.1 --no-sandbox \
    --user-data-dir "$HOME/prof2" >/tmp/mcp2.log 2>&1 &
sleep 12
echo "--- without --caps: ---"
pgrep -f 'port 8932' >/dev/null && echo RUNNING || echo DEAD
tail -15 /tmp/mcp2.log
"""  # noqa: E501 -- shell script embedded verbatim; wrapping the JSON body would change what is probed


@env.task(report=True)
async def probe() -> str:
    """Run the startup script and return the start of its output."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-c",
        SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    text = out.decode(errors="replace")
    print(text, flush=True)
    return text[:8000]
