# Repositories: the engine and the workspace

Two kinds of repository. This replaces the three-repo split (engine, sandbox, direction) of the earlier design note.

| Repository | Holds | Who writes it |
|---|---|---|
| Engine, [unionai-oss/flyte-agent-factory](https://github.com/unionai-oss/flyte-agent-factory) | the factory's code. Deployments run its default-branch head once its CI passes | pull requests only: maintainers', and yours, promoted from a workspace project like any other target ([Promotion](#promotion)) |
| Workspace template, bundled in the engine | the starting layout; a separate `unionai-oss/flyte-agent-workspace` repository is not published yet (optional, later) | maintainers; shipped in the engine at `src/flyte_agent_factory/_assets/workspace_template/` |
| Workspace, `<user>/agent-workspace` | one user's standards, profile, projects and backlog | the user, and the factory in a project's own directory under `projects/` |

## The workspace

- `standards/` is seeded from the template's example set and edited by hand ([standards.md](standards.md) is the format).
  It is read-only to the factory: the gate rejects a patch that touches it, a session's May-edit scope excludes it, and
  the template ships CODEOWNERS for it and `docs/branch-protection.md`. The template's copy is the source of the example
  set; `tests/test_workspace_template.py` checks that it parses. The engine's own `pyproject.toml` ruff and mypy settings
  are compared with the template's `standards/languages/configs/` by the `configs` gardening check
  (`_gardening.config_drift`).
- `profile.toml` is the user's Union project and domain, engines and limits; it is the workspace's only profile
  name. The template ships only `profile.example.toml`, and the bootstrap (or you) copies it to `profile.toml`. The
  engine does not look for a `profile.toml` in the workspace by itself: the first deploy passes it with
  `--profile <clone>/profile.toml`. Only when the App exists, the file carries `[direction] repo` (the
  workspace) and `profile = "profile.toml"`, so a merge to the workspace deploys itself from then on.
- `waivers.toml` is owner-only, like `standards/`. `gardening.toml` is too: the engine reads it from the workspace's default branch, and
  without it gardening has no targets (see
  [architecture.md](architecture.md#gardening-targets)).
- `projects/<name>/` is a project's directory, on branch `factory/<name>`, with one pull request per project. A
  repository whose root has a `projects/` directory is a *workspace*, and a project there works in
  `projects/<name>/`.
- Issues and a Projects v2 board are the backlog.

### Where a project works

The layout is chosen per launch repository, and the gates, the scope check and the session's instructions all
follow it:

| Layout | When | The project is |
|---|---|---|
| `workspace` | the repository's root has a `projects/` directory | `projects/<name>/` |
| `sandbox` | the `[repo] project` repository, shared by several projects, without `projects/` | `<name>/` |
| `root` | any other repository | the whole repository |

The scope check judges writes against the project's directory. The code and the example profile still call
`[repo] project` the sandbox; with the workspace template it is the workspace.

### Credentials

`[secrets] deploy_key` is the deploy key of the `[repo] project` repository. `[repo_keys]` adds one deploy key per
further `[repo]` entry. Every slot mounts all of them, but a session installs only the key of the repository it was
launched on and the rest are dropped from its environment, so a session holds one key and can push only to that
repository.

A session's checkout also carries a `pre-push` hook (`_push_guard.install`) that refuses a push to any URL other than
the launch repository's. It checks the URL only, not the branch. The key is the limit; the hook catches mistakes
before they reach it.

A repository delivered to by promotion needs no key: the session works on the imported copy in the workspace, and
the operator pushes ([Promotion](#promotion)). That includes the engine.

The factory does not merge its own work. The one exception is opt-in: with `[loop] merge = "on"` the loop task merges
its own pull request on its `[loop] repo` repository once review has passed and its checks hold
([profiles/README.md](../profiles/README.md#loop)). It acts through the GitHub App, so it needs the App installed on
that repository and does not apply to a public engine repository the owner cannot install an App on; there the
owner merges the promotion pull request.

## Promotion

Another repository is a project like any other. Nothing in the factory pushes to it: the chat operator does, on your
word, from a shell with your git and `gh` credentials, by running `ops/promote.py` (the factory MCP has no promote
task). A shell user runs the same commands. They run from an engine checkout (`PYTHONPATH=src`); `projects/<name>`
stands for the project's path in your workspace checkout, such as `../agent-workspace/projects/<name>`.

1. **Import.** `python ops/promote.py import <target-url> projects/<name> [--ref BRANCH] [--path DIR] [--new]` writes
   the target's files into the project, records the repository and the commit they came from in
   `projects/<name>/promote.toml`, and makes one commit in the workspace. `--new` is for a repository that does not
   exist yet. The operator owns `promote.toml`; the gate rejects a project change to it.
2. **Work.** A project whose brief has a `Delivers to:` line edits that tree and commits it like any other project.
   The pre-review gate runs the target's own CI commands and the changed-line coverage check on it
   (`factory:target-ci`, `factory:target-coverage`). A changed Python line the tests do not cover passes when it is
   listed in the project's `SUMMARY.md`.
3. **Review.** As for every project. Only committed, reviewed content is delivered.
4. **Deliver.** `python ops/promote.py deliver projects/<name> <branch> [--to URL] [--fork URL] [--title TEXT]
   [--base SHA] [--dco | --no-dco] [--fixes OWNER/REPO#N] [--dry-run] [--replace]` replays the project's changes onto the
   target's current default branch as one commit by the operator's git identity (`user.name` and `user.email`
   must be set), scans what the commit adds (`ops/scan_public.py`), reruns the target's CI and coverage, and
   pushes the branch and opens or updates a pull request with `SUMMARY.md` as its body. Changes made upstream
   since the import are kept. A conflict, an empty result, a finding or a red check stops it before anything
   is pushed. A target with no coverage command of its own is measured with pytest-cov (a uv project in its
   own environment), so for one that is not a uv project the `python` on your PATH needs `pytest-cov`.
   The rerun skips the workflow's machine-setup steps (`sudo`, `apt`, `npm install -g` and the like), so what they
   install must already be on your machine: for the engine that is Playwright MCP with Chromium. Without
   `gh` the report prints the pull request body to open it by hand. `--fork` pushes to your fork instead,
   `--fixes` adds a `Fixes OWNER/REPO#N` line to the body for each public issue the change resolves (the
   workspace item records that link), and `--dry-run` does everything but push. A branch that already exists
   on the target (or the fork) is replaced only when its tip is your earlier promotion; otherwise `deliver`
   refuses, and `--replace` overwrites it. A `--new` project is delivered once: after that, re-import it.
5. **Merge.** The owner merges, or tells the operator to. `deliver` never merges. After the merge the operator
   confirms each fixed public issue closed and marks the workspace item Done with a link to the merged pull request.

If the target asks for a DCO (a `.github/dco.yml`, a `DCO` file, or CONTRIBUTING or a workflow mentioning it),
`deliver` adds `Signed-off-by` with the operator's identity at promotion; `--dco` and `--no-dco` override the
detection, and `--no-dco` on such a target prints a warning that its DCO check will fail. The sign-off is the human's personal certification, made at promotion with the operator's (the owner's)
git identity; the commits a session made in the sandbox stay attributed to the factory, and factory sessions never
sign off. A CLA or a commit format in the target's rules is printed as a notice.

### Deploying the factory

Deploying is a setting, the same for every owner: with `[deploy] auto`, the deployer ships the default branch of
`[deploy] repo` (default `[repo] factory`) once its CI has passed
([profiles/README.md](../profiles/README.md#schema)). Developing the engine is a workspace project like any
other, imported with `ops/promote.py import` and promoted as above; it needs no App, deploy key or webhook on the
repository that is deployed. A modified engine copy in the workspace is deployed only for testing and benchmarking;
production, the owner's own factory included, runs what was merged. Without the App on the deployed repository the
deployer reads it anonymously and takes its deploy lock in `[direction] repo`, so `[direction]` is then required
and the App must be installed on the direction repository.

The owner's own factory deploys from `unionai-oss/flyte-agent-factory` main, as every other owner's does from the
public repository or a fork. The alternative, deploying from a private engine repository, would let a change reach
production before it is public, with the factory's App on the deployed repository and no public CI. The cost of the
public repository is latency: a change reaches production only after its promotion pull request passes the
public CI and is merged, and the deployer reads that repository without the App, so it polls (`[deploy] webhook`
off) and keeps its lock in `[direction] repo`. In exchange the owner runs exactly the documented setup, and every
deployed commit is public, reviewed and green.

## The public template

The template is public, so everything in it, history included, passes `ops/scan_public.py`: gitleaks over the
contents and the history, and the personal-literal guard of `tests/test_profile.py`. A real profile never enters it.

## No standards in the engine

The engine has no standards of its own and no fallback copy of them. The gate and the session read the workspace's
`standards/` first, then the deployment profile's `[standards] dir`; with neither there are no rules, and both the
deploy and the gate report say so. The template's `standards/` is the worked example ([standards.md](standards.md)).

## Migrating a sandbox

`ops/migrate_workspace.py --sandbox <path> --direction <path> --out <path>` builds a workspace from the template, the
sandbox's projects, the direction repository's profile, briefs and history, and writes `MIGRATION.md` with the manual
steps. The rename to `agent-workspace` is last, together with the profile's allowed-repo string.

## What lives in the workspace, not the engine

Owner content is kept out of the engine repository. In the workspace:

- `history/operations-log.md`: the runs launched and nursed, with their run ids.
- `briefs/`: the owner's project briefs. Every brief that is not `briefs/demo-*.md`,
  `briefs/smoke-wordstats.md` or `briefs/factory-gardening.md` lives in the owner's workspace.

The engine keeps only those product briefs. `ops/migrate_workspace.py` carries every other brief from the
direction checkout into a new workspace.
