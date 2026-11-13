# Deployment profiles

A profile is one TOML file holding every value that differs between deployments: the Flyte
backend, project and domain, the project repository, the agent slots, secret names,
role policy, and the factory MCP settings. Nothing deployment-specific is hard-coded in
`src/`, `ops/`, `docs/`, `skills/` or the README (`tests/test_profile.py` enforces it).

Starting out on a personal plan? Copy one of these here instead; both are minimal:
[example-claude-personal.toml](example-claude-personal.toml) (Claude Pro/Max) or
[example-codex-personal.toml](example-codex-personal.toml) (ChatGPT Plus/Pro). The steps are in
[../docs/quickstart.md](../docs/quickstart.md), the secrets in [../docs/secrets.md](../docs/secrets.md).

Guided setup (`ops/bootstrap.py`, see [../docs/quickstart.md](../docs/quickstart.md)) needs none of this: it writes
`profile.toml` at the root of your workspace and deploys with `--profile <clone>/profile.toml`. The `profiles/<name>.toml`
form below is for an engine checkout you deploy by hand.

```bash
cp profiles/example.toml profiles/mine.toml      # edit it
export AGENT_FACTORY_PROFILE=mine               # a name in profiles/ (mine or mine.toml), or a path
PYTHONPATH=src uv run python ops/deploy_work.py                                 # or: python ops/deploy_work.py --profile mine
```

`--profile <name or path>` is accepted by the ops tools (`operate.py`, `watch.py`,
`deploy_work.py`, `deploy_factory.py`, `deploy_status.py`, `wait_*.py`) and
overrides `AGENT_FACTORY_PROFILE`, and may come before or after `operate.py`'s subcommand.
The direct profile loader reads the same variable. A value without a `/` is always a name under
`profiles/`; write `./mine.toml` for a file in the current directory.

**A local default.** With neither the variable nor `--profile` set, the tools use the profile
named in `profiles/.default` (git-ignored):

```bash
echo <name> > profiles/.default
```

This is also what keeps a supervisor deployed before profiles existed working: it has neither
`AGENT_FACTORY_PROFILE` nor the profile text, so give its checkout a `profiles/.default`.
With nothing configured the error says this too.

**Real profiles are private.** `.gitignore` ignores `profiles/*.toml` except `example.toml`.
Keep yours in a private config repo or a local file, and point at it with `--profile <path>`
or `AGENT_FACTORY_PROFILE=<path>`. To select private profiles by name instead, set
`AGENT_FACTORY_PROFILES_DIR` to the folder holding them: names and `.default` are then looked
up there rather than in `profiles/`. Any `*.toml` here other than `example.toml` is one
deployment's own profile and does not belong in a published copy.

## How values reach a pod

A pod's code bundle carries `.py` modules, not data files, so a pod cannot read `profiles/`.
At deploy, the profile's text is put into the environment definition as
`AGENT_FACTORY_PROFILE_TOML` (`_profile.pod_env()`), and `_profile.current()` reads it back
from there. Everything else a pod needs (`AGENT_FACTORY_*`, `AGENT_FACTORY_<ROLE>_*`,
`FLYTE_MCP_*`, `FACTORY_MCP_*`) is set the way it always was, as environment variables on the
deployed task. Environment variables that already existed (`OPERATE_PROJECT`, `OPERATE_REPO`,
`OPERATE_CAP`, `FLYTE_MCP_PROJECT`, `FACTORY_MCP_PUBLIC_URL`, ...) still override the profile.

## Schema

