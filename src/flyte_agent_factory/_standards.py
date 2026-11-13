"""Profile-selected standards, captured before deployment and selected again at review."""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import shlex
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

ROOT = Path(__file__).resolve().parents[2]
MAX_PAYLOAD = 48_000
MAX_FILES = 512_000
TEXT_EXTENSIONS = {
    ".md",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".hcl",
    ".txt",
    ".cfg",
    ".ini",
    ".sh",
    ".py",
    ".rules",
    ".conf",
}
# The ID grammar of docs/standards.md: letters, digits, `.`, `_` and `-`, starting with a letter.
RULE = re.compile(r"^## ([A-Za-z][A-Za-z0-9._-]*) (MUST|SHOULD)\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
#: The checks built into the factory, which an `Enforced: check <name>` line may name.
FAIL_TO_PASS = "factory:fail-to-pass"
MODULE_SIZE = "factory:module-size"
LANGUAGE_SUMMARY = "factory:language-summary"
UI_EVIDENCE = "factory:ui-evidence"
SUPPRESSIONS = "factory:suppressions"
BUILTIN_CHECKS = (FAIL_TO_PASS, MODULE_SIZE, LANGUAGE_SUMMARY, UI_EVIDENCE, SUPPRESSIONS)
NO_STANDARDS = "no standards configured"
_LINT_CODES = re.compile(r"[A-Z]+[0-9]*(?:,[A-Z]+[0-9]*)*")
GUIDANCE = (
    "Engineering and security standards are in `.claude-flyte/standards/`, alongside applicable "
    "language packs and their tool configs. Precedence: brief > project's own DESIGN.md > standards; "
    "DESIGN.md may override a standard with a stated reason. Failing checks, tool-enforced MUST rules, "
    "evidenced correctness/completeness findings and scope violations block review. Craft, unevidenced "
    "findings and SHOULD "
    "findings are non-blocking. Read the selected files before implementing. Brand standards apply "
    "only to visual or customer-facing deliverables. If `brand.md` is present here, follow it; "
    "do not redecide the configured brand for demos, reports or other customer-facing work.\n\n"
)


@dataclass(frozen=True)
class Rule:
    """One rule of a standards file, with how it is enforced (see docs/standards.md)."""

    ident: str
    level: str
    body: str
    #: The standards file it came from, relative to the standards directory (`languages/python.md`).
    file: str
    #: judgement, check, command, lint, forbid, script or tool (bare); "" when `error` is set.
    kind: str = ""
    #: What follows the kind on the `Enforced:` line.
    arg: str = ""
    #: Why the `Enforced:` line cannot be used; empty when it can.
    error: str = ""

    @property
    def must(self) -> bool:
        return self.level == "MUST"

    @property
    def language_pack(self) -> bool:
        parts = PurePosixPath(self.file).parts
        return len(parts) == 2 and parts[0] == "languages"

    @property
    def brand(self) -> bool:
        return self.file == "brand.md"

    @property
    def lint_codes(self) -> list[str]:
        """The ruff codes a MUST rule selects, which the suppression policy protects."""
        if not self.must:
            return []
        if self.kind == "lint":
            return self.arg.split()[1].split(",")
        if self.kind == "command":
            return [
                code
                for selected in re.findall(r"--(?:extend-)?select(?:=|\s+)([A-Z0-9,]+)", self.arg)
                for code in selected.split(",")
            ]
        return []


def split_commands(arg: str) -> list[list[str]]:
    """Split `command` text on `&&` into argv lists, quote-aware: `-c "print('A && B')"` is one argument.

    Raises ValueError for unbalanced quotes, an empty command beside `&&`, a lone `&`, or no command at all.
    A quoted `"&&"` on its own is indistinguishable from the separator once shlex has stripped the quotes.
    """
    lexer = shlex.shlex(arg, posix=True, punctuation_chars="&")
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        raise ValueError("command has unbalanced quotes") from None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            commands.append([])
        elif set(token) == {"&"}:
            raise ValueError("empty command beside &&")
        else:
            commands[-1].append(token)
    if not all(commands):
        raise ValueError("empty command beside &&")
    return commands


def _parse_enforced(body: str) -> tuple[str, str, str]:
    """Return (kind, arg, error) for the `Enforced:` line of a rule's body."""
    lines = re.findall(r"^Enforced:[ \t]*(.*?)[ \t]*$", body, re.MULTILINE)
    if len(lines) != 1:
        return "", "", "needs exactly one Enforced: line" + (f", found {len(lines)}" if lines else "")
    kind, _, arg = lines[0].partition(" ")
    arg = arg.strip()
    if kind == "tool" and not arg:
        return "tool", "", ""  # a bare `tool` is its own kind
    if kind in {"tool", "command", "script"} and arg:
        # `tool <cmd>` is an alias of `command <cmd>`. Parse now so a quoting error is the rule's error.
        try:
            commands = split_commands(arg)
        except ValueError as exc:
            return "", "", str(exc)
        if kind == "script" and len(commands) > 1:
            return "", "", "script runs one script, not several commands"
        return ("script" if kind == "script" else "command"), arg, ""
    if kind == "judgement":
        return ("judgement", "", "") if not arg else ("", "", "judgement takes no argument")
    if kind == "check":
        if arg not in BUILTIN_CHECKS:
            return "", "", f"unknown check {arg!r}; the built-in checks are {', '.join(BUILTIN_CHECKS)}"
        return "check", arg, ""
    if kind == "command":
        return "", "", "command needs a command to run"
    if kind == "lint":
        words = arg.split()
        if len(words) != 2 or words[0] != "ruff" or not _LINT_CODES.fullmatch(words[1]):
            return "", "", "lint needs `ruff <codes>`, such as `lint ruff E722,BLE001`"
        return "lint", arg, ""
    if kind == "forbid":
        pattern = _split_forbid(arg)[0]
        try:
            re.compile(pattern)
        except re.error as exc:
            return "", "", f"forbid pattern {pattern!r} is not a regular expression: {exc}"
        return ("forbid", arg, "") if pattern else ("", "", "forbid needs a regular expression")
    if kind == "script":
        return "", "", "script needs a path"
    return "", "", f"unknown kind {kind!r}"


def _split_forbid(arg: str) -> tuple[str, list[str]]:
    """Return the regex and the globs of `forbid <regex> [in <glob>[,<glob>...]]` (no globs: every file)."""
    pattern, _, globs = arg.rpartition(" in ")
    if pattern and globs and " " not in globs:
        return pattern.strip(), globs.split(",")
    return arg, []


def forbid_parts(rule: Rule) -> tuple[str, list[str]]:
    return _split_forbid(rule.arg)


def rules(files: dict[str, str]) -> list[Rule]:
    """Every rule of the `.md` files in `files`, in file order. README files are for people, not parsed."""
    found = []
    for name, text in files.items():
        if not name.endswith(".md") or PurePosixPath(name).name == "README.md" or "/configs/" in name:
            continue
        for ident, level, body in RULE.findall(text):
            kind, arg, error = _parse_enforced(body)
            found.append(Rule(ident, level, body, name, kind, arg, error))
    return found


def check_rules(files: dict[str, str]) -> dict[str, list[str]]:
    """Return the IDs of the MUST rules that map each built-in check (`Enforced: check <name>`)."""
    mapped: dict[str, list[str]] = {}
    for rule in rules(files):
        if rule.must and rule.kind == "check":
            ids = mapped.setdefault(rule.arg, [])
            if rule.ident not in ids:
                ids.append(rule.ident)
    return mapped


@dataclass(frozen=True)
class Standards:
    dir: str = ""
    include: tuple[str, ...] = ("engineering", "security")
    languages: str | tuple[str, ...] = "auto"
    brand: str = ""


class _Selections(TypedDict):
    include: tuple[str, ...]
    languages: Literal["auto"] | tuple[str, ...]


def settings(table: dict[str, object]) -> Standards:
    unknown = set(table) - {"dir", "include", "languages", "brand"}
    if unknown:
        raise ValueError(f"[standards] unknown keys: {', '.join(sorted(unknown))}")
    for key in ("dir", "brand"):
        if key in table and not isinstance(table[key], str):
            raise ValueError(f"[standards] {key} must be a string")
    values: dict[str, str | tuple[str, ...]] = {}
    for key, default in (("include", ["engineering", "security"]), ("languages", "auto")):
        value = table.get(key, default)
        if key == "languages" and value == "auto":
            values[key] = value
            continue
        if not isinstance(value, list) or not all(
            isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9_-]*", v) for v in value
        ):
            raise ValueError(f"[standards] {key} must be a list of simple filenames")
        # The validation above establishes the element type; mypy cannot narrow through all().
        values[key] = tuple(cast(list[str], value))
    # The checks above establish the string fields and both selection keys.
    return Standards(
        dir=cast(str, table.get("dir", "")),
        brand=cast(str, table.get("brand", "")),
        **cast(_Selections, values),
    )


