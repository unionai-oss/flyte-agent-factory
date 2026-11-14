#!/usr/bin/env bash
# Checks for the factory-improvement project: the full offline suite and lint, from the repo root.
#
# The suite runs as two pytest processes at once, `not slow` and `slow` (the benchmark hidden checks:
# Terraform and pytest-in-pytest); together they select every test. The slow lane runs only with
# AGENT_FACTORY_SLOW_LANE=child or AGENT_FACTORY_RUN_SLOW=1; CI's slow job always runs it. With
# AGENT_FACTORY_SLOW_LANE=child and a launcher (AGENT_FACTORY_TEST_LANE) both lanes go through it: the fast
# lane to a child action (spot, then on-demand, twice, then this pod), the slow lane to spot, then this pod.
# AGENT_FACTORY_SLOW_LANE_HERE=1 runs the slow lane here from the start instead, for a caller with a time
# limit that a child failing mid-run would leave no room to run it in. Without a launcher both run here.
# With AGENT_FACTORY_PYTEST_JUNIT_DIR set (the pre-review does) each lane leaves a junit file there, which the
# standards gate reads instead of running the suite again. Extra arguments narrow the run, so then none is left.
set -euo pipefail
# The project root: the repository's, or a subdirectory of a workspace repository (projects/<name>/).
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Read once and taken out of the environment: the suite's own tests of this script run it again.
slow_here=${AGENT_FACTORY_SLOW_LANE_HERE:-}
unset AGENT_FACTORY_SLOW_LANE_HERE

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
  # On this 4-CPU pod the fast lane is the dearer one (about 900s here, against about 660s for the slow one), and
  # the two together here run past the checks' 900s limit, so the fast lane is the one that must get a child.
  if [[ $lane == fast && -n ${AGENT_FACTORY_TEST_LANE:-} && ${AGENT_FACTORY_SLOW_LANE:-} == child ]]; then
    [[ -z $record ]] || junit=(--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    # Spot first, then on-demand, and that twice; only then here. --no-local makes the launcher exit 75 when
    # neither placed, so the fallback below is this script's.
    local code=75 attempt
    for attempt in 1 2; do
      code=0
      "$AGENT_FACTORY_TEST_LANE" --on-demand --no-local ${junit[@]+"${junit[@]}"} -m "$marker" "$@" || code=$?
      [[ $code -eq 75 ]] || break
      echo "fast lane: no capacity on attempt $attempt" >&2
    done
    if [[ $code -eq 75 ]]; then
      echo "fast lane: running here, the child action could not be placed" >&2
      [[ -z $record ]] || junit=(--junitxml "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
      code=0
      PYTHONPATH=src uv run python -m pytest tests -q -m "$marker" ${junit[@]+"${junit[@]}"} "$@" || code=$?
    fi
    [[ $code -eq 0 || $code -eq 5 ]]
  elif [[ -z $slow_here && -n ${AGENT_FACTORY_TEST_LANE:-} && ${AGENT_FACTORY_SLOW_LANE:-} == child ]]; then
    [[ -z $record ]] || junit=(--junit "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    # The slow lane tries spot once (two minutes at most), then the launcher runs it here: 120s plus its ~660s
    # here still fits beside a fast lane in a child, where waiting out on-demand too would not.
    "$AGENT_FACTORY_TEST_LANE" ${junit[@]+"${junit[@]}"} "$@" || [[ $? -eq 5 ]]
  else
    [[ -z $record ]] || junit=(--junitxml "$AGENT_FACTORY_PYTEST_JUNIT_DIR/$lane.xml")
    PYTHONPATH=src uv run python -m pytest tests -q -m "$marker" ${junit[@]+"${junit[@]}"} "$@" || [[ $? -eq 5 ]]
  fi
}

pytest_lane fast "not slow" "$@" &
fast=$!
# The slow lane (benchmark hidden checks: real Terraform, pytest-in-pytest) costs about two thirds of the suite's
# CPU. It runs when asked for (through the launcher, or here), and always in CI's slow job; beside a fast lane
# also running here, on a 4-CPU pod, it would hold these checks past their time limit.
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
