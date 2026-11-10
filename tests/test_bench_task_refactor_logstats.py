"""The refactor-logstats task: its start fails the hidden check, its reference passes, and
refactors that change one byte or leave a module too long are rejected."""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from flyte_agent_factory import _bench_check, _bench_logstats_files, _bench_suite, _gates, _timeline

pytestmark = pytest.mark.slow  # runs terraform or the hidden benchmark check: the slow lane

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bench"
START, REF = FIXTURES / "logstats_start", FIXTURES / "logstats_ref"
TASK = _bench_suite.get("refactor-logstats")


def copy(source, tmp_path) -> Path:
    project: Path = tmp_path / "case"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return project


def check(project) -> _bench_check.CheckResult:
    return _bench_check.run_hidden_check(TASK, project, key_vars=())


def test_the_reference_passes_the_hidden_check(tmp_path):
    result = check(copy(REF, tmp_path))
    assert result["exit_code"] == 0, result["summary"]
    assert "24 passed" in result["summary"]


def test_the_start_fails_the_hidden_check_only_on_the_split(tmp_path):
    result = check(copy(START, tmp_path))
    assert result["exit_code"] != 0
    # The existing tests pass on the start, so the hidden file ran and failed, and only on the split.
    summary = result["summary"]
    assert "FAILED hidden/test_hidden_logstats.py::test_the_input_is_split_" in summary
    assert "FAILED hidden/test_hidden_logstats.py::test_every_module_is_at_most_200_lines" in summary
    assert "3 failed, 21 passed" in summary and "FAILED tests/" not in summary


def test_the_reference_splits_the_start_into_small_modules():
    assert len((START / "logstats" / "cli.py").read_text().splitlines()) in range(400, 451)
    sizes = {p.name: len(p.read_text().splitlines()) for p in (REF / "logstats").glob("*.py")}
    assert {"parse.py", "aggregate.py", "render.py", "cli.py"} <= set(sizes)
    assert max(sizes.values()) <= 200, sizes


def test_the_reference_keeps_the_start_tests_byte_for_byte():
    start = {p.relative_to(START / "tests"): p.read_bytes() for p in (START / "tests").rglob("*.py")}
    ref = {p.relative_to(REF / "tests"): p.read_bytes() for p in (REF / "tests").rglob("*.py")}
    assert start == ref and start


def test_the_golden_outputs_are_what_the_start_prints(tmp_path):
    files = _bench_logstats_files.FILES
    for case in ("case1", "case2", "case3", "case4", "case5", "case6"):
        log = tmp_path / f"{case}.log"
        log.write_bytes(files[f"{case}.log"].encode())
        for suffix, flags in (("txt", []), ("json", ["--json"])):
            done = subprocess.run(
                [sys.executable, "-m", "logstats", str(log), "--top", "4", *flags],
                cwd=START,
                capture_output=True,
                check=False,
            )
            assert done.returncode == (1 if case == "case5" else 0), (case, suffix)
            assert done.stderr == b"", (case, suffix)
            assert done.stdout.decode() == files[f"{case}.{suffix}"], (case, suffix)


def test_a_refactor_that_changes_one_byte_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    render = project / "logstats" / "render.py"
    text = render.read_text()
    # A trailing space on a heading: the existing tests do not look at it.
    assert '"top paths:"' in text
    render.write_text(text.replace('"top paths:"', '"top paths: "'))
    result = check(project)
    summary = result["summary"]
    assert result["exit_code"] != 0, summary
    # The existing tests passed (else the hidden file would not have run); the golden outputs caught it.
    assert "FAILED tests/" not in summary
    assert (
        "FAILED hidden/test_hidden_logstats.py::test_output_is_byte_identical_to_the_start[text-case1]"
        in summary
    )
    assert "10 failed, 14 passed" in summary


