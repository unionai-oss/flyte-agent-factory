# Factory model evaluation

Run from the repository root with its environment (`uv sync --group dev`), using
`PYTHONPATH=src uv run python -m ops.model_eval`. Use the same profile and API-key
environment as `ops/operate.py`. No command attaches to a parent Flyte run.

```bash
# Read-only; --days lists operated project tasks in the selected profile.
PYTHONPATH=src uv run python -m ops.model_eval costs RUN1 RUN2 --output /tmp/costs
PYTHONPATH=src uv run python -m ops.model_eval costs --days 7
# Entirely offline alternative, taking saved HTML or timeline text:
PYTHONPATH=src uv run python -m ops.model_eval costs --report-file /tmp/report.html
PYTHONPATH=src uv run python -m ops.model_eval collect RUN1 RUN2

# These two commands spend model allowance and cluster resources:
PYTHONPATH=src uv run python -m ops.model_eval --config /tmp/eval.toml replay
PYTHONPATH=src uv run python -m ops.model_eval --config /tmp/eval.toml grade
PYTHONPATH=src uv run python -m ops.model_eval report --ladder codex-low sonnet opus

# Matched coordinator experiments, launched separately:
PYTHONPATH=src uv run python ops/operate.py launch brief.md --name compact-off
PYTHONPATH=src uv run python ops/operate.py launch brief.md --name compact-on \
  --policy COMPACT_BETWEEN_ROUNDS=true
```

`costs` and `report` write Markdown and JSON. `collect` writes cases below
`ops/model_eval/cases/RUN/ACTION.json`; it copies no repository snapshots. Replay
appends `results.jsonl`; grade appends `grades.jsonl`. Paths can be overridden.
Keep one writer per results/grades file. All records are scrubbed for common
credential patterns; do not put credentials into briefs or patches. Access to
workdir URIs still requires the original artifact permissions and retention.

Example TOML (omit `[[settings]]` to use the default matrix):

```toml
concurrency = 3
task = "YOUR-SUBAGENT-ENV.subagent"
codex_auth = "/home/YOU/.codex/auth.json"
codex_models = ["gpt-5.4-mini"] # fallback if safe model discovery is unavailable

[budget]
codex_five_hour = 70
codex_weekly = 80
claude_five_hour = 70
claude_weekly = 60

# USD per million: input, output, cache-write, cache-read. Overrides defaults.
[prices]
"claude:sonnet" = [3, 15, 3.75, 0.3]

[[settings]]
name = "codex-low"
engine = "codex"
model = ""
effort = "low"

[[settings]]
name = "sonnet"
engine = "claude"
model = "sonnet"
effort = "high"
```

The default task is the last profile slot’s subagent task; configure `task` to select another. Default replay settings are Codex's unpinned default at high/medium/low, the
first available small/mini model at medium, and Claude opus/high, sonnet/high,
haiku and fable. Supported model names vary by account. Codex discovery uses
`model/list` through the same disposable home and access-token expiry guard as
`_meter.read_codex`, with no model request or token refresh; configured models
are the fallback. An unavailable Claude alias is recorded as a failed run.
Every case additionally gets three fresh original-setting calls (a baseline
and two extra repetitions). An existing completed `(case, setting, rep)` is
skipped. Failed calls can be retried; interruptions after remote submission but
before local append can leave an unrecorded remote run, so inspect the console
before resuming after a process crash. The resume key assumes cases/settings
are immutable: use a separate results path when changing their content.

Every replay/grader submission checks live operated projects and fresh meter
readings. Unknown or stale windows stop launching, as do percentages at the
configured thresholds. `--allow-live` relaxes only the live-project guard.
Claude checks the profile's work token; Codex reads local auth. Remote Claude meter probes themselves consume
small cluster resources, but no model allowance. Up to `concurrency` calls
already in flight may finish after a guard trips; this is a launch threshold,
not a hard dollar cap. Replay bounds each queue attempt to five minutes and tries
spot then on-demand. The guard runs once per logical call: an allowed spot attempt's on-demand fallback is not
checked again. Grade is sequential, independently guarded, and its usage is stored separately from candidate cost. Cache and task-result
reuse are different: replay disables Flyte run caching, but provider prompt
caching can still affect fresh-session API-equivalent cost.

