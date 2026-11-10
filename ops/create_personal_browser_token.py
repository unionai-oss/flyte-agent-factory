"""Create/rotate a personal browser identity's token; extra arguments go to `flyte create secret`.

`--identity NAME` (default `personal`) names the identity: its secret is `browser_<NAME>_token`, with
dashes in NAME written as underscores. The token is never printed.
"""

import argparse
import re
import secrets
import subprocess
import tempfile


def main() -> None:
    """Create the identity's `browser_<name>_token` secret from a fresh random token."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", default="personal")
    args, flyte_args = parser.parse_known_args()
    # The profile's rule for an identity name (`_profile._IDENTITY_NAME`); this script runs without it.
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", args.identity):
        parser.error(f"identity must match [a-z][a-z0-9-]{{0,19}}, not {args.identity!r}")
    secret = f"browser_{args.identity.replace('-', '_')}_token"
    with tempfile.NamedTemporaryFile(mode="w") as token:
        token.write(secrets.token_urlsafe(32))
        token.flush()
        subprocess.run(
            ["flyte", "create", "secret", secret, "--from-file", token.name, *flyte_args],
            check=True,
        )


if __name__ == "__main__":
    main()
