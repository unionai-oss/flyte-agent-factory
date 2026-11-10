# Engines: Claude Code or the Codex CLI

Each project has one long-lived **coordinator** session: the one that receives the brief, the inbox
direction and the supervisor's nudges, runs `delegate`, `ask-human` and `flyte-note`, and declares DONE. It
is the same session the code calls the *author* (`_author.py`). It runs on either Claude Code or the Codex
CLI; the workers it delegates to, and the reviewers, choose their engines separately (`DELEGATE_ENGINE`,
`JUDGE_ENGINE`, ...).

## Choosing the engine

Set the `COORDINATOR_ENGINE` policy key to `claude` or `codex`:

- in the profile, `[policy] COORDINATOR_ENGINE = "codex"`, or per slot under `[slots.policy]`;
- per launch, in the launch's `policy`;
- or, for a run, the environment variable `AGENT_FACTORY_COORDINATOR_ENGINE`.

A value other than `claude` or `codex` is a profile error. Pinning `codex` needs a Codex credential on the slot
(`[secrets] codex_token`, or the slot's `codex_login`); the pin is refused when the slot has neither rather than
run on Claude. The `project` task's `engine`
input beats the policy.

`AUTHOR_ENGINE` is the key's deprecated name and works as an alias, with a warning that names
`COORDINATOR_ENGINE`. Setting both to different engines is an error.

Without a pin, a new project takes the engine the planner admission chose, else the first engine deployed, which is
Claude unless the slot has only Codex; a resumed project stays on the engine whose conversation it holds (see the
`_choose_author_engine` docstring). The usage meters do not choose: they move a call to the other engine only when
the preferred one is exhausted or has refused it.

The same holds for the other roles. With no pin the builders (delegate workers, planner, contract writer) run on
Claude, and the judges run on Codex when the profile has a `codex_token` and `QA_DIFFERENT_ENGINE` is on (the default);
`JUDGE_ENGINE` pins all three judges, and a per-judge key wins over it.

## What each engine supports as coordinator

| | Claude Code | Codex CLI |
| --- | --- | --- |
| Standing instructions | `CLAUDE.md` managed block in the workspace | `AGENTS.md` managed block in `$CODEX_HOME` (not in the project's diff) |
| MCP servers (the browser) | `.mcp.json`, passed with `--mcp-config` | `$CODEX_HOME/config.toml` |
| Sub-agent roles (`scope`, `discover`, `prototype`, `shape`, `build`, `verify`) | yes, `.claude/agents/`, run in parallel inside the session | **no.** The instructions say to use `delegate` instead (one worker per chunk, `delegate --answer` for read-only review). The role files are still written to the workspace for workers; a timeline row says so |
| Browser MCP | yes | yes, through `config.toml` |
| `delegate`, `ask-human`, `flyte-note` | yes | yes (the same commands on `PATH`) |
| `PushNotification` (phone push on DONE) | yes | **no.** The instructions and the supervisor's nudges say to open the `ask-human` message with the line instead |
| Direction from the inbox | typed into the running TUI at any time | **partial.** `codex exec` takes one prompt per process: a message sent during a turn is queued, journaled, and delivered as the next turn |
| Interrupt (`/stop`, a new direction) | yes | yes, ends the running turn; no mid-turn steering |
| Permission prompts | bypassed (`bypassPermissions`) | bypassed (`--dangerously-bypass-approvals-and-sandbox`); there are no dialogs |
| Compaction between rounds (`COMPACT_BETWEEN_ROUNDS`) | yes, `/compact` | **no.** Skipped, with a timeline row; `codex exec` takes no slash commands |
| Resume after a restart | the conversation in the checkpointed config dir (`--resume`) | the thread id saved in the checkpoint, plus queued messages |
| Usage metering | the setup-token's meter feeds routing, and the author's token totals are summed from its transcript | **partial.** The meter comes from the usage Codex reports in `exec` events and feeds routing; per-round author token totals are read from Claude transcripts only, so none are reported for Codex |
| Handoff on a usage limit | to Codex, when the profile has both a `claude_token` and a `codex_token` and the run is not hard-pinned | to Claude, likewise. An unusable Codex token is handed over or ends the run |

A Codex coordinator is told in its instructions what it lacks.


## Codex credentials

Codex runs on one of two credentials:

- **An access token**, `[secrets] codex_token`, mounted on every slot. It is created on a ChatGPT Business or
  Enterprise workspace's Access tokens page and never refreshes; when it is refused, store a new one.
- **A ChatGPT login**, `[[slots]] codex_login = "<secret>"`, for one slot. This is the way for a personal Plus or
  Pro plan, which cannot create access tokens. It is the `auth.json` of a device-code sign-in. Make one in a
  fresh Codex home, so it is a separate session from the one on your machine, and store it:

  ```sh
  export CODEX_HOME=$(mktemp -d)
  codex login --device-auth
  flyte create secret <secret> --from-file "$CODEX_HOME/auth.json" -p <project> -d <domain>
  rm -rf "$CODEX_HOME"
  ```

  A login rotates its refresh token on every refresh, so it belongs to one slot: the profile refuses one secret
  on two slots, or a login that is also `codex_token`. On that slot it takes the place of `codex_token`. The
  slot's coordinator is the one holder that refreshes; it writes each refreshed copy back to the secret within a
  minute, so the next pod starts on a live chain. Its workers get a copy without the refresh token and go to the
  other engine when that copy's access token is about to expire. Do not use the stored login anywhere else.
  If the login is refused, sign in again as above.
- **A ChatGPT login made in the cluster**, `factory-setup.codex_bootstrap_login`, instead of on your machine. The task
  runs `codex login --device-auth` in a pod, shows the link and one-time code as a HITL prompt (`codex-login-...`)
  and in the run report, and stores the resulting `auth.json` as the Flyte secret `secret_name`. With no argument
  it uses the profile's only slot `codex_login`. The name must start with `codex_login`, because chat may start the
  task (`run_task(name="factory-setup.codex_bootstrap_login", inputs={"secret_name": "codex_login_..."})`) and it
  must not be able to overwrite any other secret. From a shell: `flyte run --root-dir src -p <project> -d <domain>
  src/flyte_agent_factory/agent.py codex_bootstrap_login --secret_name codex_login_...`
  ([quickstart.md](quickstart.md#codex-sign-in)). All secrets: [secrets.md](secrets.md).
