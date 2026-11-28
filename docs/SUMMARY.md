# Claude Code pin bump: 2.1.287 -> 2.1.326

This is a hand bump ahead of #117 (the measured-bump pipeline), so the factory's `fable` alias reaches Fable 5.2 now.

## Choice of release

On 2026-11-27 npm reported the dist-tags `stable` 2.1.326 (published 2026-11-16) and `latest`/`next` 2.1.332. The pin's comment says it follows `stable`, so `CLAUDE_VERSION` is 2.1.326, not 2.1.332; the comment in `_images.py` now records the channel and date. The 2.1.283 floor is unchanged, still documented in that comment, and now asserted by `tests/test_images.py::test_claude_cli_pin_is_not_below_the_floor`. `CODEX_VERSION` (0.163.0) is out of scope and unchanged.

## Live verification (subscription credential, no API key)

All live calls ran the npm 2.1.326 linux-x64 binary (`--version` checked) in a child action of this run holding the slot's subscription token, with the user settings written by `_auth.write_settings`, no tools.

**Alias resolution** (2026-11-28, prompt "Reply OK", `--output-format stream-json`). For each alias the init model, every assistant `message.model` and the sole `modelUsage` key agreed:

| Alias | Resolves to | `is_error` | `total_cost_usd` (reported) |
|---|---|---|---|
| `fable` | `claude-fable-5-2` | false | 0.057136 |
| `opus` | `claude-opus-5-5` | false | 0.017704 |
| `sonnet` | `claude-sonnet-5-5` | false | 0.008564 |

An earlier subscription probe on 2026-11-27, through a factory delegate, gave the same three ids with `is_error` false (reported costs $0.0539218, $0.0161618, $0.0077929). These costs are the CLI's notional figures; the calls were on the subscription. Spend on the API key: $0 (see History).

**Instruction loading** (2026-11-28, model `sonnet`). From a scratch folder holding `CLAUDE.md` ("The first codeword is PELICAN.") and `AGENTS.md` ("The second codeword is HERON."), the prompt "Quote every codeword given in your project instructions." was answered with both PELICAN (attributed to `CLAUDE.md`) and HERON (attributed to `AGENTS.md`). This is the manual form of the opt-in live test `tests/test_auth.py::test_claude_loads_both_claude_md_and_agents_md`, and shows the `agents-md@builtin` plugin option the engine writes still works on 2.1.326.

## Compatibility with what the engine parses

- A real 2.1.326 process started with the engine's stream flags accepted an `initialize` control request and returned a successful `control_response`.
- Against a local mock Messages service, the real 2.1.326 CLI accepted a user turn, honoured a `can_use_tool` denial and a matching Bash deny rule, and blocked a Bash call through a structured `PreToolUse` hook denial. That hook denial appeared only in the final `result`, not as a `system/permission_denied` event, so the engine would not have reported it. `_stream_process.py` now records result-only denials and de-duplicates them by `tool_use_id` against those already seen in the turn; tests in `tests/test_stream_process.py` cover hook and policy denials. The mock does not cover real service responses, queued turns or interruptions.
- Version references updated: `tests/test_process.py` and `tests/seal_corpus.py` (2.1.287 -> 2.1.326). Remaining `2.1.287` mentions are under `docs/` (historical transcripts and notes), left alone as the brief asks.

## Checks

Full `checks.sh` run on 2026-11-28, on the final source including the floor test, with both pytest lanes in child actions: exit 0. Fast lane 13,350 passed, 28 skipped (listed below); slow lane 175 passed, 1 xpassed; Ruff check and format, mypy (577 source files) and the packaged-skill checks passed.

No test was removed, skipped, or loosened, and no waiver was added.

## Remaining skips