def test_a_refactor_that_leaves_a_module_over_200_lines_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    aggregate = project / "logstats" / "aggregate.py"
    aggregate.write_text(aggregate.read_text() + "# padding\n" * 140)
    result = check(project)
    summary = result["summary"]
    assert result["exit_code"] != 0, summary
    assert "FAILED tests/" not in summary
    assert "1 failed, 23 passed" in summary and "test_every_module_is_at_most_200_lines" in summary
    assert "aggregate.py" in summary


def test_a_refactor_that_edits_a_test_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    test = project / "tests" / "test_cli.py"
    test.write_text(test.read_text() + "\n# touched\n")
    result = check(project)
    assert result["exit_code"] != 0 and "test_the_tests_are_unchanged" in result["summary"]


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.real_factory_gates
def test_the_factory_gates_have_no_blocking_failure_on_the_start(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "test")
    shutil.copytree(START, tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(tmp_path, "add", "app")
    git(tmp_path, "commit", "-q", "-m", "base")
    _gates.capture_base(tmp_path, "app")
    git(tmp_path, "config", "user.email", "app@agent-factory.localhost")
    assert _gates.run(tmp_path, "app", "", "", "DONE", {}, _timeline._Timeline()) == ""


@pytest.mark.parametrize("alternative", ["rename", "dataclass"])
def test_alternative_correct_refactors_pass(tmp_path, alternative):
    project = copy(REF, tmp_path)
    if alternative == "rename":
        for path in (project / "logstats").glob("*.py"):
            path.write_text(path.read_text().replace("build_report", "make_report"))
    else:
        path = project / "logstats" / "parse.py"
        text = path.read_text().replace("import re", "import re\nfrom dataclasses import dataclass")
        start = text.index("class Record:")
        end = text.index("\ndef _zone", start)
        text = (
            text[:start]
            + """@dataclass
class Record:
    host: str
    path: str
    status: int
    size: int
    when: datetime

"""
            + text[end:]
        )
        path.write_text(text)
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


def test_an_empty_render_module_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    render = project / "logstats" / "render.py"
    cli = project / "logstats" / "cli.py"
    text = cli.read_text().replace(
        "from logstats.render import build_report, format_time, render_json, render_text",
        render.read_text().replace("from __future__ import annotations", ""),
    )
    # Removing blank lines keeps this attack within the physical line limit.
    cli.write_text("\n".join(line for line in text.splitlines() if line.strip()) + "\n")
    render.write_text("")
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize("module", ["parse", "aggregate", "render"])
def test_delegating_a_module_back_to_cli_is_rejected(tmp_path, module):
    project = copy(REF, tmp_path)
    path = project / "logstats" / f"{module}.py"
    source = path.read_text()
    definitions = [
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_")
    ]
    cli = project / "logstats" / "cli.py"
    text = cli.read_text()
    import_line = next(line for line in text.splitlines() if line.startswith(f"from logstats.{module} "))
    text = text.replace(import_line, source.replace("from __future__ import annotations", ""))
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            ):
                node.body.pop(0)
    cli.write_text("\n".join(line for line in ast.unparse(tree).splitlines() if line.strip()) + "\n")
    wrappers = []
    for node in definitions:
        name = node.name
        if isinstance(node, ast.ClassDef):
            wrappers.append(
                f"class {name}:\n"
                "    def __new__(cls, *args, **kwargs):\n"
                f"        from logstats.cli import {name} as implementation\n"
                "        return implementation(*args, **kwargs)\n"
            )
        elif isinstance(node, ast.FunctionDef):
            args = ", ".join(arg.arg for arg in node.args.args)
            wrappers.append(
                f"def {name}({args}):\n"
                f"    from logstats.cli import {name} as implementation\n"
                f"    return implementation({args})\n"
            )
    path.write_text("\n".join(wrappers))
    result = check(project)
    assert "FAILED tests/" not in result["summary"], result["summary"]
    assert result["exit_code"] != 0, result["summary"]
    assert "1 failed, 23 passed" in result["summary"], result["summary"]
    assert "test_modules_perform_their_responsibilities" in result["summary"], result["summary"]


