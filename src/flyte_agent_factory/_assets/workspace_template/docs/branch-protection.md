# Branch protection for this workspace

The factory writes to `factory/<name>` branches and nowhere else. These settings keep it out of the files that
are yours.

1. **Protect `main`.** Require a pull request before merging; no direct pushes, including administrators if you
   can afford it. The factory's deploy key can then only create `factory/*` branches that you merge.
2. **Require review from Code Owners.** `.github/CODEOWNERS` assigns `standards/`, `waivers.toml` and
   `gardening.toml` to you (replace `OWNER` with your login first). With the rule on, no pull request that
   touches them merges without your approval, whoever authored it.
3. **Restrict who can push to `main` and who can bypass.** Leave the factory's deploy key and any App out of both lists.
4. **Keep this repository private if `profile.toml` is in it.** Only the template is public.

The factory also enforces the same boundary itself: its gate rejects a patch that touches `standards/`, and a
session may not edit it. Branch protection is the second line, for a change made by anything else.
