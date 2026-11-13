#!/usr/bin/env bash
# Checks for the factory-improvement project: the full offline suite and lint, from the repo root.
#
# The suite runs as two pytest processes at once, `not slow` and `slow` (the benchmark hidden checks:
# Terraform and pytest-in-pytest, about two thirds of the CPU); together they select every test. The slow lane
# runs only with AGENT_FACTORY_SLOW_LANE=child (in a child action through the launcher AGENT_FACTORY_TEST_LANE
# names, which falls back to this pod) or AGENT_FACTORY_RUN_SLOW=1 (here); CI's slow job always runs it. With
# AGENT_FACTORY_SLOW_LANE=child the fast lane goes through the same launcher. Without a launcher both run
# here. With AGENT_FACTORY_PYTEST_JUNIT_DIR set (the pre-review does) each lane leaves a junit file there,
# which the standards gate reads instead of running the suite again. Extra arguments narrow the run, so then
# none is left.
set -euo pipefail
# The project root: the repository's, or a subdirectory of a workspace repository (projects/<name>/).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

record=
if [[ -n "${AGENT_FACTORY_PYTEST_JUNIT_DIR:-}" && $# -eq 0 ]]; then
  record=1
  rm -f "$AGENT_FACTORY_PYTEST_JUNIT_DIR"/{fast,slow}.xml
  git rev-parse HEAD >"$AGENT_FACTORY_PYTEST_JUNIT_DIR/head"
fi

pytest_lane() {
  local lane=$1 marker=$2
  shift 2
  local junit=()
  # Exit 5 is "no tests selected": a narrowed run can leave one lane empty, which is not a failure.
  if [[ -n ${AGENT_FACTORY_TEST_LANE:-} && ${AGENT_FACTORY_SLOW_LANE:-} == child ]]; then
    [[ -z $record ]] || junit=(--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    local marker_args=()
    [[ $lane == slow ]] || marker_args=(-m "$marker")
    "$AGENT_FACTORY_TEST_LANE" ${junit[@]+"${junit[@]}"} ${marker_args[@]+"${marker_args[@]}"} "$@" || [[ $? -eq 5 ]]
  else
    [[ -z $record ]] || junit=(--junitxml "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    PYTHONPATH=src uv run python -m pytest tests -q -m "$marker" ${junit[@]+"${junit[@]}"} "$@" || [[ $? -eq 5 ]]
  fi
}

pytest_lane fast "not slow" "$@" &
fast=$!
# The slow lane (benchmark hidden checks: real Terraform, pytest-in-pytest) costs about two thirds of the suite's
# CPU. It runs in a child action, when asked for here, and always in CI's slow job; on a 4-CPU pod it would hold
# these checks past their time limit.
slow=
if [[ ${AGENT_FACTORY_SLOW_LANE:-} == child || -n ${AGENT_FACTORY_RUN_SLOW:-} ]]; then
  pytest_lane slow slow "$@" &
  slow=$!
else
  echo "slow lane: not run here (CI's slow job runs it); AGENT_FACTORY_RUN_SLOW=1 or AGENT_FACTORY_SLOW_LANE=child runs it" >&2
fi
status=0
wait "$fast" || status=1
[[ -z $slow ]] || wait "$slow" || status=1
[[ $status -eq 0 ]] || exit 1

uv run --with ruff ruff check src tests ops .github
uv run --with ruff ruff format --check src tests ops .github
# The chat skills ship as committed zips; a stale one (exit 1) or one with no version line (exit 2) fails here.
uv run python ops/package_skill.py --check skills/dist/agent-factory.zip
uv run python ops/package_skill.py --check skills/dist/since.zip
