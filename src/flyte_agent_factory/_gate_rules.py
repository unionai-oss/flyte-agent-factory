"""How the gate runs the declared kinds of a rule's `Enforced:` line that need more than a command."""

from __future__ import annotations

import fnmatch
import getopt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING

from . import _gate_base, _secret_env, _standards, _waivers

if TYPE_CHECKING:  # `_vendored` imports modules that import this one
    from . import _vendored

#: Tools that judge each file alone, with globs (`_glob_matches`) of the files they read, none for any
#: file: an unchanged upstream file's finding is upstream's, so it is left out. A whole-program check (mypy,
#: tsc, pytest, tflint) is not here: it can fail in an unchanged file because of what the project changed
#: elsewhere.
_PER_FILE_GLOBS = {
    # Ruff 0.17's default `include`, for when ruff cannot say the project's (`_list_ruff_includes`).
    "ruff": ("*.py", "*.pyi", "*.ipynb", "**/pyproject.toml", "**/ruff.toml", "**/.ruff.toml", "*.md"),
    # A flat config's `files` decide which files ESLint reads (`.vue`, `.md`, `.es6` ...), and Prettier's
    # plugins which it formats, so any file may be one they read.
    "eslint": (),
    "prettier": (),
    "shellcheck": ("*.sh",),
}


def uses_upstreams(command: list[str]) -> bool:
    """Whether `command`'s file set depends on which files are upstream's (see `with_baseline_config`)."""
    return Path(command[0]).name in _PER_FILE_GLOBS and (
        Path(command[0]).name != "ruff" or command[1:2] in (["check"], ["format"])
    )


def _list_files(project: Path, globs: tuple[str, ...]) -> list[str]:
    # Sorted, so the command the gate records is the same from run to run.
    return sorted(
        p for p in _gate_base._project_files(project) if not globs or any(_glob_matches(p, g) for g in globs)
    )


def _list_excludes(project: Path, upstreams: _vendored.Upstreams, globs: tuple[str, ...]) -> list[str]:
    # The fewest project-relative paths that cover every unchanged upstream file matching `globs` (any file
    # for none): the highest directory inside an upstream tree that holds no such project file, else the file
    # itself. Only files the tool reads keep a directory: a gitignored `__pycache__/x.pyc` is the project's,
    # but must not make ruff list each of its neighbours.
    roots = [PurePosixPath(c.root.relative_to(project)) for c in upstreams.nested]
    roots += [t.prefix for t in upstreams.trees]
    unchanged: list[PurePosixPath] = []
    owned: set[PurePosixPath] = set()  # every directory holding such a file of the project's, at any depth
    for relative in _list_files(project, globs):
        path = PurePosixPath(relative)
        if not any(path.is_relative_to(r) for r in roots):
            continue
        if upstreams.unchanged(relative):
            unchanged.append(path)
        else:
            owned.update(path.parents)
    excludes = set()
    for path in unchanged:
        # `parents` runs from the file up to "."; reversed, the first unowned one inside a tree is highest.
        inside = [d for d in reversed(path.parents) if any(d.is_relative_to(r) for r in roots)]
        excludes.add(str(next((d for d in inside if d not in owned), path)))
    return sorted(excludes)


def _escape_glob(path: str) -> str:
    # Both ruff (globset) and ESLint (minimatch) read a backslash as "the next character literally"; checked
    # for each of these characters against ruff 0.17 and ESLint 9. ESLint's `@(` / `+(` extglobs need `(`.
    return re.sub(r"([\\*?\[\]{}()!#])", r"\\\1", path)


def _timeout(rule: _standards.Rule) -> tuple[int, str]:
    """Return a rule's `Timeout: <seconds>` (default `_TOOL_TIMEOUT`), or 0 and why it is invalid."""
    limits = [line for line in rule.body.splitlines() if re.match(r"^\s*Timeout\s*:", line, re.IGNORECASE)]
    if not limits:
        return _gate_base._TOOL_TIMEOUT, ""
    limit = re.fullmatch(r"Timeout: ([1-9][0-9]*)", limits[0])
    # Bound the string before int() as well: custom standards may contain arbitrarily
    # many digits, which Python refuses to convert to an integer.
    if (
        len(limits) != 1
        or not limit
        or len(limit[1]) > len(str(_gate_base._MAX_TOOL_TIMEOUT))
        or int(limit[1]) > _gate_base._MAX_TOOL_TIMEOUT
    ):
        return 0, "invalid Timeout line(s): " + repr(limits)
    return int(limit[1]), ""


