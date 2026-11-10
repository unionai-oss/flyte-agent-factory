# Guide a first install or resume setup

Use this mode for “help me set up the factory,” including before a deployment profile or factory MCP exists.
The public procedure and commands are at
https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/quickstart.md.
Read that page when guiding setup; if you cannot retrieve it, give the link to the person and explain the
limitation. Do not invent tools, CLI flags or a hosted bootstrap service. The stock factory MCP only becomes
available after deployment. This guide is packaged with the skill so setup routing remains available offline;
the commands live in the quickstart rather than a second copy here.

## Establish where to resume

Ask for the intended GitHub owner/workspace and backend endpoint/project/domain, and whether any setup exists.
These are non-secret identifiers. Use connected GitHub tools to inspect the workspace and available tools to
check the current setup. If you have a local shell, check tools, config/profile existence and validation there.
If the shell is remote, it is not the person's laptop: give them local commands for sign-in and credentials.
Never read credential files or environment values into chat or tool output. Ask for success/failure and
redacted errors only. Track a short checklist in the conversation: complete, next, blocked, not checked.
A previous message saying “deployed” is not evidence of a reachable MCP or usable credentials.

claude.ai has no local shell. When a local action is needed, prepare one copy-paste command at that point,
with confirmed non-secret values filled in, using the existing quickstart commands. Never include a secret;
keep credential entry in the documented local non-echo prompt or client settings. Use a GitHub file-edit tool
for repository edits where available and read back the diff. Report the observed result separately from the
handoff; providing a command does not mean it ran.

Offer one next step at a time, explain what success looks like, and use its result before continuing. Do not
make the person answer every optional configuration choice up front. The first-launch path is Union plus a
Claude Pro/Max subscription: start with the Union/Claude personal profile; add optional features only when
requested. If they choose OSS Flyte, read the compatibility audit at
https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/oss-flyte-compat.md and explain the implementation gaps
before offering any Union commands. Basic Flyte
runtime support does not establish install or authenticated MCP parity. A public bootstrap service, a hosted
setup app and full install parity through the MCP are planned, not built; say so when asked.

## Move through the setup

1. **GitHub.** Check account/repository access. Reuse an existing workspace. When requested and the connected
   tool supports template creation, create it from `unionai-oss/flyte-agent-workspace` and read back its URL and
   default branch. Otherwise offer the template's web button or the single `gh repo create --template` command
   at this point. Do not claim repository-scoped chat tools can create repositories or manage Projects boards.
   Replace `@OWNER` in `.github/CODEOWNERS` with the confirmed login through the GitHub file-edit tool;
   otherwise prepare one copy-paste command that clones the workspace if there is no local checkout, makes
   that edit, shows its diff, then commits and pushes it.
2. **Local tools and engine.** Check git, ssh-keygen, uv and Claude Code before installing them. Use the public
   engine `https://github.com/unionai-oss/flyte-agent-factory`. Inspect an existing checkout's remote/changes;
   deployment uses fetched `origin/main`, not local changes. No engine fork is required for first install.
   The Union key and policy commands need Linux x86_64 (see the factory-mcp-setup platform note); on macOS
   the person runs those on a Linux machine. Once it is synced, package the setup skill with `ops/package_skill.py --setup`, which needs no profile: it
   includes this reference without deployment placeholders or launch instructions.
3. **Backend.** Collect the non-secret Union endpoint, dedicated project/domain and config path. A Union tenant
   and `flyte create api-key` are Union-only; on OSS Flyte go to the compatibility audit first. A person without
   a tenant must obtain backend access; there is no public provisioning service. Check configuration and list
   projects before creating one. An auth/network failure is not evidence that the project is absent.
4. **Profile.** Validate an existing profile, or copy the personal example only if `profiles/mine.toml` is
   absent; never overwrite a half-edited one. Keep private profiles local/private and hold secret names only.
   Both personal examples omit `[state]`: add a mandatory shared object-storage URI. Ask the backend
   administrator to provision native storage and grant list/read/write/delete access to the runtime identity
   and local operator; a Union key does not supply cloud storage permissions. Stop at this prerequisite if
   provider/access is unknown.
   Once storage is confirmed, prepare a filled-in non-secret profile in one copy-paste command: include
   `name`, `[backend]` endpoint/project/domain/config, `[repo] project`, `[secrets]` names, the example's slots,
   `[mcp]` with an empty URL, and `[state] uri`. Use the quickstart's exclusive-create/quoted-heredoc approach;
   never overwrite an existing profile. For resume, prepare a targeted edit of only confirmed fields instead,
   preserving other settings and avoiding duplicate tables. Parse the TOML and run the existing profile
   validation in that handoff; do not ask the person to fill placeholders in an editor.
