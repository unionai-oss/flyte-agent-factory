# checkpoint-policy-rebase

This folder carries checkpoint-and-policy-keys (unionai-oss/flyte-agent-factory #45 and #50, agent-workspace#126)
onto upstream 5b1cb6f (`promote.toml` base). It also adds the two follow-ups from that project's review.

## What was carried

The diff of `projects/checkpoint-and-policy-keys` from its import (51cceff9, upstream e28963a) to the merge
(516cfb33), without `checks.sh` and `promote.toml`, applied onto this folder unchanged. Every hunk applied
cleanly; `_names.py` and `tests/conftest.py` needed `git apply --3way` only because upstream context lines had
moved, and their added and removed lines are identical to the original's. The behaviour reviewed in #45 and #50 is unchanged, and so are its tests.

## Adaptations to upstream 5b1cb6f

Two of the carried tests failed on 5b1cb6f. In each case new upstream code was not yet classified. Neither test
was weakened: both still assert exact set equality.

1. **`AGENT_FACTORY_CLAUDE_TUI`** (`_author.TUI_FALLBACK_VAR`, upstream issue #219). This is the operator's switch
   back to the pty TUI author. The docs tell an operator to set it for a run (`docs/headless-author-session.md`),
   and the session reads it through `_author.is_tui_fallback`.
   - It is not a policy key, because it is not a role's model, effort or engine.
   - It is not internal, because no factory machinery sets it.

   It reaches a pod the way the tooling variables do: `agent._shell_policy_env` forwards every `AGENT_FACTORY_*`
   variable from the deploying shell except those in `POD_FIXED`. It is therefore now listed in
   `_names.TOOLING_FIXED`, and the comment on that list says so. It also has a row in the environment-variable
   table in `docs/architecture.md`. Listing it there does not change runtime behaviour, because nothing filters on
   `TOOLING_FIXED`.
2. **`AGENT_FACTORY_RESTART_APPS`** (`_deploy_apps.RESTART_APPS_ENV`). `ops/deploy_factory.py` sets it, after first
   popping any inherited value. It does this for its own deploy child, which `_deploy` reads. That is the factory's
   deploy tooling talking to itself, never a profile or a policy, so it is listed in the test's
   `INTERNAL_VARIABLES` under "Deploy tooling", next to `AGENT_FACTORY_DEPLOYER_ENV`.
3. **`('_factory.py', 'prepare_launch')`**. The hit was `_seal_launch.resolve(seal, profile)`, upstream's sealed
   launch, which only shares the name `resolve` with `_policy.resolve`. It takes a sealed spec and a profile,
   returns (spec JSON, repo URL), and reads no policy key. Adding it to `DYNAMIC_PURPOSE_SITES` would claim that a
   `_policy` table lists its keys, which is false.

   Instead, the scanner now skips calls listed in `NOT_POLICY_CALLS`, which has one entry,
   `("_seal_launch", "resolve")`. A call qualifies only if both its module and its function name match an entry.
   A new test, `test_every_call_left_out_for_sharing_a_name_is_still_called`, keeps that list exact: an entry that
   is no longer called fails it. Any other `resolve` with a computed argument, including a bare one or one on
   another module, is still reported.

## Follow-ups from the original review

1. **A NaN or infinite `last_ok_at` crashed `report_checkpoint_fields`.** `json.loads` accepts `NaN` and
   `Infinity`, and `int(now - inf)` raises. `_factory_status.report_checkpoint_fields` now treats a value that is
   not finite like any other garbled one (None). Test:
   `tests/test_checkpoint_staging.py::test_a_last_good_checkpoint_time_that_is_not_finite_does_not_fail_the_status`
   (NaN, +inf, -inf, round-tripped through JSON).
2. **The unknown-key hint for `CRAFT_EFFORT` suggested `AUTHOR_EFFORT`.** The judges share one Claude setting,
   `JUDGE_MODEL` / `JUDGE_EFFORT`. A per-concern Codex key exists, but no per-concern Claude key does. So
   `_policy.suggest_nearest_key` now answers `<CONCERN>_MODEL` and `<CONCERN>_EFFORT` with `JUDGE_MODEL` and
   `JUDGE_EFFORT`, for every concern in `JUDGE_CONCERNS`. The same misdirection also hit the other two concerns,
   whose hint was `CORRECTNESS_ENGINE` and `COMPLETENESS_ENGINE`. Test:
   `tests/test_policy_keys.py::test_a_judge_concern_s_claude_setting_points_at_the_one_the_judges_share`
   (3 concerns × MODEL/EFFORT, through `_profile.parse`).

All nine new test cases fail with the two source fixes reverted and pass with them.

## Checks

- `projects/checkpoint-policy-rebase/checks.sh` runs the full offline suite and lint. No test is skipped or waived
  by this change. Every test this diff adds or touches passes, including both `test_checkpoint_staging` copy-race
  tests (18/18 in that file). The last full run's one failure, in an upstream test, is described below.
- NOT RUN: anything that needs live infrastructure (deploys, live model tests behind
  `AGENT_FACTORY_LIVE_TESTS`). This environment has no cluster credentials beyond the factory's own.
- Last full `checks.sh` run here: 11898 passed, 28 skipped (the suite's own, none added), 1 failed. The failure was
  `tests/test_state_store.py::test_many_processes_writing_different_keys_all_succeed` ("push rejected 25 times").
  That test and `_state_store.py` are upstream 5b1cb6f, untouched by this diff. It is git-push contention among
  many processes under full-suite load on a 4-CPU pod: the test passed in the previous full run and 5 of 5 times on
  its own. It is left unchanged rather than loosened. Lint (ruff check, ruff format) and mypy pass.