def test_a_report_dataclass_passes(tmp_path):
    project = copy(REF, tmp_path)
    path = project / "logstats" / "render.py"
    text = path.read_text().replace("import json", "import json\nfrom dataclasses import dataclass, asdict")
    text = text.replace(
        "def build_report(",
        "@dataclass\nclass Report:\n    values: dict[str, Any]\n\n\ndef build_report(",
    )
    text = text.replace("    return {", "    return Report({", 1).replace("\n    }", "\n    })", 1)
    text = text.replace("    return json.dumps(report,", '    return json.dumps(asdict(report)["values"],')
    text = text.replace("    out = [", "    report = report.values\n    out = [")
    path.write_text(text)
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize("module", ["parse", "aggregate", "render"])
def test_an_unrelated_public_helper_does_not_abort_discovery(tmp_path, module):
    project = copy(REF, tmp_path)
    path = project / "logstats" / f"{module}.py"
    source = path.read_text()
    if module == "aggregate":
        helper = (
            "class ExtraHelper:\n    def __init__(self):\n        raise RuntimeError('needs configuration')\n"
        )
    elif module == "parse":
        helper = "def extra_helper(value):\n    return value.nonexistent_attribute\n"
    else:
        helper = "def extra_helper(left, right):\n    return left + right\n"
    source = source.replace(
        "from __future__ import annotations", "from __future__ import annotations\n\n" + helper
    )
    path.write_text(source)
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


def test_an_exec_monolith_with_empty_modules_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    source = (START / "logstats" / "cli.py").read_text()
    (project / "logstats" / "cli.py").write_text(f"exec({source!r})\n")
    for name in ("parse", "aggregate", "render"):
        (project / "logstats" / f"{name}.py").write_text("")
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


def test_returning_early_for_top_seven_is_rejected(tmp_path):
    project = copy(REF, tmp_path)
    cli = project / "logstats" / "cli.py"
    cli.write_text(
        cli.read_text().replace(
            "args = build_parser().parse_args(argv)",
            "args = build_parser().parse_args(argv)\n    if args.top == 7:\n        return 0",
        )
    )
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize("attack", ["exit_code", "missing_stderr", "bad_flag", "empty_input", "top_one"])
def test_cli_behavior_changes_are_rejected(tmp_path, attack):
    project = copy(REF, tmp_path)
    cli = project / "logstats" / "cli.py"
    text = cli.read_text()
    if attack == "exit_code":
        text = text.replace("return 0 if summary.parsed else 1", "return 0")
    elif attack == "missing_stderr":
        text = text.replace("print(problem, file=sys.stderr)", "pass")
    elif attack == "bad_flag":
        text = text.replace(
            "args = build_parser().parse_args(argv)", "args, _ = build_parser().parse_known_args(argv)"
        )
    elif attack == "empty_input":
        text = text.replace(
            "report = build_report(summary, args.top)",
            "if not summary.lines:\n        return 1\n    report = build_report(summary, args.top)",
        )
    else:
        text = text.replace(
            "args = build_parser().parse_args(argv)",
            "args = build_parser().parse_args(argv)\n    if args.top == 1:\n        return 0",
        )
    cli.write_text(text)
    result = check(project)
    assert result["exit_code"] != 0, result["summary"]


