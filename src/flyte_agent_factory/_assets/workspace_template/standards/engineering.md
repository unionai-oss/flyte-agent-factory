# engineering

## ENG-001 SHOULD
Use a widely used library whenever one covers the need, rather than writing your own; a recent release is a good sign, but a stable, finished library with no known security issues is fine too.

Why: Hand-rolled code is a common AI anti-pattern and adds code to maintain.

Check: For each new helper over about 20 lines, can you find a well-known library that does the same job? If yes, flag. The dependency must be acceptable: maintained or finished, licence compatible, no known CVEs, size proportionate.

Enforced: judgement

## ENG-002 SHOULD
Extend an existing file or module before creating a new one; add a new file only when it has a clearly separate job.

Why: Agents tend to create file after file, which spreads logic and duplicates code.

Check: `git diff --name-status main | grep ^A`. Does every added file have a job no existing file covers?

Enforced: judgement

## ENG-003 MUST
Experiments and throwaway runs live in the scratch sandbox; production work is promoted to a branch in the permanent repo as one clean PR, with no numbered copies or abandoned attempts carried over.

Why: You don't need multiple versions of a file, because git keeps the history.

Check: `ls | grep -E -- '-[0-9]{2}$|-exp-'` in the target repo returns nothing.

Enforced: judgement

## ENG-004 MUST
For production code and for complex demos shown only through the UI or CLI, write a failing test that describes the behaviour before writing the code, then make it pass and refactor; never delete, skip or weaken a test to get to green.