def _commands(rule: _standards.Rule) -> list[list[str]]:
    """Return the argv of each command a `command` or `lint` rule runs, in order."""
    if rule.kind == "lint":
        return [["ruff", "check", "--isolated", "--select", rule.arg.split()[1], "."]]
    return _standards.split_commands(rule.arg)


def _script_command(rule: _standards.Rule, standards_dir: Path) -> list[str] | str:
    """Return the argv a `script` rule runs, or why it cannot be run."""
    words = _standards.split_commands(rule.arg)[0]
    path = (standards_dir / words[0]).resolve()
    if not path.is_relative_to(standards_dir.resolve()):
        return f"script {words[0]} is outside the standards directory"
    if not path.is_file():
        return f"script {words[0]} not found in the standards directory"
    interpreter = {".py": [sys.executable], ".sh": ["bash"]}.get(path.suffix, [])
    return [*interpreter, str(path), *words[1:]]


def _glob_matches(relative: str, pattern: str) -> bool:
    """Whether `relative` matches `pattern`: `**/` may match no directory, a pattern without `/` the name."""
    if "/" not in pattern:
        return fnmatch.fnmatch(PurePosixPath(relative).name, pattern)
    return fnmatch.fnmatch(relative, pattern) or (
        "**/" in pattern and fnmatch.fnmatch(relative, pattern.replace("**/", ""))
    )


def _forbidden(
    project: Path, rule: _standards.Rule, upstreams: _vendored.Upstreams | None = None
) -> tuple[int, str]:
    """Run a `forbid` rule: the `file:line` of every line of the project's matching text files that matches.

    A file identical to upstream's is not the project's, so a pattern in it is upstream's (`_vendored`).
    """
    pattern, globs = _standards.forbid_parts(rule)
    regex = re.compile(pattern)
    found = []
    for relative in sorted(_gate_base._project_files(project)):
        if globs and not any(_glob_matches(relative, g) for g in globs):
            continue
        if upstreams is not None and upstreams.unchanged(relative):
            continue
        try:
            text = (project / relative).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # not text, or not readable: nothing to read lines from
        if "\0" in text:
            continue
        found += [
            f"{relative}:{n}: {line.strip()[:200]}"
            for n, line in enumerate(text.splitlines(), 1)
            if regex.search(line)
        ]
    note = upstreams.describe() if upstreams is not None else ""
    if not found:
        return 0, "\n".join([f"no line matches {pattern!r}", note][: 2 if note else 1])
    shown = found[:100]
    more = [f"... and {len(found) - 100} more"] if len(found) > 100 else []
    return 1, "\n".join(shown + more + ([note] if note else []))


def _find_ruff_config(project: Path) -> str | None:
    # The file ruff would read its configuration from at the project root: ruff prefers `.ruff.toml`, then
    # `ruff.toml`, then a `pyproject.toml` that has a `[tool.ruff` table.
    for name in (".ruff.toml", "ruff.toml"):
        if (project / name).is_file():
            return name
    pyproject = project / "pyproject.toml"
    if pyproject.is_file() and re.search(r"\[tool\.ruff(?:\]|\.)", pyproject.read_text()):
        return pyproject.name
    return None


def _has_own_eslint_config(project: Path) -> bool:
    # A legacy `.eslintrc*` counts: the project then runs an ESLint that reads it, not the flat baseline.
    return any(project.glob("eslint.config.*")) or any(project.glob(".eslintrc*"))


