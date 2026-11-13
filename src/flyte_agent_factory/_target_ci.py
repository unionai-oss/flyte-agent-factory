"""What a project that delivers to another repo must pass: that repo's own CI, found in its checkout.

The factory's checks run the factory's ruff and mypy configs, so a patch can pass review and still fail
the target's `make mypy`. This module reads the brief's `Delivers to:` line and finds, in a checkout of the
target (`_delivery.target_checkout`, or a legacy nested one), the commands the target's CI runs. `_gates`
runs them. Parsing only: nothing here runs a command.
"""

import dataclasses
import itertools
import re
import shlex
from pathlib import Path
from typing import Any

import yaml

#: `Delivers to: <git url or owner/repo>`, optionally ` at <path of the checkout inside the project>`.
_DELIVERS = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?Delivers to:[ \t]*(\S+)(?:[ \t]+at[ \t]+(\S+))?[ \t]*$", re.I | re.M
)
#: A step or command is a check when it matches one of these.
_CHECK = re.compile(
    r"\b(ruff|black|flake8|mypy|pyright|ty|pylint|codespell|check-docstrings|pre-commit|pytest|lint|format|"
    r"fmt|typecheck|type-check|docstrings?|spell|lock|tests?)\b|uv lock --(?:check|locked)|\bmake\s+\S",
    re.I,
)
#: A step named like this prepares the machine, even when the name also says "test" (`Install test deps`).
_SETUP_NAME = re.compile(r"^(?:install|set ?up|prepare|bootstrap|configure)\b", re.I)
#: A command that installs or sets up. `echo` and `export` are not here: they can precede a real check.
_SETUP = re.compile(
    r"^(?:sudo |apt(?:-get)? |pip3? |python3? -m pip |uv pip |uv sync|uv tool install |"
    r"npm (?:ci|install|i)\b|yarn (?:install|add)\b|yarn$|pnpm (?:install|i|add)\b)"
)
#: What GitHub puts in the contexts of a pull request run that do not depend on the workflow.
_STAND_INS = {
    "github.event_name": "pull_request",
    "github.base_ref": "main",
    "github.head_ref": "feature",
    "github.ref": "refs/pull/1/merge",
    "github.sha": "0" * 40,
    "github.token": "",
    "runner.os": "Linux",
}
#: The contexts that are paths of the job running the step, and the variable holding each. Discovery leaves
#: the expression as written, so a report shows it as the workflow does; `job_paths` puts the job's own path
#: in its place when the step runs.
JOB_PATHS = {"runner.temp": "RUNNER_TEMP", "github.workspace": "GITHUB_WORKSPACE"}
_STAND_INS.update({context: f"${{{{ {context} }}}}" for context in JOB_PATHS})
#: The actions that install a Rust toolchain: group 1 the action, group 2 its ref.
_RUST_ACTION = re.compile(
    r"^(dtolnay/rust-toolchain|actions-rs/toolchain|actions-rust-lang/setup-rust-toolchain)(?:@(.*))?$"
)
#: The actions that put a uv or a Python on the job's PATH, and a local action (which may do either).
_TOOL_ACTION = re.compile(r"^(astral-sh/setup-uv|actions/setup-python)(?:@.*)?$|^\./")
#: The actions that pass files between the jobs of a run, which the gate does itself (`_target_run`).
_ARTIFACT_ACTION = re.compile(r"^actions/(upload-artifact|download-artifact)(?:@.*)?$")
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][\w.-]*)|(?P<op>==|!=|&&|\|\||[!(),]))"
)
#: What each runnable `shell:` runs the script with. GitHub's `sh` has no pipefail.
SHELLS = {
    "bash": ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"],
    "sh": ["sh", "-e", "-c"],
    "python": ["python", "-c"],
}
_MAKE_TARGET = re.compile(r"^([A-Za-z][\w.-]*)[ \t]*:(?!=)")
_MAKE_CHECK = re.compile(
    r"fmt|format|lint|check.*|mypy|typecheck|type-check|ty|docstring.*|codespell|spell.*|lock.*|tests?", re.I
)
_MAKE_COVERAGE = re.compile(r"cov|coverage|test-cov|testcov", re.I)
_COVERAGE_STEP = re.compile(r"--cov\b|coverage run")
#: Only these workflow triggers make a job part of the target's gate for a pull request.
_TRIGGERS = {"pull_request", "push"}
#: Run when the target has no coverage command of its own; the data file is read with `coverage json`.
FALLBACK_COVERAGE = "python -m pytest --cov --cov-branch"
#: How the gate reads a data file, as `python -m` arguments. `--ignore-errors` skips a measured file with no
#: source (code a test compiled under a made-up name); a changed file left out of the report still counts
#: as uncovered.
COVERAGE_JSON = ("coverage", "json", "--ignore-errors")
#: The fallback for a uv project (a `uv.lock`): its own environment, with pytest-cov installed into it since
#: the project need not declare it (named by its interpreter: `uv pip` would follow `UV_PYTHON` or an active
#: venv elsewhere). Not `uv run --with`: that overlay splits the environment in two, which
#: breaks tests that build a venv from its site-packages (the engine's do).
FALLBACK_COVERAGE_UV = (
    "uv sync && uv pip install --python \"$(uv run --no-sync python -c 'import sys; print(sys.executable)')\""
    " pytest-cov && uv run --no-sync python -m pytest --cov --cov-branch"
)


