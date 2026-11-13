"""Promotion: take a reviewed workspace project to another repository as a pull request.

Runs outside the factory, by the chat session or an operator, with the CALLER's git credentials (ssh agent,
`gh` login). A factory session never holds a credential for a target repository (docs/architecture-repos.md),
so nothing here reads the factory's secrets.

`deliver` turns the project's committed tree (`_delivery`: `projects/<name>/<path>`, imported from the target
at `base`) into ONE commit on a new branch of the target's current default branch, by the operator: the
tree becomes a commit whose parent is `base`, and that commit is cherry-picked onto the target's head. A
cherry-pick is a three-way merge of base -> tree, so what the target changed since the import is neither a
conflict nor reverted. A conflict, a secret in what is added, a red check of the target's own CI, an uncovered
changed line or a missing sign-off stops it before anything is pushed. It never merges.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from . import _delivery, _gate_base, _gates, _target_ci
from ._git import _repo_identity

#: The factory's harness directory (criteria, notes, checks): it belongs to the workspace, never to a target.
HARNESS_DIR = ".claude-flyte"
#: `ops/scan_public.py` is the one scanner of what may go public; promotion runs it on what it adds.
_SCAN_PUBLIC = Path(__file__).resolve().parent.parent.parent / "ops" / "scan_public.py"
_DCO_TEXT = re.compile(r"Signed-off-by|\bDCO\b|Developer Certificate of Origin", re.I)
_CLA_TEXT = re.compile(r"\bCLA\b|Contributor License Agreement", re.I)
_CONVENTIONAL_TEXT = re.compile(r"Conventional Commits", re.I)
# A public issue a pull request resolves, as GitHub links it from a `Fixes` line.
_ISSUE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+#[1-9][0-9]*")

#: Reads a directory and returns (exit status, report); 0 is clean. Injected so tests need no scanner.
Scan = Callable[[Path], tuple[int, str]]


@dataclasses.dataclass(frozen=True)
class Report:
    """How a promotion ended. `stage` is where it stopped, or `done`; `url` the PR (or compare) link."""

    ok: bool
    stage: str
    message: str
    url: str = ""


@dataclasses.dataclass(frozen=True)
class Rules:
    """What the target asks of a contribution: a sign-off on every commit, and what we leave to the person."""

    dco: bool
    notices: tuple[str, ...] = ()
    #: What made `dco` true (a file name), for the warning of `--no-dco`; not part of what is compared.
    dco_where: str = dataclasses.field(default="", compare=False)


class _Refuse(Exception):
    """Promotion stops here, before anything is pushed; `stage` says where."""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


def _git(cwd: Path, stage: str, *args: str, env: dict[str, str] | None = None, timeout: int = 300) -> str:
    try:
        return _delivery.git(cwd, *args, env=env, timeout=timeout)
    except _delivery.DeliveryError as exc:
        raise _Refuse(stage, str(exc)) from exc


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
        if command.command == _target_ci.FALLBACK_COVERAGE and "--cov" in failure:
            # The target names no coverage command: the fallback runs on the operator's `python`.
            failure += "\nThe target has no coverage command: `pip install pytest-cov` into the PATH python."
        return failure
    with tempfile.TemporaryDirectory(prefix="promote-coverage-", ignore_cleanup_errors=True) as scratch:
        out = Path(scratch) / "coverage.json"
        if reader := _target_ci.coverage_report_command(command, out):
            if failure := _gates.run_target_command(reader, checkout):
                return f"`coverage json` failed:\n{failure[-3000:]}"
        else:
            # The python on PATH, as the gate reads it: the one the coverage command measured with.
            code, output = _run(["python", "-m", *_target_ci.COVERAGE_JSON, "-o", str(out)], checkout)
            if code:
                return f"`coverage json` exited {code}:\n{output[-3000:]}"
        report = json.loads(out.read_text())
    unlisted = sorted(
        _gates.uncovered_changed_lines(report, checkout, changed) - _target_ci.parse_listed_lines(summary)
    )
    if unlisted:
        return f"changed lines not covered: {', '.join(unlisted)}"
    return ""


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def contribution_rules(checkout: Path) -> Rules:
    """Read what the target at `checkout` asks of a contribution.

    A DCO is asked for by `.github/dco.yml`, a root `DCO` file, a CONTRIBUTING file (at the root, in
    `.github/` or `docs/`) that mentions sign-offs, or a workflow that mentions `dco`. A CLA or Conventional
    Commits is only reported: a CLA is the person's to sign, and the title is theirs to choose (`--title`).
    """
    guides = {
        Path(folder, name).as_posix(): _read(checkout / folder / name)
        for folder in (".", ".github", "docs")
        for name in ("CONTRIBUTING.md", "CONTRIBUTING.rst")
    }
    workflows = sorted((checkout / ".github" / "workflows").glob("*.y*ml"))
    if (checkout / ".github" / "dco.yml").is_file():
        where = ".github/dco.yml"
    elif (checkout / "DCO").is_file():
        where = "DCO"
    else:
        where = next((name for name, text in guides.items() if _DCO_TEXT.search(text)), "")
    if not where:
        where = next(
            (
                w.relative_to(checkout).as_posix()
                for w in workflows
                if re.search(r"\bdco\b", w.name + _read(w), re.I)
            ),
            "",
        )
    notices = []
    if any(_CLA_TEXT.search(text) for text in guides.values()):
        notices.append("the target mentions a CLA: sign it yourself; promotion adds no trailer for it")
    if any(_CONVENTIONAL_TEXT.search(text) for text in guides.values()):
        notices.append("the target mentions Conventional Commits: give a matching --title")
    return Rules(bool(where), tuple(notices), where)


def _operator() -> tuple[str, str]:
    """Return the operator's git name and email: the promotion is authored and signed off as them."""
    values = []
    for key in ("user.name", "user.email"):
        try:
            values.append(_delivery.git(Path.cwd(), "config", "--get", key, timeout=30).strip())
        except _delivery.DeliveryError:  # unset (exit 1) or hung: either way there is no identity
            values.append("")
    if not all(values):
        raise _Refuse(
            "identity",
            "git `user.name` or `user.email` is not set: the promotion is authored as you "
            '(`git config --global user.name "..."` and `user.email`)',
        )
    return values[0], values[1]


