"""Build an agent-workspace from the template, a sandbox checkout and a direction checkout.

`migrate_workspace.py --sandbox <path> --direction <path> --out <path> [--dry-run]`

Mapping:
- the sandbox's `projects/<name>/` and its top-level project directories -> `projects/<name>/`
  (their `.git` is left behind)
- the direction repo's profile (`profiles/*.toml` but `example*.toml`; `--profile` if there are several) ->
  `profile.toml`. It is the owner's: it goes into the new private workspace, never into the template
- every `briefs/*.md` and `ops/briefs/**/*.md` whose name the engine does not ship in its own `briefs/` ->
  `projects/<name>/BRIEF.md` when that project exists, else `briefs/`; `ops/briefs/factory/*` ->
  `history/briefs/factory/`
- the history documents (OPERATIONS.md, ACCEPTANCE.md, PR_DESCRIPTION.md, or their `docs/` spellings)
  -> `history/`
- `MIGRATION.md`: the manual steps the script cannot do

`--dry-run` prints the plan and writes nothing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from flyte_agent_factory import _workspace

#: The engine checkout this script ships in; its `briefs/` are the product briefs that are not carried over.
ENGINE_ROOT = Path(__file__).resolve().parents[1]
#: Top-level sandbox entries that are not projects.
_NOT_PROJECTS = {"projects", "standards", "briefs", "docs", "history", "node_modules"}
#: Each history document under its legacy spelling, then its `docs/` one.
_HISTORY_DOCS = (
    ("OPERATIONS.md", "docs/operations-log.md"),
    ("ACCEPTANCE.md", "docs/acceptance-criteria.md"),
    ("PR_DESCRIPTION.md", "docs/pr-description.md"),
)
CHECKLIST = """\
# Manual steps

The script copied files; these it cannot do. In this order:

1. Review `profile.toml`. Its `[repo] project` still names the old sandbox URL: that is the allowed-repo
   string. Keep it until step 5, then change it in the same step as the rename.
2. Create the private repository `<you>/agent-workspace`, push this directory to it, and replace `OWNER` in
   `.github/CODEOWNERS`. Turn on branch protection as in `docs/branch-protection.md`.
3. Create a deploy key for the new repository only and update the profile's `[secrets] deploy_key` to the
   Flyte secret that holds it. Remove the old sandbox's key from the old repository.
4. Move the backlog: point the Projects v2 board (`[board]` in the profile) and the issues' links at the new
   repository; check `history/` for links that still name the sandbox.
5. LAST: rename the old sandbox repository to `agent-workspace` on GitHub, and in the same step set
   `[repo] project` in the profile to the new URL and redeploy. Until the rename the old repository stays
   the allowed one, so nothing is lost if you stop earlier.
"""


def _projects(sandbox: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for parent, skip in ((sandbox, _NOT_PROJECTS), (sandbox / "projects", set())):
        if not parent.is_dir():
            continue
        for p in sorted(parent.iterdir()):
            if not p.is_dir() or p.name.startswith(".") or p.name in skip:
                continue
            if p.name in found:
                # Merging could silently pick a winner for files both layouts hold; the owner decides.
                raise SystemExit(
                    f"refusing: project {p.name!r} exists in both sandbox layouts, {found[p.name]} and {p}: "
                    "merge or remove one of them, then run again"
                )
            found[p.name] = p
    return found


def _profile(direction: Path, chosen: str) -> Path:
    if chosen:
        return direction / chosen
    options = [p for p in sorted((direction / "profiles").glob("*.toml")) if not p.stem.startswith("example")]
    if len(options) != 1:
        raise SystemExit(
            f"need exactly one profile in {direction / 'profiles'}, found {len(options)}: use --profile"
        )
    return options[0]


def plan(sandbox: Path, direction: Path, profile: str) -> list[tuple[Path, str]]:
    """(source, destination relative to the workspace) for everything that is copied."""
    steps: list[tuple[Path, str]] = []
    projects = _projects(sandbox)
    steps += [(path, f"projects/{name}") for name, path in projects.items()]
    steps.append((_profile(direction, profile), "profile.toml"))
    shipped = {p.name for p in (ENGINE_ROOT / "briefs").iterdir()}
    design_notes = direction / "ops/briefs/factory"
    for brief in [
        *sorted((direction / "briefs").glob("*.md")),
        *sorted((direction / "ops/briefs").rglob("*.md")),
    ]:
        if brief.is_relative_to(design_notes):
            dest = f"history/briefs/factory/{brief.relative_to(design_notes).as_posix()}"
        elif brief.name in shipped:
            continue
        elif brief.stem in projects:
            dest = f"projects/{brief.stem}/BRIEF.md"
        else:
            dest = f"briefs/{brief.name}"
        steps.append((brief, dest))
    steps += [
        (direction / doc, f"history/{Path(doc).name}")
        for spellings in _HISTORY_DOCS
        for doc in spellings
        if (direction / doc).is_file()
    ]
    return steps


def _removed_briefs() -> list[str]:
    """Return the brief paths the engine's git history deleted, minus those it still ships."""
    try:
        log = subprocess.run(
            ["git", "log", "--diff-filter=D", "--name-only", "--format=", "--", "briefs", "ops/briefs"],
            cwd=ENGINE_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []  # not a git checkout: nothing to derive the list from
    shipped = {p.name for p in (ENGINE_ROOT / "briefs").iterdir()}
    return sorted({path for path in log.split("\n") if path and Path(path).name not in shipped})


def missing(direction: Path) -> list[str]:
    """Return the owner briefs and history documents the engine's history lists that `direction` lacks."""
    briefs = [path for path in _removed_briefs() if not (direction / path).is_file()]
    docs = [
        spellings[-1]
        for spellings in _HISTORY_DOCS
        if not any((direction / spelling).is_file() for spelling in spellings)
    ]
    return briefs + docs


def main(argv: list[str] | None = None) -> int:
    """Run the command line; return the exit status."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--direction", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="an absent or empty directory")
    parser.add_argument("--profile", default="", help="profile path inside the direction repo")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    steps = plan(args.sandbox, args.direction, args.profile)
    if absent := missing(args.direction):
        # The engine repo used to ship these; the direction checkout holds them only if the owner kept a copy.
        print(
            "warning: not in the direction checkout, so not carried over. They live in the engine's git "
            "history before they moved out; recover each with `git show <rev>:<path>` and copy it into "
            "the workspace:\n  " + "\n  ".join(absent),
            file=sys.stderr,
        )
    print(f"template -> {args.out}")
    for source, dest in steps:
        print(f"{source} -> {args.out / dest}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    try:
        _workspace.copy_template(args.out)
    except FileExistsError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    for source, dest in steps:
        target = args.out / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    (args.out / "MIGRATION.md").write_text(CHECKLIST)
    print(f"wrote {args.out}; follow {args.out / 'MIGRATION.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