@dataclasses.dataclass(frozen=True)
class Target:
    """What `Delivers to:` names: the repo, and where its checkout is in the project (None: find it)."""

    repo: str
    path: str | None


@dataclasses.dataclass(frozen=True)
class Command:
    """A shell script from the target's CI, and where it came from (`file:line`, comma-separated).

    `env`, `cwd` and `shell` (a key of `SHELLS`) are what the workflow gives the step. `unrunnable` is why
    the gate cannot run it (an expression only GitHub evaluates, a shell it does not have, a workflow file
    that cannot be read): the gate fails it. `skipped` is why it cannot fail CI (`continue-on-error`): the
    gate does not run it. `job` is the workflow job the step is in: its steps share `$GITHUB_ENV`.
    `check` is False for a step that is not a check itself but belongs to a job that has one: it runs
    because a later check may depend on it (a formatter before `git diff --exit-code`). `rust` is the
    toolchain an earlier `uses:` step of the job sets up, as (toolchain, *components); empty if none.
    `toolchain_steps` are the earlier `rustup ...` scripts of the job (setup as far as discovery goes, but
    on a runner they are what installs the toolchain the check uses). `uv` is the version an earlier
    `astral-sh/setup-uv` step (or a local action) installs: "latest" with no version, "" if none; `python`
    is the `actions/setup-python` version.

    `timeout` is the step's `timeout-minutes`, else its job's, in seconds; None leaves the gate's default.
    `needs` are the jobs (of the same workflow) the job waits for; `job_runs_after_failure` is whether its
    `if:` runs it when one of them failed (`always()`), and `runs_after_failure` the same for the step after
    an earlier step of its job failed. `action` is `upload-artifact` or `download-artifact` for a step that
    uses that action, which the gate does itself, with `inputs` its `with:` (expressions expanded).
    """

    command: str
    origin: str
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    unrunnable: str = ""
    shell: str = "bash"
    skipped: str = ""
    job: str = ""
    check: bool = True
    rust: tuple[str, ...] = ()
    toolchain_steps: tuple[str, ...] = ()
    uv: str = ""
    python: str = ""
    timeout: int | None = None
    needs: tuple[str, ...] = ()
    runs_after_failure: bool = False
    job_runs_after_failure: bool = False
    action: str = ""
    inputs: tuple[tuple[str, str], ...] = ()


def parse_target(brief: str) -> Target | None:
    """Return the repo the brief says it delivers to, or None for an ordinary project."""
    found = _DELIVERS.search(brief)
    return Target(found[1], found[2]) if found else None


def find_checkout(project: Path, target: Target) -> Path | None:
    """Return the target's nested git checkout inside `project`: the named path, else the only one.

    The layout from before `promote.toml`: a project that imported the target's files has no nested
    checkout, and `_delivery.target_checkout` makes one for the gates. Such a checkout still passes the gates
    but cannot be delivered.
    """
    if target.path:
        path = (project / target.path).resolve()
        # The brief is a task input: a path out of the project would run commands anywhere.
        return path if path.is_relative_to(project.resolve()) and (path / ".git").exists() else None
    nested = [p for p in sorted(project.iterdir()) if (p / ".git").exists()] if project.is_dir() else []
    return nested[0] if len(nested) == 1 else None


