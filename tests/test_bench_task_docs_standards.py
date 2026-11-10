"""The docs-standards task: its hidden check, its start and its reference."""

import ast
import dataclasses
import filecmp
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flyte_agent_factory import _bench_check, _bench_suite, _gates, _timeline

pytestmark = pytest.mark.slow  # runs terraform or the hidden benchmark check: the slow lane

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bench"
START, REF = FIXTURES / "docskit_start", FIXTURES / "docskit_ref"
TASK = _bench_suite.get("docs-standards")


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


def check(project):
    return _bench_check.run_hidden_check(TASK, project, key_vars=())


def check_hidden_only(project):
    """The hidden file alone: a change to the code would also fail the case's own tests, which run first."""
    task = dataclasses.replace(TASK, check_commands=TASK.check_commands[1:])
    return _bench_check.run_hidden_check(task, project, key_vars=())


def reference(tmp_path: Path) -> Path:
    project = tmp_path / "case"
    shutil.copytree(REF, project)
    return project


def get_doc(node: ast.Module | ast.FunctionDef) -> str:
    text = ast.get_docstring(node)
    assert text is not None
    return text


def set_doc(node: ast.Module | ast.FunctionDef, text: str) -> None:
    first = node.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
    first.value.value = text


def edit(path, old, new):
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_the_task_is_the_designed_one():
    assert (TASK.kind, TASK.workdir, TASK.start_tree) == ("build", "bench/docskit", committed_tree(TASK.id))
    assert (TASK.cap_usd, TASK.cap_hours, TASK.estimate_usd, TASK.expected) == (25.0, 2.0, 15.0, "accepted")
    assert "docs-standards" in _bench_suite.TASKS


def test_the_hidden_check_fails_the_start_on_its_documentation(tmp_path):
    project = tmp_path / "start"
    shutil.copytree(START, project)
    result = check(project)
    assert result["exit_code"] != 0
    # Broken README examples and stale docstrings fail in three modules.
    assert "4 failed, 2 passed" in result["summary"]
    assert "test_every_readme_command_runs" in result["summary"]
    # The start is what the hidden check compares behaviour against.
    assert "test_the_modules_are_the_starts_apart_from_docstrings" not in result["summary"]


def test_the_hidden_check_passes_the_reference(tmp_path):
    result = check(reference(tmp_path))
    assert result["exit_code"] == 0, result["summary"]
    assert "6 passed" in result["summary"]


def test_the_reference_is_the_start_with_only_documentation_changed():
    compared = filecmp.dircmp(START, REF, ignore=["__pycache__"])
    assert compared.diff_files == ["README.md"] and not compared.left_only and not compared.right_only
    assert not filecmp.dircmp(START / "tests", REF / "tests", ignore=["__pycache__"]).diff_files
    assert sorted(filecmp.dircmp(START / "docskit", REF / "docskit", ignore=["__pycache__"]).diff_files) == [
        "__main__.py",
        "stats.py",
        "text.py",
    ]


