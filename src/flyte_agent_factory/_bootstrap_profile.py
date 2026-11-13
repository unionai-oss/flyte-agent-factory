"""Writing the workspace's `profile.toml` for the bootstrap: created once, then only ever filled in.

The person edits this file after the first run (docs/quickstart.md), so a rerun must never rewrite it: it
reads what is there with `tomllib` and adds only the keys that are missing, as text, leaving every other byte.
"""

from __future__ import annotations

import dataclasses
import json
import re
import tomllib
from pathlib import Path

from ._bootstrap_base import (
    PROFILE_FILE,
    SECRET_APP_KEY,
    SECRET_CLAUDE,
    SECRET_DEPLOY_KEY,
    BootstrapError,
    Settings,
    normalise_endpoint,
)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
ENGINE_REPO_URL = "https://github.com/unionai-oss/flyte-agent-factory"
#: Must start with `codex_login`: the chat-started login task refuses any other name (agent.py).
SECRET_CODEX_LOGIN = "codex_login_agent_factory"
_EXAMPLES = {"claude": "example-claude-personal.toml", "codex": "example-codex-personal.toml"}
#: Where the Flyte config lives, written into `[backend] config`: the path `FlyteSdk` uses by default.
FLYTE_CONFIG = "~/.flyte/config.yaml"


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)  # a JSON string is a valid TOML basic string for the values written here


def _render_table(name: str, values: dict[str, object]) -> str:
    return f"[{name}]\n" + "".join(f"{key} = {_render_value(value)}\n" for key, value in values.items())


def _render_slots(settings: Settings) -> str:
    """Return the slots of the chosen engine's example profile, with the codex login renamed."""
    example = tomllib.loads((PROFILES_DIR / _EXAMPLES[settings.engine]).read_text())
    out = []
    for slot in example["slots"]:
        slot = dict(slot)
        policy = slot.pop("policy", {})
        if "codex_login" in slot:
            slot["codex_login"] = SECRET_CODEX_LOGIN
        out.append("[[slots]]\n" + "".join(f"{k} = {_render_value(v)}\n" for k, v in slot.items()))
        if policy:
            out.append(_render_table("slots.policy", policy))
    return "\n".join(out)


def collect_wanted_keys(
    settings: Settings, app_id: str | None, mcp_url: str = ""
) -> dict[str, dict[str, object]]:
    """Every table and key the bootstrap manages, by table; the App's keys only when there is an App.

    `[repo] factory` is there either way: a deploy takes the engine from it, App or not.
    """
    # `[direction] repo` is read like `[repo]` entries: a clone URL, not owner/repo.
    workspace_url = f"git@github.com:{settings.workspace}.git"
    keys: dict[str, dict[str, object]] = {
        "backend": {
            "kind": settings.backend,
            "endpoint": settings.endpoint,
            "project": settings.project,
            "domain": settings.domain,
            "config": FLYTE_CONFIG,
        },
        "repo": {"project": workspace_url, "factory": ENGINE_REPO_URL},
        "secrets": {"claude_token": SECRET_CLAUDE, "deploy_key": SECRET_DEPLOY_KEY},
        "state": {"uri": settings.state_uri},
        "mcp": {"public_url": mcp_url},
    }
    if app_id:
        keys["github"] = {"app_id": app_id, "private_key_secret": SECRET_APP_KEY}
        keys["deploy"] = {"auto": True}
        keys["direction"] = {"repo": workspace_url, "profile": PROFILE_FILE}
    return keys


def render(settings: Settings, app_id: str | None) -> str:
    keys = collect_wanted_keys(settings, app_id)
    parts = [
        "# Written by ops/bootstrap.py; yours to edit. Every key is explained in the engine's "
        "profiles/README.md.\n"
        "# Keep this file out of any public repository: it holds your project, host and secret names.\n"
        f'name = "{settings.repo_name}"\n'
    ]
    for table in ("backend", "repo"):
        parts.append(_render_table(table, keys[table]))
    parts.append(_render_slots(settings))
    for table in ("secrets", "state", "mcp", "github", "deploy", "direction"):
        if table in keys:
            parts.append(_render_table(table, keys[table]))
    return "\n".join(parts)


