#!/bin/bash
# Run a command in its own process group and relaunch it only when asked to.
#
#   mcp_supervisor.sh PID_FILE RESTART_FILE command [args...]
#
# personal.py restarts playwright-mcp to free a profile lock left by a vanished client. It
# creates RESTART_FILE and kills the group named in PID_FILE; that exit is then a restart.
# Any other exit ends this script non-zero, which start.sh's `wait -n` turns into the app
# going down -- the behaviour every other component has.
set -u

pid_file=$1
restart_file=$2
shift 2

while :; do
    setsid "$@" &
    child=$!
    echo "$child" > "$pid_file"
    wait "$child"
    [ -e "$restart_file" ] || exit 1
    rm -f "$restart_file"
    echo "[mcp] restarting after a requested reset" >&2
done
