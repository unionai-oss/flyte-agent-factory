"""Shared parsing for the UX check scripts: product.toml, the human-facing-surface test, the audience rules.

Stdlib only: the factory runs these with its own interpreter, no third-party packages.
"""

import glob
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

UI_SUFFIXES = {".html", ".htm", ".tsx", ".jsx", ".vue", ".svelte"}
# Trees whose HTML is vendored, generated or test data, not the product's own UI: a library with coverage
# HTML, a virtualenv or test fixtures has no users. Directories starting with "." (.git, .venv, .tox,
# .claude-flyte) and any directory holding a pyvenv.cfg (a virtualenv, whatever its name) are skipped too.
# `dist` is deliberately not here: a built site is often the only copy of the UI. `build` is, because Python
# packaging copies templates there; a site built into build/ is still checked once its pages are declared,
# since declared pages make a project human-facing on their own.
SKIP_DIRS = {
    "node_modules", "venv", "htmlcov", "__pycache__", "site-packages", "_build", "build",
    "tests", "test", "__tests__", "fixtures",
}  # fmt: skip
# One part of an age band: "6-8" (also with an en dash), "65+" or "adult(s)". Anything else is descriptive.
_AGE_PART = re.compile(r"(?:(\d{1,3})\s*[-\u2013]\s*(\d{1,3})|(\d{1,3})\s*\+|(adults?))", re.IGNORECASE)
MAX_AGE = 120
OLDER_ADULT_FROM = 65
UNDER_13 = 13
EXPERTISE = ("novice", "intermediate", "expert")
DEFAULT_VIEWPORTS = ["320x640", "390x844", "1280x800"]

EXAMPLE = """\
[audience]
users = "Children learning to read"
age_band = "6-8"
expertise = "novice"            # novice | intermediate | expert
reading_grade = 2               # integer 0..18
# under_13 = true               # only when age_band is not a range like "6-8", "65+" or "adult"
main_task = "Create a world and walk into its first room"

[surfaces]
pages = ["prototype/index.html"]   # the pages to render: project-relative files or http(s) URLs
# text = ["report.md"]             # files a person reads (globs); their reading level is checked
# cli = "mytool"                   # the command a person runs, for a command-line tool
# cli_invalid = ["--no-such-option"]   # arguments the tool must reject, with a non-zero exit and a message
# cli_main = ["--version"]   # a run that must succeed; its piped output must carry no escape codes"""


def find_ui_files(root: Path) -> list[Path]:
    """Return the project's UI source files (by suffix), skipping vendored and VCS directories."""
    found = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = sorted(
            d
            for d in subdirs
            if d not in SKIP_DIRS and not d.startswith(".") and not Path(directory, d, "pyvenv.cfg").exists()
        )
        found += [Path(directory, n) for n in sorted(names) if Path(n).suffix.lower() in UI_SUFFIXES]
    return found


def load_config(root: Path) -> tuple[dict[str, Any] | None, str]:
    """Return (config, "") for a parsed product.toml, (None, "") when absent, (None, why) when unreadable."""
    path = root / "product.toml"
    if not path.is_file():
        return None, ""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), ""
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        return None, f"product.toml does not parse: {error}"