Every skip below existed before the bump: the five affected test files and the CI workflow are unchanged from upstream baseline `f83200e9dd2676b80cc9fee08e1486b06fc4fa41`, and the upstream CI setup skips the same cases (it does not install the ML dependencies, supply private profiles or the engine-as-target harness, or enable live tests; for nginx the hosted runner's binaries were not verified). Upstream outcomes are inferred from the workflow and test conditions; the baseline CI run's JUnit artifact could not be read without authorization. They are environment conditions, not unmet requirements.

- `tests/test_profile.py::test_shipped_profiles_parse[gke-personal]`: gke-personal.toml is private and not present.
- `tests/test_profile.py::test_shipped_profiles_parse[union-work]`: union-work.toml is private and not present.
- `tests/test_profile.py::test_the_union_work_profile_still_describes_what_is_deployed`: union-work.toml is private and not present.
- `tests/test_profile.py::test_union_work_slots_reach_the_factory_repo_with_its_own_key`: union-work.toml is private and not present.
- `tests/test_profile.py::test_union_work_deploys_itself`: union-work.toml is private and not present.
- `tests/test_personal_browser.py::test_unauthenticated_websocket_upgrade_to_view_is_refused`: needs an nginx binary (set NGINX_BIN).
- `tests/test_ci_lanes.py::test_terminating_the_checks_script_stops_its_launcher[NOTSET-child]`: got empty parameter set for (script).
- `tests/test_ci_lanes.py::test_terminating_the_checks_script_stops_its_launcher[NOTSET-here]`: got empty parameter set for (script).
- `tests/test_ci_lanes.py::test_a_lane_whose_upload_failed_fails_the_checks_without_a_local_run[NOTSET]`: got empty parameter set for (script).
- `tests/test_ci_lanes.py::test_an_unplaced_slow_lane_runs_here_while_the_fast_lane_tries_a_child_again[NOTSET]`: got empty parameter set for (script).
- `tests/test_auth.py::test_claude_loads_both_claude_md_and_agents_md`: live, billed model call: set AGENT_FACTORY_LIVE_TESTS=1 with the image's `claude` binary.
- `tests/test_bench_task_ml_pipeline.py::test_the_reference_passes_the_hidden_check`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_the_start_fails_the_hidden_check`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_the_hidden_check_rejects_other_incorrect_solutions[undecorated]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_the_hidden_check_rejects_other_incorrect_solutions[uncached]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_the_hidden_check_rejects_other_incorrect_solutions[bad_model]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_equivalent_pipelines_pass[cache_constant]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_equivalent_pipelines_pass[shared_decorator]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_equivalent_pipelines_pass[another_module]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_equivalent_pipelines_pass[numpy]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_attacks_are_rejected[dummy_print]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_attacks_are_rejected[ignore_eval]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_attacks_are_rejected[training_data]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_attacks_are_rejected[existing_tests]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_tasks_in_a_guarded_main_module_pass`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_round_two_attacks_are_rejected[trivial_tasks_and_knn]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_round_two_attacks_are_rejected[prints_a_perfect_score]`: ml-pipeline runtime checks require scikit-learn installed offline.
- `tests/test_bench_task_ml_pipeline.py::test_qa_round_two_attacks_are_rejected[other_model]`: ml-pipeline runtime checks require scikit-learn installed offline.

Extra evidence for some of these, outside the full run:

- The nginx case passed (1 passed) in an unprivileged child with nginx 1.22.1 extracted from the Debian package.
- `tests/test_bench_task_ml_pipeline.py` passed (23 passed) in an isolated 8 GiB worker with scikit-learn 1.6.1 and pandas 2.2.3.
- The opt-in live auth case was exercised manually through the instruction-loading probe above and passed.

## History (superseded attempts)

- The first subscription alias attempt could not start: the factory subagent action failed with `ImagePullBackOff` on an unqualified generated image name. The owner then authorised an API-key fallback capped at $3. Its three calls reported the correct init models but returned `is_error: true`, a `<synthetic>` message model, empty model usage, and $0 cost, so they proved nothing about model responses. The owner then pointed to the subscription credential, and the subscription probes above replaced them. Total API-key spend: $0.
- A first instruction-loading probe (2026-11-27) started the session without the `write_settings` user settings, so `AGENTS.md` could not load. Its result (neither codeword seen in the logged line) was inconclusive. The 2026-11-28 probe above replaced it.
