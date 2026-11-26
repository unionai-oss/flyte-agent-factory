# Writing your own standards

The factory checks every project against **your** standards: the rules in your workspace's `standards/`
directory. The engine ships no rules of its own. It knows how to run a few kinds of check and how to hand the
rest to its reviewers, and your standards say which rule is checked which way.

The workspace template, shipped in the engine at
`src/flyte_agent_factory/_assets/workspace_template/` (a separate `unionai-oss/flyte-agent-workspace` repository is not published
yet), carries one complete set as the worked example. Copy it,
then rename, rewrite or delete any rule: nothing in the engine depends on its IDs or wording.

## Where the factory reads them

In order, the first that exists:

1. `standards/` at the root of the workspace a project runs in.
2. The directory the deployment profile names in `[standards] dir`, captured when
   the factory is deployed. A profile read from a direction repository uses that repository's `standards/` when
   `dir` is unset; the engine's own `standards/` is never a fallback.
3. Nothing. The project then has no rules: the gates still run the checks that are not standards (scope,
   attribution, uncommitted work, `checks.sh`, a delivery target's CI), and both the deploy and the gate report
   say that no standards are configured.

An absolute `[standards] dir` is used as given. Relative `dir` and `brand` paths are tried beside the actual
profile file first, then against its originating repository root for compatibility. For example, for
`tree/profiles/name.toml`, `../standards` selects `tree/standards/`; `standards` first tries
`tree/profiles/standards/`, then `tree/standards/`. A missing explicit directory fails validation and names the
paths tried. Neither the installed engine checkout nor the process working directory supplies a fallback.
A hand-loaded direction profile identifies its repository root through its matching `[direction] profile`
path suffix, and with `dir` unset uses that root's `standards/` in a session's gates exactly as its deploy
would. A hand-loaded engine profile at `tree/profiles/<name>.toml` identifies `tree` as its root.
When neither layout identifies a root, only the profile directory is known; no filesystem search guesses one.
An engine profile with unset `dir` has no implicit standards. Direction defaults and explicit files are captured
before the fetched checkout is removed, and the bundle travels with the serialized profile.

The factory never writes your standards: a project's change to `standards/` is a scope violation, whatever its
brief allows.

## Files

```
standards/
  README.md            # optional: anything for people; not parsed
  engineering.md       # base files: their names are yours; [standards] include selects which apply
  security.md
  brand.md             # optional: rules for human-facing work only (implicitly `applies: human-facing`)
  ux.md                # optional: any base file may say `applies: human-facing`, as below
  demo.md              # optional: rules for demos (see "Demos")
  languages/
    python.md          # language packs: selected by their `detect:` line, or by [standards] languages
    configs/           # baseline tool configs for projects that have none of their own
      ruff.toml  mypy.ini  tsconfig.json  eslint.config.mjs  prettier.json  tflint.hcl
```

`[standards] include` lists the base files (without `.md`) to apply; the default is `engineering` and
`security`. A language pack starts with a line naming the files that make a project use that language:

```markdown
detect: pyproject.toml, setup.py, **/*.py
```

A base file whose rules are only for what people see or read says so, before its first rule heading:

```markdown
applies: human-facing
```

The key is case-insensitive, like `detect:`, and the line is not part of any rule. The rules of such a file, and
those of `brand.md` (which needs no line), are *human-facing*: `Rule.human_facing`. The factory's prompts then
say, naming the selected files, that their rules apply only to human-facing deliverables (UI, reports, CLI
output read by people, docs sites, demos, slides), and that they are judged with evidence: screenshots at the
stated viewports and a walkthrough of the main task. The session instructions, the contract writer and the
craft reviewer get that sentence; when no selected file is human-facing, nothing is said.

With `[standards] languages = "auto"` (the default) a pack applies when any project file matches; a list of
pack names applies exactly those.

## A rule

Every rule is a second-level heading with an ID and a level, then free text, then the lines the factory reads:

```markdown
## QA-7 MUST
New behaviour comes with a test that fails without it.

Why: A test that never failed proves nothing.

Check: Run the new tests on the starting tree; they must fail there and pass on the work.

Enforced: check factory:fail-to-pass
```

- **ID**: any token of letters, digits, `.`, `_` and `-` that starts with a letter (`QA-7`, `SEC.12`,
  `lint_imports`). IDs must be unique across the selected files. Keep them stable: waivers, reports and
  reviewers' findings name them.
- **Level**: `MUST` or `SHOULD`. A failing `MUST` check blocks DONE; a failing `SHOULD` check is reported and
  does not. Only `MUST` rules run as checks; a `SHOULD` rule's `Enforced:` line is documentation.
- **`Enforced:`** says how the rule is checked: one of the kinds below. Exactly one per rule.
- **`Timeout: <seconds>`** (optional) bounds each command of a `command`, `lint` or `script` rule; default
  300, at most 3600.
- **`Judged: <text>`** (optional) on a mechanically checked rule: the part a tool cannot check, which the
  reviewers judge as well.

## How a rule is enforced

### `Enforced: judgement`: the reviewers

The rule's text goes to the factory's LLM reviewers (the craft judge, and the contract writer for `MUST`
rules). A reviewer that finds the work breaks it names the rule ID and cites the file and line of the work it
applies to. `SHOULD` findings and craft findings are advisory unless the profile's policy makes craft blocking.
Rules of a human-facing file (`brand.md`, or one with `applies: human-facing`) are judged only for
human-facing deliverables.

### `Enforced: check <name>`: a check built into the factory

| Check | What it checks |
|---|---|
| `factory:fail-to-pass` | The project's tests: new or changed behaviour has a test that fails on the starting tree and passes on the work, and no existing test was weakened, deleted or renamed. A demo is exempt. |
| `factory:module-size` | No non-test source module over the size limit grows; one already over it may not grow further. |
| `factory:language-summary` | One line summarising the results of every mechanically checked rule in the selected language packs. |
| `factory:ui-evidence` | Not applicable when the project has no UI; otherwise it runs the command the project declares for the rule in `factory-evidence.toml` (below), and with none it asks for evidence the gate cannot gather itself and reports tool unavailable. |

To declare the command, put `factory-evidence.toml` at the project root:

```toml
[evidence]
"<RULE-ID>" = "npx playwright test --reporter=line"
```

The result is recorded as `factory:ui-evidence`. A declaration for a rule that is not an evidence rule is
reported as `factory:evidence` FAIL: the file supplies what is missing, it never replaces a rule's own check.

A built-in check that no rule maps runs only if it is a factory default below. When several rules map one
check, it runs once and its result names every mapped rule.

**Factory defaults** (run whether or not a rule maps them):

- `factory:suppressions`: a `# noqa` or similar suppression may not silence a code that a `MUST` rule selects
  (from a `lint` rule, or a `--select` in a `command` rule), and a broad exception handler may be suppressed only
  at a reported loop boundary. It runs whenever a `MUST` rule selects ruff codes, since those are what it
  protects; it checks only the lines a project added.

### `Enforced: command <command>`: a command that must exit zero

```markdown
Enforced: command ruff check . && ruff format --check .
```

Run in the project directory; `&&` separates commands, each must exit 0. Quoting works as in a shell, so
`python -c "print('A && B')"` is one command; unbalanced quotes or an empty command beside `&&` make the rule
fail as an invalid `Enforced:` line. `Enforced: tool <command>` is the
same and stays accepted. The tool is looked up in the project's `.venv/bin` and `node_modules/.bin` first.
When the project has no config of its own, ruff, mypy, tflint, prettier, eslint and tsc are given the baseline
from `languages/configs/`. A missing tool is reported as tool unavailable (an image defect), not as a failure.

For a rule in a language pack, a failure that the starting tree already had passes as pre-existing debt: only
new findings block.

The starting tree is where the project's own change begins. It is `base` with the newest upstream commit merged
into the branch (the factory-mandated `git merge origin/main`) merged in, when the history shows one: the commits
that are not the project's and do not build on its commits, with a single newest head that `origin/main` contains
(that descends from `base`, when the workspace has no `origin/main`). The branch's own merge on top of `base` is
not an upstream head; a merge on main is. For a project begun from main that start is the upstream commit's own
tree; for a resumed project, whose `base` is an earlier head of its branch, the earlier run's work stays in it, so
only this run's change is judged. A path on which `base` and upstream conflict takes upstream's text, so a
resolution to main's line is not a change. Everything `HEAD` differs from the start by is the project's change,
merge-commit content (a conflict resolution) included, so a path changed only in a merge is scope-checked. Without
such a commit the start is `HEAD` with each project commit reverse-applied, newest first.

When neither works (for example another author's commit edits the project's lines), the factory cannot compute
the project's changes. That is a **harness fault**, not the project's failure: `scope` and `factory:fail-to-pass`
are recorded as `harness fault` (and so is a failing language-pack rule or `factory:module-size`, since neither can be compared with a start).
It does not block, but it holds review: the supervisor escalates to a human, tells the session it is not at
fault and to `ask-human`, and bounces nothing. After the operator fixes the factory, `/review-again` resumes
the hold. If the project also has real failures, DONE bounces with those, and the next DONE reruns the gate.

### `Enforced: lint <linter> <codes>`: a linter rule selection

```markdown
Enforced: lint ruff BLE001,E722,TRY002
```

Runs only those codes, ignoring the project's own selection (`ruff check --isolated --select <codes> .`), and
makes them codes the suppression policy protects. `ruff` is the linter supported today.

### `Enforced: forbid <regex> [in <glob>[,<glob>...]]`: a pattern that must not appear

```markdown
Enforced: forbid \bprint\( in src/**/*.py
```

Fails, listing `file:line` for each match, when a line of a matching file matches the Python regex. Without
`in`, every text file of the project is searched. `.git`, `.venv`, `node_modules` and `.claude-flyte` are never
searched.

### `Enforced: script <path> [arguments]`: your own script

```markdown
Enforced: script checks/no_secrets_in_fixtures.py --strict
```

`<path>` is relative to the standards directory. The script runs with the project directory as its working
directory: a `.py` file with the factory's Python, a `.sh` file with bash, anything else directly. Exit 0
passes; its output is the report.

A script may say the rule does not apply to this project: if it exits 0 and the first non-empty line of its
output starts with `NOT APPLICABLE:`, the rule is recorded as *not applicable* with that output, and does not
block. Only `script` rules can do this. The same line with a nonzero exit is a failure, and with exit 0 but not
on the first non-empty line it is an ordinary pass.

### `Enforced: tool` with nothing after it

Kept for rules that need a project-specific command nobody has configured: reported as tool unavailable. The
project can declare the command in `factory-evidence.toml` at its root, `[evidence] "<RULE-ID>" = "<command>"`;
the rule then runs it like a `command` rule (exit 0 passes, anything else fails with its output). A rule that
has a command or any other check of its own cannot be given one this way.

## Mapping your IDs to checks

The mapping is the `Enforced:` line: there is no separate table. Any ID may map any check, several rules may map
one check, and a set may leave a built-in check unmapped (then it does not run, unless it is a factory default).
Reports, waivers and the review name your rule ID beside the check:

```
factory:fail-to-pass [QA-7]: PASS ...
QA-12: FAIL ruff check --isolated --select BLE001 . ...
```

A waiver (`waivers.toml`) may name the check (`factory:fail-to-pass`) or any rule ID mapped to it (`QA-7`).

## Vendored upstream trees

A project that builds a cross-repository patch may keep byte-identical copies of other repositories in its
directory. Those files are upstream's, not the project's, so `factory:module-size`, `factory:fail-to-pass` and
language detection judge only what the project changed in them. A copy that is a git checkout (has `.git`)
needs nothing: its own history says what changed. A copy committed without `.git` is declared in
`vendored.toml` at the project's root:

```toml
[[tree]]
path = "flyte-agent-factory"   # the copy's directory, relative to the project, inside it
repo = "https://github.com/example/flyte-agent-factory"
commit = "<full 40-hex sha the copy was taken from>"
```

The copy's root is upstream's repository root. An invalid entry (a path that is absolute, has `..`, is not a
directory or is the project itself, a `repo` that is not `https://`, a `commit` that is not 40 hex digits) or
invalid TOML is reported in the rule's output and ignored: its files count as the project's.

**Where the gate gets upstream.** From the git repository that holds the project only when the commit is in the
history of the project's trusted base, the commit the supervisor recorded when the project was created (after a
restart, `.claude-flyte/<name>/base-commit`, the base waivers are read against): any later local commit, and any
ref such as `refs/remotes/origin/*`, may be one the session made, and declaring it would let a project call its
own code upstream's. Otherwise by a shallow `https` fetch of that one commit into a cache under
`$XDG_CACHE_HOME/factory-gate/vendored` (or `~/.cache/factory-gate/vendored`), one store per commit. A fetched
commit must also be in the history of the declared repository's default branch (checked by a fetch of that branch's
history without trees, into one store per repository, reused): a repository serves a commit of any branch by its
id, so a commit pushed to a side branch of upstream is not upstream's. The two stores are kept apart because a
shallow boundary and a history without trees cannot share one: the boundary cuts the history walk, and a commit
without its tree would pass for fetched. The history store is not left a promisor remote, so a missing object
is an error, never an on-demand fetch. The fetch has no credentials and never
prompts, so a private repository, an unreachable host or a missing commit leaves the tree "unavailable": its
files are judged as the project's and the output names the tree, repository, commit and the error. The gate
never passes silently on a tree it could not read. A fetch that failed is not tried again for ten minutes.
Every git call ignores replace refs (`GIT_NO_REPLACE_OBJECTS`) and user and system git config. The gate runs in
the session's workspace, so these checks are as strong as that workspace's recorded base and the cache under
`$HOME`, which the session can write: the same limit waivers have.