Costs use the last retained cumulative usage row, never the sum of cumulative
rows. Coordinator token categories are rounded and include in-pod helpers.
The author model defaults to an explicit sonnet assumption (`--author-model`
overrides it); mixed-model author sessions cannot be priced precisely from the
report. Price snapshots and sources are in `data.py`; review them before a real
experiment. Fable has no assumed public price. Unknown prices remain null (the report warns with the count of unpriced calls). A config price key for a Codex model not named `gpt-*` must be written `codex:NAME`, or it is filed under `claude:`.
Model ids are normalised once: an existing engine prefix is stripped and re-added, so the Codex trailer's `codex:default` and a config key of `fable` or `claude:fable` all reach the same row. Full Claude IDs use a family prefix/alias map: claude-opus-* and opus map to claude:opus; claude-sonnet-*, sonnet and sonnet[1m] map to claude:sonnet; claude-haiku-* and haiku map to claude:haiku; fable maps to claude:fable. Exact keys override family rates. Family rates assume the table snapshot; override full IDs when prices differ. Each Claude modelUsage entry is priced separately from its own token counts. An unknown part makes the total null, with known_subtotal_usd retained separately. All calls, including original calls, use these price keys. CLI cost is retained separately and never used as a fallback. Codex uses the usage trailer model when available; otherwise codex:default assumes the cited gpt-5.3-codex rate and records price_assumption=true. Configure claude:fable when a defensible rate is available. These are
not subscription charges. Cache writes use the five-minute rate, not the
one-hour rate. Reports retain only 300 timeline rows. Role totals often expose
only aggregate tokens and CLI spend; those rows retain cli_cost_usd but have null cost_usd because a token mix cannot be inferred. New usage rows include token categories and engine/model identities for table pricing, including Codex calls whose CLI reports $0.00. Fallback rows preserve the exact
reset text; spend is available by fallback role suffix, not by individual
switch. Delegate starts/finishes give retained job counts/durations/file counts;
missing rows mean incomplete counts. Blocking counts are read by round when
present. Idle/review/waiting duration observations over five/sixty minutes are
cache-expiry proxies; they cannot prove a per-turn cache miss, nor establish
the size of a cold reread. A separate sensitivity scenario assumes 100k context
tokens per retained >5m observation and prices a cache write instead of a cache
read; it is not a measured penalty and observations can overlap. Totals state how many projects had known usage.

Collection identifies reviewer/planner/arbiter/contract prompts by their
factory signatures, and delegates by their writable role or delegation prompt.
It preserves inference evidence, judge identity and review round when present,
exact task inputs (apart from secret scrubbing), output and usage trailers.
Reviewer ground truth requires `replay --reference`: the original review plus
one Claude fable/high and one Codex default/xhigh review are merged by a blind adjudicator.
Configure the panel with `reference_panel` settings: exactly one member per model family.
Later reviewer rounds and arbiter outputs supply additional evidence. Only
confirmed blocking findings count; disputed findings count neither way. The reference verdict is FAIL when at least one blocking finding is confirmed, otherwise PASS. Grade verdict agreement is computed against that verdict.
The panel must be disjoint from the original setting and every candidate setting of the reviewer cases being referenced (other purposes are not checked); reference creation rejects overlap. Panel outputs are references only.
The reference persists each paid call immediately and reuses completed calls after interruption. Reviews are shuffled using a recorded seed and recorded index order before adjudication. Fenced JSON is accepted; malformed adjudications record a case error and allow later cases to proceed.
The reference stores the two review costs and adjudicator cost separately, each call tagged `panel` or `adjudicator`;
unknown prices remain null. This adds three model calls per reviewer case.
Collection passes no operational timeline rows into grading.

Cross-family judging. A model must not judge its own family's work, so the families are
the engines: `claude` (every opus/sonnet/haiku/fable variant) and `codex` (any Codex model).
The adjudicator alternates family per reviewer case by position in the sorted case list (opus/high,
then Codex default/high) and the choice is recorded in `reference.adjudicator`. Each candidate,
including the original-setting repetitions, is graded by the family other than its own
(Claude candidates by Codex, Codex candidates by Claude); grade rows record `candidate_family`,
`grader_family` and `adjudicator_family`. This removes the same-family grader, not shared
blind spots between graders. The report shows verdict agreement per candidate family x
adjudicator family. Grader bias is measured, not assumed: on a random sample of graded rows
(`grader_bias_sample`, default 0.2, chosen by a seed derived from the row key so a resumed run
samples the same rows) the candidate's own family also grades it, stored as `cross_check`, and the
report gives the same-family minus other-family correctness rate. Those extra grades cost
allowance and appear under evaluation spend.

Reading family agreement. In the reviewer cases where the adjudicator is Claude (every other case) it runs
opus/high, the same configuration as the opus baseline, and the Claude adjudicator/grader therefore shares
blind spots with opus candidates there. Read agreement per family pair: Claude candidates against a Claude
adjudicator, and opus in particular, are the optimistic cells; Claude candidates against the Codex adjudicator
and Codex candidates against either are the cleaner comparison.

Failure isolation. A remote failure in one reference or grade call is recorded and the run
continues; only budget stops end the run. Reference failures are counted per case in
`reference.failures`, grade failures as `error`/`invalid` rows with `attempts`. They are retried
on the next run, and a success replaces the failed row, so there is one row per key. After
`max_failures` (default 3) failed attempts the case or row is marked `skipped` with the reason
and is not retried. Only exception type names are stored. A failure of the budget check itself (for example a meter outage)
is a budget stop, not a case failure: the run ends and nothing is counted against any case.
A grade row that replaces an invalid one carries the earlier paid calls in `prior_attempts`, and evaluation
spend counts them; panel and adjudicator calls are all kept in `reference.calls`, invalid ones included.

