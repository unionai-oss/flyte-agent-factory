# Contributing

## Set up

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.12 or later (`requires-python = ">=3.12"`; `uv sync`
downloads 3.12 if no suitable Python is installed). Ruff, mypy and the images target 3.12.

```bash
git clone <your fork> && cd <the clone>
uv sync
```

No cluster, credentials or Claude subscription are needed to run the suite. `uv sync` and the first
`uv run --with ruff` in the checks download packages, so those need network access.

## Check your change

```bash
bash factory-improvement/checks.sh
```

It runs the whole test suite, `ruff check` and `ruff format --check` over `src tests ops`, and must exit 0 before a
change is proposed. The name is this deployment's: anywhere else, the command in the README's "Develop" section does
the same checks in one lane (`checks.sh` splits the tests into a `slow` and a not-`slow` lane that run in parallel).

To see how the DONE gate would judge a tree, run the standards check locally:

```bash
PYTHONPATH=src uv run python -m flyte_agent_factory._gates standards <dir>
```

It runs the mechanically checked rules of a standards directory over `<dir>` (formatters, linters, type checkers
and the plain pytest run, and any `lint`, `forbid` or `script` rule), and the built-in checks the rules map
(`factory:language-summary`, `factory:module-size`, ...), plus `factory:suppressions`, all judged on the whole
tree. The standards are the directory after `--standards <sdir>` (default: `<dir>/standards` if it exists; with
none, the command says that no standards are configured and checks nothing). The engine has no standards of its
own: in CI the engine's tree is checked against the workspace template's example set,
`src/flyte_agent_factory/_assets/workspace_template/standards`. See [standards.md](standards.md). It skips the checks that need a
project's history or a workspace: `attribution`, `scope`, the fail-to-pass comparison against the starting tree,
`uncommitted`, `checks.sh`, the target checks for a delivery project, and owner waivers. A pass is necessary for DONE,
not sufficient.

## Owner waivers

When a gate check fails and the owner decides to accept it for now (a `factory:fail-to-pass` test timeout, say), the owner records
that in `waivers.toml` at the root of the workspace the gate runs in. The factory cannot write it: any commit that adds or
changes `waivers.toml` is a scope violation, even under `May edit: **`.

The gate reads the file from the default branch of the project's repository URL (the supervisor's own setting),
fetched into a scratch repository outside the workspace with no workspace or user git config and no replace refs.
Nothing in the workspace's `.git` (remotes, `origin/HEAD`, refs, config) can change what is read, and no program
named in it runs. If that copy cannot be read, no waiver applies and `factory:waivers` says so (it does not block).
Without a repository URL (tests, local fixtures; a deployment always has one) the file is read from the round's base
commit as the supervisor recorded it in memory when the project was created. After a supervisor restart that record
is gone, and the only one left, `.claude-flyte/<name>/base-commit`, is written by the session: in that case waivers
are only as trustworthy as that file.

```toml
[[waiver]]
check = "factory:fail-to-pass"   # a gate check such as "factory:module-size", or your rule ID mapped to it
project = "factory-improvement"   # the project name, or "*" for every project
expires = 2026-11-30              # a TOML date; the waiver holds through the end of that day (UTC)
link = "https://github.com/<owner>/<repo>/issues/123#issuecomment-1"  # the owner's decision; required, https
reason = "one line"               # optional
```

A FAIL covered by an unexpired waiver is recorded as `WAIVED` (output starts `waived by owner until <date>: <link>`)
and does not block DONE. After `expires` the check fails again and says `waiver expired <date>`. A malformed file
fails the `factory:waivers` check, naming the entry. A waiver covers a `FAIL` only: a check whose tool is
unavailable still blocks DONE. The `scope` and `factory:waivers` checks cannot be waived.

## Tests first

Write the test that fails for the bug or the missing behaviour, watch it fail, then make it pass. A change to behaviour
without a test that would have caught its absence is not finished. Never weaken, delete or rename an existing test to
get a change through; if a test is wrong, say why in the pull request.

Keep deployment-specific values (accounts, hosts, buckets, secret names, run ids) out of the code, docs and tests. They
belong in a deployment profile; `profiles/example.toml` shows the shape. The literal guard in
`tests/test_profile.py` fails the suite when one slips in.

## Run the tests