def with_baseline_config(
    command: list[str],
    project: Path,
    configs: Path,
    shell_scripts: tuple[str, ...],
    *,
    upstreams: _vendored.Upstreams | None = None,
) -> list[str]:
    """`command` with the baseline config of its tool added when the project has none of its own.

    With `upstreams`, ShellCheck is not given the files identical to upstream's: their findings are
    upstream's, not the project's (`_vendored`). None judges every file. The other per-file tools leave them
    out in `prepare_command`.
    """
    tool = Path(command[0]).name
    pyproject = project / "pyproject.toml"
    if Path(command[0]).name == "ruff" and command[1] in {"check", "format"}:
        # Harness files (other projects' too, committed with `git add -f`) are not the project's code: the
        # gate's own walks skip them, and a checkout that does not git-ignore them (CI's) must not lint them
        # either. On the command line, because a `lint` rule's `--isolated` reads no config file.
        command += ["--force-exclude", "--extend-exclude", ".claude-flyte"]
    if (
        Path(command[0]).name == "ruff"
        and command[1] in {"check", "format"}
        and _find_ruff_config(project) is None
        and "--isolated" not in command
    ):
        command += ["--config", str(configs / "ruff.toml")]
    if Path(command[0]).name == "tflint" and not (project / ".tflint.hcl").exists():
        command += ["--config", str(configs / "tflint.hcl")]
    if (
        Path(command[0]).name == "prettier"
        and not any(project.glob(".prettier*"))
        and not (project / "prettier.config.js").exists()
    ):
        command = _insert_before_separator(command, ["--config", str(configs / "prettier.json")])
    if (
        Path(command[0]).name == "mypy"
        and not any((project / p).exists() for p in ("mypy.ini", ".mypy.ini", "setup.cfg"))
        and not (pyproject.exists() and "[tool.mypy" in pyproject.read_text())
    ):
        command += ["--config-file", str(configs / "mypy.ini")]
    if tool == "shellcheck":
        if upstreams is not None:
            command = _drop_unchanged_operands(command, upstreams)
        scripts = [
            p
            for p in _list_files(project, _PER_FILE_GLOBS[tool])
            if upstreams is None or not upstreams.unchanged(p)
        ]
        # Paths beginning with a dash would otherwise be parsed as ShellCheck options.
        scripts = [f"./{p}" if p.startswith("-") else p for p in [*scripts, *shell_scripts]]
        command = _insert_before_separator(command, scripts)
    return command


def _insert_before_separator(command: list[str], args: list[str]) -> list[str]:
    if "--" not in command:
        return [*command, *args]
    index = command.index("--")
    return [*command[:index], *args, *command[index:]]


#: ShellCheck's options as getopt's short and long specs, to tell its options from the files it is given.
_SHELLCHECK_OPTIONS = (
    "aC:i:e:f:o:P:s:S:VW:x",
    [
        "check-sourced",
        "color=",
        "include=",
        "exclude=",
        "format=",
        "list-optional",
        "norc",
        "enable=",
        "source-path=",
        "shell=",
        "severity=",
        "version",
        "wiki-link-count=",
        "external-sources",
        "help",
    ],
)


def _list_shellcheck_operands(command: list[str]) -> list[str] | None:
    # None when getopt rejects the options; ShellCheck then reports them itself. `--color`'s value is
    # optional, which getopt cannot express, so it is given one.
    try:
        _, operands = getopt.gnu_getopt(
            ["--color=auto" if arg in ("-C", "--color") else arg for arg in command[1:]], *_SHELLCHECK_OPTIONS
        )
    except getopt.GetoptError:
        return None
    return operands


def _drop_unchanged_operands(command: list[str], upstreams: _vendored.Upstreams) -> list[str]:
    # getopt validates the option syntax, but does not preserve the order needed to rebuild the command.
    # Unknown options are left to ShellCheck, including their potential arguments.
    if _list_shellcheck_operands(command) is None:
        return command
    short, long = _SHELLCHECK_OPTIONS
    takes_value = {f"--{o[:-1]}" for o in long if o.endswith("=") and o != "color="}
    short_values = set(re.findall(r"(\w):", short)) - {"C"}
    result = command[:1]
    after_separator = False
    skip_value = False
    for arg in command[1:]:
        if skip_value:
            result.append(arg)
            skip_value = False
        elif arg == "--" and not after_separator:
            result.append(arg)
            after_separator = True
        elif not after_separator and arg.startswith("-") and arg != "-":
            result.append(arg)
            if arg.startswith("--") and "=" not in arg:
                skip_value = any(option.startswith(arg) for option in takes_value)
            elif not arg.startswith("--"):
                for index, option in enumerate(arg[1:], 1):
                    if option in short_values:
                        skip_value = index == len(arg) - 1
                        break
        elif not upstreams.unchanged(str(Path(arg))):
            result.append(arg)
    return result


