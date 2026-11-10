"""Promotion: take a reviewed workspace project to another repository as a pull request.

Runs outside the factory, by the chat session or an operator, with the CALLER's git credentials (ssh agent,
`gh` login). A factory session never holds a credential for a target repository (docs/architecture-repos.md),
so nothing here reads the factory's secrets.

Patch format: `projects/<name>/patches/*.patch`, from `git format-patch` against the target's default branch,
applied in file-name order. The patches, not the project's branch, are what was reviewed.

The branch pushed is the patches applied onto the target's CURRENT default branch, so a default branch that
moved since the patches were made is rebased onto, not overwritten. A conflict, a red check of the target's
own CI, or an uncovered changed line stops promotion before anything is pushed, with a report.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import _gate_base, _gates, _target_ci, _workspace
from ._git import _repo_identity


@dataclasses.dataclass(frozen=True)
class Report:
    """How a promotion ended. `stage` is where it stopped, or `done`; `url` the PR (or compare) link."""

    ok: bool
    stage: str
    message: str
    url: str = ""


def _stop(stage: str, message: str) -> Report:
    return Report(False, stage, message)


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, proc.stdout + proc.stderr


def _check_coverage(checkout: Path, base: str, summary: str) -> str:
    """Require every changed Python line covered or listed in SUMMARY.md; return a failure report or ""."""
    everything = _gates.changed_lines(checkout, base)
    if everything is None:
        return f"cannot tell what changed since {base[:10]}"
    changed = {path: lines for path, lines in everything.items() if path.endswith(".py")}
    if not changed:
        return ""
    command = _target_ci.coverage_command(checkout)
    (checkout / ".coverage").unlink(missing_ok=True)  # a stale data file would be reported as this run's
    if failure := _gates.run_target_command(command, checkout):
        return failure
    with tempfile.TemporaryDirectory(prefix="promote-coverage-") as scratch:
        out = Path(scratch) / "coverage.json"
        code, output = _run([sys.executable, "-m", "coverage", "json", "-o", str(out)], checkout)
        if code:
            return f"`coverage json` exited {code}:\n{output[-3000:]}"
        report = json.loads(out.read_text())
    unlisted = sorted(
        _gates.uncovered_changed_lines(report, checkout, changed) - _target_ci.parse_listed_lines(summary)
    )
    if unlisted:
        return f"changed lines not covered: {', '.join(unlisted)}"
    return ""


def _open_pull_request(
    checkout: Path, repo_url: str, default: str, branch: str, title: str, body: str
) -> tuple[str, str]:
    """Open or update the PR with `gh` when it is installed; return its link and what went wrong, if anything.

    Without `gh`, or when it fails, the link is the compare URL (or a note), and the second value is the
    failed call's output so the caller sees why there is no PR.
    """
    problem = ""
    if shutil.which("gh"):
        code, existing = _run(["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"], checkout)
        if code == 0:
            code, output = _run(["gh", "pr", "edit", branch, "--title", title, "--body", body], checkout)
            if code:
                problem = f"gh pr edit failed ({code}): {output[-2000:]}"
            return existing.strip(), problem
        code, out = _run(
            ["gh", "pr", "create", "--head", branch, "--base", default, "--title", title, "--body", body],
            checkout,
        )
        if code == 0:
            return out.strip().splitlines()[-1], ""
        problem = f"gh pr create failed ({code}): {out[-2000:]}"
    identity = _repo_identity(repo_url)
    if identity.startswith("github.com/"):
        return f"https://{identity}/compare/{default}...{branch}?expand=1", problem
    return f"branch {branch} pushed to {repo_url}; open a pull request there", problem


def promote(workspace: Path, project: str, repo_url: str, branch: str, *, dry_run: bool = False) -> Report:
    """Apply the project's `patches/*.patch` onto `repo_url`'s default branch as `branch`, and push it."""
    # The workspace layout keeps a project in `projects/<name>/`; the sandbox layout in `<name>/`.
    project_dir = next(
        (d for d in (workspace / "projects" / project, workspace / project) if (d / "patches").is_dir()),
        workspace / "projects" / project,
    )
    patches = sorted((project_dir / "patches").glob("*.patch"))
    if not patches:
        return _stop("patches", f"no patches in {project_dir / 'patches'}")
    summary_file = project_dir / "SUMMARY.md"
    summary = summary_file.read_text(errors="replace") if summary_file.is_file() else ""

    with tempfile.TemporaryDirectory(prefix="promote-") as scratch:
        checkout = Path(scratch) / "target"
        # A fresh clone is the target's current state: there is no older local copy to rebase.
        code, output = _run(["git", "clone", "-q", repo_url, str(checkout)], Path(scratch))
        if code:
            return _stop("clone", f"git clone failed ({code}): {output[-2000:]}")
        try:
            default = (
                _workspace.git(checkout, "rev-parse", "--abbrev-ref", "origin/HEAD")
                .strip()
                .removeprefix("origin/")
            )
        except RuntimeError:
            # Cloning an empty repository succeeds but leaves no `origin/HEAD` to read.
            return _stop("clone", "the target has no default branch")
        base = _workspace.git(checkout, "rev-parse", f"origin/{default}").strip()
        _workspace.git(checkout, "checkout", "-q", "-b", branch, base)
        try:
            _workspace.git(checkout, "am", "--3way", *map(str, patches))
        except RuntimeError as exc:
            _run(["git", "am", "--abort"], checkout)
            return _stop("apply", f"the patches do not apply onto {default} ({base[:10]}):\n{exc}")

        ci = _gates.target_ci_result(checkout, against_base=False)
        # Only a pass is pushed: a CI that could not run (a toolchain that cannot be installed) shows nothing.
        if ci.status not in (_gate_base.Status.PASS, _gate_base.Status.NOT_APPLICABLE):
            outcome = "fails" if ci.status == _gate_base.Status.FAIL else f"is {ci.status.value}"
            return _stop("ci", f"the target's own CI {outcome}:\n{ci.output}")
        failure = _check_coverage(checkout, base, summary)
        if failure:
            return _stop("coverage", failure)
        if dry_run:
            return Report(
                True, "dry-run", f"{len(patches)} patch(es) apply onto {default} and pass; nothing pushed"
            )

        # --force-with-lease: a rerun updates the PR branch after a rebase. The lease needs the remote's
        # current value, which a fresh clone has only after fetching the branch.
        _run(["git", "fetch", "-q", "origin", branch], checkout)
        code, output = _run(["git", "push", "--force-with-lease", "origin", branch], checkout)
        if code:
            return _stop("push", f"git push failed ({code}): {output[-2000:]}")
        title = f"{project}: promoted from agent workspace"
        url, problem = _open_pull_request(checkout, repo_url, default, branch, title, summary or title)
        message = f"pushed {branch} onto {default} ({base[:10]})"
        return Report(True, "done", f"{message}\n{problem}" if problem else message, url)
