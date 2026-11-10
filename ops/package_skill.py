"""Render the chat skill (for a deployment profile, or the setup-only variant) into an installable zip."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

from flyte_agent_factory import _profile

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "skills/agent-factory/SKILL.md"
OUTPUT = ROOT / "skills/dist/agent-factory.zip"
# The source description advertises operating the factory; the setup package cuts that text out.
SETUP_DESCRIPTION = (
    "Guide first installation of the Flyte agent factory, or resume a partial one: GitHub workspace, "
    "Flyte backend, local tools, locally stored credentials, deployment, factory MCP, skill and GitHub "
    "connector. Use when the person asks to set up, install or finish installing the factory."
)


def render_skill(source: str, profile: _profile.Profile) -> str:
    """Return `source` with the profile's project, domain, repositories and tasks filled in."""
    repos = profile.mcp.repos if profile.mcp else ("project",)
    values = {
        "project": profile.backend.project,
        "domain": profile.backend.domain,
        "repo": profile.repo,
        "repos": ", ".join(f"`{profile.repos[key]}`" for key in repos),
        "work_tasks": ", ".join(f"`{task}`" for task in profile.tasks),
        "intake_label": profile.github.intake_label,
    }

    # string.Template uses dollar syntax; keep the skill's documented {{name}} placeholders.
    def replace(match: re.Match[str]) -> str:
        key = match[1]
        if key not in values:
            raise ValueError(f"unknown placeholder: {key}")
        return values[key]

    rendered = re.sub(r"\{\{([^{}]+)\}\}", replace, source)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("unresolved placeholder in rendered skill")
    return rendered


def main(argv: list[str] | None = None) -> int:
    """Write a setup or profile-rendered skill zip and print its path."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--profile", help="deployment profile name or TOML path")
    mode.add_argument("--setup", action="store_true", help="setup instructions without a deployment profile")
    args = parser.parse_args(argv)
    source = SOURCE.read_text()
    if args.setup:
        # Setup must work before a profile exists and must not suggest deployment defaults or launch calls.
        rendered, boundary, _ = source.partition("## Deployment\n")
        if not boundary:
            raise ValueError("missing deployment boundary in skill source")
        # JSON strings are valid YAML double-quoted scalars; a plain scalar can't hold the `: ` in the text.
        rendered, count = re.subn(
            r"^description: .*$",
            lambda _: f"description: {json.dumps(SETUP_DESCRIPTION)}",
            rendered,
            count=1,
            flags=re.M,
        )
        if not count:
            raise ValueError("missing description in skill source")
    else:
        rendered = render_skill(source, _profile.load(args.profile))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("agent-factory/SKILL.md", rendered)
        archive.write(SOURCE.parent / "references/setup.md", "agent-factory/references/setup.md")
    print(f"Wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