Why: Without test-first discipline, agents pile up complexity and fake green (Beck's B+ Tree experiment; TDAD cut regressions about 70%).

Check: Factory fail-to-pass gate: tests added or changed must fail against base non-test code and pass on head. Non-demo projects only; DONE may declare `refactor: no behaviour change` (recorded exemption). Removing, skipping or deleting tests requires the brief to ask for it.

Enforced: check factory:fail-to-pass

## ENG-005 MUST
Pattern demos (code-walkthrough demos) are one simple file that does one thing, written for teaching, with one functional test written before the code that runs the demo end to end, and no unit tests.

Why: Demo code has to be readable on screen, and agents still need an objective signal for "done".

Check: Is the demo a single source file with exactly one end-to-end test and no unit tests?

Enforced: judgement

## ENG-006 SHOULD
Mock only at the system's outer edges, such as external APIs and paid services; test your own code for real and assert on outcomes, not on which functions were called.

Why: Over-mocking ties tests to implementation details and makes them brittle.

Check: `grep -rn "patch(" tests/`. Is every patched target external? Are there any `assert_called` assertions on internal functions?

Enforced: judgement

## ENG-007 MUST
Let errors crash loudly with a clear message; catch only specific errors you can actually handle; a broad catch is allowed only at a loop boundary, with a reason, and must report. In Flyte tasks, raise `flyte.errors.NonRecoverableError` for failures retrying cannot fix, set `retries=` on long-running tasks, and checkpoint their completed work.

Why: Swallowed errors hide the traceback the supervisor needs to fix the code, and catch-all handlers are nearly twice as common in AI code. Retrying cannot repair invalid inputs, and a restarted task should keep its completed work.

Check: ruff check --isolated --select BLE001,E722,TRY002,TRY203,TRY400 .; in Flyte tasks, inspect terminal failure paths for NonRecoverableError and long-running tasks for `retries=` and checkpointing.

Enforced: lint ruff BLE001,E722,TRY002,TRY203,TRY400

The `factory:suppressions` gate checks Python comments and exception handlers independently of project
Ruff configuration. Use `# noqa: CODE -- reason` (or `# noqa: CODE # reason`) with specific codes and a reason
of at least 10 characters. Codes explicitly selected by MUST rules cannot be suppressed. The sole exception is
BLE001 on a broad handler inside a loop that directly reports with `logger.exception(...)`, a logging
call with `exc_info=True`, or `traceback.print_exc()`. Swallowing broad handlers and
`contextlib.suppress` of `Exception` or `BaseException` fails, including imported aliases.
File-wide Ruff/noqa directives are not allowed.

Judged: the Flyte clause (NonRecoverableError, `retries=`, checkpointing), which no tool checks.

## ENG-008 SHOULD
Each module has one clear responsibility; split a file when it covers several unrelated jobs, with size left to judgement.

Why: Agents handle nearby code well and long-range dependencies poorly.

Check: Can you describe the module's job in one sentence without using "and"?

Enforced: judgement

## ENG-009 MUST
Every function name is a verb phrase that says what it does; no nicknames, even for central loops. Exceptions: main, properties, test functions, fixtures, validators and dunder methods.

Why: You should be able to read a function's purpose without hunting through its body.

Check: Does any function name lack a verb or need its body read to understand it? If yes, fail.

Enforced: judgement

## DOC-001 MUST
Agent instructions live in one short AGENTS.md per repo, with no CLAUDE.md, containing only what an agent can't infer from the code, like custom commands and hard constraints.

Why: ETH Zurich found that bloated or generated context files hurt agents, and AGENTS.md is read by both Codex and Claude Code.

Check: `git ls-files '*CLAUDE.md'` is empty. Does every line in AGENTS.md state something the code can't show?

Enforced: judgement

## DOC-002 MUST
Each repo has one to-the-point README; add a docs folder only when the README would otherwise get too long, and keep no other markdown files at the root.

Why: Fewest documents possible.

Check: `ls *.md` at the root returns only README.md and AGENTS.md.

Enforced: judgement

## DOC-003 MUST
Docs describe what the code does now; don't record abandoned approaches, and explain a rejected option only when someone would otherwise obviously try it again. History goes in commit messages.

Why: Docs about dead ends are noise.

Check: `grep -rniE "we tried|removed|no longer|previously" *.md docs/` returns nothing unjustified.

Enforced: judgement

## ENG-010 MUST
Real deployment profiles are versioned in a separate private config repo (or kept untracked locally) and referenced by path/env; the factory repo commits only profiles/example.toml and profiles/README.md. Commit only an example config file; real config files are gitignored, hold secrets only by reference, and are validated at startup, failing fast on anything missing or malformed.

Why: This prevents leaked secrets and half-configured runs.

Check: `git ls-files profiles/` lists only profiles/example.toml and profiles/README.md. Does startup with a missing key fail immediately?

Enforced: judgement

## ENG-011 SHOULD
Use TOML for structured config; use .env only for a few flat values.

Why: TOML has types and nesting, and Python reads it natively.

Check: Does any .env file contain nested or list-shaped values? If yes, fail.

Enforced: judgement

## ENG-012 MUST
Nothing with a UI is done until a separate verifier runs it in a real browser, with a scripted check that must pass plus screenshots, console errors and failed requests reported as evidence; a zero exit code is never proof.

Why: Agents misreport their own work about 23% of the time, often on UI behaviour.

Check: Does the QA report include a passing browser script run plus screenshots and console output?

Enforced: check factory:ui-evidence

## ENG-013 MUST
QA is an independent verifier, given only the acceptance criteria, the artifact and live system state, never the builder's summary; it is told to find reasons to reject, and every verdict cites concrete evidence such as a test, a code line or a browser observation.

Why: Reviewers anchor on the builder's confident summary and share its blind spots.

Check: Does the verifier prompt exclude the builder's output summary? Does every verdict line cite evidence?

Enforced: judgement

## ENG-014 SHOULD
Use a different model provider for QA than the builder used, when both are available.

Why: Different models have different blind spots.

Check: Does the run log show different providers for builder and verifier?

Enforced: judgement

## ENG-015 MUST
Every language's standard formatter and linter runs as a required check, in the factory before work counts as done and in CI on every commit.

Why: Style and lint should be enforced by machines, not by review.

Check: `ruff format --check`, `ruff check`, `terraform fmt -check -recursive`, `tflint` and `shellcheck` all pass in CI and in the factory gate.

Enforced: check factory:language-summary

## ENG-016 MUST
Delete a branch once its work is merged or abandoned for good; a branch sent back for fixes stays until it's merged or dropped. Automatic deletion of merged branches is turned on.

Why: Stale branches pile up and keep dead code reachable.

Check: `git branch -r --merged origin/main` lists nothing but main.

Enforced: judgement

## ENG-017 MUST
Never log credentials or sensitive payloads.

Why: Logs have a wider audience.

Check: Inspect logging calls and captured failure logs for secrets and personal data.

Enforced: judgement

## ENG-018 MUST
Lock dependency versions for reproducible installation.

Why: Unbounded resolution changes behaviour.

Check: Install using the committed lockfile or exact version manifest.

Enforced: judgement

## ENG-019 MUST
Make repeatable scripts safe to rerun.

Why: Retries must not destroy existing state.

Check: Run provisioning twice in a disposable environment and compare resulting state.

Enforced: judgement

## ENG-020 MUST
Stop scripts on failed required steps.

Why: Later commands must not publish partial success.

Check: Inspect shell scripts for strict mode or explicit checks on every fallible required command.

Enforced: judgement

## ENG-021 MUST
Release files, connections and processes on failure.

Why: Leaks accumulate under retries.

Check: Inspect resource ownership and exercise cancellation or a failure after acquisition.

Enforced: judgement

## ENG-022 MUST
Make timezones and measurement units explicit.

Why: Ambiguous values produce wrong decisions.

Check: Inspect persisted timestamps and numeric API fields for documented timezone and units.

Enforced: judgement

## ENG-023 MUST
Preserve compatibility or document a migration.

Why: Consumers need a safe upgrade.

Check: Compare changed public interfaces and run the stated migration on old data.

Enforced: judgement

## ENG-024 MUST
Keep each source module under 1,500 lines; a module already over the limit may not grow. Applies to source modules, not to tests, migrations or vendored and generated files.

Why: Agents handle nearby code well and long-range dependencies poorly; very large modules hide duplication and make every change expensive.

Check: The factory gate's `factory:module-size` counts physical lines in each Python source module and fails a new module over the limit, or an existing one that grew past it, against the project's starting tree.

Enforced: check factory:module-size

## ENG-025 SHOULD
Choose deliberately which tests run, in which lane, and what they cost. A test gives the same result on every run for the same code: timing and concurrency tests control time or wait on the condition they check, never race the wall clock. A time bound is set from the test's measured run time, recorded beside it, not picked to make a run pass. A slow test earns its place by checking something a cheaper test cannot; when a cheaper test would check the same thing and save minutes per run, file it as follow-up rather than spend effort shaving seconds.

Why: Suite time is paid on every pre-review and CI run, and a test that fails at random blocks unrelated work; arbitrary bounds either flake under load or hide hangs.

Check: For each new or raised timeout, new slow-lane test or test that failed without a code change: is its measured duration recorded, does it pass repeatedly in the slow-lane configuration, and would a cheaper test cover the same behaviour?

Enforced: judgement
