# Branch protection: require the factory's verdict before merging

This page covers two repositories. The factory's verdict on a project's repository (below) and the engine's own
CI on the factory repository ([The factory repository itself](#the-factory-repository-itself)), which applies now.

**Status of the project-repo rule: deferred to v1.** Until then nothing enforces it: a project's pull request carries a `factory/verifier`
commit status, but GitHub lets anyone merge without it.

At v1, the default branch of the profile's `[repo] project` repository requires the `factory/verifier` check. The
factory posts it itself, on the reviewed head commit of each project's pull request
(`src/flyte_agent_factory/_pull_requests.py`): `success` when review passes, `failure` when review fails or escalates.
With the rule in place, and the check restricted to the factory's App (step 5 below), a pull request can merge only
once the factory has passed its head commit. What it passed is that head, not the merge result: commits that land on
the default branch afterwards are not re-reviewed (see step 6).

The `[repo] factory` repository does not require `factory/verifier`: people also merge and push to its default
branch directly, and a required verdict would reject those pushes. It does require its own CI, below.

## Before you enable it

- **Plan.** On a GitHub Free personal account, private repositories have no branch protection or rulesets; they
  need Pro, Team or higher.
- **Direct pushes stop.** A required check also rejects a push of a commit without a passing status. Projects whose
  profile has no `[github]` table push straight to the default branch, and they will be rejected. Turn the rule on
  only once every deployment that uses this repository has a `[github]` table. A slot whose pull-request setup
  fails (for example a missing App key) falls back to the same direct push, and that push is rejected too, so the
  project cannot deliver until the setup is fixed.
- **Bypass.** Decide who may skip the rule. The strict choice is nobody: an empty bypass list, or
  `enforce_admins: true` for classic protection. Owners can otherwise merge a red pull request.
- **Classic `PUT` replaces.** The API call below overwrites any protection the branch already has; read it first
  (`gh api repos/<owner>/<repo>/branches/main/protection`).

## Who can set it

The factory's GitHub App (`[github] app_id`) has no Administration permission, so it cannot set protection: the API
answers 403. Pick one:

- **(a) An owner sets it by hand** (below).
- **(b) Grant the App "Administration: Read and write".** With an installation token minted from the App's key,
  the API call below then works for the factory as well. This lets the App change any repository setting, so prefer
  (a) unless it has to be automated.

## (a) In the GitHub UI: a ruleset

1. Repository → Settings → Rules → Rulesets → New ruleset → New branch ruleset.
2. Name it (for example `factory verdict`). Set **Enforcement status: Active**; Disabled and Evaluate enforce
   nothing.
3. Bypass list: leave it empty (strict) or add only the people allowed to merge without a verdict.
4. Target branches → Add target → **Include default branch**.
5. Enable **Require status checks to pass**. Add the check `factory/verifier` and set its source to the factory's
   App, not "any source"; anyone with write access can otherwise post a passing status by hand.
6. Leave "Require branches to be up to date before merging" off. With it on, every passed pull request blocks as
   soon as the default branch moves: updating the branch makes a new head commit the factory has not reviewed, and
   it is reviewed again only on the project's next DONE. The cost of leaving it off is that the merge result itself
   is never reviewed, only the pull request's head.
7. Create.

Classic protection (Settings → Branches → Add classic branch protection rule) works the same way: pattern `main`,
"Require status checks to pass before merging", check `factory/verifier` with the factory's App as its source. Classic protection
offers only checks that completed successfully on the repository in the last seven days, so let one factory pull
request pass review first (a draft, or a failed review, posts nothing that counts).

## (b) Through the API: classic protection

```sh
gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{"required_status_checks": {"strict": false, "checks": [{"context": "factory/verifier", "app_id": <app_id>}]},
 "enforce_admins": true, "required_pull_request_reviews": null, "restrictions": null}
JSON
```

## Verify

```sh
# A ruleset:
gh api repos/<owner>/<repo>/rules/branches/main --jq '.[] | select(.type=="required_status_checks")'
# Classic protection:
gh api repos/<owner>/<repo>/branches/main/protection/required_status_checks --jq .checks
# expect factory/verifier with the App's id
```

Then open a real project's pull request and confirm that **Merge** stays disabled until `factory/verifier` is
green.

## The factory repository itself

The engine's repository requires its own CI on `main`, the standards gate included
([`docs/ci.md`](ci.md#what-main-requires) says what each check is). These are GitHub Actions check-runs, named
exactly:

- `Changes`
- `Tests (not slow)`
- `Tests (slow)`
- `Lint`
- `Standards gate`

`tests/test_ci_workflow.py` keeps this list equal to the jobs in `.github/workflows/checks.yml`. Only a repository
admin can apply it (the factory's App has no Administration permission). The same cautions as above apply: the
classic `PUT` replaces whatever protection `main` has, so read it first; on a private repository it needs a paid
plan.

### Apply it in one step

`15368` is GitHub Actions' App id, so only Actions can satisfy the checks. `strict: false` leaves "require branches
to be up to date" off: each pull request's checks already run on its merge with the current base, and every push to
`main` runs every check again on `main` itself.

```sh
gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{"required_status_checks": {"strict": false, "checks": [
   {"context": "Changes", "app_id": 15368},
   {"context": "Tests (not slow)", "app_id": 15368},
   {"context": "Tests (slow)", "app_id": 15368},
   {"context": "Lint", "app_id": 15368},
   {"context": "Standards gate", "app_id": 15368}]},
 "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null}
JSON
```

`enforce_admins: false` keeps an admin's direct push to `main` possible, which people do today; set it `true` to hold
admins to the checks too.

In the UI instead, as classic protection (the same rule as the command): Settings → Branches → Add classic branch
protection rule, pattern `main`, **Require status checks to pass before merging**, add the five checks above with
source **GitHub Actions**, leave "Require branches to be up to date before merging" and "Do not allow bypassing the
above settings" (`enforce_admins`) off, and create. Classic protection only offers checks that completed on the
repository in the last seven days, so let one run with the `Standards gate` job finish first.

A ruleset (Settings → Rules → Rulesets) works too, but differs in two ways: it has no `enforce_admins`, so it holds
admins to the checks unless they are on its bypass list (add the repository admin role there to keep direct pushes),
and classic verification below does not see it.

### Verify

```sh
# Classic protection (the command above):
gh api repos/<owner>/<repo>/branches/main/protection/required_status_checks --jq '[.checks[].context] | sort'
# expect ["Changes","Lint","Standards gate","Tests (not slow)","Tests (slow)"]
# A ruleset:
gh api repos/<owner>/<repo>/rules/branches/main \
  --jq '[.[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context] | sort'
```
