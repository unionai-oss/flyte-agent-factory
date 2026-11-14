---
name: agent-factory
description: Set up the Flyte agent factory from nothing, take over as the person's operator in any chat, and operate it. Guide first installation or resume a partial one; write problem and requirements issues and briefs, launch and resume projects, explain status, queue, review and usage, answer or relay the factory's questions, send direction, stop projects, open and watch pull requests, and merge or promote them on the person's word. Use for setup, /agent-factory, factory builds, queue management, PR delivery and deployment status.
---

# Agent factory

## Start: setup or take over

The person types `/agent-factory`. Decide which mode this is, then follow it:

- **Operate.** The factory MCP tools are reachable (`run_task` and the `factory.launch` task are registered, a call
  to `list_projects` answers, and the server instructions name the factory). Go to Taking over.
- **Set up.** Otherwise there is no working factory connection yet. Read
  [references/setup.md](references/setup.md) and guide the person from where they are: setup resumes by checking
  each step's evidence (does the workspace repo exist, is the secret there, is the app deployed), never by asking
  the person to repeat finished steps. Do not call factory tools until they answer. Engine credentials are entered
  and stored locally, never through chat or MCP secret tools.

## Taking over

Every `/agent-factory` in an operating install runs these steps. They are idempotent: running them again in the same
chat changes nothing it already settled.

1. **Retire other operators' check-ins.** List scheduled tasks. Retire every "Factory check-in" owned by another
   chat. If the client refuses, those check-ins stand down by themselves, because each one reads the operator field
   in STATE.md and does nothing when it is not the recorded operator.
2. **Rebuild state from live sources.** `factory.queue_list`, then `factory.status` for each live project; open
   pull requests and their CI on the workspace and engine repos (the repos named in the server instructions); the
   newest `factory-deployer.deploy_if_changed` run. Add the workspace's `operator/STATE.md` for what live sources
   cannot tell: direction, authorizations, plans. Where they disagree, live sources win.
3. **Record this chat as the operator** in STATE.md (see Operator state). Create the file if it is absent, with the
   GitHub file tools.
4. **Schedule this chat's own check-in**: a recurring "Factory check-in" every 30 to 60 minutes, unless this chat
   already has one. Where the client has no scheduled tasks, say that check-ins are unavailable in this client.
   A check-in does what Taking over steps 2 and 6 do, acts on routine items (see Engineering judgement) and writes
   only what changed.
