"""How the gate runs the declared kinds of a rule's `Enforced:` line that need more than a command."""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path, PurePosixPath

from . import _gate_base, _standards


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


def _forbidden(project: Path, rule: _standards.Rule) -> tuple[int, str]:
    """Run a `forbid` rule: the `file:line` of every line of the matching text files that matches."""
    pattern, globs = _standards.forbid_parts(rule)
    regex = re.compile(pattern)
    found = []
    for relative in sorted(_gate_base._project_files(project)):
        if globs and not any(_glob_matches(relative, g) for g in globs):
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
    if not found:
        return 0, f"no line matches {pattern!r}"
    shown = found[:100]
    return 1, "\n".join(shown + ([f"... and {len(found) - 100} more"] if len(found) > 100 else []))


def with_baseline_config(
    command: list[str], project: Path, configs: Path, shell_scripts: tuple[str, ...]
) -> list[str]:
    """`command` with the baseline config of its tool added when the project has none of its own."""
    pyproject = project / "pyproject.toml"
    configured_ruff = any((project / p).exists() for p in ("ruff.toml", ".ruff.toml")) or (
        pyproject.exists() and re.search(r"\[tool\.ruff(?:\]|\.)", pyproject.read_text())
    )
    if Path(command[0]).name == "ruff" and command[1] in {"check", "format"}:
        # Harness files (other projects' too, committed with `git add -f`) are not the project's code: the
        # gate's own walks skip them, and a checkout that does not git-ignore them (CI's) must not lint them
        # either. On the command line, because a `lint` rule's `--isolated` reads no config file.
        command += ["--extend-exclude", ".claude-flyte"]
    if (
        Path(command[0]).name == "ruff"
        and command[1] in {"check", "format"}
        and not configured_ruff
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
        command += ["--config", str(configs / "prettier.json")]
    if (
        Path(command[0]).name == "mypy"
        and not any((project / p).exists() for p in ("mypy.ini", ".mypy.ini", "setup.cfg"))
        and not (pyproject.exists() and "[tool.mypy" in pyproject.read_text())
    ):
        command += ["--config-file", str(configs / "mypy.ini")]
    if Path(command[0]).name == "shellcheck":
        command += [
            str(p.relative_to(project))
            for p in project.rglob("*.sh")
            if not any(part in {".venv", "node_modules", ".git", ".claude-flyte"} for part in p.parts)
        ]
        command += shell_scripts
    return command
