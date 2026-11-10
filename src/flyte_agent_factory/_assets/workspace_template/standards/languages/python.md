# languages/python

detect: pyproject.toml, setup.py, **/*.py

## PY-001 MUST
Public modules, classes and functions get a docstring, one line unless callers need details like errors or side effects; private helpers get none, and comments only explain why something non-obvious is done.

Why: This follows PEP 8 and PEP 257, and avoids comment bloat.

Check: `ruff check --select D .` passes with pydocstyle convention pep257 for public names; test files are exempt from docstrings wherever they are (`**/test_*.py`, `**/*_test.py`, `**/tests/**`, `conftest.py`; the shipped ruff baseline ignores `D` for them).

Enforced: command ruff check --select D .

## PY-002 SHOULD
Prefer passing state explicitly through arguments or an object over changing module-level variables; module-level constants are fine.

Why: Hidden shared state makes testing hard and leaks between tests and restarts.

Check: `grep -rn "^\s*global " src/` returns little or nothing.

Enforced: judgement

## PY-003 SHOULD
Expose operational tasks as subcommands of one installed command declared in pyproject.toml, with the logic living in the library; no loose scripts, and no subcommand that just wraps a single existing command.

Why: The command line should be a thin consumer of the library.

Check: `ls ops/ scripts/` is empty or absent, and `[project.scripts]` declares the CLI.

Enforced: judgement

## PY-004 MUST
Run Ruff lint and format checks.

Why: Consistent tooling catches common errors and enforces consistent formatting.

Check: ruff check . && ruff format --check .

Enforced: command ruff check . && ruff format --check .

## PY-005 MUST
Run behaviour tests with pytest.

Why: Runtime paths need executable evidence.

Check: python -m pytest

Enforced: command python -m pytest
Timeout: 1200

## PY-007 MUST
Do not use mutable default arguments.

Why: Defaults persist across calls and can leak state between callers.

Check: Run Ruff B006 and inspect function defaults; call stateful helpers twice to confirm independent results.

Enforced: command ruff check --select B006 .

## PY-008 MUST
Use context managers for owned files, connections and locks.

Why: Resources must close when an operation fails.

Check: Inspect resource acquisition for with or async with; exercise an exception inside the owned lifetime and confirm release.

Enforced: judgement

## PY-009 MUST
Pass subprocess arguments as a sequence, avoid shell=True, and bound execution time.

Why: Shell expansion enables injection and stalled children block workers.

Check: Inspect subprocess calls for argument arrays and timeouts; test a failed child and a child that exceeds the timeout.

Enforced: judgement

## PY-010 MUST
Keep unit tests independent of live networks and wall-clock timing.

Why: External services and real sleeps make failures intermittent.

Check: Inspect unit tests for HTTP calls, current-time reads and sleeps; replace them with injected fakes or controlled clocks and run with network access disabled.

Enforced: judgement

## PY-011 MUST
Declare package metadata in pyproject.toml and commit a dependency lockfile.

Why: Installations must use the same resolved dependency graph.

Check: Install from a clean environment using the documented frozen or locked install command; confirm it leaves the lockfile unchanged.

Enforced: judgement

## PY-012 MUST
Keep blocking work out of async functions and preserve cancellation.

Why: A blocked loop stalls unrelated requests and swallowed cancellation prevents shutdown.

Check: Inspect async paths for blocking I/O and sleeps; use async APIs or asyncio.to_thread, then cancel an in-flight task and confirm cleanup completes and cancellation propagates.

Enforced: judgement


## PY-014 MUST
Type public interfaces and run the type checker (mypy or Pyright).

Why: Annotations only protect callers when checked.

Check: mypy .; inspect public parameters and return types for missing annotations; test files (`**/test_*.py`, `**/*_test.py`, `**/tests/**`, `conftest.py`) are exempt from disallow_untyped_defs and disallow_incomplete_defs, and so may call their own untyped helpers (disallow_untyped_calls).

Enforced: command mypy .
