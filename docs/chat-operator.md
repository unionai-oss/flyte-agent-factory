# The chat operator

The default way to use the factory is through a chat agent: Claude, with the factory MCP connected, the
`agent-factory` skill installed and GitHub's MCP server as a second connector. You talk to the operator; the
operator talks to the factory. The terminal (`ops/operate.py`), labelled-issue intake and direct `flyte run` reach
the same tasks and are there for scripts and automation.

## Why an operator between you and the factory

- **It answers the factory so you don't have to.** A session asks questions (`ask-` conditions) whenever it is
  unsure. The operator answers the engineering ones from what the conversation and the brief already settle, and
  brings you only product choices, tradeoffs, money and anything irreversible. Every question has a timeout and a
  default, so nothing stops while you are away, and the operator tells you which defaults were taken.
- **It holds the credentials the factory doesn't.** The factory pushes code only to the repository it was launched
  on and does not merge. Merging and promoting to another repository need your credentials, which the operator
  holds and uses only when you say so. Deciding what is on the board (Backlog, Ready, order) is yours as well; by
  default the factory sets neither Backlog nor Ready.
- **It helps you write better inputs.** Issues and briefs are problem and requirements documents: what is wrong,
  who needs it, what must be true when it is done. The operator drafts them with you and keeps implementation
  designs out, because working out the design is the factory's discovery stage.
- **It explains what is going on.** Status, the queue, review rounds and their blocking findings, usage meters and
  reserves, and why a project is waiting or stalled, in plain terms rather than run ids and condition names.

## What each side can do

| | The factory, on its own | The operator, on your word |
|---|---|---|
| Code | push to the repository it was launched on, with that repository's deploy key: branch `factory/<name>` with `[github]`, the default branch without it (the `pre-push` hook pins the repository, not the branch; branch protection holds the rest) | merge a pull request, with the GitHub connector |
| Pull requests | open a draft, mark it ready on a pass, post the `factory/verifier` status, through the GitHub App (Contents: read by default, so it cannot merge; the settings below need write added to the App) | review, comment, merge, close |
| Other repositories | pushes code to none: a project that delivers elsewhere leaves `git format-patch` files | promote them with `ops/promote.py`, which needs a shell and your git credentials: an operator with a shell (Claude Code) runs it, otherwise you do |
| Issues and the board | intake comments with `[github]`; with `[board]`, moves a project's card to In flight, In review and Done with the board's own token; by default it never sets Backlog or Ready, by rule rather than for want of access (the settings below change that) | file and edit issues, set Backlog and Ready, order the backlog |
| Deploys | none of its own; with `[deploy] auto` the deployer ships what lands on the engine repository's default branch | with `[deploy] auto`, merging to the engine repository is the deploy, otherwise run `ops/deploy_factory.py` from a shell; through the factory MCP, retry a failed deploy and manage the factory's apps and triggers; engine credentials are stored with local commands, never through chat |
| Projects | ask questions, take defaults on timeout, declare DONE | launch, answer, send direction, stop, cancel, abort |

Some settings widen the factory's reach, all off by default:

- `[loop] merge = "on"` lets the loop merge, close and label a passing pull request on the engine repository.
- `[loop] enabled` runs the daily backlog generator, which files at most five improvement issues a day on the
  engine repository, even with `merge = "dry-run"`. With `[board]` it also puts each on the board: Ready for an
  issue that touches no owner-only files, while fewer than three of its cards are Ready, otherwise Backlog.
- `[planner] admission = "planner"` admits work from the board's Ready column. When it takes over, it also moves
  the card of each queued launch that names an issue to Ready, once. It and `[bench]` write refs and a results
  branch in the direction repository.

The merge, issue and ref writes need write access added to the GitHub App ([profiles/README.md](../profiles/README.md)).
`[board]` mounts a classic token with the `project` and `repo` scopes in project supervisors, so limit that machine
account to the board's repositories. Slot secrets are not an isolation boundary between a session and its own slot,
which is a further reason the GitHub connector's token is never given to a slot.

