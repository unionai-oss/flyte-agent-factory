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
- [ ] The tree builds and scans clean from main's head, from a clean checkout:

  ```bash
  git switch main && git pull --ff-only
  python ops/public_tree.py build /tmp/public/flyte-agent-factory
  python ops/scan_public.py /tmp/public/flyte-agent-factory
  ```

  Run it with `FACTORY_OWNER_LITERALS` set, so the scan looks for your identifiers as well as the generic
  shapes (it prints which it used). `build` makes the tree (`git archive HEAD` minus `PRIVATE` and whatever the
  tree's own `.gitignore` excludes; one commit, a neutral author, no remote); `ops/scan_public.py` runs gitleaks over
  the files and the history, and the personal-literal list. It needs `gitleaks` on `PATH`. CI runs the same scan
  (`Scan the public tree`). The tests run in the public repository's own CI.

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
    The required checks are listed there (they follow the jobs of `.github/workflows/checks.yml`).
- [ ] Tag the first release (`v0.1.0` or as decided) so deployments can pin it.

## 3. Point the factory at it

The public repository is a target like any other: **no App, deploy key or webhook is installed or added on it**,
and the owner is not asked to administer it. Changes reach it as promotion PRs that the owner merges
([Promotion](architecture-repos.md#promotion)). The deployer reads it anonymously and deploys its default branch
once its CI has passed.

1. Deploy this release from the current repository as today, with the profile unchanged (no `[deploy] repo` yet).
   An older deployer rejects the new key.
2. Bootstrap the public repository from this one, as a dry run and then for real; the owner merges the PR:

   ```bash
   PYTHONPATH=src python ops/public_tree.py build /tmp/pub
   PYTHONPATH=src python ops/promote.py deliver /tmp/pub engine-as-target --base ab86dc3 \
       --to https://github.com/unionai-oss/flyte-agent-factory.git --dry-run
   ```

   Then the same command without `--dry-run` (add `--fork <your fork>` if you cannot push branches to it).
3. Import it into the workspace. `import` makes the commit itself (the tree and `promote.toml`, one commit in the
   workspace). Then add the one-line harness `checks.sh` (`exec bash "$(dirname "$0")/factory-improvement/checks.sh" "$@"`),
   commit it, and push the workspace; the file must be committed and pushed, or the project has no declared checks:

   ```bash
   PYTHONPATH=src python ops/promote.py import https://github.com/unionai-oss/flyte-agent-factory.git projects/flyte-agent-factory
   ```

4. **Operator action** (check a tick in `ops/deploy_status.py`): in the
   private workspace's `profile.toml` set `[deploy] repo` to `https://github.com/unionai-oss/flyte-agent-factory`
   and `webhook = false` (pushes to a repository without the App never reach the webhook, so the deployer polls).
   The deploy lock lives in `[direction] repo`, which the App is already installed on. This switch is made by the
   chat operator, not by the engine, and is not done until the status line shows the public head deployed:
   record that line.
5. From then on: project in the workspace, review, `deliver`, PR CI green, the owner merges, the deployer deploys.
   Evidence for each change: the PR URL, its CI run and the `ops/deploy_status.py` line, in a `flyte-note`.

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
