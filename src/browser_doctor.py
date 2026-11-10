r"""Diagnose the browser app's startup, where app logs aren't reachable by CLI.

`flyte get logs` takes a run, not an app, so a crash-looping app is opaque from
here. Running the same startup pieces inside a *task* on the same image gives
logs, and checks each component separately instead of guessing which one exits.

    PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" \
        -d "$FLYTE_DOMAIN" src/browser_doctor.py doctor
"""

import asyncio

import flyte

from flyte_agent_factory._images import browser_image

env = flyte.TaskEnvironment(
    name="browser-doctor",
    image=browser_image(),
    resources=flyte.Resources(cpu=1, memory="2Gi"),
)

CHECKS = [
    ("playwright-mcp present", "command -v playwright-mcp && playwright-mcp --version"),
    # The flag I added. If the spelling is wrong, the server exits and the
    # whole script follows it down via `wait -n`.
    ("--caps accepted", "playwright-mcp --help 2>&1 | grep -iE 'caps|devtools' | head -20"),
    ("node + chromium", "node --version; ls /ms-playwright 2>&1 | head -5"),
    ("novnc assets", "ls /usr/share/novnc/vnc.html 2>&1"),
    ("xvfb + x11vnc + websockify", "command -v Xvfb x11vnc websockify"),
    # Renders the template exactly as start.sh does, then asks nginx.
    (
        "nginx config valid",
        (
            "BROWSER_APP_PORT=8080 BROWSER_TOKEN=probe /bin/bash -c '"
            'python3 - 8080 "" <<PY\n'
            "import os, pathlib, sys\n"
            't = pathlib.Path("/opt/browser/nginx.conf.template").read_text()\n'
            'r = (t.replace("__PORT__", sys.argv[1]).replace("__TOKEN_GATE__", sys.argv[2])\n'
            '      .replace("__OUTPUT_DIR__", os.environ["HOME"] + "/browser-output"))\n'
            'pathlib.Path("/tmp/nginx.conf").write_text(r)\n'
            "PY\n"
            'mkdir -p "$HOME/browser-output" /tmp/nginx-body && nginx -t -c /tmp/nginx.conf\''
        ),
    ),
    (
        "playwright-mcp actually starts",
        (
            "export DISPLAY=:99 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright; "
            "Xvfb :99 -screen 0 1280x720x24 -nolisten tcp & sleep 3; "
            "timeout 25 playwright-mcp --port 8931 --host 127.0.0.1 --no-sandbox "
            "--caps=devtools,pdf --user-data-dir /tmp/prof --output-dir /tmp/out "
            "> /tmp/mcp.log 2>&1 & sleep 18; "
            "echo '--- exit check ---'; pgrep -f playwright-mcp >/dev/null && echo RUNNING || echo DEAD; "
            "echo '--- log ---'; tail -30 /tmp/mcp.log"
        ),
    ),
]


@env.task(report=True)
async def doctor() -> str:
    """Run each startup component independently and report what it said."""
    lines = []
    for label, command in CHECKS:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            executable="/bin/bash",
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace").strip()
        status = "ok" if proc.returncode == 0 else f"exit {proc.returncode}"
        lines.append(f"===== {label} [{status}] =====\n{text}")
        print(lines[-1], flush=True)
    return "\n\n".join(lines)[:12000]
