"""Which CI jobs a change can affect, from the files it changes.

`checks.yml`'s `Changes` job runs this and `Lint` runs only when its lane is `true`. The test jobs run on
every run, whatever `fast` and `slow` say: the standards gate's test rule covers both lanes on every commit
and reads their junit (docs/ci.md). Both are still printed, for the log. A skipped
job reports a `skipped` check-run, which `_github.commit_health` counts as passing, so the rules here decide
what "green" means for the deployer and the loop's merge rule. Hence the file lives under `.github/` (an owner
path in `_loop.OWNER_PATHS`) and fails closed: whenever the change cannot be listed, every lane runs.

    python .github/ci_lanes.py --base <sha> [--head <sha>]  # base..head's lanes, as `name=true|false` lines
    python .github/ci_lanes.py --all                         # every lane (schedule, workflow_dispatch)

The lanes:

- `fast`: the `not slow` pytest lane. It reads nearly every tracked file (doc links, the personal-literal
  guard over docs, profiles and skills), so it runs for any change outside `.claude-flyte/`, which holds other
  projects' harness files.
- `lint`: the inputs of ruff (Python, the code blocks in `ops/**.md`) and shellcheck (every tracked
  `.sh`). `Lint` now runs only shellcheck over `.claude-flyte/` (the gate runs the rest): a superset.
- `slow`: the `slow` pytest lane, for a change to a file in the slow tests' static import closure or to one of
  the runtime inputs they read (`SLOW_INPUTS`). A dependency that is neither imported nor listed is missed
  here; the weekly scheduled run of every lane is what catches it.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANES = ("fast", "slow", "lint")

#: Files and prefixes every lane depends on: the CI definition itself and the test environment.
EVERYTHING = (".github/", "pyproject.toml", "uv.lock", "factory-improvement/checks.sh", "tests/conftest.py")
#: Read by the slow tests at run time rather than imported: the benchmark fixtures, the standards the gate
#: applies to them, and the image assets (the browser install the browser-gate tests drive).
SLOW_INPUTS = ("tests/fixtures/", "standards/", "src/flyte_agent_factory/_assets/", "ops/bench_seed.py")
#: Not read by any test: other projects' harness files.
INERT = (".claude-flyte/",)

_SLOW_MARK = re.compile(r"\bmark\.slow\b")
#: `python -m flyte_agent_factory.<module>` and other by-name loads of a package module.
_BY_NAME = re.compile(r"[\"'](flyte_agent_factory\.[A-Za-z_][\w.]*)")


def _module_file(root: Path, name: str) -> Path | None:
    parts = name.split(".")
    for base in (root / "src", root, root / "tests"):
        for candidate in (base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts, "__init__.py")):
            if candidate.is_file():
                return candidate
    return None


def _imports(path: Path, root: Path) -> set[str]:
    """Return the module names `path` imports, statically or as a quoted `flyte_agent_factory.<module>`."""
    text = path.read_text()
    names = set(_BY_NAME.findall(text))
    parts = path.relative_to(root).parts[:-1]
    package = ".".join(parts[1:] if parts[:1] == ("src",) else parts)
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                module = ".".join([*anchor, module] if module else anchor)
            names.add(module)
            # `from pkg import mod` names a module as often as an attribute.
            names.update(f"{module}.{alias.name}" for alias in node.names)
    expanded = set()
    for name in names:
        parts = name.split(".")
        expanded.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return expanded


def slow_tests(root: Path = ROOT) -> list[Path]:
    """Return the test files that hold a test marked `slow`."""
    return sorted(p for p in (root / "tests").rglob("test_*.py") if _SLOW_MARK.search(p.read_text()))


def slow_closure(root: Path = ROOT) -> set[str]:
    """Repo-relative paths of the slow tests and every repository module they import, transitively."""
    seen: set[Path] = set()
    todo = slow_tests(root)
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imports(path, root):
            found = _module_file(root, name)
            if found is not None and found not in seen:
                todo.append(found)
    return {p.relative_to(root).as_posix() for p in seen}


def lanes(changed: list[str], root: Path = ROOT) -> dict[str, bool]:
    """Which lanes the changed paths (repo-relative, POSIX) can affect."""
    if any(p.startswith(EVERYTHING) or p in EVERYTHING for p in changed):
        return dict.fromkeys(LANES, True)
    live = [p for p in changed if not p.startswith(INERT)]
    closure = slow_closure(root) if live else set()
    return {
        "fast": bool(live),
        "slow": any(p.startswith(SLOW_INPUTS) or p in closure for p in live),
        "lint": any(
            p.endswith((".py", ".sh")) or (p.startswith("ops/") and p.endswith(".md")) for p in changed
        ),
    }


def changed_files(base: str, head: str, root: Path = ROOT) -> list[str] | None:
    """Paths that differ between `base` and `head`, or None when that cannot be told (fail closed)."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", base) or not base.strip("0"):
        return None  # a new branch's push has an all-zero `before`

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)

    if git("cat-file", "-e", f"{base}^{{commit}}").returncode != 0:
        # A shallow checkout: fetch the base alone. After a force push it may be gone, which fails closed.
        if git("fetch", "--no-tags", "--depth=1", "origin", base).returncode != 0:
            return None
    # NUL-delimited: without -z git quotes non-ASCII names ("ops/caf\303\251.py"), so no suffix matches.
    diff = git("diff", "--name-only", "-z", "--no-renames", base, head)
    if diff.returncode != 0:
        return None
    return [name for name in diff.stdout.split("\0") if name]


def main(argv: list[str] | None = None) -> int:
    """Print `lane=true|false` for each lane; return 0."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="the commit the change is measured from")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all", action="store_true", help="run every lane")
    args = parser.parse_args(argv)
    changed = None if args.all or not args.base else changed_files(args.base, args.head)
    result = dict.fromkeys(LANES, True) if changed is None else lanes(changed)
    reason = "every lane" if changed is None else f"{len(changed)} changed file(s)"
    print(f"lanes for {reason}: {result}", file=sys.stderr)
    for path in changed or []:
        print(f"  {path}", file=sys.stderr)
    for lane in LANES:
        print(f"{lane}={'true' if result[lane] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