def write(path: Path, settings: Settings, app_id: str | None) -> str:
    """Create `path` (never overwriting) or, when it exists, fill in only the keys it lacks.

    Return what happened.
    """
    try:
        with path.open("x") as handle:
            handle.write(render(settings, app_id))
    except FileExistsError:
        added = fill_missing(path, settings, app_id)
        return f"added {', '.join(added)}" if added else "already complete"
    return "created"


def _add_keys(text: str, path: Path, table: str, have: object, values: dict[str, object]) -> str:
    """Return `text` with `values` added to `[table]`: after its header if it has one, else as a new table."""
    lines = "".join(f"{k} = {_render_value(v)}\n" for k, v in values.items())
    if not isinstance(have, dict):
        return text.rstrip("\n") + f"\n\n[{table}]\n" + lines
    header = re.search(rf"^\[{re.escape(table)}\][ \t]*(#.*)?$", text, re.MULTILINE)
    if not header:
        raise BootstrapError(f"[{table}] in {path} is written in a form this tool will not edit")
    end = text.find("\n", header.end())
    # A header that ends the file gets the newline it lacks, so `at` counts it.
    at = len(text) + 1 if end == -1 else end + 1
    return (text if end != -1 else text + "\n")[:at] + lines + text[at:]


def _fill_missing_text(path: Path, settings: Settings, app_id: str | None) -> tuple[str, list[str]]:
    """`path`'s text with the managed keys it lacks added, and their `table.key` names."""
    text = path.read_text()
    try:
        present = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BootstrapError(f"{path} is not valid TOML ({exc}); fix it and run this again") from exc
    added: list[str] = []
    # `auto = false` is the person's choice: `[direction]` only makes sense with auto deploys, so neither the
    # flag nor the table is put back.
    auto_off = isinstance(present.get("deploy"), dict) and present["deploy"].get("auto") is False
    for table, values in collect_wanted_keys(settings, app_id).items():
        if auto_off and table == "direction":
            continue
        have = present.get(table)
        missing = {k: v for k, v in values.items() if not isinstance(have, dict) or k not in have}
        if missing:
            text = _add_keys(text, path, table, have, missing)
            added.extend(f"{table}.{k}" for k in missing)
    return text, added


def find_conflicts(path: Path, settings: Settings) -> dict[str, tuple[str, str]]:
    """Return `table.key` -> (the file's value, the command line's) for each setting that disagrees.

    The file is the person's after the first run, so a different value on the command line is a mistake or
    a second project, never an edit to make silently.
    """
    data = tomllib.loads(path.read_text())
    backend, state = data.get("backend", {}), data.get("state", {})
    wanted = {
        "backend.kind": (backend.get("kind"), settings.backend),
        "backend.endpoint": (backend.get("endpoint"), settings.endpoint),
        "backend.project": (backend.get("project"), settings.project),
        "backend.domain": (backend.get("domain"), settings.domain),
        "state.uri": (state.get("uri"), settings.state_uri),
    }
    conflicts = {}
    for name, (have, given) in wanted.items():
        if have is None:  # a missing key is `find_missing_keys`'s to fill
            continue
        if name == "backend.endpoint":
            have = normalise_endpoint(str(have))
        if have != given:
            conflicts[name] = (str(have), given)
    return conflicts


def read_settings(path: Path, settings: Settings) -> Settings:
    """Return a copy of `settings` with the values `path` holds, for the command that matches the file."""
    data = tomllib.loads(path.read_text())
    backend, state = data.get("backend", {}), data.get("state", {})
    return dataclasses.replace(
        settings,
        backend=backend.get("kind", settings.backend),
        endpoint=normalise_endpoint(str(backend.get("endpoint", settings.endpoint))) or settings.endpoint,
        project=backend.get("project", settings.project),
        domain=backend.get("domain", settings.domain),
        state_uri=state.get("uri", settings.state_uri),
    )


