"""bugfix-shortlinks: the new regression tests must catch each seeded bug, not just exist."""

import shutil
from pathlib import Path

import pytest

from flyte_agent_factory import _bench_check, _bench_suite

pytestmark = pytest.mark.slow  # runs the hidden benchmark check: the slow lane

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bench"
TASK = _bench_suite.get("bugfix-shortlinks")

SLUG_ONLY = """\
from test_api import call, port  # type: ignore[import-not-found]  # noqa: F401


def test_deleted_slug_is_reusable(port):
    body = {"slug": "reusable", "url": "https://example.com"}
    assert call(port, "POST", "/links", body)[0] == 201
    assert call(port, "DELETE", "/links/reusable")[0] == 204
    assert call(port, "POST", "/links", body)[0] == 201
"""

VISITS_ONLY = """\
from concurrent.futures import ThreadPoolExecutor

from test_api import call, port  # type: ignore[import-not-found]  # noqa: F401


def test_parallel_visits_are_counted(port):
    assert call(port, "POST", "/links", {"slug": "p", "url": "https://example.com"})[0] == 201
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: call(port, "GET", "/p")[0], range(50)))
    assert call(port, "GET", "/links/p")[1]["visits"] == 50
"""

DUMMY = "def test_nothing():\n    assert True\n"


def case_with_tests(tmp_path: Path, **files: str) -> Path:
    project = tmp_path / "case"
    shutil.copytree(FIXTURES / "shortlinks_ref", project)
    (project / "tests" / "test_regressions.py").unlink()
    for name, text in files.items():
        (project / "tests" / f"{name}.py").write_text(text)
    return project


def check(project: Path) -> _bench_check.CheckResult:
    return _bench_check.run_hidden_check(TASK, project, key_vars=())


def test_the_reference_regression_tests_pass_the_mutation_check(tmp_path):
    project = tmp_path / "case"
    shutil.copytree(FIXTURES / "shortlinks_ref", project)
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]
    assert "6 passed" in result["summary"]


@pytest.mark.parametrize(
    "files",
    [
        {"test_dummy": DUMMY},
        {"test_slug": SLUG_ONLY},
        {"test_visits": VISITS_ONLY},
        {"test_dummy": DUMMY, "test_slug": SLUG_ONLY},
    ],
    ids=["dummy", "slug-only", "visits-only", "dummy-and-slug"],
)
def test_regression_tests_that_miss_a_bug_are_rejected(tmp_path, files):
    result = check(case_with_tests(tmp_path, **files))
    assert result["exit_code"] != 0, result["summary"]
    assert "test_new_regression_tests_fail_on_each_seeded_bug" in result["summary"]


def test_regression_tests_split_across_files_are_accepted(tmp_path):
    result = check(case_with_tests(tmp_path, test_slug=SLUG_ONLY, test_visits=VISITS_ONLY))
    assert result["exit_code"] == 0, result["summary"]


def test_the_shipped_buggy_source_is_the_pinned_start():
    shipped = TASK.check_files["shortlinks_start_main.txt"]
    assert shipped == (FIXTURES / "shortlinks_start" / "shortlinks" / "__main__.py").read_text()