```bash
PYTHONPATH=src uv run python -m pytest tests -q && uv run ruff check src tests ops && uv run ruff format --check src tests ops     # the whole suite and ruff
PYTHONPATH=src uv run python -m pytest tests/test_e2e_session.py -v -s      # one scripted session end to end, with the task log it would produce
```

No cluster and no Claude subscription needed. They cover the parts
that are awkward to debug once deployed: the pty runner against a fake TUI
(first-run prompts, typed input, session-URL scraping), the `ask-human` file
protocol driven as a real subprocess, snapshot archives (git state survives,
`node_modules` doesn't, a stray socket doesn't abort the snapshot), the Remote
Control preflight, condition naming across retries, and the job layer (specs
survive a restart as files, each is claimed exactly once, concurrency is capped,
a failed job is recorded rather than crashing the project).

`PYTHONPATH=src uv run python -m pytest tests/test_e2e_session.py -v -s` is the closest thing to launching a run: `tests/fake_claude.py`
stands in for the `claude` CLI on a real pty and follows a script, while the
real `ClaudeProcess`, `Bridge`, `flyte-note` wrapper and `_pump` supervise it --
premature `DONE`, the preflight bounce, a failed and a passed review, a silent
session being nudged and declared parked. Only the cloud reviewers are stubbed.
`tests/test_drift.py` keeps the memory text and the README honest against the code they describe;
`tests/test_docs_match_code.py` does the same for the names, settings and links in the docs.

What they can't cover is the real Claude Code TUI, which needs a token and a
cluster. Expect the first deploy to need a round of
`PromptRule` tuning.

## Known rough edges

- **Project state is `Dir` + `Checkpoint`, not a Volume.** A Union `Volume` is
  the nicer design — a real mounted filesystem, nothing to archive — but it
  mounts through the `smarter-devices/fuse` device, and on this cluster all 13
  fuse-capable nodes are at capacity with no headroom to scale, so those pods
  never schedule. `Dir`/`Checkpoint` need nothing from the node, and work on OSS
  Flyte, which a Union-only plugin does not. The cost: `Checkpoint.save`
  archives the whole state root each tick, so checkpoint cost scales with
  workspace size. Keep datasets in child-action outputs.
- **`child.attach()` leans on two private APIs.** `flyte.load_interactive_ctx`
  is public but documented for debuggers; `create_controller` and
  `write_interactive_run_context` are not. If a future SDK moves them this
  breaks loudly and immediately, and it is ~20 lines to re-pin — the trade
  against maintaining a bridge forever.
- **The pty prompt answering is best-effort.** Claude Code asks
  `Trust <directory>?` on a first start, and `bypassPermissions` shows a two-option menu whose default is
  *"No, exit"* — a bare Enter quits. `_process.default_prompt_rules` answers
  each by pattern, and there is a test asserting no rule sends a blind Enter.
  The wording is not contractual; if a first start hangs, read the run logs for
  an unanswered prompt and add a rule.
- **Typed input is a TUI, not an API.** Direction is delivered by typing into
  the session's prompt, which is why multi-line content is written to a file and
  sent as a one-line pointer.
- **Containers run unprivileged.** Nothing may assume it can write outside
  `$HOME`. This caused three separate failures — the agent's state paths, the
  `claude` install location (`/root/.local/bin`, unreadable by the task user),
  and the browser app's `/var/lib` profile directory, which crash-looped the
  app on its first line under `set -e`.
- **Package data must ship in the image.** Flyte's code bundle carries Python
  modules, not package data — role definitions read from the installed package
  were silently absent in the container. Anything in `_assets/` goes in via
  `with_source_folder`.
- **Trusted Devices**, if your org requires it, needs a `/login` on the machine
  running the session. A container is not a device you can enrol, so such an org cannot run the factory.

## Sign your commits (DCO)

Every commit a human makes needs a `Signed-off-by` line, certifying the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "Describe the change"
```

which adds `Signed-off-by: Your Name <you@example.org>` using your git `user.name` and `user.email`.

The certification is personal, so it applies to commits a human makes. Factory sessions do not sign off: the
operator's promotion does, with the owner's git identity, when it delivers a project
([Promotion](architecture-repos.md#promotion)).

## Reporting security problems

Not in an issue. See [security.md](security.md).
