"""Compare browser and direct public IPs inside a slot with a file-mounted auth key."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, cast

from flyte_agent_factory._local_browser import LocalBrowser, _rpc


async def probe(exit_node: str, secret: str) -> tuple[str, str]:
    """Return `(browser public IP, direct public IP)` with the browser routed through `exit_node`."""
    with tempfile.TemporaryDirectory(prefix="exit-node-probe-") as directory:
        browser = LocalBrowser(
            profile_dir=Path(directory) / "profile",
            output_dir=Path(directory) / "output",
            exit_node=exit_node,
            tailscale_secret=secret,
        )
        try:
            await browser.start()
            reply = await asyncio.to_thread(
                _rpc,
                browser.endpoint,
                [
                    (
                        "tools/call",
                        {
                            "name": "browser_run_code_unsafe",
                            "arguments": {
                                "code": "async (page) => { await page.goto('https://api.ipify.org'); "
                                "return (await page.locator('body').innerText()).trim(); }"
                            },
                        },
                    )
                ],
            )
            # _RpcReply types only the tools/list shape; a tools/call reply also carries content and errors.
            result = cast("dict[str, Any]", reply)
            if result.get("error") or result.get("result", {}).get("isError"):
                raise RuntimeError("browser public IP request failed")
            public_ip = ""
            # MCP wraps the tool's string result in text, with a Result section.
            for block in result.get("result", {}).get("content", []):
                for line in block.get("text", "").splitlines():
                    try:
                        value = json.loads(line)
                        public_ip = str(ipaddress.ip_address(value))
                    except (ValueError, TypeError):
                        continue
            if not public_ip:
                raise RuntimeError("browser did not return a public IP")

            # Explicit ProxyHandler({}) makes the comparison direct even in an operator shell with proxies.
            def direct() -> str:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open("https://api.ipify.org", timeout=30) as response:
                    return str(ipaddress.ip_address(response.read().decode().strip()))

            return public_ip, await asyncio.to_thread(direct)
        finally:
            await browser.stop()


def main() -> None:
    """Probe whether the browser's public IP differs from the direct one, and print both."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exit-node", required=True)
    parser.add_argument("--secret", required=True, help="Union secret name mounted at /etc/flyte/secrets")
    args = parser.parse_args()
    try:
        browser_ip, direct_ip = asyncio.run(probe(args.exit_node, args.secret))
    except (OSError, RuntimeError, ValueError, TimeoutError):
        parser.exit(1, "browser exit-node probe failed; check the mount, tailnet policy and exit node\n")
    print(browser_ip)
    print(direct_ip)


if __name__ == "__main__":
    main()
