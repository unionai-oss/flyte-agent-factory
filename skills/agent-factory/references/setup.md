# Guide a first install or resume setup

Use this mode when `/agent-factory` finds no reachable factory, and for “help me set up the factory”, including
before any deployment exists. The factory MCP only exists after a deployment, so before it you work with GitHub
tools, your own knowledge of the steps and one local command. The public procedure is at
https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/quickstart.md. Read that page when guiding setup;
if you cannot retrieve it, give the link to the person and explain the limitation. Do not invent tools, CLI flags or
a hosted bootstrap service: the machine work is done by one command, `ops/bootstrap.py`, described below.

## Is there a factory already?

Mode detection comes first. A factory is reachable only if the factory MCP's tools (`run_task` and the registered
`factory.launch`) are present and a call such as `list_projects` answers, and its server instructions name the
factory. If so, stop here and take over as the operator (the skill's other sections). If the tools are there but
fail, or answer for a different deployment, treat it as a half-finished install. Otherwise do guided setup.

## Establish where to resume

Ask: **is Flyte or Union already up for you?** If yes, ask for its URL; the backend endpoint is that URL's host, so do not ask for it again. Then collect
the non-secret values, one question at a time:

- the project (a dedicated one) and domain (default `development`);
- the GitHub owner and workspace name (default `agent-workspace`), personal or organization;
- the state bucket URI (`s3://`, `gs://`, `abfs://`), see Prerequisites below;
- the engine, `claude` (default) or `codex`. With `codex` the `engine-token` step ends `todo` and prints ONE filled-in
  command that signs in to ChatGPT on their machine and stores the login as `codex_login_agent_factory`; relay it as it
  is printed.

These are identifiers, not secrets. Track a short checklist in the conversation: complete, next, blocked, not
checked. A previous message saying “deployed” is not evidence of a reachable MCP or usable credentials.

claude.ai has no local shell. When a local action is needed, prepare one copy-paste command at that point, with
confirmed non-secret values filled in. Use a GitHub file-edit tool for repository edits where available and read back
the diff. Report the observed result separately from the handoff; providing a command does not mean it ran. In
Claude Code, run it yourself and read only its step lines.

Offer one next step at a time, explain what success looks like, and use its result before continuing. Do not make the
person answer every optional choice up front.

## Prerequisites

Check, and say plainly which are missing, before handing over anything:

- **GitHub:** an account, and `gh` signed in on their machine (the board also needs `gh auth refresh -s project`).
  Check the workspace with GitHub tools if connected: whether `<owner>/<workspace>` exists, its visibility and its
  default branch.
- **Flyte or Union:** an endpoint. Union-only for the whole path today: an account to sign in with, API keys, the
  remote image builder and the hosted MCP's authentication. Someone without it must get backend access first; there
  is no public provisioning service. If they chose OSS Flyte, read the compatibility audit at
  https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/oss-flyte-compat.md and explain the gaps
  before offering any Union-only step. The bootstrap runs the other steps against OSS with `--backend oss` and
  stops before the deploy, naming what is Union-only.
- **State bucket** (`--state-uri`, the profile's `[state] uri`): a private, encrypted object-storage prefix that the factory's runtime identity and the person's
  machine can list, read, write and delete under. A Union key does not grant native storage permissions; their
  administrator provisions it. Stop here if the provider or access is unknown.
- **Claude Pro or Max,** which supplies the workers' token (created at the bootstrap's hidden prompt, below). An
  Anthropic API key is not needed.
- **Local tools:** `git`, `ssh-keygen`, `gh`, `uv`. The bootstrap checks them and prints the one install command for
  anything missing.

## Do what you can yourself

With GitHub tools, create the private `<owner>/<workspace>` repository with `create_repository` if the connector
allows it, and read back its URL and default branch. Reuse an existing workspace; never recreate or overwrite it. If
you cannot create it, the bootstrap does (`gh repo create --private`), so say so and move on. Do not claim
repository-scoped chat tools can manage Projects boards. Leave the template, CODEOWNERS and push to the bootstrap.

## Hand over one copy-paste command

Hand over ONE copy-paste command with the confirmed values filled in (use `--no-app` or `--no-board` only if they ask; with
`--no-app` its `github-app` line says, and you repeat before they run it: "what is lost without the GitHub App: CI visibility (check-runs and statuses are read through the App); draft pull requests and the factory/verifier status; auto-deploy and the [direction] repository; issue intake. The backlog board is kept: it is made with gh and needs no App"):

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine
```

It is idempotent: each step checks live state first and is skipped when done, so a rerun after any interruption
resumes. Optional flags: `--domain`, `--backend union|oss`, `--engine claude|codex`, `--no-app`, `--no-board`,
`--dir <path to the workspace clone>`, `--redeploy`, `--dry-run`. With a Union deployment key in `_UNION_EAGER_API_KEY` (see the deploy prompt below) the deploy needs no further input. Its output is one line per step,
`[done|skip|todo|blocked] <step>: <detail>`, and it never prints a secret.

Explain each prompt before they meet it:

- **Browser sign-in** to Flyte (the SDK's PKCE login). Nothing to paste.
- **Hidden token prompt**, only if the engine token secret is missing: they run `claude setup-token` in another
  terminal and paste the result at the prompt. Nothing is echoed or logged; it goes straight into a Flyte secret.
- **App creation page** (default): a local page in their browser posts the App manifest to GitHub; they confirm the name
  and click Create GitHub App. The key is stored in a Flyte secret, never printed.
- **Install page** for the workspace: they install the App on it; the command waits until it sees the installation.
  An organization where they are not an admin may make that impossible; setup then works without the App (see below).
- The deploy needs a Union deployment API key (`--deploy-key`, not the GitHub SSH key of the `deploy-key` step). The command uses `_UNION_EAGER_API_KEY` from their environment, otherwise
  creates one for the run and deletes it at the end, otherwise stops: the deploy makes its one-off Union API key with flyteplugins-union, which installs on Linux x86_64 only, so on macOS or Linux on ARM tell them
  to run the command on a Linux x86_64 machine, or to export `_UNION_EAGER_API_KEY` with a key made there
  (https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/quickstart.md, Local Union deployment bootstrap).
  The stopped `deploy` line prints that Union key's command filled in; it is this one, with the same arguments plus
  `--deploy-key`. If that machine has no Flyte config for `--endpoint`, the command writes one and opens the browser sign-in first; a config for another endpoint stops it before any change. It makes the `factory-deploy` key and its `factory-deploy` policy and assignment (`contributor` on
  the project and domain), shows the key once on that terminal only (never stdout, never chat), and says to store it
  in a password manager and load it with the quickstart's hidden-prompt block. If Union refuses them the policy or
  the assignment (another admin grants), it still shows the key, ends `todo` and prints one filled-in command for
  that admin: the same command with `--grant factory-deploy` and the key's non-secret client_id in place of
  `--deploy-key`. It gives the policy to that key and makes no key; tell them to send it as printed.

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --deploy-key
```

Never ask for a credential, token or key in chat, and never use the MCP `create_secret` for engine credentials.
If they paste something that looks like one, tell them to rotate it.

## Resume from `--status`

`ops/bootstrap.py --status` with the same identifying arguments is read-only and prints the step table. Ask them to
paste it back (it has no secrets). Read it and continue at the first `todo` or `blocked` line; do not redo `done` or
`skip` steps. The steps, in the command's order, are: `tools`, `workspace`, `template`, `profile`, `signin`, `project`,
`deploy-key`, `github-app`, `ruleset`, `board`, `engine-token`, `deploy`, `mcp-url`, `connect`. Distinguish
absent from inaccessible; an auth or network failure is not evidence that something is missing. If you cannot inspect
something, say “not checked” and ask for the next read-only check.

## What the bootstrap sets up, in the command's order

The command runs the steps in the order of the list above. The numbered list below is the owner's order (workspace,
profile, deploy key, App, ruleset, board, deploy), with three exceptions that the dependencies force. Say so if asked,
and do not reorder them: the Flyte sign-in and project (`signin`, `project`) come before the deploy key, because the
deploy key and the App's key are both stored as Flyte secrets; the ruleset (`ruleset`) follows every push to `main`
except the last, the factory-URL commit, which exists only after the deploy and is pushed by the owner, whom the
ruleset lets through; the engine token (`engine-token`) comes after the board.

