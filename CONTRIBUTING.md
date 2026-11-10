# Contributing to flyte-agent-factory

Thanks for helping. The full guide is [docs/contributing.md](docs/contributing.md): setting up, the checks, tests
first, owner waivers and the DCO. The short version:

1. **Set up** with [uv](https://docs.astral.sh/uv/) and Python 3.12 or later: `uv sync --python 3.12`. No cluster,
   credentials or Claude subscription are needed to run the suite.
2. **Write the failing test first**, then the change. Never weaken, delete or rename an existing test to get a
   change through; if a test is wrong, say why in the pull request.
3. **Run the checks** before you open a pull request: `bash factory-improvement/checks.sh` (the not-`slow` test lane,
   `ruff check`, `ruff format --check`; `AGENT_FACTORY_RUN_SLOW=1` adds the `slow` lane, which needs Terraform).
   CI runs the jobs your change can affect; [docs/ci.md](docs/ci.md) says which.
4. **Keep deployment-specific values out**: accounts, hosts, buckets, secret names and run ids belong in a
   deployment profile, never in code, docs or tests. The literal guard in `tests/test_profile.py` fails the suite
   when one slips in.
5. **Sign off your commits** (`git commit -s`) to certify the [Developer Certificate of
   Origin](https://developercertificate.org/).

Security problems go through [SECURITY.md](SECURITY.md), not issues. By contributing you agree that your
contribution is licensed under the project's [Apache License 2.0](LICENSE).
