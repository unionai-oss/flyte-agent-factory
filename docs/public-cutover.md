# Cutting over to the public repository

The checklist for publishing the engine as `unionai-oss/flyte-agent-factory`. **The owner runs it.** Nothing in
the engine pushes to a `unionai-oss` repository.

The public repository starts with **fresh history**: one commit holding the current tree. The private
repository's history (operational notes, review rounds, owner identifiers in old commits) stays private.

## 1. Before: make the tree publishable

- [ ] Every PR for issue #110 merged: CI minutes ([ci.md](ci.md)), the #108 follow-ups, this checklist and
      `ops/public_tree.py`.
- [ ] The decision on the CI signal recorded ([ci-signal-options.md](ci-signal-options.md)).
- [ ] Owner material is out of the engine. Your deployment profiles (`profiles/<name>.toml`, git-ignored) and
      your briefs and history live in your workspace repository ([architecture-repos.md](architecture-repos.md));
      `.claude-flyte/` (project harness files) is tracked here but left out of the public tree by
      `ops/public_tree.py` (`PRIVATE`), and so would root `standards/` be (removed in #122): the engine no
      longer reads it (the workspace template carries the example set, [standards.md](standards.md)).
- [ ] Your deployment profile sets `[standards] dir` to your workspace's `standards/` (or reads its profile
      from a direction repository that has one): without it the engine applies no rules.
- [ ] The personal-literal guard names no one. It checks generic shapes (`ops/personal_literals.py`: a
      Union tenant host, a Union-managed bucket, AWS account ids) and your own identifiers from a private
      list, one regex per line, in the file `FACTORY_OWNER_LITERALS` names. The checks of your private
      profiles in `tests/test_profile.py` read their values from those profiles. Keep the list outside the
      repository, and give it to CI as a repository secret of the same name on both repositories (the fast
      lane writes it to a file); without it CI checks the generic shapes only:

  ```bash
  FACTORY_OWNER_LITERALS=~/private/owner-literals.txt PYTHONPATH=src uv run python -m pytest tests/test_profile.py -q -k literal
  gh secret set FACTORY_OWNER_LITERALS -R unionai-oss/flyte-agent-factory < ~/private/owner-literals.txt
  ```

  The list this run was built with is the one `tests/test_profile.py` held as `_LITERALS` until #110: take it
  from the private repository's history before archiving it.
