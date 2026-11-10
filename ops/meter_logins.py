"""Measure the work token directly, whether or not a project is running on it.

    python ops/meter_logins.py

One tiny run mounts the token's secret and returns only the parsed meter, never the token
(`_factory.read_login_meter`). Like `operate.py launch` this is a top-level run, not a child of the
operator, so there is no `attach()`.

Every slot, sub-agent and bench case shares the token, so this is the reading for all of them.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timedelta
from typing import Protocol

import flyte
from flyte import remote

from flyte_agent_factory import _profile
from flyte_agent_factory._errors import REMOTE_ERRORS

_profile.select_from_argv()  # before _factory reads the profile at import

from flyte_agent_factory import _factory, _factory_base, _factory_meters, _meter  # noqa: E402 -- must follow select_from_argv() so the profile is chosen first

# Longer than a probe needs to start; it is a bound, not a budget.
QUEUE_BOUND = timedelta(minutes=5)


def _init() -> None:
    flyte.init_from_api_key(
        api_key=os.environ["_UNION_EAGER_API_KEY"], project=_factory.PROJECT, domain=_factory.DOMAIN
    )


def _account(token: str) -> str:
    return f"claude {token}"


def _submit(token: str, *, interruptible: bool) -> remote.Run:
    task = _factory.read_login_meter.override(
        secrets=[flyte.Secret(key=token, as_env_var="CLAUDE_CODE_OAUTH_TOKEN")],
        interruptible=interruptible,
        timeout=flyte.Timeout(max_queued_time=QUEUE_BOUND),
    )
    return flyte.with_runcontext(mode="remote", project=_factory.PROJECT, domain=_factory.DOMAIN).run(
        task, account=_account(token)
    )


class _Submit(Protocol):
    def __call__(self, token: str, *, interruptible: bool) -> remote.Run: ...


def measure(
    token: str,
    submit: _Submit = _submit,
    *,
    cached: _meter.Meter | None = None,
) -> _meter.Meter:
    """Return the token's meter; spot first, then on-demand, each bounded in the queue."""
    if cached is not None and time.time() - cached.observed_at < _meter.MIN_INTERVAL_S:
        return cached
    for interruptible in (True, False):
        run = submit(token, interruptible=interruptible)
        run.wait(quiet=True)
        try:
            run.raise_for_status()
        except flyte.errors.MaxQueuedTimeExceededError:
            continue
        (payload,) = run.outputs()
        return _meter.Meter.from_dict(json.loads(payload))
    raise RuntimeError("the probe could not be placed on spot or on-demand capacity")


def _line(token: str, submit: _Submit, cached: _meter.Meter | None = None) -> str:
    try:
        return _meter.format_line(measure(token, submit, cached=cached))
    except (*REMOTE_ERRORS, _meter.MeterError) as e:
        return f"{_account(token)}: unavailable ({type(e).__name__}: {str(e)[:200]})"


def main(argv: list[str] | None = None, submit: _Submit = _submit) -> None:
    """Measure the work token's meter and print the reading."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", help="deployment profile name or path (default: $AGENT_FACTORY_PROFILE)")
    parser.parse_args(argv)
    _init()
    token = _factory_base.PROFILE.secrets.claude_token
    # Pods key a setup-token's readings by its fingerprint, never by the bare secret name.
    cached = _factory_meters.token_meter(token, _factory_meters.login_meters().values())
    print(_line(token, submit, cached))


if __name__ == "__main__":
    main()