def _unsigned(checkout: Path, base: str) -> list[str]:
    """Return the commits in `base..HEAD` (all of HEAD if `base` is "") with no sign-off by their author."""
    commits = _git(checkout, "signoff", "rev-list", f"{base}..HEAD" if base else "HEAD").split()
    unsigned = []
    for sha in commits:
        shown = _git(
            checkout,
            "signoff",
            "show",
            "-s",
            "--format=%an%x00%ae%x00%(trailers:key=Signed-off-by,valueonly,unfold)",
            sha,
        )
        name, email, trailers = shown.split("\0", 2)
        if f"{name} <{email}>" not in trailers.splitlines():
            unsigned.append(sha)
    return unsigned


def _scan_public(path: Path) -> tuple[int, str]:
    if not _SCAN_PUBLIC.is_file():
        return 2, f"cannot find {_SCAN_PUBLIC}: run promote from the engine's checkout"
    code, output = _run([sys.executable, str(_SCAN_PUBLIC), str(path)], path.parent)
    return code, output


def _scan_added(checkout: Path, base: str, scan: Scan) -> str:
    """Scan what the new branch adds or changes against `base`; return a refusal text or "".

    Refuses outright a file under the factory's harness directory, which no scan would flag.

    Not the whole tree: upstream files the promotion does not touch can name people legitimately (third-party
    history), and would block every promotion. Every byte this promotion introduces is scanned.
    """
    if base:
        listed = _git(checkout, "scan", "diff", "--name-only", "--diff-filter=ACMR", "-z", base, "HEAD")
    else:
        listed = _git(checkout, "scan", "ls-files", "-z")
    names = [n for n in listed.split("\0") if n]
    if harness := [n for n in names if n.split("/", 1)[0] == HARNESS_DIR]:
        raise _Refuse("harness", f"the factory's harness files stay in the workspace: {', '.join(harness)}")
    with tempfile.TemporaryDirectory(prefix="promote-scan-", ignore_cleanup_errors=True) as scratch:
        root = Path(scratch) / "added"
        root.mkdir()
        for name in names:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checkout / name, root / name, follow_symlinks=False)
        code, output = scan(root)
    return (
        f"the scan of the {len(names)} added or changed file(s) exited {code}:\n{output[-3000:]}"
        if code
        else ""
    )