| Table / key | Required | Meaning |
|---|---|---|
| `name` | yes | Profile name. |
| `[backend]` `kind` | yes | `union` or `oss`. Recorded for docs and guards. |
| `[backend]` `endpoint`, `project`, `domain` | yes | Flyte endpoint host, project, domain. |
| `[backend]` `org`, `console_host`, `signin_host`, `config` | no | Org; console host (reports, console login in the browser); its sign-in host; the Flyte config file passed to the CLI. |
| `[repo]` `project` | yes | The workspace: the repository projects are pushed to by default. `[secrets] deploy_key` reaches only this one; another `[repo]` entry needs a `[repo_keys]` key. |
| `[repo]` `factory` | no | The engine repository. It is the default `[deploy] repo`, and the benchmark's default engine and baseline (`[bench]` requires it unless `[loop]` names the engine's repository). Changing the engine is not a launch on this entry: it is a workspace project, imported and promoted as a pull request ([Promotion](../docs/architecture-repos.md#promotion)). |
| `[[slots]]` `env`, `subagent_env` | no; without any, the profile deploys one shared environment (see "Shared environment: `[concurrency]`") | Agent environment name (its task is `<env>.project`) and its sub-agent environment. Each must be unique across slots. Every slot runs on `[secrets] claude_token` (and `codex_token` when set); a slot carries no credential of its own. |
| `[slots.policy]` | no | This slot's own role policy, merged over `[policy]` when the slot is deployed. |
| `cap` | no | Projects live at once; 1 to the number of slots (default: the number of slots). |
| `[concurrency]` `max_projects`, `per_engine` | no | Only for a profile without `[[slots]]`; refused together with them. `max_projects` (integer, at least 1; default 4) is how many projects are live at once on the one shared environment; `per_engine` (`{ claude = N, codex = M }`, each from 1 to `max_projects`) also caps the projects whose author runs on that engine. See "Shared environment: `[concurrency]`". |
| `[secrets]` `deploy_key` | yes | Single-repo deploy key secret. |
| `[secrets]` `claude_token` | yes | The Flyte secret holding a `claude setup-token` token; every Claude session runs on it (see "Claude setup-token"). |
| `[repo_aliases]` `<repo key> = ["<old URL>", ...]` | no | Earlier URLs of a `[repo]` entry, e.g. `factory = ["git@github.com:you/old-name.git"]` after a GitHub rename. A launch, resume or queued request that names an old URL (ssh and https spellings match) is treated as the entry's current URL, and the slot's `AGENT_FACTORY_REPO_KEYS` maps each old URL to the entry's deploy key. Each key must name a `[repo]` key; an old URL may not be a current `[repo]` URL or be listed under two keys. |
| `[repo_keys]` `<repo key> = "<secret>"` | no | A deploy key secret for another `[repo]` entry (not `project`, which uses `[secrets] deploy_key`), e.g. `factory = "my_factory_deploy_key"`. Each must name a `[repo]` key and differ from `[secrets] deploy_key`. A slot mounts it as `GIT_DEPLOY_KEY_<KEY>` and gets `AGENT_FACTORY_REPO_KEYS` (JSON, repo URL to variable). The pod installs the key for the repo the project was launched on (the default key for any other), then removes every `GIT_DEPLOY_KEY*` variable, so a project holds exactly one key, as a file. The `pre-push` hook refuses a push to any URL other than the launch repository's and checks nothing else. |
| `[secrets]` `gateway_key`, `api_key`, `codex_token` | no | LLM gateway key; a project's Anthropic key (mounted as `PROJECT_ANTHROPIC_API_KEY`); Codex access token (see "Codex access token"). |
| `[policy]` `<KEY> = "<value>"` | no | Deployed as `AGENT_FACTORY_<KEY>`, e.g. `AUTHOR_MODEL = "opus"`. Applies over the same variable in the deploy shell. A key is `A-Z`, `0-9` and `_`, with no `AGENT_FACTORY_` prefix and not starting with `PROFILE`. |
| `[browser]` `personal`, `profile_uri`, `personal_env`, `exit_node`, `tailscale_secret` | no | For the in-pod browser, set `exit_node` and `tailscale_secret` together to enable [browser-only exit-node egress](../docs/browser-exit-node.md). For the personal browser, with `personal = true`, `profile_uri` and `personal_env` are required; `personal_env` names one of the `[[slots]]` envs, and projects on that slot get the personal browser. `[self_agent]` was retired (self-improvement is an ordinary workspace project, imported and promoted: [Promotion](../docs/architecture-repos.md#promotion)) and a profile that still has it is accepted with a warning and the table is ignored (pods deployed before the retirement carry it). |
| `allow_placeholders` | no | Top-level. A value containing `TODO-` is refused unless this is `true`; only a skeleton sets it. |
| `[deploy]` `auto` | no | `true` makes the `factory-deployer` task poll the `[deploy] repo` default branch every 5 minutes (every 60 minutes when `webhook_secret` is set and `[deploy] webhook = true`, with immediate push wakeups) and deploy the slots, factory, factory-mcp and itself, so a merge ships on the next tick plus build time. It skips a commit while its CI is failing or pending, or has no passing check (none, or only skipped ones). Default `false`: the trigger still ticks and does nothing. Deploys `[deploy] repo`, default `[repo] factory` (a github.com repository; set it to deploy another, such as the public engine repository, which needs no App there: the deployer reads it anonymously, deploys only a head whose CI passed, and takes its lock in `[direction] repo` instead; production deploys only that merged default branch, and a modified engine copy in the workspace is deployed only for testing and benchmarking (`factory-bench`), never as production's source; `webhook = true` only when pushes to it reach the factory webhook, that is when the App is installed there; otherwise leave it `false` so the 5-minute poll stays). Requires `[state] uri` and `[github] app_id` and `private_key_secret`. The children are deployed with the merged tree's `profiles/<name>.toml` when the tree has that profile (an invalid one deploys nothing), else with the profile the deployer pod started with; its standards are those its `[standards] dir` names, since an engine tree's own `standards/` is never a default (a `[direction]` profile instead defaults to its direction repository's `standards/`, below). **A new profile key ships with its code in one release.** The running deployer is the previous release, so it decides nothing about what is deployed: it fetches the engine at the SHA, reads the profile only for what it acts on itself (`name` and `[direction]`; every table it knows, `[direction]`, `[deploy]`, `[github]`, `[state]`, `[repo]`, `[[slots]]` and the rest, is still validated, and a top-level key it does not know is let through with a warning), and then runs that engine's own code, `python -m flyte_agent_factory._deploy_child`, from the fetched tree. That code reads the profile strictly and decides everything else: the slot recipes and their secrets, which factory environments the profile enables, and the deploy child scripts. So a release that adds `[board]` (with a new environment, or a new slot variable) and the profile commit that sets it deploy together, in one tick. The check runs before anything is deployed (2 minutes at most); a key that engine does not know either (a typo, or the engine unchanged) fails the tick naming it, deploys nothing, and counts towards the 3 attempts. The deploy itself is killed after 50 minutes, its deploy children with it, and recorded as a failed attempt. `last.json`'s `envs` are what the deploy children report having deployed; if one the engine planned is missing, the tick fails instead of recording it. Limits: only top-level keys are tolerated, so a new key inside an existing table (say `[deploy]`) still waits until the engine that knows it is deployed. An engine commit without `_deploy_child` (from before this behaviour) is refused, so rolling back past it is a hand deploy. A deployer from before this behaviour still refuses any key it does not know and still deploys with its own recipes, so the release that brings this behaviour is deployed before any new key is added. |
| `[direction]` `repo`, `profile`, `ref` | no | Read the deployed profile from a separate direction repository instead of the deployed engine tree's `profiles/<name>.toml`. `repo` (required) is a github.com repository, as `[repo] factory`; `profile` is the path inside it (default `profiles/<profile name>.toml`; relative, no `..`); `ref` is a branch (default: the repository's default branch). Requires `[deploy] auto = true` and so the `[github]` App, which must be installed on the direction repo too. Each tick resolves the engine head E (the head of `[deploy] repo`) and the direction head D, and deploys when the pair (E, D) differs from `deployer/last.json`'s `sha` and `direction_sha` (a `last.json` without `direction_sha` deploys once). Code comes from the engine tree at E; the profile and its default `standards/` come from the direction tree at D; the profile must have the same `name` and must itself carry the same `[direction]` table (the deployer redeploys itself with it; a file without it, or naming another repo, fails the tick). **Switching to a direction repo:** install the App on it, put the profile (with this `[direction]` table) there, then deploy once with `--profile <direction checkout>/profiles/<name>.toml` (`ops/deploy_factory.py` and `ops/deploy_work.py`; for a workspace made by guided setup, the bootstrap's `--redeploy` does both, see [quickstart.md](../docs/quickstart.md)); from then on the deployer follows it. Changing `[direction]` itself is again a hand deploy; if the change already used up the 3 attempts at that (E, D) pair, the deployer reports "giving up" until either repo moves, which is harmless because the hand deploy shipped it. A relative `[standards] dir` or `brand` resolves beside the actual direction profile first, then against the direction repository root for compatibility; it never uses the installed engine or process working directory. With `dir` unset, the direction repository's root `standards/` is captured before the fetched checkout is removed. `failed.json` and the 3-attempt cap key on the pair. Commit health is checked on E and, when it has statuses or check-runs, on D too; a failing or pending D skips the tick. **No fallback:** if D cannot be resolved, fetched or parsed, or the file is missing, the tick fails and nothing is deployed, never with the pod's profile. Pods carry the direction profile's text as `AGENT_FACTORY_PROFILE_TOML`. |
| `[state]` `uri` | with `auto` | Object-storage prefix for the factory's own state; the deployer keeps `deployer/last.json` (last deployed SHA), `deployer/failed.json` (a SHA that failed to deploy, retried at most 3 times) and `deployer/lease.json` under it. The scheme must be one of `s3`, `gs`, `gcs`, `abfs`, `abfss` or `file`. |
| `[github]` `app_id`, `private_key_secret` | with `auto` | The GitHub App the factory acts as (`app_id` is digits, as a TOML integer or a string), and the Flyte secret holding its PEM private key, mounted as `GITHUB_APP_PRIVATE_KEY`. The deployer uses it to read commit statuses and check-runs and fetch source (without Checks: read it judges a commit by its statuses alone, which misses GitHub Actions results, and its deploy line says so) from the `[deploy] repo` repository (anonymously when the App is not installed there). With both keys set, every project slot also mounts the key, and each project works on branch `factory/<name>` and ends in a pull request opened by the App: a draft when the session declares DONE (opened ready on a repository that does not support drafts), marked ready for review (with a verdict comment and a `factory/verifier` commit status on the reviewed commit) when review passes, and a `factory/verifier` failure status when review fails or escalates. It does not merge, except that `[loop] merge = "on"` lets the loop merge a passing pull request on the `[loop] repo` repository; to make its status required, see [branch protection](../docs/branch-protection.md). The App needs permissions Contents: read (it never writes contents; pushes use the deploy key), Pull requests: write, Commit statuses: write, Checks: read and Metadata: read (Issues: write for [issue intake](../docs/deploy-webhook.md#issue-intake) status comments), and must be installed on every repository projects run on. The key and tokens are kept out of the session's and sub-agents' environments; a same-user session can still reach slot secrets (via /proc or the Flyte API), so this is not a security boundary until the session runs as a separate user (tracked separately). Pushes use the deploy key, never the App; the session merges the default branch into `factory/<name>` and pushes that branch, with no force-push (the `pre-push` hook pins the repository URL, not the branch). Without the table, projects push to the default branch and open no pull request. |
| `[github]` `webhook_secret` | no | Flyte secret holding the GitHub App webhook HMAC secret. Mounted only in `factory-webhook`; enables signed push wakeups (and, with `[deploy] webhook = true`, slows the deploy reconcile from 5 to 60 minutes) and, with `maintainers`, issue intake. `factory-issues` (intake and its 5-minute comment drainer) is deployed only when `webhook_secret` and `maintainers` are both set. Without it the 5-minute poll remains. See [webhook setup](../docs/deploy-webhook.md). |
| `[github]` `maintainers`, `intake_label` | no | Valid GitHub logins (compared case-insensitively) allowed to turn an issue into a project and to send direction by comment, and the label that marks an issue for intake (default `factory`, non-empty, at most 50 characters). Empty `maintainers` (the default) disables intake. Intake arrives through the webhook, so it also needs `webhook_secret` and `[state] uri`. The issue author and, for a label event, the labelling actor must both be maintainers; bots are ignored. Maintainer comments reach only linked, live projects. See [issue intake](../docs/deploy-webhook.md#issue-intake). |
| `[gcp]` `secret_name`, `roles`, `apis` | no | Defaults for the `gcp_bootstrap` task ([docs/gcp-bootstrap.md](../docs/gcp-bootstrap.md)): the Flyte secret the service-account key is stored as, the project roles it is granted (task default `roles/owner`), and the APIs enabled (short names such as `compute` or full `<name>.googleapis.com`). An argument to the task wins. |
| `[bench]` `repo`, `repo_key`, `max_usd`, `claude_token`, `token_concurrency` | no | The factory benchmark ([below](#factory-benchmark)). `repo` (required) is a dedicated github.com repository, distinct from every production `[repo]` entry. `repo_key` (required) is its deploy-key secret, distinct from `[secrets] deploy_key`, every `[repo_keys]` value and every other secret the production profile names. `max_usd` (default 0) limits the reserved sum of case budgets, including stop reserves; cost is enforced at the spend rate measured over the last 10 minutes, so a sudden sustained rise can overshoot slightly (see below). 0 allows estimates only. `claude_token` (optional) names the token secret bench cases run on; it defaults to `[secrets] claude_token` and may equal it. `token_concurrency` (integer, at least 1; default 1) is how many bench environments run cases at once on that token. Requires `[repo] factory` (or a `[loop]` repo, which is then the default engine) and the `[github]` App, which must be installed on the bench repo. Setting it deploys `factory-bench` and `factory-bench-check`. |
| `[board]` `owner`, `owner_type`, `number`, `pat_secret`, `pat_expires` | no | The Projects v2 board ([below](#projects-v2-board)). |
| `[planner]` `admission` | no | `legacy` (default) or `planner` ([below](#gardening)). |
| `[standards]` `dir`, `include`, `languages`, `brand` | no | The shared standards ([below](#shared-standards)). |
| `[loop]` `enabled`, `repo`, `path`, `merge`, `daily_usd`, `weekly_usd`, `owner_floor_pct` | no | The PR loop ([below](#loop)); `merge = "on"` is the one case where the factory merges. |
| `[gardening]` `min_runs_per_week`, `max_days_between`, `escalate_on_regression`, `max_open_issues` | no | The weekly gardening project ([below](#gardening)). |
| `[mcp]` `public_url`, `repos` | no | Public URL (`FACTORY_MCP_PUBLIC_URL` in the shell wins); the `[repo]` keys whose URLs chat may pass to `run_task` as `repo` (default `["project"]`). Access is Union sign-in and Union RBAC per call; there is no user allowlist. |

Unknown keys are errors, so a typo cannot silently fall back to a default.

## GCP access

Optional `[gcp]` defaults configure `gcp_bootstrap`: `secret_name` names the Flyte secret,
`roles` lists project IAM roles (default `roles/owner`), and `apis` lists services to enable
(short names or full `*.googleapis.com` names). Explicit task arguments take precedence.
The project ID is always required; billing roles default to `roles/billing.costsManager`
on the project's linked billing account, including when `billing_account` is omitted.

```toml
[gcp]
secret_name = "my_gcp_infra_key"
roles = ["roles/owner"]
apis = ["compute", "container", "iam"]
```

Run `PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py gcp_bootstrap --project_id my-infra-123` with your selected profile. See
[GCP bootstrap](../docs/gcp-bootstrap.md) for sign-in, billing, key rotation and organisation
policy requirements. The stored service-account key remains valid until you delete it.

## Deployment history

A deployment's operational history (`history/` in the workspace, and every brief that is not a product brief) records
that one deployment and is not de-personalised, so the literal guard does not scan it. Keep it in the deployment's private
config repo with its profiles, not in a published copy.

## Profiles kept in a separate checkout

The ops tools (`deploy_work.py`, `deploy_factory.py`, `deploy_status.py`, `operate.py`) take
`--profile /path/to/direction-checkout/profiles/mine.toml`, or find it through
`AGENT_FACTORY_PROFILES_DIR` plus a name. Code comes from the engine checkout. For a hand-loaded direction
profile, its absolute file path must end with its declared `[direction] profile` path to identify the originating
repository root. Relative standards paths are tried beside that file first, then against that root for
compatibility. With `dir` unset, that direction root's `standards/` is captured at deployment. No parent-directory
search is performed when the declared path does not match; only the profile's directory is known then.
A hand-loaded engine profile at `tree/profiles/<name>.toml` identifies `tree` as its repository root for
explicit relative paths. For other engine profile paths, only the profile's directory is known.

## Shared standards

`[standards]` is optional, and the engine ships no rules of its own ([docs/standards.md](../docs/standards.md) is
the format). `dir` is the standards directory. An absolute path is used as given. A relative path is tried
beside the actual profile file first, then against the originating repository root for compatibility. For
`tree/profiles/name.toml`, `../standards` selects `tree/standards/`; `standards` selects
`tree/profiles/standards/` if present, then `tree/standards/`. A missing explicit directory is an error.
Neither the installed engine nor the process working directory is a fallback. With `dir` unset, a direction
profile uses its repository root's `standards/` when present; an engine profile has no implicit standards.
With no `dir` and no direction `standards/` there are no rules: the deploy prints
`no standards configured`, a workspace's own `standards/` still wins at run time, and without either the gate
records `factory:standards` as not applicable and checks only what is not a standards rule (scope, attribution,
uncommitted work, `checks.sh`). `include`
is a list of base filenames without `.md`, default `["engineering", "security"]`.
`languages` is `"auto"` (detect from the project's files again at review) or an
explicit list such as `["python", "terraform"]`. `brand` is a path resolved like
`dir`, or empty to retain inferred `BRAND.md`. See the standards directory's `README.md` for precedence.

Custom files are captured when the deployment profile is handed to the deployment
subprocess or pod, compressed inside the existing `AGENT_FACTORY_PROFILE_TOML`
payload. This works for private folders outside this repository and does not depend
on image package data. All language packs travel so newly created project files can
activate a pack at review. The bundle is limited to 512 KB uncompressed and 48 KB
encoded; oversized bundles fail with an actionable error rather than truncating
rules. Redeploy to pick up edits. As with other non-policy tables, select the
profile using `AGENT_FACTORY_PROFILE`; no separate standards override convention
is introduced. The internal `standards_payload` key is generated by deployment.

Policy keys are strings: `CRAFT_BLOCKING = "false"` (default) keeps craft advisory; `QA_DIFFERENT_ENGINE = "true"` (default) selects a reviewer provider different from the builder when the profile has both a `claude_token` and a `codex_token`. Set the latter false to retain explicit/default routing.

## Factory benchmark

An optional `[bench]` table enables `factory-bench.benchmark`, which measures one engine SHA on a fixed
task suite: whether each case reaches an accepted PR, its cost and its wall time. It deploys the engine
at that SHA as envs of its own, `bench-<sha7>-<i>` (`[bench] token_concurrency` of them, with sub-agents
`bench-<sha7>-<i>-sub`), so production slots and the queue never see a case and production keeps
launching its own version. Those envs carry a cut-down profile: the bench token (`[bench] claude_token`,
else `[secrets] claude_token`), the bench repo as the project repository with only its deploy key,
non-routing `[policy]` settings and `[standards]`. They have no App, Codex token, API key, gateway key, `[state]`, `[board]`, `[mcp]`,
`[deploy]`, `[direction]`, intake or other repository keys. Step 1 accepts Claude-only candidate
policies. Each case pushes its own `<case>/` directory to an orphan `bench/<case>` branch using
the bench repo's deploy key. The harness holds the App for fetching and deploying; both names of its key are stripped
from deploy children.

The hidden check runs in `factory-bench-check`. Flyte injects `_UNION_EAGER_API_KEY` even though this
environment requests no secrets. Before running case code, its parent uses Linux `PR_SET_DUMPABLE=0`
and a clean child scans readable `/proc/*/environ` for that key. A readable key or failed scan refuses
the check. Commands get a minimal environment; pytest uses `python -I -m pytest` with explicit paths
and a working directory outside the case, so a case's `pytest.py` cannot replace pytest.

```python
import flyte
from flyte import remote

flyte.init_from_config("<your Flyte config>")
bench = remote.Task.get("factory-bench.benchmark", auto_version="latest")
# max_usd = 0 returns the estimate and spends nothing; a max_usd at or above it runs the cases.
run = flyte.with_runcontext(mode="remote").run(bench, candidate_sha="<sha or ref>", max_usd=0)
```

Results are committed to the fixed orphan branch `bench-results` in `[direction] repo`. The harness
uses the GitHub App installation token; the App needs **Contents: read and write** on that repository.
The store refuses to use the repository's default branch. Bench environments receive neither the
direction repository nor the App key. No additional `[bench]` setting is needed.

Each finished case is committed and pushed to `runs/<UTC-date>-<bench_run>/cases.jsonl`, and the
summary to that directory's `summary.json`. Concurrent writers fetch, rebase and retry; conflicting
JSONL appends are replayed without dropping either case. Restarting the same benchmark run skips
recorded case names and includes their records in the summary; changed engine, policy, suite or model
inputs are refused. A new Flyte run name starts a new benchmark. Baseline lookups match engine SHA,
policy, suite digest, task and repeat, with a strict age limit measured from `ended_at`. A/A history
lives in `aa/<UTC-date>-<bench_run>.json` with case records, `sigma_aa`, digest, model and token secret;
history defaults to runs younger than 28 days, oldest first.

The run's outputs are the summary (JSON) and one JSONL line per case; its report is the dashboard.
"Accepted" requires the supervisor's recorded `passed_round` and correctness and completeness PASS
verdicts for that exact round. Missing verdict rows produce `harness_error` with "no verdict evidence".
Phase `done` alone is insufficient. `accepted_verified` additionally requires the hidden check to pass. A refused check has `verified = null`.

Each case's budget includes a stop reserve: the harness polls every 60 seconds and sends `/stop`
when reported spend reaches `cap_usd - reserve`. The reserve is the burn rate times the full poll,
ten-minute delivery retry window and ten-minute shutdown grace (1260 seconds total). The initial
rate is $15 per three-hour case (about $0.0014/second). Once two complete cost readings exist, the
rate is their cost difference divided by their time difference, with the initial rate as a floor.
Both author and sub-agent cost must be present; zero counts as present. An unreadable, stale or
incomplete-cost report for five minutes stops the case with `harness_error`.

The run ceiling is the sum of case caps, with stop reserves inside those caps, and must fit both
finite `max_usd` limits. The cost cap is enforced at the burn rate measured over the last 10 minutes of
cost readings, so a burst within one poll does not stop a case. A case whose spend rate jumps and then stays
high can overshoot its cap by roughly the jump times the few minutes the window takes to catch up (QA:
$3 over a $40 cap after a sudden sustained $1.50/min, about 5x the measured rate). The hard backstop is the
case run's Flyte `max_runtime`.
Flyte also enforces a task `max_runtime` of `cap_hours` plus the ten-minute delivery window and
ten-minute shutdown grace, even if the harness or control plane becomes unavailable. This absolute
wall-time limit includes usage-limit waits.
Stop delivery is retried only for ten minutes after the stop decision. A delivered stop gets ten
minutes to finish before abort; failed aborts retry for at most ten more minutes, then record
`abort_unconfirmed`. Both that error and `stop_undeliverable` emit a stderr `BENCH HARNESS ERROR`
alert. An undeliverable stop never permits abort. Either error, or a run still live when its case
returns, quarantines its bench env for the rest of the
benchmark. Cases left with no safe env are recorded `skipped_login_busy` (the outcome keeps its name).
Branches are retained while their run is live. On harness restart, if an earlier run cannot be stopped
and its env assignment is unknown, all bench envs are quarantined.

The reserve depends on reported cost and measured burn; faster bursts, delayed reports, unknown
in-flight charges or an unavailable control plane can exceed it. Use a provider spending limit
when an absolute billing cap is required. `overshoot_usd` records the final reported excess above
each case budget.

Non-Claude `by_engine` rows make subagent `usd_source` "unpriced", even when the snapshot says $0.
`unpriced_calls` remains null until step 2 adds per-call engine metrics; the current snapshot cannot
count them. Missing case task outputs become `harness_error` records without losing other cases.

A deploy never removes an environment, so removing `[bench]` leaves deployed bench envs in place;
they cost nothing idle.

## Projects v2 board

An optional `[board]` table enables board updates, which `factory-reconcile` (every five minutes)
keeps true. Set `owner` to the account login, `owner_type` to `"user"` or `"org"`, `number` to the
positive project number in its URL, and `pat_secret` to the Flyte secret holding a **classic PAT**
with `project` and `repo` scopes. `[state] uri` is not needed. `pat_expires = "YYYY-MM-DD"` is
optional; otherwise the authentication expiration response header supplies the date. Renewal alerts
appear once per project run, and on every reconcile tick, starting fourteen days before expiry; a 401
alerts immediately.

GitHub Apps cannot access personal Projects v2. Use a separate machine account with board admin
access and repository access restricted to the board's repositories. The App still handles issues,
PRs, and commit statuses. The PAT is used only for Projects v2 GraphQL, mounted only in project
supervisors and `factory-reconcile`. Both its explicit environment name and Union's secret-derived
name are removed before children start. This shares the App key's same-uid limitation: environment
removal does not isolate secrets from processes running as the supervisor's user.

The board's Status (Backlog, Ready, In flight, In review, Done), Size, Kind (GitHub reserves `Type`),
Flyte, Branch and Engine fields are created by `factory-reconcile` whenever one is missing, including
one deleted by hand later. Project input `issue="owner/repo#123"`, or the issue a factory launch links
(its queue request's `issue`, with or without issue intake), associates a run with an existing board
item. Nothing is queued in `[state]`: an item's values are a projection of its issue's newest project
run, found by the run's `factory-issue` label. A live run is In flight, or In review once it has a
pull request; a run that passed review is Done; Flyte, Branch and Engine come from the run and its
report. A failed or aborted run moves nothing, and Backlog and Ready are only ever set by the owner.

The supervisor writes its moves (start, PR opened, release) as they happen. A write that fails is kept
in memory, with one ⚠️ row per failing streak (🚨 for a refused token or an item missing from the
board), and retried every minute for the life of the run; board failures never stop project work.
`factory-reconcile` looks at every board item, whatever its Status, whose issue has a project run that is
live or ended in the last two hours, and writes only the values that differ. Past those two hours it
leaves the item alone, so it never undoes a move the owner made by hand. For a run the item already
shows (its Flyte field is that run's URL) it only moves Status forward (Backlog, Ready, In flight, In
review, Done), so a report that lags the supervisor, or a Done the owner set by hand on a live run,
is never overwritten. Only the supervisor's direct write when it starts a review round puts a Done
item back In review. If that write fails and the supervisor cannot retry it, reconciliation does not
heal the reopen: preserving an owner's Done takes priority. An item that shows another run, or none,
takes the new run's Status. A run that never started, or whose report is missing, is read as an empty
status. One tick lists the slots' runs once (one call per slot task, not one per item). A tick that cannot
read or write the board fails, with
a 🚨 row in its log when the board is not visible or the PAT is refused: the run list in the console
is the failure streak. Omit `[board]` to skip the board; `factory-reconcile` is still deployed when the
GitHub App is configured, for the issue milestones (`docs/deploy-webhook.md`).

The issue may also be given in the short form `repo#123` when `repo` is one of the profile's
`[repo]` repositories; names match case-insensitively. Any other value is reported in the timeline
and the project runs without board updates.

The `factory-board-sync` task is not deployed, and every factory deploy deletes its 5-minute trigger.
A deploy never removes an environment, so the task itself stays listed, inert. Nothing reads
`[state]/board/`; `ops/state_report.py` prints what its outbox holds.

### Reproducing the benchmark start

Every task's start is committed under `tests/fixtures/bench/<name>_start/` (`ops/bench_seed.py` `STARTS`
maps task ids to them); reference solutions live only in this engine repository's test fixtures. From
this checkout, run `uv run python ops/bench_seed.py --profile <profile name or path>` with git
credentials for that profile's `[bench] repo`. The script publishes every start in one commit, at each
task's `workdir`, and prints one `<task> <tree id>` line per task. Each id must match that task's
`_bench_suite.TASKS[...].start_tree`; a task with no pin is refused at launch. The harness checks the
remote tree before every new case; a mismatch is a `harness_error` and launches no agent. Changes to a
start require recomputing and committing its pin, and bump `SUITE`. Generated bytecode is excluded.

Every start passes the factory's own pre-review gates as committed (strict ruff and mypy with the
standards materialized), so a case is never charged for lint debt unrelated to its task.

Each case starts on `bench/<case>` with a root commit containing only its task's start directory; it has no
parent history or other projects. The existing project `branch` input selects this branch, and the
agent pushes there. The hidden check fetches that branch. After the record is written, the harness
deletes `bench/<case>` and any `factory/<case>` branch; cleanup failure is recorded in `cleanup_error`
and the caveats without discarding measurements.

A dedicated bench repository is required. Its key can still fetch concurrent case branches and
previous solutions. To limit answer exposure, seed a fresh repository with only the buggy start
and keep `[bench] token_concurrency` at 1 so cases run serially; concurrent cases carry `contamination_risk`. Branch scoping by itself
does not restrict what the repository key can read.

### Suite v2: what each hidden check proves, and what it does not

Six build tasks are scored by hidden checks run on the case's pushed directory: `bugfix-shortlinks`,
`feature-expiry`, `refactor-logstats`, `docs-standards`, `tf-bucket` (Terraform 1.9.8 and a pinned AWS
provider in the check image; mocked `terraform test` plus an evaluated plan against unroutable endpoints,
so nothing reaches AWS) and `ml-pipeline` (predictions on a label-free holdout are scored by the check;
`LogisticRegression.fit` and the Flyte tasks must actually run). Every check hash-protects the start's
own tests. Two escalate tasks, `vague` and `too-big`, are scored from the case's asks and commit history
(author time; paths that only copy the start are scaffold, not implementation).

Known limits, accepted after two rounds of adversarial QA:

- `vague` and `too-big` cannot judge whether a question is relevant or a split is sensible; they check
  that a blocking ask came before any implementation and that nothing was built before its answer.
  Their successes carry `spot_check: true`, and the summary counts the unadjudicated ones next to the
  pass rate. Read escalate pass rates as provisional until spot-checked.
- `docs-standards` verifies docstrings by running their doctests, so a false example fails, but false
  prose in the README is not machine-verified.
- `feature-expiry` probes the `>=` boundary, slug collisions and stored visits through hooks a typical
  implementation exposes; an implementation that hides them falls back to weaker real-time probes.
- `bugfix-shortlinks`' concurrency probe is probabilistic for a race without the seeded sleep.
- `tf-bucket`'s check takes about 25 s and is budgeted 150 s, above the design's 60 s per check.

`passed_round` comes from review verdict rows captured at PASS, with the supervisor's existing
checkpointed pass record retaining it across restarts. It does not depend on the cleared failing-round
scores. Missing author or subagent cost makes `usd_complete=false`; totals then contain only the
available figures. Non-finite measurements are refused. Harness errors appear separately and are
excluded from pass-rate denominators. Repeated cases include min, max and inclusive IQR for wall time
and measured dollars. Wall time includes `waited_on_limit_s`; observed usage-limit waits are excluded
from the polled time cap. Flyte’s absolute task timeout still includes those waits. Polling makes
those waits approximate, and a harness restart can lose earlier waits.

Step 1 still omits a quota guard, the supervisor metrics block (stage times, tokens per role,
preflight bounce counts, per-call pricing, meters at start and observed knobs), baseline/A-B arms,
held-out tasks, policy search/promotion and durable result persistence outside the run outputs.
These omissions are also disclosed in report caveats. A budget based on partial costs remains partial.

### `[loop]`

The optional `factory-loop.loop_run(pr_number)` task processes one PR in `repo` (`owner/repo`: one of the profile's repositories,
`[repo] factory`, `[deploy] repo` or `[direction] repo`). The engine is the `src/` of `path` in that repo; `path` is
optional, relative and inside the repository (no `..`), and empty means the repo root, so an engine copy kept as a
project folder (`path = "projects/engine"`) is benchmarked and judged where it is. The benchmark's candidate and
baseline are then commits of `repo` built from `path`, and only diffs under `path` are the loop's: a PR touching
anything outside it needs the owner, and the owner paths below are read relative to `path`. `enabled = false` is the default; the deploy registers `factory-loop` only when enabled.
Enabling requires `[github]` and `[bench]`. The App installation needs Contents and Pull requests write
permissions, plus Issues write for comments and labels.

`merge = "dry-run"` (default) posts the decision without merging, closing or labelling. `merge = "on"` applies
it. Review must have posted `factory/verifier = success` at the PR head, and checks must pass. Diffs touching
owner paths always require the owner; docs/tests/briefs alone skip the benchmark. Other diffs run the paired
benchmark with the `[bench] max_usd` ceiling. There is no configurable or default band: without qualifying,
current A/A history the verdict cannot merge. An inconclusive result leaves the PR open.

An issue labelled `loop-kill` by a `[github] maintainers` login or this App stops benchmarks and merges.
Only a maintainer closing the issue restarts the loop; removing the label is not a restart. The App reads
label history, including removed labels and closed issues. Head and default-branch SHAs are checked again
before a merge, and GitHub's merge request pins the head SHA. GitHub has no atomic main-SHA condition on that
endpoint; a main update between the final read and the merge is a remaining race.

`daily_usd` and `weekly_usd` cap API-equivalent loop and benchmark spend read from Flyte run outputs and
project reports. Both default to 0, which blocks autonomous iterations until the owner sets explicit caps.
The daily window resets at 00:00 UTC; the weekly window follows the engine meter's reset (a rolling seven days
when no reset is known). `owner_floor_pct` defaults to 30, in the range 0–100. While a non-loop project is live,
a new case waits when its engine's tightest headroom is at or below that floor. With no owner demand it runs
until exhaustion. Running cases finish; quota waits are logged, rechecked each minute and bounded to eight days.
Two reverts in seven days, an A/A verdict of `better` or `worse`, or three consecutive harness errors cause the
App to create and label a `loop-kill` issue. Only a maintainer closing it resumes the loop.

### `[gardening]`

Optional. The weekly `factory-gardening` project (brief: `briefs/factory-gardening.md`) tidies only the repos
and paths the owner lists in `gardening.toml` (`[[target]]` with `repo` or `path`, optional `cadence_days` and
`checks`; `repo` may be a key of the `[repo]` table). The weekly project runs in the workspace repository and the engine reads the file from its
default branch. Gardening is opt-in: without the file, or with no targets, the weekly task skips without launching, and the factory never edits the file. It is launched at `low` priority, which
takes a slot only when nothing else waits; these keys guarantee it a share:

- `min_runs_per_week` (default 1): while fewer gardening runs completed in the last 7 days, the queued launch
  takes the next free slot ahead of normal work (never ahead of `expedite`). 0 turns the reserved share off.
- `max_days_between` (default 7): no completed run for longer queues it at normal priority.
- `escalate_on_regression` (default true): so does a standards-gate regression on a target's main since the
  last clean run.
- `max_open_issues` (default 10): so do more open issues labelled `gardening` than this.

The weekly task `factory.garden_weekly` launches the project in the workspace repository (`[repo] project`), like
any other project; it needs no `[repo] factory` entry. Changes it proposes for a repository you do not control,
a public engine included, leave the workspace as pull requests ([Promotion](../docs/architecture-repos.md#promotion)). Before it decides the priority it scans: a
child action in the `gardening-scan` environment clones each repo target's default branch with that repo's deploy
key (`[secrets] deploy_key` for the project repository, `[repo_keys]` for the others) and runs the standards gate on
it. The findings per rule are stored under `[state] uri`. The first scan of a target is its baseline; a later scan
that has no more findings on any rule replaces the baseline, and one with more findings on a rule is the regression
that `escalate_on_regression` acts on, named by target and rule. A target the scan cannot clone (no key in the
profile, a clone error) or reach (a `path` target) is reported as unscanned and never escalates.

The reason for a raised priority shows in `operate.py overview` and `factory.queue_list`.

`[planner] admission = "legacy"` is the default. It preserves issue intake, queue sweep and worker
handover. `"planner"` enables Ready-board admission through one webhook lifespan consumer and
`factory.plan_pass`. It requires `[direction]`, `[board]`, the GitHub App, the signed webhook and
`[state]` for importing legacy requests and retaining explicit manual MCP requests. Comments wake a
scan; their text does not supply direction in planner mode.

Planner submission records, launch intents and the planner lock are JSON commit messages on refs in
the direction repository. The App installation needs Contents write permission there. Each planner pass
holds `refs/factory/locks/planner`; the next planner can replace its holder only after Flyte reports a
terminal phase. An unreadable holder or ambiguous submission blocks admission until reconciled by label
or resolved by an operator. Never delete an unresolved intent merely because a listing is empty.
Launch intents use `refs/factory/intents/<issue-node>-<generation>` and remain through failed board writes.

This deliberately changes the original design: the GitHub CAS lock supplies cross-process exclusion,
so a backend without a stop-before-start deployment strategy can deploy planner mode. One app process,
one ASGI worker and one replica remain required safeguards; multiple active planners stop admission.
Drain workers running an older legacy release before changing admission: their frozen code/profile can
still dispatch the old queue. New planner-mode workers only wake the app. The default remains legacy
until independent QA and the coordinator's `ops/probes/planner_smoke.py` smoke pass.

The current slot entry point still supports Claude authors only. A planner proposal selecting Codex
fails closed before submission; author parity must land before a profile relying on Codex admission
can cut over. A terminal Flyte run alone does not establish a QA pass.


### Codex access token

`[secrets] codex_token` names a secret holding a Codex **access token**. It is mounted as
`CODEX_ACCESS_TOKEN` and installed into the private `$CODEX_HOME` with `codex login --with-access-token`
(on stdin, never argv). It never refreshes and is never written back, so any number of pods can share it.
Its account is `codex <secret>`. It cannot be read through the app-server rate-limit endpoint, so its meter
comes from the usage Codex reports in `exec` events. Without it no Codex engine is available, and a slot
policy that pins `COORDINATOR_ENGINE=codex` (formerly `AUTHOR_ENGINE`) is refused.

A Codex turn that fails with an expired or rejected token is not retried: the author is handed to Claude
(unless the run is hard-pinned to Codex) and Codex is kept out of routing for six hours. Replace the secret's
value to restore it.

### Claude setup-token

`[secrets] claude_token` is required. It names the Flyte secret holding a `claude setup-token` token:

```bash
claude setup-token                      # prints the token once
flyte create secret <name>              # paste the token at the prompt
```

Every slot, sub-agent and Claude fallback delegate mounts it as `CLAUDE_CODE_OAUTH_TOKEN`. There is no
OAuth login file, no refresh and no write-back, so the token is the only Claude credential the profile has.
Accounts and secret names belong only in private deployment profiles.

### Migrating from logins

Earlier profiles named one login secret per slot. These keys are accepted, ignored and warned about once
per load: `[[slots]] claude_login`, `[[slots]] codex_login`, `[secrets] claude_login`,
`[secrets] codex_login` and `[bench] logins`. Each warning names the key that replaces it. To migrate,
create the token secret (above), name it in `[secrets] claude_token` (and a Codex access token in
`[secrets] codex_token` if the profile used Codex), then delete the old keys. A profile with none of the
legacy keys loads without warnings.

### Usage reserve

Each account (`claude`, `codex`) has a reserve constant `k`: the percentage of a usage window held back for
every hour left until that window resets. A new launch starts on an account only while, for every window the
account reports, `remaining % >= k x hours until reset`. With `k = 0.5` and 61 hours to the weekly reset, 30.5%
of the week is held back, so an account with 22% left takes no new project until the reserve, which falls
as the reset nears, drops below what is left (44 hours before the reset in this example). The default is
`k = 0`: nothing is held back.

- **Per window.** The 5-hour and weekly windows are each checked against their own hours to reset. A window
  the account does not report is not checked, so a weekly-only plan gates on the weekly reserve alone. A
  window that has reset holds nothing back.
- **What waits.** An account under its reserve counts as not launchable now, as if it had refused until the
  reserve falls. The planner leaves the item waiting as `no_capacity` with the reason, and a legacy queue
  keeps the request waiting. Work already running is never stopped by the reserve. With both accounts
  held back the launch waits; with one, it goes to the other.
- **Seeing it.** The queue listing (`factory.queue_list`, `operate.py queue`) and `operate.py overview` show
  `claude reserve: weekly 22% left < 0.5%/h x 61h = 30.5% held back (resets Mon 09:00Z)`.
- **Setting it.** From chat, `factory.set_reserve` with `account` and `k` (0 removes it), and
  `factory.get_reserve` to read every constant. The values are stored as `reserve.json` under `[state] uri`,
  so the next admission uses a change without a redeploy.

### Shared environment: `[concurrency]`

A setup-token does not rotate, so any number of sessions can share it. A profile may therefore leave out `[[slots]]`: it deploys
one shared project environment (`agent-shared`, sub-agent environment `agent-shared-sub`) and every project
runs on it. `[concurrency]` bounds them instead of the slot count:

```toml
[secrets]
deploy_key = "my_deploy_key"
claude_token = "claude_setup_token"

[concurrency]
max_projects = 4                         # live projects at once; the default
per_engine = { claude = 3, codex = 2 }   # optional
```

A launch starts at once while fewer than `max_projects` projects are live (running, not released or ended)
and fewer than the `per_engine` cap of the engine it will author on are; otherwise it waits in the launch
queue. The engine is the launch's `COORDINATOR_ENGINE` pin, else the profile's `[policy] COORDINATOR_ENGINE`, else
Claude. A launch held back only by its engine's cap does not block a later launch on the other engine. The
queue sweep (every 5 minutes, and at each project's end) admits the next waiting launch when a project ends.

`max_projects` defaults to 4. That number is a guess to be measured: no result of
`ops/probes/setup_token.py --max-k` is recorded in this repository, so run the sharing probe and set
`max_projects` to the largest K it reports as safe. Limits to know:

- `cap` and `[planner] admission = "planner"` also need `[[slots]]` and are refused without them.
- `[concurrency]` together with `[[slots]]` is refused: slots keep working exactly as before, one live project
  per slot, bounded by `cap`.

The coordinator runs the guarded probes `ops/probes/token_meter.py` and
`ops/probes/setup_token.py`, with the token already mounted, a fresh `--nonce`, and an exclusive
`--output` path. The sharing probe also requires `--max-k`; it makes model requests and records
the largest contiguous successful K. Retain both results for independent QA. Only enable
`_meter.SETUP_TOKEN_USAGE_SUPPORTED` when the meter probe returns both utilization windows.
Until then, missing windows remain unknown and CLI quota observations/refusals inform routing.

The bench runs on the production token unless `[bench] claude_token` names another secret; it may share
production's. Set `[bench] token_concurrency` to the sharing probe's safe K (default 1); the bench deploys
that many environments and runs at most that many cases at once across arms.

Changes under `profiles/` require recorded owner approval before merge. The probes and this
implementation do not constitute that approval.