The gate fetches; the dashboard's standards snapshot and the selection made on the supervisor's event loop never
do. They read only the cache, which the supervisor fills off the loop before the gate runs, and until then judge
a vendored tree as the project's.

**What changes.** A file identical to upstream's at the same path (compared by git blob id) is skipped by
`factory:module-size`, is not one of the project's changed tests for `factory:fail-to-pass`, and is left out of
language detection (a vendored Django `package.json` does not select the TypeScript pack).

**What still fails.** Judgement is against upstream's version, not against nothing: growing an over-limit
module (`... lines, was N`), adding a skip to a vendored test or removing a test from it
("tests removed, deleted or skipped"), a new module over the limit, and any new or changed file in the copy,
which counts as the project's. A file the project's start tree already had is judged against the start tree,
whatever upstream commit it matches. No waiver is needed for upstream's own files.
`vendored.toml` does not change the scope of a language pack's `mypy` rule: a project with vendored trees gives mypy a
project-level configuration that excludes them; that is a configuration, not a waiver.

**What is not judged.** A copy may be partial: a project vendors what it builds against, so a directory upstream
has and the copy leaves out entirely (a whole `tests/` included) is not a removal; the copy's tests are upstream's,
and they run in upstream's CI against the delivered patches. A test file upstream has, missing from a directory the
copy does have, is a removed test: `factory:fail-to-pass` fails naming it ("tests removed, deleted or skipped"),
unless the brief permits removing tests, and the rule output lists such files (the first ten, then a count).