def _has_no_shellcheck_files(command: list[str]) -> bool:
    # Invalid options count as files here: ShellCheck is then run as given and reports them.
    if Path(command[0]).name != "shellcheck":
        return False
    operands = _list_shellcheck_operands(command)
    return operands == [] and not any(a in command for a in ("--help", "--version", "-V", "--list-optional"))


#: The flat configs ESLint looks for in the working directory, in its order of preference.
_ESLINT_CONFIGS = tuple(f"eslint.config.{suffix}" for suffix in ("js", "mjs", "cjs", "ts", "mts", "cts"))


@contextmanager
def prepare_command(
    command: list[str], project: Path, configs: Path, upstreams: _vendored.Upstreams | None
) -> Iterator[tuple[list[str], str]]:
    """Yield `command` as the gate runs and records it, and a note for its output, with the files it names.

    With `upstreams`, ruff, ESLint and Prettier leave out the files identical to upstream's.
    """
    tool = Path(command[0]).name
    with ExitStack() as stack:

        def create(suffix: str) -> IO[str]:
            # In the project, so that a config's imports and relative paths resolve as the project's own do.
            return stack.enter_context(
                tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=suffix, dir=project)
            )

        if _has_no_shellcheck_files(command):
            # Given no file, ShellCheck prints its usage and fails; a harmless script lets it check options.
            script = stack.enter_context(tempfile.NamedTemporaryFile(mode="w", suffix=".sh"))
            script.write("#!/bin/sh\n")
            script.flush()
            yield _insert_before_separator(command, [script.name]), ""
            return
        note = ""
        excludes: list[str] = []
        globs = _PER_FILE_GLOBS.get(tool, ())
        # Asking ruff for its globs costs a run, so only when there is an upstream tree to judge against.
        judged = upstreams is not None and (upstreams.trees or upstreams.nested)
        if judged and upstreams is not None and uses_upstreams(command) and tool != "shellcheck":
            if tool == "ruff":
                globs = _list_ruff_includes(command, project)
            excludes = _list_excludes(project, upstreams, globs)
        if tool == "ruff" and excludes and upstreams is not None:
            command, note = _exclude_from_ruff(command, project, upstreams, globs, excludes, create)
        elif tool == "eslint":
            command = _configure_eslint(command, project, configs, upstreams, excludes, create)
        elif tool == "prettier" and excludes and upstreams is not None:
            command = _exclude_from_prettier(command, project, upstreams, excludes, create)
        yield command, note


#: The most bytes of excludes, or of files to check, the gate puts on a command line. Linux refuses one
#: argument over 128 KiB ("Argument list too long"), and all of them share a limit (a quarter of the stack
#: limit, commonly 2 MiB) with the environment; half the first leaves room for the rest of the command.
_MAX_ARGUMENTS = 64 * 1024

#: The most ignore patterns ESLint and Prettier are given; past it, they get the project's files instead.
#: Each pattern is matched against every file they walk: against a 40,000-file checkout, ESLint 9 took 33 s
#: with no pattern, 35 s with 50 and 60 s with 200, and 300 s did not suffice for 2,500.
_MAX_IGNORES = 50

_RUFF_CONFIG_NOTE = (
    "Ruff read no configuration nested below the project root: the files identical to upstream's were too"
    " many to name on the command line, so they were excluded through a configuration file."
)


def _list_targets(project: Path, upstreams: _vendored.Upstreams, globs: tuple[str, ...]) -> list[str]:
    # The project's own files matching `globs` (any file for none), as the fewest paths: the highest
    # directory below the root that holds none identical to upstream's, else the file.
    upstream: set[PurePosixPath] = set()  # every directory holding such a file identical to upstream's
    owned: list[PurePosixPath] = []
    for relative in _list_files(project, globs):
        path = PurePosixPath(relative)
        if upstreams.unchanged(relative):
            upstream.update(path.parents)
        else:
            owned.append(path)
    targets = set()
    for path in owned:
        below_root = [d for d in reversed(path.parents) if d != PurePosixPath(".")]
        targets.add(str(next((d for d in below_root if d not in upstream), path)))
    return sorted(targets)