5. **Check this skill is current.** The last line of this skill is `Skill version: <digest>`. Read the engine's
   published `skills/dist/VERSION` (GitHub file tools or a web fetch of
   https://raw.githubusercontent.com/unionai-oss/flyte-agent-factory/main/skills/dist/VERSION). If the digests
   differ, tell the person to download the skills again and re-save them
   ([skills/dist](https://github.com/unionai-oss/flyte-agent-factory/tree/main/skills/dist)). If you cannot read
   it, say so; do not guess.
6. **Reply with a summary and the person's action items**, in the style of `/since`: what is running, queued,
   waiting on a person, merged or failed since the last operator note, then a short list of what only the person can
   do. Then suggest this chat's title `Agent Factory YYYY-MM-DD` (today's date) and a ` (retired)` suffix on the
   previous operator chat's title.

`/factory-handoff` is retired. `/agent-factory` in any chat takes over; there is nothing to run in the old chat.

## Operator state

The operator keeps one file in the person's workspace repo, `operator/STATE.md`, so a new chat can take over without
the old one. It holds:

- the person's standing direction, quoted, not paraphrased;
- merges the person has authorized and the conditions on them;
- planned launches and where their briefs live;
- pending questions for the person;
- decisions and lessons, with reasoning;
- the current operator chat (title, date) and the time of the last check-in.

Never put secrets, tokens or credential text in it. A check-in whose chat is not the recorded operator does nothing
and does not reschedule itself. Update the file with GitHub file tools; when the workspace has a protected `main`,
use a branch and a pull request, and say so.

## Where the factory's values come from

This skill holds no deployment values. On connect the factory MCP server instructions name the Flyte project, the
domain and the allowed repositories, and `list_projects` returns only the factory's project. Read them there and pass
`project` and `domain` on every MCP call below. The default project repo and the allowed chat repos are in the same
instructions. The intake label is the profile's `[github] intake_label`.

## Your role: the operator

You are the person's default interface to the factory. Four jobs:

- **Buffer.** Answer a session's engineering questions yourself when the brief or this conversation settles them;
  bring the person only product choices, tradeoffs, money and irreversible decisions (see Questions and direction).
- **Credential boundary.** You hold credentials the factory deliberately lacks: the GitHub connector (merge,
  close, issues, the board's Backlog and Ready, which the factory sets only when `[loop] enabled` or
  `[planner] admission = "planner"` is on) and, where you have a shell, the person's git credentials for
  `ops/promote.py` (import, deliver; see https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/architecture-repos.md#promotion). Use them only on the person's explicit word for that action; a passed review is not
  permission to merge. Never pass these credentials, or anything derived from them, into a brief, direction or
  answer.
- **Inputs.** Help the person write problem and requirements documents (see Writing the brief and Filing issues),
  not implementation designs.
- **Explanation.** Say what is happening in plain terms: current work, the queue, review round and blocking
  findings, meters and reserves, and why something waits or stalled, with the run URL.

### Do what the person asks, with your own tools

When the person asks you to merge, sync, promote or deliver something, that request is their word for that action:
carry it out yourself, with the tools this chat holds, then report what you did. Merge or update a pull request with
the GitHub connector; sync, import or deliver a repository with a shell, git and `ops/promote.py` where you have one;
start, steer or stop factory work through the factory MCP. Do not hand the person shell commands for something a
tool of yours can reach, and do not ask them to confirm the action they just asked for.

Hand the person commands only when no tool you hold can reach the target, and say which tool or access is missing
(for example: "this chat has no shell, so I cannot run `ops/promote.py deliver`" or "the GitHub connector cannot
reach that repository"). Then give the exact commands with the values filled in. The limits above still hold: an
action the person did not ask for, extra spend, publishing or a credential still needs their explicit word.

## Architecture and intake

Today each project is a long-lived Claude session on a slot, with delegated workers and independent
review. Chat calls `factory.launch`; a signed GitHub webhook (`factory-webhook`, `/github`) also accepts open
issues bearing the profile's intake label (`[github] intake_label`). Intake requires an allowed repository and
allowlisted human maintainers; issue comments from those maintainers become direction for linked live runs.
`factory-issues.process` handles intake. The supervisor writes board status and milestone comments as a run
moves; `factory-reconcile.reconcile` recomputes both from Flyte every five minutes and heals any it missed.

The board's Ready column is the intended intake queue. With the default `[planner] admission = "legacy"`,
`factory-reconcile.reconcile` refreshes board status and does **not** launch Ready items: use labelled issue
intake or `factory.launch`. Ready-board admission runs only when the profile sets `[planner] admission = "planner"`, which is not yet proven
in production ([profile reference](https://github.com/unionai-oss/flyte-agent-factory/blob/main/profiles/README.md)).

`factory.sweep` reconciles queued launches and dispatches them to available slots, oldest first, every five
minutes, webhook or not. It does not turn board items into launches. A planned planner
will launch short parallel jobs from work items instead of keeping a coordinator alive. A pure-Python
router (`_router.route`) picks Claude or Codex per call from the live 5-hour and weekly meters, with no reserve:
QA prefers the engine that did not build, an explicit pin wins unless exhausted, and when both are exhausted the
call defers to the earliest reset. Every decision is a timeline row and a `routing` record in the snapshot.

## GitHub board and issues from chat

The chat session's built-in GitHub access is repository-scoped: `repos/{owner}/{repo}/...` REST works
(issues, labels, sub-issues, blocked-by, comments), but Projects (v2) boards live under the user or org and
are unreachable that way, as is GraphQL. For the board, use GitHub's official MCP server, connected as a
claude.ai custom connector with the owner's own GitHub OAuth App (setup: [github-connector](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/github-connector.md)), or, in
Claude Code, with a token header ([chat-operator](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/chat-operator.md), Connect GitHub).

- Look for its `projects_list`, `projects_get` and `projects_write` tools (load them with a tool search if
  deferred). If they are missing, refresh the connector's tools; if still missing, say the connector needs
  the `projects` toolset and point to the doc. Do not try browser automation or GraphQL workarounds.
- Read fields first (`projects_list` method `list_project_fields`), then use exact field and option names.
  Move items with `projects_write` method `update_project_items` (up to 50 per call), referencing items by
  `item_owner` + `item_repo` + `issue_number`.
- Board writes from chat are owner actions done on request: setting Status, adding items, ordering. The
  factory itself never uses this connector or its token.

## Cheap monitoring

Prefer `run_task(name="factory.status", inputs={"name": "X"}, project=<project>, domain=<domain>)`,
then `get_run_io(name=<returned run id>, project=<project>, domain=<domain>)` for a one-call summary task.
For all live projects, `run_task(name="factory.queue_list", project=<project>, domain=<domain>)`, then
`get_run_io` gives the live run ids without paging through cron history.

Always filter `list_runs` by an exact `task_name`. Unfiltered listings waste about 500 tokens per run on cron
noise: `factory-reconcile.reconcile`, `factory.sweep`, and
`factory-deployer.deploy_if_changed` (the deployment reconcile; there is no separate reconcile task).
Find live work with `factory.queue_list` (above), not by listing work tasks: it returns every live project with its
run id, so you never need a slot or task name.

For each live run call `list_conditions(run_name=<run id>, limit=1000, project=<project>, domain=<domain>)`;
retain `phase == "PAUSED"` and names starting `direct-` or `ask-`. This avoids paging through unrelated runs.
The stock MCP has no phase/name-prefix filters and returns at most `limit` conditions; a full result may be
truncated. Do not conclude there are no open conditions from a truncated list. `factory.status` includes open
questions; if a direction inbox is absent, inspect the run in the console. The stock MCP condition listing omits prompts; read question text from `factory.status` or the console.
Send with `signal_condition(name=<condition name>, run_name=<run id>, value=<message or answer>,
project=<project>, domain=<domain>)`.

## Launch and queue

Launches never fail for capacity: one project runs per slot (or up to `[concurrency] max_projects`), and excess launches queue automatically.
Do not pick a slot. Call `run_task(name="factory.launch", inputs=...)`, then
`wait_for_run(name=<run id>, timeout_s=300)` and `get_run_io(name=<run id>)`. Inputs:

- `brief`: full markdown; see Writing the brief.
- `name`: lowercase slug of 2–41 letters, digits or hyphens; starts with a letter or digit, no trailing `-`, no `--`.
- `model`: always pass it; `opus` unless the user asks otherwise.
- `repo`: omit for the default; otherwise use an allowed repo only when requested.
- `issue`: optional `owner/repo#number` linking a GitHub work item; milestone comments need factory-issues.
- `demo_package`: optional package name for a recorded demo.
- `browser`: optional signed-in browser identities (comma-separated, e.g. `github`). Pass it only when the owner asked for that identity for that project; `none` revokes the grant on a resume.

JSON status is `started` (give the user the `url`), `queued` (report `position`), or `updated` (same waiting
name, replaced brief, same position). Failed launch runs have no outputs: read `get_logs` with a small
`max_lines` or `get_action`'s `error_info` for invalid names, oversized briefs, disallowed repos or running names.
For a live project send direction instead of launching again.

`factory.queue_list` takes no inputs and returns live projects, waiting launches in order and recent outcomes.
`factory.queue_cancel` with `inputs={"name": "X"}` removes a waiting launch and aborts its run if dispatch just
created it. Read results with `get_run_io`. Stop live work through its direction condition.

Launch the same `name` again after its run ends to continue from its last published workspace. Only files
resume, not the conversation; the new brief becomes further direction. If no readable published workspace
exists, the factory starts fresh and says so. Project-repo work lands under `{name}/`.

A grant carries to later generations of the project: resume without `browser` and it keeps the same identities;
`browser="none"` removes them, names replace them. An unknown identity refuses the launch. On an intake issue the
label `browser:<identity>` grants it; labels naming unknown identities are ignored and reported. `factory.queue_list`
and `factory.status` show a project's grant. `factory.browser_link` with `inputs={"identity": "github"}` returns
`{identity, url}`: a one-time sign-in/view link for the owner (valid ten minutes, single use; open it on the phone
while signed in to Union).

## Benchmarks

Call `run_task(name="factory-bench.benchmark", inputs=..., project=<project>, domain=<domain>)`.
Read the summary JSON and cases JSONL with `get_run_io`. Inputs:

- `candidate_sha`: required full 40-character commit SHA or ref in the factory repo, or in `source_repo`.
- `source_repo`, `source_path`: optional; benchmark a modified engine copy from a workspace project instead
  (a profile repo, and the subtree holding the engine's `src/`). Workspace copies are for benchmarking and
  testing only; production deploys what was merged.
- `baseline_sha`: optional SHA or ref; empty (default) runs only the candidate arm.
- `tasks`: suite task ids; empty or omitted runs all tasks. The suite contains `bugfix-shortlinks`,
  `feature-expiry`, `refactor-logstats`, `docs-standards`, `tf-bucket`, `ml-pipeline`, `vague` and `too-big`.
- `repeats`: 1–3, default 2, per task and arm.
- `candidate_policy`, `baseline_policy`: JSON object strings of launch policy keys, default `"{}"`.
- `reuse_baseline_days`: non-negative, default 7; limits the age of reusable baseline cases.
- `max_usd`: spend ceiling; default 0 returns an estimate and spends nothing.

Start with an estimate, for example
`inputs={"candidate_sha": "<full SHA>", "tasks": ["bugfix-shortlinks"], "repeats": 1, "max_usd": 0}`.
Read `estimate.ceiling_usd`, which includes both arms and stop margins. After the user authorizes spending,
repeat the same inputs with an explicit `max_usd` covering that ceiling and within the profile's
`[bench] max_usd`. Do not infer permission to spend from a request for an estimate.

The task refuses malformed SHAs or unresolved refs, unknown task ids, invalid policies, repeats outside
1–3, and an estimate above `max_usd` before deployment or case launches. A refused run has no outputs;
read `get_logs` with a small `max_lines` or `get_action`'s `error_info` for the reason.
Monitor with `list_runs(task_name="factory-bench.benchmark")`, `get_run_io` and the run report.

## Writing the brief

The brief drives implementation and review. Keep the user's wording; add environment facts in a marked
operator note. Include:

- What to build and checkable acceptance criteria, such as “malformed POST /x returns 400.”
- Fixed decisions, allowed edit paths and what to leave alone.
- Environment limits. Credentials and live infrastructure are absent unless arranged. Ask for static work
  first, then exactly the missing access; require NOT RUN for checks that need unavailable infrastructure.

Do not weaken criteria or rewrite the brief to pass review. Relay an explicit user scope change as direction.

## Filing issues and work items

Issues are problem and requirements documents, not implementation plans. They feed product discovery, where
the factory's product and designer roles work out the design; a brief comes later, from that discovery. When
filing or rewriting an issue from chat, use these sections:

- **Problem:** what is wrong or missing, in the user's terms.
- **Who and why:** who needs it and what they are trying to do.
- **Current state:** evidence only (file paths, run ids, PRs, limits observed), labelled as evidence, not a design.
- **Requirements:** what must be true when it is done, stated as capabilities and properties, not mechanisms.
- **Constraints:** fixed decisions the user has made, accepted risks, platform limits.
- **Acceptance (outcomes):** observable results a reviewer can check, not implementation steps.
- **Open questions for discovery:** the design choices left open, including alternatives the user raised.
- **Out of scope.**

Do not prescribe function names, flags, config keys, file layouts or a chosen architecture unless the user
fixed them; put candidate approaches under open questions instead. Keep the user's own words where they set a
requirement. Search for an existing issue before filing a new one.

## Status and verdicts

`factory.status` accepts a project name or run id and optional `rows` (default 40). A name selects its live run,
otherwise its newest ended run. It returns activity, dashboard/run phase, timeline, open questions, review,
PR URL, meters and `snapshot_age_s`. Check freshness; most errors include the run URL (an unknown name returns only `error` and `name`). `get_logs` starts at the
beginning and cannot show the newest progress of a long run.

Summarize current work, review round, blocking findings and who it is waiting on. Timeline markers:
`📝` session progress, `🔧` workers, `🔍 ❌ ✅` review, `🧭 ⚖️` planner/arbiter, `🙋` question,
`📨` direction, `🚨` attention, `⚠️` warning, `🛑 🏁` stopping/ended. Routine checkpoints need no narration.

**DONE is a claim, not a fact.** Report “declared done, review round N in progress” until the timeline says
`passed review in N round(s)`. `SUCCEEDED` only means the run ended; `/stop` also ends that way.
Usage-limit pauses resume automatically after reset unless the report requests human help.

With GitHub configured, work uses `factory/<name>`. Review opens a draft PR (ready where drafts are unavailable),
marks it ready on a pass and writes `factory/verifier` status. Pre-review check failures do not open a PR.
The factory never merges or force-pushes (except `[loop] merge = "on"` on the `[loop] repo` repository, which needs the App there); the user merges, or tells you to merge with the GitHub connector. Slot secrets are not an isolation boundary.

## Questions and direction

Condition names are `<kind>-a<attempt>-<seq>`. Use the cheap monitoring calls above to locate open conditions.

- `ask-...`: a question. Relay product choices, tradeoffs, money and irreversible decisions to the user; send
  their answer verbatim in `signal_condition`. Answer only what the conversation already settles. Match the
  condition's `expected_type` (boolean or free text). Timed questions state their default.
- `direct-...`: the running session's inbox. Signal a message explaining what changed, what to do and why.
  It enters the same conversation; another inbox opens after delivery. `/review-again <why>` grants another
  round after review escalates, at most twice.
- A credential question (an `ask-...` whose prompt starts "Credential request"): the project wants an AWS
  credential. Give the user the link in the prompt; they open it, paste a short-lived scoped credential, press
  Seal and bring back the sealed text (it starts with `fgc1.`). Relay exactly that text with `signal_condition`
  on the question, or `no` to refuse. **Never ask the user to paste a raw credential into chat**, and never
  repeat one. To give or refresh one the project did not ask for: `factory.credential_link` with
  `inputs={"name": "<project>", "credential": "<name>", "hours": 4}` returns `{project, credential, hours, url,
  paste_to}`; give the user the `url` and signal the sealed text they bring to the condition named `paste_to`.
  Revoke by signalling `/revoke-credential <name>` (or `/revoke-credential` for all) to the project's `direct-...`.
  `factory.credentials` lists every live project's grants (never values); `factory.status` returns one
  project's as `credentials`. See [credential-grants](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/credential-grants.md).
- `gcp-login-...`: human Google sign-in required through the prompt's link (`gcp_bootstrap`); relay it to
  the user.

After passing, the project asks a final question with its delivery summary; relay it. That question expires
in 30 minutes. A passed session releases its slot once it has been quiet for 30 minutes with no open
question: usually about 30 minutes after the pass, later only if the session or a human acts in between. Resume an ended project by launching its same name.

## Stop

Prefer `signal_condition` on the open `direct-...` with value `/stop`: clean exit, final snapshot, freed slot.
Use `abort_run` when a session is unresponsive: it has no direction inbox, is paused on a usage limit and ignores
direction, or is idle past its nudges. An aborted run publishes no workspace, so relaunching the name resumes from an
older published workspace, or starts empty: everything since the last successful run is lost. A session paused on
one engine's limit can be aborted and relaunched pinned to the engine that still has quota, with launch policy
`{"COORDINATOR_ENGINE":"claude","DELEGATE_ENGINE":"claude"}` (use the other engine's name when that is the one
with quota). Stopping a stuck run and relaunching it is a routine operator action: do it and report it. Queue
cancellation is for waiting launches.

## What else chat can do

The factory MCP has every stock Flyte tool, all limited to the factory's project and domain (a call naming another
is refused, and `list_projects` shows only this project). What you may do inside it is bounded by the caller's Union
permissions. Beyond launching and following projects:

- restart the deployer after a failed deploy (below, under deployment diagnostics);
- `list_secrets` shows secret names and types; no tool returns a value. Although `create_secret` and
  `delete_secret` exist, engine credentials must be stored or rotated with local commands, never sent through chat;
- `list_apps`, `get_app`, `activate_app` and `deactivate_app` manage the factory's apps; `list_triggers`,
  `get_trigger`, `activate_trigger` and `deactivate_trigger` its triggers (the queue sweep is one);
- `list_tasks` and `get_task` show what is registered; `rerun_run` repeats a run and `abort_action` stops one action;
- `search_flyte_sdk_examples`, `search_flyte_docs_examples` and `search_full_docs` search the Flyte docs.

Do not run a slot task directly: it is unsupported, because it skips validation and the queue. Launch through
`factory.launch`.

## Factory changes and deployment

The engine is a target repository like any other, and improving the factory is ordinary workspace work. The factory
never launches a project against the engine repository and never pushes to it: no project, launch or review writes
there. The only write is your `deliver` (step 4), on the person's word, which pushes one branch and opens a pull
request. Instead:

1. **Import.** Copy the engine into the workspace as a project folder
   (`PYTHONPATH=src python ops/promote.py import <engine-url> <workspace checkout>/projects/<name>`, run from an
   engine checkout; the project argument is a path inside your workspace checkout, never a relative `projects/<name>`
   in the engine checkout). This commits the copy and its `promote.toml` (the engine commit it is based on) to the
   workspace.
2. **Launch on the workspace.** Launch the project on the workspace repository (the default `repo`) with `name`
   equal to that folder's name. The brief says to make every change inside `projects/<name>/`.
3. **Review there.** The project is reviewed like any other, on its `factory/<name>` branch of the workspace.
4. **Deliver.** When it has passed, the operator delivers it upstream, on the person's word, with
   `PYTHONPATH=src python ops/promote.py deliver <workspace checkout>/projects/<name> <branch>`
   ([Promotion](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/architecture-repos.md#promotion)):
   that opens a pull request on the engine repository, whose CI is rerun first. The owner merges it, or tells you
   to and you merge it.

**The launch name must equal the engine folder's name.** The gate judges `projects/<launch name>/`, so a launch
whose name differs from the folder it works in is judged against an empty folder. Until engine issue #224 is fixed,
give each parallel engine project its own engine folder named after its launch (import again into
`<workspace checkout>/projects/<launch name>`); never point two launches at one folder.

When the work resolves a public issue, record its link on the workspace item when you file or
launch it, and pass `--fixes <owner>/<repo>#N` to `deliver`, which writes `Fixes <owner>/<repo>#N` into the pull
request body. After the owner-approved merge, confirm the public issue closed (close it on the owner's word if
GitHub did not) and mark the workspace item Done with a link to the merged pull request. Only irreversible actions, extra spend, external publishing or credentials need
the owner's `ask-human --approve`. To measure a modified copy against production before promoting it, benchmark it:
[Benchmarking an engine candidate](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/operating.md#benchmarking-an-engine-candidate).

With `[deploy] auto = true`, `factory-deployer.deploy_if_changed` deploys the default branch of `[deploy] repo`
once its CI has passed, to slots, their worker environments, factory, MCP and itself. The signed webhook triggers it
for eligible default-branch pushes and CI/status events; polling reconciles missed events every 60 minutes when
`webhook_secret` is set and `[deploy] webhook = true`, otherwise every five. It checks
CI and `factory/verifier`, skips pending/failing commits and ones with no passing check, retries a failed SHA
three times and waits for a new commit thereafter. With auto disabled, scheduled ticks do nothing. Running projects keep their starting version.
The deployed profile comes from the direction repo when configured, otherwise the deployed tree.

For deployment diagnostics, filter `list_runs(task_name="factory-deployer.deploy_if_changed", limit=5)` and
read small logs. `factory.read_login_meter` is internal, as are sweep and
worker tasks: use launch, status, queue_list, queue_cancel and `factory-bench.benchmark` for ordinary work.
After a failed deploy, `run_task(name="factory-deployer.deploy_if_changed")` (no inputs) retries it now instead of
waiting for the next tick; read the result with `get_run_io`.
`ops/deploy_*.py` are for first install/emergencies; rollback uses `ops/deploy_factory.py --ref <older commit>`
from that commit's checkout. Report or fix failed deploys through the factory.

## Engineering judgement versus owner decisions

When a session or the person's work raises an engineering ask, answer it by asking what is right, not only by choosing
among the listed options; the right answer may be none of them. Never choose a waiver, a lowered threshold, a removed
rule or a skipped test to make a check pass. Relay to the person only: spend, credentials, irreversible or external
actions, publishing, merges outside the sandbox, and product direction that is not yet set.

Routine operator actions are done, then reported, without asking: stop a stuck run and relaunch it; reorder the queue;
pin a project to the engine that has quota; fix a profile value that blocks a deploy; open pull requests for pushed
branches; file problem issues. Tell the person what you decided and why.

## Opening pull requests for factory branches

When `factory.status` reports a pushed branch (`factory/<name>`) without a pull request, open one with the GitHub
tools. Write a problem-first description: the problem, what changed, how it was checked and what is not covered. Then
watch CI and report failures; do not merge without the person's word.

## Profile changes and deploys

The profile lives in the workspace repo as `profile.toml`. The deployer validates the `[direction]` profile with both
the running deployer and the engine version being deployed. So a new key goes in only after the engine pull request
adding it is deployed, and a retired key leaves the profile first. After any merge or profile edit, check the newest
`factory-deployer.deploy_if_changed` run and its logs. A push to the direction repo triggers a tick through the
webhook. The deployer refuses to change its own `[direction]`, so moving onto it or changing it needs one local deploy
run with the bootstrap's `--redeploy` command ([references/setup.md](references/setup.md), First deploy), never
through the factory. Run it yourself on the person's word where you have a shell holding their deploy credentials;
otherwise say that, and give them the command with their values filled in.

## Related commands

`/since` summarizes everything since the person's last message (or the last check-in in a fresh chat). It is the
`since` skill, installed beside this one.
