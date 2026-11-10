"""Coordinator-only concurrent setup-token sessions; makes paid model requests.

Run after token_meter.py with CLAUDE_CODE_OAUTH_TOKEN mounted:
    PYTHONPATH=src uv run python ops/probes/setup_token.py --nonce UNIQUE --max-k 4 --output sharing.json

Each K uses fresh isolated config dirs and overlapping calls. The result records
only success, timing and tested K, never CLI output or credentials. Retain it for QA;
safe_k is the largest contiguous successful K tested here, not an unbounded guarantee.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from flyte_agent_factory._auth import write_settings


def session(nonce: str, barrier: threading.Barrier) -> dict[str, Any]:
    """Run one `claude -p` session on the setup token and return its outcome and elapsed seconds."""
    with tempfile.TemporaryDirectory(prefix="setup-token-") as directory:
        root = Path(directory)
        config = root / "claude"
        write_settings(config, hold_dialogs=False)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CREDENTIALS_JSON")
        }
        env["CLAUDE_CONFIG_DIR"] = str(config)
        barrier.wait(timeout=60)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [
                    "claude",
                    "-p",
                    f"Reply with exactly {nonce}",
                    "--output-format",
                    "json",
                    "--max-turns",
                    "1",
                    "--tools",
                    "",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                timeout=180,
            )
            payload = json.loads(proc.stdout)
            ok = proc.returncode == 0 and not payload.get("is_error") and nonce in payload.get("result", "")
            category = "ok" if ok else "refused-or-unexpected-result"
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
            category = "process-or-result-unavailable"
        return {"status": category, "elapsed_s": round(time.monotonic() - started, 3)}


def probe(nonce: str, max_k: int) -> dict[str, Any]:
    """Run 1 to `max_k` concurrent sessions on one setup token; return each round's outcomes."""
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        raise RuntimeError("mount CLAUDE_CODE_OAUTH_TOKEN before running the probe")
    rounds = []
    safe_k = 0
    for k in range(1, max_k + 1):
        barrier = threading.Barrier(k)
        with concurrent.futures.ThreadPoolExecutor(max_workers=k) as pool:
            results = list(pool.map(session, [f"{nonce}-{k}-{i}" for i in range(k)], [barrier] * k))
        rounds.append({"k": k, "sessions": results})
        if any(result["status"] != "ok" for result in results):
            break
        safe_k = k
    return {"nonce": nonce, "safe_k": safe_k, "rounds": rounds}


def main() -> None:
    """Run the probe and write its JSON result to `--output`, which must not exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--max-k", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", args.nonce) or not 1 <= args.max_k <= 16:
        parser.error("use an 8-64 character alphanumeric/hyphen nonce and max-k from 1 to 16")
    with args.output.open("x") as output:
        json.dump(probe(args.nonce, args.max_k), output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
