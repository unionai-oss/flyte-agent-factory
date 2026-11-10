# Operating a project

Launching, watching, steering and stopping projects, and how a session spins off work. Day to day you ask the chat
operator, which runs the tasks below for you ([chat-operator.md](chat-operator.md)); the commands here are what it
calls, and what a script or a terminal uses. How the parts relate is in [architecture.md](architecture.md).

## Running a project

```bash
PYTHONPATH=src flyte run --root-dir src src/flyte_agent_factory/agent.py project \
  --name oauth-device-flow \
  --repo git@github.com:me/myproject.git \
  --brief "Add OAuth device-flow login to the CLI. Done when ..."
```

A direct `flyte run` like this always starts fresh. Launching the same name again through the queue
(`factory.launch` or `ops/operate.py launch`, below) resumes that project's workspace (and its git state)
from its newest successful run, with the new brief appended as further direction. The conversation does not
come back: the coordinator starts a fresh session over that workspace.

## The launch queue

Projects started through the factory (`factory.launch` from chat, or `ops/operate.py launch`) never fail for
capacity. A launch is validated (name slug, brief size, repo allowlist, model), written to
`<[state] uri>/queue/<name>/`, and started at once if a slot is free; otherwise it waits and the
`factory.sweep` task (every 5 minutes, [webhook setup](deploy-webhook.md)) starts it when
a slot frees. The queue is first in, first out. Launching a
name that is already waiting replaces its brief and keeps its place; launching the name of a finished project
resumes it from its last published workspace with the new brief appended as direction. `factory.queue_list` and
`factory.queue_cancel` (and `ops/operate.py queue` / `cancel NAME`) show and edit the queue; `operate.py overview`
shows it too. This needs `[state] uri` in the profile, and every identity that launches needs write access to it.
A project that has passed review releases 30 minutes after its session goes quiet (no direction, open question,
child action or delegation, and not on a usage limit), typically about 60 minutes after the pass because the
post-pass ask runs 30 minutes first. It snapshots, ends and frees its slot; launching the same name resumes it. Every
normal ending (release, `end-project`, `/stop`, a clean exit, but not a crash) publishes the workspace and then
dispatches the next queued project onto the slot it is freeing, so the queue does not wait for the sweep. The
dashboard shows the `released` state and, with `[state] uri`, who is waiting.
Concurrency is bounded by `cap` or `[concurrency] max_projects`; a launch over it waits.

### Why a launch waits

A waiting launch has one of these reasons. Every row of `factory.queue_list` carries the reason of the last
admission pass (the dispatcher's sweep, or the planner's run), so it can be one pass old: `wait_reason` is the
text and `wait` the detail, a `code` plus the keys below. A code both admission paths write has the same keys from
either.

| `code` | Meaning | Detail keys | Written by |
|---|---|---|---|
| `project_running` | its project's previous run is still live | `holders`: that run (absent before the first pass judges it) | both |
| `engine_busy` | the `[repo] factory` lane is full | `holders`, `limit` | both |
| `engine_limit` | its engine is at `[concurrency] per_engine` | | dispatcher |
| `no_token` | it pins an engine whose login is not deployed | | dispatcher |
| `no_codex_slot` | it pins Codex and no free slot holds a Codex login | | dispatcher |
| `loop_budget` | a loop launch whose slot budget says wait | | dispatcher |
| `no_capacity` | no slot it may use has headroom (a usage reserve or meter) | | both |
| `slot_contention` | another launch took its slot first; it retries | | dispatcher |
| `launch_retry` | its launch failed and is retried first next sweep | | dispatcher |
| `behind_retry` | it was not reached because a launch ahead is retried first | `holders`: that launch | dispatcher |
| `no_free_slot` | every slot is busy | | dispatcher |
| `priority_behind` | `low`, behind higher-priority launches that could start | `behind`: how many | dispatcher |
| `low_priority_waits` | the planner's word for `priority_behind` | `behind` | planner only |
| `file_conflict`, `pr_open` and the planner's other codes | the planner's own holds | `detail`: what holds it | planner only |
| `awaiting_admission` | queued since the last pass; not yet judged | | the listing |
| `unreadable_request` | the stored request cannot be read; the row has an `error` | | the listing |

