"""The tf-bucket task: the seed, reference, incorrect fixes and shared check image."""

import contextlib
import fcntl
import json
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flyte_agent_factory import _bench_check, _bench_suite, _gates, _images, _timeline
from ops import bench_seed

pytestmark = pytest.mark.slow  # runs terraform or the hidden benchmark check: the slow lane

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "bench"
START, REF = FIXTURES / "tfbucket_start", FIXTURES / "tfbucket_ref"
terraform = pytest.mark.skipif(
    shutil.which("terraform") is None,
    reason="Terraform is not on PATH; coordinator must run these checks with Terraform 1.9.x installed",
)


def committed_tree(task_id: str) -> str:
    """The git tree of the task's committed start: what `ops/bench_seed.py` publishes, and the pin."""
    from pathlib import Path

    from ops import bench_seed

    return bench_seed.tree_id(Path(__file__).parent / "fixtures" / "bench" / bench_seed.STARTS[task_id])


#: At most this many hidden checks run at once across the parallel test workers: each `terraform plan` is
#: CPU-heavy, and a dozen at once ran past the check's own 150s budget while the other workers sat idle.
_TERRAFORM_SLOTS = 2


@contextlib.contextmanager
def _terraform_slot() -> Iterator[None]:
    """Hold one of `_TERRAFORM_SLOTS` lock files shared by every worker process."""
    slots = Path(tempfile.gettempdir()) / f"factory-tests-terraform-{os.getuid()}"
    slots.mkdir(exist_ok=True)
    while True:
        for index in range(_TERRAFORM_SLOTS):
            with open(slots / f"slot-{index}", "w") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                yield
                return
        time.sleep(0.5)


def check(source, tmp_path):
    project = tmp_path / "case"
    shutil.copytree(source, project)
    with _terraform_slot():
        # 300s: a case takes ~50s alone, and a loaded machine (two suites at once) ran past 150s.
        # The check's own 150 s budget assumes an idle check pod; the suite runs it beside the fast lane.
        return _bench_check.run_hidden_check(
            _bench_suite.get("tf-bucket"), project, timeout=660, key_vars=(), terraform_budget_s=600
        )


def test_the_benchmark_keeps_its_150_second_terraform_budget():
    """Only the engine's own tests widen it (BENCH_TF_BUDGET_S); the benchmark's check runs with 150 s."""
    script = _bench_suite.get("tf-bucket").check_files
    text = "\n".join(script.values())
    assert 'time.monotonic() + float(os.environ.get("BENCH_TF_BUDGET_S") or 150)' in text


def test_the_task_is_the_designed_one():
    task = _bench_suite.get("tf-bucket")
    assert (task.kind, task.workdir, task.start_tree) == ("build", "bench/tfbucket", committed_tree(task.id))
    assert (task.cap_usd, task.cap_hours, task.estimate_usd, task.expected) == (40.0, 3.0, 15.0, "accepted")
    assert bench_seed.STARTS[task.id] == START.name
    assert "Terraform is not installed" in task.brief
    assert (REF / "examples" / "basic" / "main.tf").is_file()


def test_terraform_has_a_published_checksum_before_building_the_check_image():
    assert re.fullmatch(r"1\.9\.\d+", _images.TERRAFORM_VERSION)
    assert _images.TERRAFORM_SHA256 != "TODO", "Coordinator: fill in HashiCorp's published SHA256"
    assert re.fullmatch(r"[0-9a-f]{64}", _images.TERRAFORM_SHA256)
    hidden = _bench_suite.get("tf-bucket").check_files["check_tfbucket.py"]
    assert f'version = "{_bench_check.TERRAFORM_PROVIDER_VERSION}"' in hidden


@terraform
def test_the_hidden_check_fails_the_start(tmp_path):
    result = check(START, tmp_path)
    assert result["exit_code"] != 0, result["summary"]
    assert "terraform test" in result["summary"], result["summary"]
    assert "Reference to undeclared resource" in result["summary"], result["summary"]


@terraform
def test_the_hidden_check_passes_the_reference(tmp_path):
    result = check(REF, tmp_path)
    assert result["exit_code"] == 0, result["summary"]
    assert "6 passed, 0 failed" in result["summary"]


