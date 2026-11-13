# Secrets

Every Flyte secret a profile can name, in one table. A profile key holds the *name* of a Flyte secret, never its
value. Create each secret in the Flyte project and domain of `[backend]`, with `-p <project> -d <domain>` (or
`FLYTE_PROJECT` / `FLYTE_DOMAIN`, [quickstart.md](quickstart.md#4-store-engine-credentials-locally)).
Create a secret before the deploy that mounts it: a missing one is a hard pod mount failure in Flyte, and the
deployer deploys nothing until it exists.

Only `claude_token` and `deploy_key` are needed for a first project. Everything else is optional and is listed
with what it unlocks. Never paste a token on a command line: use `--from-file` (a 0600 temporary file) as below.
`flyte create secret` refuses to overwrite; see "Rotation" to replace one.

| Profile key | Required? | What it unlocks | How to obtain the value | Store it | Mounted as, and where |
|---|---|---|---|---|---|
| `[secrets] claude_token` | required | every Claude session: coordinator, workers, judges, the usage meter | `claude setup-token` on a machine signed in to a personal Pro or Max plan; copy the printed token | `uv run bash scripts/export_local_credentials.sh` (reads the token without echo and stores it), or `flyte create secret <name> --from-file <file> -p <project> -d <domain>` | env `CLAUDE_CODE_OAUTH_TOKEN`, every slot and sub-agent pod |
| `[secrets] deploy_key` | required | pushing the workspace repository | an ed25519 key made in a `mktemp` directory, added as a deploy key with write access on the workspace repository: the block in [quickstart.md §4](quickstart.md#4-store-engine-credentials-locally) | the same block stores it as the secret and removes the local key | env `GIT_DEPLOY_KEY` (the private key's text), every slot; a session writes it 0600 |
| `[repo_keys] <key>` | optional | launching projects into a further `[repo]` entry, such as a repository of yours that is not the workspace | as `deploy_key`, one key per repository; it must differ from `deploy_key` | as `deploy_key` | env `GIT_DEPLOY_KEY_<KEY>` (`<KEY>` upper case), every slot; a session installs only the key of the repository it was launched on |
| `[secrets] codex_token` | optional | Codex on every slot, on a Business or Enterprise workspace | the workspace's Access tokens page in ChatGPT. A personal Plus or Pro plan cannot create one: use `codex_login` | `flyte create secret <name> --from-file <file> -p <project> -d <domain>` | env `CODEX_ACCESS_TOKEN`, every slot |
| `[[slots]] codex_login` | optional | Codex on one slot, on a personal ChatGPT Plus or Pro plan. The name must start with `codex_login`; one secret per slot, never shared and never also `codex_token` | either `codex_bootstrap_login` in the cluster ([quickstart.md](quickstart.md#codex-sign-in)), which creates the secret itself, or `CODEX_HOME=$(mktemp -d) codex login --device-auth` and use `$CODEX_HOME/auth.json` ([engines.md](engines.md#codex-credentials)) | `flyte create secret <name> --from-file "$CODEX_HOME/auth.json" -p <project> -d <domain>`, then `rm -rf "$CODEX_HOME"` | env `CODEX_ACCESS_TOKEN` (the `auth.json` text) on that slot only; the coordinator writes each refreshed login back to the secret |
| `[secrets] gateway_key` | optional | sending model traffic through an LLM gateway | your gateway's key | `flyte create secret <name> --from-file <file> -p <project> -d <domain>` | env `LLM_GATEWAY_KEY`, every slot |
| `[secrets] api_key` | optional | an Anthropic API key for the project's own code (not the session) | the Anthropic console | as `gateway_key` | env `PROJECT_ANTHROPIC_API_KEY`, every slot (never `ANTHROPIC_API_KEY`: sessions do not see an API key) |
| `[github] private_key_secret` | optional | the GitHub App: pull requests, commit statuses, the deployer's reads. Needs `app_id` | the App's settings, Generate a private key (a PEM file). Create the App with the permissions in [profiles/README.md](../profiles/README.md) | `flyte create secret <name> --from-file app-key.pem -p <project> -d <domain>` | env `GITHUB_APP_PRIVATE_KEY`, in project slots (the supervisor removes it from the session's environment), `factory`, `factory-deployer`, `factory-reconcile` |
| `[github] webhook_secret` | optional | push wake-ups and issue intake ([deploy-webhook.md](deploy-webhook.md)) | `openssl rand -hex 32`; paste the same value in the App's webhook settings | `flyte create secret <name> --from-file <file> -p <project> -d <domain>` | env `GITHUB_WEBHOOK_SECRET`, `factory-webhook` only |
| `[board] pat_secret` | optional | moving issue cards on a Projects v2 board ([github-connector.md](github-connector.md)) | a classic personal access token of a machine user, scopes `project` and `repo` | as `gateway_key` | env `GITHUB_BOARD_PAT`, the supervisor and `factory-reconcile` |
| `[browser] tailscale_secret` | optional | routing the in-pod browser through a Tailscale exit node (needs `exit_node`) | a reusable, ephemeral, pre-approved Tailscale auth key tagged `tag:factory-browser` ([browser-exit-node.md](browser-exit-node.md)) | `flyte create secret <name> --from-file <file> -p <project> -d <domain>` | a file under `/etc/flyte/secrets`, in slot pods; read only by the supervisor |
| personal browser identity token (fixed name `browser_<identity>_token`, dashes as underscores; one per `[browser.identities.<identity>]`, or `browser_personal_token` for the legacy `personal = true`) | optional | one signed-in browser identity ([personal-browser.md](personal-browser.md)) | generated, never shown | `python ops/create_personal_browser_token.py --identity <identity> -p <project> -d <domain>` | env `BROWSER_<IDENTITY>_TOKEN`, every slot and the factory (read by the supervisor, removed from the session) and the identity's browser app |
| personal browser Tailscale pair (fixed names `tailscale_oauth_client_id`, `tailscale_oauth_client_secret`; only when a personal browser identity egresses through an exit node) | optional | the personal browser's exit node | a Tailscale OAuth client ([personal-browser.md](personal-browser.md)) | `flyte create secret tailscale_oauth_client_id --value <id> -p <project> -d <domain>`, and the same for the secret (from a file) | the personal browser app only |
| `[gcp] secret_name` | optional | the Flyte secret name the `gcp_bootstrap` task stores the service-account key under when the task is given none; empty by default, and then the task needs `secret_name` ([gcp-bootstrap.md](gcp-bootstrap.md)) | the task creates the key; you do not | none: the task creates the secret | not mounted by the profile; a project that needs it lists it in `AGENT_FACTORY_SECRETS` ([setup.md](setup.md#5-extra-secrets-optional)) |
| `[bench] repo_key` | optional | the factory benchmark's repository; must not be a production key | as `deploy_key`, for the bench repository | as `deploy_key` | the bench case environments, as the deploy key |
| `[bench] claude_token` | optional | a separate Claude token for benchmark cases; defaults to `[secrets] claude_token` | as `claude_token` | as `claude_token` | the bench case environments, as `CLAUDE_CODE_OAUTH_TOKEN` |

`[repo_keys]` is a table: one row of the table above per entry, the key naming the `[repo]` entry it belongs to.

A profile that still has `[secrets] browser_token` is refused with `unknown key(s) in [secrets]: browser_token`:
nothing ever mounted it, so delete the line.

## Credentials for one project

A cloud credential that one project needs for a while (AWS access to install and test something, say) is **not** a
Flyte secret. Flyte secrets belong to the whole Union project and domain, so every factory project and every child
action could read one, and a new one reaches a project only through a redeploy. Give it with a credential grant
instead: the owner seals it in the browser to that project's in-memory key, and it is held in memory until the time
the owner chose. See [credential-grants.md](credential-grants.md).

## Rotation

A secret mounted into a running pod does not change while the pod runs. A replaced secret reaches a project only
through a new pod.

1. **Replace the Claude token.** Run `claude setup-token` again, then `uv run bash scripts/export_local_credentials.sh`
   with `FLYTE_PROJECT` and `FLYTE_DOMAIN` set and `CLAUDE_TOKEN_SECRET=<the name in [secrets] claude_token>`. It
   deletes the old secret, waits for the delete, and creates the new one. By hand:
   `flyte delete secret <name> -p <project> -d <domain>`, wait until `flyte get secret <name> ...` no longer finds
   it, then `flyte create secret <name> --from-file <file> -p <project> -d <domain>`.
2. **Move running projects onto it.** From chat, with the factory MCP connected
   ([factory-mcp-setup.md](factory-mcp-setup.md)), call `run_task(name="factory.rotate_token")` and read the
   result with `get_run_io`. It sends `/stop` to every live project (each saves a checkpoint and ends) and queues
   one resume of each, which starts on a new pod. Details: [operating.md](operating.md#rotating-the-token).
3. **Projects that failed on a refused token** are launched again by name once the secret is replaced.
4. **Other secrets** are replaced the same way (delete, wait, create). A new deploy key also needs the old one
   removed from the repository. A `codex_login` needs no rotation: the coordinator refreshes it and stores each
   copy. If it is refused, make a new one ([quickstart.md](quickstart.md#codex-sign-in)).
