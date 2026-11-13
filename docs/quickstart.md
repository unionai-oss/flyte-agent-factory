# Guided setup

## Start here

1. **Install the skills.** Download `agent-factory.zip` and `since.zip` from the engine repository on GitHub
   ([agent-factory.zip](https://github.com/unionai-oss/flyte-agent-factory/blob/main/skills/dist/agent-factory.zip),
   [since.zip](https://github.com/unionai-oss/flyte-agent-factory/blob/main/skills/dist/since.zip): use the
   download button on each page). Every user installs the same files; nothing in them is specific to you.
   - In claude.ai: Settings, Capabilities, Skills, upload each zip.
   - In Claude Code, unzip both into your skills directory:

     ```bash
     mkdir -p ~/.claude/skills
     unzip -o ~/Downloads/agent-factory.zip -d ~/.claude/skills/
     unzip -o ~/Downloads/since.zip -d ~/.claude/skills/
     ```
2. **Run `/agent-factory`.** It looks for a reachable factory. Finding none, it starts guided setup below; finding
   one, it takes over as your operator ([chat-operator.md](chat-operator.md)). A half-finished setup resumes where
   it stopped: each step checks live state, not a note of what was done.

In claude.ai, also connect GitHub's MCP server first ([github-connector.md](github-connector.md)) so the chat can
read and edit your workspace repository. If your chat cannot read the skill's instructions, open this page and
follow the same steps yourself. The skill's procedure is its
[setup reference](../skills/agent-factory/references/setup.md).

## What /agent-factory walks you through

It asks whether Flyte or Union is already running for you and for its URL, then collects the non-secret values:
endpoint, project, domain, the GitHub owner for your workspace, the state bucket and the engine. It never asks for a
credential. The deploy key into your workspace is the only GitHub credential setup requires; the App is optional
([github-app.md](github-app.md), which also says why the model is assumed and where it is decided, #179). claude.ai has no local shell, so it ends the conversation part with **one copy-paste command**
with those values filled in, which you run on your machine (Claude Code runs it for you). That command,
`ops/bootstrap.py` in an engine checkout, does the machine work and prompts locally for anything secret.

The setup it performs, in the order a person thinks of it:

1. **Private workspace.** A private `you/agent-workspace` repository, filled from the engine's bundled workspace
   template into a local clone (keeping `.git`; it fills it with the same template code `ops/new_workspace.py` uses), `.github/CODEOWNERS` set to you,
   committed and pushed. The chat creates the repository itself where its GitHub connector allows it (the
   `create_repository` tool); the bootstrap otherwise runs `gh repo create --private`.
2. **Profile at the workspace root.** `profile.toml` in the workspace, with `[repo] project`, your backend and the
   secret names. `[repo] factory` stays the public engine: the factory follows `unionai-oss/flyte-agent-factory`
   `main`, not a fork of it. `[direction]` and `[deploy] auto = true`, only when the App exists: `[direction]` has
   `repo` = the workspace and `profile = "profile.toml"`, so the deployer reads the profile from the workspace;
   without the App the profile has neither, and every deploy is by hand. The file is written once and never overwritten; on a resume only missing keys are filled in, and a
   value you set yourself (`auto = false`, say) is kept.
3. **Write deploy key.** A key pair made with `ssh-keygen` in a `mktemp` directory, its public half added with
   `gh repo deploy-key add --allow-write`, its private half stored as a Flyte secret through the Flyte SDK (the same as `flyte create secret --from-file`),
   and the directory wiped. The key is never shown in chat.
4. **Your own GitHub App.** Registered through GitHub's manifest flow in your browser, its key stored straight into
   a Flyte secret, then installed on the workspace. It is the default; `--no-app` skips it, and the `github-app` line then reads: "what is lost without the GitHub App: CI visibility (check-runs and statuses are read through the App); draft pull requests and the factory/verifier status; auto-deploy and the [direction] repository; issue intake. The backlog board is kept: it is made with gh and needs no App". What
   it enables: [github-app.md](github-app.md). If you are not an administrator of the owning organization,
   installing may be impossible: run the command again with `--no-app`. Nothing needs undoing, because the profile
   names the App only after the installation is confirmed (`[github] app_id`, `[deploy] auto` and `[direction]`), and
   the ruleset then lets the deploy key push. The other way round also works: rerun without `--no-app` to add the
   App later, and the ruleset stops letting the deploy key bypass it. A rerun with `--no-app` over a profile that
   already names an App removes those keys (the status line says so), because you chose to go without it.
5. **Protected main.** A ruleset named `agent-workspace-main` on the workspace, created with `gh api`. If GitHub
   refuses (a plan without rulesets on private repositories, or no admin rights), it prints the Settings > Rules
   steps instead ([branch-protection.md](branch-protection.md)). The chat verifies the ruleset through its GitHub
   connector where that can read rulesets, and otherwise from the `ruleset` line of `--status`, which reads it back
   from GitHub.
6. **Board.** A Projects board you own, created and linked to the workspace with `gh project create` and
   `gh project link` (this needs `gh auth refresh -s project`). It is the standard backlog: unrelated projects,
   your own items, and copies of issues pulled from tracked repositories. The bootstrap already links the board, so
   the chat verifies that with GitHub tools. GitHub offers no command for the board's **Default repository**, so
   the bootstrap prints these three clicks (the second only if needed):
   - on the board, Settings, **Default repository**, and pick the workspace;
   - on the workspace, Projects tab, **Link a project**, and pick the board, only if the Projects tab does not list it;
   - on the board, Settings, **Workflows**, turn on the built-in workflows that add new issues to the board.

   `[board]` automation (a classic token mounted in project supervisors) is optional and not added by default.
7. **First deploy.** From the public `unionai-oss/flyte-agent-factory` `main`, with
   `--profile <your workspace clone>/profile.toml`, so a new user has no separate move step. From then on the
   deployer follows `[direction]`. Moving an **existing** install onto `[direction]` later needs one deploy with
   that same `--profile` argument, because the deployer refuses to change its own direction, on purpose: that is
   the bootstrap's `--redeploy` command below, run once with your values (see [setup.md](setup.md#deploying)).

   **Deploying later engine changes.** The deployer deploys the default-branch head of `[deploy] repo` (default
   `[repo] factory`, the public engine) once that head's CI passed. Your App is installed on your workspace, not on
   `unionai-oss/flyte-agent-factory`, so it reads that repository anonymously, polls (`[deploy] webhook = false`) and
   keeps its deploy lock in `[direction] repo`. With neither the App nor `[direction]` the profile is refused. To
   deploy the engine's current `main` at once, run the bootstrap again with `--redeploy`, with the same values as the
   first time. It deploys the work slots and the factory again, even though the last deploy matches your profile:

   ```bash
   d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --redeploy
   ```

Around those seven, the bootstrap also checks tools and signs you in to Flyte, makes sure the project exists,
stores your engine token, and, after the deploy, reads the factory MCP's URL into the profile and redeploys. The
full table has one line per step, and a line stays `todo` until its live check passes. The command runs the steps in this table's
order, which is the numbered order above with three exceptions the dependencies force: the Flyte sign-in and project
come before the deploy key, because the key and the App's key are stored as Flyte secrets; the ruleset follows every
push to `main` except the last (the factory URL, which exists only after the deploy), and that push is yours, which
the ruleset lets through; and the engine token comes after the board.

| Step | Plain words | You do |
|---|---|---|
| `tools` | `git`, `ssh-keygen`, `gh` (signed in) and `uv` are present | install what it names |
| `workspace` | your private workspace repository exists | nothing |
| `template` | the workspace holds the template, `CODEOWNERS` set | nothing |
| `profile` | `profile.toml` exists and validates | nothing |
| `signin` | a Flyte config for your endpoint works | sign in at the browser page it opens |
| `project` | the Flyte project exists | nothing |
| `deploy-key` | the write deploy key exists on GitHub and as a Flyte secret | nothing |
| `github-app` | your App exists, is stored and is installed | confirm on the GitHub pages it opens |
| `ruleset` | `main` is protected | the Settings steps, if it printed them |
| `board` | a board is linked to the workspace (any board: if one is already linked, the bootstrap makes none) | the three clicks above (the second only if the Projects tab lacks the board) |
| `engine-token` | your Claude token is a Flyte secret | run `claude setup-token` in another terminal, paste at the hidden prompt |
| `deploy` | the factory tasks are registered and current (`--redeploy` runs it again) | on Union, nothing on Linux x86_64: it makes a one-off deployment key for the run and deletes it at the end. Elsewhere it stops and says so (see below) |
| `mcp-url` | `[mcp] public_url` is set | nothing |
| `connect` | cannot be checked from here | add the connector ([below](#connect-the-chat-and-launch)) |

Each line of its output reads `[done|skip|todo|blocked] <step>: <detail>`. Rerun the same command after any
interruption, or add `--status` for a read-only table; paste that table into the chat (it holds no secrets) and the
operator picks up at the first `todo`. The command:

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine
```

The chat fills in your values. Optional flags: `--domain` (default `development`), `--backend union|oss`,
`--engine claude|codex`, `--no-app`, `--no-board`, `--dir <path to the workspace clone>` (an empty directory is
cloned into), `--redeploy` and `--dry-run`.

What you will see while it runs, in order:

- a **browser sign-in** to Flyte (the SDK's PKCE login);
- a **hidden prompt** for your Claude token, only if the secret is missing. Nothing is echoed or logged;
- GitHub's **App creation page**, then the **install page** for your workspace;
- the board clicks (its line says "three clicks remain") and, if GitHub refused it, the ruleset steps, printed for you to do by hand.

Credentials are read by those local prompts and go straight into Flyte secrets. No secret value is printed,
logged or part of an error message.

### Connect the chat and launch

The last bootstrap step (`connect`) prints the factory MCP's connector URL, the claude.ai steps, and one
copy-paste command: the same bootstrap command with `--chat-key`, run on a Linux x86_64 machine. If that machine has no Flyte config for `--endpoint`, the command writes one and opens the browser sign-in first; a config for another endpoint stops it before any change. That command makes
the `factory-chat` key and its policy grant. If `claude` is on your PATH and `claude mcp list` shows no factory
connector (`factory` or `agent-factory`), it adds the `agent-factory` connector to Claude Code itself, with the key
as the header and nothing secret printed. Otherwise it shows the key once on that terminal: in claude.ai add a
custom connector at `<public_url>/flyte-mcp/mcp` and paste the key into its request header, never into a chat. By
hand instead, the hidden-prompt block in [chat-operator.md](chat-operator.md#1-connect-the-factory-mcp) reads a key
without echoing it. The chat then checks `list_projects` (it
returns only the factory's project) and that `factory.launch` is registered, takes over, and helps you write and
launch your first project (below). It ends by naming the one thing still blocking you, if any.

## Prerequisites

- A GitHub account, and `gh` signed in locally (`gh auth login`; the board also needs `gh auth refresh -s project`).
- A **Flyte backend**: a Union organization (you get an endpoint such as `flyte.example.com`). Union is required
  for the whole path today. This guide cannot create a tenant or a cluster.
- A **state bucket**: a private, encrypted object-storage prefix the factory's tasks and your machine can list,
  read, write and delete ([Required shared state storage](#required-shared-state-storage)). Its URI is
  `--state-uri`.
- **Claude Pro or Max**, whose `claude setup-token` supplies the workers; an Anthropic API key is not needed.
  Codex is optional and still needs a Claude token ([engines.md](engines.md)). With `--engine codex` the
  `engine-token` step prints one command (`codex login --device-auth`, then `flyte create secret
  codex_login_agent_factory --from-file ...`) that works before any factory is deployed; run it, then rerun the bootstrap.
- `git`, OpenSSH (`ssh-keygen`), [uv](https://docs.astral.sh/uv/getting-started/installation/), and Claude Code
  or claude.ai with request-header connectors (a beta; see [chat-operator.md](chat-operator.md)).

**OSS Flyte** is a partial path. The bootstrap runs every step except the deploy against an OSS endpoint
(`--backend oss`), then stops and names what remains Union-only (API keys, the remote image builder and MCP
authentication). See [oss-flyte-compat.md](oss-flyte-compat.md). Do not follow Union-only commands against an OSS
endpoint.

**Template repository.** Setup uses the workspace template bundled in the engine, so it works from the engine
repository alone. Docs link to `unionai-oss/flyte-agent-workspace`, but that repository does not exist yet. Publishing
it (a copy of the bundled template, marked as a GitHub template repository) is optional and later; until it exists,
the **Use this template** button and `gh repo create --template` in the manual reference fail.

**Publish the engine first.** The bootstrap command clones `https://github.com/unionai-oss/flyte-agent-factory`
and runs `ops/bootstrap.py` from its `main`, and the skill links in [Start here](#start-here) point at
`skills/dist/` on that same `main`. These resolve only once this change is on `unionai-oss/flyte-agent-factory`
`main`; until it is merged there, `ops/bootstrap.py` and both zips are missing from the public engine's `main`,
so the zip links and the raw `ops/bootstrap.py` URL return 404 and the bootstrap command stops because the clone
has no `ops/bootstrap.py`.

---

# Manual reference

Everything above is automated by `ops/bootstrap.py`. This reference is the same procedure by hand, for anyone who
is not using the bootstrap, or who wants to see what it does. The packaged operator skill's
[setup instructions](../skills/agent-factory/references/setup.md) describe how the chat guides it.

## 1. GitHub access and your workspace

Ask which GitHub account or organization should own the workspace and whether one already exists. Inspect it
before creating anything. The workspace holds your standards, briefs and project output; it is separate from the
[engine](https://github.com/unionai-oss/flyte-agent-factory). Create it private and empty; step 3 fills it from the
engine's bundled template.

If the chat's GitHub tool can create a repository (`create_repository`), have it do that on your request, then
read back the URL and default branch. Repository access in chat does not imply that capability. Otherwise run this
when asked, replacing `you`:

```bash
gh auth status
gh repo create you/agent-workspace --private
```

If `gh` is missing, GitHub's **New repository** page (private, empty, no README) needs no installation. If
authentication is missing, sign in through GitHub or `gh auth login`; do not paste a GitHub token into chat.
Do not recreate or overwrite an existing workspace. Its `.github/CODEOWNERS` must name your login in place of
`@OWNER`: have chat use its GitHub file-edit tool where available, then read back the diff. Otherwise have it prepare
one copy-paste command for your local workspace checkout that
replaces only `@OWNER` with the confirmed login and shows `git diff -- .github/CODEOWNERS`. Review and commit
that change before continuing. If you have no local checkout of the workspace yet, the command clones it first
(`gh repo clone you/agent-workspace`, or `git clone` with its SSH URL) and ends by committing and pushing the
change. Review its `standards/` and `waivers.toml`, which the factory cannot
change. Branch protection is a separate owner choice ([branch-protection.md](branch-protection.md)); the
bootstrap creates the `agent-workspace-main` ruleset for you.

## 2. Backend access and local tools

Collect non-secret values: Union endpoint, project ID, domain and Flyte config path. If you have no Union
organization, get backend access before continuing; this guide does not provision a tenant or a Kubernetes
cluster. For OSS, stop at the compatibility audit above. Reuse an existing project only if it is dedicated to
these agent credentials.

You need `git`, OpenSSH (`ssh-keygen`), [uv](https://docs.astral.sh/uv/getting-started/installation/), and
[Claude Code](https://docs.claude.com/en/docs/claude-code/setup). Check each before installing it with its official
instructions. `gh` is optional. All subsequent shell blocks run locally in the engine checkout unless stated.
The Union key and policy commands in step 5 need Linux x86_64; on macOS, run those on a Linux machine
([factory-mcp-setup.md](factory-mcp-setup.md#connecting-a-client) has the platform note).

```bash
git --version
command -v ssh-keygen
uv --version
claude --version
```

Clone the public engine if absent:

```bash
git clone https://github.com/unionai-oss/flyte-agent-factory.git
cd flyte-agent-factory
uv sync --python 3.12
git remote -v
```

If already cloned, inspect the remote and working tree before updating it. Python 3.12 is downloaded by `uv` if
needed. Deployment scripts fetch `origin/main` and deploy that committed code, not local edits. A read-only
public engine clone suffices for first install. Auto-deploy with the App follows the public engine, so `[repo] factory` stays the public engine; a fork is only for
changing the engine itself ([setup.md](setup.md#deploying)). The deployer's expected failure with the public engine
is described under step 7 above.

The operator skill is the same for everyone and carries no deployment values; install it as in [Start here](#start-here)
if you have not. Resume with the same installed skill.

If your config already exists, inspect its endpoint locally and reuse it. Otherwise, substitute the values you
chose (the output is a file, not a directory), then sign in on first use:

```bash
uv run flyte create config --endpoint flyte.example.com --project my-agents --domain development --output ~/.flyte/config.yaml
uv run flyte --config ~/.flyte/config.yaml get project
```

If the selected project is absent, create it; do not interpret an authentication or network error as absence:

```bash
uv run flyte --config ~/.flyte/config.yaml create project --id my-agents --name "Agents"
```

## 3. Profile and secret names

If the public workspace template in step 1 was unavailable, this local fallback generates the workspace.
**Skip it if your workspace already exists.** Create an empty GitHub repository `you/my-workspace` first
(GitHub, New repository, empty, no README), then run from the engine checkout. This fallback uses `gh` with
HTTPS authentication for your local push; the factory will use its separate SSH deploy key. Check
`gh auth status`; if needed run `gh auth login` and choose GitHub.com, HTTPS and browser sign-in. Then run
`gh auth setup-git` to configure git's credential helper.

Substitute your GitHub login for `you` (on macOS use `sed -i ''`):

```bash
uv run python ops/new_workspace.py ../my-workspace
cd ../my-workspace
sed -i 's/@OWNER/@you/' .github/CODEOWNERS
git var GIT_AUTHOR_IDENT
```

If git reports no identity, set it in this workspace only, using your name and GitHub verified or noreply email:

```bash
git config user.name "Your Name"
git config user.email "you@users.noreply.github.com"
git var GIT_AUTHOR_IDENT
```

Once the identity check succeeds, continue from the workspace directory:

```bash
git commit -qam "Set the code owner"
git remote add origin https://github.com/you/my-workspace.git
git push -u origin main
cd ../flyte-agent-factory
```

The generator initializes git and makes the first commit; do not initialize it again. Continue with the
engine's deployment profile rather than the generated workspace's profile suggestion.

If `profile.toml` already exists in the workspace, validate and resume it; never overwrite a half-edited one.
Otherwise have chat prepare the filled-in non-secret profile as one copy-paste command that writes `profile.toml`
at the workspace root only if absent, using a quoted heredoc and exclusive creation (`open(..., "x")` in
Python). Start from the workspace's own `profile.example.toml` and your confirmed `name` (the workspace
repository's name), `[backend] endpoint`, `project`, `domain`, `config`, `[repo] project` (the workspace's SSH URL),
`[secrets]` names and the slots of `profiles/example-claude-personal.toml`. It also carries `[direction]`, so the
deployer reads this same file from the workspace:

```toml
[direction]
repo = "you/my-workspace"
profile = "profile.toml"
```

Include `[state] uri` only after the storage prerequisite below is satisfied. If `profile.toml` already exists (an
earlier run, or a hand copy), never overwrite it: use a targeted edit of the confirmed fields, preserving all other
settings, in one copy-paste command instead. Chat can request non-secret profile fields to prepare that edit; never
read credential files or secret values into chat. Have the command parse the resulting TOML with Python's `tomllib`
and run the profile validation below. Keep `[mcp] public_url` empty until the first deployment. Choose names
for `[secrets] claude_token` and `deploy_key`; these are references, never values. The profile holds your
project, host and secret names, so keep the workspace private. With a GitHub App ([github-app.md](github-app.md))
also set `[github]`, `[deploy] auto = true`, `[direction]` and `[repo] factory`, the engine repository the deployer follows (the
public engine; step 7 above explains the expected deployer failure). The engine
loads the file by path, with `--profile` or `AGENT_FACTORY_PROFILE`; it does not look for it in the workspace itself.

To edit by hand instead of through chat, copy the example once and edit it. The test form copies only
when the file is absent, and behaves the same on macOS and Linux, where `cp -n` does not (recent GNU `cp -n`
reports a skipped file as an error):

```bash
[ -e ../my-workspace/profile.toml ] || cp ../my-workspace/profile.example.toml ../my-workspace/profile.toml
```

```bash
export AGENT_FACTORY_PROFILE=../my-workspace/profile.toml
PYTHONPATH=src uv run python -m flyte_agent_factory._profile name
export FLYTE_CONFIG="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile config)"
export FLYTE_PROJECT="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile project)"
export FLYTE_DOMAIN="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile domain)"
```

### Required shared state storage

Both `example-claude-personal.toml` and `example-codex-personal.toml` omit `[state]`. **Add it to your copied
profile before deploying.** Every launch uses the queue and rejects an empty state URI, even with auto-deploy
disabled. Profile parsing alone does not check storage access.

Ask your backend administrator which native object-storage provider and runtime identity this Union project
uses. Have them provision a private, encrypted bucket/container and a dedicated prefix for this deployment,
or authorize an existing one. Use that provider's console and IAM tools; the factory does not create storage
or attach cloud roles. Give the factory tasks (including sweep/reconcile/deployer), project slots and any
local operator using `ops/operate.py` access to list, read, create/overwrite and delete objects under this
prefix. A Union API key grants control-plane access; it does not grant native object-storage permissions.
The local machine and runtime need their own provider authentication; a laptop's cloud login is not inherited
by remote tasks. Ask the administrator to configure the runtime workload identity, not long-lived cloud keys
in a profile or chat.

For **AWS S3**, the administrator must allow `s3:ListBucket` on the bucket restricted to the chosen prefix,
and `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on objects under it. SSE-KMS additionally needs the key's
policy and encrypt/decrypt/data-key permissions for those identities. For **GCS**, use bucket/prefix-scoped
object list/get/create/delete permissions (overwrite needs delete). For **Azure**, use scoped blob data
list/read/write/delete permissions, such as Storage Blob Data Contributor on the intended container.
Do not choose a URI scheme until the administrator confirms the provider and grants. There is no single
cloud-provisioning command valid across these backends. If that access is unavailable, stop here: setup cannot
continue until the administrator supplies the provider, bucket/prefix and runtime identity.

Once the administrator confirms storage, have chat include `[state] uri` in the profile handoff, or prepare
one copy-paste command to add/update only that field in the existing profile and validate it. Do not append a
duplicate `[state]` table. For an administrator-confirmed S3 bucket and prefix, the resulting section is:

```toml
[state]
uri = "s3://my-agent-state/factory/mine"
```

Use `gs://`/`gcs://` for confirmed GCS or `abfs://`/`abfss://` for confirmed Azure instead. `file://` on your
laptop or a pod's disk is not shared durable state for the factory.

After selecting and provisioning the provider, this command tests local list/write/read/delete access using
the same Flyte storage interface as the queue. It writes only a uniquely named probe under the chosen prefix;
run locally with provider credentials already configured, and do not send credential errors unredacted to chat:

```bash
PYTHONPATH=src uv run python - <<'PYTHON'
import uuid
import flyte.storage
from flyte_agent_factory import _profile

uri = _profile.require().state.uri
if not uri:
    raise SystemExit("Set [state] uri first")
fs = flyte.storage.get_underlying_filesystem(path=uri)
path = uri.rstrip("/") + "/setup-probe-" + uuid.uuid4().hex
fs.find(uri)
try:
    fs.pipe_file(path, b"factory setup probe")
    assert fs.cat_file(path) == b"factory setup probe"
finally:
    if fs.exists(path):
        fs.rm(path)
print("Local storage probe passed; runtime identity still needs its own check")
PYTHON
```

A local pass does not prove runtime access; the administrator must check the same operations as the runtime
identity before first launch. This project has not run that live check against a real bucket; see
[install-audit.md](install-audit.md#setup-walkthrough-verification).

List secret names/metadata in the selected project/domain:

```bash
uv run flyte --config "$FLYTE_CONFIG" get secret -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
```

Check secret **names** before creating them. Existing secrets do not prove valid credentials; reuse them until
an authentication failure or an explicit rotation request requires replacement. Never ask for, read back or
send an engine secret through chat, including through MCP `create_secret`.

## 4. Store engine credentials locally

For a missing Claude token, sign in to your Pro or Max subscription with `claude setup-token` on your machine.
Keep its output local. Then run the existing helper, setting `CLAUDE_TOKEN_SECRET` to the name in your profile:

```bash
claude setup-token
CLAUDE_TOKEN_SECRET=claude_setup_token FLYTECTL_CONFIG="$FLYTE_CONFIG" uv run bash scripts/export_local_credentials.sh
```

The helper reads the token without echo and passes it to Flyte using a temporary 0600 file. It **replaces** an
existing secret, so skip it when resuming with a working token. The token never belongs in a chat, command
argument, brief, profile or commit. Check only the secret's metadata:

```bash
uv run flyte --config "$FLYTE_CONFIG" get secret claude_setup_token -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
```

For a missing workspace deploy key, make a temporary local key and add only its public half to the workspace
with write access. The following block uses Bash and `gh`; substitute your workspace and profile's secret name.
Without `gh`, add the `.pub` file through GitHub Settings, Deploy keys before the Flyte create command.

```bash
(
set -euo pipefail
key_dir="$(mktemp -d)"
trap 'rm -rf "$key_dir"' EXIT
ssh-keygen -t ed25519 -N "" -f "$key_dir/agent-key"
gh repo deploy-key add "$key_dir/agent-key.pub" --repo you/my-workspace --allow-write
uv run flyte --config "$FLYTE_CONFIG" create secret my_deploy_key --from-file "$key_dir/agent-key" -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
)
```

If storing the secret fails after adding the public key, remove that unused deploy key in GitHub before retrying
with a new pair. Do not delete a working key to restart setup. Additional repositories need separate deploy keys
and `[repo_keys]`; the operator's GitHub connector credential never goes into a slot ([secrets.md](secrets.md)).

## 5. Deploy and connect the factory

### Local Union deployment bootstrap

The bootstrap's `deploy` step uses `_UNION_EAGER_API_KEY` from your environment if it is set. Otherwise it makes a
one-off key for the run and deletes it at the end, and that needs this: the deploy makes its one-off Union API key with flyteplugins-union, which installs on Linux x86_64 only. On another machine
(macOS, Linux on ARM) no single command can make the key, so the step stops and says so; run the bootstrap on a Linux
x86_64 machine, or export `_UNION_EAGER_API_KEY` with a key made there, as below.

The deployment child scripts call `flyte.init_from_api_key` with `os.environ["_UNION_EAGER_API_KEY"]`.
The selected Flyte config does **not** automatically supply this environment variable. The pinned SDK expects
the full base64-encoded `endpoint:client_id:client_secret:org` credential, not just the client secret or a
`client_id:secret` pair. Its endpoint must match your intended backend. Keep it local.

Reuse a suitable deployment key from your local password manager if one exists. Otherwise, on a Linux x86_64
machine, the stopped `deploy` line prints this one command with your values filled in (here with the example
values); it makes the `factory-deploy` key with no default policies, a `factory-deploy` policy binding
`contributor` to the factory's project and domain, and the assignment, then writes the encoded key once to that
terminal only, never to stdout. If that machine has no Flyte config for `--endpoint`, the command writes one and opens the browser sign-in first; a config for another endpoint stops it before any change.

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --deploy-key
```

Store the encoded value in your local password manager. If a `factory-deploy` key already exists, the command stops
and says how to delete it by hand; it never deletes one. Never send the encoded key or client secret to chat.

If you run with another administrator, make the key and the grant by hand instead. Create the key:

```bash
uv run flyte --config "$FLYTE_CONFIG" create api-key --name factory-deploy --no-default-policies
```

The CLI displays a client ID and an `export FLYTE_API_KEY=...` suggestion containing the encoded credential.
**Do not execute that suggested export with a literal secret.** Store the encoded value in your local password
manager. With `--no-default-policies` the key has no permissions until a policy is assigned. Run the grant block
in [factory-mcp-setup.md](factory-mcp-setup.md#connecting-a-client) with `policy=factory-deploy` and this key's
client ID; ask chat to prepare that existing grant block as one copy-paste command with `policy=factory-deploy`
and `client_id` filled in from the non-secret client ID.
The chat key gets its own policy later. It binds `contributor` to the factory's project and domain;
if `ops/deploy_work.py` fails with a permission error, that page gives the command that widens the policy. Policy
creation, the assignment and live deployment have not been run against a live tenant by this project; see
[install-audit.md](install-audit.md#setup-walkthrough-verification).

In a local bash or zsh terminal with session recording disabled, load only the encoded value at the prompt:

```bash
{
set +x
printf 'Union encoded deployment key: '
stty -echo
IFS= read -r _UNION_EAGER_API_KEY
stty echo
printf '\n'
if [ -n "$_UNION_EAGER_API_KEY" ]; then export _UNION_EAGER_API_KEY; else printf 'No key entered; run this block again.\n'; fi
}
```

This works in both bash and zsh, which differ on `read -s` and `read -p`; the braces make the shell read the whole
block before the prompt, so the rest of the paste is not taken as the key. The prompt prevents echo and a
literal credential in shell history; if you interrupt it, run `stty echo` to restore your terminal. Do not
print the variable, put it in a profile, or paste the terminal output into chat. The key is available to local
deployment child processes; this is separate from the runtime's backend credentials and native storage
identity.

Confirm the selected profile and intended target, then run the two first-install commands. Deploying builds
images and consumes backend compute; run it when you are ready to deploy, rather than as a connection test.

```bash
export AGENT_FACTORY_SECRETS="$(PYTHONPATH=src uv run python -m flyte_agent_factory._profile agent_secrets)"
PYTHONPATH=src uv run python ops/deploy_work.py
PYTHONPATH=src uv run python ops/deploy_factory.py
```

The first deploy from a workspace clone names its profile once, as `--profile ../my-workspace/profile.toml` (or the
environment variable above). Because that file carries `[direction]`, the deployer reads it from the workspace from
then on. Moving an **existing** install onto `[direction]` later needs one deploy with that `--profile` too: the
bootstrap's `--redeploy` command of step 7 above, run once. The deployer refuses to change its own direction by
design.

Keep a successful slots deployment if the factory deployment fails; resolve the named failure and rerun only
the failed step. When resuming, inspect what is already deployed first:

```bash
uv run flyte --config "$FLYTE_CONFIG" get task -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
uv run flyte --config "$FLYTE_CONFIG" get app -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN"
```

`--dry-run` on `ops/deploy_work.py` or `ops/deploy_factory.py` describes a deployment but still fetches code
and initializes the backend; it is not an offline readiness check.

Read `factory-mcp public URL:` from the result, or the console's Apps page. Give chat only that non-secret URL;
have it prepare one copy-paste command that updates only `[mcp] public_url` in the existing local profile,
preserves its other settings, parses the result and runs the profile validation from step 3. Do not append a
second `[mcp]` table. The resulting section looks like:

```toml
[mcp]
public_url = "https://factory-mcp.example.com"
```

Then rerun `ops/deploy_factory.py` while the deployment key is still loaded. After the final deployment
(or before leaving a failed session), remove it from the shell:

```bash
unset _UNION_EAGER_API_KEY
```

To resume a failed deployment in a new shell, restore the profile exports in step 3 and reload the saved key
with the non-echo prompt; do not create another key. This is a two-pass bootstrap; a deployed app URL
is not proof a chat client can authenticate. The connector path is `<public_url>/flyte-mcp/mcp`.

Follow [chat-operator.md, Setup](chat-operator.md#setup) in order:

1. Create or reuse a scoped **Union** API key locally and configure the factory MCP in the client's
   credential settings. At this point have chat prepare the existing grant block as one copy-paste command
   with `policy=factory-chat` and the non-secret `client_id` filled in; keep the encoded key local. Use the
   [grant block](factory-mcp-setup.md#connecting-a-client), without changing its safety checks or role scope.
   Do not paste the key into the conversation. In Claude Code, run `claude mcp list` first (or
   `claude mcp get agent-factory`) and skip the add if the connector is already there. Claude Code supports a bearer
   header. claude.ai works only where its request-header connector beta is available; OAuth discovery is
   unsupported by this stock app. If the header UI is absent, continue with Claude Code. There is no hosted
   setup app that closes this gap.
2. Call `whoami` to check authentication, then list tasks in the selected project/domain and confirm
   `factory.launch` and `factory.status`. A successful sign-in alone does not establish these permissions.
3. Install the operator skill if you have not ([Start here](#start-here)). It is the same package for every
   deployment: it reads the project, domain and allowed repositories from the factory MCP's server instructions
   when it connects, so there is nothing to rebuild when the profile changes.
4. Connect GitHub's official MCP separately ([github-connector.md](github-connector.md)) and check repository
   access. For boards also check `projects_list`, `projects_get`, `projects_write`; repo access alone does not
   grant Projects access. Install tokens/OAuth secrets in connector settings, never in chat or factory secrets.

## 6. Launch and confirm

Tell the operator the name, outcome and acceptance criteria, for example:

> Launch `csv-export` on my workspace: add a CSV export to the report page. Done when `GET /report.csv`
> returns the page's rows and existing tests pass.

The operator checks for a live or queued project with that name first, then calls `factory.launch` with the
brief, name and explicit model. It reads the result and gives the run URL for `started`, the position for
`queued`, or explains the failure. Use `factory.status` to follow work and answer its questions. A launch is
not finished work: independent review must pass. Without `[github]` the session pushes the workspace default
branch and opens no PR; your own GitHub App ([github-app.md](github-app.md)) enables draft PRs and verifier statuses. It is separate from
the operator's GitHub connector. Merging and promotion require your instruction.

See [operating.md](operating.md) for direction, stop and resume, and [setup.md](setup.md) for optional browser,
auto-deploy, GitHub App and further repositories. This project has not run the fresh-tenant and claude.ai
flows end to end; [install-audit.md](install-audit.md) records what was checked and what remains.

### Codex sign-in

Optional after the Claude path is working. Use `profiles/example-codex-personal.toml` and the credential
procedure in [engines.md](engines.md#codex-credentials). A cluster `codex_bootstrap_login` runs only after
its task is deployed. Because the slot mounts its login secret, a placeholder secret is needed before that
first deployment; the bootstrap task replaces it. That placeholder/deploy/sign-in sequence has not been run
end to end. Keep device-login output and `auth.json` local; a chat can relay the one-time sign-in link/code, never
an engine credential. The pure-Codex path without a Claude token is not supported by the profile parser.