Grader and adjudicator evidence is constructed without setting, model, engine, effort or usage metadata. Briefs and patches retain their exact content, even when source mentions these words. Candidate prose redacts only explicit, case-sensitive identities (Claude, Anthropic, Opus, Sonnet, Haiku, Fable, Codex, OpenAI and GPT model names); code fences and diff hunks are preserved. Review grades record verdict agreement,
matched/unmatched blocking findings and classified extras. Planner grades
assess finding coverage and overruled proposals. Delegate replays retain the
returned workspace URI and patch file/count. Patch files are stored separately with only high-confidence credential formats scrubbed. Any changed patch has patch_scrubbed=true and is excluded from mechanical results. The grader receives
the original workdir and patch, extracts the delegation archive if needed,
applies the patch in isolation and runs repository checks. Missing artifacts
or unrunnable checks must produce partial, never pass. A separate credential-free top-level container task downloads the original workspace, applies the patch,
and runs its declared factory checks. It runs in the agent image (`flyte_agent_factory.agent_image()`, which has git and the project's dependencies), which `Backend.mechanical` sets on the task at submission time because `mechanical.py` itself cannot import `flyte_agent_factory`; the pod's `_F_IMG_ID` is only the bare Flyte base image and is not used. Include ops/model_eval/mechanical.py when deploying. Collection records the parent project name, so named cases run <project>/checks.sh exactly as the factory does; unnamed legacy cases fall back to checks.sh or .claude-flyte/checks.sh. The container task runs checks with the image's own environment, as the factory's `_run_declared_checks` does,
minus the model-billing/credential variables and anything credential-named, with a scratch HOME, so `python -m pytest`
resolves as in production. The task must not import `flyte_agent_factory` (the image does not install it; the bundle
carries `src/` only as files), so the few lines it needs are copied and pinned to the originals by a test.
`grade --local-mechanical` runs the same harness on the operator machine inside a fresh `env -i` (only PATH and a
scratch HOME) in a new session. That is not a sandbox: it is still the operator's user, so other processes'
`/proc/*/environ` and every file that user can read remain reachable. Prefer the container path for untrusted patches. A container failure is recorded per case and never aborts other grades. Missing checks remain unknown. `report` lists errored checks under Problems, and exits with status 2 (after writing the report) when a requested ladder has no usable cases, so an all-errored delegate ladder is never silently empty. Its result,
not the grader's tests_passed judgement, controls delegate escalation.

The Markdown report has candidate, ladder, evaluation-spend (panel, adjudicator, grader), family-agreement,
grader-bias and failure sections; money is shown to two decimals, rates as percentages, with n, standard
deviation, unknown-cost count and token columns. `report` also reads `--cases` for the panel and adjudicator spend.

Quality summaries include agreement, blocking recall, extras, correct outcomes,
cost per correct outcome, tokens and wall time. Standard deviations across
calls combine case difficulty and stochastic variation; the separate
within-case correctness deviations use the original-setting repetitions.
Absent grades and unknown costs are counted explicitly and excluded from
metrics that require them. Single samples have no estimated standard deviation.
Grade cost is recorded but excluded from candidate cost per correct outcome and from the production ladder.

Ladders are paired, using rep 0 only, excluding cases missing any rung, original
baseline, grade or cost. Delegate escalation uses the reported repository-test
failure signal; final grader correctness measures its blind spots but does not
control escalation. Reviewers use verdict disagreement with an independent
original-setting reviewer. That second opinion is charged once per case; agreement does not guarantee recall and can share blind
spots. The analysis compares these routes with each case's fixed original
setting, without further calls. This is a counterfactual estimate, not a live
routing policy, and it does not model correlation or queue contention.

Compaction is opt-in via `AGENT_FACTORY_COMPACT_BETWEEN_ROUNDS` or a profile's
`[policy] COMPACT_BETWEEN_ROUNDS = "true"`. Rejection and passed-with-findings
messages first submit `/compact` with preservation instructions. The supervisor
requires new terminal output followed by the pump's quiet interval before
sending findings. It records elapsed time; this session adapter exposes no
reliable context-size measurement. One attempt is allowed per round. A 15-minute timeout delivers findings uncompacted
with a timeline warning. Waiting runs as a task while the pump handles human direction. The terminal signal is
a proxy: validate it with the deployed Claude Code version before comparing
real runs. The flag defaults off.

Offline coverage is in `tests/test_model_eval.py`, `tests/test_nudge.py`,
`tests/test_operate.py` and the existing meter/factory tests. Run the suite and
lint commands from the brief. Implementation test results are a handoff to QA,
not independent QA approval; no live experiment was run during implementation.
