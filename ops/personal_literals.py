"""What the personal-literal guard looks for: generic shapes here, the owner's identifiers in a private file.

`tests/test_profile.py`'s guard and `ops/scan_public.py` both use `pattern()`. It joins

- `GENERIC`: shapes of deployment-specific values that name nobody in particular, safe to publish: a Union
  tenant host, a Union-managed bucket, an AWS account id in an ARN, an ECR host or after the word "account";
- the owner's list: one Python regex per line (`#` starts a comment) in the file `FACTORY_OWNER_LITERALS`
  names. It is private: kept outside the repository, and on GitHub given to CI as the repository secret of
  the same name (`.github/workflows/checks.yml` writes it to a file). Without it only the generic shapes are
  checked, which is what a fork's pull request and a stranger's checkout get.

    FACTORY_OWNER_LITERALS=~/private/owner-literals.txt python ops/scan_public.py <dir>
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

#: The environment variable naming the owner's private list (not `AGENT_FACTORY_*`: tests/conftest.py clears
#: those).
ENV = "FACTORY_OWNER_LITERALS"

#: Deployment-specific shapes, none of them an owner's value.
GENERIC = (
    r"\b[\w-]+\.hosted\.unionai\.cloud\b",  # a Union tenant's host
    r"\b(?:s3|gs)://union-[a-z0-9-]+",  # a Union-managed bucket
    r"\barn:aws:[\w-]*:[\w-]*:\d{12}:",  # an AWS account id in an ARN
    r"\b\d{12}\.dkr\.ecr\.",  # an AWS account id in an ECR host
    r"\b(?i:account(?:[ _-]?id)?)\W{0,3}\d{12}\b",  # an AWS account id after "account"
)

#: `(path, text)`: a line of `path` containing `text` holds an owner literal without naming an owner: a test
#: named after the generic ANTHROPIC_API_KEY, and an assertion that a retired task name is gone. Each `text`
#: is spelled so that it matches no literal itself. Paths are relative to the scanned root.
LINE_ALLOWLIST = (
    ("tests/test_auth.py", "def test_the_session_never_sees_an_"),
    ("tests/test_deploy_status.py", '"claude-agent.project", "agent-factory.project"'),
)


def read_list(path: Path) -> list[str]:
    """Return the regexes in an owner list file; raise ValueError naming a line that does not compile."""
    patterns = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
        except re.error as exc:
            raise ValueError(f"{path}:{number}: not a regex: {exc}") from exc
        patterns.append(line)
    return patterns


def private_list(environ: Mapping[str, str] = os.environ) -> list[str]:
    """Return the owner's regexes from the file `FACTORY_OWNER_LITERALS` names; none when it is unset."""
    name = environ.get(ENV, "")
    if not name:
        return []
    path = Path(name).expanduser()
    if not path.is_file():
        # Set but missing is a mistake, not "no list": failing keeps the guard from passing on nothing.
        raise FileNotFoundError(f"{ENV}={name} is not a file")
    return read_list(path)


def pattern(extra: list[str] | None = None) -> re.Pattern[str]:
    """Return the guard's regex: the generic shapes and the owner's list (`extra`, else `private_list()`)."""
    owner = private_list() if extra is None else extra
    return re.compile("|".join(f"(?:{p})" for p in (*GENERIC, *owner)))