def _scan_text(text: str, scan: Scan) -> str:
    """Scan the pull request's title and body, which leave as public text too; return a refusal text or ""."""
    with tempfile.TemporaryDirectory(prefix="promote-scan-", ignore_cleanup_errors=True) as scratch:
        root = Path(scratch) / "pull-request"
        root.mkdir()
        (root / "pull-request.md").write_text(text)
        code, output = scan(root)
    return f"the scan of the pull request's title and body exited {code}:\n{output[-3000:]}" if code else ""


def _owner_of(url: str) -> str:
    """Return the GitHub owner a repository URL names, or "" for any other host."""
    identity = _repo_identity(url)
    return identity.split("/")[1] if identity.startswith("github.com/") else ""


def _open_pull_request(
    checkout: Path,
    repo_url: str,
    head_owner: str,
    default: str,
    branch: str,
    title: str,
    body: str,
    pushed_to: str = "",
) -> tuple[str, str]:
    """Open or update the PR with `gh` when it is installed; return its link and what went wrong, if anything.

    Without `gh`, or when it fails, the link is the compare URL (or a note), and the second value is the
    failed call's output so the caller sees why there is no PR. `pushed_to` is the fork the branch went
    to, when it did not go to `repo_url`. This is the only thing done to the PR:
    nothing here merges, approves or enables auto-merge.
    """
    identity = _repo_identity(repo_url)
    repo = identity.removeprefix("github.com/") if identity.startswith("github.com/") else repo_url
    head = f"{head_owner}:{branch}" if head_owner else branch
    problem = ""
    if shutil.which("gh"):
        # `pr view <branch>` also finds a merged or closed PR, which must not be edited. `--head` takes the
        # bare branch name, so another fork's same-named branch would match too: keep only the PR whose head
        # is in the repository the branch was pushed to.
        owner = head_owner or repo.partition("/")[0]
        pick = f'map(select(.headRepositoryOwner.login == "{owner}"))[0].url // empty'
        code, existing = _run(
            [
                *("gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open"),
                *("--json", "url,headRepositoryOwner", "--jq", pick),
            ],
            checkout,
        )
        if code == 0 and existing.strip():
            link = existing.strip()
            code, output = _run(["gh", "pr", "edit", link, "--title", title, "--body", body], checkout)
            if code:
                problem = f"gh pr edit failed ({code}): {output[-2000:]}"
            return link, problem
        code, out = _run(
            [
                *("gh", "pr", "create", "--repo", repo, "--head", head, "--base", default),
                *("--title", title, "--body", body),
            ],
            checkout,
        )
        if code == 0:
            return out.strip().splitlines()[-1], ""
        problem = f"gh pr create failed ({code}): {out[-2000:]}"
    if identity.startswith("github.com/"):
        return f"https://{identity}/compare/{default}...{head}?expand=1", problem
    return f"branch {branch} pushed to {pushed_to or repo_url}; open a pull request there", problem


def _default_branch(checkout: Path) -> str:
    try:
        output = _delivery.git(checkout, "rev-parse", "--abbrev-ref", "origin/HEAD")
    except _delivery.DeliveryError:
        # Cloning an empty repository succeeds but leaves no `origin/HEAD` to read.
        raise _Refuse("clone", "the target has no default branch") from None
    return output.strip().removeprefix("origin/")