def _resolve(value: str, base: Path, root: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    local = base / path
    if local.exists() or root is None:
        return local
    fallback = root / path
    return fallback if fallback.exists() else local


def _read_standards(directory: Path) -> dict[str, str]:
    """Read UTF-8 standards, excluding binaries and hidden or cache directories."""
    files = {}
    for parent, children, names in os.walk(directory, followlinks=False):
        # Running a tool in a standards checkout leaves its state there: `ruff check .` treats
        # languages/configs/ruff.toml as a project root and writes .ruff_cache/ beside it.
        children[:] = [n for n in children if not n.startswith(".") and n != "__pycache__"]
        for filename in names:
            path = Path(parent) / filename
            name = str(path.relative_to(directory))
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                if path.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                raise ValueError(f"[standards] {name} is not UTF-8 text: {path}") from exc
            except OSError as exc:
                raise ValueError(f"[standards] {name} cannot be read: {exc}") from exc
            # A NUL byte marks a binary file that happens to decode as UTF-8.
            if "\0" in text:
                if path.suffix.lower() not in TEXT_EXTENSIONS:
                    continue
                raise ValueError(f"[standards] {name} is not UTF-8 text: {path}")
            files[name] = text
    return files


def capture(config: Standards, base: Path, *, root: Path | None = None) -> dict[str, str]:
    """Read the configured standards directory; with none configured there are no rules (and no files)."""
    directory = _resolve(config.dir, base, root) if config.dir else None
    if directory is None:
        files: dict[str, str] = {}
    elif directory.is_dir():
        files = _read_standards(directory)
    else:
        tried = f" (also tried {root / config.dir})" if root and not Path(config.dir).is_absolute() else ""
        raise ValueError(f"[standards] dir does not exist: {directory}{tried}")
    if directory is not None:
        for name in config.include:
            if f"{name}.md" not in files:
                raise ValueError(f"[standards] missing base file: {name}.md")
        if config.languages != "auto":
            for name in config.languages:
                if f"languages/{name}.md" not in files:
                    raise ValueError(f"[standards] missing language pack: {name}")
    # All packs travel together: a new language may first appear after startup.
    selected = {
        n: t
        for n, t in files.items()
        if n in {f"{v}.md" for v in config.include}
        or n.startswith("languages/")
        or n in {"README.md", "demo.md"}
        or _is_support_file(n)
    }
    selected.update({n: files[n] for n in referenced_files(selected) if n in files})
    if config.brand:
        selected["brand.md"] = _resolve(config.brand, base, root).read_text()
    return selected


def _is_support_file(name: str) -> bool:
    """Say whether `name` is a file the rules refer to (a script), kept in a non-pack directory."""
    parts = PurePosixPath(name).parts
    return len(parts) > 1 and parts[0] != "languages"


def evidence_rules(must: list[Rule]) -> list[str]:
    """Return the IDs of the rules that name no command of their own: a bare `tool`, or UI evidence."""
    return [r.ident for r in must if r.kind == "tool" or (r.kind == "check" and r.arg == UI_EVIDENCE)]


def referenced_files(files: dict[str, str]) -> set[str]:
    """Return the files the `script` rules of `files` run, which must travel with them wherever they sit."""
    return {split_commands(r.arg)[0][0] for r in rules(files) if r.kind == "script" and not r.error}


def encode(files: dict[str, str]) -> str:
    raw = json.dumps(files).encode()
    if len(raw) > MAX_FILES:
        raise ValueError("[standards] files exceed 512 KB; reduce the selected standards")
    payload = base64.b64encode(zlib.compress(raw)).decode()
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("[standards] compressed payload exceeds 48 KB; reduce the selected standards")
    return payload


def decode(payload: str) -> dict[str, str]:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("[standards] payload exceeds 48 KB")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(base64.b64decode(payload, validate=True), MAX_FILES + 1)
    if len(raw) > MAX_FILES or not decompressor.eof:
        raise ValueError("[standards] invalid or oversized bundle")
    files = json.loads(raw)
    if not isinstance(files, dict) or not all(
        isinstance(n, str)
        and isinstance(t, str)
        and n
        and PurePosixPath(n).parts
        and not n.endswith("/")
        and "\\" not in n
        and not PurePosixPath(n).is_absolute()
        and ".." not in PurePosixPath(n).parts
        for n, t in files.items()
    ):
        raise ValueError("[standards] invalid bundle filenames or contents")
    return files


def demo_kind(brief: str, package: str = "") -> str:
    """Return the demo kind a launch asked for, or "" for an ordinary project.

    Only two things make a project a demo: a demo package name, or a brief line that starts
    `Demo kind: pattern|showcase`. Guessing from the brief's wording once turned "Do not build a
    demo." and a heading about demos into demos.
    """
    kind = re.search(r"^Demo kind:\s*(pattern|showcase)\b", brief, re.MULTILINE | re.IGNORECASE)
    if kind:
        return kind[1].lower()
    return "pattern" if package else ""


def _detects(path: str, pattern: str) -> bool:
    return (
        fnmatch.fnmatch(path, pattern)
        or ("/" not in pattern and fnmatch.fnmatch(PurePosixPath(path).name, pattern))
        or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))
    )


