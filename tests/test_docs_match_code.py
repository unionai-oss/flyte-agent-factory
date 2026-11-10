"""The docs name only what the code defines, mention every pluggable setting, and link only to what exists.

Three checks over README.md and docs/*.md:

* names: an inline-code identifier that looks like an MCP task (`factory.<name>`), a factory environment
  variable, a policy key in a "Policy key" table, or a role in a "Role" table must be defined in the code;
* coverage: every policy key, every fixed variable `_names.py` lists, and every tool the factory MCP
  exposes is mentioned in one of the pages that document what is pluggable;
* links: every relative markdown link resolves to a file, and its `#anchor` to a heading.

Fenced code blocks are not read: they hold commands and sample output, not claims. Placeholders such as
`AGENT_FACTORY_<ROLE>_MODEL` do not match the name patterns (the `<`), so they are skipped by construction.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from flyte_agent_factory import _factory_mcp, _names, _policy, _profile, _review, _usage
from flyte_agent_factory._roles import ROLE_ALIASES

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "flyte_agent_factory"
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
#: The pages that say what is pluggable. Coverage (b) looks here and nowhere else.
PLUGGABLE_DOCS = [
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "engines.md",
    ROOT / "profiles" / "README.md",
]

_FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_NEW_ENV = re.compile(r"AGENT_FACTORY_[A-Z0-9_]+")
_OLD_ENV = re.compile(r"CLAUDE_(?:FACTORY|AGENT|FLYTE|BROWSER|SUBAGENT)_[A-Z0-9_]+")
_TASK = re.compile(r"factory\.[a-z_]+")
_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)\)")


def _prose(path: Path) -> str:
    """Return the page without fenced code blocks."""
    return _FENCE.sub("", path.read_text())


def _spans(path: Path) -> list[str]:
    return _CODE_SPAN.findall(_prose(path))


def _tables(path: Path, first_header: str) -> list[list[str]]:
    """Return, per markdown table whose first header cell is `first_header`, its first-column cells."""
    tables: list[list[str]] = []
    rows: list[str] | None = None  # the open table's rows, or None between tables
    wanted = False
    for line in _prose(path).splitlines():
        if not line.startswith("|"):
            rows = None
            continue
        cell = line.strip().strip("|").split("|")[0].strip()
        if rows is None:  # the header row
            rows, wanted = [], cell.lower() == first_header.lower()
            if wanted:
                tables.append(rows)
        elif wanted and not set(cell) <= set("-: "):
            rows.append(cell)
    return tables


def _first_column_names(path: Path, first_header: str) -> list[str]:
    return [m for table in _tables(path, first_header) for cell in table for m in _CODE_SPAN.findall(cell)]


# ---- what the code defines ----


def _factory_tasks() -> set[str]:
    """The `@env.task` functions of `_factory.py`, which Flyte names `factory.<function>`."""
    tree = ast.parse((PACKAGE / "_factory.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if ast.unparse(target) == "env.task":
                    found.add(f"factory.{node.name}")
    return found


def _policy_keys() -> set[str]:
    """Every policy key the code reads, found where it reads them rather than from a copy of the list."""
    keys = set(_profile._NUMERIC_POLICY) | set(_profile._ENGINE_KEYS) | {"COORDINATOR_ENGINE"}
    for role in _policy.DEFAULTS:
        keys |= {f"{role.upper()}_MODEL", f"{role.upper()}_EFFORT"}
    for role in _policy.CODEX_DEFAULTS:
        keys |= {f"{role.upper()}_CODEX_MODEL", f"{role.upper()}_CODEX_EFFORT"}
    # `_usage._static_engine` reads `<PURPOSE>_ENGINE` for each purpose a call is routed for: the author
    # (the coordinator), the delegate workers, the planner, the contract writer, the arbiter and the judges
    # (routed by their concern, not as "judge").
    purposes = set(_usage._QA_ROLES) | {"author", "delegate", "planner", "contract", "arbiter"}
    keys |= {f"{purpose.upper()}_ENGINE" for purpose in purposes}
    for source in PACKAGE.glob("*.py"):
        keys |= set(re.findall(r'_policy\.read\(\s*"([A-Z0-9_]+)"', source.read_text()))
    return keys


def _env_names() -> set[str]:
    """Every factory variable: the fixed ones in `_names.py`, the policy keys, and literals in the source."""
    names = set(_names.FIXED)
    names |= {_names.PREFIX + key for key in _policy_keys()}
    for source in [*PACKAGE.glob("*.py"), *(ROOT / "ops").rglob("*.py")]:
        names |= set(_NEW_ENV.findall(source.read_text()))
    return names


def _roles() -> set[str]:
    shipped = {p.stem for p in (PACKAGE / "_assets" / "agents").glob("*.md")}
    # The review loop reports a judge as `judge-<concern>`, the planner as `plan` and the arbiter as
    # `arbitrate`; `contract` and `delegate` are the purposes `_policy` already names.
    review = {f"judge-{concern}" for concern, _ in _review._JUDGES} | {"plan", "arbitrate", "contract"}
    assert {"planner", "arbiter", "contract", "delegate"} <= set(_policy.CODEX_DEFAULTS)
    return shipped | set(ROLE_ALIASES) | review | {"delegate"}


def _mcp_names() -> set[str]:
    return set(_factory_mcp.TOOLS) | {f"factory.{task}" for task in _factory_mcp.CHAT_TASKS}


# ---- (a) names in the docs are defined ----


@pytest.mark.parametrize("page", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_names_in_the_docs_are_defined_in_the_code(page):
    tasks, env, keys, roles = _factory_tasks(), _env_names(), _policy_keys(), _roles()
    problems = []
    for span in _spans(page):
        name = span.split("=", 1)[0]
        if _TASK.fullmatch(span) and span not in tasks:
            problems.append(f"{span}: no such factory task")
        elif _NEW_ENV.fullmatch(name) and name not in env:
            problems.append(f"{name}: no such environment variable")
        elif _OLD_ENV.fullmatch(name):
            problems.append(f"{name}: obsolete environment variable")
    problems += [f"{k}: not a policy key" for k in _first_column_names(page, "Policy key") if k not in keys]
    problems += [f"{r}: not a role" for r in _first_column_names(page, "Role") if r not in roles]
    assert not problems, f"{page.name} names things the code does not define:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    ("family", "suffix"),
    [
        ("FACTORY", "PROFILE"),
        ("AGENT", "ENV_NAME"),
        ("FLYTE", "BASE"),
        ("BROWSER", "ISOLATED"),
        ("SUBAGENT", "ENV_NAME"),
    ],
)
@pytest.mark.parametrize("assignment", ["", "=example"])
def test_the_docs_guard_rejects_obsolete_environment_names(family, suffix, assignment, tmp_path):
    name = f"CLAUDE_{family}_{suffix}"
    page = tmp_path / "obsolete.md"
    page.write_text(f"Select the setting with `{name}{assignment}`.\n")
    with pytest.raises(AssertionError, match=re.escape(f"{name}: obsolete environment variable")):
        test_names_in_the_docs_are_defined_in_the_code(page)


def test_the_docs_guard_accepts_current_and_claude_code_environment_names(tmp_path):
    page = tmp_path / "current.md"
    page.write_text("Use `AGENT_FACTORY_PROFILE=example` and `CLAUDE_CODE_OAUTH_TOKEN`.\n")
    test_names_in_the_docs_are_defined_in_the_code(page)


# ---- (b) every pluggable setting is documented ----


def test_every_pluggable_setting_is_documented():
    text = "\n".join(page.read_text() for page in PLUGGABLE_DOCS)
    wanted = _policy_keys() | _names.FIXED | _mcp_names()
    missing = sorted(
        name
        for name in wanted
        if not re.search(rf"(?<![A-Za-z0-9_.]){re.escape(name)}(?![A-Za-z0-9_])", text)
    )
    assert not missing, (
        "not mentioned in " + ", ".join(str(p.relative_to(ROOT)) for p in PLUGGABLE_DOCS) + f": {missing}"
    )


def test_the_roles_tables_exist():
    """A page that lost its tables would pass the name check by having nothing to check."""
    arch = ROOT / "docs" / "architecture.md"
    assert arch.exists(), "docs/architecture.md is missing"
    assert {"scope", "build", "verify", "judge-correctness", "arbitrate"} <= set(
        _first_column_names(arch, "Role")
    )
    assert {"AUTHOR_MODEL", "CORRECTNESS_ENGINE"} <= set(_first_column_names(arch, "Policy key"))


# ---- (c) links resolve ----


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"`|\*\*|\*|_(?=\W|$)|(?<=\W)_", "", heading.strip()).lower()
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in _prose(path).splitlines():
        if match := re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line):
            slug = _slug(match.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            out.add(slug if count == 0 else f"{slug}-{count}")
    return out


@pytest.mark.parametrize("page", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(page):
    problems = []
    text = _CODE_SPAN.sub("", _prose(page))
    for target in _LINK.findall(text):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue
        path_part, _, anchor = target.partition("#")
        resolved = page if not path_part else (page.parent / path_part).resolve()
        if not resolved.exists():
            problems.append(f"{target}: no such file")
        elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
            problems.append(f"{target}: no heading with that anchor")
    assert not problems, f"{page.name} has broken links:\n" + "\n".join(problems)