def _check_branch(checkout: Path, url: str, branch: str, email: str, replace: bool) -> str:
    """Refuse to replace a `branch` of `url` that is not an earlier promotion; return its tip ("" if none).

    An earlier promotion is one commit by the operator over a commit the target's head (as cloned) has.
    Anything else is someone's work, which only `replace` may overwrite.
    """
    if not _git(checkout, "branch", "ls-remote", "--heads", url, f"refs/heads/{branch}").strip():
        return ""
    _git(checkout, "branch", "fetch", "-q", url, f"refs/heads/{branch}", timeout=600)
    tip = _git(checkout, "branch", "rev-parse", "FETCH_HEAD").strip()
    if replace:
        return tip
    shown = _git(checkout, "branch", "show", "-s", "--format=%P%x00%ae", tip).strip()
    parents, _, author = shown.partition("\0")
    if len(parents.split()) == 1 and author == email:
        try:
            _delivery.git(checkout, "merge-base", "--is-ancestor", parents, "HEAD")
            return tip
        except _delivery.DeliveryError:  # exit 1: the parent is not in the target's history
            pass
    raise _Refuse(
        "branch",
        f"{branch} already exists on {url} at {tip[:10]} and is not a promotion of yours over the "
        "target's default branch: pick another branch name, or pass --replace to overwrite it",
    )


def _build_branch(
    checkout: Path, tree: Path, branch: str, base: str, message: str, identity: dict[str, str], scratch: Path
) -> str:
    """Put the tree on `branch` of `checkout` as one commit over the target's head; return that head's sha.

    The tree is committed into the clone's object store with a temporary index (the operator's own index
    and the clone's working tree are untouched), as a child of `base`, then cherry-picked onto the head.
    """
    env = {
        "GIT_DIR": str(checkout / ".git"),
        "GIT_WORK_TREE": str(tree),
        "GIT_INDEX_FILE": str(scratch / "index"),
    }
    _git(tree, "build", "add", "-A", "-f", ".", env=env)
    # Import skips submodules (gitlinks), so the tree lacks them: without them the commit would delete each.
    for entry in _git(checkout, "base", "ls-tree", "-r", "-z", base).split("\0"):
        meta, _, path = entry.partition("\t")
        if meta.startswith("160000 ") and not os.path.lexists(tree / path):
            link = meta.split()[2]
            _git(tree, "build", "update-index", "--add", "--cacheinfo", f"160000,{link},{path}", env=env)
    tree_sha = _git(tree, "build", "write-tree", env=env).strip()
    if tree_sha == _git(checkout, "base", "rev-parse", f"{base}^{{tree}}").strip():
        raise _Refuse("empty", "nothing to promote: the project's tree is the tree it was imported as")
    imported = _git(
        checkout, "build", "commit-tree", tree_sha, "-p", base, "-m", message, env=identity
    ).strip()
    head = _git(checkout, "apply", "rev-parse", "HEAD").strip()
    _git(checkout, "apply", "checkout", "-q", "-b", branch, head)
    try:
        _delivery.git(checkout, "cherry-pick", "-n", imported, env=identity)
    except _delivery.DeliveryError as exc:
        conflicts = _delivery.git(checkout, "diff", "--name-only", "--diff-filter=U").split()
        if conflicts:
            raise _Refuse(
                "apply",
                f"the project's changes conflict with the target's {head[:10]} in: {', '.join(conflicts)}",
            ) from exc
        raise _Refuse("apply", str(exc)) from exc
    if not _git(checkout, "apply", "diff", "--cached", "--name-only").strip():
        raise _Refuse("empty", "nothing to promote: the target already has these changes")
    _git(checkout, "apply", "commit", "-q", "--no-verify", "-m", message, env=identity)
    return head


def deliver(
    project: Path,
    branch: str,
    *,
    to: str = "",
    fork: str = "",
    title: str = "",
    base: str = "",
    dco: bool | None = None,
    fixes: Sequence[str] = (),
    dry_run: bool = False,
    replace: bool = False,
    scan: Scan = _scan_public,
) -> Report:
    """Push the project's committed tree to `to` (else `promote.toml`'s repo) as `branch`, and open a PR.

    `base` stands in for a project without `promote.toml` (the one-off bootstrap from a built tree). `dco`
    None follows what the target asks for. `fixes` names the public issues (`owner/repo#N`) the pull request
    resolves: each becomes a `Fixes owner/repo#N` line of its body. `dry_run` stops before the push, with
    everything checked. An existing `branch` is only replaced when it is this operator's earlier promotion
    (see `_check_branch`), or `replace` says to.
    """
    try:
        if bad := [ref for ref in fixes if not _ISSUE_REF.fullmatch(ref)]:
            raise _Refuse("fixes", f"not an issue reference of the form owner/repo#N: {', '.join(bad)}")
        return _deliver(
            project.resolve(), branch, to, fork, title, base, dco, tuple(fixes), dry_run, replace, scan
        )
    except _Refuse as exc:
        return Report(False, exc.stage, str(exc))


