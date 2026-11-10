# The required CI signal: the factory's own checks, or a runner on the cluster

**For the owner to decide. Nothing here is implemented.** Today the signal is GitHub-hosted Actions
([ci.md](ci.md)); this page lays out the two alternatives raised in issue #110 and what each would take.

## What the signal has to do

Whatever produces it, two readers consume it through `_github.commit_health`
([`src/flyte_agent_factory/_github.py`](../src/flyte_agent_factory/_github.py)), which reads a commit's
**statuses and check-runs** together:

- **The auto-deployer** (`_deployer._not_deployable`) deploys main's head unless a status or check-run on it is
  failing or pending. A commit with none at all counts as deployable, so removing a signal without replacing it
  silently turns "green" into "nobody looked".
- **The loop's merge rule** (`_loop_flow`) merges a loop pull request only when its head has `factory/verifier`
  `success` and `commit_health` is `ok`.

A replacement must therefore land a result **on main's head commit** (squash merges make a new SHA that no
pull-request run ever saw) and on pull-request heads, as a status or check-run, before the deployer's next tick.

## The cost today

GitHub-hosted Linux runners cost $0.006 a minute on a private repository (2026 pricing), so the $10 left for the
month is about 1,600 minutes. Before [ci.md](ci.md)'s cuts a run billed about 23 minutes; after them about 11, so
roughly 140 runs. **On a public repository standard GitHub-hosted runners are free**, so after the cutover to
`unionai-oss/flyte-agent-factory` the minutes problem goes away by itself. The decision below is about the weeks
until then, and about which signal the public repository should require.

## Option A: the factory's pre-review checks as the signal

The factory already runs each project's `checks.sh` (here `factory-improvement/checks.sh`) and the standards
gate when a session declares DONE, in the supervisor's pod with both pytest lanes in child actions of their own
(`_slow_lane`), and posts `factory/verifier` on the reviewed pull-request head after review passes
(`_pull_requests.STATUS_CONTEXT`).

What it would take to make that the required signal:

1. **A post-merge check of main.** The pre-review runs on the branch head, not on what lands on main. The deployer
   (or a small scheduled task beside it) would run `checks.sh` with `AGENT_FACTORY_SLOW_LANE=child` on each new main
   head and post a status, e.g. `factory/checks`, `pending` first and then `success` or `failure`, with the run's
   URL. The deployer already holds the App credentials and already waits on `pending`.
2. **The same for pull requests not opened by the factory** (the owner's own, contributors'), or the owner accepts
   that those get no signal.
3. Drop the Actions workflow, or keep it on `workflow_dispatch` only.

| | |
|---|---|
| Cost | Cluster CPU only: two spot child actions of ~5 and ~9 minutes per main commit. No Actions minutes. |
| Trust | The status is posted by the factory's GitHub App, so a required check can be pinned to its `app_id` (see [branch-protection.md](branch-protection.md)). But the checker and the thing checked are the same system: a factory bug that breaks the checks can also break the reporting of them, and a deployer outage means no signal at all, which `commit_health` reads as pending, not green (good) — unless the status was never created, which reads as deployable (bad; the deployer would have to require `factory/checks` explicitly rather than "nothing failing"). |
| Public repo | A contributor's fork PR gets nothing unless the factory runs untrusted code from forks in the cluster — the same hazard as option B. Public projects are expected to show Actions results on PRs. |
| Work | A new deployer step, a status context, a change to `commit_health` or the deployer to require that context, tests. Moderate: days, not weeks. |
| Failure mode | Silent: the factory is down and nothing runs. Actions keeps running when the factory is down. |

## Option B: a self-hosted GitHub Actions runner on the cluster

Run Actions jobs on the cluster: GitHub's Actions Runner Controller (ARC) in Kubernetes, with ephemeral runner
pods, and `runs-on: [self-hosted, ...]` in `checks.yml`.

| | |
|---|---|
| Cost | No Actions minutes (GitHub announced a $0.002/minute fee for self-hosted runners in Dec 2025 and postponed it; check before relying on it). Cluster cost of always-on controller plus runner pods. |
| Trust | Unchanged from today: check-runs posted by GitHub from the workflow, so `commit_health`, the deployer and any future required checks keep working as they are. |
| Public repo | **The blocker.** GitHub's guidance is not to use self-hosted runners on public repositories: any fork pull request can run arbitrary code on the runner, and here the runner sits on the cluster that holds the factory's secrets. It would need fork PRs gated behind approval, runners in an isolated namespace with no cluster credentials, and ephemeral pods. And once public, hosted runners are free anyway. |
| Work | Installing ARC needs cluster-admin on the Kubernetes cluster under Flyte. On a Union-hosted data plane that is Union's, not ours; on the personal GKE cluster it is a Helm chart plus a GitHub App or PAT for runner registration. The runner image must carry what the slow lane installs (Terraform, Playwright and Chromium, gitleaks, shellcheck). |
| Failure mode | Jobs queue forever when no runner is up, which reads as pending: safe for the deployer, but it blocks merges. |

## Side by side

| | Hosted Actions (today, with the cuts) | A: factory checks | B: cluster runner |
|---|---|---|---|
| Minutes before cutover | ~11 per run | 0 | 0 |
| Minutes after cutover | free | 0 | 0 |
| Fork PRs on the public repo | yes, safely | no | only with isolation work |
| Independent of the factory being up | yes | no | yes |
| Code to write | none | moderate | infra, little code |

## Recommendation

Neither, for now. Keep GitHub-hosted Actions with the cuts in [ci.md](ci.md) until the cutover: at about 11
billed minutes a run the remaining budget covers roughly 140 runs, and the weekly backstop run is about 25 more
minutes a week. After the cutover the public repository runs hosted Actions free, which is also what contributors
expect to see on their pull requests.

If the budget runs short before the cutover, the cheapest stopgap is not A or B but running the slow lane on main
only (not on pull requests), which saves about 7 minutes per pull-request push; say if you want it.

Option A remains worth doing later as a **second** signal rather than the required one: a `factory/checks` status
on main's head that the deployer requires, so a deploy waits for the factory's own checks as well as CI's.

## Decision needed

1. Stay on hosted Actions with the cuts (recommended), A, or B?
2. If the budget runs out before the cutover: slow lane on main only, or pause CI and rely on the pre-review?
