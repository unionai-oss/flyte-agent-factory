"""Evidence rules: a MUST rule that is `Enforced: tool` with no command names no check of its own.

A project declares the command that satisfies such a rule in `factory-evidence.toml` at its root:

    [evidence]
    "<RULE-ID>" = "npx playwright test --reporter=line"

`_gates.standards_results` runs it like any tool rule's command. A command for a rule that has a check of
its own is refused: the file may supply what is missing, never replace a check.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

FILE = "factory-evidence.toml"


def declared(project: Path, evidence_rules: Iterable[str]) -> tuple[dict[str, str], list[str]]:
    """Return the project's declared commands for `evidence_rules`, and what is wrong with the file."""
    path = project / FILE
    if not path.is_file():
        return {}, []
    try:
        table = tomllib.loads(path.read_text()).get("evidence", {})
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return {}, [f"{FILE} cannot be read: {exc}"]
    if not isinstance(table, dict):
        return {}, [f'{FILE}: `evidence` must be a table of RULE-ID = "command"']
    wanted = set(evidence_rules)
    commands: dict[str, str] = {}
    problems: list[str] = []
    for ident, command in table.items():
        if not isinstance(command, str) or not command.strip():
            problems.append(f"{FILE}: the command for {ident} must be a non-empty string")
        elif ident not in wanted:
            problems.append(
                f"{FILE}: {ident} is not an evidence rule (a MUST rule enforced by a tool with no command), "
                "so it cannot take a declared command: it cannot replace a rule's own check"
            )
        else:
            commands[ident] = command
    return commands, problems