def get_surfaces_table(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return the [surfaces] table, or {} when it is absent or malformed (`find_surface_problems` says so)."""
    table = (config or {}).get("surfaces", {})
    return table if isinstance(table, dict) else {}


def find_surface_problems(config: dict[str, Any] | None, root: Path | None = None) -> list[str]:
    """Return one line per malformed [surfaces] key, so a typo fails with a message, never a traceback.

    With `root`, pages and text must also stay inside the project: the reviewer has only the project.
    """
    if config is None or "surfaces" not in config:
        return []
    table = config["surfaces"]
    if not isinstance(table, dict):
        return [f"surfaces: must be a table (a [surfaces] section with pages, text, cli, ...), got {table!r}"]
    problems = []
    for key in ("pages", "text"):
        if key in table and not _is_string_list(table[key]):
            problems.append(f"surfaces.{key}: must be a list of non-empty strings, got {table[key]!r}")
    cli = table.get("cli")
    if cli is not None and not (isinstance(cli, str) and cli.strip()) and not _is_string_list(cli):
        problems.append(f"surfaces.cli: must be a command string or a list of them, got {cli!r}")
    invalid = table.get("cli_invalid")
    if invalid is not None and not (_is_string_list(invalid) and invalid):
        problems.append(
            "surfaces.cli_invalid: must be a non-empty list of argument strings the tool must reject, "
            f"got {invalid!r}"
        )
    main = table.get("cli_main")
    # An empty list is valid: it means "run the bare command", unlike cli_invalid where it would test nothing.
    if main is not None and not _is_string_list(main):
        problems.append(
            "surfaces.cli_main: must be a list of argument strings for one successful run "
            f"(an empty list runs the bare command), got {main!r}"
        )
    viewports = table.get("viewports")
    if viewports is not None and not (
        isinstance(viewports, list) and all(_is_viewport(v) for v in viewports)
    ):
        problems.append(
            "surfaces.viewports: must be a list of WIDTHxHEIGHT strings, each side 100 to 10000, "
            f"got {viewports!r}"
        )
    serve = table.get("serve")
    if serve is not None and not (isinstance(serve, str) and serve.strip()):
        problems.append(f"surfaces.serve: must be a shell command string, got {serve!r}")
    ready = table.get("ready")
    if ready is not None and not (isinstance(ready, str) and re.match(r"https?://[^/\s]", ready)):
        problems.append(f"surfaces.ready: must be an http(s) URL string, got {ready!r}")
    if serve is not None and ready is None:
        problems.append("surfaces.ready: required when serve is set (the URL that answers once it is up)")
    if root is not None:
        problems += _find_outside_problems(root, table)
        problems += _find_text_problems(root, table)
    return problems


def _is_viewport(value: Any) -> bool:
    match = re.fullmatch(r"(\d+)x(\d+)", value) if isinstance(value, str) else None
    return match is not None and all(100 <= int(n) <= 10000 for n in match.groups())


def _find_outside_problems(root: Path, table: dict[str, Any]) -> list[str]:
    """Name each declared page or text glob that is absolute or climbs out of the project."""
    problems = []
    for key in ("pages", "text"):
        values = table.get(key)
        for value in values if _is_string_list(values) else []:
            if key == "pages" and re.match(r"https?://", value):
                continue
            if Path(value).is_absolute():
                problems.append(
                    f"surfaces.{key}: {value} is an absolute path; give a path inside the project"
                )
            elif not is_inside(root, root / value):
                problems.append(f"surfaces.{key}: {value} is outside the project")
    return problems


def _find_text_problems(root: Path, table: dict[str, Any]) -> list[str]:
    """Name each [surfaces] text entry that matches no file, or matches one outside the project."""
    values = table.get("text")
    problems = []
    for pattern in values if _is_string_list(values) else []:
        if Path(pattern).is_absolute() or not is_inside(root, root / pattern):
            continue  # _find_outside_problems has named it
        problems += find_text_matches(root, pattern)[1]
    return problems


def find_text_matches(root: Path, pattern: str) -> tuple[list[Path], list[str]]:
    """Return (project-relative regular files `pattern` matches, problem lines) for a [surfaces] text entry.

    A match that resolves outside the project (a symlinked directory, say) is a problem, not a file.
    """
    found = []
    problems = []
    for match in sorted(glob.glob(pattern, root_dir=root, recursive=True)):
        if not (root / match).is_file():
            continue
        if is_inside(root, root / match):
            found.append(Path(match))
        else:
            problems.append(f"surfaces.text: {pattern} matches {match}, which is outside the project")
    if not found and not problems:
        problems.append(f"surfaces.text: {pattern} matches no file")
    return found, problems


def is_inside(root: Path, path: Path) -> bool:
    """Say whether `path`, with links and `..` resolved, is inside `root`."""
    return path.resolve().is_relative_to(root.resolve())


def find_outside_problem(root: Path, path: Path, label: str) -> str | None:
    """Return the problem line when `path` resolves outside the project (a symlink?), else None."""
    if is_inside(root, path):
        return None
    return f"{label} leads outside the project (a symlink?); the evidence must be inside it"


def _is_string_list(value: Any) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def exit_if_malformed(config: dict[str, Any] | None, root: Path) -> None:
    """Print the FAIL lines and exit 1 when [surfaces] is malformed; return otherwise."""
    problems = find_surface_problems(config, root)
    if problems:
        print("FAIL: product.toml [surfaces] is malformed")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)


def get_declared(config: dict[str, Any] | None, key: str) -> list[str]:
    """Return the distinct non-blank strings of [surfaces] <key>, in order; a lone string is a list of one."""
    value = get_surfaces_table(config).get(key)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v for v in value if isinstance(v, str) and v.strip()))


def get_declared_pages(config: dict[str, Any] | None) -> list[str]:
    """Return the strings of [surfaces] pages; empty when absent or not a list."""
    return get_declared(config, "pages")


def detect_cli(root: Path) -> str:
    """Name the command-line entry point the project's packaging declares, or return "" when there is none.

    A console script is a surface a person uses even when nobody wrote product.toml. Unreadable packaging
    files are not this check's to report, so they count as no entry point.
    """
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        pyproject = {}
    project = pyproject.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if isinstance(scripts, dict) and scripts:
        return f"pyproject.toml [project.scripts] {', '.join(sorted(scripts))}"
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        package = {}
    binary = package.get("bin") if isinstance(package, dict) else None
    if isinstance(binary, str) and binary.strip():
        return f'package.json "bin" {binary}'
    if isinstance(binary, dict) and binary:
        return f'package.json "bin" {", ".join(sorted(binary))}'
    return ""


def has_surface(root: Path, config: dict[str, Any] | None) -> bool:
    """Say whether the project has a human-facing surface.

    It has one when it declares pages, text or a CLI, names an audience at all (declaring users means someone
    uses it), has UI files, or its packaging declares a command-line entry point.
    """
    if config is not None and "audience" in config:
        return True
    if any(get_declared(config, key) for key in ("pages", "text", "cli")):
        return True
    return bool(find_ui_files(root)) or bool(detect_cli(root))


def find_audience_problems(config: dict[str, Any] | None) -> list[str]:
    """Return one line per missing or invalid [audience] field."""
    audience = (config or {}).get("audience")
    problems = []
    if audience is None:
        # Name every field even so: the person fixing it needs the list, not the table's name alone.
        problems.append("[audience]: table is missing; every field below is needed")
        audience = {}
    elif not isinstance(audience, dict):
        problems.append(f"[audience]: must be a table, got {audience!r}; every field below is needed")
        audience = {}
    for field in ("users", "age_band", "main_task"):
        value = audience.get(field)
        if value is None:
            problems.append(f"audience.{field}: missing (a non-empty string)")
        elif not isinstance(value, str) or not value.strip():
            problems.append(f"audience.{field}: must be a non-empty string, got {value!r}")
    expertise = audience.get("expertise")
    if expertise is None:
        problems.append("audience.expertise: missing (one of novice, intermediate, expert)")
    elif not isinstance(expertise, str) or expertise.strip() not in EXPERTISE:
        problems.append(f"audience.expertise: must be one of {', '.join(EXPERTISE)}, got {expertise!r}")
    problems += find_reading_grade_problems(audience)
    return problems + find_age_problems(audience)


def parse_age_band(text: str) -> tuple[bool, bool] | None:
    """Return (includes children under 13, includes older adults) for a readable age band, else None.

    Readable: parts separated by commas, semicolons or "and", each a range "6-8", an open range "65+", or
    "adult"/"adults". Older adults are those 65 or over. Descriptive text has no answer; it needs under_13.
    """
    parts = re.split(r"\s*(?:,|;|\band\b)\s*", text.strip(), flags=re.IGNORECASE)
    under_13 = older = False
    for part in parts:
        match = _AGE_PART.fullmatch(part)
        if not match:
            return None
        if match[4]:
            continue
        low = int(match[1] or match[3])
        high = int(match[2]) if match[2] else MAX_AGE
        if low > high or high > MAX_AGE:
            return None
        under_13 = under_13 or low < UNDER_13
        older = older or low >= OLDER_ADULT_FROM
    return under_13, older


def find_age_problems(audience: dict[str, Any]) -> list[str]:
    """Return the problem with audience.under_13 or its fit with audience.age_band, if any."""
    under_13 = audience.get("under_13")
    if under_13 is not None and not isinstance(under_13, bool):
        return [f"audience.under_13: must be true or false, got {under_13!r}"]
    age_band = audience.get("age_band")
    if not isinstance(age_band, str) or not age_band.strip():
        return []  # the missing or invalid age_band is reported by find_audience_problems
    parsed = parse_age_band(age_band)
    if parsed is None:
        if under_13 is None:
            return [
                f"audience.under_13: age_band {age_band!r} is not a range like 6-8 or 65+ or adult, so set "
                "audience.under_13 = true or false (true when children under 13 use the product)"
            ]
        return []
    if under_13 is not None and under_13 != parsed[0]:
        reads = "includes" if parsed[0] else "does not include"
        return [
            f"audience.under_13 = {str(under_13).lower()} contradicts audience.age_band {age_band!r}, "
            f"which {reads} children under 13"
        ]
    return []


def serves_under_13(config: dict[str, Any] | None) -> bool:
    """Say whether the declared audience includes children under 13 (the stricter reading when unclear)."""
    audience = (config or {}).get("audience")
    if not isinstance(audience, dict):
        return False
    age_band = audience.get("age_band")
    parsed = parse_age_band(age_band) if isinstance(age_band, str) else None
    return bool(parsed and parsed[0]) or audience.get("under_13") is True


def serves_older_adults(config: dict[str, Any] | None) -> bool:
    """Say whether the declared age band is for older adults: one of its parts starts at 65 or over."""
    audience = (config or {}).get("audience")
    age_band = audience.get("age_band") if isinstance(audience, dict) else None
    parsed = parse_age_band(age_band) if isinstance(age_band, str) else None
    return bool(parsed and parsed[1])


def find_reading_grade_problems(audience: dict[str, Any]) -> list[str]:
    """Return the problem with audience.reading_grade, if any."""
    grade = audience.get("reading_grade")
    if grade is None:
        return ["audience.reading_grade: missing (an integer 0..18)"]
    # bool is an int in Python; `reading_grade = true` is not a grade.
    if isinstance(grade, bool) or not isinstance(grade, int) or not 0 <= grade <= 18:
        return [f"audience.reading_grade: must be an integer 0..18, got {grade!r}"]
    return []


def exit_not_applicable(reason: str) -> NoReturn:
    """Print the not-applicable marker the factory looks for on the first line, and exit 0."""
    print(f"NOT APPLICABLE: {reason}")
    sys.exit(0)
