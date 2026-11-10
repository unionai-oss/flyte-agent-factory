# Credential grants

How to give one project a credential that nothing else in the factory can use, for a time you choose. The first
use is AWS access for installing and testing a Marketplace offering in your own account.

## In short

1. The project asks: `credential-grant request aws-marketplace --hours 4 --why "install the offering and test it"`.
   You get a question (`ask-a<attempt>-NNN`) with a link.
2. You open the link (signed in to Union), paste a **short-lived, scoped** AWS credential, pick when access ends,
   and press **Seal**. The page encrypts the credential in your browser to that project's key and shows a block of
   sealed text. Nothing is sent anywhere.
3. You copy the sealed text back as the answer to the question (in chat: "answer ask-a0-007 with <the text>").
   Answer `no` to refuse; nothing else is ever accepted as a credential. Words, quotes or backticks around the
   sealed text, or line breaks inside it, do no harm. A sealed text works once and for a day: seal a new one to
   give the credential again. Never paste the raw credential: one sent as the answer or to the project's inbox,
   with its `export` names or as bare values, is neither used nor passed on, and you should end it in AWS.
4. The project uses it (`credential-grant exec aws-marketplace -- aws ...`) until the time you chose, until you
   revoke it, or until the project ends, whichever is first. Then it is gone, and the project is told.

| You want to | Do |
|---|---|
| give a credential the project asked for | open the link in its question, seal, paste the sealed text as the answer |
| give one it did not ask for, or refresh one | `factory.credential_link(name=<project>, credential=<name>, hours=<n>)`, open the link, paste the sealed text into the project's `direct-` inbox |
| revoke | send `/revoke-credential <name>` (or `/revoke-credential` for all) to the project's `direct-` inbox |
| see who holds what, until when | `factory.credentials` (every live project) or `factory.status` (one) |
| seal without a browser | `python -m flyte_agent_factory._grants seal --link '<link>'` (reads the credential without echo) |

## Which credential to give (blast radius)

Give the narrowest, shortest-lived credential your provider can mint. The grant's expiry is the factory's limit;
the provider's own expiry is the one that still holds if anything goes wrong.

- **Best: IAM Identity Center.** In the AWS access portal, choose the account and a permission set scoped to the job
  (for a Marketplace offering: CloudFormation, the services its template creates, `aws-marketplace:*Subscri*` /
  `ViewSubscriptions` as needed), then *Access keys*, and copy the block of `export AWS_...` lines. These expire in
  1-12 hours on AWS's side, whatever the factory does.
- **Good: a scoped role.** `aws sts assume-role --role-arn arn:aws:iam::<account>:role/<marketplace-test> \
  --role-session-name factory --duration-seconds 14400` and paste the JSON it prints. The role's policy is the
  blast radius; its `Expiration` caps the grant.
- **Last resort: `aws sts get-session-token --duration-seconds 14400`** from an IAM user, which turns that user's
  long-lived key into a temporary one with the same rights.
- **Refused: a long-lived access key** (`AKIA...`, with or without a session token). The page and the project both
  reject it. Never paste root or personal long-lived keys.

Formats accepted: the portal's `export`/`SET`/`$Env:` lines, a credentials-file `[profile]` block, the JSON of
`aws sts assume-role`/`get-session-token` (`{"Credentials": {...}}`) and of `aws configure export-credentials`.

## What the project can do with it

The supervisor serves the credential to its own session on `127.0.0.1` only, using the AWS container-credentials
protocol, so the AWS CLI, boto3, Terraform and the Go SDK pick it up and refresh it without the value ever being in
an environment variable or a file.

```sh
credential-grant request aws-marketplace --hours 4 --why "..."   # asks you; waits (--timeout, default 1 hour)
credential-grant list                                  # names, kinds, expiry, state; never values
credential-grant exec aws-marketplace -- aws sts get-caller-identity
credential-grant exec aws-marketplace -- terraform apply
credential-grant revoke aws-marketplace                # done early: give it back
```

`request` exits 0 only when the credential is granted; a refusal, a wrong or damaged sealed text, or no answer in
time exits non-zero (1 refused or damaged, 3 no answer) with the reason (never a value). `exec` exits 2 if the
grant is missing or ended and 127 if the command cannot be run. The project is also told, in its session, when a grant
starts, is about to expire and ends, so it need not poll.