def _name_targets(command: list[str], targets: list[str]) -> list[str] | None:
    # `command` checking `targets` in place of its `.` operand, None when it has none or they are too long.
    # Only `.` (every rule the gate writes, and the usual declared command) can be swapped: other operands
    # may name upstream files.
    if "." not in command[1:] or not targets or sum(len(t.encode()) + 1 for t in targets) > _MAX_ARGUMENTS:
        return None
    index = command.index(".", 1)
    # A path beginning with a dash would otherwise be read as an option.
    return [*command[:index], *(f"./{t}" if t.startswith("-") else t for t in targets), *command[index + 1 :]]


def _list_ruff_includes(command: list[str], project: Path) -> tuple[str, ...]:
    # The globs of the files ruff reads walking the project, `include` and `extend-include` as the
    # configuration the command reads resolves them (its `extend` chain and ruff's own defaults included):
    # only ruff knows them all. Ruff matches each against a file's name and its path, as `_glob_matches` does.
    # A nested configuration's own are not asked for. Ruff shows the settings of one file: of the first it
    # walks to for `.`, which may be in a checkout with its own configuration, so it is asked about an empty
    # file at the project root, which it resolves from the root's configuration. Ruff prints these settings
    # only as text (no JSON), a list either `[]` or one quoted glob per line. `--no-force-exclude` keeps a
    # project that excludes such names (`tmp*`) from refusing the file and so hiding its globs.
    settings = [command[0], "check", "--show-settings", "--no-force-exclude"]
    args = iter(command[2:])
    for arg in args:
        if arg == "--isolated" or arg.startswith("--config="):
            settings.append(arg)
        elif arg == "--config":
            settings += [arg, next(args, "")]
    with tempfile.NamedTemporaryFile(suffix=".py", dir=project) as root_file:
        code, output = _gate_base._run([*settings, Path(root_file.name).name], project, tail=None)
    found = re.findall(
        r"^file_resolver\.(?:extend_)?include = \[(?:\]|\n(.*?)^\])$", output, re.MULTILINE | re.DOTALL
    )
    if code or len(found) != 2:
        return _PER_FILE_GLOBS["ruff"]  # the command then fails on the same configuration, and says why
    # Each line is a quoted string, as JSON quotes it, and a comma.
    return tuple(json.loads(line.strip().removesuffix(",")) for block in found for line in block.splitlines())


def _exclude_from_ruff(
    command: list[str],
    project: Path,
    upstreams: _vendored.Upstreams,
    globs: tuple[str, ...],
    excludes: list[str],
    create: Callable[[str], IO[str]],
) -> tuple[list[str], str]:
    # Ruff applies a nested `ruff.toml` to the files below it, which may be stricter than the project root's.
    # An inline `--config` override adds to each configuration it discovers, so that still applies; so it
    # does to files and directories named on the command line, which `--force-exclude` keeps the project's
    # excludes applying to. A configuration file would replace them all, so it is the last resort, and the
    # output says so. `./` anchors a pattern at the project (ruff resolves it against the working
    # directory); a bare `repo` would also exclude the project's own `tools/repo`. Not `--extend-exclude`,
    # which splits its value at commas, and a file name may hold one.
    patterns = [f"./{_escape_glob(p)}" for p in excludes]
    inline = f"extend-exclude = {json.dumps(patterns, ensure_ascii=False)}"
    if len(inline.encode()) <= _MAX_ARGUMENTS:
        return [*command, "--config", inline], ""
    # Ruff lints a named path even when it is git-ignored, which walking `.` skips.
    targets = _list_targets(project, upstreams, globs)
    ignored = _list_git_ignored(project, targets)
    if (named := _name_targets(command, [t for t in targets if t not in ignored])) is not None:
        return named, ""
    return _write_ruff_config(command, project, patterns, create(".toml")), _RUFF_CONFIG_NOTE