def select(
    files: dict[str, str],
    config: Standards,
    project: Path,
    *,
    demo: str = "",
    fetch: bool = True,
    trusted_base: str = "",
) -> dict[str, str]:
    """Return the standards files that apply to `project`: its language packs, configs and referenced files.

    A vendored file identical to upstream says nothing about the project's languages (`_vendored`). With
    `fetch=False` upstream is read only from the workspace and the cache: a snapshot on the supervisor's event
    loop must never wait on the network. `trusted_base` is the commit the project was created at.
    """
    # Imported here: `_vendored` reaches modules that import this one.
    from . import _gate_base, _vendored

    selected = {f"{n}.md": files[f"{n}.md"] for n in config.include if f"{n}.md" in files}
    paths: list[str] = []
    # Prune dependency and harness trees before walking: they are not project sources.
    for directory, children, names in os.walk(project):
        children[:] = [n for n in children if n not in _gate_base._SKIP_DIRS]
        paths.extend(str((Path(directory) / n).relative_to(project)) for n in names)
    upstreams: list[_vendored.Upstreams] = []
    owned: dict[str, bool] = {}

    def owns(path: str) -> bool:
        # Hashed only for a path a pattern matched, and the trees read once: a snapshot runs on every flush.
        if path not in owned:
            if not upstreams:
                upstreams.append(
                    _vendored.load(project, _gate_base._SKIP_DIRS, trusted_base=trusted_base, fetch=fetch)
                )
            owned[path] = not upstreams[0].unchanged(path)
        return owned[path]

    for name, text in files.items():
        if not name.startswith("languages/") or len(PurePosixPath(name).parts) != 2:
            continue
        match = re.search(r"^detect:\s*(.+)$", text, re.MULTILINE)
        patterns = [p.strip() for p in match[1].split(",")] if match else []
        detected = any(_detects(p, pattern) and owns(p) for p in paths for pattern in patterns)
        if (config.languages == "auto" and detected) or (
            config.languages != "auto" and PurePosixPath(name).stem in config.languages
        ):
            selected[name] = text
    selected.update(
        {n: t for n, t in files.items() if n.startswith("languages/configs/") or _is_support_file(n)}
    )
    selected.update({n: files[n] for n in referenced_files(selected) if n in files})
    if demo and "demo.md" in files:
        demo_text = files["demo.md"]
        if demo == "pattern":
            # "Pattern demos replace QA-3, QA-9 and SEC-2." A rule ID may contain a full stop, so the
            # sentence ends at one followed by whitespace.
            replaced = re.search(r"\breplaces? (.+?)\.(?=\s|\Z)", demo_text)
            ids = set(re.findall(r"[A-Za-z][A-Za-z0-9._-]*", replaced[1])) if replaced else set()
            selected = {n: RULE.sub(lambda m: "" if m[1] in ids else m[0], t) for n, t in selected.items()}
            selected["demo.md"] = demo_text
        else:
            selected["demo.md"] = "".join(
                m[0] for m in RULE.finditer(demo_text) if re.search(r"^Applies:[ \t]*showcase\b", m[3], re.M)
            )
    if "brand.md" in files:
        selected["brand.md"] = files["brand.md"]
    return selected


