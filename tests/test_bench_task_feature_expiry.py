"""The feature-expiry task: its hidden check fails on the committed start and passes on the reference."""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from flyte_agent_factory import _bench_check, _bench_suite, _gates, _timeline

pytestmark = pytest.mark.slow  # runs terraform or the hidden benchmark check: the slow lane

BENCH = Path(__file__).resolve().parent / "fixtures" / "bench"
START = BENCH / "expiry_start"
REF = BENCH / "expiry_ref"
TASK_ID = "feature-expiry"
TESTS = (
    "test_invalid_expires_in_is_rejected",
    "test_valid_expires_in_sets_expires_at",
    "test_no_expiry_means_expires_at_null",
    "test_an_expired_link_is_gone_and_its_slug_stays_taken",
    "test_a_410_is_not_a_visit",
    "test_expiry_includes_the_exact_boundary",
    "test_random_generation_does_not_return_an_expired_slug",
)


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


def project(tmp_path: Path, source: Path = REF) -> Path:
    copy = tmp_path / "case"
    shutil.copytree(source, copy, ignore=shutil.ignore_patterns("__pycache__"))
    return copy


def check(case: Path) -> _bench_check.CheckResult:
    # One at a time across the parallel workers: the hidden tests time a live server over HTTP, and several
    # at once on a loaded machine turned an expected failure into an error.
    lock = Path(tempfile.gettempdir()) / f"factory-tests-expiry-{os.getuid()}.lock"
    with open(lock, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _bench_check.run_hidden_check(_bench_suite.get(TASK_ID), case, key_vars=())


def test_the_task_is_registered_and_pinned():
    task = _bench_suite.get(TASK_ID)
    assert TASK_ID in _bench_suite.TASKS
    assert (task.kind, task.cap_usd, task.cap_hours, task.estimate_usd) == ("build", 40.0, 3.0, 15.0)
    assert task.workdir == "bench/expiry" and task.start_tree == committed_tree(task.id)


def test_the_hidden_check_passes_the_reference(tmp_path):
    result = check(project(tmp_path))
    assert result["exit_code"] == 0, result["summary"]
    assert f"{len(TESTS) + 1} passed" in result["summary"]


def test_the_hidden_check_fails_the_start(tmp_path):
    result = check(project(tmp_path, START))
    assert result["exit_code"] != 0
    assert f"{len(TESTS)} failed" in result["summary"]


def test_the_start_has_no_blocking_gate_failure(tmp_path):
    workspace = tmp_path / "ws"
    shutil.copytree(START, workspace / "bench-case")
    for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "start")):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
            cwd=workspace,
            check=True,
            capture_output=True,
        )
    assert _gates.run(workspace, "bench-case", "", "", "", {}, _timeline._Timeline()) == ""


# Each variant is one plausible wrong implementation; the check must name exactly the test it breaks.
VARIANTS = {
    "accepts_true": (
        "type(expires_in) is not int or not 1 <= expires_in <= MAX_EXPIRES_IN",
        "not isinstance(expires_in, (int, float)) or not 1 <= expires_in <= MAX_EXPIRES_IN",
        "test_invalid_expires_in_is_rejected",
    ),
    "off_by_one_cap": (
        "1 <= expires_in <= MAX_EXPIRES_IN",
        "1 <= expires_in <= MAX_EXPIRES_IN + 1",
        "test_invalid_expires_in_is_rejected",
    ),
    "zero_allowed": (
        "1 <= expires_in <= MAX_EXPIRES_IN",
        "0 <= expires_in <= MAX_EXPIRES_IN",
        "test_invalid_expires_in_is_rejected",
    ),
    "expiry_ignored_on_redirect": (
        "            if expired(row):\n                return self.reply(410",
        "            if False:\n                return self.reply(410",
        "test_an_expired_link_is_gone_and_its_slug_stays_taken",
    ),
}


@pytest.mark.parametrize("variant", VARIANTS)
def test_the_hidden_check_rejects_other_incorrect_solutions(tmp_path, variant):
    old, new, failing = VARIANTS[variant]
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    text = module.read_text()
    assert old in text
    module.write_text(text.replace(old, new))
    result = check(case)
    assert result["exit_code"] != 0, result["summary"]
    assert failing in result["summary"]


