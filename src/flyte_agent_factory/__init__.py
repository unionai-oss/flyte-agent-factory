"""Long-lived coding agents (Claude Code or Codex) that run as Flyte tasks on Union.

One Flyte run per project, driveable two ways at once: from a chat agent over
Flyte conditions, and with a browser that runs beside it as a Flyte app.

    from flyte_agent_factory import project          # the long-lived agent task
    from flyte_agent_factory import browser_env      # the browser app to `flyte serve`

Attributes resolve lazily. Importing the package eagerly would pull in `flyte`
and build a `TaskEnvironment`, which matters because `ask-human` -- the command
the running session calls to ask a question -- lives in `flyte_agent_factory._bridge`
and should cost nothing but a file write.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

_LAZY = {
    "CLAUDE_VERSION": ("._images", "CLAUDE_VERSION"),
    "PLAYWRIGHT_MCP_VERSION": ("._images", "PLAYWRIGHT_MCP_VERSION"),
    "agent_image": ("._images", "agent_image"),
    "browser_image": ("._images", "browser_image"),
    "agent_env": (".agent", "env"),
    "project": (".agent", "project"),
    "browser_env": (".browser", "browser_env"),
}

__all__ = [
    "CLAUDE_VERSION",
    "PLAYWRIGHT_MCP_VERSION",
    "agent_env",
    "agent_image",
    "browser_env",
    "browser_image",
    "project",
]

assert sorted(__all__) == sorted(_LAZY), "__all__ and the lazy attribute table disagree"


def __getattr__(name: str) -> Any:
    try:
        module, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(importlib.import_module(module, __name__), attr)


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # pragma: no cover - import-time cost is the point
    from ._images import (
        CLAUDE_VERSION,
        PLAYWRIGHT_MCP_VERSION,
        agent_image,
        browser_image,
    )
    from .agent import env as agent_env
    from .agent import project
    from .browser import browser_env