- [ ] The dry run passes on main's head, from a clean checkout:

  ```bash
  git switch main && git pull --ff-only
  python ops/public_tree.py dry-run /tmp/public/flyte-agent-factory
  ```

  Run it with `FACTORY_OWNER_LITERALS` set, so the scan looks for your identifiers as well as the generic
  shapes (it prints which it used). It builds the tree (`git archive HEAD` minus `PRIVATE` and whatever the
  tree's own `.gitignore` excludes, such as the generated skill archive; one commit, a neutral author, no
  remote), runs `ops/scan_public.py` (gitleaks over the files and the one-commit history, and the
  personal-literal list) inside it, then `factory-improvement/checks.sh`, then the slow lane after it, and ends
  `dry run PASSED`. It needs `uv`, `gitleaks` and `terraform` on `PATH` (without Terraform the Terraform tests
  skip rather than run).
  Last run, 2026-11-08, on 674f2db (this paragraph's commit differs from it only by this paragraph), from a
  relative destination (`dry-run relative-public`, resolved to an absolute path before anything runs): 485 files,
  one commit, neutral author, no remote; `scan_public` read all 485 files and was clean on the generic shapes only
  (`FACTORY_OWNER_LITERALS` was not available to that run; gitleaks 8.30.1 over the files and the history); the
  checks ran in a temporary clone of the tree: the fast checks 6153 passed (28 skipped: the owner's private
  profiles, live and billed calls, and the harness `checks.sh` tests, which have no harness script in the public
  tree), ruff clean; the slow lane 160 passed, 1 xpassed; `dry run PASSED: scan_public=0 checks=0 slow_lane=0
  tree_as_built=0`, and afterwards the tree held exactly its commit (`git status --porcelain --ignored` empty, no
  `__pycache__`). A second dry run into the same directory stopped before touching it (`is not empty`, exit 2).
  With the owner list: CI's `Scan the public tree` step on 674f2db (`Tests (not slow)`, job 128802734840) printed
  `built ... (485 files)`, loaded 13 owner literals from the repository secret, read all 485 files, clean. An
  earlier local run used a list of 27; check that the secret holds the whole list, and rerun with it before
  publishing. The scan refuses a root that is not a directory, or a non-empty one in which it read no file, so a
  wrong path cannot pass as clean.
  Two timing tests can fail while both lanes share four CPUs and pass alone:
  `test_limit_resume.py::test_stale_notice_after_carry_on_does_not_repeat_resume[20000]` and
  `test_codex_author.py::test_stop_drains_final_output_and_acknowledges_with_detached_stdout[True-False]`;
  rerun before reading a failure there as a real one.
- [ ] Read the tree once by eye: `git -C /tmp/public/flyte-agent-factory ls-files | less`. Anything you would not
      put on a slide, take out of the engine first and rerun.

## 2. Publish

- [ ] Create `unionai-oss/flyte-agent-factory`, **empty** (no README, licence or `.gitignore`: the tree has them),
      public, default branch `main`.
- [ ] Push the dry run's tree, which has exactly one commit and no remote (`git log --oneline` prints one line):

  ```bash
  cd /tmp/public/flyte-agent-factory
  git log --oneline
  git remote add origin git@github.com:unionai-oss/flyte-agent-factory.git
  git push -u origin main
  ```

- [ ] Settings on the new repository:
  - Actions enabled; on a public repository GitHub-hosted runners are free.
  - The repository secret `FACTORY_OWNER_LITERALS` (above).
  - "Require approval for all outside collaborators" for fork pull-request workflows.
  - Private vulnerability reporting enabled (Security tab), which [SECURITY.md](../SECURITY.md) points to.
  - Branch protection or a ruleset on `main` once you want it: [branch-protection.md](branch-protection.md).
    Required checks, if any: `Changes`, `Tests (not slow)`, `Tests (slow)`, `Lint` (a skipped job counts as
    passing).
- [ ] Tag the first release (`v0.1.0` or as decided) so deployments can pin it.

## 3. Point the factory at it

- [ ] The GitHub App: install it on `unionai-oss/flyte-agent-factory` (Contents, Pull requests, Issues, Checks:
      read and the permissions [setup.md](setup.md) lists), or create an App owned by the organisation.
      `[github] app_id` and `private_key_secret` change if the App does.
- [ ] Your profile: `[repo] factory` (and any `[repo_keys]` entry) to `git@github.com:unionai-oss/flyte-agent-factory.git`;
      a new deploy key for it (`[repo_keys]`), added to the new repository with write access only if the factory
      is to open pull requests there.
- [ ] The deployer: it deploys the engine's main from `[repo] factory`; check `[deploy]` and, with a direction
      repository, `[direction]`, then let one tick run and read its log (`ops/deploy_status.py`).
- [ ] The webhook, if deployed: add the App's webhook events for the new repository
      ([deploy-webhook.md](deploy-webhook.md)).
- [ ] Confirm the public URLs used by the guided setup resolve for an unauthenticated reader:
      `https://github.com/unionai-oss/flyte-agent-factory` and
      `https://github.com/unionai-oss/flyte-agent-workspace`. The template repository must also be populated
      (the workspace layout, not an empty repository) and marked as a template, so its **Use this template**
      button and `gh repo create --template` work. The docs name these public homes; their presence in a
      guide is not evidence that publication has happened.

## 4. After

- [ ] Archive the private repository, or keep it as the place for owner-only history; either way stop merging
      engine changes there, so the two do not diverge.
- [ ] Open a first issue on the public repository for anything left from [install-audit.md](install-audit.md)'s
      "Still to prove externally".

## If something is wrong after publishing

A secret that reached the public repository is compromised whatever happens next: **rotate it first**
([secrets.md](secrets.md)), then remove it. Removing it from a one-commit history means building the tree again
and force-pushing, which is cheap only before others have cloned.