## Demos

`demo.md` holds the rules for demo projects. A line in it of the form

```markdown
Pattern demos replace QA-3, QA-9 and SEC-2.
```

(a sentence starting with a word and containing `replace`, listing IDs and ending with a full stop) removes those
rules for a `Demo kind: pattern` project. A rule with the line `Applies: showcase` also applies to a
`Demo kind: showcase` project, whose other rules are not applied.

## Naming your users and brand: product.toml

A project may keep a `product.toml` at its root (beside its `README.md`, not in the standards directory). It
tells the factory who the product is for and which brand applies to this project. Without the file nothing
below happens.

```toml
[audience]
users = "Children learning to read, and the parent who sets the game up"   # who, in a sentence (required)
age_band = "6-8"          # required: shown in prompts; "6-8", "13+", "65+", "adult" are read for their ages
# under_13 = true         # true | false: required when age_band is any other text, and must agree with a range
expertise = "novice"      # required: novice | intermediate | expert
reading_grade = 2         # required: highest US school grade of text users must read, integer 0..18
main_task = "Create a world and walk into its first room"   # required: the one task a first-time user must finish

[brand]
file = "BRAND.md"   # path relative to the project root; "none" = no brand rules; omitted = the workspace brand

[surfaces]          # used by the workspace's UX check scripts, not by the engine
pages = ["prototype/index.html"]          # project files or http(s) URLs to render
serve = "npx --yes serve -l 4173 dist"    # optional, started before rendering
ready = "http://localhost:4173/"          # polled until it answers, when serve is set
viewports = ["320x640", "390x844", "1280x800"]
text = ["src/strings/*.json"]             # extra files whose user-facing text is reading-level checked
cli = "mytool"                            # the command a person runs, for a command-line tool
cli_invalid = ["--no-such-option"]        # arguments the tool must reject; required when cli is set
cli_main = ["--version"]                  # one successful run; required when cli is set; [] = bare command
```

