# Agent notes

- Run every check with `bash factory-improvement/checks.sh` (pytest, `ruff check`, `ruff format --check`; line length 110).
  Ruff also formats the Python code blocks inside `.md` files under `ops/`.
- Pytest's default run (`addopts`) leaves out `slow`; `checks.sh` runs the `not slow` lane and, with
  `AGENT_FACTORY_RUN_SLOW=1` or `AGENT_FACTORY_SLOW_LANE=child`, the `slow` lane; CI runs both. Mark a test that runs
  Terraform or a benchmark hidden check (seconds each) `slow`; the two lanes together must select every test.
- Never print environment variables or secret values. List names only, with `compgen -e`.
- A probe under `ops/probes/` must guard its entry point with `if __name__ == "__main__":`; the Flyte bundler imports the
  module, so unguarded top-level code runs on import.
- The code bundle ships only statically imported `.py` modules. A module loaded by name at runtime, or any non-Python file,
  is missing in the container unless it is put into the image.
- Owner-specific values (accounts, buckets, secret names, run ids) belong in a deployment profile, never in code, docs or
  tests. `tests/test_profile.py::test_no_personal_literal_is_left_in_shareable_files` enforces it.
- Write the failing test first; never weaken or delete an existing test to make a change pass.
