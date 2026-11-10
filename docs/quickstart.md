# Guided setup

Start here with nothing installed, or return here to finish a partial install. Tell your chat agent:

> Help me set up Flyte Agent Factory using
> https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/quickstart.md.
> Check what I already have, guide me one step at a time, and keep credentials out of chat.

The operator uses available GitHub tools for repository work. It gives you a local command only when the next
step needs your machine or a secret. At that step, ask it to prepare one copy-paste command with the confirmed
non-secret values filled in; report success/failure or redacted errors, never credentials. claude.ai has no local shell;
run its handoffs on your machine. A chat without a shell cannot install tools or deploy for you. A public
bootstrap service, a hosted setup app and full install parity through the MCP are planned, not built: before
the factory exists, its MCP cannot provision it. If your
chat cannot read this page, open it yourself and follow the same steps below. The packaged operator skill's
[setup instructions](../skills/agent-factory/references/setup.md) describe how to guide this conversation.

The first-launch path uses **Union and Claude Pro or Max**. A Claude subscription supplies the workers through
`claude setup-token`; an Anthropic API key is not needed. Codex is optional and still needs a Claude token:
see [engines.md](engines.md). **OSS Flyte 2 is not a drop-in alternative for this installer.** Runtime primitives
exist, but builds, credentials, child attachment and MCP authentication need adaptation; see
[oss-flyte-compat.md](oss-flyte-compat.md). Union API keys, hosted app authentication and console notifications
are Union capabilities, not OSS features. Do not follow Union-only commands against an OSS endpoint.

## 1. GitHub access and your workspace