def _step_lines(text: str) -> dict[tuple[str, int], int]:
    """Return the 1-based line of each step's `run:` (else of the step), by (job id, index in its steps).

    `safe_load` keeps no positions, so the workflow is composed again for its nodes' marks. Searching the
    text for the script instead finds the first step with the same text, in whichever job.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    lines: dict[tuple[str, int], int] = {}
    for jobs in _children(root, "jobs"):
        if not isinstance(jobs, yaml.MappingNode):
            continue
        for job_id, job in jobs.value:
            for steps in _children(job, "steps"):
                for index, step in enumerate(steps.value if isinstance(steps, yaml.SequenceNode) else []):
                    run = next(iter(_children(step, "run")), step)
                    lines[(str(job_id.value), index)] = run.start_mark.line + 1
    return lines


def _children(node: yaml.Node | None, key: str) -> list[yaml.Node]:
    """Return the values under `key` of a mapping node; none if it is not a mapping."""
    if not isinstance(node, yaml.MappingNode):
        return []
    return [value for name, value in node.value if name.value == key]


def _mapping(node: Any, what: str) -> dict[str, Any]:
    """Return `node` if it is a mapping, `{}` if absent; a workflow of any other shape is unreadable."""
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise ValueError(f"{what} is not a mapping")
    return node


def _env_of(node: dict[str, Any], what: str) -> dict[str, str]:
    """Return the `env:` of a workflow, job or step, values as GitHub would pass them to the shell."""
    env = _mapping(node.get("env"), what)
    return {str(k): _env_value(v) for k, v in env.items()}


def _env_value(value: Any) -> str:
    """Return `value` as the runner exports it: YAML null is empty, booleans are lower case."""
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _run_default(node: dict[str, Any], key: str, what: str) -> str | None:
    run = _mapping(_mapping(node.get("defaults"), f"{what} defaults").get("run"), f"{what} defaults.run")
    value = run.get(key)
    return str(value) if value else None


def _is_setup(name: str, script: str) -> bool:
    """Whether a step only prepares the machine: its name says so, or every row of its script installs.

    A step whose name selects it as a check is not setup because of its rows.
    """
    if _SETUP_NAME.match(name):
        return True
    if _CHECK.search(name):
        return False
    rows = [row.strip() for row in script.replace("\\\n", " ").splitlines()]
    rows = [row for row in rows if row and not row.startswith("#")]
    return bool(rows) and all(_SETUP.match(row) for row in rows)


def repo_name(repo: str) -> str:
    """Return `owner/name` for what `Delivers to:` names (a URL or `owner/name`), as `github.repository`."""
    return "/".join(repo.removesuffix(".git").rstrip("/").split("/")[-2:])


class _Expression:
    """Evaluate a GitHub Actions expression against a scope of known values.

    A value is a str or a bool; None is "cannot be decided offline", with the first cause in `why`.
    Only what a pull request check needs is implemented (see `_condition` for the list): anything else,
    including a syntax error, is undecidable rather than guessed. Written here because no maintained
    Python package evaluates workflow expressions; the grammar is small. `failed` is what the status
    functions see: whether an earlier step of the job (or a job it needs) failed.
    """

    def __init__(self, text: str, scope: dict[str, str], *, failed: bool = False) -> None:
        self.scope = scope
        self.failed = failed
        self.why = ""
        self.tokens: list[tuple[str, str]] = []
        position = 0
        text = text.rstrip()
        while position < len(text):
            found = _TOKEN.match(text, position)
            if not found:
                raise ValueError(f"cannot parse `{text[position:]}`")
            self.tokens.append((found.lastgroup or "", found[found.lastgroup or ""]))
            position = found.end()
        self.position = 0

    def value(self) -> str | bool | None:
        result = self._or()
        if self.position != len(self.tokens):
            raise ValueError("unexpected " + self.tokens[self.position][1])
        return result

    def _peek(self) -> str:
        return self.tokens[self.position][1] if self.position < len(self.tokens) else ""

    def _take(self) -> tuple[str, str]:
        if self.position >= len(self.tokens):
            raise ValueError("expression ends early")
        self.position += 1
        return self.tokens[self.position - 1]

    def _unknown(self, why: str) -> None:
        self.why = self.why or why

    def _or(self) -> str | bool | None:
        result = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            if _known_true(result):
                continue
            result = (right if _known_true(right) else None) if result is None else right
        return result

    def _and(self) -> str | bool | None:
        result = self._equality()
        while self._peek() == "&&":
            self._take()
            right = self._equality()
            if result is None:
                result = right if _known_false(right) else None
            elif not _known_false(result):
                result = right
        return result

    def _equality(self) -> str | bool | None:
        result = self._unary()
        while self._peek() in ("==", "!="):
            negate = self._take()[1] == "!="
            right = self._unary()
            if result is None or right is None:
                result = None
            else:
                result = _same(result, right) != negate
        return result

    def _unary(self) -> str | bool | None:
        if self._peek() == "!":
            self._take()
            operand = self._unary()
            return None if operand is None else not _truthy(operand)
        return self._atom()

    def _atom(self) -> str | bool | None:
        kind, text = self._take()
        if text == "(" and kind == "op":
            result = self._or()
            if self._take()[1] != ")":
                raise ValueError("expected )")
            return result
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return text
        if kind != "name":
            raise ValueError(f"unexpected {text}")
        name = text.lower()
        if self._peek() == "(":
            self._take()
            args = []
            while self._peek() != ")":
                args.append(self._or())
                if self._peek() == ",":
                    self._take()
            self._take()
            return self._call(name, args)
        if name in ("true", "false"):
            return name == "true"
        if name == "null":
            return ""
        if name in self.scope:
            return self.scope[name]
        if name.startswith(("secrets.", "vars.")):
            return ""
        self._unknown(f"`{text}` is only known at run time")
        return None

    def _call(self, name: str, args: list[str | bool | None]) -> str | bool | None:
        if name in ("success", "failure"):
            return self.failed == (name == "failure")
        if name in ("always", "cancelled"):
            return name == "always"
        if name in ("startswith", "endswith", "contains") and len(args) == 2:
            if args[0] is None or args[1] is None:
                return None
            haystack, needle = (str(a).lower() for a in args)
            return {
                "startswith": haystack.startswith,
                "endswith": haystack.endswith,
                "contains": haystack.__contains__,
            }[name](needle)
        self._unknown(f"{name}() is only known at run time")
        return None


def _truthy(value: str | bool) -> bool:
    return value if isinstance(value, bool) else value != ""


def _known_true(value: str | bool | None) -> bool:
    return value is not None and _truthy(value)


def _known_false(value: str | bool | None) -> bool:
    return value is not None and not _truthy(value)


def _same(left: str | bool, right: str | bool) -> bool:
    """Whether GitHub's `==` holds: case-insensitive for strings, numeric when both read as numbers."""
    a, b = (str(v).lower() for v in (left, right))
    try:
        return float(a) == float(b)
    except ValueError:
        return a == b