The operator's Union API key is powerful too: inside the factory's project and domain it can run any task, set and
delete secrets and turn apps and triggers on or off. Give it only the role it needs
([factory-mcp-setup.md](factory-mcp-setup.md#connecting-a-client)).

## Setup

For a new or partial install, start with [guided setup](quickstart.md). The operator can guide setup before
its factory MCP exists; local commands handle engine credentials. For the connections below,
you need a deployed factory first ([quickstart.md](quickstart.md), steps 1 to 5), its `factory-mcp` URL in the
profile's `[mcp] public_url`, and a second `deploy_factory.py` run after setting it (step 5).

### 1. Connect the factory MCP

The server is `<public_url>/flyte-mcp/mcp`; `deploy_factory.py` prints it as `Connector URL:`. It authenticates
with `Authorization: Bearer <token>`, where the token is a Union API key:

```bash
uv run flyte --config "$FLYTE_CONFIG" create api-key --name factory-chat --no-default-policies
```

The command prints the client ID and, once only, a line `export FLYTE_API_KEY="..."`. The bearer value is the
long string between the quotes. A key created with `--no-default-policies` can do nothing until a policy is
assigned: create the `factory-chat` policy (role `contributor`, limited to the factory's project and domain) and
assign it to the client ID with the block in [factory-mcp-setup.md](factory-mcp-setup.md#connecting-a-client).
The key and policy commands need Linux x86_64; on macOS run them on a Linux machine (same page, platform note).
Run the commands locally and enter the key string directly into
your client settings, never the conversation; do not run the `export`. Then add it to your chat client. In
Claude Code, for every directory (`-s user`), first check that the connector is not already there with
`claude mcp list` (or `claude mcp get factory`) and skip the add if it is:

```bash
bash -c '
set +x
trap "unset factory_mcp_key" EXIT
read -r -s -p "Union factory MCP key: " factory_mcp_key || exit 1
[ -n "$factory_mcp_key" ] || exit 1
printf "\n"
claude mcp add -s user --transport http factory "<public_url>/flyte-mcp/mcp" --header "Authorization: Bearer $factory_mcp_key"
'
```

Replace `<public_url>/flyte-mcp/mcp` with the `Connector URL:` line `deploy_factory.py` printed. Run the
prompt/header block in a local terminal, bash or zsh, with session recording disabled. It starts bash
because the non-echo prompt differs between the two shells; if `claude` is only a shell alias, replace `claude` in
the block with its full path (`command -v claude` in a new bash shows whether bash finds it). An empty entry
stops without adding the connector. The variable keeps the literal credential out of shell history and is unset
when that bash exits; Claude Code stores the expanded header in its local MCP configuration.
Keep that configuration private, never commit or read it into chat, and remove the connector there to revoke
local storage. The header is also a local process argument while `claude mcp add` runs.

In claude.ai (web, desktop, mobile) the connector needs request-header authentication, which claude.ai offers as a
beta to a limited set of organizations: Customize, Connectors, Add custom connector (on Team and Enterprise, an
Owner adds it under Organization settings, Connectors), URL `<public_url>/flyte-mcp/mcp`, Authentication **No
sign-in**, and under Request headers `authorization` with the value `Bearer <key>`. If the dialog has no Request
headers section, use Claude Code. Either way leave OAuth out: the stock app serves no RFC 9728 metadata, so a
connector set up for OAuth discovery cannot connect. Check the connection by asking the operator to call `whoami`.

### 2. Install the skill

The skill tells the operator the factory's project, domain, repositories and task names, and how to behave as the
operator (what to answer itself, what to bring you, what never to do without your word). Build it from your
profile and install the zip: in the Claude apps through Settings, Capabilities, Skills; in Claude Code by
unzipping it into `~/.claude/skills/`.

```bash
PYTHONPATH=src uv run python ops/package_skill.py --profile mine
```

The zip is `skills/dist/agent-factory.zip`. Install it in Claude Code with:

```bash
mkdir -p ~/.claude/skills
unzip -o skills/dist/agent-factory.zip -d ~/.claude/skills/
```

Before a profile exists, use `ops/package_skill.py --setup` as described in
[guided setup](quickstart.md). After deployment replace that neutral setup skill with this profile package.
The zip holds deployment-specific values and the guided setup reference; keep it private and untracked.

### 3. Connect GitHub

Add GitHub's official MCP server as a second connector, with the `pull_requests`, `issues` and `projects` toolsets
so the operator can merge, file issues and edit the board. In claude.ai, use your own GitHub OAuth App as in
[github-connector.md](github-connector.md). In Claude Code, a token in a header:

```bash
bash -c '
set +x
trap "unset github_mcp_token" EXIT
read -r -s -p "GitHub MCP token: " github_mcp_token || exit 1
[ -n "$github_mcp_token" ] || exit 1
printf "\n"
claude mcp add -s user --transport http github https://api.githubcopilot.com/mcp/ \
  --header "Authorization: Bearer $github_mcp_token" --header "X-MCP-Toolsets: context,repos,issues,pull_requests,users,projects"
'
```

Use the same local prompt precautions, and the same `claude mcp list` check, as for the factory header above.

For a board your user account owns, use a classic token with the `repo` and `project` scopes. For an
organization-owned board, a fine-grained token needs the organization **Projects: Read and write** permission on
top of Contents, Pull requests and Issues write on your repositories; repository permissions do not grant board
access. Without a board, those repository permissions are enough
([github-connector.md](github-connector.md#scope-and-risk)).
It is yours and stays in the client's credential settings; do not paste it into the conversation.

### 4. First launch

Ask in plain words, for example:

> Launch a project called `csv-export` on the default repo: add a CSV export to the report page. Done when
> `GET /report.csv` returns the same rows as the page and the existing tests pass.

The operator turns that into a brief, calls `factory.launch` and replies with the run URL, or the project's
position in the queue. From then on ask it how the project is doing, what it is waiting on, or to pass something
on. When review passes it tells you the pull request is ready; it merges when you say so.

## What to ask it

| You want to | Say |
|---|---|
| write up a problem | "Help me write an issue for ...": it drafts the problem, requirements and acceptance, not a design |
| start or resume work | "Launch `<name>` with this brief", or "Pick `<name>` up again with ..." |
| know what is happening | "How is `<name>` doing?", "What's running and what's queued?", "Why is it waiting?" |
| steer | "Tell `<name>` to ...", "Answer its question: ...", "Stop `<name>`" |
| deliver | "Merge the PR for `<name>`", "Promote `<name>` to `<repo>`" (with a shell) |
| manage spend | "What are the meters at?", "Hold back 0.5% of each window per hour to its reset" (`factory.set_reserve`, `k = 0.5`) |

The tools behind these are in [architecture.md](architecture.md#the-controls-on-the-mcp) and the day-to-day detail
in [operating.md](operating.md).