@pytest.mark.real_factory_gates
def test_the_factory_gates_do_not_block_the_start(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    shutil.copytree(START, tmp_path / "docskit")
    git("add", "docskit")
    git("commit", "-m", "base")
    _gates.capture_base(tmp_path, "docskit")
    blocking = _gates.run(tmp_path, "docskit", "", "pattern", "DONE", {}, _timeline._Timeline())
    assert blocking == ""
    results = json.loads((tmp_path / ".claude-flyte/docskit/standards-results.json").read_text())
    assert results and not any(r["status"] == "fail" for r in results)


# A fix that changes how the code behaves must be rejected, even when the docs are right.
@pytest.mark.parametrize(
    "path, old, new",
    [
        ("docskit/stats.py", "ordered[middle]) / 2", "ordered[middle]) // 2"),
        ("docskit/text.py", "limit - 3", "limit - 2"),
        ("docskit/__main__.py", "return 0", "return 1"),
        ("docskit/stats.py", "def median", "def _unused():\n    pass\n\n\ndef median"),
    ],
)
def test_the_hidden_check_rejects_a_change_of_behaviour(tmp_path, path, old, new):
    project = reference(tmp_path)
    edit(project / path, old, new)
    result = check_hidden_only(project)
    assert result["exit_code"] != 0
    assert "test_the_modules_are_the_starts_apart_from_docstrings" in result["summary"]


def test_the_hidden_check_rejects_a_new_module(tmp_path):
    project = reference(tmp_path)
    (project / "docskit" / "extra.py").write_text("VALUE = 1\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0 and "test_the_modules_are_the_starts_apart" in result["summary"]


@pytest.mark.parametrize("module, function", [("text", "slugify"), ("stats", "mean")])
def test_the_hidden_check_rejects_one_missing_docstring(tmp_path, module, function):
    project = reference(tmp_path)
    path = project / "docskit" / f"{module}.py"
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
    assert ast.get_docstring(node)
    node.body.pop(0)
    path.write_text(ast.unparse(tree) + "\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0
    assert "1 failed, 5 passed" in result["summary"]
    assert f"test_every_public_function_has_a_docstring[{module}]" in result["summary"]


def test_the_hidden_check_rejects_a_readme_command_that_fails(tmp_path):
    project = reference(tmp_path)
    edit(project / "README.md", "docskit summary 1 2 3 4", "docskit stats 1 2 3 4")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0 and "1 failed, 5 passed" in result["summary"]
    assert "test_every_readme_command_runs" in result["summary"]


def test_the_hidden_check_rejects_a_readme_without_commands(tmp_path):
    project = reference(tmp_path)
    (project / "README.md").write_text("# docskit\n\nRun it with `python -m docskit`.\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0 and "has no fenced shell command" in result["summary"]


@pytest.mark.parametrize(
    "commands",
    [
        '```sh\npython -m docskit slug \\\n  "Hello, World"\npython -m docskit summary 1 2 3 4\n```',
        'Use `python -m docskit slug "Hello, World"` and `python -m docskit summary 1 2 3 4`.',
        '```bash\nword="Hello, World"\ncd docskit\ncd ..\npython -m docskit slug "$word"\n'
        "python -m docskit summary 1 2 3 4\n```",
    ],
)
def test_alternative_shell_examples_are_accepted(tmp_path, commands):
    project = reference(tmp_path)
    readme = project / "README.md"
    source = readme.read_text()
    start = source.index("```sh")
    end = source.index("```", start + 3) + 3
    readme.write_text(source[:start] + commands + source[end:])
    result = check_hidden_only(project)
    assert result["exit_code"] == 0, result["summary"]


def test_false_docstrings_are_rejected(tmp_path):
    project = reference(tmp_path)
    for path in (project / "docskit").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef)) and ast.get_docstring(node):
                set_doc(node, "Always returns None and performs no calculation.")
        path.write_text(ast.unparse(tree) + "\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


def test_false_readme_with_trivial_example_is_rejected(tmp_path):
    project = reference(tmp_path)
    (project / "README.md").write_text(
        "# docskit\n```sh\ntrue\n```\n`slug` preserves uppercase; `summary` prints only the sum.\n"
    )
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize("change", ["delete", "modify", "add"])
def test_existing_tests_are_protected(tmp_path, change):
    project = reference(tmp_path)
    path = project / "tests" / "test_docskit.py"
    if change == "delete":
        path.unlink()
    elif change == "modify":
        path.write_text("def test_nothing():\n    pass\n")
    else:
        (path.parent / "test_extra.py").write_text("def test_nothing():\n    pass\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


def test_start_has_public_docstrings():
    for path in (START / "docskit").glob("*.py"):
        tree = ast.parse(path.read_text())
        assert ast.get_docstring(tree), f"{path.name}: module"
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
                assert ast.get_docstring(node), f"{path.name}:{node.name}"


@pytest.mark.parametrize(
    "old, new",
    [
        ("python -m docskit summary 1 2 3 4", "true"),
        ("Run the tests with `python -m pytest -q`.", "Try `python -m docskit slug --wrong hello`."),
        ("python -m docskit slug", "false\npython -m docskit slug"),
    ],
)
def test_missing_examples_and_failed_commands_are_rejected(tmp_path, old, new):
    project = reference(tmp_path)
    edit(project / "README.md", old, new)
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize(
    "module, function",
    [("__main__", "main"), ("stats", "mean"), ("stats", "median"), ("text", "slugify"), ("text", "truncate")],
)
@pytest.mark.parametrize("keep_examples", [False, True])
def test_each_seeded_false_docstring_must_be_corrected(tmp_path, module, function, keep_examples):
    project = reference(tmp_path)
    path = project / "docskit" / f"{module}.py"
    start = ast.parse((START / "docskit" / path.name).read_text())
    stale = next(
        get_doc(node) for node in start.body if isinstance(node, ast.FunctionDef) and node.name == function
    )
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function:
            set_doc(node, stale + ("\n\n" + get_doc(node) if keep_examples else ""))
    path.write_text(ast.unparse(tree) + "\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


def test_readme_truth_is_checked_even_with_working_examples(tmp_path):
    project = reference(tmp_path)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text() + "\nslug preserves uppercase and uses underscores; summary prints only the sum.\n"
    )
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


def test_correct_docstrings_can_use_other_words(tmp_path):
    project = reference(tmp_path)
    edit(
        project / "docskit" / "text.py",
        "Return `text` lower-cased, with each run of other characters replaced by one `-`.",
        "Make a lowercase slug, joining runs of non-ASCII letters and digits with a hyphen.",
    )
    edit(
        project / "docskit" / "stats.py",
        "Return the average of `values`; raises ZeroDivisionError when it is empty.",
        "Compute the arithmetic mean; empty inputs raise ZeroDivisionError.",
    )
    result = check_hidden_only(project)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize(
    "commands",
    [
        '```sh\ncommand=slug\npython -m docskit "$command" "Hello, World"\n'
        "python -m docskit summary 1 2 3 4\n```",
        '~~~sh\npython -m docskit slug "Hello, World"\npython -m docskit summary 1 2 3 4\n~~~',
    ],
)
def test_round_two_shell_examples_are_accepted(tmp_path, commands):
    project = reference(tmp_path)
    readme = project / "README.md"
    source = readme.read_text()
    start = source.index("```sh")
    end = source.index("```", start + 3) + 3
    readme.write_text(source[:start] + commands + source[end:])
    result = check_hidden_only(project)
    assert result["exit_code"] == 0, result["summary"]


def test_mean_can_be_documented_without_average_keywords(tmp_path):
    project = reference(tmp_path)
    edit(
        project / "docskit" / "stats.py",
        "Return the average of `values`; raises ZeroDivisionError when it is empty.",
        "Return the total divided by the number of entries; raises ZeroDivisionError when it is empty.",
    )
    result = check_hidden_only(project)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize(
    "docstring",
    [
        "Return twice the average of values; on empty input return 42.",
        "Return twice the average of values; on empty input return 42.\n\n"
        ">>> mean([2.0, 4.0])\n6.0\n>>> mean([])\n42",
    ],
)
def test_false_mean_claims_are_rejected(tmp_path, docstring):
    project = reference(tmp_path)
    path = project / "docskit" / "stats.py"
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "mean")
    set_doc(function, docstring)
    path.write_text(ast.unparse(tree) + "\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize("function", ["mean", "median"])
def test_each_function_needs_its_own_doctest(tmp_path, function):
    project = reference(tmp_path)
    path = project / "docskit" / "stats.py"
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function)
    set_doc(node, get_doc(node).split(">>>", 1)[0].strip())
    path.write_text(ast.unparse(tree) + "\n")
    result = check_hidden_only(project)
    assert result["exit_code"] != 0, result["summary"]
