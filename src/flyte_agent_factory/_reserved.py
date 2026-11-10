"""The owner-reserved categories and standing rules, in one dependency-free place.

The judge, the session's asking guidance and the operator chat's instructions all classify a question
as owner-reserved or engineering judgement. They render this one list so a category cannot be dropped
from one of them. `_judgement` imports `_reviewer` and more, which the session and operator modules
must not pull in just to read a list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flyte_agent_factory._profile import JudgementPolicy

#: Categories that always go to the owner, whatever a profile adds. A profile cannot remove one.
OWNER_RESERVED_FLOOR: tuple[str, ...] = (
    "spending money, or any paid or API-key usage",
    "credentials, secrets, access grants or sign-ins",
    "irreversible or external actions: deleting data, force-pushing shared branches, acting on "
    "third-party systems",
    "publishing, or anything outward-facing",
    "merging outside the factory's sandbox",
    "product direction or scope the owner has not set",
    "a report of finished work, or a request for an action only a human can take "
    "(retrying a review, relaunching, signing in)",
    "anything the owner has explicitly kept for themself",
)

#: The owner's standing direction, in every judge prompt.
STANDING_RULES: tuple[str, ...] = (
    "No waivers: when a check fails, fix the code or fix the check. Never add a waiver, lower a "
    "threshold, remove a rule or skip a test to make it pass.",
    "The factory never merges outside its sandbox without the owner.",
    "Engineering judgement is decided inside the factory; only owner-reserved questions reach the owner.",
)


def list_owner_reserved(policy: JudgementPolicy | None) -> tuple[str, ...]:
    """Return the floor plus whatever the profile adds; a profile cannot remove a category."""
    return (*OWNER_RESERVED_FLOOR, *(policy.owner_reserved if policy else ()))


def list_standing_rules(policy: JudgementPolicy | None) -> tuple[str, ...]:
    """Return the built-in standing rules plus the profile's standing direction."""
    return (*STANDING_RULES, *(policy.standing_direction if policy else ()))