def _deliver(
    project: Path,
    branch: str,
    to: str,
    fork: str,
    title: str,
    base: str,
    dco: bool | None,
    fixes: tuple[str, ...],
    dry_run: bool,
    replace: bool,
    scan: Scan,
) -> Report:
    try:
        delivery = _delivery.load(project)
    except _delivery.DeliveryError as exc:
        raise _Refuse("project", str(exc)) from exc
    if delivery is None:
        if not (to and base):
            raise _Refuse(
                "project",
                f"no {_delivery.FILE} in {project}: import the target first (`promote.py import`), "
                "or give --to and --base",
            )
        delivery = _delivery.Delivery(to, base)
    else:
        # What the import recorded, unless a project commit changed it: that would move the base (reverting
        # what the target changed since) or the repository past the review.
        try:
            _delivery.import_commit(project)
        except _delivery.DeliveryError as exc:
            raise _Refuse("project", str(exc)) from exc
        delivery = dataclasses.replace(delivery, repo=to or delivery.repo, base=base or delivery.base)
    # Only committed (reviewed) content leaves: a dirty project is not what was reviewed.
    if dirty := _git(project, "project", "status", "--porcelain", "--", ".").strip():
        raise _Refuse(
            "project", f"{project} has uncommitted changes; commit or discard them:\n{dirty[:1000]}"
        )
    name, email = _operator()
    identity = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    summary_file = project / "SUMMARY.md"
    summary = summary_file.read_text(errors="replace") if summary_file.is_file() else ""
    if fixes:
        summary = (
            summary.rstrip("\n")
            + ("\n\n" if summary.strip() else "")
            + "".join(f"Fixes {ref}\n" for ref in fixes)
        )
    title = title or f"{project.name}: promoted from agent workspace"

    # A process the target's own tests leave running may still write into the checkout while it is removed:
    # the verdict stands, and the leftover stays in the temp directory.
    with tempfile.TemporaryDirectory(prefix="promote-", ignore_cleanup_errors=True) as directory:
        scratch = Path(directory)
        tree = scratch / "tree"
        try:
            _delivery.export_tree(project, f"HEAD:./{'' if delivery.path == '.' else delivery.path}", tree)
        except _delivery.DeliveryError as exc:
            raise _Refuse("project", f"the committed tree is unreadable: {exc}") from exc
        _delivery.drop_harness(tree, delivery)
        if not any(tree.iterdir()):
            raise _Refuse("empty", "nothing to promote: the tree has no files")

        checkout = scratch / "target"
        if delivery.base:
            # A fresh clone is the target's current state: there is no older local copy to rebase.
            _git(scratch, "clone", "clone", "-q", delivery.repo, str(checkout), timeout=900)
            default = _default_branch(checkout)
            try:
                base_sha = _delivery.git(
                    checkout, "rev-parse", "--verify", f"{delivery.base}^{{commit}}"
                ).strip()
            except _delivery.DeliveryError as exc:
                raise _Refuse(
                    "base",
                    f"the target does not have {delivery.base[:12]}, the commit this tree was made from: "
                    "re-import it",
                ) from exc
            rules = contribution_rules(checkout)
            seen = _check_branch(checkout, fork or delivery.repo, branch, email, replace)
        else:
            default, base_sha, seen = "", "", ""
            if _git(scratch, "new", "ls-remote", delivery.repo).strip():
                raise _Refuse(
                    "new",
                    f"{delivery.repo} now has commits: it is no longer a new repository, and a first "
                    "commit would be unrelated to its history; re-import the project from it "
                    "(`promote.py import`) and carry your changes over",
                )
            rules = contribution_rules(tree)
        use_dco = rules.dco if dco is None else dco
        message = title
        if use_dco:
            message += f"\n\nSigned-off-by: {name} <{email}>"

        if base_sha:
            head = _build_branch(checkout, tree, branch, base_sha, message, identity, scratch)
        else:
            # A repository that does not exist yet: the tree is its first commit, pushed as `branch`.
            shutil.copytree(tree, checkout, symlinks=True)
            _git(checkout, "build", "init", "-q", "-b", branch)
            _git(checkout, "build", "remote", "add", "origin", delivery.repo)
            _git(checkout, "build", "add", "-A", "-f")
            _git(checkout, "build", "commit", "-q", "--no-verify", "-m", message, env=identity)
            head = ""
        sha = _git(checkout, "build", "rev-parse", "HEAD").strip()

        if failure := _scan_added(checkout, head, scan) or _scan_text(f"{title}\n\n{summary}", scan):
            raise _Refuse("scan", failure)
        ci = _gates.target_ci_result(checkout, against_base=False)
        # Only a pass is pushed: a CI that could not run (a toolchain that cannot be installed) shows nothing.
        if ci.status not in (_gate_base.Status.PASS, _gate_base.Status.NOT_APPLICABLE):
            outcome = "fails" if ci.status == _gate_base.Status.FAIL else f"is {ci.status.value}"
            raise _Refuse("ci", f"the target's own CI {outcome}:\n{ci.output}")
        # A new repository has no head: its whole tree is the change, as in the gate (empty-tree baseline).
        if failure := _check_coverage(checkout, head or _delivery.EMPTY_TREE, summary):
            raise _Refuse("coverage", failure)
        if use_dco and (unsigned := _unsigned(checkout, head)):
            raise _Refuse(
                "signoff", f"commits without a Signed-off-by by their author: {', '.join(unsigned)}"
            )

        notes = "".join(f"\n{notice}" for notice in rules.notices)
        if rules.dco and not use_dco:
            notes += (
                f"\nwarning: the target asks for DCO sign-off ({rules.dco_where}); --no-dco left the "
                "commit unsigned, so its DCO check will fail"
            )
        if dry_run:
            return Report(
                True,
                "dry-run",
                f"{branch} is {sha[:10]} over {head[:10] or 'nothing (new repository)'}, by {name} <{email}>"
                f"; sign-off: {'yes' if use_dco else 'no'}; scan clean; the target's CI {ci.status.value}; "
                f"nothing pushed{notes}",
            )
        return _push(
            checkout, delivery.repo, fork, default, branch, head, title, summary or title, sha, notes, seen
        )