@terraform
@pytest.mark.parametrize(
    "variant", ["suspended", "hardcoded_days", "disabled_rule", "wrong_bucket", "filtered_objects"]
)
def test_the_hidden_check_rejects_incorrect_fixes(tmp_path, variant):
    project = tmp_path / "source"
    shutil.copytree(REF, project)
    main = project / "main.tf"
    old, new = {
        "suspended": ('status = "Enabled"', 'status = "Suspended"'),
        "hardcoded_days": ("days = var.expiration_days", "days = 30"),
        "disabled_rule": ('status = "Enabled"', 'status = "Disabled"'),
        "wrong_bucket": ("bucket = aws_s3_bucket.this.bucket", 'bucket = "another-bucket"'),
        "filtered_objects": ("filter {}", 'filter {\n      prefix = "only/"\n    }'),
    }[variant]
    if variant == "disabled_rule":
        old, new = 'status = "Enabled"\n\n    filter', 'status = "Disabled"\n\n    filter'
    assert old in main.read_text()
    main.write_text(main.read_text().replace(old, new, 1))
    result = check(project, tmp_path)
    assert result["exit_code"] != 0, result["summary"]
    assert "terraform test" in result["summary"], result["summary"]


@terraform
@pytest.mark.parametrize("variant", ["separate_file", "empty_local", "empty_variable"])
def test_the_hidden_check_accepts_equivalent_configurations(tmp_path, variant):
    project = tmp_path / "source"
    shutil.copytree(REF, project)
    main = project / "main.tf"
    if variant == "separate_file":
        bucket, retention = main.read_text().split('resource "aws_s3_bucket_versioning"', 1)
        main.write_text(bucket)
        (project / "retention.tf").write_text('resource "aws_s3_bucket_versioning"' + retention)
    else:
        declaration, expression = {
            "empty_local": ('locals {\n  empty_prefix = ""\n}\n', "local.empty_prefix"),
            "empty_variable": ('variable "empty_prefix" {\n  default = ""\n}\n', "var.empty_prefix"),
        }[variant]
        main.write_text(
            declaration
            + "\n"
            + main.read_text().replace("filter {}", "filter {\n      prefix = " + expression + "\n    }")
        )
    subprocess.run(["terraform", "fmt", "-recursive"], cwd=project, check=True, capture_output=True)
    result = check(project, tmp_path)
    assert result["exit_code"] == 0, result["summary"]


@terraform
@pytest.mark.parametrize(
    "variant", ["interpolation", "rule_prefix", "example_days", "module_outputs", "example_output"]
)
def test_the_hidden_check_rejects_qa_attacks(tmp_path, variant):
    project = tmp_path / "source"
    shutil.copytree(REF, project)
    main = project / "main.tf"
    example = project / "examples" / "basic" / "main.tf"
    if variant == "interpolation":
        main.write_text(
            'locals {\n  logs_prefix = "logs/"\n}\n\n'
            + main.read_text().replace("filter {}", 'filter {\n      prefix = "${local.logs_prefix}"\n    }')
        )
    elif variant == "rule_prefix":
        main.write_text(main.read_text().replace("filter {}", 'prefix = "logs/"'))
    elif variant == "example_days":
        example.write_text(example.read_text().replace("expiration_days = 7", "expiration_days = 30"))
    else:
        if variant == "module_outputs":
            (project / "outputs.tf").write_text("")
        example.write_text(example.read_text().split('output "bucket_id"')[0])
    subprocess.run(["terraform", "fmt", "-recursive"], cwd=project, check=True, capture_output=True)
    result = check(project, tmp_path)
    assert result["exit_code"] != 0, result["summary"]
    assert "terraform test" in result["summary"], result["summary"]


