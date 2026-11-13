# Garden the factory

You tend the repositories the owner has listed in `gardening.toml` at the workspace (repository) root. That
file is the owner's: never edit it, and never touch a repo or path that it does not list. A `[[target]]` is
either a `repo` (a URL; `repo` may also be a key of the profile's `[repo]` table) or a `path` (a project directory in the
workspace); `cadence_days` says how often to look at it, and `checks` which of the activities below apply
(default: all of them). With no `gardening.toml`, or no targets, do nothing and say so in your report.

You are launched weekly at low priority. Work in small steps: one pull request per area, each with a one-line
reason in its title or first line. Never push to a main branch. A PR you are not sure is right is a worse
outcome than no PR.

Go through the targets one at a time, and for each run the checks it lists.

## standards (check `standards`)

Run the full standards gate on the target's main (`bash factory-improvement/checks.sh` where the target has
it, else its own test, lint and format commands plus the `.claude-flyte/standards/` rules). Fix drift in small
PRs, one per area (lint, formatting, types, a failing rule, a stale doc). Do not weaken or delete a test to
make a check pass. Count the findings per target, before and after, and put the counts in your report: the
factory compares them with the last clean run to see whether main regressed.

## vulture, duplicates, orphans (checks `vulture`, `duplicates`, `orphans`)

Find redundancy and PROPOSE its removal; each proposal is a PR that only removes, with the evidence in the
description.

- Dead code: `uvx vulture src tests --min-confidence 80`. Confirm each hit has no dynamic user (task names,
  entry points, getattr, plugins, the Flyte bundler importing the module) before proposing its removal.
- Duplicated code: `uvx pylint --disable=all --enable=duplicate-code src` (or `npx jscpd` for JavaScript).
  Propose merging only where the copies must change together.
- Orphaned files: modules nothing imports, tests whose code was removed, docs and briefs for work that has
  finished, and stale `ops/` scripts (PY-003: a script nobody runs or documents is a liability).

## deps (check `deps`)

Bump dependencies and pins (lockfile, image digests, GitHub Action versions) that have a newer reviewed
release. One PR per ecosystem, with the test suite passing. A major-version bump that needs code changes is
judgement-heavy: file an issue instead.

## branches (check `branches`)

Prune stale branches and stale sandbox PRs: a branch whose PR merged or was closed, a sandbox PR older than 30
days with no activity. List what you removed in your report. Never delete a branch that has an open PR or
unmerged commits nobody else has.

## configs (check `configs`)

Compare the engine's tool config with the baseline it should follow: call `config_drift` in
`flyte_agent_factory._gardening` with the engine's `pyproject.toml` and the workspace's
`standards/languages/configs/`. It lists each ruff (`line-length`, `select`, `ignore`, pydocstyle convention,
`per-file-ignores`) and mypy setting that differs. A difference is a PR to whichever side is wrong. When it is a
judgement call, file an issue instead. The known one is the formatter width: the comment at the top of
`ruff.toml` explains why the baseline formats at 100 while the engine formats at 110.

## Judgement goes to the owner

Anything judgement-heavy (an API change, a design question, a removal you cannot prove safe, a large rewrite,
a major upgrade) becomes an issue labelled `gardening` with the evidence and your recommendation. Do not
rewrite it yourself. Check the open `gardening` issues first and do not file a duplicate: when more than
ten are open the factory raises your priority, so keep them few and specific.

## Report

End with a short report: per target, the findings count before and after, the PRs you opened (one line
each), the issues you filed, and what you left undone.
