# languages/shell

detect: **/*.sh

## SH-001 MUST
Never wrap a single command in a script; document the command. In demos, show the actual Flyte commands even if there are several. No Makefile at all unless chaining genuinely multi-step work. Document direct commands (`uv run pytest`, `uv run ruff check`) in README/AGENTS.md instead.

Why: Demos exist to show Flyte commands, and wrappers hide them.

Check: Does any script or Makefile target contain only one command? If yes, fail.

Enforced: judgement

## SH-002 MUST
Check shell scripts with ShellCheck.

Why: Quoting errors are hard to reproduce.

Check: shellcheck scripts/*.sh

Enforced: command shellcheck

## SH-003 MUST
Use strict failure handling for Bash scripts.

Why: Failed prerequisites must stop execution.

Check: Inspect Bash entry points for set -euo pipefail or explicit justified error handling.

Enforced: judgement

## SH-004 MUST
Quote expansions unless splitting or globbing is intentional and documented.

Why: Whitespace and wildcard characters otherwise change command arguments.

Check: Run ShellCheck and exercise scripts with paths containing spaces and glob characters; inspect intentional unquoted expansions.

Enforced: judgement

## SH-005 MUST
Create temporary files with mktemp and clean them with traps.

Why: Predictable names allow collisions and interrupted scripts leave sensitive files behind.

Check: Inspect temporary paths for mktemp and traps installed immediately after creation; force a command failure and send TERM to confirm cleanup.

Enforced: judgement

## SH-006 MUST
Use globs or find instead of parsing ls output.

Why: Filenames can contain whitespace and newlines.

Check: Inspect filename loops for ls pipelines or command substitution; test with filenames containing spaces, newlines and leading dashes.

Enforced: judgement

## SH-007 MUST
Make scripts safe to rerun after partial failure.

Why: Operators retry scripts when a command fails.

Check: Run twice in a disposable environment, then rerun after an injected intermediate failure; confirm intended state and preserved existing data.

Enforced: judgement

## SH-008 MUST
Bound network operations with connection and overall timeouts.

Why: Unreachable services must not stall automation indefinitely.

Check: Inspect curl, wget, ssh and other network commands for supported timeout options; exercise an unreachable endpoint and confirm bounded failure.

Enforced: judgement

## SH-009 MUST
Check required external tools before side effects.

Why: Missing prerequisites should fail before partial mutation.

Check: Inspect startup command -v checks; remove a required tool from PATH and confirm a useful error before mutation.

Enforced: judgement

## SH-010 MUST
Choose a shebang that matches the syntax and supported runtime.

Why: Bash syntax cannot run reliably under sh.

Check: Inspect executable scripts for #!/usr/bin/env bash or a documented interpreter path; run syntax checks with that interpreter and ShellCheck with the matching dialect.

Enforced: judgement

## SH-011 MUST
Pass data as arguments and avoid eval or interpolated command strings.

Why: Re-parsing data as shell code enables injection.

Check: Inspect eval, sh -c and bash -c calls; test untrusted inputs containing semicolons and command substitutions and confirm they remain literal data.

Enforced: judgement

## SH-012 MUST
Handle failures that strict mode does not catch.

Why: Conditionals, pipelines and substitutions have surprising errexit behaviour.

Check: Inspect required commands in conditionals, substitutions and pipelines; inject failures and confirm nonzero exit with no subsequent publication or mutation.

Enforced: judgement

## SH-013 SHOULD
Provide actionable diagnostics on stderr and propagate exit status.

Why: Callers need machine-readable success and useful failure context.

Check: Run with invalid arguments and a failed external command; confirm nonzero status, diagnostic context on stderr and no success output.

Enforced: judgement