@pytest.mark.parametrize(
    "variant",
    [
        "valid",
        "empty_prefix",
        "no_filter",
        "prefix",
        "rule_prefix",
        "tags",
        "and",
        "size",
        "days",
        "outputs",
        "constant_outputs",
        "constant_arn",
        "other_resource",
        "no_expression",
    ],
)
def test_evaluated_plan_checks(tmp_path, monkeypatch, variant):
    # Fake only the CLI boundary so plan assertions run even on offline hosts without Terraform.
    project = tmp_path / "project"
    hidden = tmp_path / "hidden"
    project.mkdir()
    hidden.mkdir()
    script = hidden / "check_tfbucket.py"
    script.write_text(_bench_suite.get("tf-bucket").check_files[script.name])
    monkeypatch.setenv("BENCH_PROJECT", str(project))
    monkeypatch.setenv("BENCH_HIDDEN", str(hidden))
    commands = []

    def terraform_run(argv, **kwargs):
        commands.append(argv[1])
        assert Path(kwargs["env"]["TF_PLUGIN_CACHE_DIR"]).is_dir()
        is_example = kwargs["cwd"] == project / "examples" / "basic"
        rule: dict[str, Any] = {
            "status": "Enabled",
            "prefix": "",
            "filter": [{"prefix": None}],
            "expiration": [{"days": 7 if is_example else 30}],
        }
        lifecycle = {
            "type": "aws_s3_bucket_lifecycle_configuration",
            "values": {"rule": [rule]},
        }
        module = {"resources": [lifecycle]}
        plan: dict[str, Any] = {
            "planned_values": {"root_module": {"child_modules": [module]} if is_example else module},
            "configuration": {
                "root_module": {
                    "outputs": {
                        # Terraform lists the attribute reference and then the resource itself.
                        "bucket_id": {
                            "expression": {"references": ["aws_s3_bucket.this.id", "aws_s3_bucket.this"]}
                        },
                        "bucket_arn": {
                            "expression": {"references": ["aws_s3_bucket.this.arn", "aws_s3_bucket.this"]}
                        },
                    }
                }
            },
        }
        outputs = plan["configuration"]["root_module"]["outputs"]
        if variant == "empty_prefix":
            rule["filter"][0]["prefix"] = ""
        elif variant == "no_filter":
            del rule["filter"]
        elif variant == "prefix":
            rule["filter"][0]["prefix"] = "logs/"
        elif variant == "rule_prefix":
            rule["prefix"] = "logs/"
        elif variant in ("tags", "and", "size"):
            key, value = {
                "tags": ("tags", {"kind": "logs"}),
                "and": ("and", [{}]),
                "size": ("object_size_greater_than", 0),
            }[variant]
            rule["filter"][0][key] = value
        elif variant == "days":
            rule["expiration"][0]["days"] = 30
        elif variant == "outputs":
            plan["configuration"]["root_module"]["outputs"] = {}
        elif variant == "constant_outputs":
            # A constant has `constant_value` and no `references`.
            outputs["bucket_id"] = {"expression": {"constant_value": "WRONG"}}
            outputs["bucket_arn"] = {"expression": {"constant_value": "WRONG"}}
        elif variant == "constant_arn":
            outputs["bucket_arn"] = {"expression": {"constant_value": "WRONG"}}
        elif variant == "other_resource":
            outputs["bucket_id"] = {
                "expression": {"references": ["aws_s3_bucket.other.id", "aws_s3_bucket.other"]}
            }
        elif variant == "no_expression":
            outputs["bucket_id"] = {}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(plan))

    monkeypatch.setattr(subprocess, "run", terraform_run)
    (project / "examples" / "basic").mkdir(parents=True)
    if variant in ("valid", "empty_prefix", "no_filter"):
        runpy.run_path(str(script))
        assert "plan" in commands
        assert "show" in commands
    else:
        with pytest.raises((SystemExit, AssertionError)):
            runpy.run_path(str(script))
    assert not list(project.rglob("zz_bench_override.tf"))


@pytest.mark.real_factory_gates
def test_the_factory_gates_do_not_block_the_start(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    shutil.copytree(START, tmp_path / "tfbucket")
    git("add", "tfbucket")
    git("commit", "-qm", "base")
    _gates.capture_base(tmp_path, "tfbucket")
    assert _gates.run(tmp_path, "tfbucket", "", "", "DONE", {}, _timeline._Timeline()) == ""


@terraform
@pytest.mark.parametrize("broken", [False, True])
def test_solution_test_modules_are_initialized(tmp_path, broken):
    project = tmp_path / "source"
    shutil.copytree(REF, project)
    tests = project / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "example.tftest.hcl").write_text(
        'run "basic_example" {\n  command = plan\n  module {\n    source = "./examples/basic"\n  }\n}\n'
    )
    if broken:
        (project / "examples" / "basic" / "broken.tf").write_text(
            'output "broken" {\n  value = missing.resource.id\n}\n'
        )
    result = check(project, tmp_path)
    assert "Module not installed" not in result["summary"], result["summary"]
    if broken:
        assert result["exit_code"] != 0, result["summary"]
        assert "undeclared resource" in result["summary"], result["summary"]
    else:
        assert result["exit_code"] == 0, result["summary"]