1. **Private workspace.** A private `<owner>/agent-workspace` filled from the engine's bundled template with
   `ops/new_workspace.py` into the clone (keeping `.git`), CODEOWNERS set to the owner, committed and pushed.
2. **Profile at the workspace root.** `profile.toml`, with `[repo] project` and, only once the App's installation is
   confirmed, `[github] app_id`, `[deploy] auto = true` and `[direction]` (`repo` = the workspace, `profile = "profile.toml"`); `[repo] factory` stays the
   public engine. It is never overwritten; a resume fills in missing keys only and keeps what the person chose
   (`auto = false` stays). It is the only profile name; never suggest another path.
3. **Write deploy key.** `ssh-keygen` in a `mktemp` directory, `gh repo deploy-key add --allow-write`, then the
   private half is stored as a Flyte secret through the Flyte SDK, and the directory wiped. Never in chat.
4. **Your own GitHub App.** The default, through the manifest flow. Permissions: Contents read, Metadata read, Pull
   requests write, Commit statuses write, Checks read, Issues write. It enables CI visibility, draft PRs and the
   verifier, auto-deploy with `[direction]`, and issue intake. Without it, projects push the default branch, there are
   no PRs or verifier status, and every deploy is by hand. There is no shared public App. If the installation cannot
   happen (an organization they do not own), the profile has no trace of the App: tell them to run the command again
   with `--no-app`; the ruleset is then updated to let the deploy key push. Rerunning without `--no-app` later adds
   the App and takes that bypass off; rerunning with `--no-app` over an App profile removes the App's keys, and says
   so in the `profile` line. Details:
   https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/github-app.md
