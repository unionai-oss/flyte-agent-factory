# Documentation

Every page, grouped by the reader it is written for. The project overview is the [README](../README.md).

## Evaluating the factory

| Page | Answers |
|---|---|
| [architecture.md](architecture.md) | What it does end to end, how a person interacts with it, how work moves through git, the MCP controls, the agent roles and when each runs, what is pluggable, and why it works this way |
| [architecture-repos.md](architecture-repos.md) | The engine, the workspace template and your workspace; what each holds and who writes it; promotion |
| [engines.md](engines.md) | Claude Code or Codex as the coordinator, and what each supports |
| [security.md](security.md) | How to report a vulnerability |

## Setting up a workspace

| Page | Answers |
|---|---|
| [quickstart.md](quickstart.md) | Start here: install the skills, run `/agent-factory`, and what it walks you through; prerequisites, and the same steps by hand, on a personal Claude Pro/Max or ChatGPT Plus/Pro plan |
| [github-app.md](github-app.md) | Registering your own GitHub App (the bootstrap's manifest flow, or by hand): permissions, what it enables and what works without it |
| [state-store.md](state-store.md) | The factory's state: git (zero-setup trial), a dedicated bucket (recommended production default) or a volume (OSS single node), and the contract all three keep |
| [READBACK.md](READBACK.md) | Plain-language readback of the dedicated factory bucket work (#11): what changed for state and the browser archive, and how to use it |
| [terraform-factory-bucket.md](terraform-factory-bucket.md) | What `terraform-flyte` and `terraform-union-dataplane` must add for the factory's bucket: versioning, lifecycle, encryption, least-privilege IAM per prefix, outputs |
| [secrets.md](secrets.md) | Every Flyte secret a profile can name: what it unlocks, how to get it, how to store it, where it is mounted, how to rotate it |
| [install-audit.md](install-audit.md) | The gaps found walking the README as a stranger, what fixed each, and what is still to prove on a personal cluster |
| [setup.md](setup.md) | From nothing to a first project: workspace, profile, setup-token, deploy key, deploying |
| [small-nodes.md](small-nodes.md) | Running on one 4 CPU / 16 GiB node: what each component requests, the arithmetic, BestEffort apps, raising it per component |
| [READBACK.md](READBACK.md) | How #15 (small nodes) was re-applied on #12's sharded test lanes: the shard budget arithmetic and the tests adjusted where the two meet |
| [REPORT.md](REPORT.md) | #48: why a project stopped before review passed no longer shows Done, the new Stopped column, and the answers to the issue's three open questions |
| [../profiles/README.md](../profiles/README.md) | Every profile table and key |
| [standards.md](standards.md) | Writing your own standards: the rule format, how each rule is enforced, mapping your rule IDs to the factory's built-in checks |
| [chat-operator.md](chat-operator.md) | The default interface: why a chat agent sits between you and the factory, what each side can do, and setting it up |
| [factory-mcp-setup.md](factory-mcp-setup.md) | The factory MCP's authentication, scoping the API key, and building the chat skill |
| [mcp-stock-move.md](mcp-stock-move.md) | The move to flyte 2.13.5's stock MCP server: each wrapper removed, set or kept, with evidence, and what changes on redeploy |
| [mcp-sdk-requirements.md](mcp-sdk-requirements.md) | What the factory MCP still builds around Flyte's stock MCP server, written as requirements for flyte-sdk, ranked |
| [mcp-operator-smoke.md](mcp-operator-smoke.md) | The live smoke test of the factory MCP from chat after a redeploy, on Union and on open-source Flyte, and the rollback |
| [github-connector.md](github-connector.md) | GitHub issues and the Projects board from chat |
| [branch-protection.md](branch-protection.md) | Requiring the factory's verdict before a merge |
| [gcp-bootstrap.md](gcp-bootstrap.md) | Giving a project access to Google Cloud |
| [credential-grants.md](credential-grants.md) | Giving one project a short-lived credential (AWS) that nothing else can use: asking, sealing, revoking, and why it is not a Flyte secret |

## Operating

| Page | Answers |
|---|---|
| [chat-operator.md](chat-operator.md#what-to-ask-it) | What to ask the chat operator day to day |
| [operating.md](operating.md) | Launching, the queue, steering, questions, delegation |
| [process-reaping.md](process-reaping.md) | Why tini is PID 1 in every factory pod: where orphans come from, what zombies cost (the `/proc` scan, the pids limit), the probe that proves reaping, and the process-health line in status and gate output |
| [judgement.md](judgement.md) | Who decides a question: the owner or the factory, the no-waiver rule, overruling a decision |
| [judge-inputs.md](judge-inputs.md) | What each review judge was shown, as recorded in the round row, and recreating the diff from it |
| [scoped-key-deploy.md](scoped-key-deploy.md) | Why a deploy as a project-scoped `deployer_key` polls apps, deploys the deployer last and fails once without a key; run-time calls checked |
| [deploy-webhook.md](deploy-webhook.md) | Push wake-ups, issue intake and the deploy safety net |
| [state-escalation.md](state-escalation.md) | State backend benchmark results (git vs bucket vs volume under 12 writers) and the measurements and thresholds that would justify a Postgres backend |
| [triggers.md](triggers.md) | Every timer, its cadence and what it costs |
| [personal-browser.md](personal-browser.md) | The single-seat logged-in browser |
| [browser-exit-node.md](browser-exit-node.md) | Routing the in-pod browser through a Tailscale exit node |
| [sealed-runs.md](sealed-runs.md) | Sealing a benchmark run: what to redeploy, launching sealed, the plain arm, the record, the audit and `gold.json`, the report, and the rerun procedure for the 14 tasks |
| [slot-tools.md](slot-tools.md) | The tools installed in the slot image |
| [context-guard.md](context-guard.md) | The guard (on by default, with an off-switch) that trims the coordinator's CLAUDE.md, denies whole-file reads of large files and compacts earlier |

## Contributing to the engine

| Page | Answers |
|---|---|
| [contributing.md](contributing.md) | Set up, the checks, tests first, waivers, the DCO |
| [ci.md](ci.md) | What GitHub Actions runs for a change, what it skips and why, and what "green" means to the deployer |
| [ci-signal-options.md](ci-signal-options.md) | For the owner: the factory's own checks or a runner on the cluster as the required CI signal, side by side |
| [public-cutover.md](public-cutover.md) | For the owner: publishing the engine as a fresh-history public repository, the dry run, and pointing the factory at it |
| [architecture.md](architecture.md#where-things-live) | Which module holds which responsibility |
| [oss-flyte-compat.md](oss-flyte-compat.md) | What running on OSS Flyte needs |
| [test-lanes.md](test-lanes.md) | How pre-review splits the suite into small child shards on 2- and 4-CPU nodes, its knobs, and how to read a capacity, setup or test outcome |
