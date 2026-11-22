"""Policy keys are strict: an unknown one is refused, and the known set is what the code reads.

`_policy.POLICY_KEYS` is the one list. The drift tests scan the source rather than trust a copy of it:
`_policy.read("KEY")` is where a policy knob is read, so a literal there must be listed, and a listed key must
be read. The keys built from a role or purpose name (`<ROLE>_MODEL`, `<PURPOSE>_ENGINE`, ...) are checked
against the roles and purposes the source really asks for.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from flyte_agent_factory import _factory_policy, _names, _policy, _profile, _review, _roles, _usage

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "flyte_agent_factory"
EXAMPLE = ROOT / "profiles" / "example.toml"


def _trees():
    return {path: ast.parse(path.read_text()) for path in sorted(PACKAGE.rglob("*.py"))}


def _calls(name: str):
    """Every call of `name` or `<module>.name` in the source."""
    for tree in _trees().values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == name) or (
                    isinstance(func, ast.Name) and func.id == name
                ):
                    yield node


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_reads() -> set[str]:
    """The keys read by name: `_policy.read("KEY")` (inside `_policy.py` itself `read` is the helper)."""
    reads = set()
    for call in _calls("read"):
        func = call.func
        if isinstance(func, ast.Attribute) and ast.unparse(func.value) == "_policy" and call.args:
            if (key := _literal(call.args[0])) is not None:
                reads.add(key)
    return reads


def _purposes() -> set[str]:
    """Roles and purposes the source names when it asks for a model, effort or engine."""
    found = {
        c
        for c in (
            _literal(call.args[0])
            for name in ("resolve", "resolve_codex")
            for call in _calls(name)
            if call.args
        )
        if c
    }
    for call in _calls("_call_reviewer"):
        found |= {v for kw in call.keywords if kw.arg == "purpose" and (v := _literal(kw.value))}
    for call in _calls("_call_reviewer_raw"):
        found |= {v for kw in call.keywords if kw.arg == "purpose" and (v := _literal(kw.value))}
    return found | {concern for concern, _ in _review._JUDGES}


# --- one set, read from the code -----------------------------------------------------------------------


def test_every_literal_read_is_a_known_key():
    assert _literal_reads() - _policy.POLICY_KEYS == set()


def test_every_literal_key_in_the_set_is_read():
    literal = set(_policy._LITERAL_KEYS)
    # Read as `_policy.read("KEY")`, or as the variable `AGENT_FACTORY_KEY` (`_names.coordinator_engine`,
    # `_context_guard`).
    by_variable = {name.removeprefix(_names.PREFIX) for name in _env_constants()}
    assert literal - _literal_reads() - by_variable == set()


def test_the_context_guard_switches_are_policy_keys_the_pod_honours():
    """`docs/context-guard.md`: the bench arm `{"CONTEXT_GUARD": "0"}` turns the guard off in the pod."""
    from flyte_agent_factory import _context_guard

    assert _factory_policy.normalize_policy({"CONTEXT_GUARD": "0", "CONTEXT_GUARD_BYTES": "4096"}) == {
        "AGENT_FACTORY_CONTEXT_GUARD": "0",
        "AGENT_FACTORY_CONTEXT_GUARD_BYTES": "4096",
    }
    for slot in (False, True):
        profile = _profile.parse(_with_policy('CONTEXT_GUARD = "0"\n', slot=slot))
        policies = [s.policy for s in profile.slots] if slot else [profile.policy]
        assert {"CONTEXT_GUARD": "0"}.items() <= max(policies, key=len).items()
    assert not _context_guard.enabled(_factory_policy.normalize_policy({"CONTEXT_GUARD": "0"}))
    assert _context_guard.enabled(_factory_policy.normalize_policy({}))


def test_the_roles_behind_the_generated_keys_are_roles_the_code_asks_for():
    asked = _purposes()
    assert set(_policy.DEFAULTS) <= asked, "a role in DEFAULTS that nothing resolves is a dead key"
    assert set(_policy.CODEX_DEFAULTS) - {"judge"} <= asked
    # The finding check's purposes are built as `f"{judge}-check"`, which no literal scan sees.
    assert set(_policy._ENGINE_PURPOSES) - set(_policy.CHECK_PURPOSES) <= asked
    assert 'purpose=f"{judge}-check"' in (PACKAGE / "_check_findings.py").read_text()
    assert set(_policy.CHECK_PURPOSES) == {f"{c}-check" for c in ("correctness", "completeness")}


def test_every_purpose_the_code_asks_for_is_decided():
    keyed = set(_policy.DEFAULTS) | set(_policy.CODEX_DEFAULTS) | set(_policy._ENGINE_PURPOSES)
    undecided = _purposes() - keyed
    assert undecided == set(), "each is a purpose the code asks for: add it to `_policy`"


def test_the_generated_keys_are_the_documented_families():
    keys = _policy.POLICY_KEYS
    assert {"AUTHOR_MODEL", "JUDGE_EFFORT", "FINDING_CHECK_MODEL", "PLANNER_EFFORT"} <= keys
    assert {"PLANNER_CODEX_MODEL", "CORRECTNESS_CODEX_EFFORT", "ARBITER_CODEX_EFFORT"} <= keys
    assert {"COORDINATOR_ENGINE", "AUTHOR_ENGINE", "CRAFT_ENGINE", "JUDGE_ENGINE", "ARBITER_ENGINE"} <= keys
    assert {"JUDGEMENT_ENGINE", "JUDGEMENT_CODEX_MODEL", "VERIFY_CODEX_EFFORT", "DEV_CODEX_MODEL"} <= keys
    assert "SUBAGENT_CODEX_MODEL" in keys
    assert "JUDGE_CODEX_MODEL" not in keys, "nothing resolves a Codex setting for the role `judge`"
    assert all(key == key.upper() and key.replace("_", "").isalnum() for key in keys)


# --- keys built at runtime ---------------------------------------------------------------------------------

_ASKED = ("resolve", "resolve_codex", "_static_engine", "_engine_for", "_route", "_call_reviewer")

# Every call of those whose purpose is not a string literal, as (file, outermost enclosing function), and
# where the purposes it passes get their keys. A new one fails until someone says which `_policy` table
# lists its keys.
DYNAMIC_PURPOSE_SITES = frozenset(
    {
        # `resolve_codex(purpose)` for a sub-agent role: `_policy.SUBAGENT_ROLES` (`_roles` and "subagent").
        ("_subagent_run.py", "_route_direct_subagent"),
        # `_engine_for`, `resolve_codex` and `_route` for the purposes `_call_reviewer` is called with, all
        # literals (`test_every_purpose_the_code_asks_for_is_decided`), judgement among them.
        ("_reviewer.py", "_call_reviewer"),
        # `_call_reviewer(purpose=f"{judge}-check")`: `_policy.CHECK_PURPOSES`.
        ("_check_findings.py", "run"),
        # `_call_reviewer(purpose=judge)` for each of `_review._JUDGES`: `JUDGE_CONCERNS`.
        ("_review.py", "_one_judge"),
        # `_static_engine(purpose)` reads `<PURPOSE>_ENGINE` for what `_engine_for` and `_route` are asked:
        # `_policy._ENGINE_PURPOSES`.
        ("_usage.py", "_engine_for"),
        ("_usage.py", "_route"),
    }
)


# Calls of another module's function that only shares a name with one in `_ASKED`, as (module, function): they
# take no purpose and read no policy key, so they are not purpose sites.
NOT_POLICY_CALLS = frozenset(
    {
        # `_seal_launch.resolve(seal, profile)` turns a sealed launch's spec into (spec JSON, repo URL).
        ("_seal_launch", "resolve"),
    }
)


def _computed_purpose_sites(name_only: set[tuple[str, str]] | None = None) -> set[tuple[str, str]]:
    """(file, outermost enclosing function) of every call in `_ASKED` whose purpose is not a literal.

    A call in `NOT_POLICY_CALLS` is left out, and added to `name_only` when that is given.
    """
    found = set()
    for path, tree in _trees().items():
        # Methods sit in classes; a nested function belongs to the one around it.
        scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))]
        for fn in (f for scope in scopes for f in scope.body):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                owner = getattr(func.value, "id", "") if isinstance(func, ast.Attribute) else ""
                if (owner, name) in NOT_POLICY_CALLS:
                    if name_only is not None:
                        name_only.add((owner, name))
                    continue
                keyword = next((k.value for k in node.keywords if k.arg == "purpose"), None)
                purpose = node.args[0] if node.args else keyword
                if name in _ASKED and purpose is not None and _literal(purpose) is None:
                    found.add((path.name, fn.name))
    return found


def test_every_call_with_a_computed_purpose_is_listed():
    assert _computed_purpose_sites() == DYNAMIC_PURPOSE_SITES


def test_every_call_left_out_for_sharing_a_name_is_still_called():
    name_only: set[tuple[str, str]] = set()
    _computed_purpose_sites(name_only)
    assert name_only == NOT_POLICY_CALLS, "listed but never called: remove from NOT_POLICY_CALLS"


def test_the_sub_agent_roles_cover_every_installed_agent_file():
    stems = {path.stem for path in (PACKAGE / "_assets" / "agents").glob("*.md")}
    assert stems <= set(_policy.SUBAGENT_ROLES)


def _read_by(monkeypatch, call) -> set[str]:
    monkeypatch.setattr(_policy, "knobs_seen", {})
    call()
    return set(_policy.knobs_seen)


@pytest.mark.parametrize("role", sorted(set(_policy.SUBAGENT_ROLES)))
def test_a_sub_agent_role_reads_only_listed_keys(monkeypatch, role):
    assert _read_by(monkeypatch, lambda: _policy.resolve_codex(role)) <= _policy.POLICY_KEYS


@pytest.mark.parametrize("purpose", sorted(_policy._ENGINE_PURPOSES))
def test_a_routed_purpose_reads_only_listed_keys(monkeypatch, purpose):
    assert _read_by(monkeypatch, lambda: _usage._static_engine(purpose)) <= _policy.POLICY_KEYS
    assert _read_by(monkeypatch, lambda: _policy.resolve_codex(purpose)) <= _policy.POLICY_KEYS


def test_every_alias_stands_for_a_session_role():
    assert set(_roles.ROLE_ALIASES.values()) <= set(_roles.SESSION_ROLES)


def test_the_judgement_purpose_is_routed_and_tuned_on_its_own():
    assert "judgement" in _policy._ENGINE_PURPOSES and "judgement" in _policy.CODEX_DEFAULTS


# --- a typo is refused ---------------------------------------------------------------------------------


def _with_policy(lines: str, *, slot: bool = False) -> str:
    if slot:
        return EXAMPLE.read_text().replace(
            'subagent_env = "agent-a-sub"\n', f'subagent_env = "agent-a-sub"\n[slots.policy]\n{lines}', 1
        )
    return EXAMPLE.read_text().replace("[policy]\n", f"[policy]\n{lines}", 1)


@pytest.mark.parametrize("slot", [False, True], ids=["policy", "slots.policy"])
def test_a_misspelt_key_fails_the_profile_load_and_names_the_nearest_key(slot):
    section = "slots.policy" if slot else "policy"
    with pytest.raises(_profile.ProfileError) as exc:
        _profile.parse(_with_policy('COORDINATR_ENGINE = "codex"\n', slot=slot), "profile.toml")
    assert str(exc.value) == (
        f"profile.toml: unknown key in [{section}]: COORDINATR_ENGINE (did you mean COORDINATOR_ENGINE?)"
    )


def test_a_key_that_is_nothing_like_a_known_one_gets_no_hint():
    with pytest.raises(_profile.ProfileError) as exc:
        _profile.parse(_with_policy('QQQQQQQQQQQQ = "x"\n'), "profile.toml")
    assert str(exc.value) == "profile.toml: unknown key in [policy]: QQQQQQQQQQQQ"


@pytest.mark.parametrize("concern", _policy.JUDGE_CONCERNS)
@pytest.mark.parametrize("part", ["MODEL", "EFFORT"])
def test_a_judge_concern_s_claude_setting_points_at_the_one_the_judges_share(concern, part):
    # `CRAFT_EFFORT` is spelt nearest to `AUTHOR_EFFORT`, but the craft judge's effort is `JUDGE_EFFORT`.
    key = f"{concern.upper()}_{part}"
    assert key not in _policy.POLICY_KEYS
    with pytest.raises(_profile.ProfileError) as exc:
        _profile.parse(_with_policy(f'{key} = "x"\n'), "profile.toml")
    assert str(exc.value) == f"profile.toml: unknown key in [policy]: {key} (did you mean JUDGE_{part}?)"


def test_a_misspelt_key_is_refused_in_a_launch_policy():
    with pytest.raises(ValueError, match=r"unknown key in \[policy\]: REVEIW_MODEL"):
        _factory_policy.normalize_policy({"REVEIW_MODEL": "opus"})
    # The nearest key that is read: there is no REVIEW_MODEL.
    with pytest.raises(ValueError, match=r"did you mean (JUDGE|AUTHOR|PLANNER|FINDING_CHECK)_MODEL\?"):
        _factory_policy.normalize_policy({"AGENT_FACTORY_REVEIW_MODEL": "opus"})


def test_a_launch_policy_still_takes_every_known_key_with_or_without_the_prefix():
    keys = {key: "x" for key in ("JUDGE_MODEL", "ADMITTED_ENGINE", "ADMISSION_NOTE")}
    out = _factory_policy.normalize_policy(keys | {"AGENT_FACTORY_PLANNER_MODEL": "opus"})
    assert out == {f"AGENT_FACTORY_{k}": v for k, v in (keys | {"PLANNER_MODEL": "opus"}).items()}


def test_a_requeued_request_keeps_the_keys_the_planner_set_itself():
    # `_planner_runtime` writes these into a request's policy and normalizes it again on the next pass.
    once = {
        "AGENT_FACTORY_ADMITTED_ENGINE": "codex",
        "AGENT_FACTORY_ADMISSION_NOTE": "claude was at its limit",
    }
    assert _factory_policy.normalize_policy(once) == once


def test_browser_keys_are_still_refused_with_their_own_message():
    with pytest.raises(ValueError, match="a browser grant comes from the launch's browser input"):
        _factory_policy.normalize_policy({"BROWSER_IDENTITIES": "github"})


# --- every shipped profile loads -----------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted((ROOT / "profiles").glob("*.toml")), ids=lambda p: p.name)
def test_every_shipped_profile_loads(path):
    assert _profile.load(str(path)).slots


@pytest.mark.parametrize("path", sorted((ROOT / "tests" / "profiles").glob("*.toml")), ids=lambda p: p.name)
def test_every_fixture_profile_loads(path):
    _profile.load(str(path))


@pytest.mark.parametrize(
    "path",
    [
        *sorted((ROOT / "profiles").glob("*.toml")),
        PACKAGE / "_assets" / "workspace_template" / "profile.example.toml",
    ],
    ids=lambda p: p.name,
)
def test_the_policy_keys_a_shipped_profile_suggests_in_comments_are_known(path):
    """The bootstrap template and the examples comment keys out for the owner to enable: a typo there would
    be copied into a live profile."""
    suggested = re.findall(r"^#\s*([A-Z][A-Z0-9_]+) = \"", path.read_text(), re.MULTILINE)
    assert set(suggested) - _policy.POLICY_KEYS == set()


# --- the deprecated names warn -------------------------------------------------------------------------


@pytest.fixture
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(_profile, "_warned", set())


def test_author_engine_warns_naming_its_replacement_and_its_removal(fresh_warnings, capsys):
    profile = _profile.parse(_with_policy('AUTHOR_ENGINE = "claude"\n'), "profile.toml")
    assert profile.policy["COORDINATOR_ENGINE"] == "claude"
    assert (
        "profile.toml: [policy] AUTHOR_ENGINE is deprecated and will be removed; use COORDINATOR_ENGINE"
        in capsys.readouterr().err
    )


def test_the_environment_alias_warns_naming_its_replacement(monkeypatch, caplog):
    monkeypatch.setattr(_names, "_warned", set())
    monkeypatch.delenv("AGENT_FACTORY_COORDINATOR_ENGINE", raising=False)
    monkeypatch.setenv("AGENT_FACTORY_AUTHOR_ENGINE", "claude")
    with caplog.at_level(logging.WARNING, logger=_names.log.name):
        assert _names.coordinator_engine() == "claude"
    assert "AGENT_FACTORY_AUTHOR_ENGINE is deprecated and will be removed" in caplog.text
    assert "AGENT_FACTORY_COORDINATOR_ENGINE" in caplog.text


def test_personal_env_is_ignored_but_warns_naming_the_identity_tables(fresh_warnings, capsys):
    text = EXAMPLE.read_text().replace(
        "[policy]\n",
        '[browser]\npersonal = true\nprofile_uri = "memory://p"\npersonal_env = "agent-a"\n\n[policy]\n',
        1,
    )
    if "\n[browser]\n" in EXAMPLE.read_text():
        pytest.fail("the example now has a [browser] table; extend this test")
    profile = _profile.parse(text, "profile.toml")
    assert profile.browser.personal_env == "agent-a"
    err = capsys.readouterr().err
    assert "profile.toml: [browser] personal_env is ignored and will be removed" in err
    assert "[browser.identities.<name>]" in err


def test_no_personal_env_no_warning(fresh_warnings, capsys):
    _profile.parse(EXAMPLE.read_text(), "profile.toml")
    assert "personal_env" not in capsys.readouterr().err


# --- `_names.py` holds no dead names -------------------------------------------------------------------

# Read only by the test suite, as the documented opt-in for billed tests (tests/conftest.py, test_auth.py).
READ_BY_TESTS = frozenset({"AGENT_FACTORY_LIVE_TESTS"})


def _source_strings(*, skip: Path) -> set[str]:
    """Every string constant in the source and `ops/` outside `skip`, docstrings and comments excluded."""
    found = set()
    for path in [*PACKAGE.rglob("*.py"), *(ROOT / "ops").rglob("*.py")]:
        if path == skip:
            continue
        tree = ast.parse(path.read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        found |= {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
        }
    return found


def _env_constants() -> set[str]:
    """Every `AGENT_FACTORY_*` variable named in a string constant of the source and `ops/`, but `_policy`."""
    pattern = re.compile(r"AGENT_FACTORY_[A-Z0-9_]*[A-Z0-9]")
    return {name for text in _source_strings(skip=PACKAGE / "_policy.py") for name in pattern.findall(text)}


# Variables the code reads that are neither fixed names in `_names` nor policy keys: set by the factory's own
# machinery for itself, never by a profile or a launch policy.
INTERNAL_VARIABLES = frozenset(
    {
        # The browser grant: set by the factory from the profile's [browser] tables, and refused from a policy
        # (`_factory_policy._BROWSER_KEYS`).
        "AGENT_FACTORY_BROWSER_ARCHIVE_KEY_SECRET",
        "AGENT_FACTORY_BROWSER_GATE",
        "AGENT_FACTORY_BROWSER_IDENTITIES",
        "AGENT_FACTORY_BROWSER_IDENTITY_NAMES",
        # Deploy tooling: the deployer's own environment and report.
        "AGENT_FACTORY_DEPLOYER_ENV",
        "AGENT_FACTORY_DEPLOY_KEY_ENV",
        # The factory-stage apps `ops/deploy_factory.py` asks its deploy child to restart (`_deploy_apps`).
        "AGENT_FACTORY_RESTART_APPS",
        "AGENT_FACTORY_STANDARDS_REPORT",
        "AGENT_FACTORY_STATE_BUCKET",
        # GitHub mode, derived from the profile's [github] table and exported by the session setup.
        "AGENT_FACTORY_GITHUB_ISOLATION",
        "AGENT_FACTORY_GITHUB_PUSH",
        "AGENT_FACTORY_PUSH_WORKFLOWS",
        # Credential grants: the grant service's address and the pod's token for it.
        "AGENT_FACTORY_GRANTS_TOKEN",
        "AGENT_FACTORY_GRANTS_URL",
        # Test lanes: the sharded and slow lanes tell their children how to run.
        "AGENT_FACTORY_CPU_MAX_FILE",
        "AGENT_FACTORY_LANE_BUDGET_S",
        "AGENT_FACTORY_LANE_CPUS",
        "AGENT_FACTORY_LANE_JUNIT",
        "AGENT_FACTORY_LANE_MAX_SHARDS",
        "AGENT_FACTORY_LANE_RUN_HERE",
        "AGENT_FACTORY_LANE_TARGET_S",
        "AGENT_FACTORY_PYTEST_JUNIT_DIR",
        "AGENT_FACTORY_SLOW_LANE",
        "AGENT_FACTORY_SLOW_LANE_PYTHON",
        "AGENT_FACTORY_TEST_LANE",
        # A child action's run context, written by `child.write_context`.
        "AGENT_FACTORY_RUN_CONTEXT",
        # Union identity plumbing: which key, secret and tier a script or session signs in with.
        "AGENT_FACTORY_SESSION_UNION_KEY",
        "AGENT_FACTORY_UNION_IDENTITIES",
        "AGENT_FACTORY_UNION_KEY",
        "AGENT_FACTORY_UNION_SECRET",
        "AGENT_FACTORY_UNION_TIER",
    }
)


def test_every_factory_variable_the_code_reads_is_fixed_a_policy_key_or_listed_internal():
    """A new `AGENT_FACTORY_X` read is a fixed name, a policy key (or a profile setting it is refused), or is
    listed above as the factory's own."""
    named = _env_constants()
    keys = {_names.PREFIX + key for key in _policy.POLICY_KEYS}
    assert named - _names.FIXED - keys - INTERNAL_VARIABLES == set()
    assert INTERNAL_VARIABLES - named == set(), "listed but nothing names it: remove from INTERNAL_VARIABLES"


def test_every_fixed_name_in_names_is_read_somewhere_else():
    read = _source_strings(skip=PACKAGE / "_names.py")
    dead = sorted(name for name in _names.FIXED - READ_BY_TESTS if name not in read)
    assert dead == [], "set but never read: delete from _names.py"


def test_every_other_constant_and_function_in_names_is_used_elsewhere():
    source = "\n".join(p.read_text() for p in [*PACKAGE.rglob("*.py")] if p.name != "_names.py")
    public = [name for name in vars(_names) if name.isupper() or name in ("getenv", "coordinator_engine")]
    unused = [name for name in public if name not in ("FIXED",) and f"_names.{name}" not in source]
    assert unused == []


def test_the_retired_judge_codex_keys_name_the_per_concern_keys_that_replace_them():
    with pytest.raises(ValueError) as refused:
        _factory_policy.normalize_policy({"JUDGE_CODEX_MODEL": "gpt-5"})
    message = str(refused.value)
    assert (
        "unknown key in [policy]: JUDGE_CODEX_MODEL (it was never read; set CORRECTNESS_CODEX_MODEL"
        in message
    )
    assert "CRAFT_CODEX_MODEL)" in message