5. **Protected main.** A `main` ruleset named `agent-workspace-main` through `gh api`; the deploy key may bypass it
   exactly when there is no App, and a rerun in the other mode updates the existing ruleset. On a refusal it prints
   the Settings > Rules steps for the person to follow. Verify it yourself where you can: if the GitHub connector can
   read the workspace's rulesets (or the rules that apply to `main`), check that `agent-workspace-main` exists, is
   active and targets the default branch. If it cannot, rely on the `ruleset` line of the bootstrap's `--status`
   output (`done` means it read the ruleset back from GitHub just now); say which of the two you used.
6. **Board.** A user-owned board that the bootstrap already links to the workspace. It is the standard backlog:
   unrelated projects, the person's own items and copies of issues pulled from tracked repositories. Verify the link
   with GitHub tools (`projects_list`, `projects_get`). GitHub has no command for the Default repository, so that one
   stays a manual click, an honest boundary: on the board, Settings, **Default repository**, choose the workspace.
   The bootstrap's own message says "three clicks remain": that one, the repository's Projects tab, and the board's built-in workflows; give the **Link a project**
   click (workspace, Projects tab) only if the link check shows it missing. The classic-token `[board]` automation is optional; do not add it unasked.
7. **First deploy.** From the public `unionai-oss/flyte-agent-factory` `main`, with `--profile <workspace
   clone>/profile.toml`, so there is no separate move step for a new user; the deployer then follows `[direction]`.
   Moving an existing install onto `[direction]` later needs one deploy with that `--profile`, because the
   deployer refuses to change its own direction, on purpose: when it applies, give the `--redeploy` command below
   with their values filled in, run once.

   **Deploying later engine changes.** The deployer deploys the default-branch head of `[deploy] repo` (default
   `[repo] factory`, the public engine) once that head's CI passed. The person's App is installed on their workspace,
   not on the public engine, so it reads the engine anonymously, polls (`[deploy] webhook = false`) and keeps its
   deploy lock in `[direction] repo`; with neither the App nor `[direction]` the profile is refused, so do not remove
   `[deploy] auto` or `[direction]`, and do not ask them to install the App on unionai-oss (they cannot). To deploy an
   engine change at once, give this one command, with the same values as the first run; `--redeploy` runs the
   work-slot and factory deploys again from the public engine's `main`, with a one-off deployment key that it deletes
   at the end:

   ```bash
   d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --redeploy
   ```

## Connect the factory MCP, then verify

After the `mcp-url` step the command prints the connector URL and one copy-paste command, with the same arguments plus
`--chat-key`, on a Linux x86_64 machine. If that machine has no Flyte config for `--endpoint`, the command writes one and opens the browser sign-in first; a config for another endpoint stops it before any change. It makes the `factory-chat` key (no default policies), its `factory-chat`
policy (`contributor` on the project and domain) and the assignment. Then, if `claude` is on their PATH and
`claude mcp list` shows no connector named `agent-factory` or `factory`, it adds the connector to Claude Code with
the key as the header, and nothing secret is printed. Otherwise (claude.ai) it shows the key once on their terminal
only; they paste it into the connector's request header, `Bearer ` followed by the key (a beta; if the header section
is missing, offer Claude Code and say it is a gap), and never into the conversation. A rerun with the connector
present skips; if a `factory-chat` key already exists it stops and says how to delete it by hand. If Union refuses
them the policy or the assignment (another admin grants), it still hands over the key, ends `todo` and prints one
filled-in command for that admin, with `--grant factory-chat` and the key's non-secret client_id in place of
`--chat-key`; tell them to send it as printed.

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --chat-key
```

The connector URL is `<public_url>/flyte-mcp/mcp`. The block for adding it to Claude Code by hand is in
https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/chat-operator.md.

Verify rather than assume: call `list_projects` (it returns only the factory's project) and confirm `factory.launch`
is registered, then read the server instructions for the project, domain and allowed repositories. Add GitHub's own MCP
separately if it is not connected.

## Take over, launch, and finish

Once it answers, take over as the operator and help write and launch the first project: gather a name, an outcome and
checkable acceptance criteria; check the queue and status first to avoid duplicating live work; call `factory.launch`
with an explicit model; report started with the URL, queued with the position, or the failure with its repair. Without
the App the output is pushed to the default branch and no PR opens. Never merge without instruction.

End by naming the one blocking thing, if any, and what you observed. Skip working parts on resume; do not rotate keys,
delete resources or restart anything only to make setup look linear. Unperformed external steps are NOT RUN; do not call
a plan or a printed command a successful install.

## If Flyte or Union is not up

Without a Union organization, they need backend access first: sign up with Union (the whole path needs it today) or
get an administrator to provide an endpoint and a project. For OSS Flyte, set expectations from the compatibility
audit: everything up to the deploy works, and the deploy, API keys, remote image builder and MCP authentication are
Union-only gaps that need adaptation. Offer to run the rest now (`--backend oss`) so that only the deploy is left.
