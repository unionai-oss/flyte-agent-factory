r"""Run the guided setup from the command line.

It creates the Flyte project, workspace repository, secrets, GitHub App and profile, then deploys the
factory. Idempotent: run it again after any interruption.

    uv run python ops/bootstrap.py --endpoint <host> --project <id> --workspace <owner>/agent-workspace \\
        --state-uri s3://<bucket>/<prefix> [--backend union|oss] [--engine claude|codex] \\
        [--domain <name>] [--dir <clone>] [--redeploy] [--no-app] [--no-board]
    uv run python ops/bootstrap.py --status ...   # read-only: print each step's check
    uv run python ops/bootstrap.py --dry-run ...  # print what each step would do; change nothing
    uv run python ops/bootstrap.py ... --chat-key | --deploy-key | --grant <policy> <client-id>
        # instead of the steps: Union API keys (Linux x86_64)

Design: docs/quickstart.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

# A fresh clone runs under `uv run`, which does not install this project (`package = false`); without
# this the import below fails with ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flyte_agent_factory import _bootstrap

if __name__ == "__main__":
    sys.exit(_bootstrap.main())