def _push(
    checkout: Path,
    repo: str,
    fork: str,
    default: str,
    branch: str,
    head: str,
    title: str,
    body: str,
    sha: str,
    notes: str,
    seen: str = "",
) -> Report:
    """Push `branch`; `seen` is the tip it had on the remote when it was checked, "" if it did not exist."""
    remote = "origin"
    if fork:
        _git(checkout, "push", "remote", "add", "fork", fork)
        remote = "fork"
    # The lease names the tip `_check_branch` looked at (empty: the branch must not exist), so a branch
    # created or moved since is never overwritten, which a lease taken from a fresh fetch would allow.
    lease = [f"--force-with-lease=refs/heads/{branch}:{seen}"] if head else []
    code, output = _run(["git", "push", *lease, remote, branch], checkout, timeout=600)
    if code:
        return Report(False, "push", f"git push failed ({code}): {output[-2000:]}")
    if not head:
        return Report(True, "done", f"pushed {branch} ({sha[:10]}) as the first commit of {repo}{notes}")
    url, problem = _open_pull_request(
        checkout, repo, _owner_of(fork), default, branch, title, body, pushed_to=fork
    )
    message = f"pushed {branch} ({sha[:10]}) onto {default} ({head[:10]}){notes}"
    if problem:
        message += f"\n{problem}"
    if problem or not shutil.which("gh"):
        # No pull request was opened: the operator opens it by hand, with this body (its `Fixes` lines too).
        message += f"\n\nPull request body:\n{body}"
    return Report(True, "done", message, url)
