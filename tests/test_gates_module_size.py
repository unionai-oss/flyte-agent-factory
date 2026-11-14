"""The module-size ratchet: a source module may not pass the limit, and one already over it may not grow."""

import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _gate_base, _gate_start, _gate_tests, _gates, _module_size
from tests import _standards as template

pytestmark = pytest.mark.real_factory_gates

LIMIT = _module_size.MODULE_LINE_LIMIT


def _write(root: Path, relative: str, lines: int) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines)


def _check(tmp_path: Path, head: dict[str, int], start: dict[str, int] | None) -> tuple[int, str]:
    for relative, lines in head.items():
        _write(tmp_path / "head", relative, lines)
    (tmp_path / "head").mkdir(exist_ok=True)
    if start is not None:
        for relative, lines in start.items():
            _write(tmp_path / "start", relative, lines)
        (tmp_path / "start").mkdir(exist_ok=True)
    return _module_size.check(
        tmp_path / "head", tmp_path / "start" if start is not None else None, _gate_tests._is_test
    )


def test_the_rule_is_shipped_and_parsed():
    [rule] = [r for r in template.RULES if r.ident == template.MODULE_SIZE]
    assert rule.level == "MUST"
    assert "factory:module-size" in rule.body
    assert (rule.kind, rule.arg) == ("check", "factory:module-size")


def test_a_new_module_over_the_limit_fails(tmp_path):
    code, output = _check(tmp_path, {"pkg/big.py": LIMIT + 1}, {"pkg/small.py": 3})
    assert code == 1
    assert f"pkg/big.py: {LIMIT + 1} lines, new file" in output


def test_an_over_limit_module_that_grew_fails(tmp_path):
    code, output = _check(tmp_path, {"big.py": LIMIT + 6}, {"big.py": LIMIT + 5})
    assert code == 1
    assert f"big.py: {LIMIT + 6} lines, was {LIMIT + 5}" in output


def test_an_over_limit_module_that_shrank_or_stayed_passes_with_a_note(tmp_path):
    code, output = _check(
        tmp_path, {"a.py": LIMIT + 4, "b.py": LIMIT + 5}, {"a.py": LIMIT + 5, "b.py": LIMIT + 5}
    )
    assert code == 0
    assert f"over the limit but not grown: a.py ({LIMIT + 4} lines, was {LIMIT + 5})" in output
    assert f"over the limit but not grown: b.py ({LIMIT + 5} lines, was {LIMIT + 5})" in output


def test_a_module_moved_with_its_package_is_judged_against_its_old_path(tmp_path):
    """A package rename moves every module; one over the limit that did not grow is debt, not a new file."""
    start = {"src/old_pkg/big.py": LIMIT + 5}
    code, output = _check(tmp_path, {"src/new_pkg/big.py": LIMIT + 5}, start)
    assert code == 0
    assert "over the limit but not grown: src/new_pkg/big.py" in output
    code, output = _check(tmp_path / "grew", {"src/new_pkg/big.py": LIMIT + 6}, start)
    assert code == 1
    assert f"src/new_pkg/big.py: {LIMIT + 6} lines, was {LIMIT + 5}" in output


def test_a_new_module_that_only_shares_a_name_is_still_new(tmp_path):
    """Only a path differing in one directory counts as a move; a same-named file elsewhere does not."""
    code, output = _check(tmp_path, {"a/b/c/big.py": LIMIT + 1}, {"x/y/big.py": LIMIT + 1})
    assert code == 1
    assert "new file" in output


def test_a_module_at_the_limit_passes(tmp_path):
    code, output = _check(tmp_path, {"ok.py": LIMIT}, {})
    assert code == 0
    assert "over the limit" not in output


def test_without_a_start_tree_any_module_over_the_limit_fails(tmp_path):
    code, output = _check(tmp_path, {"big.py": LIMIT + 1}, None)
    assert code == 1
    assert "big.py" in output


def test_tests_and_vendored_trees_are_excluded(tmp_path):
    big = LIMIT + 1
    head = {
        "tests/test_big.py": big,
        "pkg/test_big.py": big,
        "conftest.py": big,
        ".venv/lib/dep.py": big,
        "node_modules/pkg/dep.py": big,
        "app/migrations/0001_initial.py": big,
    }
    assert _check(tmp_path, head, {})[0] == 0


def test_only_python_modules_count(tmp_path):
    (tmp_path / "head").mkdir()
    (tmp_path / "head/uv.lock").write_text("x\n" * (LIMIT + 1))
    assert _module_size.check(tmp_path / "head", None, _gate_tests._is_test)[0] == 0


def test_standards_results_runs_the_check_when_the_rule_is_selected(tmp_path):
    _write(tmp_path, "big.py", LIMIT + 1)
    rules = {"engineering.md": "## SIZE-1 MUST\nEnforced: check factory:module-size\n"}
    results = {r.rule: r for r in _gates.standards_results(tmp_path, rules, configs=tmp_path)}
    assert results["factory:module-size"].status is _gate_base.Status.FAIL
    assert results["factory:module-size"].output.startswith("[SIZE-1] ")
    assert "SIZE-1" not in results


def test_standards_results_does_not_run_the_check_when_no_rule_maps_it(tmp_path):
    _write(tmp_path, "big.py", LIMIT + 1)
    rules = {"engineering.md": "## SIZE-1 MUST\nEnforced: judgement\n"}
    assert _gates.standards_results(tmp_path, rules, configs=tmp_path) == []


def test_this_repository_does_not_grow_past_the_limit_from_its_own_head():
    root = Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True).returncode:
        pytest.skip("not a git checkout")
    with _gate_start._start_tree(root, "HEAD", None) as start:
        code, output = _module_size.check(root, start, _gate_tests._is_test)
    assert code == 0, output
    # The repository's last over-limit module (_bench_suite.py) was split: nothing is left over the limit.
    assert output.splitlines()[0] == "no module over the size limit"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_a_scan_judges_growth_against_the_previous_commit(tmp_path):
    """`standards <dir>` has no round start tree: inherited debt is a note, growth since the parent fails."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/big.py", LIMIT + 10)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "debt")
    (tmp_path / "README.md").write_text("x\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "unrelated")
    reference = _gates._parent_tree(tmp_path, tmp_path.parent / "parent")
    assert reference is not None
    assert _module_size.check(tmp_path, reference, _gate_tests._is_test)[0] == 0

    _write(tmp_path, "pkg/big.py", LIMIT + 20)
    _git(tmp_path, "commit", "-qam", "grew")
    reference = _gates._parent_tree(tmp_path, tmp_path.parent / "parent2")
    code, output = _module_size.check(tmp_path, reference, _gate_tests._is_test)
    assert code == 1 and "was 1510" in output


def test_a_scan_outside_git_or_on_a_root_commit_has_no_reference(tmp_path):
    assert _gates._parent_tree(tmp_path, tmp_path / "parent") is None
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "a.py", 1)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "root")
    assert _gates._parent_tree(tmp_path, tmp_path / "parent") is None