def _list_git_ignored(project: Path, paths: list[str]) -> set[str]:
    # Asked of the repository holding each path, as ruff reads a nested checkout's `.gitignore` though the
    # project around it is no repository. `--no-index`: ruff matches the patterns without asking whether git
    # tracks the file. The session's git config is not read (`_waivers.git_env`): a global excludes file
    # there would hide the project's own files from the linter.
    by_repository: dict[Path, list[str]] = {}
    for relative in paths:
        path = project / relative
        repository = next((d for d in path.parents if (d / ".git").exists()), None)
        if repository is not None:  # outside a repository ruff reads no `.gitignore` either
            by_repository.setdefault(repository, []).append(str(path.relative_to(repository)))
    ignored: set[str] = set()
    for repository, inner in by_repository.items():
        try:
            result = subprocess.run(
                _secret_env.git_argv("check-ignore", "--no-index", "--stdin", "-z"),
                cwd=repository,
                env=_waivers.git_env(),
                input="".join(f"{p}\0" for p in inner).encode(),
                capture_output=True,
                timeout=_gate_base._TOOL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue  # those paths are then named, and checked: stricter than walking, never laxer
        if result.returncode == 0:  # 1 is "none ignored"
            found = [p for p in result.stdout.decode().split("\0") if p]
            ignored.update(str((repository / p).relative_to(project)) for p in found)
    return ignored


def _write_ruff_config(command: list[str], project: Path, patterns: list[str], config: IO[str]) -> list[str]:
    # A configuration file replaces the one ruff would find, so it `extend`s that one: the file the command
    # names (the gate's baseline), else the project's own. `--isolated` refuses a file, but one that extends
    # nothing reads no other configuration either.
    base: str | None = None
    isolated = "--isolated" in command
    kept: list[str] = []
    args = iter(command)
    for arg in args:
        if arg == "--isolated":
            continue
        value = next(args, None) if arg == "--config" else None
        if value is not None and (project / value).is_file():
            base = str(project / value)
        else:
            kept += [arg] if value is None else [arg, value]
    base = base or (None if isolated else _find_ruff_config(project))
    lines = [f"extend = {json.dumps(str(project / base), ensure_ascii=False)}"] if base else []
    # JSON strings and arrays are TOML ones.
    lines.append(f"extend-exclude = {json.dumps(patterns, ensure_ascii=False, indent=0)}")
    config.write("\n".join(lines) + "\n")
    config.flush()
    return [*kept, "--config", config.name]


def _find_option(command: list[str], *names: str) -> tuple[int, int, str] | None:
    # Where the first of these options and its value are in `command` (`-c x`, `--config x`, `--config=x`),
    # as the slice they span, and the value.
    for index, arg in enumerate(command[1:], 1):
        if arg == "--":
            break
        if arg in names and index + 1 < len(command):
            return index, index + 2, command[index + 1]
        if arg.startswith("--") and (name := arg.partition("=")[0]) in names and name != arg:
            return index, index + 1, arg.partition("=")[2]
    return None


def _configure_eslint(
    command: list[str],
    project: Path,
    configs: Path,
    upstreams: _vendored.Upstreams | None,
    excludes: list[str],
    create: Callable[[str], IO[str]],
) -> list[str]:
    # ESLint gets the gate's baseline config when the project has none. Past `_MAX_IGNORES` excludes, it is
    # given the project's files, of any extension, in place of `.`: it reads no `.gitignore`, so none is left
    # out.
    # `--no-warn-ignored` and `--no-error-on-unmatched-pattern`: a named file the project's config ignores,
    # or a named directory holding none it lints, is then skipped as walking `.` would skip it. Otherwise
    # the excludes go in a flat config that imports the one ESLint would use (the one the command names,
    # else the project's) and adds them as global ignores, rather than one `--ignore-pattern` each, which
    # outgrew the kernel's limit on a command line for a few thousand files. A legacy `.eslintrc` ESLint
    # reads an ignore file instead, and is never given the project's files: it lints a named file whatever
    # its extension, where walking `.` reads only `.js` and its `overrides`' `files`.
    flat = next((name for name in _ESLINT_CONFIGS if (project / name).is_file()), None)
    legacy = not flat and any(project.glob(".eslintrc*"))
    if upstreams is not None and not legacy and len(excludes) > _MAX_IGNORES:
        named = _name_targets(command, _list_targets(project, upstreams, _PER_FILE_GLOBS["eslint"]))
        if named is not None:
            options = ["--no-error-on-unmatched-pattern", "--no-warn-ignored"]
            command, excludes = _insert_before_separator(named, options), []
    if legacy:
        if not excludes:
            return command
        ignore = create("")
        # `--ignore-path` replaces `.eslintignore`, so the file carries its patterns too, or those of the
        # ignore file the command names, which it then replaces.
        given = _find_option(command, "--ignore-path")
        source = project / (given[2] if given else ".eslintignore")
        if source.is_file():
            ignore.write(source.read_text() + "\n")
        ignore.writelines(f"/{_escape_ignore_line(path)}\n" for path in excludes)
        ignore.flush()
        if given:
            command = [*command[: given[0]], *command[given[1] :]]
        return [*command, "--ignore-path", ignore.name]
    given = _find_option(command, "-c", "--config")
    # `--no-config-lookup` without a config of the command's: ESLint reads none, so the wrapper imports none.
    lookup = "--no-config-lookup" not in command
    own = given[2] if given else flat if lookup else None
    if not own and lookup:
        baseline = create(".mjs")
        baseline.write((configs / "eslint.config.mjs").read_text())
        baseline.flush()
        if not excludes:
            return [*command, "--config", baseline.name]
        own = Path(baseline.name).name
    if not excludes:
        return command
    if given:
        command = [*command[: given[0]], *command[given[1] :]]
    # ESLint loads a TypeScript config through a loader a `.mjs` file's import does not use.
    wrapper = create(".mts" if own and own.endswith("ts") else ".mjs")
    # A global ignore is resolved against the project directory, so a project-relative path is anchored
    # already (`./repo` would match nothing). The wrapper is not the project's code either.
    ignores = json.dumps(
        [_escape_glob(p) for p in [*excludes, Path(wrapper.name).name]], ensure_ascii=False, indent=0
    )
    if own:
        # An import is resolved against the wrapper, which is in the project, as a named config is.
        specifier = os.path.relpath(project / own, project)
        specifier = specifier if specifier.startswith("../") else f"./{specifier}"
        wrapper.write(
            f"import base from {json.dumps(specifier)};\n"
            "// A flat config exports one config object or an array of them, or a promise of either.\n"
            f"export default [...[await base].flat(), {{ ignores: {ignores} }}];\n"
        )
    else:
        wrapper.write(f"export default [{{ ignores: {ignores} }}];\n")
    wrapper.flush()
    return [*command, "--config", wrapper.name]


def _exclude_from_prettier(
    command: list[str],
    project: Path,
    upstreams: _vendored.Upstreams,
    excludes: list[str],
    create: Callable[[str], IO[str]],
) -> list[str]:
    # Past `_MAX_IGNORES` excludes, Prettier is given the project's files in place of `.`; it applies its
    # ignore files to named paths too. `--ignore-unknown` and `--no-error-on-unmatched-pattern`: a named
    # file it has no parser for, or a named directory holding none it formats, is then skipped as walking
    # `.` would skip it. Otherwise an ignore file holds the excludes: it applies to files named on the
    # command line as well as to those found in a directory.
    if len(excludes) > _MAX_IGNORES:
        named = _name_targets(command, _list_targets(project, upstreams, _PER_FILE_GLOBS["prettier"]))
        if named is not None:
            return _insert_before_separator(named, ["--ignore-unknown", "--no-error-on-unmatched-pattern"])
    ignore = create("")
    ignore.writelines(f"/{_escape_ignore_line(path)}\n" for path in excludes)
    ignore.flush()
    # Supplying any ignore path replaces Prettier's defaults.
    if not any(a == "--ignore-path" or a.startswith("--ignore-path=") for a in command):
        command = _insert_before_separator(
            command, ["--ignore-path", ".gitignore", "--ignore-path", ".prettierignore"]
        )
    return _insert_before_separator(command, ["--ignore-path", ignore.name])


def _escape_ignore_line(path: str) -> str:
    # A gitignore-syntax line (Prettier's and legacy ESLint's ignore files) takes these characters literally
    # only escaped.
    return re.sub(r"([\\*?\[\]#! ])", r"\\\1", path)