The table below says where each hold also shows outside `wait`.

A `low` launch yields only to a higher-priority launch that could start now. One held by a reason of its own
(its lane, engine, account, project or files) does not make `low` work wait, so a free slot is not left idle behind
it; when its hold clears it goes before any `low` launch. Expedite goes before normal, and a normal resume ranks
as a normal launch. When a resume's own run ends and hands over its slot, another project may take that slot but
`low` work may not: it is kept for the resume (on the `[repo] factory` repository the ending run still holds its
lane for `low` work), and `low` work may take any other free slot. A launch that fails for good, or is cancelled, holds nothing.

| Reason | Where it shows |
|---|---|
| a launch ahead of it in order, or a higher priority that could start (`low` starts only when no other launch that could start waits) | its position in the `queue` rows, and `priority_behind` with a count in `wait`. A gardening launch that was raised to a higher priority says why in a `gardening` line |
| the cap: `cap` live projects, or `max_projects` on the shared environment | `no_free_slot` in `wait`; the live projects are the `slots` rows |
| the engine's cap, `[concurrency] per_engine`; only that launch waits, a later one on the other engine may go | `engine_limit` in `wait` |
| a usage reserve: the launch's account has less than `k` times the hours to reset left | a `reserve` line in `queue_list` and `overview`, such as `claude reserve: weekly 22% left < 0.5%/h x 61h = 30.5% held back`, and `no_capacity` in `wait` on a launch it holds |
| its own project still has a live run: a resume waits until that run has ended and published its workspace, so two pods never work on one project; a hand-over frees its slot for other projects only, and not for `low` work | a `waiting_for` line in `queue_list`, `factory.status` and `overview`, and the log line `[queue] <name> waits: project_running: <run>` |
| engine busy: a launch on the `[repo] factory` repository while `[repo] factory_projects` (default 1) projects on it already run or were admitted in this sweep | `engine_busy` in `wait`, with `holders` and `limit`, and the log line `[queue] <name> waits: engine_busy: <running projects>` (with a limit above 1: `engine_busy: a, b (limit 2)`) |
| the launch pins an engine whose login is not deployed | `no_token` in `wait` |
| every engine of a slot refused a call (a usage limit), so the slot cannot take a project | `no_capacity` in `wait` |
| a loop launch whose slot budget says wait (the `[loop]` caps are unset, or the daily or weekly cap is spent) | `loop_budget` in `wait` |

There is no command that sets the reserve from a terminal: use `factory.set_reserve` from chat. `ops/operate.py`
only shows the holds.

## Rotating the token

A secret mounted into a running pod does not change while the pod runs, so a replaced token reaches a project only
through a new pod. To rotate: update the Flyte secret named by `[secrets] claude_token` (or `codex_token`), then call
`factory.rotate_token` from chat (`run_task`, then `get_run_io`). It sends `/stop` to every live project, which
saves a checkpoint, publishes the workspace and ends, and queues exactly one resume of each, in the same lineage and
not as a new project. A resume waits for its old run to end (see "Why a launch waits") and starts on the next
dispatch, the end of another project or the `factory.sweep` tick, so a project is back within about five minutes of
stopping. Calling it again while the resumes wait queues nothing more; it repeats the stop for a run that has not
stopped. A live run that the queue did not start is reported as `skipped` and left running: stop it and launch it
again by name. `factory.queue_list`, `factory.status` and `ops/operate.py overview` show each waiting resume with the
reason `token rotated`. With no live project the answer is `nothing to rotate`.

## Steering it

| What | How |
| --- | --- |
| Send it work | signal the open `direct-` condition with your message |
| Answer a question | signal the `ask-` condition it opened |
| Watch the browser | open the browser app's URL |
| Stop it cleanly | signal a `direct-` condition with `/stop` |

```bash
flyte get condition <run-name>
flyte signal condition <run-name> <action-name> 'Also handle refresh-token rotation.'
```

Direction reaches the coordinator in its running conversation, so the session keeps its context. The
conditions are named `direct-a<attempt>-NNN` and `ask-a<attempt>-NNN`; `flyte get condition` lists the open ones.
`stop` and `/exit` stop a project like `/stop`.

## How it asks you things

