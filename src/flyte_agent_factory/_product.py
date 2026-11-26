"""A project's `product.toml`: who its users are and which brand applies (docs/standards.md).

Parsing never raises: a file that cannot be used is reported as problems, because a session's prompts and the
standards selection run on every flush and must not die over a typo in a file the agent wrote.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Literal

from . import _standards

FILE = "product.toml"
EXPERTISE = ("novice", "intermediate", "expert")
#: `[brand] file` value that says the project has no brand rules at all.
NO_BRAND = "none"
_AUDIENCE_KEYS = ("users", "age_band", "expertise", "reading_grade", "main_task", "under_13")
# Mirrors the audience check script (standards/checks/_ux_common.py), which cannot import the engine; the
# agreement test in tests/test_product.py keeps the two the same.
_AGE_PART = re.compile(r"(?:(\d{1,3})\s*[-\u2013]\s*(\d{1,3})|(\d{1,3})\s*\+|(adults?))", re.IGNORECASE)
_MAX_AGE = 120
_UNDER_13 = 13


@dataclass(frozen=True)
class Audience:
    users: str
    age_band: str
    expertise: str
    reading_grade: int
    main_task: str
    #: Whether children under 13 are among the users, from `age_band` or else `under_13`.
    under_13: bool = False


@dataclass(frozen=True)
class Product:
    #: None when `[audience]` is missing or invalid; `problems` says why.
    audience: Audience | None = None
    #: The `[brand] file` value, or None when it is omitted.
    brand_file: str | None = None
    #: Everything wrong with the file, one sentence each.
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Brand:
    """What the project's `[brand]` table does to the workspace's brand.md."""

    #: "keep" (leave the workspace's), "none" (remove it) or "replace" (use `text`).
    action: Literal["keep", "none", "replace"] = "keep"
    text: str = ""
    problems: tuple[str, ...] = field(default=())


def _read_text_field(table: dict[str, object], key: str, problems: list[str]) -> str:
    value = table.get(key)
    if value is None:
        problems.append(f"[audience] {key} is missing")
        return ""
    if not isinstance(value, str) or not value.strip():
        problems.append(f"[audience] {key} must be a non-empty string")
        return ""
    return value.strip()


def _includes_under_13(age_band: str) -> bool | None:
    """Say whether a readable age band ("6-8", "65+", "adult", or a list of them) includes under-13s."""
    under_13 = False
    for part in re.split(r"\s*(?:,|;|\band\b)\s*", age_band, flags=re.IGNORECASE):
        match = _AGE_PART.fullmatch(part)
        if not match:
            return None
        if match[4]:
            continue
        low = int(match[1] or match[3])
        high = int(match[2]) if match[2] else _MAX_AGE
        if low > high or high > _MAX_AGE:
            return None
        under_13 = under_13 or low < _UNDER_13
    return under_13


def _read_under_13(table: dict[str, object], age_band: str, problems: list[str]) -> bool:
    declared = table.get("under_13")
    if declared is not None and not isinstance(declared, bool):
        problems.append("[audience] under_13 must be true or false")
        return False
    read = _includes_under_13(age_band) if age_band else None
    if read is None:
        if declared is None and age_band:
            problems.append("[audience] under_13 is missing: age_band is not a range like 6-8, 65+ or adult")
        return bool(declared)
    if declared is not None and declared != read:
        problems.append("[audience] under_13 contradicts age_band")
    return read


def _parse_audience(table: object) -> tuple[Audience | None, list[str]]:
    if table is None:
        return None, ["[audience] is missing"]
    if not isinstance(table, dict):
        return None, ["[audience] must be a table"]
    problems: list[str] = []
    users = _read_text_field(table, "users", problems)
    age_band = _read_text_field(table, "age_band", problems)
    main_task = _read_text_field(table, "main_task", problems)
    expertise = _read_text_field(table, "expertise", problems)
    if expertise and expertise not in EXPERTISE:
        problems.append(f"[audience] expertise must be one of {', '.join(EXPERTISE)}")
    grade = table.get("reading_grade")
    if grade is None:
        problems.append("[audience] reading_grade is missing")
    elif not isinstance(grade, int) or isinstance(grade, bool) or not 0 <= grade <= 18:
        problems.append("[audience] reading_grade must be an integer from 0 to 18")
    under_13 = _read_under_13(table, age_band, problems)
    unknown = [f"[audience] unknown key ignored: {k}" for k in sorted(set(table) - set(_AUDIENCE_KEYS))]
    if problems or not isinstance(grade, int):
        return None, problems + unknown
    return Audience(users, age_band, expertise, grade, main_task, under_13), unknown


def parse(text: str) -> Product:
    """Parse the text of a `product.toml`; whatever is wrong is in `problems`, never raised."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Product(problems=(f"{FILE} is not valid TOML: {exc}",))
    audience, problems = _parse_audience(data.get("audience"))
    brand_file = None
    brand = data.get("brand")
    if brand is not None:
        if not isinstance(brand, dict):
            problems.append("[brand] must be a table")
        else:
            problems += [f"[brand] unknown key ignored: {k}" for k in sorted(set(brand) - {"file"})]
            if "file" in brand:
                if isinstance(brand["file"], str) and brand["file"].strip():
                    brand_file = brand["file"].strip()
                else:
                    problems.append("[brand] file must be a non-empty string")
    return Product(audience, brand_file, tuple(problems))


