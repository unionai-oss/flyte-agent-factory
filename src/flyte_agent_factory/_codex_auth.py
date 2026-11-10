"""The Codex (ChatGPT) auth file: telling a dead credential from other failures.

Codex runs on either a long-lived access token (`[secrets] codex_token`, a ChatGPT workspace token) that
never refreshes, so a refusal means the token must be replaced, or a ChatGPT login (`[[slots]] codex_login`,
an `auth.json` from `codex login --device-auth`, which works on personal plans). A login rotates its refresh
token on every refresh, so it belongs to one slot and its author writes each refresh back to the secret.
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime

#: Codex's own words (stderr or `error` / `turn.failed` events) for a login that cannot be refreshed.
#: Narrow on purpose: tool output and the model's text must not match, and a bare "401" is not here
#: because Codex prints it for every websocket reconnect attempt, most of which succeed. `invalid_grant`
#: is any OAuth server's refusal (an MCP server's own sign-in too), so it counts only on a line that is
#: about Codex's refresh: "refresh" or OpenAI's token endpoint beside it.
REFRESH_FAILURE = re.compile(
    r"refresh token (was already used|has expired|was revoked|has been revoked)"
    r"|refresh_token_(reused|expired|invalidated)"
    r"|(refresh|auth\.openai\.com)[^\n]*invalid_grant|invalid_grant[^\n]*(refresh|auth\.openai\.com)"
    r"|please log out and sign in again",
    re.IGNORECASE,
)
#: The final failure of a turn that Codex could not authenticate at all.
_FINAL_UNAUTHORIZED = re.compile(r"401 unauthorized|token_expired|token has expired", re.IGNORECASE)

#: A sub-agent whose access token ends sooner than this is routed to Claude instead: it cannot refresh.
MIN_ACCESS_LIFE_S = 20 * 60


def is_auth_failure(text: str, *, final: bool = False) -> bool:
    """Whether `text` says the login is dead. `final` is the message of a `turn.failed` event."""
    return bool(REFRESH_FAILURE.search(text) or (final and _FINAL_UNAUTHORIZED.search(text)))


def access_expiry(auth_json: str) -> float | None:
    """Return when the access token in an `auth.json` expires (epoch seconds), or None if unreadable."""
    try:
        token = json.loads(auth_json)["tokens"]["access_token"]
        payload = token.split(".")[1]
        expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
    if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires != expires:
        return None
    return float(expires)


def strip_refresh_token(auth_json: str) -> str:
    """Return the login without its refresh token, so a copy cannot rotate the chain.

    The field is emptied, not removed: Codex fails to parse a file with no `refresh_token`, but
    accepts an empty one and simply cannot refresh with it.
    """
    try:
        data = json.loads(auth_json)
        data["tokens"]["refresh_token"] = ""
    except (ValueError, KeyError, TypeError):
        return auth_json
    return json.dumps(data)


def is_login(credential: str) -> bool:
    """Whether a mounted Codex credential is a ChatGPT login (`auth.json`) rather than an access token."""
    try:
        return isinstance(json.loads(credential).get("tokens"), dict)
    except (ValueError, AttributeError):
        return False


def last_refresh(auth_json: str) -> float:
    """Return the file's `last_refresh` (epoch seconds); 0 when it has none."""
    try:
        return datetime.fromisoformat(str(json.loads(auth_json)["last_refresh"])).timestamp()
    except (ValueError, KeyError, TypeError):
        return 0.0


def pick_fresher_login(mounted: str, on_disk: str) -> str:
    """Return the newer of two copies of one login: later refresh, then later token expiry.

    A tie goes to `on_disk`, which keeps a resumed project on its own chain. Anything that does not
    parse loses to anything that does.
    """

    def age(raw: str) -> tuple[float, float]:
        return last_refresh(raw), access_expiry(raw) or 0.0

    return mounted if age(mounted) > age(on_disk) else on_disk


def has_refresh_token(auth_json: str) -> bool:
    """Whether the login can still refresh: a stripped copy (or junk) must never replace the chain."""
    try:
        return bool(json.loads(auth_json)["tokens"]["refresh_token"])
    except (ValueError, KeyError, TypeError):
        return False


def expires_soon(auth_json: str, *, margin_s: float = MIN_ACCESS_LIFE_S) -> bool:
    """Whether a copy that cannot refresh would be dead within `margin_s`.

    An expiry that cannot be read is not evidence of anything, so it is not "soon": Codex decides.
    """
    expires = access_expiry(auth_json)
    return expires is not None and expires - time.time() < margin_s