Every project runs unattended: there is no attended mode and no `unattended`
option. The session never blocks on a permission prompt, and each question it
asks carries a timeout and a stated default that it takes if nobody answers.
A caller that still passes `unattended=` to `launch_project` or `run_project`
gets a `TypeError`, and `ops/operate.py launch --unattended` is rejected by
argparse. The slot task itself (started through Flyte directly; the MCP server can run it but that is unsupported)
silently drops an input it does not declare, so `unattended=False` there gets an
unattended run.

If a permission prompt does appear anyway (Claude Code still asks about some commands,
such as an `rm -rf` on a glob it cannot resolve), the supervisor presses Esc to decline it and
tells the session the prompt was declined. Nothing is typed into the session while a menu is
open, because text or Enter landing on it could approve the command. Review results,
direction, delegation notices and reminders are held until the menu closes, then delivered
in order. The decline message is the exception: if the menu is still open after two minutes,
it is dropped, since a later decline sends a fresh one. A session that keeps retrying the same prompt gets a different reply
from the third decline on. "The same prompt" means the same command, or the same rejection
reason. Claude Code's reworded description line is ignored, and so are redraws with letters or
spaces lost. The declines must be within 30 minutes of each other. The reply names the command
and the reason, and tells the session not to retry it or a variant. It also lists safer
alternatives. For a dangerous `rm`, those are a `mktemp -d` directory per run, explicit
absolute paths, or `shutil.rmtree` on a resolved path. Later repeats get a one-line reminder.
Six declines of any prompts within 30 minutes get a general "change approach" reply as well,
so alternating between two prompts is caught too. The timeline gets one row for each streak,
not one for each repeat, and nobody is asked. A human is told once, either after three
change-approach replies to one prompt or at the six-decline mark. Even then the supervisor
keeps declining, so the session is never left blocked on a menu.

A question is judged first: an engineering judgement call the factory settles itself, and only an
owner-reserved question opens a condition; see [judgement.md](judgement.md).

The session has commands on its PATH:

- `ask-human "<question>"` / `ask-human --approve "<question>"` — opens a Flyte
  condition and waits (up to its timeout). The run suspends durably, survives a restart, and can be
  answered from the Union UI, `flyte signal`, or `Condition.signal` — so the
  chat agent that started the project can answer too.
- `flyte-note "<progress>"` — writes a line into the run report.

Claude's own push notifications reach your phone when it needs a decision, if
your Claude Code is set up for them. Conditions are the channel for anything an
agent should be able to answer.

## Spinning off work

The coordinator (the policy role is still called `author`, so its keys are `AUTHOR_MODEL` and `AUTHOR_EFFORT`)
coordinates implementation through `delegate "<standalone brief>"`
(or `delegate --file brief.md`; use `delegate submit "status"` for a reserved word). Submission prints a job id immediately;
`delegate status` lists jobs and `delegate wait <id> --timeout 3600` prints the
worker summary and patch path, or exits nonzero on failure. The coordinator reviews
the patch, commits any pending local work (three-way apply requires the index to match the
worktree for files touched by the patch), and applies the patch with `git apply --3way <patch-path>`, runs checks, and commits.
Workers receive a snapshot including uncommitted changes; they never edit the
session's workspace directly. Results stay in ignored `.claude-flyte/delegations/`.
For read-only exploration or research, use `delegate --answer "<question>"`;
`delegate wait <id>` prints the answer, saved as `summary.md`, without a patch.

`AGENT_FACTORY_DELEGATE_ENGINE=claude|codex` overrides the default (Claude: delegates stay on the
builder side, and judges go to Codex when it is deployed and `QA_DIFFERENT_ENGINE` is on). `AGENT_FACTORY_DELEGATE_MAX`
sets concurrency, default 3. Submitted workers are reattached after a supervisor
restart; jobs not yet submitted become failures. Set `AGENT_FACTORY_AUTHOR_MODEL` and
`AGENT_FACTORY_AUTHOR_EFFORT` at deploy time to change the coordinator's defaults;
explicit `project(model=..., effort=...)` arguments take precedence.
Codex roles use `AGENT_FACTORY_<ROLE>_CODEX_MODEL` and
`AGENT_FACTORY_<ROLE>_CODEX_EFFORT` (uppercase role, punctuation replaced by
underscores). Models default to Codex's own default; delegate/author effort is
`medium`, review/planning effort `high`. Effort accepts
`minimal|low|medium|high|xhigh`; invalid settings warn and use defaults.
Usage-limit fallback maps the Claude effort to Codex (`max` becomes `xhigh`),
unless a Codex effort override is set.