`exec` runs one command with the endpoint configured and every other AWS credential source (static keys, a
profile, the pod's own web identity) removed from that command's environment, so the command uses the grant or
nothing. In-pod work the project starts (its in-process sub-agents, its scripts and tests) can use it the same
way. **Child actions, delegate workers and sub-agent pods cannot**: they run in other pods, and every channel
into another pod (task inputs, Flyte secrets, the workspace snapshot) is readable by others with access to the
Union project.

## Design

### Requirements, in your order

1. Nothing else sees it: one project and the work it starts; not other projects, the bench, reviewers, chat, the
   deployer; never in logs, reports, timelines, pull requests, commits or review output.
2. It expires: at your time and at project end; gone afterwards; the project is told.
3. Easy: a link you paste into, no chat or shell history; give, refresh and revoke are one action each, no engine
   change or deploy.

### The decision: a sealed envelope to the project's in-memory key

- **Key.** Each project's supervisor generates an ECDH P-256 key pair when it starts. The private key exists only in
  that (non-dumpable) supervisor process: never serialised, never checkpointed. The public key and its id are
  published in the project's report, and go into the links the project hands out.
- **Seal.** The page (`/flyte-mcp/grant` on the factory-mcp app, already behind Union sign-in) is static: the
  public key, project, credential name and suggested hours ride in the URL fragment, which browsers never send to
  a server. It makes no network requests (`connect-src 'none'`). It seals the credential with WebCrypto and shows
  the result.
- **Return.** You paste the sealed text where you already answer the project: its question, or its inbox. Chat,
  Union's condition store and the run's history see only ciphertext, which nothing but that one process can open.
- **Hold.** The supervisor opens it, checks it (AWS session credential, not a long-lived key), and keeps it in
  memory until `min(your expiry, the credential's own Expiration, 7 days)`. It is served to the session over a
  loopback endpoint guarded by a per-start bearer token.
- **End.** At expiry, revocation or supervisor exit the value is dropped and overwritten, the endpoint answers
  "ended", and the session gets an inbox message saying so. Expiry is checked on every read, not only on the
  supervisor's 5-second tick: a grant past its time is never listed as active or started by `exec`. A restart loses the private key, so a blob sealed
  before it can never be opened again: "nothing left behind" holds cryptographically, not by clean-up alone.

Envelope format (`fgc1`), so the page, the Python store and the CLI fallback agree exactly:

```
sealed  = "fgc1." + base64url_nopad(JSON {"v": 1, "kid": kid, "epk": b64u(65-byte uncompressed P-256 point),
                                          "iv": b64u(12 random bytes), "ct": b64u(AES-GCM ciphertext+tag)})
kid     = base64url_nopad(SHA-256(raw public key))[:16]
secret  = ECDH(ephemeral private, project public)            # the 32-byte x coordinate
key     = HKDF-SHA256(ikm=secret, salt=b"", info=b"flyte-agent-factory credential grant v1", length=32)
aad     = UTF-8("fgc1|" + kid + "|" + project)
plain   = JSON {"name": str, "kind": "aws", "expires_at": int epoch seconds, "sealed_at": int, "value": str,
                "region": str (may be "")}
link    = <factory-mcp public URL>/flyte-mcp/grant#v=1&p=<project>&n=<name>&k=<b64u public key>&kid=<kid>&h=<hours>
```

### Options considered and rejected

- **A Flyte secret the owner writes and deletes (the fallback you offered).** Flyte secrets are scoped to the Union
  project and domain, which every factory project shares: any session holds a Union API key and can submit a child
  action that mounts any secret by name, so it is not one project's. A new secret also reaches a project only
  through a redeploy or new pod (the profile's `AGENT_FACTORY_SECRETS`), and nothing deletes it when the work ends
  unless you remember to. It fails requirements 1 and 2 and the "no deploy" part of 3.
- **Pasting the credential into chat or an `ask-human` answer.** The value would sit in the chat history, Union's
  condition store and the run's timeline, readable by anyone with access to the project.
- **A page that posts the credential to a factory app which forwards it.** It works, but puts the plaintext through
  a shared pod and its logs; sealing in the browser means no server ever holds it. "Copy it back" is the flow you
  said you like.
- **RSA-OAEP alone.** A 3072-bit RSA key encrypts at most 318 bytes; an AWS session token is often longer. Hybrid
  ECDH + AES-GCM is the standard construction and both WebCrypto and `cryptography` have it built in.
- **Handing the credential to child actions.** Task inputs and Flyte secrets are readable outside the project, so
  this is not offered (see above).

### Workload identity (access without any stored secret)

Checked in a project pod on 2026-11-06: the pod has `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE`, that is EKS
IAM Roles for Service Accounts. Your account could trust that role (or the cluster's OIDC provider) to assume a
Marketplace-testing role with no stored secret at all. It is **not used**, because today it is not per-project:
the identity token names the Kubernetes service account, which every factory pod in the namespace shares, so every
project (and the bench and reviewers) could assume the role. It becomes the better option if the Union tenant
gives each run its own service account or an OIDC token with the run in its claims (`sub` per project); the trust
policy would then name one project. Until then, `credential-grant exec` deliberately removes the pod's web-identity
variables from the command it runs, so a grant is never silently replaced by the pod's own role.

### Residual exposure

The session must be able to use the credential, so it can deliberately read it from its loopback endpoint (as it
can read the Union API key it holds today). The instructions forbid printing or writing it; every published path
(the timeline, the report, the bridge, the session log, and a pull request's title, body and comments) redacts the
live values and AWS credential shapes; a pre-push hook refuses a push whose commits or commit messages hold a
credential shape or a live value, and refuses one it cannot fully check (the grant server does not answer, a
live grant cannot be read), so it fails closed (`git push --no-verify` or editing `.git/hooks` skips it); and the
provider-side expiry bounds the damage of a leak. The sealed text in a condition is harmless once the project that could open it has ended.

## Live check after deploy

1. Launch a project with a brief that installs your Marketplace offering in a test account.
2. When it asks, give it an Identity Center credential for a scoped permission set, with a 1-hour expiry.
3. Meanwhile, in a second live project, run `credential-grant list` (empty) and
   `credential-grant exec aws-marketplace -- aws sts get-caller-identity` (refused: no such grant), and check its
   environment and workspace hold nothing.
4. Watch the first project install, test and redeploy; `factory.credentials` shows the grant and its expiry.
5. After the hour: the first project's timeline shows the grant ended, `credential-grant exec` is refused, and
   `factory.credentials` shows it as `expired`.
