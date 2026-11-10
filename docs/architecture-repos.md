# Repositories: the engine and the workspace

Two kinds of repository. This replaces the three-repo split (engine, sandbox, direction) of the earlier design note.

| Repository | Holds | Who writes it |
|---|---|---|
| Engine, [unionai-oss/flyte-agent-factory](https://github.com/unionai-oss/flyte-agent-factory) | the factory's code. Deployments run a pinned release | maintainers, through ordinary pull requests. Users never write to it |
| Workspace template, `unionai-oss/flyte-agent-workspace` | the starting layout. Public | maintainers; also shipped in the engine at `src/flyte_agent_factory/_assets/workspace_template/` |
| Workspace, `<user>/agent-workspace` | one user's standards, profile, projects and backlog | the user, and the factory in a project's own directory under `projects/` |

## The workspace

- `standards/` is seeded from the template's example set and edited by hand ([standards.md](standards.md) is the format).
  It is read-only to the factory: the gate rejects a patch that touches it, a session's May-edit scope excludes it, and
  the template ships CODEOWNERS for it and `docs/branch-protection.md`. The template's copy is the source of the example
  set; `tests/test_workspace_template.py` checks that it parses. The engine's own `pyproject.toml` ruff and mypy settings
  are compared with the template's `standards/languages/configs/` by the `configs` gardening check
  (`_gardening.config_drift`).
- `profile.toml` is the user's Union project and domain, engines and limits. The template ships only
  `profile.example.toml`. The engine does not look for a `profile.toml` in the workspace: it loads
  `profiles/<name>.toml` from its own checkout (or `AGENT_FACTORY_PROFILES_DIR`), or the path you pass. To have a
  merge to the workspace deploy itself, point `[direction] repo` and `profile` at it (`profile = "profile.toml"`).
- `waivers.toml` is owner-only, like `standards/`. `gardening.toml` is too: the engine reads it from the workspace's default branch, and
  without it gardens the engine (see [architecture.md](architecture.md#gardening-targets)).
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
| `root` | any other repository, such as the engine's | the whole repository |

The scope check judges writes against the project's directory. The code and the example profile still call
`[repo] project` the sandbox; with the workspace template it is the workspace.

### Credentials

`[secrets] deploy_key` is the deploy key of the `[repo] project` repository. `[repo_keys]` adds one deploy key per
further `[repo]` entry, for example the engine repository for a factory-improvement launch. Every slot mounts all of
them, but a session installs only the key of the repository it was launched on and the rest are dropped from its
environment, so a session holds one key and can push only to that repository. A launch into the engine repository
therefore pushes there, on `factory/<name>`.

A session's checkout also carries a `pre-push` hook (`_push_guard.install`) that refuses a push to any URL other than
the launch repository's. It checks the URL only, not the branch. The key is the limit; the hook catches mistakes
before they reach it.

The factory does not merge its own work. The exception is the loop: with `[loop] merge = "on"` the loop task merges
a pull request on the `[repo] factory` repository once review has passed and its checks hold
([profiles/README.md](../profiles/README.md#loop)).

## Promotion

Delivering to another repository happens outside the factory. A project whose brief has a `Delivers to:` line leaves
its result as patches: `projects/<name>/patches/*.patch`, made with `git format-patch` against the target's default
branch. The session that does the work produces them, and the pre-review gate's `factory:target-patches` check holds
a DONE back until they are there. A project that does not deliver elsewhere leaves no patches.

`ops/promote.py` takes a reviewed project's patches and:

1. clones the target and creates the branch from its current default branch,
2. applies the patches with `git am --3way`; a conflict stops and reports,
3. runs the target's own CI commands and the changed-line coverage check (`_target_ci`); a red check stops and reports. The CI commands run as in the gate (a runner-like
   environment; a failure the unpatched base has too is not the project's, and a command that cannot run here is `NOT RUN`),
4. pushes the branch and opens or updates a pull request with `gh`, or prints the compare URL.

The coverage check covers changed Python lines only. A changed line the tests do not cover passes when it is listed in
the project's `projects/<name>/SUMMARY.md`; that file is also the body of the pull request.

`--dry-run` does everything but push. Promotion runs with the caller's own git credentials (ssh agent, `gh` login),
so "run from chat" means a chat client with a local shell that can run `ops/promote.py`. The factory MCP has no
promote task, and a factory session holds a credential for the target only if the owner lists that repo's deploy key under `[repo_keys]` (every slot mounts those).

The factory's own improvement is a root-layout project on the engine repository: it is launched into the `[repo]
factory` repository, which needs a `[repo_keys]` entry and a place in `[mcp] repos`, and works on `factory/<name>`
there.

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
