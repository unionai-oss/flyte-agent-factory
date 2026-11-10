# languages/typescript

detect: package.json, **/*.ts, **/*.tsx

Copy the matching baseline from `configs/` into your project.

## TS-001 MUST
Type-check without emitting output.

Why: Type errors must fail the build.

Check: tsc --noEmit

Enforced: command tsc --noEmit

## TS-002 MUST
Run ESLint and Prettier checks.

Why: Shared tooling makes style review repeatable.

Check: eslint . && prettier --check .

Enforced: command eslint . && prettier --check .

## TS-003 MUST
Enable strict TypeScript checking for application source.

Why: Implicitly nullable or untyped values move failures to runtime.

Check: Run npx tsc --showConfig and confirm strict is true with no disabled strict sub-options; confirm source files are included.

Enforced: judgement

## TS-004 MUST
Use unknown and narrow it instead of any unless an exception is justified.

Why: Any bypasses checks for all downstream consumers.

Check: Run typed ESLint and inspect explicit any, assertions and suppression comments; require a specific reason for each escape hatch.

Enforced: judgement

## TS-005 MUST
Handle discriminated unions exhaustively.

Why: New variants must produce a compile-time review signal.

Check: Run ESLint switch-exhaustiveness-check; inspect non-switch dispatch for a never assertion and reject defaults that silently swallow new variants.

Enforced: judgement

## TS-006 MUST
Await or return promises and handle rejected background work explicitly.

Why: Unobserved rejections can drop work or terminate the process.

Check: Run ESLint no-floating-promises and no-misused-promises; inspect intentional background tasks for an attached error handler and exercise rejection.

Enforced: judgement

## TS-007 MUST
Validate untrusted data at runtime before using domain types.

Why: Type assertions do not validate network, storage or user input.

Check: Inspect input boundaries for schema validation such as Zod or an existing equivalent; test malformed and missing fields and confirm rejection before side effects.

Enforced: judgement

## TS-008 MUST
Commit the package-manager lockfile and use frozen installs in CI.

Why: Dependency resolution must be repeatable.

Check: Run npm ci or the package manager equivalent with frozen lockfile enforcement; confirm no lockfile changes.

Enforced: judgement

## TS-009 MUST
Keep secrets out of client bundles.

Why: Browser-delivered values are public even when named as environment variables.

Check: Inspect client imports and environment substitutions; build production assets with a synthetic secret marker and confirm the marker is absent from bundles and source maps.

Enforced: judgement

## TS-010 MUST
Audit production dependencies and resolve or document findings.

Why: Pinned packages can still contain known vulnerabilities.

Check: Run npm audit --omit=dev or the package manager equivalent; record each unresolved advisory with an owner, reason and review date.

Enforced: judgement

## TS-011 MUST
Test delivered behaviour and async failure paths.

Why: Type checking cannot prove runtime behaviour.

Check: Run the documented test command and inspect tests for rejected requests, invalid boundary inputs and cleanup after failure.

Enforced: judgement

## TS-012 SHOULD
Narrow nullable values rather than relying on non-null assertions.

Why: Assertions hide missing data instead of handling it.

Check: Inspect non-null assertions and unchecked index access; confirm a runtime guard or documented invariant at each use.

Enforced: judgement
