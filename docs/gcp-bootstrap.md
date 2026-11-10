# GCP access for a project: `gcp_bootstrap`

Click a link, sign in to Google, and a project can run against GCP without your own broad
Google credential being left anywhere. What stays behind is one service-account key, stored as a
Flyte secret.

```bash
PYTHONPATH=src uv run flyte --config "$FLYTE_CONFIG" run --root-dir src -p "$FLYTE_PROJECT" -d "$FLYTE_DOMAIN" src/flyte_agent_factory/agent.py gcp_bootstrap --project_id my-infra-123 \
  --billing_account 012345-6789AB-CDEF01 --create_project
# or: flyte run ... src/flyte_agent_factory/agent.py gcp_bootstrap --project_id my-infra-123
```

`project_id` is required and there is no default: the task grants a service account (Owner,
by default) on that project only. Arguments: `billing_account`, `create_project`, `region`
(default `us-west1`), `secret_name`, `roles`, `billing_roles`, `apis`. An empty `secret_name`,
`roles` or `apis` takes the profile's `[gcp]` value (`profiles/README.md`), then the built-in
default (`roles/owner`; `roles/billing.costsManager` on the billing account; the 14 APIs the
infrastructure needs). `secret_name` must come from the argument or the profile.

The task runs in the `factory-setup` environment, which mounts no Claude or Codex credential and no project
volume.

## What you see

1. A `gcp-login-a<attempt>-<n>` condition whose prompt has an **Open the Google sign-in page**
   link. Open it, sign in as an owner of the project (and a billing admin if the task creates
   the project or grants billing roles), approve, and Google's page shows a code.
2. Signal the condition with that code. A wrong or expired code opens a fresh link, up to
   `attempts` (3) times; `/abort` gives up. No answer within `max_wait_s` (900) counts as a
   failed attempt.
3. The run report lists each step as it completes, and the task returns a summary: project,
   service account, roles granted, APIs enabled, the `PREEMPTIBLE_NVIDIA_L4_GPUS` quota in the
   region (reported only, never requested), and the secret name.

## What it does

With your token, in memory only: create the project and link billing (or check that it exists
and is linked; a project linked to a different billing account is an error, never moved); enable
the APIs; create `factory-infra@<project>.iam.gserviceaccount.com`; grant it the roles on the
project and `billing_roles` on the linked billing account (discovered from the project when
`billing_account` is omitted); create a key, store it as the secret, then retire only the previous
key recorded for that secret. If storing the secret fails,
the new key is deleted again. Last, your token is revoked at `oauth2.googleapis.com/revoke`.

Every step is idempotent. If one fails, the error names it and lists what was already in place;
run it again.

## Sign-in method and security

A direct OAuth 2.0 authorization-code flow with PKCE, using the Cloud SDK's published
installed-app client and the `https://sdk.cloud.google.com/authcode.html` redirect, which just
shows the code. `gcloud auth login --no-launch-browser` does the same thing, but would need the
SDK in the image (or downloaded at run time), screen-scraping its prompt, and it keeps your
credential in its own store on disk. Google's out-of-band redirect is retired and is not used.

- Scope: `cloud-platform`. Project creation, service enabling and IAM policy on a project and a
  billing account span Resource Manager, Service Usage, IAM and Billing; the narrower scopes do
  not cover them.
- No offline access is requested. Google may still issue a refresh token for this installed-app
  client; tokens stay only in memory. The access token (about an hour) is never written to disk,
  logged or stored. Revocation at the end, success or failure, revokes the whole grant.
- The only stored credential is the service-account key.
- Old keys: Flyte cannot return secret values. A project label named `factory-key-` followed
  by the first 32 hex characters of the secret name's SHA-256 records that secret's key ID.
  After storing the replacement, the task updates the record and deletes only its predecessor.
  Other keys and project labels are preserved. A secret without a record leaves existing keys
  alone, including keys from older versions of this task; review and retire those manually.
  Reserve these labels for this task, use distinct secret names for distinct credentials on
  the same GCP project, and run one bootstrap at a time per project. A failed record update or
  deletion can leave an extra key to retire manually; the newly stored key remains usable.
- **When the project is finished, delete the key and the service account**
  (`gcloud iam service-accounts delete factory-infra@<project>.iam.gserviceaccount.com`) and the
  Flyte secret.

## Service-account key creation blocked by organisation policy

Organisations created on or after May 3, 2024 enforce
`iam.disableServiceAccountKeyCreation` by default. Key creation then fails at the `key` step
with HTTP 400; the Google error identifies the constraint or says key creation is disabled.
This is an organisation policy, not propagation lag, so retrying cannot fix it. Ask an
organisation admin to exempt this project from the constraint, or use Workload Identity
Federation instead of this key-based bootstrap. See Google's
[service-account key guidance](https://cloud.google.com/iam/docs/keys-create-delete).
