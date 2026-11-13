# Your own GitHub App

The factory acts on GitHub as a GitHub App that **you** register and own. There is no shared public App: its private
key would let anyone holding it act on every repository it is installed on, so each installation has its own. The
App is the default in [guided setup](quickstart.md) and optional: setup works without it, with the losses listed
[below](#what-works-without-it).

The deploy key into your `agent-workspace` repository (one per repository, write access) is the
only GitHub credential setup requires; everything the App adds is optional. "The App by default plus a deploy key" is the
credential model this guide assumes. It is still open: issue #179 decides the final model. The deploy key is what a
session pushes with. The App's key is stored only as a Flyte secret and mounted in the factory and deployer
tasks, never in a project slot's environment or a chat. It cannot write contents: pushes are the deploy key's job.

## What it enables

| With the App | Without it |
|---|---|
| The deployer reads CI statuses and check-runs, so it skips a commit whose checks are failing or pending | The deployer cannot judge a commit by its checks; auto-deploy is unavailable |
| Each project works on `factory/<name>` and ends in a draft pull request, marked ready on a pass, with a `factory/verifier` status | Projects push the workspace's default branch and open no pull request; the verifier status is never posted |
| `[deploy] auto` and `[direction]`, written to the profile only when the App exists: a merge to the workspace redeploys the factory (a merge to the public engine does so too, once its CI is green: the deployer reads it anonymously, see [Deploying the public engine](#deploying-the-public-engine)) | No `[direction]` and no `auto` in the profile; every deploy is a hand deploy (`ops/deploy_work.py`, `ops/deploy_factory.py`) |
| Issue intake: a maintainer's issue with the profile's `[github] intake_label` becomes a project, with status comments | No intake; launch from chat or `ops/operate.py` |

The chat operator's GitHub connector is a separate credential and is not replaced by the App
([github-connector.md](github-connector.md)).

## Register it with the bootstrap (default)

`ops/bootstrap.py` uses GitHub's [manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest):

1. It serves one page on `http://127.0.0.1:<port>` that posts the App's manifest (name, permissions and events
   below) to GitHub, and opens it in your browser. You confirm the name and click **Create GitHub App**.
2. GitHub redirects back to that local page with a one-time code. The bootstrap exchanges the code for the App's
   id and private key and stores the key straight into a Flyte secret. The key is not printed or written to disk.
3. It prints the installation URL for the workspace and waits until the App is installed on it, then records
   `[github] app_id` and `private_key_secret` in your `profile.toml`.

The profile names the App only once the installation is confirmed. Skip the App with `--no-app`; the bootstrap then
says what is lost, and a rerun without that flag registers the App later. Installing an App on an organization's
repository needs an organization owner. If you are not one, run the bootstrap again with `--no-app` (or ask an
owner to approve the installation request): the failed install left nothing in `profile.toml`, and the ruleset is
updated to let the deploy key push. Going the other way, from an App to `--no-app`, removes `[github] app_id` and
`private_key_secret`, `[deploy] auto` and `[direction]` from the profile and says so, since you chose to go without.

GitHub creates the App when you click **Create GitHub App** and hands its private key over once, in step 2. The
bootstrap records the App's id and slug in the clone's local git config as soon as it has them, before it stores the
key, so a run that stops after that resumes with the same App and installs it by its own slug. A run that stops
before the key is stored (or before the exchange) leaves an App nobody has the key to, and its name is taken: the
next run says so, names the App and gives its settings page (`https://github.com/settings/apps/<slug>/advanced`, or
the organization's), and does not register a second one. Delete the App there, run the command again, and it
registers a new one.

## Register it by hand

Settings > Developer settings > GitHub Apps > **New GitHub App** (for an organization: the organization's settings).

- **Name:** anything unique, for example `agent-factory-<you>`. **Homepage URL:** the engine repository.
- **Webhook:** leave it inactive unless you use signed push wake-ups and issue intake
  ([deploy-webhook.md](deploy-webhook.md)); then set the URL and a secret named by `[github] webhook_secret`.
- **Where can it be installed:** only on this account.
- **Repository permissions:**

  | Permission | Access |
  |---|---|
  | Contents | read |
  | Metadata | read |
  | Pull requests | write |
  | Commit statuses | write |
  | Checks | read |
  | Issues | write |

  Contents is read-only on purpose. Issues write is for intake status comments and the `[loop]` labels. The
  settings that merge or write refs (`[loop] merge`, `[planner]`, `[bench]`) need more; see
  [profiles/README.md](../profiles/README.md).
- **Subscribe to events** (only with a webhook): Push, Issues, Issue comment, Pull request.

Create it, note the **App ID**, then **Generate a private key** (a `.pem` download). Store the key as a Flyte secret
from the file and delete the file; never paste it into a chat:

```bash
uv run flyte create secret agent_factory_github_app_key --project <project> --domain <domain> --from-file ~/Downloads/your-app.private-key.pem
```

Install it: the App's page > **Install App** > your account > **Only select repositories** > the workspace only. You
cannot install it on `unionai-oss/flyte-agent-factory`, and nothing asks you to; see
[Deploying the public engine](#deploying-the-public-engine). Then add the table to the workspace's `profile.toml`:

```toml
[github]
app_id = 123456
private_key_secret = "agent_factory_github_app_key"
```

and redeploy with `--redeploy` ([above](#deploying-the-public-engine)).

## Deploying the public engine

The deployer deploys the default-branch head of `[deploy] repo` (default `[repo] factory`, the public
`unionai-oss/flyte-agent-factory`) once that head's CI passed. Your App is installed on your workspace only, so for a
repository it is not installed on the deployer reads anonymously (`git ls-remote`, a clone by the public URL), polls
instead of waiting for a webhook (`[deploy] webhook = false`) and keeps its deploy lock in `[direction] repo`, the
workspace. With neither the App nor `[direction]` the profile is refused, and the error names both options. Do not
try to install the App on `unionai-oss`.
To deploy the engine's `main` at once instead of waiting for a poll, run the bootstrap again with `--redeploy` (the
same values as the first time; it deploys the work slots and the factory, with a one-off deployment key that it
deletes afterwards):

```bash
d=~/flyte-agent-factory; u=https://github.com/unionai-oss/flyte-agent-factory; { [ -d "$d/.git" ] || git clone -q "$u" "$d"; } && case "$(git -C "$d" config --get remote.origin.url)" in "$u"|"$u.git") ;; *) echo "$d is not a clone of $u" >&2; false ;; esac && git -C "$d" checkout -q main && git -C "$d" pull -q --ff-only origin main && cd "$d" && uv sync -q --python 3.12 && uv run python ops/bootstrap.py --endpoint flyte.example.com --project my-agents --workspace you/agent-workspace --state-uri s3://my-agent-state/factory/mine --redeploy
```

## What works without it

Everything in [quickstart.md](quickstart.md) up to a first launched project: the workspace, the deploy key, the
factory, the MCP connection and `factory.launch`. Output goes to the workspace's default branch, you review it there,
and you deploy by hand. Without the App the bootstrap writes neither `[direction]` nor `[deploy] auto`, and its `github-app` line reads: "what is lost without the GitHub App: CI visibility (check-runs and statuses are read through the App); draft pull requests and the factory/verifier status; auto-deploy and the [direction] repository; issue intake. The backlog board is kept: it is made with gh and needs no App". Add the App later by rerunning the bootstrap (without `--no-app`); nothing already set up is replaced, and the ruleset's deploy-key bypass is removed.
