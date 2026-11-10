The slot and sub-agent image carries the tools required by the shipped standards:
Ruff, mypy, pytest, ShellCheck, Terraform, TFLint, TypeScript, ESLint and Prettier.
Node is present even when the browser is disabled. Python tools live in
`/opt/gate-tools`, independently of the factory and project environments.
It also bakes gitleaks, which the factory's own scanner tests run. Versions and installation steps are defined in
`src/flyte_agent_factory/_images.py`, except gitleaks' and TFLint's, pinned with their SHA-256 in
`src/flyte_agent_factory/_scanners.py` (see [CI: scanners](ci.md#scanners-gitleaks-and-tflint)).

At supervisor startup, missing tools required by the applicable MUST standards
produce an image defect in the timeline and snapshot risks. Gate reports also
name missing tools as image defects with the rule IDs that require them. These
reports do not block DONE: fixing the image requires a factory deployment.

The image definition guard checks all shipped tool commands against the install
steps, including language packs that are not applicable to this repository.
