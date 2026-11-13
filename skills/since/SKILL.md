---
name: since
description: Summarize everything that happened in the Flyte agent factory since the person's last message in this chat, or since the last check-in recorded in the workspace's operator/STATE.md in a fresh chat, then list the person's action items. Use for /since, "what happened", "catch me up".
---

# Since

Work out the start time: the time of the person's previous message in this chat. In a fresh chat, use the last
check-in time in the workspace's `operator/STATE.md` (read it with the GitHub file tools); if there is none, or the
GitHub tools are not connected, say so and cover the last day.

Gather only from live sources, using the factory MCP's project and domain from its server instructions on every call:

- `factory.queue_list`: live projects, waiting launches, recent outcomes.
- `factory.status` for each project that changed: phase, review round, blocking findings, open questions, PR URL.
- Open pull requests and their CI on the workspace and engine repos, when the GitHub tools are connected; otherwise
  say they were not checked.
- The newest `factory-deployer.deploy_if_changed` run, when something was merged.

Reply concisely, in bullets, with no em dashes:

- **Milestones** since the start time: launched, passed review, PR opened, merged, deployed, failed, stopped. One
  line each, with the run or PR link.
- **Now**: what is running, queued or waiting, and on whom. A project that declared done is "declared done, review
  round N in progress" until its timeline says it passed review.
- **Action items for you**: questions only the person can answer, merges awaiting their word, credentials or spend
  to approve. Say "nothing needs you" when that is true.

Do not change anything: this command only reads. If the factory tools do not answer, say that and suggest
`/agent-factory`.