## Coordinator context guard

The [context guard](context-guard.md) is on by default. Set `AGENT_FACTORY_CONTEXT_GUARD=0` to turn it off.
It redirects large whole-file reads to exact source ranges and trims the coordinator's starting instructions.
Compare per-run `tokens.coordinator` before and after rollout and monitor review passes that fail hidden checks;
switch it off if either worsens. The benchmark was confounded and did not establish a measured improvement.

## What a project cost

The run report's *Cost & limits* section splits spend into four groups, each shown with its tokens (cache
reads included, since they are most of the coordinator's) and its dollars, and adds a **total**:

- **coordinator**: the long-lived main conversation, priced at list price (the interactive session reports no
  dollars);
- **in-pod helpers**: the coordinator's Agent-tool sub-agents, which share its pod, priced the same way;
- **sub-agents**: the review roles and other child-action sub-agents;
- **workers**: `delegate` runs.

The total adds all four. A Codex-authored run has no transcript to read, so the coordinator group is marked
`measured: false`, shows "not measured", and the total reads "without the coordinator (not measured)"; a project
that handed off from Claude to Codex keeps the Claude part as measured but partial. A coordinator model with no
list price shows tokens only, and the total reads "without the coordinator (no list price)". A group with no
usage otherwise shows zeros. The older `coordinator` row still shows the main
conversation and the helpers together, so figures from earlier runs stay comparable. In the JSON snapshot these
are `cost.breakdown` and `cost.total_usd`; the metrics block has `usage.coordinator` and `usage.helpers` next to
`usage.author`. The benchmark's case records carry `tokens.coordinator` and its report a `coord_cache_read` guard
(coordinator cache-read tokens per accepted case); the guard is shown beside the verdict and does not change it.

## Three tiers of help

Three tiers, cheapest first.

**In-process subagents** — the session roles in `.claude/agents/`
(`scope`, `discover`, `prototype`, `shape`, `build`, `verify`; see [architecture.md](architecture.md#the-roles)). Threads of the one process, sharing the container and
workspace: right for reading, reviewing and parallel edits, and free. Several
`verify` at once is the intended use.

**Child actions** — for anything needing its own hardware. The session attaches
to its own run and then writes ordinary Flyte:

```python
import asyncio, flyte
from flyte_agent_factory.child import attach

attach()

env = flyte.TaskEnvironment(
    name="train", image=os.environ["FLYTE_INTERNAL_IMAGE"], resources=flyte.Resources(cpu=2, memory="8Gi")
)


@env.task
async def train(lr: float) -> float: ...


print(asyncio.run(train(0.01)))
```

Own container, own hardware, own logs, own row in the run — and never a new
run, so the project stays one thing to watch, abort and bill. This replaced a
CLI with six verbs and a JSON file protocol: about 450 lines of worse.

Two rules, both learned by breaking them on a real cluster:

- **The image must already exist.** Submission works regardless, but a bare
  `flyte.Image` name has no registry inside a pod and the child cannot pull it.
  Reuse `$FLYTE_INTERNAL_IMAGE` or a public image; use
  `flyte.sandbox.create(packages=[...])` when a new image is genuinely needed —
  that goes through the platform builder and works from inside a run.
- **Bound the queue, then fall back.** `max_runtime` only counts once a pod is
  placed, so without `max_queued_time` an unplaceable job waits forever in
  `WAITING_FOR_RESOURCES`, which is neither failure nor progress. Catch
  `MaxQueuedTimeExceededError`: try spot (`interruptible=True`) first, then
  on-demand with the timeout cleared, since the bound carries through
  `override()`.

**Sub-agents** — `subagent(brief=..., role="<role>")`, a nested headless
`claude -p` in its own container, on a warm `ReusePolicy` pool. For work
deserving its own context window and a separate transcript. Nothing it produces
is merged automatically.