5. **Credentials.** Confirm access to a Claude Pro/Max subscription; guide local `claude setup-token` only when
   needed. Check secret names/metadata only. A missing Claude token is stored with the existing local helper;
   a workspace deploy key is generated and stored locally. Never ask for engine tokens or private keys in
   chat and never use MCP `create_secret` for them. Skip working secrets on resume: the helper replaces its
   secret. If a deploy-key upload partially succeeds, identify the unused public key for cleanup before retry.
6. **Deploy.** Explain the target and compute implications; a setup request is not permission for unrelated
   deployments. Use the existing deployment scripts once the person is ready. They require
   `_UNION_EAGER_API_KEY` locally; the selected config does not populate it. Follow the quickstart's Union
   encoded-key creation and policy grant (Union-only; the key does nothing until a policy is assigned) and its
   grant block: prepare one copy-paste command with `policy=factory-deploy` and non-secret `client_id` filled
   in, retaining the block's safety checks and scope. Keep the encoded key local. Use the documented
   non-echo prompt; never put a literal secret in shell history or chat. Unset the variable after the final deployment
   or on leaving a failed session. Retain each successful step.
   Capture only non-secret results and errors. Get the app URL and prepare one copy-paste command that edits
   only `[mcp] public_url` in the existing profile and validates it, preserving other settings without a duplicate
   table. Then deploy the factory again. Do not claim a deployment succeeded without the command result.
7. **Factory MCP.** Guide a scoped Union key into local client/connector settings, never the conversation.
   The scoped Union key, its policy grant and the hosted app's bearer authentication are Union-only. The
   key does nothing until a policy is assigned: give the person the
   [grant block](https://github.com/unionai-oss/flyte-agent-factory/blob/main/docs/factory-mcp-setup.md#connecting-a-client).
   Prepare that block as one copy-paste command with `policy=factory-chat` and non-secret `client_id` filled
   in at this step; never ask for the encoded key or client secret. Preserve the block's safety checks and scope.
   Claude Code supports the required bearer header; claude.ai requires its request-header beta. OAuth
   discovery is not implemented by the stock app. If headers are unavailable, offer Claude Code and state the
   gap. Before adding the connector, check `claude mcp list` (or `claude mcp get <name>`) and skip the add if it
   is present. Check `whoami` and registered launch/status tasks, then the selected project/domain permissions.
8. **Skill and GitHub connector.** After deployment, replace the setup skill with the `--profile` package, which
   includes this reference, and keep it private. Rebuild after profile changes. Configure GitHub separately in
   client settings, with the same `claude mcp list` check; check repo access, plus Projects tools only if boards
   are wanted. Fine-grained repository tokens need Contents, Pull requests and Issues write, and an
   organization-owned board also needs the organization Projects permission. Enter headers through local
   non-echo prompts, never literal token commands. Its credential stays outside factory slots. A factory GitHub
   App for draft PRs is optional and different from this interactive connector.
9. **Launch.** Gather a name, outcome and checkable acceptance criteria. Check queue/status before launching to
   avoid duplicating live work. Use `factory.launch` with explicit model and read its outputs. Report started
   with URL, queued with position, or failure with the next repair. Review passing is separate from launch;
   without `[github]` output is pushed to the default branch and no PR opens. Never merge without instruction.

## Resume and report accurately

Before each create/store/deploy action, check what already exists; do not rotate keys, delete resources or
restart working parts merely to make setup look linear. Distinguish absent from inaccessible. If inspection is
unavailable, say “not checked” and ask the person to run the next read-only check. Resolve a failed step before
advancing; keep completed steps. Finish with what was observed, what is blocked, and the next action.

No fresh-tenant or claude.ai end-to-end execution is implied by this guide. Report unperformed external steps as
NOT RUN and identify any local/source substitute used. Do not call a static command check a successful install.