Ask which GitHub account or organization should own the workspace and whether one already exists. Inspect it
before creating anything. The workspace holds your standards, briefs and project output; it is separate from the
[engine](https://github.com/unionai-oss/flyte-agent-factory). Use the public
[workspace template](https://github.com/unionai-oss/flyte-agent-workspace/generate).

If the chat's GitHub tool supports creating a repository from a template, have it do that on your request, then
read back the URL and default branch. Repository access in chat does not imply template-creation capability.
If the tool is absent, use GitHub's **Use this template** button, or run this when asked, replacing `you`:

```bash
gh auth status
gh repo create you/my-workspace --private --template unionai-oss/flyte-agent-workspace
```

If `gh` is missing, the web button needs no installation. If authentication is missing, sign in through GitHub
or `gh auth login`; do not paste a GitHub token into chat. If the public template is unavailable, report that
bootstrap gap; once the engine is cloned, `ops/new_workspace.py` can generate the same starting layout locally
([step 3](#3-profile-and-secret-names)). Do not recreate or overwrite an existing workspace. Replace `@OWNER` in
its `.github/CODEOWNERS` with your login: have chat use its GitHub file-edit tool where available, then read
back the diff. Otherwise have it prepare one copy-paste command for your local workspace checkout that
replaces only `@OWNER` with the confirmed login and shows `git diff -- .github/CODEOWNERS`. Review and commit
that change before continuing. If you have no local checkout of the workspace yet, the command clones it first
(`gh repo clone you/my-workspace`, or `git clone` with its SSH URL) and ends by committing and pushing the
change. Review its `standards/` and `waivers.toml`, which the factory cannot
change. Branch protection is a separate owner choice ([branch-protection.md](branch-protection.md)).

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
public engine clone suffices for first install. To change the engine or enable auto-deploy of your own changes,
use a fork and configure `[repo] factory` ([setup.md](setup.md#deploying)).

You can install the setup skill now, before you have a profile or deployed MCP:

```bash
PYTHONPATH=src uv run python ops/package_skill.py --setup
```

Install `skills/dist/agent-factory.zip` through Settings, Capabilities, Skills in Claude apps. For Claude Code:

```bash
mkdir -p ~/.claude/skills
unzip -o skills/dist/agent-factory.zip -d ~/.claude/skills/
```

This neutral package contains only setup guidance and its bundled reference. It has no deployment values or
launch instructions. Resume with the same installed skill; replace it with the profile package in step 5.

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
PYTHONPATH=src uv run python ops/new_workspace.py ../my-workspace
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

If a profile already exists, validate and resume it; never overwrite a half-edited one. Otherwise have chat
prepare the filled-in non-secret profile as one copy-paste command that writes `profiles/mine.toml` only if
absent, using a quoted heredoc and exclusive creation (`open(..., "x")` in Python). Include the personal
example's slots and tables, with your confirmed `name`, `[backend] endpoint`, `project`, `domain`, `config`,
`[repo] project` (the workspace's SSH URL), and `[secrets]` names. Include `[state] uri` only after the
storage prerequisite below is satisfied. If `profiles/mine.toml` already exists (an earlier run, or a hand
copy), never overwrite it: use a targeted edit of the confirmed fields, preserving all other settings, in one
copy-paste command instead. Chat can request non-secret profile fields to prepare that edit; never read
credential files or secret values into chat. Have the command parse the resulting TOML with Python's `tomllib`
and run the profile validation below. Keep `[mcp] public_url` empty until the first deployment. Choose names
for `[secrets] claude_token` and `deploy_key`; these are references, never values. Profiles under
`profiles/mine.toml` are git-ignored; do not commit a real profile to the public engine. The engine does not
automatically load a workspace's `profile.toml`.

To edit by hand instead of through chat, copy the Claude example once and edit it. The test form copies only
when the file is absent, and behaves the same on macOS and Linux, where `cp -n` does not (recent GNU `cp -n`
reports a skipped file as an error):

```bash
[ -e profiles/mine.toml ] || cp profiles/example-claude-personal.toml profiles/mine.toml
```
A direction repository is an explicit alternative ([profiles/README.md](../profiles/README.md)).

```bash
export AGENT_FACTORY_PROFILE=mine
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

The deployment child scripts call `flyte.init_from_api_key` with `os.environ["_UNION_EAGER_API_KEY"]`.
The selected Flyte config does **not** automatically supply this environment variable. The pinned SDK expects
the full base64-encoded `endpoint:client_id:client_secret:org` credential, not just the client secret or a
`client_id:secret` pair. Its endpoint must match your intended backend. Keep it local.

Reuse a suitable deployment key from your local password manager if one exists. Otherwise create one locally:

```bash
uv run flyte --config "$FLYTE_CONFIG" create api-key --name factory-deploy --no-default-policies
```

The CLI displays a client ID and an `export FLYTE_API_KEY=...` suggestion containing the encoded credential.
**Do not execute that suggested export with a literal secret.** Store the encoded value in your local password
manager. With `--no-default-policies` the key has no permissions until a policy is assigned. Run the grant block
in [factory-mcp-setup.md](factory-mcp-setup.md#connecting-a-client) with `policy=factory-deploy` and this key's
client ID; ask chat to prepare that existing grant block as one copy-paste command with `policy=factory-deploy`
and `client_id` filled in from the non-secret client ID. Never send the encoded key or client secret to chat.
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
   `claude mcp get factory`) and skip the add if the connector is already there. Claude Code supports a bearer
   header. claude.ai works only where its request-header connector beta is available; OAuth discovery is
   unsupported by this stock app. If the header UI is absent, continue with Claude Code. There is no hosted
   setup app that closes this gap.
2. Call `whoami` to check authentication, then list tasks in the selected project/domain and confirm
   `factory.launch` and `factory.status`. A successful sign-in alone does not establish these permissions.
3. Package and install the deployment-specific operator skill:

   ```bash
   PYTHONPATH=src uv run python ops/package_skill.py --profile mine
   ```

   Keep `skills/dist/agent-factory.zip` private and untracked. In Claude Code unzip it into `~/.claude/skills/`;
   in Claude apps use Settings, Capabilities, Skills. Rebuild it if the profile changes.
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
branch and opens no PR; the optional GitHub App enables draft PRs and verifier statuses. It is separate from
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
