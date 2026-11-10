"""The standards set the tests run against: the workspace template's, read at run time.

The engine ships no rules of its own, so a test that needs one finds it in the template set by what it is
(its `Enforced:` kind and its file), never by an ID it spells out.
"""

from __future__ import annotations

from pathlib import Path

from flyte_agent_factory import _standards

TEMPLATE = (
    Path(__file__).resolve().parents[1] / "src/flyte_agent_factory/_assets/workspace_template/standards"
)
#: Every file of the template set, by its name relative to the standards directory.
FILES: dict[str, str] = _standards._read_standards(TEMPLATE)
RULES: list[_standards.Rule] = _standards.rules(FILES)


def find(*, kind: str | None = None, arg: str | None = None, file: str | None = None, text: str = "") -> str:
    """Return the ID of the one template rule with this kind, argument, file and text in its body."""
    found = [
        r.ident
        for r in RULES
        if (kind is None or r.kind == kind)
        and (arg is None or r.arg == arg)
        and (file is None or r.file == file)
        and text in r.body
    ]
    assert len(found) == 1, (kind, arg, file, text, found)
    return found[0]


def rule_text(ident: str) -> str:
    """The `## ID LEVEL` section of the template rule `ident`, as written in its file."""
    for rule in RULES:
        if rule.ident == ident:
            return f"## {rule.ident} {rule.level}\n{rule.body}"
    raise KeyError(ident)


FAIL_TO_PASS = find(kind="check", arg="factory:fail-to-pass")
MODULE_SIZE = find(kind="check", arg="factory:module-size")
LANGUAGE_SUMMARY = find(kind="check", arg="factory:language-summary")
UI_EVIDENCE = find(kind="check", arg="factory:ui-evidence")
#: The exception-handling rule: `lint ruff` codes that the suppression policy protects.
EXCEPTIONS = find(kind="lint")
RUFF_AND_FORMAT = find(kind="command", arg="ruff check . && ruff format --check .")
PYTEST = find(kind="command", arg="python -m pytest")
MYPY = find(kind="command", arg="mypy .")
DOCSTRINGS = find(kind="command", arg="ruff check --select D .")
MUTABLE_DEFAULTS = find(kind="command", arg="ruff check --select B006 .")
SHELLCHECK = find(kind="command", arg="shellcheck")
PROMOTION = find(kind="judgement", file="engineering.md", text="promot")
#: The rules the template's demo file says a pattern demo replaces, and two of its own.
DEPLOYMENT_CONFIG = find(file="engineering.md", text="Real deployment profiles")
DEMO_TEST = find(file="demo.md", text="functional test that runs it end to end")
SHOWCASE_RULE = find(file="demo.md", text="one documented command")
PATTERN_DEMO = find(file="engineering.md", text="Pattern demos (code-walkthrough")
#: The first rule of the security file.
SECURITY = next(r.ident for r in RULES if r.file == "security.md")
