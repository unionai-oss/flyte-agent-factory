"""Print the newest rows of a run's report, as text: what `flyte get logs` should have shown."""

from __future__ import annotations

import os
import re
import sys

import flyte
from flyte import remote

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first


def main() -> None:
    """Print the newest rows and the status of the report of the run named in argv[1]."""
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"],
        project=_factory.PROJECT,
        domain=_factory.DOMAIN,
    )
    html = str(remote.Run.get(sys.argv[1]).get_report())
    html = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    rows = re.findall(r"<tr><td><code>(.*?)</code></td><td>(.*?)</td><td>(.*?)</td></tr>", html, re.DOTALL)
    for at, icon, text in rows[: int(sys.argv[2]) if len(sys.argv) > 2 else 15][::-1]:
        print(at, icon, re.sub(r"\s+", " ", text)[:600])
    status = re.search(r"<b>Status:</b>(.*?)</p>", html, re.DOTALL)
    print("STATUS:", status.group(1).strip() if status else "?")


if __name__ == "__main__":
    main()
