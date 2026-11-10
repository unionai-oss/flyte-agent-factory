#!/usr/bin/env bash
# Store a `claude setup-token` token as the Flyte secret the profile names in [secrets] claude_token.
#
# Run `claude setup-token` first and have the token to hand. It is read from the terminal without
# echo, or from CLAUDE_CODE_OAUTH_TOKEN if that is set, and never printed.
#
#   ./scripts/export_local_credentials.sh                 # create/update the secret
#
# The secret is project-scoped, and the agent does not run in the project your
# ~/.union/config.yaml defaults to, so the scope is passed explicitly rather
# than inherited. Set FLYTE_PROJECT / FLYTE_DOMAIN to override.
#
# There is deliberately no flag that prints the token. Nothing needs the plaintext but
# `flyte create secret`, which reads it from a 0600 temp file below.
set -euo pipefail

# Set CLAUDE_TOKEN_SECRET to the name in the profile's [secrets] claude_token.
SECRET_NAME="${CLAUDE_TOKEN_SECRET:-claude_setup_token}"
PROJECT="${FLYTE_PROJECT:?"set FLYTE_PROJECT (the profile's [backend] project)"}"
DOMAIN="${FLYTE_DOMAIN:?"set FLYTE_DOMAIN (the profile's [backend] domain)"}"
SCOPE=(-p "$PROJECT" -d "$DOMAIN")

read_token() {
    if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
        printf '%s' "$CLAUDE_CODE_OAUTH_TOKEN"
        return 0
    fi
    local token
    read -r -s -p "Paste the 'claude setup-token' token: " token </dev/tty >&2
    echo >&2
    printf '%s' "$token"
}

token="$(read_token)"
if [[ -z "$token" ]]; then
    echo "no token given. Run 'claude setup-token' first." >&2
    exit 1
fi

echo "==> storing the token as Flyte secret '$SECRET_NAME' in $PROJECT/$DOMAIN"

# Via a file rather than a pipe: `--from-file` stats its argument, which rules
# out /dev/stdin, and passing the token as `--value` would put it in this
# process's argv where `ps` can read it.
tmp="$(mktemp)"
chmod 600 "$tmp"
trap 'rm -f "$tmp"' EXIT
printf '%s' "$token" > "$tmp"

# `flyte create secret` refuses to overwrite, so replace rather than branch on
# whether this is the first run. The delete is asynchronous -- it returns while
# the secret is still "scheduled for deletion", and a create against that state
# is rejected -- so wait for it to actually go before creating.
flyte delete secret "${SCOPE[@]}" "$SECRET_NAME" >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
    flyte get secret "${SCOPE[@]}" "$SECRET_NAME" >/dev/null 2>&1 || break
    sleep 2
done
flyte create secret "${SCOPE[@]}" "$SECRET_NAME" --from-file "$tmp"

echo "==> done. Verify with: flyte get secret ${SCOPE[*]} $SECRET_NAME"
