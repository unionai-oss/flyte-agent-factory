"""Coordinator-only usage-endpoint probe; run with CLAUDE_CODE_OAUTH_TOKEN mounted.

    PYTHONPATH=src uv run python ops/probes/token_meter.py --nonce UNIQUE --output result.json

Retain the nonce-labelled result for QA. Set _meter.SETUP_TOKEN_USAGE_SUPPORTED
from its accepts_token field only after the coordinator reviews the live evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from flyte_agent_factory import _meter


async def probe(nonce: str) -> dict[str, Any]:
    """Return whether the usage endpoint accepts a setup token, with the meter it returned."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("mount CLAUDE_CODE_OAUTH_TOKEN before running the probe")
    supported = _meter.SETUP_TOKEN_USAGE_SUPPORTED
    # The probe must make the request even before production enables this credential.
    _meter.SETUP_TOKEN_USAGE_SUPPORTED = True
    try:
        meter = await _meter.read_claude(token, setup_token=True)
    except _meter.MeterError as exc:
        refused = "(401)" in str(exc) or "HTTP 403" in str(exc)
        return {"nonce": nonce, "accepts_token": False if refused else None, "error": str(exc)}
    finally:
        _meter.SETUP_TOKEN_USAGE_SUPPORTED = supported
    return {
        "nonce": nonce,
        "accepts_token": meter.five_hour_pct is not None and meter.weekly_pct is not None,
        "meter": meter.to_dict(),
    }


def main() -> None:
    """Run the probe and write its JSON result to `--output`, which must not exist."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", args.nonce):
        parser.error("nonce must be 8-64 letters, digits or hyphens")
    with args.output.open("x") as output:
        json.dump(asyncio.run(probe(args.nonce)), output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
