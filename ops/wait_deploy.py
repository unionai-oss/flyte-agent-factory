"""Block until the first slot's project task has a version other than --current, then print it."""

import argparse
import os
import time

import flyte
from flyte import remote

from flyte_agent_factory import _profile

_profile.select_from_argv()

from flyte_agent_factory import _factory, _factory_base  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first

p = argparse.ArgumentParser()
p.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
p.add_argument("--current", required=True)
p.add_argument("--every", type=int, default=600)
p.add_argument("--max", type=int, default=7000)
a = p.parse_args()
flyte.init_from_api_key(
    api_key=os.environ["_UNION_EAGER_API_KEY"], project=_factory.PROJECT, domain=_factory.DOMAIN
)
end = time.time() + a.max
while time.time() < end:
    v = remote.Task.get(_factory_base.SLOTS[0], auto_version="latest").fetch().version
    if v != a.current:
        print("NEW VERSION", v)
        break
    time.sleep(a.every)
else:
    print("no new deploy yet")