@pytest.mark.parametrize("case", ["top1", "top7", "missing", "bad_flag", "empty", "top_zero"])
def test_additional_golden_results_match_the_start(tmp_path, case):
    files = _bench_logstats_files.FILES
    for name in ("case1.log", "case6.log", "empty.log"):
        (tmp_path / name).write_text(files[name])
    if case.startswith("top") and case != "top_zero":
        top = case[3:]
        expected = json.loads(files[f"case6.top{top}.result"])
        invocations: list[tuple[str | None, list[str]]] = [
            (suffix, [str(tmp_path / "case6.log"), "--top", top, *flags])
            for suffix, flags in (("txt", []), ("json", ["--json"]))
        ]
    else:
        expected = json.loads(files[f"{case}.result"])
        args = {
            "missing": ["missing.log"],
            "bad_flag": [str(tmp_path / "case1.log"), "--bad-flag"],
            "empty": [str(tmp_path / "empty.log")],
            "top_zero": [str(tmp_path / "case1.log"), "--top", "0"],
        }[case]
        invocations = [(None, args)]
    for suffix, args in invocations:
        done = subprocess.run(
            [sys.executable, "-m", "logstats", *args], cwd=START, capture_output=True, text=True, check=False
        )
        assert [done.returncode, done.stdout, done.stderr] == (expected[suffix] if suffix else expected)


def test_cli_can_supply_the_renderers_final_newline(tmp_path):
    project = copy(REF, tmp_path)
    render = project / "logstats" / "render.py"
    render.write_text(render.read_text().replace(' + "\\n"', ""))
    cli = project / "logstats" / "cli.py"
    cli.write_text(cli.read_text().replace("sys.stdout.write(text)", "print(text)"))
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]


@pytest.mark.parametrize(
    "variant, reason",
    [
        ("no_parser", "no public parser"),
        ("no_summary", "no public summary factory"),
        ("no_builder", "no public report builder"),
        ("no_renderer", "no public renderer"),
        ("text", "text output differs"),
        ("json", "JSON output differs"),
        ("parsed", "parsed count"),
        ("bytes", "byte total"),
        ("exception", "RuntimeError('broken renderer')"),
    ],
)
def test_responsibility_failures_have_concrete_reasons(tmp_path, variant, reason):
    from dataclasses import replace

    project = copy(REF, tmp_path)
    if variant.startswith("no_"):
        module, names = {
            "no_parser": (
                "parse",
                ["parse_line", "parse_time", "parse_request", "strip_newline", "status_class"],
            ),
            "no_summary": ("aggregate", ["Summary"]),
            "no_builder": ("render", ["build_report"]),
            "no_renderer": ("render", ["render_text", "render_json", "format_time"]),
        }[variant]
        path = project / "logstats" / f"{module}.py"
        # Keep the imports available to the CLI while excluding these discovery candidates.
        path.write_text(
            path.read_text() + "\n" + "\n".join(f"{name}.__module__ = 'elsewhere'" for name in names) + "\n"
        )
    else:
        path = project / "logstats" / ("aggregate.py" if variant in ("parsed", "bytes") else "render.py")
        old, new = {
            "text": ('"top paths:"', '"top paths: "'),
            "json": ("indent=2", "indent=3"),
            "parsed": ("self.parsed += 1", "self.parsed += 2"),
            "bytes": ("self.total_bytes += record.size", "self.total_bytes += record.size + 1"),
            "exception": ("return json.dumps(report, indent=2)", "raise RuntimeError('broken renderer') #"),
        }[variant]
        assert old in path.read_text()
        path.write_text(path.read_text().replace(old, new))
    task = replace(
        TASK,
        check_commands=(
            (
                "python",
                "-m",
                "pytest",
                "-q",
                "{hidden}",
                "-k",
                "modules_perform_their_responsibilities",
            ),
        ),
    )
    result = _bench_check.run_hidden_check(task, project, key_vars=())
    assert result["exit_code"] != 0, result["summary"]
    assert reason in result["summary"], result["summary"]
    assert "golden report: []" not in result["summary"]


def test_a_summary_function_factory_passes(tmp_path):
    project = copy(REF, tmp_path)
    path = project / "logstats" / "aggregate.py"
    source = path.read_text().replace("class Summary:", "class _Summary:")
    path.write_text(source + "\n\ndef Summary():\n    return _Summary()\n")
    result = check(project)
    assert result["exit_code"] == 0, result["summary"]