def load(project: Path) -> Product | None:
    """Return the project's `product.toml`, or None when it has none."""
    path = project / FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return Product(problems=(f"{FILE} cannot be read: {type(exc).__name__}",))
    return parse(text)


def _keep_judgement_only(text: str, dropped: list[str], clashing: list[str], taken: frozenset[str]) -> str:
    """`text` with only its judgement rules whose IDs no workspace rule has; the rest named in the lists."""

    def keep(match: re.Match[str]) -> str:
        if _standards._parse_enforced(match[3])[0] != "judgement":
            dropped.append(match[1])
            return ""
        if match[1] in taken:
            clashing.append(match[1])
            return ""
        return match[0]

    return _standards.RULE.sub(keep, text)


def resolve_brand(project: Path, product: Product, taken: Iterable[str] = ()) -> Brand:
    """Return what `[brand] file` does to the selection: keep the workspace's brand, drop it or replace it.

    A path that leaves the project, or is no file, is reported and ignored. Only judgement rules are taken
    from a project's brand file: the project must not grant itself a mechanical check or a script, which
    would run with the gate's authority.
    """
    value = product.brand_file
    if value is None:
        return Brand()
    if value == NO_BRAND:
        return Brand("none")
    keep = f"{FILE} [brand] file {value!r}"
    relative = PurePath(value)
    if relative.is_absolute() or ".." in relative.parts:
        return Brand(problems=(f"{keep} must be a path inside the project; the workspace brand is kept",))
    root = project.resolve()
    path = (root / value).resolve()  # resolves symlinks, so a link out of the project is caught below
    if not path.is_relative_to(root) or not path.is_file():
        return Brand(problems=(f"{keep} is not a file inside the project; the workspace brand is kept",))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return Brand(problems=(f"{keep} cannot be read ({type(exc).__name__}); the workspace brand is kept",))
    dropped: list[str] = []
    clashing: list[str] = []
    text = _keep_judgement_only(text, dropped, clashing, frozenset(taken))
    notes = (
        *(f"{value}: rule {i} dropped: a project brand file holds judgement rules only" for i in dropped),
        *(f"{value}: rule {i} dropped: a workspace standard already has that ID" for i in clashing),
    )
    return Brand("replace", text, notes)


def find_problems(project: Path, taken: Iterable[str] = ()) -> list[str]:
    """Every problem with the project's `product.toml` and the brand it names, for the people who fix it.

    `taken` is the rule IDs of the other selected standards, which a project brand rule may not reuse.
    """
    product = load(project)
    if product is None:
        return []
    return [*product.problems, *resolve_brand(project, product, taken).problems]


def format_audience_block(project: Path) -> str:
    """Return the `## This product's users` section of a prompt; empty without a `product.toml`."""
    product = load(project)
    if product is None:
        return ""
    notes = [*product.problems, *resolve_brand(project, product).problems]
    a = product.audience
    if a is None:
        return (
            f"## This product's users\n\n`{FILE}` is present but its audience is not usable, so who the "
            "users are is unknown: " + "; ".join(notes) + ". Say so rather than guess.\n\n"
        )
    return (
        "## This product's users\n\n"
        f"- Users: {a.users}\n"
        f"- Age band: {a.age_band}\n"
        f"- Includes children under 13: {'yes' if a.under_13 else 'no'}\n"
        f"- Expertise: {a.expertise}\n"
        f"- Reading grade: text must be readable at US school grade {a.reading_grade} or below\n"
        f"- Main task of a first-time user: {a.main_task}\n\n"
        f"From `{FILE}`. This overrides any audience named in brand.md.\n\n"
        + "".join(f"Note: {n}\n" for n in notes)
        + ("\n" if notes else "")
    )
