Run `shellcheck scripts/*.sh` (ShellCheck 0.10 or newer) and
`bash -n scripts/example.sh` for each Bash script. Use `#!/usr/bin/env bash`
and `set -euo pipefail` for Bash entry points. Check prerequisites with
`command -v` before mutation. Use `mktemp` with an EXIT cleanup trap and
explicit connection and overall timeouts for network commands. Strict mode
needs explicit status checks in conditionals and other errexit exceptions.
