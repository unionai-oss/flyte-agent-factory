# CI: what runs for a change

The engine's GitHub Actions workflow is [`.github/workflows/checks.yml`](../.github/workflows/checks.yml). It
runs what [`factory-improvement/checks.sh`](../factory-improvement/checks.sh) runs locally, split into jobs, and
the factory's own standards gate, which blocks. [What `main` requires](#what-main-requires) lists the checks branch
protection should require.

## What "green" means

Two things read the result, both through `_github.commit_health`
([`src/flyte_agent_factory/_github.py`](../src/flyte_agent_factory/_github.py)):

- **The auto-deployer** deploys main's head only when no check-run on it is failing (`failure`, `timed_out`,
  `cancelled`, `action_required`) or still running, and no check suite is still running (see
  [Between jobs](#between-jobs)). A commit with no check-runs at all counts as deployable.
- **The loop's merge rule** merges a loop pull request only when its head is `ok`.

A job skipped by its `if:` (or because a job it `needs` failed) reports a check-run with conclusion `skipped`,
which counts as passing. So the rules that decide what to skip decide what green means. They live in [`.github/ci_lanes.py`](../.github/ci_lanes.py),
under `.github/`, which is an owner path (`_loop.OWNER_PATHS`): a loop pull request that touches it goes to the
owner rather than merging itself.

## The jobs

| Job (check-run) | Runs when | Typical time |
|---|---|---|
| `Changes` | always, except a superseded pull-request run | under a minute |
| `Tests (not slow)` | every run | ~6 minutes |
| `Tests (slow)` | every run | ~7 minutes |
| `Lint` | a change to a `.py`, a `.sh`, an `ops/**.md`, `pyproject.toml`, `uv.lock` or `.github/` | under a minute |
| `Standards gate` | every run, once both test jobs have passed | ~1-2 minutes after them (see below) |

### `Standards gate`

The factory's own gate, held to the bar it enforces on projects: `python -m flyte_agent_factory._gates standards .
--standards src/flyte_agent_factory/_assets/workspace_template/standards`, the code the DONE gate uses, against the
workspace template's example set (the engine has no standards of its own). Every MUST rule of that set runs; none
is skipped or waived for the engine. A failing rule fails the check-run, so a pull request's check is red and the
deployer skips a main commit that fails it.

Nothing it judges is run twice:

- **Tests.** Its test rule (`python -m pytest`, both lanes, as the DONE gate runs it) is judged from the junit
  files the two test jobs upload, through `--pytest-junit`: `fast.xml`, `slow.xml` and `head`, the commit they ran
  on. The gate takes them only when `head` is the commit it is checking and every test in both passed; otherwise
  (a missing or unparsable file, another commit) it runs the suite itself. The gate job does not install gitleaks,
  Playwright or the owner literal list, so a suite it runs itself would fail rather than pass on a test that needs
  them; the junit artifacts are kept seven days so that re-running the gate job reuses them. It `needs` both test jobs, so it starts
  when they have passed. When a test job fails the gate is skipped (a `skipped` check-run) and the failing test job
  is what fails the commit.
- **Lint.** Its ruff rules (`ruff check .`, `ruff format --check .`, and the docstring and `B006` selections) and its
  shellcheck rule read the same files `Lint` used to, with the same tool versions (`uv.lock`) and this repo's ruff
  config, so `Lint` no longer runs them. `Lint` keeps only shellcheck over the tracked `.claude-flyte/` scripts,
  which the gate's file walk skips.
- **Measured.** On a 16-CPU pod (2026-11-07), the gate step reading both lanes' junit took 16.5 seconds, every
  rule included; the same command without `--pytest-junit` took 12 minutes, nearly all of it the suite run again.
  In CI the job adds its setup (checkout, `uv sync`, the Terraform and shellcheck downloads, tflint from the cache) and a mypy
  that may start from an older cache on a 4-CPU runner.
- **Type check.** mypy runs only here. Its incremental cache is kept between runs (`actions/cache`, keyed on
  `uv.lock` and `pyproject.toml`), so a run re-checks what changed.

The job installs every tool a rule needs on this tree (shellcheck, and Terraform and tflint for the Terraform pack,
which the benchmark fixtures' `.tf` files select): a missing tool reports `tool unavailable`, which blocks. It
checks out two commits, so the module-size rule measures growth against the commit before (on a pull request, the
base it merges into; on a push of several commits to main, the last one's parent): a module already over the limit
that did not grow is reported as debt, not a failure.

### Why both test jobs run on every change

Before the gate blocked, `Tests (not slow)` and `Tests (slow)` ran only for a change that could affect them
(`.github/ci_lanes.py`'s `fast` and `slow` lanes). The gate's test rule covers both lanes on every commit, so a
skipped lane would make the gate run it itself, after the other lane, adding its minutes to the wall time. Running
both jobs every time, side by side, is the shortest wall time the gate allows: about max(fast, slow) plus the gate's
own minute or two. `ci_lanes.py` still prints all three lanes in the `Changes` log; only `lint` gates a job.

`Changes` runs `ci_lanes.py`, which prints one `lane=true|false` per lane:

- **`lint`** runs for ruff's and shellcheck's inputs, the files `Lint` read when it ran both. That is a superset of
  what it reads now, which only makes it run more often than it must.
- **`fast`** and **`slow`** are reported but gate no job (above).
- A change to `.github/`, `pyproject.toml`, `uv.lock`, `factory-improvement/checks.sh` or `tests/conftest.py`
  sets every lane.

## What `main` requires

Branch protection on `main` should require exactly these check-runs, all from GitHub Actions:

- `Changes`
- `Tests (not slow)`
- `Tests (slow)`
- `Lint`
- `Standards gate`

GitHub counts a `skipped` required check as passing, as `commit_health` does. That is safe here: `Lint` is skipped
only for a change that holds nothing it reads, and `Standards gate` is skipped only when a test job, itself
required, has failed. [`branch-protection.md`](branch-protection.md#the-factory-repository-itself) has the one
command that applies this and the command that verifies it. A test (`tests/test_ci_workflow.py`) keeps this list
equal to the workflow's job names.

## Against what a change is measured

- **A pull request**: its merge commit against the merge commit's first parent, i.e. exactly what merging would
  bring onto the base branch now.
- **A push to main**: against the previous main head (`github.event.before`). Every change that lands on main
  thus runs every check on main itself, whatever ran on its pull request.
- **Fails closed.** When the change cannot be listed (a new branch, a force push whose `before` is gone, a git
  error), every lane runs. If `Changes` itself fails, its check-run fails and the commit is not green.

## Between jobs

GitHub creates a job's check-run only when the job is queued, i.e. once the jobs it `needs` have finished. In the
seconds between the test jobs finishing and `Standards gate` being queued, every check-run on the commit has
passed; and just after a push, before Actions has queued any job, there are no check-runs at all. `commit_health`
therefore also reads the commit's check suites: a GitHub Actions suite that is not `completed` makes the commit
`pending`, so neither the deployer nor the loop's merge rule acts on it before every job, the gate included, has
reported. Another App's suite counts only once it holds a check-run: GitHub leaves the suite of an App that never
posts one queued forever.

## Scanners: gitleaks and tflint

No gate depends on a live download of either. Both are pinned once, version and archive SHA-256, in
[`src/flyte_agent_factory/_scanners.py`](../src/flyte_agent_factory/_scanners.py) (standard library only), which the
factory image, CI and the tests read; `tests/test_scanners.py` fails if another file declares a pin.

- **Factory image.** `_images.agent_image()` bakes both into `/usr/local/bin` at build time (`curl --retry`, then
  `sha256sum -c`, then the binary is run). Project pods and the pre-review
  test lanes (`_slow_lane`, a container on the same image) use those binaries; nothing in the lanes or the gate
  fetches a scanner. Pods started before the engine is redeployed keep the old image until they restart.
- **CI.** Each job that needs a scanner (both test jobs: gitleaks and tflint; `Standards gate`: tflint) computes
  `scanner-<tool>-<version>-<sha256>` with `_scanners.py cache-key`, restores that key with
  `actions/cache/restore`, and runs `_scanners.py install`. The installer checks the cached archive's SHA-256 and,
  when it matches, makes no request at all. On a cold cache (or a cached archive that fails its checksum) it
  downloads with up to 6 attempts, backing off 5, 10, 20, 40 and 80 seconds, verifies the checksum, and the job saves the
  archive straight away (`actions/cache/save`), before its tests, so a failing test does not cost the next run a
  download. A cache entry is immutable per key, so a corrupted cached archive is re-downloaded, verified, on every
  run until GitHub evicts it.
- **Never skipped.** If every attempt fails, the install step fails the job with `could not obtain <tool> <version>
  from <url>`. A test that needs a scanner fails when the binary is missing, wherever it runs. For local setup,
  run `python3 src/flyte_agent_factory/_scanners.py install --cache ~/.cache/factory-scanners --bin ~/.local/bin <tool>`
  and include `~/.local/bin` on PATH. Nothing waives a scan.

To move a pin: change the version, URL and SHA-256 (from the release's checksums file) in `_scanners.py`; the cache
key changes with it, so the first CI run downloads once and the image rebuilds on the next deploy.

## The owner literal list

The personal-literal guard in the fast lane checks generic shapes, plus the owner's own identifiers when the
repository secret `FACTORY_OWNER_LITERALS` holds their list (one regex per line; see
[`ops/personal_literals.py`](../ops/personal_literals.py)). A fork's pull request gets no secrets, so there the
guard checks the generic shapes only.

The fast lane's `Scan the public tree` step builds the tree the cutover publishes (`ops/public_tree.py build`)
and runs `ops/scan_public.py` on all of it. Its log says how many owner literals it loaded (or that it checked
generic shapes only) and how many files it read, without printing the list.

## Concurrency

A newer push to a pull request cancels that pull request's older run. Runs on main are never cancelled: a
cancelled check-run is failing to `commit_health`, so the deployer would refuse that commit. Every non-PR run is
its own concurrency group, so nothing on main waits behind or replaces anything else.

## The weekly run

The weekly scheduled run (Mondays, 06:17 UTC) and any manual run (`gh workflow run checks.yml`) run every job on
main's head, so a change to a tool or a runner image surfaces within a week even when nothing lands. While that run
is in progress main's head reads as pending, and the deployer retries on its next tick.

## Minutes

Per run: `Changes` 1, fast 6, slow 7, lint 1 x 0.91 (measured over the 204 first-parent commits on main from
2026-10-15 to 2026-11-05) and the gate about 2 (setup, `uv sync`, mypy with its cache, ruff, shellcheck, Terraform
and tflint), each rounded up to the minute: about 17 billed minutes. The lanes had brought it to about 11, with the
slow lane running for 39% of commits and the gate once a week; running the slow lane and the gate on every commit is
the price of the gate blocking. Without the junit reuse the gate would add about 9 minutes of its own to every run,
after the tests. A pull request pays the same per push, less every run a newer push cancels.
