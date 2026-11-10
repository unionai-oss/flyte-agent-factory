# Trigger cadence and event dispatch

Use events for normal work and an hourly safety net when a webhook is configured. An hourly tick
costs one twelfth of a five-minute tick, while the supervisor already writes board/milestones and
launches/handoffs already dispatch work. There is no evidence that five-minute repair is worth the
extra idle pods and API calls. Without a webhook, keep deployer and queue polling at five minutes:
there is no push/CI wake, so hourly polling would add up to 55 minutes to ordinary deployment latency.
Outcome repair remains hourly in either configuration. These values are fixed at deployment, not
runtime configuration. Redeploy factory environments to apply them, and work slots to install
the final completion notification.

## Cost and latency

A tick starts a task pod requesting at least about 1 CPU and 1 GiB. Counts below exclude events,
retries and real deployment work. Let `s` be the mean pod lifetime in seconds, including startup.
The minimum resource allocation per day is `ticks × s / 3600` CPU-hours and the same number of
GiB-hours. Multiply each by the backend's CPU/memory rates for currency; no price or measured
pod lifetime is assumed here. The deployer actually requests 2 GiB, so double its memory column.

| Cadence | Task runs/day per trigger | Minimum CPU-hours/day | Minimum GiB-hours/day | Missed-event wait until next tick |
| --- | ---: | ---: | ---: | --- |
| 5 minutes | 288 | 0.080 × s | 0.080 × s | at most 5 minutes; average 2.5 |
| 30 minutes | 48 | 0.0133 × s | 0.0133 × s | at most 30 minutes; average 15 |
| 60 minutes | 24 | 0.00667 × s | 0.00667 × s | at most 60 minutes; average 30 |

For example, at 60 seconds per tick these are 4.8, 0.8 and 0.4 CPU-hours/day per trigger.
With a webhook, the three safety-net triggers now start 72 pods/day (72 CPU-pod and at least
72 GiB-pod allocations), versus 384 before (288 repair + 48 deployer + 48 sweep), saving 312
scheduled pods/day. At 60 seconds each, minimum CPU allocation falls from 6.4 to 1.2 CPU-hours/day.
All three at five minutes would start 864 pods/day. Event submissions add work proportional to
activity; bursts coalesce in the app and repair writes only differences. The always-on webhook
requests 1 CPU and 512 MiB: 24 CPU-hours and 12 GiB-hours/day regardless of timer cadence.
It already serves deploy and intake events; this change adds no app replica.

The single-page quiet board/five-milestone example in [the webhook guide](deploy-webhook.md)
costs 2 GraphQL and 7 REST requests/tick: 576/2,016 a day at five minutes, 96/336 at thirty minutes,
and 48/168 hourly, plus event repairs. These are request counts, not measured billing. Observe
actual Flyte run counts, pod lifetime, event rate and GitHub requests after deployment before
claiming currency savings. A CI-heavy repository may spend more on event passes than idle polling;
coalescing bounds concurrency, and the documented counts make that comparison possible.

## Complete timer inventory

`_deploy.RECONCILE_MINUTES` is the shared hourly safety-net interval. Trigger definitions consume
it at import/deployment; `reconcile_minutes(profile)` retains the no-webhook polling exception.

| Definition | Trigger/task | Interval | Purpose |
| --- | --- | --- | --- |
| `_reconcile.py` | `factory-reconcile` / `factory-reconcile.reconcile` | 60 minutes, 24/day | Heal board and issue milestones |
| `_deployer.py` | `deploy-on-merge` / `factory-deployer.deploy_if_changed` | 60 minutes with webhook; 5 without | Missed push/CI delivery or polling without events |
| `_factory.py` | `queue_sweep` / `factory.sweep` | 5 minutes | Legacy queue repair after a crash or lost handoff; no admission in planner mode |
| `_webhook.py` | app-local planner wake timer | 60 minutes, at most 24 planner passes/day | Missed planner wakes, only with planner admission; uses the serialized consumer |
| `_bench_flow.py` | `collect_labels` / `factory-bench.collect_labels` | 1,440 minutes, 1/day | Collect production labels, not a dispatch safety net |
| `_loop_flow.py` | `factory-loop-backlog` / `factory-loop.generate_backlog` | 1,440 minutes, 1/day | Generate improvement backlog, independent of dispatch |
| `_factory.py` | `factory_gardening` / `factory.garden_weekly` | 10,080 minutes, 1/week | Queue the low-priority gardening project (`briefs/factory-gardening.md`); not a dispatch safety net |

