# agent-workspace

A workspace is the repository the agent factory's projects are pushed to. The factory itself (the engine) lives in a
separate repository and runs a pinned release. A project works only in its own directory here; it reaches the engine
repository only when you give the profile a deploy key for it (`[repo_keys]`).

## Layout

| Path | What | Who writes it |
|---|---|---|
| `standards/` | the rules every project is checked against; the engine ships none of its own, so this set is the worked example for the format in the engine's `docs/standards.md` | you, by hand. The factory cannot: its gate rejects a patch that touches it |
| `profile.toml` | your Union project and domain, engines, limits (start from `profile.example.toml`). The engine loads it by path or through `[direction]`; it does not look for it here by itself | you |
| `waivers.toml` | checks you accept failing for a while | you |
| `gardening.toml` | what gardening may tend (the engine by default, also when this file is absent; remove the target to turn gardening off). The engine reads it from this repository's default branch | you |
| `projects/<name>/` | one directory per project, on branch `factory/<name>` with `[github]`, one pull request each. This directory makes the repository a workspace | the factory, inside the project's own directory |
| issues and a Projects v2 board | the backlog | you and the factory |

## Start

1. Copy this template to `<you>/agent-workspace` (GitHub "Use this template", or `ops/new_workspace.py` in the engine).
2. Copy `profile.example.toml` to `profile.toml` and fill it in. Replace `OWNER` in `.github/CODEOWNERS`.
3. Turn on branch protection as in `docs/branch-protection.md`.
4. Add a deploy key for this repository only and deploy the engine release you want to run with this profile.
5. Launch a project from chat. It works in `projects/<name>/`. With `[github]` in the profile it opens a pull request
   on `factory/<name>`; without it, it pushes the default branch.

## Delivering to another repository

A session pushes only to the repository it was launched on and holds no credential for a target repository. A project
whose brief has a `Delivers to:` line leaves `projects/<name>/patches/*.patch`, made with `git format-patch`. When it
is reviewed, run `promote` from the engine (`ops/promote.py`) on a machine with a shell and your own credentials: it
applies the patches to the target repository on a new branch, runs that repository's tests and the changed-line
coverage check, and opens a pull request there.
