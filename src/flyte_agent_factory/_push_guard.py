"""A session pushes only to its workspace.

The hard limit is the deploy key: it is scoped to the workspace repository, so a push anywhere else has no
credential. This is the second line, which also stops a push that would succeed by some other route (a
different key in the environment, an https token): a `pre-push` hook that refuses any remote URL other than
the workspace's. A hook is advisory against a session that edits `.git/hooks`; it catches the mistake, the
key stops the attack. (`_secret_env.GIT_SAFE_CONFIG` turns hooks off for the harness's own git calls on
purpose, so the harness's push is not subject to it.)

`git push --no-verify` skips the hook, so the guard also writes `pushInsteadOf` config, which git applies
to every push URL (a remote's, or one given on the command line) whatever the flags: each URL form a
session could name is rewritten to a directory that does not exist, except URLs under the workspace's, which
are rewritten to themselves. git picks the longest matching prefix, so the workspace entry wins for it.
The missing directory's name is the explanation, because git prints the URL when it fails to open it:
"fatal: '.../not the workspace repository/...' does not appear to be a git repository". (An unknown URL
scheme would be tidier, but git then fails with "not a git command" and nothing says why.)

What remains: this is config in the checkout, so a session can still edit it, and a URL spelled in a form
not listed in `_REFUSED_PREFIXES` (a bare relative path, an scp-style `host:path` without `git@`) or one that
merely starts with the workspace URL (`<workspace>-other`) is not rewritten. Closing that is the
credential's job (a later design round).

The hook also scans what a push carries. A session can write a credential into a committed file or a commit
message, and nothing else sits between `git commit` and the workspace repository, so `scan_main` reads the
added lines of every new commit and its message and refuses the push if one holds an AWS credential shape
with a secret in it (`_bridge.find_credential_shape`) or the value of a live credential grant. It prints the
commit and the line number, never the line. It fails closed: a push it cannot fully check (the grant server
does not answer, a live grant cannot be read, git cannot show a commit) is refused too. The harness never
pushes the session's commits (`_promote` pushes engine code, and its git calls run with
`core.hooksPath=/dev/null`), so it is not subject to this.

The honest limit: `git push --no-verify` and editing `.git/hooks` skip the scan exactly as they skip the URL
check. It catches the mistake; redaction on every published path and the provider-side expiry of the
credential are the lines behind it.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

from ._bridge import find_credential_shape
from ._grants import TOKEN_ENV, URL_ENV
from ._secret_env import GIT_NO_DRIVERS
from ._workspace import build_entry_command, git

# A push URL that starts with one of these is rewritten to the refused directory. "." covers "./" and "../".
#: `git@` is the scp-style form GitHub hands out (`git@github.com:org/repo.git`), the one real remotes use.
_REFUSED_PREFIXES = (
    *("/", ".", "file://", "ssh://", "git://", "http://", "https://", "ftp://", "ftps://"),
    "git@",
)
# Relative to the git dir; never created. The rest of a rewritten URL is appended to it.
_REFUSED_DIR = "push refused: this is not the workspace repository"

# git passes the remote's name and URL as $1 and $2 and lists the refs on stdin, which `scan_main` reads.
_HOOK = """\
#!/bin/sh
# Installed by flyte_agent_factory._push_guard: sessions push only to the workspace.
if [ "$2" != {url} ]; then
    echo "push refused: $2 is not the workspace repository" >&2
    exit 1