def materialize(
    workspace: Path,
    project: Path,
    config: Standards,
    files: dict[str, str],
    *,
    demo: str = "",
    fetch: bool = True,
    trusted_base: str = "",
) -> dict[str, str]:
    selected = select(files, config, project, demo=demo, fetch=fetch, trusted_base=trusted_base)
    directory = workspace / ".claude-flyte/standards"
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.rglob("*"):
        if old.is_file() and str(old.relative_to(directory)) not in selected:
            old.unlink()
    for name, text in selected.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if text.startswith("#!"):
            # A `script` rule may run its script directly; the bundle does not carry the executable bit.
            target.chmod(0o755)
    return selected


def review_text(
    files: dict[str, str], budget: int = 24_000, *, must_only: bool = False, judgement_only: bool = False
) -> str:
    chosen = [
        (rule.level, f"{rule.file}: {rule.ident} {rule.level}\n{rule.body.strip()}\n")
        for rule in rules(files)
        if (not must_only or rule.must)
        and (not judgement_only or rule.kind == "judgement" or "\nJudged:" in rule.body)
    ]
    chosen.sort(key=lambda rule: rule[0] != "MUST")
    result = ""
    for _, text in chosen:
        if len(result) + len(text) + 1 <= budget:
            result += text + "\n"
    return result
