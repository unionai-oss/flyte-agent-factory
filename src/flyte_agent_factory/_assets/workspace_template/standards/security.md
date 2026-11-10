# security


## SEC-001 MUST
Read secrets from a secret store; never embed them in code, logs, history or images.

Why: Published credentials cannot be recalled.

Check: Run a secret scanner over the tree and history and inspect image build arguments.

Enforced: judgement

## SEC-002 MUST
Grant only required IAM roles, token scopes and filesystem permissions.

Why: Compromise must have a bounded impact.

Check: List configured IAM actions, token scopes and filesystem modes; map each to an exercised operation and flag grants without a required operation.

Enforced: judgement

## SEC-003 MUST
Validate untrusted inputs at each trust boundary.

Why: Internal assumptions do not constrain callers.

Check: Exercise malformed, oversized and out-of-range inputs at public entry points.

Enforced: judgement

## SEC-004 MUST
Use argument arrays, parameterised SQL and confined paths for untrusted values.

Why: String interpolation enables injection.

Check: Inspect subprocess, query and file access calls and test shell metacharacters and traversal inputs.

Enforced: judgement

## SEC-005 MUST
Pin dependencies and images to reviewed versions or digests from trusted sources.

Why: Build inputs are executable supply-chain inputs.

Check: Confirm clean installs use the committed lockfile, images use reviewed immutable digests, and each package index or image registry matches the documented trusted sources.

Enforced: judgement

## SEC-006 MUST
Require authentication and deny unauthorised requests by default.

Why: Accidental exposure is a breach.

Check: Call every network endpoint without credentials and with insufficient privileges; document intentional public endpoints.

Enforced: judgement

## SEC-007 MUST
Minimise personal data and restrict its storage, retention and output.

Why: Data not retained cannot leak.

Check: Inspect schemas, output samples and retention settings against documented needs.

Enforced: judgement

## SEC-008 MUST
Use TLS with certificate validation across untrusted networks.

Why: Transport interception exposes credentials and data.

Check: Inspect client verification settings and service TLS configuration.

Enforced: judgement

## SEC-009 MUST
Enable encryption at rest for generated persistent infrastructure.

Why: Lost storage must not reveal plaintext.

Check: Inspect storage encryption settings in the infrastructure plan.

Enforced: judgement

## SEC-010 MUST
Do not expose administrative ports to 0.0.0.0/0 or ::/0.

Why: Administrative access needs a restricted boundary.

Check: Inspect firewall and security-group plans for globally open admin ports.

Enforced: judgement

## SEC-011 SHOULD
Audit security-relevant actions without sensitive payloads.

Why: Incidents need an attributable timeline.

Check: Trigger an access change and confirm actor, action, target, time and outcome are recorded.

Enforced: judgement