fi
{scan}
"""
_ZEROS = "0" * 40


def install(checkout: Path, workspace_url: str, remote: str = "origin") -> None:
    """Make `checkout` push only to `workspace_url`, through `remote` by default.

    The URL is compared as the string git is given, so `workspace_url` must be spelled as the remote is
    configured; it is what `_git.clone` was called with.
    """
    git(checkout, "config", "remote.pushDefault", remote)
    if remote in git(checkout, "remote").split():
        git(checkout, "remote", "set-url", remote, workspace_url)
    else:
        git(checkout, "remote", "add", remote, workspace_url)
    hook = _git_path(checkout, "hooks/pre-push")
    _refuse_other_push_urls(checkout, workspace_url, _git_path(checkout, _REFUSED_DIR))
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        _HOOK.format(
            url=shlex.quote(workspace_url),
            scan=build_entry_command("flyte_agent_factory._push_guard", "scan_main"),
        )
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _git_path(checkout: Path, name: str) -> Path:
    path = Path(git(checkout, "rev-parse", "--git-path", name).strip())
    return path if path.is_absolute() else checkout / path


def _refuse_other_push_urls(checkout: Path, workspace_url: str, refused_dir: Path) -> None:
    # `--replace-all` on the first value of each key keeps a second `install` from stacking duplicates.
    refused = f"url.{refused_dir}/.pushInsteadOf"
    git(checkout, "config", "--replace-all", refused, _REFUSED_PREFIXES[0])
    for prefix in _REFUSED_PREFIXES[1:]:
        git(checkout, "config", "--add", refused, prefix)
    git(checkout, "config", "--replace-all", f"url.{workspace_url}.pushInsteadOf", workspace_url)


class _Unscannable(Exception):
    """The push cannot be checked; the text says why, and never holds a value."""


def scan_main() -> int:
    """Run as the pre-push hook: refuse a push whose new commits carry a credential. Return the exit code.

    It fails closed: a push that cannot be fully checked (a live grant whose value cannot be read, a commit
    git cannot show) is refused, saying why, never let through unchecked.
    """
    try:
        # Every line is checked before any is scanned: a line git did not write in the documented shape
        # means the hook cannot tell what is being pushed, so the push is refused, not skipped.
        refs = []
        for number, raw in enumerate(sys.stdin.buffer, 1):
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _Unscannable(f"line {number} of the pre-push input is malformed") from None
            fields = line.split()
            if len(fields) != 4:
                raise _Unscannable(f"line {number} of the pre-push input is malformed")
            refs.append(fields)
        values = _fetch_live_values()
        for _, local, _, remote in refs:
            if local == _ZEROS:  # a deletion carries no commits
                continue
            for commit in _list_new_commits(local, remote):
                found = _find(commit, values)
                if found:
                    what, where = found
                    print(
                        f"push refused: commit {commit[:12]} holds {what} in {where}; remove it and commit "
                        "again. Granted values and AWS credential shapes never leave this pod.",
                        file=sys.stderr,
                    )
                    return 1
    except _Unscannable as exc:
        print(f"push refused: {exc}; nothing was pushed. Try again in a moment.", file=sys.stderr)
        return 1
    return 0


def _fetch_live_values() -> tuple[str, ...]:
    """Return the values of the active grants, read from the grant server the session itself can reach.

    Each grant is read on its own. One that ended between the listing and the read has no value left
    anywhere in the factory, so the scan goes on with the others and says so; one that cannot be read for
    any other reason, or a server that does not answer, makes the push unscannable.
    """
    # Imported here: the CLI is only needed when a grant server is configured, and this keeps the hook
    # free of it otherwise.
    from ._grants_cli import _call, _CliError

    if not (os.environ.get(URL_ENV) and os.environ.get(TOKEN_ENV)):
        return ()
    try:
        names = [g["name"] for g in _call("GET", "/v1/grants")["grants"] if g["state"] == "active"]
    except (_CliError, KeyError, TypeError) as exc:
        raise _Unscannable("the grant server did not answer, so granted values cannot be checked") from exc
    values: list[str] = []
    for name in names:
        try:
            creds = _call("GET", f"/v1/aws/{name}")
            values += [creds["SecretAccessKey"], creds["Token"]]
        except _CliError as exc:
            if exc.reason != "GrantEnded":
                raise _Unscannable(
                    f"credential {name} could not be read, so its value cannot be checked"
                ) from exc
            print(f"push scan: credential {name} ended during the scan; checked the others", file=sys.stderr)
        except (KeyError, TypeError) as exc:
            raise _Unscannable(
                f"credential {name} could not be read, so its value cannot be checked"
            ) from exc
    return tuple(v for v in values if v)


def _read_git_lines(*args: str) -> list[str]:
    """Return git's output lines; a git that fails makes the push unscannable rather than empty."""
    proc = subprocess.run(["git", *args], capture_output=True)
    if proc.returncode != 0:
        raise _Unscannable(f"git {args[0]} failed, so the commits cannot be checked")
    # Split on LF alone: git ends a physical line only there. `text=True` and `str.splitlines()` also split
    # on CR, form feed, vertical tab and Unicode separators, which can sit inside one added line and shift
    # the hunk counts in `_collect_added_lines`, hiding a later addition.
    text = proc.stdout.decode(errors="replace")
    return text.removesuffix("\n").split("\n") if text else []


def _list_new_commits(local: str, remote: str) -> list[str]:
    """Return the commits this push adds, oldest first: those `remote` does not have."""
    if remote != _ZEROS:
        try:
            commits = _read_git_lines("rev-list", "--reverse", f"{remote}..{local}")
        except _Unscannable:
            commits = []  # a remote sha this clone does not know: fall back to the remote-tracking refs
        if commits:
            return commits
    # A new branch (or a remote sha this clone does not know): what no remote-tracking ref has.
    return _read_git_lines("rev-list", "--reverse", local, "--not", "--remotes")


def _find(commit: str, values: tuple[str, ...]) -> tuple[str, str] | None:
    """Return what `commit` holds and where, for the first line that carries a credential."""
    message = _read_git_lines("show", "-s", "--format=%B", commit)
    diff = _read_git_lines("show", "--format=", "-p", "-m", *GIT_NO_DRIVERS, commit)
    added = _collect_added_lines(diff)
    for where, lines in (
        ("the commit message", list(enumerate(message, 1))),
        (None, added),
    ):
        for number, text in lines:
            what = find_credential_shape(text) or (
                "a granted credential value" if _holds(text, values) else ""
            )
            if what:
                return what, where or f"line {number} of its diff"
    return None


def _holds(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _collect_added_lines(diff: list[str]) -> list[tuple[int, str]]:
    """Return (diff line number, text) for each line a patch adds.

    A line starting `+++` is a file header only outside a hunk; inside one it is added content that begins
    `++`. The `@@ -a,b +c,d @@` header says how many old and new lines the hunk holds, so the hunk's end is
    known by counting, not by guessing from what a line looks like. (Skipping every `+++` line is what let
    `++aws_secret_access_key=...` through.)
    """
    added: list[tuple[int, str]] = []
    old_left = new_left = 0
    for number, line in enumerate(diff, 1):
        if old_left or new_left:
            if line.startswith("+"):
                new_left -= 1
                added.append((number, line[1:]))
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith(" "):
                old_left -= 1
                new_left -= 1
            # else: "\ No newline at end of file", which is in neither file
            continue
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            old_left, new_left = (1 if count is None else int(count) for count in hunk.groups())
    return added