The retired `issue_outbox` / `factory-issues.drain_outbox` and `factory-board-sync` /
`factory-board-sync.sync_board` schedules have no active trigger definitions. Factory deployment
deletes both old triggers. There are no other FixedRate definitions. The planner backend's two-second
terminal-phase check observes a known active planner; it is not a periodic scan that starts task pods.
Completion waits likewise observe one known project, and finish on its terminal phase.

## Failure modes and recovery

Normal events reach dispatch within delivery/submission time, without waiting for a timer. PR and CI
events wake the app consumer for configured repositories; project completion wakes repair/planning
only after Flyte reports a terminal run. A launch queued through the existing interface still wakes
the current admission path. A successful deploy that changes the profile's project-slot set also
wakes dispatch once: `factory.sweep` in legacy mode, or the webhook's signed internal planner wake
in planner mode. The successful release records its slot set, so later deploys with the same slots
do not wake dispatch again. A failed wake falls back to the safety net. None of these events switch
the configured admission mode.

A dropped GitHub event, rejected body, failed submission, crashed app or a project killed before its
last act leaves authoritative facts in Flyte/GitHub. The next successful hourly tick re-reads those
facts; a lost event alone therefore costs at most one tick plus queue/startup/repair runtime. The
board's two-hour ended-run window covers a one-hour miss, and milestones have a 24-hour window.
`test_dropped_webhook_event_is_healed_by_one_hourly_tick` deliberately delivers no PR-status webhook
event and shows one hourly task tick moving the stale board item to Done.

GitHub/Flyte outages, spot preemption and unschedulable pods can also lose a tick: the one-tick bound
assumes dependencies and capacity are available for that tick. Sustained outages extend it, and a
board repair delayed beyond two hours intentionally leaves owner-controlled state alone. Failed task
runs remain visible in Flyte. A consumer restart scans again, and a wake during an active pass leaves
one further pass dirty. Pending CI and coalesced deploy events can still defer deployment to the next
CI event or safety-net tick. The deployer's lease and planner's durable intent/lock retain their
existing concurrency protection. Event repair and scheduled repair can race at a tick boundary;
reconciliation is derived and idempotent, but its existing concurrent field-bootstrap limitation remains.

The no-webhook five-minute exception is a primary polling path, not a safety-net optimization. If
measured event failures or event volume justify a shorter interval later, compare the counts and
missed-event latency above before changing trigger definitions and their cadence tests.

## What only the hourly safety net catches

Each case below waits for the next tick of the timer named, so the worst case is one interval (60
minutes with a webhook) plus queue, startup and run time. Nothing in this change shortens a cadence.

| Case | Why no event arrives | Caught by | Worst-case wait |
| --- | --- | --- | --- |
| A slot killed outside the project process (OOM, node loss, manual abort) | The slot sends its completion wake as its last act, so a process that dies first never sends it | Planner timer (planner mode); `factory.sweep` and the reconcile (legacy) | 60 min |
| A lost or coalesced deploy event | A failed delivery is not retried; a CI event landing while a deploy run is live is folded into that run | `deploy-on-merge` | 60 min |
| A launch race with no later wake | A launch that lost a slot race has nothing to wake it once the slot frees by other means | Planner timer; `factory.sweep` (legacy) | 60 min |

Not on this list: a successful deploy that adds, removes or renames project slots; a board item
moved to Ready; and an issue opened, reopened, edited, labelled or unlabelled. Slot changes use the
post-deploy wake described above. Board and issue events wake the planner on delivery when the
GitHub App owner enables **Projects v2 item** (organization permission Projects: read) and **Issues**
events on the App (see
`docs/deploy-webhook.md`). Without **Projects v2 item** a board move waits for the planner timer, as it
did before events.

Noticing an abort or crash sooner is not cheap. The sweep, the reconcile and the planner timer are all
hourly, so having one of them look at released slots gains nothing over the planner's own pass. Nothing
tells the webhook when a run ends: the two-second terminal-phase check only follows a run that already
announced itself. A faster reader would need its own, shorter cadence, which is a cost decision for the
owner (see the cadence table above).

## User-owned boards

GitHub delivers `projects_v2_item` webhooks for organization boards only. On a board whose
`[board] owner_type` is `user`, a move to Ready never arrives as an event, so the webhook app keeps
the planner's poll at three minutes there, as before this change. It drops to the hourly safety net only for
`owner_type = "org"`. Moving the board to an organization makes board moves event-driven.