def _evaluate(text: str, scope: dict[str, str], *, failed: bool = False) -> tuple[str | bool | None, str]:
    """Return the value of the expression `text` and, when it is None, why it cannot be decided."""
    try:
        parsed = _Expression(text, scope, failed=failed)
        return parsed.value(), parsed.why or "it is not an expression this check reads"
    except ValueError as exc:
        return None, str(exc)


def _expand(text: str, scope: dict[str, str]) -> str | None:
    """Return `text` with each `${{ ... }}` replaced by its value, or None if one is undecidable offline."""
    unresolved = False

    def substitute(found: re.Match[str]) -> str:
        nonlocal unresolved
        value, _ = _evaluate(found[1], scope)
        if value is None:
            unresolved = True
            return ""
        return str(value).lower() if isinstance(value, bool) else value

    expanded = _EXPRESSION.sub(substitute, text)
    return None if unresolved else expanded


def _condition(node: dict[str, Any], scope: dict[str, str]) -> str:
    """Return why a step or job does not run for a pull request, "" if it does.

    Understood: literals, `==`, `!=`, `!`, `&&`, `||`, parentheses, `startsWith`, `endsWith`, `contains`,
    `success()`, `always()` (true) and `failure()`, `cancelled()` (false). Anything that needs the run
    (`hashFiles`, `steps.*`, `needs.*`, `inputs.*`, `fromJSON`) cannot be decided: the step is listed as
    skipped, as `continue-on-error` is, rather than failed.
    """
    if "if" not in node:
        return ""
    value, why = _evaluate(_if_text(node), scope)
    if value is None:
        return f"its if cannot be decided offline: {why}"
    return "" if _truthy(value) else "its if is false for a pull request"


def _if_text(node: dict[str, Any]) -> str:
    """Return the expression of a step's or job's `if:`, without the optional `${{ }}` around it."""
    text = str(node["if"]).strip()
    if text.startswith("${{") and text.endswith("}}") and text.count("${{") == 1:
        text = text[3:-2]
    return text


#: A status function in an `if:`; without one GitHub adds `success() &&`.
_STATUS_CALL = re.compile(r"\b(?:success|failure|always|cancelled)\s*\(", re.I)


def _runs_after_failure(node: dict[str, Any], scope: dict[str, str]) -> bool:
    """Whether a step (or job) still runs once an earlier step of its job (or a job it needs) failed."""
    if "if" not in node or not _STATUS_CALL.search(text := _if_text(node)):
        return False
    value, _ = _evaluate(text, scope, failed=True)
    return value is not None and _truthy(value)


