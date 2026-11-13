# agent-workspace

A workspace is the repository the agent factory's projects are pushed to. The factory itself (the engine) lives in a
separate repository, the public `unionai-oss/flyte-agent-factory`; deployments run the head of its default branch once
its CI passes. A project works only in its own directory here. Work on another repository, the engine included, is
imported into a project here and promoted back as a pull request
([Delivering to another repository](#delivering-to-another-repository)).

## Layout

| Path | What | Who writes it |
|---|---|---|
| `standards/` | the rules every project is checked against; the engine ships none of its own, so this set is the worked example for the format in the engine's `docs/standards.md` | you, by hand. The factory cannot: its gate rejects a patch that touches it |
| `profile.toml` | your Union project and domain, engines, limits (start from `profile.example.toml`). The engine loads it by path or through `[direction]`; it does not look for it here by itself | you |
| `waivers.toml` | checks you accept failing for a while | you |
| `gardening.toml` | what gardening may tend (opt-in: no targets, or no file, means gardening does nothing). The engine reads it from this repository's default branch | you |
| `projects/<name>/` | one directory per project, on branch `factory/<name>` with `[github]`, one pull request each. This directory makes the repository a workspace | the factory, inside the project's own directory |
| issues and a Projects v2 board | the standard backlog: unrelated projects, your own items, and copies of issues pulled from tracked repositories | you and the factory |

## Start

1. Install the engine's two skills, `agent-factory` and `since`, in your chat and run `/agent-factory` (later,
   `/since` tells you what changed since you last looked). It hands you one command,
   `ops/bootstrap.py` in the engine, which creates `<you>/agent-workspace` from this template
   (`ops/new_workspace.py`), sets `CODEOWNERS`, writes `profile.toml`, adds the write deploy key as a Flyte secret,
   registers your own GitHub App (skip with `--no-app`), protects `main`, creates a Projects board and runs the first
   deploy.
2. The board is linked for you, but GitHub has no command for the rest, so three clicks remain: on the board,
   Settings, **Default repository**, pick this workspace; on this repository's Projects tab, **Link a project** and
   pick the board, only if the tab does not list it; on the board, Settings, **Workflows**, turn on the built-in
   workflows that add new issues to the board.
3. By hand instead: copy `profile.example.toml` to `profile.toml` and fill it in. Replace `OWNER` in
   `.github/CODEOWNERS` (the bootstrap sets it). Without the App, projects push the default branch and open no pull
   request; see the engine's `docs/github-app.md`.
4. Turn on branch protection as in `docs/branch-protection.md`.
5. Add a deploy key for this repository only and deploy the public engine's `main` with this profile
   (`--profile <this clone>/profile.toml`).
6. Launch a project from chat. It works in `projects/<name>/`. With `[github]` in the profile it opens a pull request
   on `factory/<name>`; without it, it pushes the default branch.

## Delivering to another repository

A session holds no credential for a target repository. The operator imports the target into the project, and promotes
the reviewed result as a pull request from a shell with your own credentials:
[Promotion](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/architecture-repos.md#promotion).