def find_missing_keys(path: Path, settings: Settings, app_id: str | None) -> list[str]:
    """Return the managed `table.key` names `path` lacks, without touching it."""
    return _fill_missing_text(path, settings, app_id)[1]


def fill_missing(path: Path, settings: Settings, app_id: str | None) -> list[str]:
    """Add the managed keys `path` lacks (a key the person set, to anything, stays); return their names."""
    text, added = _fill_missing_text(path, settings, app_id)
    if added:
        path.write_text(text)
    return added


#: What only an installed App justifies in the profile: the keys `collect_wanted_keys()` adds for one.
#: `[deploy] auto` and `[direction]` make the deployer act as the App, and `[github] app_id` names it.
_APP_KEYS: dict[str, tuple[str, ...] | None] = {
    "github": ("app_id", "private_key_secret"),
    "deploy": ("auto",),
    "direction": None,  # the whole table
}


def find_app_keys(path: Path) -> list[str]:
    """Return the `table.key` names of `_APP_KEYS` that `path` has."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found: list[str] = []
    for table, keys in _APP_KEYS.items():
        have = data.get(table)
        if isinstance(have, dict):
            found.extend(f"{table}.{k}" for k in (have if keys is None else keys) if k in have)
    return found


def _drop(text: str, table: str, keys: tuple[str, ...] | None) -> str:
    """Return `text` without `keys` of `[table]` (every key when None).

    The header goes too when nothing else is left in the table. Comments and other keys stay; values
    written by this tool are one line.
    """
    lines = text.splitlines(keepends=True)
    header_line = re.compile(rf"\[{re.escape(table)}\][ \t]*(#.*)?")
    header = next((i for i, line in enumerate(lines) if header_line.fullmatch(line.rstrip("\n"))), None)
    if header is None:
        return text
    end = next((i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))

    def is_dropped(line: str) -> bool:
        assignment = re.match(r"\s*([A-Za-z0-9_-]+)\s*=", line)
        return assignment is not None and (keys is None or assignment[1] in keys)

    body = [line for line in lines[header + 1 : end] if not is_dropped(line)]
    left = any(line.strip() and not line.lstrip().startswith("#") for line in body)
    return "".join(lines[:header] + (lines[header : header + 1] + body if left else []) + lines[end:])


def drop_app_keys(path: Path) -> list[str]:
    """Remove the App's keys from `path` (the person chose `--no-app`); return the names removed."""
    removed = find_app_keys(path)
    if removed:
        text = path.read_text()
        for table, keys in _APP_KEYS.items():
            text = _drop(text, table, keys)
        path.write_text(text)
    return removed


def read_app_id(path: Path) -> str | None:
    """Return the `[github] app_id` already in `path`, if any."""
    try:
        return str(tomllib.loads(path.read_text()).get("github", {}).get("app_id", "")) or None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def set_mcp_url(path: Path, url: str) -> None:
    """Set `[mcp] public_url` when it is empty or absent; a URL already there is the person's and stays."""
    text = path.read_text()
    have = tomllib.loads(text).get("mcp")
    if isinstance(have, dict) and have.get("public_url"):
        return
    empty = re.compile(r"^(public_url\s*=\s*)(\"\"|'')", re.MULTILINE)
    if isinstance(have, dict) and "public_url" in have and empty.search(text):
        path.write_text(empty.sub(lambda m: f"{m[1]}{_render_value(url)}", text, count=1))
    else:
        path.write_text(_add_keys(text, path, "mcp", have, {"public_url": url}))


def is_mcp_url_set(path: Path) -> bool:
    try:
        return bool(tomllib.loads(path.read_text()).get("mcp", {}).get("public_url"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
