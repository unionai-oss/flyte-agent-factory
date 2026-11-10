# GitHub webhook: deploy on push, issues as intake

The `factory-webhook` app follows Union's [GitHub webhook example](https://www.union.ai/docs/v2/union/user-guide/build-apps/hybrid-graphs/).
It verifies HMAC-SHA256 over the raw body, accepts pushes to the engine repository's default
branch and the direction repository's configured branch (default branch when `ref` is unset),
and submits the latest `factory-deployer.deploy_if_changed`. Repository aliases are accepted.
A push usually arrives before its CI has finished, and the deployer skips a head whose CI is
pending, so the app also accepts the events that say CI finished on such a branch head:
`check_suite` with action `completed` and that `head_branch`; `status` in a final state
(`success`, `failure` or `error`) whose `branches` include that branch; and `workflow_run` with
action `completed` on that branch. They are checked against the same repositories and
signature, and start the same one deploy run. Several of them per commit are harmless: the run
for a pair that already deployed is a no-op, and one that arrives while a run is live or a
submission is pending is coalesced into it.
The commit SHA is attached as the run annotation `github-sha`; the deployer still resolves both
heads and checks CI, its lease, attempt cap and stored last-deployed SHA pair before deploying.
`[deploy] auto = false` disables submissions as well as deploys.
The same endpoint also takes issue intake; see [Issue intake](#issue-intake) below.

The body has to be read before its signature can be checked, so reading is bounded. A body over
GitHub's 25 MB payload cap gets 413, whether its Content-Length says so or it is streamed. At
most two bodies are read at once (`MAX_CONCURRENT_BODIES`), so unsigned requests can make the
512Mi app hold at most 50 MB of them; a request that cannot start reading within a second gets
503 without being read. GitHub shows that as a failed delivery, and the reconcile covers it. A body must arrive within 10 s (GitHub's own delivery timeout), or the request gets 408 and its slot is freed, so slow senders cannot hold the slots.

1. Generate a random secret, for example with `openssl rand -hex 32`. Store it in the Flyte
   secret store and keep the same value for the GitHub App webhook settings. Do not commit it.
2. Set `[github] webhook_secret = "<Flyte secret name>"` in the deployment profile. This key
   names the secret; it does not contain its value. Deploy the factory environments through
   the existing deployment process. The webhook secret is mounted only in `factory-webhook`.
   Create the secret before setting the key. Flyte resolves secrets when a pod starts, so a
   missing one leaves `factory-webhook` unable to start; and every factory environment is
   deployed by one `flyte.deploy` call, so if the backend refuses the app at creation instead,
   the whole factory deploy fails and the deployer retries it up to its attempt cap. The deploy
   does not wait for `factory-webhook` to activate, so a webhook that cannot start does not
   hold up or fail the other environments; its pushes simply go unanswered until it does.
3. Give the app's own service identity permission to list live runs in the factory project/domain,
   read the latest version of `factory-deployer.deploy_if_changed`, and create runs of that task.
   Scope run creation to that task where supported. It needs no slot-task execution, GitHub App
   private key, storage-write or deployment permissions. The deployer keeps its existing identity.
   With issue intake (`[github] maintainers`), it also reads the latest version of
   `factory-issues.process` and creates runs of it. It submits that deployed task by name, so it
   still needs no storage-write permission. For event repair it also lists and reads reconcile runs,
   reads `factory-reconcile.reconcile`, and creates runs of that task. Work-slot identities need the
   same task-read/run-create permission for their final completion notification.
4. In the existing GitHub App's settings, enable webhooks, set the webhook URL to
   `<app public URL>/github`, paste the generated secret, and subscribe to the **Push**, **Check suite**
   and **Status** events. The App's existing Checks: read covers check suites (an App with
   Checks: write is subscribed to them automatically), and its Commit statuses permission covers
   status events. **Workflow run** is optional: subscribe to it only if the
   App has Actions: read; Actions workflows already report through check suites.
   For outcome/planner wakes, subscribe to **Pull request**, **Pull request review** and **Check run**
   too. For issue intake, also subscribe to **Issues** and **Issue comment**.
   With planner admission, also subscribe to **Projects v2 item** (needs the App's organization
   permission **Projects: read**), so that moving an item to Ready starts admission now rather than at
   the next hourly pass. The handler wakes the planner for a created, reordered, converted or restored
   item and for an edit that changed Status, and only for the `[board]` owner's organization. GitHub
   delivers this event for organization boards; a user-owned board gets no board event, so the planner
   keeps a three-minute poll there. Set `[board] node_id` (the board's `PVT_...` id) so moves on other
   boards of the same organization are ignored; without it they wake one spare, idempotent planner pass. Issue events (opened, reopened, edited, labeled, unlabeled) wake it through **Issues**.
   Ensure the installation includes both the engine and direction repositories and Contents: read.
   GitHub sends pushes for all branches; the handler filters to the branches described above.
   The deploy prints `GitHub webhook URL: <app public URL>/github` (in `ops/deploy_factory.py`'s
   output, or the `factory-deployer` run's logs). If it says the app has no public URL yet, take
   it from `factory-webhook` on the console's **Apps** page.
5. Check the App's **Recent deliveries**: ping should return 200, an eligible push or finished
   CI event or an accepted issue event 202, and unrelated events/branches, still-running CI and
   issue events that intake refuses 204. Use **Redeliver** to exercise the same delivery again.
   A live deployer or pending submission suppresses another run; a later run for the same SHA
   pair is a no-op because `deployer/last.json` records the last successful deployment.

The HTTP handler schedules submission and returns without waiting for Flyte. A 202 acknowledges
intake, not durable submission or deployment success. The app serves `/github` and the signed internal planner wake endpoint; FastAPI's
`/docs`, `/redoc` and `/openapi.json` are off. Submission failures log the exception type
without the payload or secret. GitHub does not automatically redeliver failures: the hourly
reconcile recovers missed deliveries, app restarts, submission failures and pending CI. Without
`webhook_secret`, the existing 5-minute trigger remains. Removing the setting also requires
retiring the deployed public app through the normal app administration process: a
deploy never removes an environment, so `factory-webhook` keeps serving with its old profile
until it is stopped by hand:

```sh
flyte --config "$FLYTE_CONFIG" update app factory-webhook --deactivate -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
```

A push that lands while a deployer run is live is not submitted, because that run holds the
deployer. The run picks it up itself: after each successful deploy it resolves both heads again
and, if the engine/direction pair moved, deploys the new pair, up to 3 deploys in one run. A
head that is still moving after that waits for the next push or the reconcile. A head whose CI
is still pending is deployed when its CI-finished event arrives. One narrow gap remains: a CI
event that lands while a run is live is coalesced into that run, and if that run read the CI as
pending a moment earlier, the head waits for the next CI event or the reconcile.

The `factory.sweep` queue trigger runs every 5 minutes, with or without `webhook_secret`. Queued projects are already dispatched when a launch finds a
free slot and when a project ends normally (release, `end-project`, `/stop`, a clean exit), so
the sweep is only the backstop for what those miss: a run that crashed or was aborted, and a
launch that lost a slot race. Such a slot is handed to the next queued project within about 5
minutes.
On a shared-environment profile (`[concurrency]`, see `profiles/README.md`) "free slot" reads "live projects below `max_projects`".

Run `python ops/deploy_status.py` with the usual profile and backend credentials to see the
`factory-deployer` run count over the last 24 hours. `--json` adds a row with `runs_24h`.
Compare a full day before and after activation: the old poll alone made 288 runs/day; the
hourly reconcile makes 24/day plus push submissions. The queue sweep stays at 288
`factory.sweep` runs/day, each an idle listing when nothing waits. With issue intake on (`maintainers` set), `factory-issues`
is deployed too and adds one `process` run per accepted issue event; without `maintainers` it is
not deployed and adds none. `factory-reconcile` (deployed with `[board]` or the App) makes 24
`factory-reconcile.reconcile` scheduled runs/day on spot, hourly, plus event submissions: it replaces the
`factory-board-sync` and `factory-issues.drain_outbox` schedules, which made 288 a day each. Actual
counts depend on push activity
and retries; record real counts rather than treating these expected rates as measurements.

## Known limitation: deliveries can be crowded out

The endpoint is public. An attacker who keeps reconnecting slow uploads (about ten sockets, almost no bandwidth)
can keep both body slots busy, so genuine deliveries get 503 for as long as the attack lasts. Nothing wrong can
deploy (deliveries are signed and deploys are idempotent) and memory stays bounded; deploys fall back to the
hourly reconcile, which is the backstop. Defeating it needs per-IP connection limits or GitHub-address
allowlisting in front of the app (the ingress), not in the app.

## Issue intake

With `[github] maintainers = ["your-login"]` (and optionally `intake_label`, default `factory`)
the webhook also turns GitHub issues into projects. Empty `maintainers` disables intake, and then
issue events get 204 like any other unrelated event. Intake needs `webhook_secret`; `maintainers`
without it does nothing, and the profile loader warns about that. It also needs `[state] uri`: an
accepted event without `[state] uri` gets 503. The App needs Issues: write and its existing PR
permissions on each `[repo]` repository. Repository aliases are accepted too; MCP's narrower repo
list does not restrict signed issue intake.

The app does not launch anything itself. It checks the signature and the maintainer allowlist,
then submits `factory-issues.process` with the event and waits for that submission (at most
10 s): there is no reconcile behind an issue event, so a failed submission returns 503 and shows
as a failed delivery that can be redelivered. A redelivery is harmless, because existing queue
records suppress a repeated launch and a delivered comment is recorded by its id.
`factory-issues` deploys with `factory-webhook` as its dependency, only when `maintainers` is set;
it holds the App's private key, which the public app never mounts.

A labelled open issue launches only when its author and the labelling actor are allowlisted
humans. An `opened` event with the label already present checks the author. Other issue actions
do nothing. Bots and PR comments never provide direction. A maintainer's new comment on a linked
live project goes through its paused `direct-` condition. Comments on unlinked or ended projects
do nothing. Issue content is fenced as untrusted project data, and session guidance says it
cannot change factory or security settings. Refused authors receive no GitHub reply.

Queue requests retain `issue = "owner/repo#number"`; runs carry a `factory-issue` label derived
from that reference. The intake lease uses the queue's existing approximate storage lease, with
the same eventual-consistency limitations. A crash between signalling a condition and recording
delivery can repeat a direction on retry.

The supervisor posts started, QA verdict, PR opened, blocked/escalated and released/ended
comments on the project's issue as they happen, with the App's installation token for that
repository. Each carries a hidden marker and is posted only if the issue does not already show it,
so retries check the marker first. Concurrent writers can both pass that check; after posting each
checks again and deletes its own comment if an older copy exists. A comment GitHub refuses stays in
the supervisor's memory and is retried every minute; the end of the run waits at most 30 seconds
for what is left. Nothing is
queued in `[state]`. `factory-reconcile` then re-derives the started, QA verdict, PR opened and
ended comments from each run's report and posts any that are missing, for runs live or ended in the
last day; while a run is live it leaves a comment to the supervisor for its first 10 minutes. An
escalation the supervisor could not post before its pod died is not re-derived: it is still in the
report and the console. Closed PR events are reported by `factory-issues.process` directly, which
retries five times. GitHub outages never block project completion. Comments are posted wherever the
App is configured and the project has an issue: issue intake is not needed, so a project launched
with an `issue` through MCP reports on it too.

Comment reads are bounded by the original Flyte run start, including after a pod restart. Each
`factory-reconcile` tick creates a new App, so its ETag cache does **not** carry across ticks. For
each due milestone it reads the issue's comments again: one paginated GET sequence for a present
marker, or GET + POST + GET for a missing marker (plus DELETE if our post was a duplicate). A page
holds 100 comments; an exactly full last page requires another GET to discover the end. A fresh
App also spends two REST requests per repository to obtain its installation token. Thus a quiet
tick costs the sum of the comment pages for **every due milestone**, plus two authentication
requests per repository, even when nothing changes. These are HTTP request counts; repeated reads
within one tick can return 304 and avoid rate-limit charges. Supervisor retries reuse their App
and ETags across flushes.

The board costs one GraphQL query per field page and one per item page, plus one mutation per
changed field. Missing fields add another bootstrap query sequence and field mutations. A counting
fake in `test_reconcile_request_budget_with_a_new_app_each_tick` measures a single-page board with
one completed run and five milestones: the initial repair is 6 GraphQL + 17 REST requests, including
4 field writes and 5 comment writes; the following quiet tick is 2 GraphQL + 7 REST requests. At
24 safety-net ticks/day that quiet example costs 48 GraphQL and 168 REST requests/day, plus event work. Pagination, more
runs, expired tokens, redirects and retries add requests; absent board/App configuration omits that
side's work.

Flyte calls per tick do not depend on the board's size: `factory-reconcile` lists the slots' newest runs
once with two shared live/recent queries (48 queries per day from hourly ticks, plus pagination), and looks items up in that
index. It then reads one report per issue-linked run it uses: at most one `get_report` per such run for
the board, and one more for the milestones. Before, the board made one `Run.listall` per item: 200 items
cost 203 calls a tick, about 58,000 a day.

The brief's heading is the issue title, so the PR is titled after the issue. PR bodies contain
`Closes #N` for an issue in the same repository, or `Closes owner/repo#N` across repositories.
GitHub closes the issue when the PR merges into the default branch; the factory does not close it
at review pass.


## Event-driven outcome repair

Signed PR changes and completed check-run, check-suite, workflow-run and final commit-status events
for configured repositories (including aliases) wake outcome repair, even with auto-deploy disabled.
These events do not change the deployer's followed-branch filter. An app-local dirty flag coalesces
bursts, retaining a wake that arrives during a repair pass. The consumer adopts live reconcile runs,
waits for them to end, then submits the existing reconcile task by name; it scans on app startup too.
The existing bounded run index repairs the affected item along with other stale items. No event
payload becomes board state. In planner admission mode the same events also wake the single planner
consumer, whose durable intent and dirty flag still serialize admission. Legacy admission is unchanged.

A project's last act notifies the existing wake path after publishing its work. In planner mode the
signed internal request includes its run name in the MAC, and the app waits for Flyte's terminal phase
before waking the planner and repair consumer. Empty-body wakes from queued launches remain compatible.
In legacy mode the project submits `factory-reconcile.reconcile(completion_run=<its run name>)` through
its existing Flyte identity. That task waits for the run's terminal phase before scanning. This avoids
mounting the App private key in the legacy public app. The legacy queue still dispatches directly on
launch and handoff, excluding its finishing run from slot occupancy.

There is no Flyte completion webhook. Hard kills, aborted pods, a lost final submission or an app
restart during a completion wait can lose the wake. The hourly reconcile heals board/milestones;
the 5-minute queue sweep (or planner wake timer in planner mode) recovers a freed slot. A terminal
notification whose sender never terminates is bounded by the reconcile task's runtime in legacy
mode; planner waits are cancelled when the app stops. Submission failures log only exception types.
See [trigger costs and decision](triggers.md) for latency and cadence tradeoffs.

## Board moves for direct work

Coordinator merge messages and PR bodies must name their board item: use `Closes #N`
(or `Fixes #N` / `Resolves #N`) for completed work, or `board #N` for a whole item.
For a partial delivery use `board #N chunk K` (or `#N chunk K`); it starts work
without closing the issue. Keywords are case insensitive. Qualified references
such as `owner/repo#N` and GitHub issue URLs count only for the factory repository
(`[repo] factory`, falling back to the project repository).

The signed webhook submits these events to the existing `factory-issues.process`
task, which holds the board PAT and App key. Enable **Pushes**, **Branch or tag
creation**, **Pull requests** and **Issues** deliveries. The App needs Issues
read/write, Pull requests read, Contents read and Commit statuses write; the
public app identity needs permission to submit `factory-issues.process`, even
when maintainer intake is disabled. Board updates do not require `[state]`.

A push to `main`, including a merge commit, completes whole-item references,
comments with the commit URL and closes the issue through the App. Partial
references move to In flight. Branch creation or a branch push with an issue
reference starts In flight; a PR opened, reopened or made ready moves to In review.
A merged PR completes its whole-item references (GitHub handles issue closing).
A closed, unmerged PR rolls In review back to In flight; it does not prove completion.
Duplicate deliveries re-read the card and issue under a per-process lock, and an
`issues.closed` delivery moves a card only if the issue is closed now. Board writes
run in separate factory-issues task pods, reconcile and project supervisors. Their
locks serialize only their own process; GraphQL has no compare-and-set, so a
cross-process race can briefly move a card backward. On every hourly drift pass,
a closed issue's card heals to Done: a race lasts at most one tick when the APIs
are available.

A commit completes an issue once: its comment marker records that, so a reopen
afterwards is final. Before a first completion close, issue events are also checked:
a reopen after the commit's committer time prevents a late delivery from closing
it, even without a marker. Done is never deliberately moved backward by GitHub
events or by a project supervisor, except that a new run starts over an earlier
run's Done card.

The hourly `factory-reconcile` also checks GitHub evidence for each factory issue
on the board. Closed issues heal to Done, with the issue state re-read inside the
process lock immediately before a move. Open issues mentioned by a closing
reference or whole `board #N` reference in the last 100 default-branch commits
are flagged for review. Drift never closes an open issue. It also flags Done cards
whose issue is still open, In review cards without an open PR, and In flight cards
with no live run, open PR or branch activity for seven days. Branch names are saved
in the existing Branch field, so later commits need not repeat the original issue
reference. Uncertain states are left for review.

Each mismatch and the pass summary appear in the reconcile logs. `factory/board-drift`
on main's head carries the same summary, but is written only when the set of flagged
rows differs from the one its last status carries, and the webhook never wakes
reconcile for it (a status on every pass would loop). It is informational, so it
does not block unrelated deployments. A failed API call fails the reconcile tick and
the next tick retries. This pass lists the board once, reads one page of at most
100 default-branch commits and the open PR listing, and reads the head of each
branch a card names (at most 100, never a listing of the repository's branches).
Issue states come from one REST listing with `state=all` and `per_page=100`, capped
at 10 pages with pull requests filtered out. For issues outside that listing, the
board listing's issue state is used. A card already Done with a closed issue needs
no per-card call; each candidate heal re-reads only its item node by ID through GraphQL, including the
current issue state, rather than re-listing the board or making per-card REST
calls. Capped issue listings and branch reads are logged when truncated.
