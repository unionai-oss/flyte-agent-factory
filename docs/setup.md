# Setup

From nothing to a first project. The deploy and the key modes require Union: with `--backend oss` the bootstrap
runs every step before the deploy, and OSS Flyte needs the adaptations in
[the compatibility audit](oss-flyte-compat.md). The factory also needs a Claude
`setup-token` (and optionally a Codex access token), one GitHub repository of your own for the workspace, and a
deployment profile that names them. How the pieces fit together is in [architecture.md](architecture.md).
Start with [guided setup](quickstart.md): install the skills, run `/agent-factory`, and it checks existing resources
and takes you from GitHub access to a first launch one step at a time; every secret is in
[secrets.md](secrets.md). This page is the detail behind them.

## The short path

1. **Create the workspace repository.** It is the repository projects are pushed to (`[repo] project`). Create a
   private GitHub repository for it, then fill a clone of it from the engine's bundled template:

   ```bash
   uv run python ops/new_workspace.py ../agent-workspace
   ```

   Copy the workspace's `profile.example.toml` to `profile.toml` at its root and fill it in
   ([quickstart.md](quickstart.md), step 3), replace `OWNER` in `.github/CODEOWNERS`, and turn on branch
   protection ([branch-protection.md](branch-protection.md)). `ops/bootstrap.py` does all of this, and more, in
   one resumable command. `standards/` and `waivers.toml` are yours; the
   factory cannot change them. So is `gardening.toml`; without it there are no gardening targets
   ([architecture.md](architecture.md#gardening-targets)).
2. **Store one `claude setup-token`** as a Flyte secret and name it in the profile's `[secrets] claude_token`
   (section 1 below). Every Claude session runs on it.
3. **Add a deploy key** for the workspace repository, store it as a secret and name it in
   `[secrets] deploy_key` (section 3). To launch projects into another repository, give it a
   deploy key too and list it under `[repo_keys]`. The workspace deploy key is the only GitHub credential setup requires; the
   GitHub App is optional and yours to register ([github-app.md](github-app.md)).
4. **Select the profile and deploy** with `--profile <workspace clone>/profile.toml` (or `AGENT_FACTORY_PROFILE` set to
   that path). The first install is `ops/deploy_work.py` and `ops/deploy_factory.py` ("Deploying", below): the only
   hand step besides creating secrets. After that a merge to the workspace deploys itself when the profile has
   `[direction]` (below). A merge to the public engine deploys once its CI is green: the App is installed on the
   workspace only, so the deployer reads the engine anonymously, polls (`[deploy] webhook = false`) and keeps its lock
   in `[direction] repo`. To deploy sooner, run the
   bootstrap with `--redeploy` ([quickstart.md](quickstart.md#what-agent-factory-walks-you-through)). The personal browser app
   follows the same rule ([personal-browser.md](personal-browser.md)); a merge to the workspace deploys
   only if the profile has `[direction]` pointing at it (`repo`, and `profile = "profile.toml"`), which the workspace's
   own `profile.toml` carries. The engine does not look for a `profile.toml` in the workspace by itself: the first
   deploy names it, and moving an existing install onto `[direction]` later needs one run of the bootstrap with
   `--redeploy` ([quickstart.md](quickstart.md)), which deploys with that `--profile`.
5. **Link a backlog board.** A GitHub Projects board owned by the same account, linked to the workspace with it as
   the board's default repository, is the standard backlog: unrelated projects, your own items, and copies of
   issues pulled from tracked repositories. The bootstrap creates and links it (`gh project create`,
   `gh project link`; skip with `--no-board`) and prints the clicks GitHub has no command for.
6. **Connect chat** to the factory MCP ([chat-operator.md](chat-operator.md#setup),
   [factory-mcp-setup.md](factory-mcp-setup.md)) and launch a project
   ([operating.md](operating.md)).

The sections below are the detail behind each step.

## One-time setup

### 0. A deployment profile

Everything specific to your deployment (Flyte project and domain, console host, project
repo, project slots, secret names, role policy, MCP settings) lives in one TOML
profile: `profile.toml` at the root of your workspace. Fill in the workspace's `profile.example.toml`, and select it
for every command and ops tool, either with the environment variable or with `--profile <path>` on `ops/*.py`:

```bash
export AGENT_FACTORY_PROFILE=../agent-workspace/profile.toml
```

See [profiles/README.md](../profiles/README.md) for the schema. The names below in angle brackets are the profile's.

### 1. A Claude setup-token

Every Claude session, the benchmark's included, runs on one `claude setup-token` credential.
Run `claude setup-token`, store the token as a Flyte secret (`flyte create secret <name>`) and name
that secret in `[secrets] claude_token` in the deployment profile; never commit the token.
The pod mounts it as `CLAUDE_CODE_OAUTH_TOKEN`, without an OAuth login file. A restored login file
is deleted before a session starts. Direction and questions use Flyte conditions.

The coordinator runs `ops/probes/token_meter.py` with the mounted token and a fresh nonce to
record whether the usage endpoint returns both 5-hour and weekly utilization. Until that evidence
is reviewed, `SETUP_TOKEN_USAGE_SUPPORTED` stays false. Quota headers exposed by CLI result events
and 429/usage-limit refusals supply observations through the existing router meters. Missing
utilization remains unknown. Refusals block until the reset, with bounded backoff when no reset
is available. The CLI may omit headers; in that case only refusals provide a quota signal.

For direct Flyte commands, resolve the selected profile in your shell first:

```bash
export FLYTE_CONFIG="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile config)"
export FLYTE_PROJECT="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile project)"
export FLYTE_DOMAIN="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile domain)"
export AGENT_FACTORY_SECRETS="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile agent_secrets)"
```

For a project that needs GCP, `PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py gcp_bootstrap --project_id <id>` signs in to
Google through a Flyte condition and leaves only a service-account key behind; see
[docs/gcp-bootstrap.md](gcp-bootstrap.md).

Then deploy with the profile-selected credential mounted. The slots, `factory`, `factory-mcp`,
`factory-deployer` and, with `[browser] personal`, the personal browser app are deployed by the factory itself: with `[deploy] auto = true` a merge to the default branch of the repository
named by `[deploy] repo` (default `[repo] factory`) ships on the next deployer tick once its head's CI passed;
`ops/deploy_work.py` and `ops/deploy_factory.py` cover the first install. Engine improvement is a workspace project,
imported and promoted like any other ([architecture-repos.md](architecture-repos.md#promotion)); nothing is
deployed by hand. The voice app is
deployed directly:

```bash
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" deploy --all --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/voice.py
```

### 2. A project of its own

Secrets are scoped per project and domain, so the agent runs in its own —
holding the Claude token and the deploy keys of the repositories it may push to. Its identity then
cannot reach anything else you keep in your main project. Use the profile's `[backend] project`:

```bash
flyte create project --id <project> --name "Agents"
```

### 3. The repositories it can reach

A GitHub **deploy key** scoped to one repository, stored as a secret. This one is the workspace's. Each further
repository is another deploy key under `[repo_keys]`. A slot mounts all of them, but a session installs only the key of
the repository it was launched on, so other repositories are unreachable because the session holds no credential
for them, not because a rule forbids it. The secret name is the profile's `[secrets] deploy_key`:

Generate and store it with the block in [quickstart.md §4](quickstart.md#4-store-engine-credentials-locally),
which makes the key in a `mktemp` directory and removes it afterwards.

The agent writes it 0600 and pins `GIT_SSH_COMMAND` with `IdentitiesOnly=yes`,
so ssh cannot offer another key, plus `StrictHostKeyChecking=accept-new` and
`GIT_TERMINAL_PROMPT=0` so a first clone can neither hang nor prompt.

Optionally, a **GitHub App** (the profile's `[github]` table, see
[profiles/README.md](../profiles/README.md)) lets the supervisor open pull
requests. A project then works on `factory/<name>`, the supervisor opens a
draft PR (a ready one where the repository has no drafts) when the session
declares DONE and marks it ready, with a verdict
comment and a `factory/verifier` commit status, when review passes. The App's
key and tokens are kept out of the session's and sub-agents' environments and
the session still pushes with the deploy key. The session runs as the same user
as the supervisor and holds a Union API key (`_secret_env.SESSION_KEEPS` keeps
`_UNION_EAGER_API_KEY`), so it can still reach slot secrets through the Flyte API; the
supervisor is non-dumpable, so `/proc` is closed. This is not a security boundary until the
session runs as a separate user (tracked separately). Without `[github]`,
projects push to the default branch and open no pull request. Merging is not gated on the verdict until
branch protection requires it ([branch protection](branch-protection.md)).

### 4. The browser app (optional)

```bash
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" serve --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/browser.py browser_env
```

Access control is the platform's. The app's public URL is behind Union's own
auth, and the agent reaches it on the cluster-internal Service address. The `/mcp` endpoint has no token of
its own: a shared token would only have blocked the agent it was meant to serve.

To run without the browser entirely, deploy with `AGENT_FACTORY_BROWSER=0`. The
browser is a per-run choice: `PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py project --browser`, or `--browser` /
`--no-browser` directly.

### 5. Extra secrets (optional)

A secret that doesn't exist is a hard pod mount failure in Flyte, so optional
secrets are opted into at deploy time:

```bash
AGENT_FACTORY_SECRETS=github_token,npm_token=NPM_TOKEN flyte deploy src/flyte_agent_factory/agent.py
```

## Deploying

Flyte's CLI loads a task file two different ways, and a package layout needs
help with both: `flyte deploy --all` imports it package-aware as dotted path
relative to `--root-dir`, while `flyte run <file>` imports it as a top-level
module via `spec_from_file_location` with no package at all. So every
invocation carries `--root-dir src` (which makes the in-container module name
`flyte_agent_factory.agent`) and `PYTHONPATH=src` (which resolves the absolute
intra-package imports locally). Use the direct commands below. In order, they:

1. store a `claude setup-token` token as the Flyte secret named in `[secrets] claude_token`;
2. redeploy only the work slots from `origin/main` (safe from inside the agent);
3. deploy the factory, factory-mcp and factory-deployer from `origin/main` (the first install of the auto-deploy
   trigger);
4. verify the sandbox child-action contract on the cluster;
5. start a project.

```bash
bash scripts/export_local_credentials.sh
PYTHONPATH=src uv run python ops/deploy_work.py
PYTHONPATH=src uv run python ops/deploy_factory.py
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/sandbox_probe.py probe
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py project
```

Merged changes deploy themselves: with `[deploy] auto = true` in the profile, the `factory-deployer` task polls the
default branch of `[deploy] repo` (default `[repo] factory`) every 5 minutes and ships a head whose CI passed, so a
merge ships on the next tick plus build time. For a user on the public `unionai-oss/flyte-agent-factory`, where the
App is not installed, the deployer reads that repository anonymously and keeps its deploy lock in `[direction] repo`;
to deploy the engine's `main` at once, run the bootstrap with `--redeploy`
([quickstart.md](quickstart.md#what-agent-factory-walks-you-through)). With
`[github] webhook_secret`, a signed push starts it instead and the schedule is an hourly reconcile
([webhook setup](deploy-webhook.md)). It deploys the
slots, factory, factory-mcp, itself and the personal browser (when the profile enables it, replaced
stop-then-start), and skips a commit while its CI is failing or still pending. The profile and
standards it deploys with are the merged tree's `profiles/<name>.toml` when that exists (else the profile the
deployer pod started with), so a merged change to either ships too. With `auto = false` the trigger still ticks,
and does nothing. If a Flyte secret that a planned environment mounts does not exist, it deploys nothing and
its run report and tick message name the secret; create it and the next tick deploys (where the deployer works at all; see above). The `ops/deploy_*.py`
scripts above remain for the first install and for emergencies; the personal browser has no `ops/` path and
is deployed only by the deployer.

With `[board]`, GitHub branch and PR events advance issue cards. Completion requires a non-forced push
whose head is still on the current default branch, or a PR merged into that branch. Commit references
count only on the first line; revert subjects and quoted lines are ignored. Status changes reread the
card and only advance it, except an unmerged PR closure can return In review to In flight. Done stays
Done. Without a GitHub App, completion still moves the card and logs that the issue could not be closed.
The hourly board drift scan checks at most the latest 100 default-branch commits. It heals closed
issues' cards to Done and flags open issues for review; historical text never closes a reopened issue.

To roll back, run `ops/deploy_factory.py --ref <commit older than the deployer>`. It needs that commit's own ops
scripts, so run it from a checkout of that commit rather than the current `ops/`. The next merge deploys the default
branch again.

A `float` task input needs a float literal default (`4.0`, not `4`). Flyte serializes every task default at deploy
time (`convert_upload_default_inputs`) and rejects an `int` there; a dry run (`--dry-run`) does not run that step.
`tests/test_deploy_factory.py::test_deployed_task_defaults_serialize` runs it for every task a factory or slot
deploy sends. When a deploy reports `cannot schedule new futures after shutdown` for one task, the cause is the first
`Failed to deploy task` line in the log: the SDK keeps deploying the other tasks while the process exits, and one of
them then finds the event loop's thread pool closed.
