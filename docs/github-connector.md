# GitHub from chat: the official GitHub MCP server

The chat operator ([chat-operator.md](chat-operator.md)) manages pull requests, issues and the Projects (v2) board
through GitHub's official remote MCP server, added as a claude.ai custom connector (or, in Claude Code, with
`claude mcp add`, as in [chat-operator.md](chat-operator.md#3-connect-github)). Factory sessions
do not use it; they keep their own scoped credentials.

## Why this and not the built-in GitHub integration

- Claude's GitHub integration (the Claude GitHub App) is scoped to repositories by design. Cloud
  sessions can call `repos/{owner}/{repo}/...` REST paths only, so they cannot reach a Projects (v2)
  board, which is owned by a user or organization (`/users/<login>/projectsV2/...`), not by a repo.
  Linking a board to a repo does not change that.
- A GitHub App cannot read or write user-owned Projects (v2) boards at all: there is no account-level
  Projects permission, only an organization one. An **OAuth App** acts as the user and can.

## Setup

1. **Register a GitHub OAuth App** (GitHub, Settings, Developer settings, OAuth Apps, New OAuth App).
   - Homepage URL: anything you own.
   - Authorization callback URL: `https://claude.ai/api/mcp/auth_callback` for the hosted Claude apps,
     as specified in [Anthropic's authentication guide](https://claude.com/docs/connectors/building/authentication#callback-urls).
   - Generate a client secret. Keep the client ID and secret for step 2; enter secrets only in the
     connector settings, never in chat.
2. **Add a claude.ai custom connector.** For an individual plan, open Customize > Connectors >
   **+ Add** > Add custom connector. For Team or Enterprise, an authorized owner adds it through
   Organization settings > Connectors > Add > Custom > Web
   ([Anthropic setup guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)).
   - URL: `https://api.githubcopilot.com/mcp/x/all`, which enables every toolset, including `projects`.
     Alternatively, use `https://api.githubcopilot.com/mcp/` with Request headers
     `X-MCP-Toolsets: context,repos,issues,pull_requests,users,projects`. An explicit list selects the
     toolsets; unknown names are ignored. These are GitHub's documented
     [remote server URL and header options](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md).
   - After entering the URL, click Continue and review authentication. Select **Sign in now**, then
     **Use your own OAuth client** and enter the OAuth App's client ID and secret. In the single-screen
     dialog, use Advanced settings for those credentials
     ([Anthropic authentication settings](https://claude.com/docs/connectors/custom/add-unlisted#choose-authentication-settings)).
     GitHub requires the host to obtain a token using its own registered app or supply a PAT; it does
     not register OAuth clients automatically
     ([GitHub host integration guide](https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md)).
3. Save with Add, connect and approve GitHub's consent. Enable the connector from **+ > Connectors**
   in the conversation. Check that `projects_list`, `projects_get` and `projects_write` are listed.

**PAT alternative.** Choose **No sign-in** and add a required `authorization` request header whose value
is `Bearer ` followed by your GitHub PAT. Enter it only in the connector settings, never in chat or a
shell command. Do not combine an `authorization` request header with OAuth.
[Anthropic's request-header guide](https://claude.com/docs/connectors/custom/add-unlisted#authenticate-with-request-headers)
still describes this as a beta limited to some organizations; it also notes that custom header names
need approval. If Request headers is missing, use the OAuth setup above with the `/mcp/x/all` URL,
or the Claude Code setup in [chat-operator.md](chat-operator.md#3-connect-github). If `X-MCP-Toolsets`
is rejected, use `/mcp/x/all` instead.

## Scope and risk

- An OAuth App token carries classic scopes (for example `repo`, `project`, `read:org`), broader than
  a fine-grained PAT. Keep this credential for the human-supervised chat operator; never give it to
  an unattended factory session. Revoke it from GitHub, Settings, Applications, Authorized OAuth Apps.
  GitHub can also revoke tokens automatically, including after a year of inactivity
  ([token expiration and revocation](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/token-expiration-and-revocation)).
- For a fine-grained PAT, select only the repositories chat operates on and an expiry. Grant
  **Contents: Read and write** for merging and **Pull requests: Read and write** for PR operations
  ([GitHub PR permissions](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)). To file and edit
  issues, also grant **Issues: Read and write**: Contents and Pull requests write alone do not allow
  issue creation ([GitHub issue permissions](https://docs.github.com/en/rest/issues/issues#create-an-issue)).
- **Board access is separate.** Repository permissions do not grant Projects (v2) access. For a user-owned
  board, use a classic PAT with `project` for board writes and `repo` for private repository access, as
  in [profiles/README.md](../profiles/README.md#projects-v2-board)
  ([GitHub Projects authentication](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects#authentication)).
  An organization-owned board needs the separate organization **Projects: Read and write** permission
  when using a fine-grained PAT; keep the repository permissions above for issues and PRs
  ([GitHub organization Projects permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens#organization-permissions-for-projects)).

## What chat uses it for

- Board: list fields and items, set Status (`Backlog` / `Ready` / ...), add items, bulk-update up to
  50 items per call (`projects_write` method `update_project_items`).
- Issues: create, edit, label, sub-issues and comments. Repo-scoped REST through the session's
  built-in GitHub access still works for these; the connector adds the board.