**`[audience]`.** When all five fields are valid (and `under_13`, where `age_band` needs it, agrees with the age band), a short "This product's users" section is added to the
session instructions, the contract writer's prompt and the prompt of every reviewer. It states the fields and
that it overrides any audience named in `brand.md`, including whether children under 13 are among the users. When the file is present but not usable (invalid TOML, a
missing field, an `expertise` outside the three values, a `reading_grade` that is not an integer from 0 to 18),
the section says which fields are wrong instead, and the problems are also printed to standard error. An
unknown key in `[audience]` or `[brand]` is reported and ignored; it does not discard the valid fields.

**`[brand]`.** `file` names a file inside the project; its text becomes the project's `brand.md` for this
project only (the workspace's file is not touched). `"none"` removes `brand.md` from the selection. Omitted, the
workspace's brand applies as before, and the session's `BRAND.md` fallback is unchanged. A path that is absolute,
climbs out with `..`, leaves the project through a link, or is not a file is reported and ignored: the workspace
brand is kept. Only `Enforced: judgement` rules are taken from a project's brand file; a rule with any other
kind is dropped and reported, because a project must not grant itself a mechanical check or a script. So is a
rule whose ID another selected standard already uses, which would otherwise hide that rule. A file
without rules (colours, type, tone) is fine and is passed on as guidance text.

**`[surfaces]`** is not read by the engine. The workspace's UX check scripts (`Enforced: script`) read it from
the project's directory, which is their working directory; a script with no `[surfaces]` to check can print
`NOT APPLICABLE:` as above. Each script validates every declared `text` entry (it must match a regular file
inside the project) before it reports a result. The CLI script runs each declared `cli` command, without a
shell, with `--help`, with the `cli_invalid` arguments and with the `cli_main` arguments: help must identify
itself as usage (the word "usage" or an option list), the error must name a rejected argument or point to
`--help`, and the main run must exit 0. The output of all three is piped, and any escape code or carriage
return without a newline in it fails; the colour switches in the environment (`NO_COLOR`, `FORCE_COLOR`,
`CLICOLOR`, `CLICOLOR_FORCE`) are removed and `TERM` is `xterm-256color`, so only the pipe can keep it plain.
How to write a UX standard of your own, and what
the template's checks measure, is in [ux-standard.md](ux-standard.md).

The behaviour lives in `_product.py` (`parse`, `load`, `resolve_brand`, `find_problems`, `format_audience_block`); the
brand is applied in `_standards.select`, so it holds for `materialize`, the gate and the reviewers alike.

## Checking a tree against your standards

```bash
PYTHONPATH=src uv run python -m flyte_agent_factory._gates standards <dir> --standards <your standards dir>
```

runs every mechanically checked `MUST` rule over `<dir>` as the DONE gate would, and prints each result.

## The worked example

The template's set maps its rules as follows (its IDs are its own; any others work the same):

| Kind | Rules |
|---|---|
| `check factory:fail-to-pass` | the tests-first rule in `engineering.md` |
| `check factory:module-size` | the module-size rule |
| `check factory:language-summary` | the language-gate rule |
| `check factory:ui-evidence` | the UI-evidence rule |
| `lint ruff ...` | the exception-handling rule |
| `command ...` | the formatter, linter, type-checker and test rules in `languages/` |
| `judgement` | everything else, including all of `security.md`, `brand.md` and `demo.md` |

The behaviours added for human-facing work, in one place:

| Behaviour | Where |
|---|---|
| A `script` that prints `NOT APPLICABLE:` first and exits 0 is not applicable | `Enforced: script` |
| A base file with `applies: human-facing` is limited to human-facing work, like `brand.md` | "Files" |
| `product.toml` names the audience, and may replace or remove the project's brand | "Naming your users and brand" |