def test_a_410_that_is_counted_as_a_visit_fails_only_that_test(tmp_path):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    text = module.read_text()
    old = (
        '            if expired(row):\n                return self.reply(410, {"error": "expired"})\n'
        "            if ATOMIC_VISITS:"
    )
    new = (
        "            if expired(row):\n                with LOCK:\n"
        '                    db.execute("UPDATE links SET visits = visits + 1 WHERE slug = ?", (slug,))\n'
        '                    db.commit()\n                return self.reply(410, {"error": "expired"})\n'
        "            if ATOMIC_VISITS:"
    )
    assert old in text
    module.write_text(text.replace(old, new))
    result = check(case)
    assert "1 failed" in result["summary"] and "test_a_410_is_not_a_visit" in result["summary"]


def test_the_hidden_check_accepts_a_different_visit_column(tmp_path):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    text = module.read_text().replace("visits", "hit_count")
    module.write_text(text.replace('"hit_count": row[3]', '"visits": row[3]'))
    result = check(case)
    assert result["exit_code"] == 0, result["summary"]


def test_the_hidden_check_accepts_a_moved_server_and_renamed_helper(tmp_path):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    (module.parent / "server.py").write_text(module.read_text().replace("expired(", "is_gone("))
    module.write_text("from shortlinks import server\n")
    result = check(case)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_the_hidden_check_preserves_original_tests(tmp_path, change):
    case = project(tmp_path)
    original = case / "tests" / "test_import.py"
    if change == "edit":
        original.write_text("def test_replacement():\n    assert True\n")
    else:
        original.unlink()
        (original.parent / "test_replacement.py").write_text("def test_replacement():\n    assert True\n")
    result = check(case)
    assert result["exit_code"] != 0, result["summary"]
    assert "test_original_tests_are_unchanged" in result["summary"]


@pytest.mark.parametrize(
    "old,new,failing",
    [
        (
            'while not slug or (not body.get("slug") and find(slug, deleted=True)):',
            "while not slug:",
            "test_random_generation_does_not_return_an_expired_slug",
        ),
        (
            "datetime.now(timezone.utc) >= datetime.fromisoformat(row[4])",
            "datetime.now(timezone.utc) > datetime.fromisoformat(row[4])",
            "test_expiry_includes_the_exact_boundary",
        ),
    ],
)
def test_the_hidden_check_rejects_missing_collision_and_boundary_checks(tmp_path, old, new, failing):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    assert old in module.read_text()
    module.write_text(module.read_text().replace(old, new))
    result = check(case)
    assert result["exit_code"] != 0, result["summary"]
    assert failing in result["summary"]


def test_the_start_contains_no_reference_switches():
    init = (START / "shortlinks" / "__init__.py").read_text()
    assert "Not the bench start tree" not in init
    assert "FREE_DELETED_SLUGS" not in init
    assert "ATOMIC_VISITS" not in init


def test_an_unobservable_counter_records_only_a_subassertion_skip(tmp_path):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    text = module.read_text().replace("visits", "hit_count").replace("expires_at", "deadline")
    text = text.replace('"hit_count": row[3]', '"visits": row[3]')
    module.write_text(text.replace('"deadline":', '"expires_at":'))
    result = check(case)
    assert result["exit_code"] == 0, result["summary"]
    assert "410 visit count not observable" in result["summary"]


def test_other_clocks_and_generators_are_allowed_with_recorded_limits(tmp_path):
    case = project(tmp_path)
    module = case / "shortlinks" / "__main__.py"
    text = module.read_text().replace(
        "datetime.now(timezone.utc)", "datetime.fromtimestamp(time.time(), timezone.utc)"
    )
    module.write_text(text.replace("random.choices(", "random.SystemRandom().choices("))
    result = check(case)
    assert result["exit_code"] == 0, result["summary"]
    assert "exact-equality assertion unavailable" in result["summary"]
    assert "forced-collision assertion unavailable" in result["summary"]