def _timeout_of(node: dict[str, Any]) -> int | None:
    """Return a step's or job's `timeout-minutes` in seconds; None when absent or only known at run time."""
    value = node.get("timeout-minutes")
    if isinstance(value, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*", value):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(value * 60)


def _continues_on_error(node: dict[str, Any]) -> bool:
    return str(node.get("continue-on-error")).strip().lower() == "true"


#: The most matrix combinations considered per job; past it the rest are not looked at.
_MAX_COMBINATIONS = 256


def _matrix_of(job: dict[str, Any], job_id: str) -> tuple[list[dict[str, str]], bool]:
    """Return the job's `strategy.matrix` combinations, in GitHub's order, as `matrix.<key>` scope entries.

    The product of the list-valued keys, without `exclude` matches, then `include` entries (which extend
    a matching combination or add one). The flag is whether part of the matrix is an expression: GitHub
    builds it when the job starts. A job without a matrix has one empty combination.
    """
    matrix = _mapping(job.get("strategy"), f"strategy of job {job_id}").get("matrix")
    if not isinstance(matrix, dict):
        return [{}], isinstance(matrix, str)  # absent, or itself an expression: no combination is known
    axes = {
        str(key): value if isinstance(value, list) and value else [value]
        for key, value in matrix.items()
        if key not in ("include", "exclude")
    }
    excludes = [e for e in matrix.get("exclude") or [] if isinstance(e, dict)]
    combinations = [
        dict(zip(axes, values, strict=True))
        for values in itertools.islice(itertools.product(*axes.values()), _MAX_COMBINATIONS)
    ]
    combinations = [
        c for c in combinations if not any(all(c.get(str(k)) == v for k, v in e.items()) for e in excludes)
    ] or ([{}] if not axes else [])
    for entry in matrix.get("include") or []:
        if not isinstance(entry, dict):
            continue
        extra = {str(k): v for k, v in entry.items()}
        # GitHub: an entry extends each combination its matrix keys match, else it is a new combination.
        matching = [c for c in combinations if all(c.get(k) == v for k, v in extra.items() if k in axes)]
        for combination in matching:
            for key, value in extra.items():
                combination.setdefault(key, value)
        if not matching:
            combinations.append(extra)
    dynamic = False
    scopes: list[dict[str, str]] = []
    for combination in combinations or [{}]:
        known = {
            f"matrix.{key.lower()}": _env_value(value)
            for key, value in combination.items()
            if isinstance(value, (str, int, float, bool)) and "${{" not in str(value)
        }
        dynamic = dynamic or len(known) < len(combination)
        scopes.append(known)
    return scopes, dynamic


def _steps_of(
    document: dict[str, Any],
    lines: dict[tuple[str, int], int],
    where: str,
    repo: str,
    checkout: Path | None,
    *,
    coverage: bool,
) -> list[Command]:
    """Return the steps of the workflow `document` that are checks, or (`coverage`) measure coverage.

    A job with a check runs every step of it but the pure installs, in order: what the check reads may be
    what an earlier step wrote. Coverage wants the one measuring step, so it takes only that.
    """
    found: list[Command] = []
    base_scope = {**_STAND_INS, "github.repository": repo_name(repo)}
    shared_env = _env_of(document, "env")
    shared_directory = _run_default(document, "working-directory", "workflow")
    shared_shell = _run_default(document, "shell", "workflow")
    for job_id, job in _mapping(document.get("jobs"), "jobs").items():
        in_job: list[Command] = []
        job = _mapping(job, f"job {job_id}")
        steps = job.get("steps") or []
        if not isinstance(steps, list):
            raise ValueError(f"steps of job {job_id} is not a list")
        combinations, dynamic_matrix = _matrix_of(job, job_id)
        job_env = {**shared_env, **_env_of(job, f"env of job {job_id}")}
        job_directory = _run_default(job, "working-directory", f"job {job_id}") or shared_directory
        job_shell = _run_default(job, "shell", f"job {job_id}") or shared_shell or "bash"
        rust: tuple[str, ...] = ()
        toolchain_steps: tuple[str, ...] = ()
        tool_steps: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            step = _mapping(step, f"a step of job {job_id}")
            rust = _rust_setup(step) or rust
            if _TOOL_ACTION.match(str(step.get("uses") or "")):
                tool_steps.append(step)
            script = str(step.get("run") or "").strip()
            if not script and not _ARTIFACT_ACTION.match(str(step.get("uses") or "")):
                continue
            # A step runs in every combination whose conditions hold: judge it in the first such one.
            chosen: tuple[dict[str, str], Command] | None = None
            for matrix in combinations:
                command = _judge_step(
                    step,
                    _Job(job, job_id, job_env, job_directory, job_shell, dynamic_matrix),
                    {**base_scope, **matrix},
                    coverage=coverage,
                )
                if command is None:
                    break  # setup, or not a check: the same in every combination
                if chosen is None or (chosen[1].skipped and not command.skipped):
                    chosen = (matrix, command)
                if not command.skipped:
                    break
            if chosen is None:
                toolchain_steps += _rustup_rows(_expand(script, {**base_scope, **combinations[0]}) or "")
                continue
            matrix, command = chosen
            combination = ",".join(f"{key.removeprefix('matrix.')}={value}" for key, value in matrix.items())
            origin = f"{where}:{lines.get((str(job_id), index), 1)}" + (
                f" [{combination}]" if combination else ""
            )
            uv, python = _tools_of(tool_steps, {**base_scope, **matrix}, checkout)
            in_job.append(
                dataclasses.replace(
                    command, origin=origin, rust=rust, toolchain_steps=toolchain_steps, uv=uv, python=python
                )
            )
        if coverage:
            found.extend(c for c in in_job if c.check)
        elif any(c.check for c in in_job):
            found.extend(in_job)
    return found


def _rust_setup(step: dict[str, Any]) -> tuple[str, ...]:
    """Return (toolchain, *components) if `step` is an action that sets up Rust, else `()`."""
    action = _RUST_ACTION.match(str(step.get("uses") or ""))
    if not action:
        return ()
    given = step.get("with")
    inputs: dict[str, Any] = given if isinstance(given, dict) else {}
    # dtolnay/rust-toolchain names the toolchain in its ref (`@nightly`, `@1.80`); `@master` means stable.
    ref = action[2] if action[1].startswith("dtolnay/") and action[2] not in {"master", "main"} else ""
    toolchain = str(inputs.get("toolchain") or ref or "stable")
    if "${{" in toolchain or re.fullmatch(r"[0-9a-f]{40}", toolchain):
        toolchain = "stable"  # a matrix value or a pinned commit of the action: no toolchain is named
    components = [c for c in re.split(r"[,\s]+", str(inputs.get("components") or "")) if c]
    return (toolchain, *components)


def _rustup_rows(script: str) -> tuple[str, ...]:
    """Return `script` as one setup script if it runs `rustup`, else `()`; rows of other tools are dropped."""
    rows = [row.strip() for row in script.replace("\\\n", " ").splitlines()]
    rows = [row for row in rows if row.startswith("rustup ")]
    return ("\n".join(rows),) if rows else ()


def _tools_of(
    steps: list[dict[str, Any]], scope: dict[str, str], checkout: Path | None, depth: int = 0
) -> tuple[str, str]:
    """Return the (uv version, Python version) the `uses:` steps set up, reading local actions' steps."""
    uv = python = ""
    for step in steps:
        action, _, _ = str(step.get("uses") or "").partition("@")
        given = step.get("with")
        raw: dict[str, Any] = given if isinstance(given, dict) else {}
        inputs = {str(k).lower(): _expand(str(v), scope) for k, v in raw.items()}
        if action == "astral-sh/setup-uv":
            # An expression only GitHub evaluates stays as written: the gate then says it cannot install it.
            version = inputs.get("version")
            uv = str(raw["version"]) if version is None and "version" in raw else version or "latest"
        elif action == "actions/setup-python":
            python = inputs.get("python-version") or ""
        elif action.startswith("./") and checkout is not None and depth < 3:
            inner_uv, inner_python = _local_action_tools(checkout, action, inputs, scope, depth)
            uv, python = inner_uv or uv, inner_python or python
    return uv, python


def _local_action_tools(
    checkout: Path, action: str, given: dict[str, str | None], scope: dict[str, str], depth: int
) -> tuple[str, str]:
    for name in ("action.yml", "action.yaml"):
        path = checkout / action / name
        if path.is_file():
            break
    else:
        return "", ""
    try:
        document = yaml.safe_load(path.read_text(errors="replace"))
        declared = _mapping(document.get("inputs"), "inputs")
        steps = [s for s in _mapping(document.get("runs"), "runs").get("steps") or [] if isinstance(s, dict)]
    except (yaml.YAMLError, ValueError, AttributeError):
        return "", ""  # a malformed action: its job then runs with the pod's tools, as before
    inner = dict(scope)
    for name, spec in declared.items():
        default = spec.get("default") if isinstance(spec, dict) else None
        value = given.get(str(name).lower(), None if default is None else str(default))
        if name.lower() in given or default is not None:
            inner[f"inputs.{str(name).lower()}"] = value or ""
    return _tools_of(steps, inner, checkout, depth + 1)


@dataclasses.dataclass(frozen=True)
class _Job:
    """What a job contributes to each of its steps."""

    node: dict[str, Any]
    id: str
    env: dict[str, str]
    directory: str | None
    shell: str
    dynamic_matrix: bool


def _judge_step(step: dict[str, Any], job: _Job, scope: dict[str, str], *, coverage: bool) -> Command | None:
    """Judge one step in one matrix combination (origin left empty); None when it is a pure install.

    A step that is not a check comes back with `check` False; for coverage, one that does not measure
    coverage is None.
    """
    script = str(step.get("run") or "").strip()
    name = str(step.get("name") or "")
    # What the runner would see: expressions replaced. None is one only GitHub can evaluate.
    env: dict[str, str | None] = {
        key: _expand(value, scope)
        for key, value in {**job.env, **_env_of(step, f"env of a step of job {job.id}")}.items()
    }
    step_scope = {**scope, **{f"env.{k.lower()}": v for k, v in env.items() if v is not None}}
    action = _ARTIFACT_ACTION.match(str(step.get("uses") or ""))
    inputs: dict[str, str | None] = {}
    if action:
        if coverage:
            return None
        given = _mapping(step.get("with"), f"with of a step of job {job.id}")
        inputs = {str(k).lower(): _expand(_env_value(v), step_scope) for k, v in given.items()}
        # What a report calls the step; the gate runs `inputs`, not this.
        script = f"actions/{action[1]} " + str(given.get("name") or given.get("pattern") or "*")
    expanded = _expand(script, step_scope)
    directory = str(step.get("working-directory") or "") or job.directory or ""
    expanded_directory = _expand(directory, step_scope)
    if not action and _is_setup(name, expanded or script):
        return None
    is_check = not action and bool(
        _COVERAGE_STEP.search(script) if coverage else _CHECK.search(f"{name} {expanded or script}")
    )
    if coverage and not is_check:
        return None
    shell = str(step.get("shell") or job.shell)
    skipped = _condition(step, step_scope) or _condition(job.node, scope)
    if not skipped and (_continues_on_error(step) or _continues_on_error(job.node)):
        skipped = "continue-on-error: it cannot fail CI"
    unrunnable = ""
    unresolved = (
        expanded is None or expanded_directory is None or None in env.values() or None in inputs.values()
    )
    if unresolved and job.dynamic_matrix and not skipped:
        # What the step needs may be a value only the generated matrix has: nothing to run offline.
        skipped = "its matrix cannot be decided offline"
    elif unresolved:
        unrunnable = "cannot run offline: uses an expression"
    elif shell not in SHELLS and not _is_shell_template(shell):
        unrunnable = f"cannot run offline: shell {shell}"
    return Command(
        expanded if expanded is not None else script,
        "",
        tuple((k, v) for k, v in env.items() if v is not None),
        (directory if expanded_directory is None else expanded_directory) or None,
        unrunnable,
        shell,
        skipped,
        str(job.id),
        is_check,
        timeout=_timeout_of(step) or _timeout_of(job.node),
        needs=tuple(_listed(job.node.get("needs"))),
        runs_after_failure=_runs_after_failure(step, step_scope),
        job_runs_after_failure=_runs_after_failure(job.node, scope),
        action=action[1] if action else "",
        inputs=tuple((k, v) for k, v in inputs.items() if v is not None),
    )


def _is_shell_template(shell: str) -> bool:
    """Whether `shell` is a known shell's command line with `{0}` for the script file, which runs as given."""
    return "{0}" in shell and shell.split()[0] in SHELLS


def _glob_matches(pattern: str, name: str) -> bool:
    """Whether the GitHub filter glob `pattern` matches `name`: `*` stays inside a directory, `**` does not.

    Not `fnmatch`: its `*` crosses `/`.
    """
    regex = ""
    position = 0
    while position < len(pattern):
        if pattern.startswith("**/", position):
            regex += "(?:.*/)?"
            position += 3
        elif pattern.startswith("**", position):
            regex += ".*"
            position += 2
        elif pattern[position] == "*":
            regex += "[^/]*"
            position += 1
        elif pattern[position] == "?":
            regex += "[^/]"
            position += 1
        else:
            regex += re.escape(pattern[position])
            position += 1
    return re.fullmatch(regex, name) is not None


def _listed(node: Any) -> list[str]:
    """Return a trigger filter (`paths`, `branches`) as a list of patterns."""
    return [node] if isinstance(node, str) else [str(item) for item in node] if isinstance(node, list) else []


def _selected(patterns: list[str], name: str) -> bool:
    """Whether the filter `patterns` select `name`: the last pattern that matches decides, `!` excludes."""
    chosen = False
    for pattern in patterns:
        if _glob_matches(pattern.removeprefix("!"), name):
            chosen = not pattern.startswith("!")
    return chosen


def _is_pull_request_check(triggers: Any, changed: set[str] | None) -> bool:
    """Whether GitHub would run a workflow with these `on:` triggers for a pull request changing `changed`.

    `changed` None (unknown) keeps every workflow with a `pull_request` or `push` trigger.
    """
    if isinstance(triggers, str):
        triggers = [triggers]
    if isinstance(triggers, list):
        triggers = dict.fromkeys(triggers)
    if not isinstance(triggers, dict):
        return False
    for event in _TRIGGERS & set(triggers):
        filters = _mapping(triggers[event], f"the {event} trigger")
        if event == "push":
            branches = _listed(filters.get("branches"))
            tags_only = ("tags" in filters or "tags-ignore" in filters) and not (
                "branches" in filters or "branches-ignore" in filters
            )
            ignored = _listed(filters.get("branches-ignore"))
            if tags_only or (branches and not any(_selected(branches, b) for b in ("main", "master"))):
                continue
            if ignored and all(_selected(ignored, b) for b in ("main", "master")):
                continue
        if changed is None:
            return True
        paths = _listed(filters.get("paths"))
        ignored_paths = _listed(filters.get("paths-ignore"))
        if paths and not any(_selected(paths, name) for name in changed):
            continue
        if ignored_paths and all(_selected(ignored_paths, name) for name in changed):
            continue
        return True
    return False


def _workflow_commands(
    checkout: Path, changed: set[str] | None, repo: str = "", *, coverage: bool
) -> list[Command]:
    """Return the `run:` scripts of pull-request jobs that are checks, or (`coverage`) measure coverage.

    A step is one script: `cd`, `export` and a multi-line `if` mean what they mean to the runner. A
    workflow that cannot be read is a Command the gate fails, not an exception out of the gate. `changed`
    is the files the pull request changes (None: unknown), which the workflows' `paths:` filters select on.
    """
    found: list[Command] = []
    for path in sorted((checkout / ".github/workflows").glob("*.y*ml")):
        where = str(path.relative_to(checkout))
        text = path.read_text(errors="replace")
        try:
            document = yaml.safe_load(text)
            if not isinstance(document, dict):
                continue
            # YAML 1.1 reads the bare key `on` as the boolean True.
            if not _is_pull_request_check(document.get("on", document.get(True)), changed):
                continue
            lines = _step_lines(text)
            found.extend(_steps_of(document, lines, where, repo, checkout, coverage=coverage))
        except (yaml.YAMLError, ValueError) as exc:
            found.append(Command(f"read {where}", where, unrunnable=f"cannot read {where}: {exc}"))
    return found


def _makefile_targets(checkout: Path, wanted: re.Pattern[str]) -> list[Command]:
    makefile = checkout / "Makefile"
    if not makefile.is_file():
        return []
    found = []
    for number, row in enumerate(makefile.read_text(errors="replace").splitlines(), 1):
        target = _MAKE_TARGET.match(row)
        if target and wanted.fullmatch(target[1]):
            found.append(Command(f"make {target[1]}", f"Makefile:{number}"))
    return found


def _merge(commands: list[Command]) -> list[Command]:
    """One entry per script: a workflow step `make lint` and the Makefile target `lint` are one check."""
    merged: dict[tuple[Any, ...], Command] = {}
    # The first key of each script whatever its toolchain, for the Makefile targets.
    by_script: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for item in commands:
        # A step that is no check runs for the job it is in: the same script in another job is not a repeat.
        scope = (item.origin, item.job) if not item.check else ()
        script = (
            item.command,
            item.env,
            item.cwd,
            item.unrunnable,
            item.shell,
            item.skipped,
            item.uv,
            *scope,
        )
        # The same `cargo test` under another toolchain (an MSRV job) is another check.
        key = (*script, item.python, item.rust, item.toolchain_steps)
        if item.origin.startswith("Makefile:"):
            # A Makefile target has no toolchain of its own: it is the workflow step that runs it, under
            # whichever toolchain that job sets up (the workflows come first).
            key = by_script.get(script, key)
        by_script.setdefault(script, key)
        first = merged.setdefault(key, item)
        if first is not item:
            merged[key] = dataclasses.replace(first, origin=f"{first.origin}, {item.origin}")
    return list(merged.values())


def _required(commands: list[Command]) -> list[Command]:
    return _merge([c for c in commands if not c.skipped])


def discover(checkout: Path, changed: set[str] | None = None, repo: str = "") -> list[Command]:
    """Return what the target's CI runs that a patch must pass: lint, types, docstrings, locks, tests.

    `changed` is the files the patch changes (None: unknown, every pull-request workflow counts); `repo`
    is what `Delivers to:` names, for `github.repository`.
    """
    workflow = _workflow_commands(checkout, changed, repo, coverage=False)
    return _required(workflow + _with_uv_of(workflow, _makefile_targets(checkout, _MAKE_CHECK)))


def _with_uv_of(workflow: list[Command], targets: list[Command]) -> list[Command]:
    """Give Makefile targets the uv the workflows install: a developer's `make` runs on the same uv.

    With several versions among the workflows the newest is taken, since the lock file is written by it.
    """
    versions = {c.uv for c in workflow if c.uv}
    exact = sorted(
        (v for v in versions if re.fullmatch(r"\d+(\.\d+)*", v)), key=lambda v: tuple(map(int, v.split(".")))
    )
    uv = exact[-1] if exact else "latest" if versions else ""
    return [dataclasses.replace(c, uv=uv) for c in targets]


def skipped(checkout: Path, changed: set[str] | None = None, repo: str = "") -> list[Command]:
    """Return the check-like steps that cannot fail the target's CI, which `discover` leaves out."""
    workflow = _workflow_commands(checkout, changed, repo, coverage=False)
    return _merge([c for c in workflow if c.skipped and c.check])


def coverage_command(checkout: Path) -> Command:
    """Return how the target measures coverage: its `make coverage`, a workflow step, else pytest-cov.

    The pytest-cov config of a target (`addopts = --cov` in pyproject, setup.cfg or tox) is not read:
    `FALLBACK_COVERAGE` asks for the same measurement, and a repeated `--cov` is harmless.
    """
    own = _required(
        _makefile_targets(checkout, _MAKE_COVERAGE) + _workflow_commands(checkout, None, coverage=True)
    )
    own = [c for c in own if not c.unrunnable]
    if own:
        return own[0]
    workflow = _workflow_commands(checkout, None, coverage=False)
    # The fallback reruns the target's pytest under --cov: it gets the longest limit the target's CI gives a
    # pytest step, not the gate's per-step default (the engine's suite outruns that default).
    limits = [c.timeout for c in workflow if c.timeout and re.search(r"\bpytest\b", c.command)]
    timeout = max(limits, default=None)
    if (checkout / "uv.lock").is_file():
        # The uv the workflows install: the pod's own may be too old for the lock file.
        return _with_uv_of(workflow, [Command(FALLBACK_COVERAGE_UV, "default", timeout=timeout)])[0]
    return Command(FALLBACK_COVERAGE, "default", timeout=timeout)


def coverage_report_command(measured: Command, out: Path) -> Command | None:
    """Return how to write `measured`'s data file as JSON to `out` when the python on PATH cannot.

    None: `python -m coverage json` from PATH reads it. The uv fallback measured in the project's own
    environment, where it installed pytest-cov (and so coverage), so its report is written there too, with
    the same uv.
    """
    if measured.command != FALLBACK_COVERAGE_UV:
        return None
    report = f"uv run --no-sync python -m {' '.join(COVERAGE_JSON)} -o {shlex.quote(str(out))}"
    return dataclasses.replace(measured, command=report)


def parse_env_file(text: str) -> dict[str, str]:
    """Return the variables a step wrote to `$GITHUB_ENV`: `NAME=value` rows and `NAME<<DELIMITER` blocks."""
    found: dict[str, str] = {}
    rows = iter(text.splitlines())
    for row in rows:
        if "<<" in row.partition("=")[0]:
            name, _, delimiter = row.partition("<<")
            body = []
            for inner in rows:
                if inner == delimiter:
                    break
                body.append(inner)
            found[name] = "\n".join(body)
        elif "=" in row:
            name, _, value = row.partition("=")
            found[name] = value
    return found


def parse_listed_lines(summary: str) -> set[str]:
    """`path:line` entries under `## Uncovered changed lines` in SUMMARY.md that give a reason."""
    listed: set[str] = set()
    inside = False
    for row in summary.splitlines():
        if row.startswith("#"):
            inside = row.lstrip("#").strip().lower() == "uncovered changed lines"
        elif inside and (entry := re.match(r"^\s*(?:[-*]\s*)?`?(\S+?:\d+)`?\s+--\s+\S", row)):
            listed.add(entry[1])
    return listed


def job_paths(text: str, env: dict[str, str]) -> str:
    """Put the job's own paths (`RUNNER_TEMP`, `GITHUB_WORKSPACE` in `env`) in place of their expressions."""
    for context, variable in JOB_PATHS.items():
        text = text.replace(_STAND_INS[context], env[variable])
    return text
