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
  brand.md             # optional: rules for visual or customer-facing work only
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
Rules in `brand.md` are judged only for visual or customer-facing deliverables.

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
directory. Those files are upstream's, not the project's, so `factory:module-size`, `factory:fail-to-pass`,
language detection, `factory:ui-evidence`, `factory:suppressions`, shell lint (shellcheck), `forbid` rules, ruff,
eslint and prettier judge only what the project changed in them. A copy that is a git checkout (has `.git`)
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
`$XDG_CACHE_HOME/factory-gate/vendored` (or `~/.cache/factory-gate/vendored`), made once per repository and
reused. A fetched commit must also be in the history of the declared repository's default branch (checked by a
fetch of that branch's history without trees): a repository serves a commit of any branch by its id, so a commit
pushed to a side branch of upstream is not upstream's. The cache is not left a promisor remote, so a missing object
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
`factory:module-size`, is not one of the project's changed tests for `factory:fail-to-pass`, is left out of
language detection (a vendored Django `package.json` does not select the TypeScript pack) and does not make the
project a UI for `factory:ui-evidence`. The same holds for the tools that judge each file alone: shellcheck is not
given an unchanged `.sh` (one named in the rule's command is dropped from it), ruff, eslint and prettier leave
the unchanged paths out as below, and a `forbid` rule skips the file. No command line grows with the number of
excluded files beyond 64 KiB (Linux refuses a single argument over 128 KiB):

- `ruff check`, `ruff format`: by default the paths are added inline, `--config 'extend-exclude = ["./repo"]'`.
  That adds to every configuration ruff finds, so the project's settings, its own `extend-exclude` included,
  still apply, and so does a `ruff.toml` or `pyproject.toml` in a subdirectory, for the files below it. The ruff
  `lint` kind gets the same and keeps `--isolated`, so it still reads no configuration but the excludes.
  - When that argument would exceed 64 KiB and the command checks `.`, the `.` is replaced by the project's own
    files that ruff reads, as the fewest paths: a directory below the project root that holds no unchanged file,
    else the file. Ruff still finds the closest configuration for each, and `--force-exclude` keeps the project's
    excludes applying to them. A git-ignored path, which ruff would skip walking `.` but lints when named, is left
    out (asked of the git repository holding it; outside one ruff reads no `.gitignore`).
  - When that list would exceed 64 KiB too, or the command checks something other than `.`, the excludes go in a
    temporary configuration file passed with `--config`, which `extend`s the configuration ruff would otherwise
    read at the project root (the project's `.ruff.toml`, `ruff.toml` or `pyproject.toml` with `[tool.ruff`, else
    the gate's baseline; for the `lint` kind, which runs without `--isolated` then since ruff refuses a file
    with it, nothing). A file replaces ruff's search for configuration, so no configuration nested below the
    project root is read, and the rule's output says so: `Ruff read no configuration nested below the project
    root: ...`.
- eslint: a flat config that imports the one eslint would use (the file the rule's command names with `-c` or
  `--config`, which the wrapper then replaces, else the project's `eslint.config.*`, else the gate's baseline) and
  appends a global `ignores` entry, passed with `--config`; that config's rules and ignores still apply. With a
  legacy `.eslintrc*` and no flat config, an ignore file passed with `--ignore-path`, holding the lines of the
  ignore file the command names with `--ignore-path` (which it replaces), else of `.eslintignore`, and the
  excludes, since `--ignore-path` replaces `.eslintignore`. With `--no-config-lookup` and no config named,
  eslint reads no config, and the wrapper imports none. A flat config file in a subdirectory is not read by
  eslint 9 in any case, so wrapping the root one loses none.
- prettier: an ignore file passed with `--ignore-path`. It also applies to files named in the command, and
  `.gitignore` and `.prettierignore` are passed with it, since any `--ignore-path` replaces prettier's defaults.
- eslint (with a flat config) and prettier, past 50 excluded paths when the command checks `.`: the `.` is
  replaced by the project's own files of any extension, collapsed as for ruff, and no ignore list is built. Eslint slows with every global ignore it matches each file against (on a checkout of
  40,000 files: 33 s with none, 35 s with 50, 60 s with 200, over 300 s with 2,500), and prettier likewise, so
  naming the project's files keeps such a run near the time of a plain one. Eslint is passed
  `--no-error-on-unmatched-pattern --no-warn-ignored`, prettier `--ignore-unknown
  --no-error-on-unmatched-pattern`, so a named file that the project's own ignores or config skip (for eslint,
  one no config's `files` matches) is skipped silently, as walking `.` would; the project's config and ignores, prettier's `.gitignore` and
  `.prettierignore` included, still apply to named files. Eslint reads no `.gitignore`, so no git-ignored path is
  left out for it. When the list would exceed 64 KiB, or the command checks something other than `.`, the ignore
  list is used as above; so it is with a legacy `.eslintrc`, since legacy eslint lints a named file whatever its
  extension.

Eslint's and prettier's files hold any number of paths. The temporary files are removed when the tool exits. The
recorded command is the one that ran, temporary file names included.

The excluded paths are as few as cover every unchanged file the tool reads: the highest directory inside an upstream
tree that holds no project file the tool reads (a whole untouched checkout is one path, `repo`), and an unchanged
file one by one only where its directory also holds one. Which files ruff reads is asked of ruff
(`ruff check --show-settings --no-force-exclude` for an empty file at the project root, with the command's
`--isolated` or `--config`, so a project that excludes such a file's name still answers: the `include` and `extend-include` the project root's configuration resolves to, by default `*.py`,
`*.pyi`, `*.ipynb`, `*.md`, `pyproject.toml`, `ruff.toml` and `.ruff.toml`), so a git-ignored
`__pycache__/x.pyc` of the project's does not keep its directory from being excluded whole. Only the root
configuration's `include` is asked, not a nested checkout's: a nested ruff configuration with a narrower one makes
ruff skip a file the gate counts as read, which can only make ruff lint an owned file it otherwise would not,
never skip one. A flat eslint config's `files` decide which files eslint reads (`.vue`, `.md`, `.es6` ...), and
prettier's plugins which it formats, so for both any file of the project's keeps its directory, and every
unchanged upstream file is excluded whatever its extension. Each path is anchored at the project: ruff's start with `./` (a bare `repo`
would also exclude the project's own `tools/repo`), eslint's are resolved against the project, ignore-file
lines start with `/`. Glob characters in a file name are escaped (`a\[1\].py`), so an exclude never matches a
file of the project's that the pattern would otherwise cover.

In a nested git checkout a file is upstream's only when it is in the checkout's base tree and git reports no change
to it. A file new to the checkout is the project's, including one the checkout's `.gitignore` hides (git status
does not list it) and any file inside a submodule (status names only the submodule, and its files are not blobs of
the outer tree): `factory:module-size`, test removal, language detection, `factory:ui-evidence` and the linters
judge it as any other file of the project's. Walking a directory, ruff still skips git-ignored files,
the project's own as much as these; one named in the rule's command is linted.

The rule's output ends with what was taken as upstream's, including nested git checkouts: `Nested git checkouts
judged against their base: repo (base <12 hex>).` A checkout whose base or change set is unknown (no default
branch to take a merge-base with) is judged as the project's, and the output says so. A nested git checkout needs
no manifest; a copy without `.git` is recognised through `vendored.toml`, by the same rule: a file is upstream's
only when identical to it.

How each gate path that walks the project's files or runs a tool over them treats an unchanged upstream file. A
file new to a nested checkout, git-ignored or inside a submodule, is not one: every path judges it as the
project's.

| Gate path | Unchanged upstream file | Why |
|---|---|---|
| language detection | not counted | says nothing about the project's languages |
| `factory:module-size` | skipped | judged against upstream's line count |
| `factory:fail-to-pass` (test removal) | not one of the project's changed tests | judged against upstream's version |
| `factory:fail-to-pass` (did the start tree have tests, for the `refactor: no behaviour change` exemption) | counted | an upstream test suite is tests the project can run and must keep passing, so a refactor of the checkout has behaviour to preserve |
| `factory:ui-evidence` (is there UI: `.html`, `.htm`, `.tsx`, `.jsx`, `.vue`, `.svelte`) | not counted | an unchanged upstream page is upstream's UI, not the project's |
| shellcheck file list | not listed | per-file linter: its findings are upstream's |
| `forbid` rules | skipped | a pattern in upstream's file is upstream's |
| `ruff check` / `ruff format` | excluded (inline `--config 'extend-exclude = [...]'`; past 64 KiB the project's files named in place of `.`; past that a temporary `--config` file, noted in the output) | per-file linter |
| ruff `lint` kind (`--isolated`) | excluded (as above, keeping `--isolated`; the temporary file replaces it and extends nothing) | per-file linter |
| eslint | excluded (temporary flat config importing the config the command names, else the project's or the baseline config; `--ignore-path` file for a legacy `.eslintrc`; past 50 paths, with a flat config, the project's files named in place of `.`) | per-file linter, when run on a directory or glob |
| prettier `--check` | excluded (temporary ignore file; past 50 paths the project's files named in place of `.`) | per-file linter |
| mypy / pyright | still judged | whole-program: the project's change elsewhere can break an unchanged file; configure it to exclude vendored trees |
| pytest | still run | tests run as a whole; upstream's own failures are not filtered |
| tsc (incl. the baseline `files` list) | still judged | whole-program, like mypy |
| tflint | still judged | evaluates a module as a whole |
| `terraform fmt -check -recursive` | still judged | has no exclusion option, and a per-directory workaround would change what the rule checks |
| suppressions policy (`changed_lines`) | skipped | with a base commit it judges only the lines the project added, and an identical file adds none; without one it checks every `.py` except a file identical to upstream's, and its output names the trees |
| `script` rules | not affected | run by the rule's own script, which gets no file list |

Two other walks read the project's tree but judge nothing in it: finding the nested git checkouts themselves, and
the content hash of the start tree that keys the cache of its rule results.

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
