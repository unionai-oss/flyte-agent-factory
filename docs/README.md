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
| [quickstart.md](quickstart.md) | Start here: prerequisites and every step from a clone to a first launched project, on a personal Claude Pro/Max or ChatGPT Plus/Pro plan |
| [secrets.md](secrets.md) | Every Flyte secret a profile can name: what it unlocks, how to get it, how to store it, where it is mounted, how to rotate it |
| [install-audit.md](install-audit.md) | The gaps found walking the README as a stranger, what fixed each, and what is still to prove on a personal cluster |
| [setup.md](setup.md) | From nothing to a first project: workspace, profile, setup-token, deploy key, deploying |
| [../profiles/README.md](../profiles/README.md) | Every profile table and key |
| [standards.md](standards.md) | Writing your own standards: the rule format, how each rule is enforced, mapping your rule IDs to the factory's built-in checks |
| [chat-operator.md](chat-operator.md) | The default interface: why a chat agent sits between you and the factory, what each side can do, and setting it up |
| [factory-mcp-setup.md](factory-mcp-setup.md) | The factory MCP's authentication, scoping the API key, and building the chat skill |
| [github-connector.md](github-connector.md) | GitHub issues and the Projects board from chat |
| [branch-protection.md](branch-protection.md) | Requiring the factory's verdict before a merge |
| [gcp-bootstrap.md](gcp-bootstrap.md) | Giving a project access to Google Cloud |
| [credential-grants.md](credential-grants.md) | Giving one project a short-lived credential (AWS) that nothing else can use: asking, sealing, revoking, and why it is not a Flyte secret |

## Operating

| Page | Answers |
|---|---|
| [chat-operator.md](chat-operator.md#what-to-ask-it) | What to ask the chat operator day to day |
| [operating.md](operating.md) | Launching, the queue, steering, questions, delegation |
| [judgement.md](judgement.md) | Who decides a question: the owner or the factory, the no-waiver rule, overruling a decision |
| [deploy-webhook.md](deploy-webhook.md) | Push wake-ups, issue intake and the deploy safety net |
| [triggers.md](triggers.md) | Every timer, its cadence and what it costs |
| [personal-browser.md](personal-browser.md) | The single-seat logged-in browser |
| [browser-exit-node.md](browser-exit-node.md) | Routing the in-pod browser through a Tailscale exit node |
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
